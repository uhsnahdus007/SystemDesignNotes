# Chapter 8: Apache Spark — From Zero to Performance Tuning

> **Verification status.** Everything marked ✅ **RAN** was executed on **real PySpark 4.0.1** (local mode, Java 21) in a small sandbox (**1 physical core**, ~3 GB RAM; I ran Spark with `local[2]` threads on it). Output is pasted as-is.
> - **Timings are relative, not absolute.** A single-core VM cannot show cluster-scale effects (e.g. skew's straggler *wall-time*); I show the *row distributions and plans* instead, and say so.
> - ⚠️ **NOT RUN:** JDBC (no driver/DB wired to Spark), Delta in Spark (jars unavailable), the Spark UI, and a real 2 TB job. The OOM interview question uses **illustrative** UI metrics I made up to teach the method; they are not from a real run.
> - Sample data: `transactions_df` (51 rows incl. 1 exact duplicate, 2 NULL amounts, 2 orphan users 21/22 not in `users_df`), `users_df` (20), `events_df` (40, JSON properties). Generator code is in A0.

---

## Part A — Concepts + Code

### A0. Sample data (run this first)  ✅ RAN

```python
import random, json, datetime as dt
from pyspark.sql import SparkSession, functions as F, types as T, Window

spark = (SparkSession.builder.master("local[2]").appName("ch8")
         .config("spark.sql.shuffle.partitions", "8").config("spark.sql.session.timeZone", "UTC").getOrCreate())

def make_data(spark):
    random.seed(42)
    cities = [("Pune","IN"),("Mumbai","IN"),("Austin","US"),("Dallas","US"),("Berlin","DE"),("Paris","FR")]
    cats = ["grocery","fuel","dining","travel","luxury"]; merch = ["m_a","m_b","m_c","m_d","m_e","m_f"]
    users = [(u, f"user{u}", dt.date(2023, 1 + u % 12, 1 + u % 28), ["gold","silver","bronze"][u % 3], *cities[u % 6]) for u in range(1, 21)]
    users_df = spark.createDataFrame(users, "user_id int, name string, signup_date date, tier string, city string, country string")
    txns = []
    for i in range(1, 51):
        u = random.randint(1, 22); c = cities[u % 6]                       # users 21,22 do NOT exist in users_df (orphans)
        amt = None if i % 17 == 0 else round(random.random() * 400, 2)     # a few NULL amounts
        txns.append((i, u, amt, random.choice(cats), dt.date(2024, 3, 1) + dt.timedelta(days=random.randint(0, 40)), random.choice(merch), c[0], c[1]))
    txns.append(txns[4])                                                    # one exact duplicate row
    txn_df = spark.createDataFrame(txns, "txn_id int, user_id int, amount double, category string, txn_date date, merchant string, city string, country string")
    ev, types_ = [], ["login","view","add_to_cart","purchase","logout"]
    for i in range(1, 41):
        u = random.randint(1, 8); t = dt.datetime(2024, 3, 1, 9, 0) + dt.timedelta(minutes=random.randint(0, 600))
        props = json.dumps({"device": random.choice(["ios","android","web"]),
                            "page": {"url": f"/p/{random.randint(1,5)}", "ref": random.choice(["google","direct",None])},
                            "items": [{"sku": f"S{random.randint(1,9)}", "qty": random.randint(1,3)} for _ in range(random.randint(0,3))]})
        ev.append((i, u, random.choice(types_), t, props))
    ev_df = spark.createDataFrame(ev, "event_id int, user_id int, event_type string, event_timestamp timestamp, properties_json string")
    return txn_df, users_df, ev_df
txn, users, ev = make_data(spark)
```
`transactions=51 users=20 events=40`

### A1. Spark Architecture  ✅ RAN (config) / diagrams are conceptual

```
                    ┌────────────────────────── Cluster ──────────────────────────┐
  Your code         │                                                              │
 ┌───────────┐      │   ┌─────────────┐   resources   ┌─────────────────────┐     │
 │  Driver   │──────┼──►│  Cluster    │──────────────►│  Worker node 1      │     │
 │ SparkSession│    │   │  Manager    │               │   Executor (JVM)    │     │
 │ DAG/Catalyst│    │   │ (YARN, K8s, │               │    ├ Task (core 1)  │     │
 │ schedules   │◄───┼───┤ Standalone, │               │    ├ Task (core 2)  │     │
 │ tasks       │ results│ Databricks) │               │    └ cache/shuffle  │     │
 └───────────┘      │   └─────────────┘               ├─────────────────────┤     │
                    │                                  │  Worker node 2 ...  │     │
                    └──────────────────────────────────┴─────────────────────┴─────┘
```
- **Driver:** runs your `main`, holds the `SparkSession`, builds the logical/physical plan, splits it into **jobs → stages → tasks**, and collects results. `collect()` brings *everything* to the driver (OOM risk).
- **Executors:** JVM processes on workers that run tasks, hold cached data, and write shuffle files.
- **Cluster manager:** allocates executors (YARN, Kubernetes, Standalone, Mesos-legacy; Databricks manages it for you).
- **Hierarchy:** an **action** triggers a **job**; a job splits at shuffle boundaries into **stages**; a stage = one **task per partition**.

| | Local mode | Cluster mode |
|---|---|---|
| Where | One JVM, N threads (`local[4]`) | Driver + many executors |
| Use | Dev, unit tests (this chapter) | Production |
| Fails like | OOM at driver memory | Executor loss, shuffle fetch failures, skew stragglers |

```python
spark = (SparkSession.builder
    .master("local[2]")                                     # prod: omit, let spark-submit/cluster set it
    .appName("ch8")
    .config("spark.sql.shuffle.partitions", "8")
    .config("spark.sql.adaptive.enabled", "true")
    .config("spark.executor.memory", "4g").config("spark.executor.cores", "4")   # ignored in local mode
    .getOrCreate())
spark.conf.set("spark.sql.autoBroadcastJoinThreshold", 20 * 1024 * 1024)        # runtime-changeable SQL configs
```
```
master: local[2] | defaultParallelism: 2 | version: 4.0.1
  spark.sql.shuffle.partitions = 8              (default is 200)
  spark.sql.adaptive.enabled = true             (AQE is ON by default in Spark 3.2+)
  spark.sql.autoBroadcastJoinThreshold = 10485760b   (10 MB)
```
> **COMMON MISTAKE:** Setting `spark.executor.memory`/`cores` via `spark.conf.set` *after* the session starts: they're launch-time settings and silently ignored. SQL configs (`spark.sql.*`) can be changed at runtime.

### A2. RDD Basics  ✅ RAN

```python
sc = spark.sparkContext
rdd = sc.parallelize(range(1, 11), 3)
rdd.map(lambda x: x * x).filter(lambda x: x % 2 == 0).reduce(lambda a, b: a + b)          # 4+16+36+64+100 = 220
sc.parallelize(["a b a", "b c", "a"]).flatMap(lambda l: l.split()).map(lambda w: (w, 1)).reduceByKey(lambda a, b: a + b).collect()
sc.textFile("/tmp/r.txt").collect();  rdd.glom().collect()
```
```
220 | partitions: 3
word count: [('a', 3), ('b', 2), ('c', 1)]
glom (rows per partition): [[1, 2, 3], [4, 5, 6], [7, 8, 9, 10]]
```
**Why DataFrames replaced RDDs:** an RDD is an opaque collection of Python/JVM objects, so Spark can't see inside your lambda: no column pruning, no predicate pushdown, no join reordering, Python objects serialized per row. A DataFrame carries a **schema** and an expression tree, so the **Catalyst optimizer** can rewrite the plan and **Tungsten** can use compact binary rows and generated code (A8). Use RDDs only for truly unstructured/custom-partitioned logic. *Transformations* (`map`, `filter`, `flatMap`) are lazy; *actions* (`collect`, `reduce`, `count`) run the job. `reduceByKey` beats `groupByKey` because it combines map-side before shuffling.

### A3. DataFrame API  ✅ RAN

**Creating DataFrames**
```python
spark.createDataFrame(rows, "col1 int, col2 string")                    # from a list, with a DDL-string schema
spark.read.option("header", True).csv(path);  spark.read.parquet(path);  spark.read.json(path)
spark.read.format("delta").load(path)                                   # ⚠️ not run (needs the Delta package)
spark.read.format("jdbc").option("url", ...).option("dbtable", ...).load()   # ⚠️ not run (see A6)
```
**Explicit vs inferred schema:** always give a schema in production (`inferSchema` scans the data an extra time and can guess wrong). `StructType([StructField("id", IntegerType(), False), ...])` or a DDL string `"id int, name string"`.

**Select / filter / withColumn / when / cast**
```python
(txn.filter((F.col("amount") > 300) & F.col("country").isin("IN", "US"))
    .withColumn("size", F.when(F.col("amount") > 380, "XL").when(F.col("amount") > 340, "L").otherwise("M"))
    .withColumn("amount_int", F.col("amount").cast("int")).select("txn_id", "amount", "amount_int", "size", "country").orderBy("txn_id").show(5))
```
```
|txn_id|amount|amount_int|size|country|
|    13|342.13|       342|   L|     US|
|    19|322.02|       322|   M|     US|
|    20|365.25|       365|   L|     IN|
|    24|398.93|       398|  XL|     US|
|    25|302.31|       302|   M|     US|
```
> **COMMON MISTAKE:** Python `and`/`or`/`not` on columns raise errors; use `&`, `|`, `~` **with parentheses** (operator precedence).

**NULL handling**
```
null amounts: 2 | na.drop rows: 49 | na.fill(0) null count: 0
NULL comparison trap: amount>100: 38 + amount<=100: 11 = 49, but table has 51 rows; the 2 NULL rows match NEITHER filter
```
`NULL > 100` is `NULL` (not true, not false), so filters silently drop NULL rows. Use `isNull()/isNotNull()`, `coalesce()`, `eqNullSafe()`, `na.fill`.

**String & date functions**
```python
users.select("name", F.upper("name"), F.concat_ws("-", "city", "country"), F.substring("name", 1, 4),
             F.regexp_extract("name", r"(\d+)", 1), F.regexp_replace("name", "user", "u_"), F.split("city", "u"))
```
```
|name  |up    |loc      |sub |num|rep |split    |
|user1 |USER1 |Mumbai-IN|user|1  |u_1 |[M, mbai]|
|user10|USER10|Berlin-DE|user|10 |u_10|[Berlin] |
```
```python
F.date_add("txn_date", 7); F.datediff(F.lit("2024-06-01").cast("date"), "txn_date"); F.months_between(...); F.date_trunc("month", "txn_date")
F.to_date(F.lit("03/15/2024"), "MM/dd/yyyy"); F.current_date(); F.date_sub(...); F.to_timestamp(...)
```
```
txn_date=2024-03-16 | plus7=2024-03-23 | days_to_jun1=77 | months=2.51612903 | month_start=2024-03-01 00:00:00 | parsed=2024-03-15
```
**Complex types: structs, arrays, maps, JSON**
```python
schema = T.StructType([T.StructField("device", T.StringType()),
    T.StructField("page", T.StructType([T.StructField("url", T.StringType()), T.StructField("ref", T.StringType())])),
    T.StructField("items", T.ArrayType(T.StructType([T.StructField("sku", T.StringType()), T.StructField("qty", T.IntegerType())])))])
parsed = ev.withColumn("p", F.from_json("properties_json", schema))
parsed.select("event_id", "p.device", F.col("p.page.url").alias("url"), F.size("p.items").alias("n_items")).show(4)
```
```
|event_id| device| url|n_items|
|       1|    ios|/p/1|      0|
|       2|    ios|/p/2|      3|
|       3|android|/p/3|      3|
|       4|    ios|/p/1|      3|
```
```
explode_outer rows: 79   (events with 0 items kept with NULL sku)   |   plain explode(): 71 rows   <- 8 events with empty arrays DISAPPEARED
get_json_object("$.page.url") -> '/p/1', '/p/2'
```
```python
parsed.groupBy("p.device").agg(F.collect_set("p.page.url").alias("urls"), F.count("*").alias("n"))     # collect_list keeps dups, collect_set dedupes
users.select(F.create_map(F.lit("city"), "city", F.lit("tier"), "tier").alias("m")).select(F.map_keys("m"), F.element_at("m", "tier"))
parsed.filter(F.array_contains(F.transform("p.items", lambda x: x.sku), "S3")).count()     # 6 events contain sku S3
users.select(F.struct("city", "country").alias("loc")).select("loc.city")
```
```
| device|distinct_urls|  n|
|android|            4|  8|
|    ios|            5| 23|
|    web|            5|  9|
```
> **COMMON MISTAKE:** `explode()` drops rows whose array is empty/NULL (silently losing 8 of 40 events above). Use `explode_outer()` when you need to keep them. Also `posexplode()` adds the element index. After `groupBy`, you must reference a nested field as `"p.device"` (I first wrote `"device"` and got `UNRESOLVED_COLUMN`).

**Aggregations, pivot, windows**
```python
txn.na.drop().groupBy("country").agg(F.count("*").alias("n"), F.round(F.sum("amount"), 2).alias("total"), F.round(F.avg("amount"), 2).alias("avg"))
txn.na.drop().groupBy("country").pivot("category", ["grocery", "fuel", "dining"]).agg(F.round(F.sum("amount")))    # list the pivot values: avoids an extra pass
```
```
|country|  n|  total|   avg|          |country|grocery|  fuel|dining|
|     DE|  5|1274.63|254.93|          |     DE|   37.0|  NULL|  NULL|
|     FR|  6|1172.02|195.34|          |     FR|   NULL| 181.0|  NULL|
|     IN| 18|4045.02|224.72|          |     IN| 1232.0| 445.0| 823.0|
|     US| 20|4166.68|208.33|          |     US| 1711.0|1074.0| 299.0|
```
```python
w = Window.partitionBy("user_id").orderBy("txn_date", "txn_id")
df.select("user_id", "txn_date", "amount", F.row_number().over(w).alias("rn"), F.lag("amount").over(w).alias("prev"), F.round(F.sum("amount").over(w), 2).alias("running"))
```
```
|user_id|  txn_date|amount| rn|  prev|running|
|      2|2024-03-18|322.02|  1|  NULL| 322.02|
|      2|2024-04-02|343.85|  2|322.02| 665.87|
|      3|2024-03-10|302.31|  1|  NULL| 302.31|
|      3|2024-03-25|342.13|  2|302.31| 644.44|
```
Ties: `[(10, rank=1, dense=1, rn=1), (10, rank=1, dense=1, rn=2), (5, rank=3, dense=2, rn=3)]`. `rank` leaves gaps, `dense_rank` doesn't, `row_number` is always unique (arbitrary among ties unless you add a tie-breaker). With an `orderBy` and no frame, the default is `RANGE UNBOUNDED PRECEDING..CURRENT ROW`; use `rowsBetween` explicitly when you mean rows.

**UDFs: Python vs Pandas (vectorized) vs built-in**  ✅ RAN (1,000,000 rows)
```python
@udf("double")
def py_udf(x): return round(x * 0.18, 2)                                  # row-at-a-time: JVM -> Python pickle -> JVM
@pandas_udf("double")
def pd_udf(x: pd.Series) -> pd.Series: return (x * 0.18).round(2)         # batches via Arrow
F.round(F.col("amount") * 0.18, 2)                                         # built-in: runs inside the JVM, optimized
```
```
built-in expression :   1.88 s
Python UDF          :   7.51 s   (4x slower than built-in)
pandas (vectorized) :   3.60 s   (2.1x faster than Python UDF)
plan: Python UDF -> BatchEvalPython ;  pandas UDF -> ArrowEvalPython
```
> **KEY INSIGHT:** The order is **built-in ≫ pandas UDF > Python UDF**. Even a pandas UDF is slower than a built-in because the data crosses the JVM↔Python boundary, and a UDF is a **black box**: Catalyst can't push filters through it or optimize it. Reach for UDFs last; first look for a built-in (`create_map`, `transform`, `regexp_*`, `when`). (The 4× here is on a tiny single-core VM; real gaps are often 10×+ and it also wastes executor memory.)

### A4. Joins  ✅ RAN (real `EXPLAIN` output, edited for width)

**1. Broadcast hash join: small × big, no shuffle of the big side.** Spark broadcasts a table below `autoBroadcastJoinThreshold` (default 10 MB), or you force it with a hint:
```
Hint: txn.join(F.broadcast(users), "user_id")
  BroadcastHashJoin [user_id], [user_id], Inner, BuildRight
  +- BroadcastExchange HashedRelationBroadcastMode(...)
```
With AQE on, a join Spark *initially planned* as sort-merge was **converted at runtime** to a broadcast join after it saw the real shuffle sizes (final plan for `txn.join(users, "user_id")`):
```
BroadcastHashJoin [user_id], [user_id], Inner, BuildLeft
 :- BroadcastQueryStage 2
 :  +- BroadcastExchange ...
```
**2. Sort-merge join: default for large × large.** Both sides are shuffled by key and sorted:
```
SortMergeJoin [user_id], [user_id], Inner
 :- Sort [user_id ASC NULLS FIRST]
 :  +- Exchange hashpartitioning(user_id, 8)
 +- Sort [user_id ASC NULLS FIRST]
    +- Exchange hashpartitioning(user_id, 8)
```
**3. Shuffle hash join** (hint `SHUFFLE_HASH`): shuffle both, build a hash table on the smaller side, no sort:
```
ShuffledHashJoin [user_id], [user_id], Inner, BuildRight
 :- Exchange hashpartitioning(user_id, 8)
 +- Exchange hashpartitioning(user_id, 8)
```
| Join | Shuffle? | Sort? | Use when | Risk |
|---|---|---|---|---|
| Broadcast hash | none (small side broadcast) | no | one side ≪ executor memory | Driver/executor OOM if "small" is not small |
| Sort-merge | both sides | both | both large (default) | Skew → straggler tasks |
| Shuffle hash | both sides | no | one side much smaller but too big to broadcast | Hash table OOM on a skewed key |
Look for `Exchange` (= shuffle, the expensive thing) and the join node in every plan.

**COMMON MISTAKE: joining on NULLable columns**  ✅ RAN
```
a: [(1,'x'), (NULL,'y'), (NULL,'z')]    b: [(1,'p'), (NULL,'q')]
inner join on k = k            : 1 row   (NULL never equals NULL)
null-safe  eqNullSafe (<=>)    : 3 rows  (1=1 matches, AND NULL<=>NULL: 2 x 1 = 2 more matches)
anti-join (txn users missing from users_df): [21, 22]
inner join silently dropped 7 orphan transaction rows; left join keeps them with NULL tier
```
Decide deliberately: do NULL keys mean "unknown" (should *not* match; the default is right, but then you lose those rows on an inner join) or "same missing value" (use `eqNullSafe`)? After a join, **always compare row counts** and use `left_anti` to inspect what didn't match.

**Skew and salting**  ✅ RAN (85% of rows share one key; 300,000 rows)
```
naive join : rows per shuffle partition (task) = [261917, 7645, 7271, 6231, 5369, 4403, 3607, 3557]   <- one task does 87% of the work
salted (x8): rows = 300,000 (same result)  |  rows per partition = [69886, 68433, 38732, 38295, 37430, 37122, 5097, 5005]
```
```python
SALT = 8
facts_s = facts.withColumn("salt", (F.rand(seed=5) * SALT).cast("int")).withColumn("skey", F.concat_ws("_", "key", "salt"))
dim_s   = dim.withColumn("salt", F.explode(F.array([F.lit(i) for i in range(SALT)]))).withColumn("skey", F.concat_ws("_", "key", "salt"))
salted  = facts_s.join(dim_s, "skey")       # hot key is split into 8 sub-keys; dim rows replicated x8
```
> **Honest caveat:** my single-core sandbox timings (4.6 s vs 3.1 s) don't demonstrate the production effect. On a cluster the *largest task* sets the stage time, so cutting 262 K → 70 K rows in the biggest task is the win. The cost is replicating the dimension ×SALT and a bigger shuffle, so **salt only when needed, or isolate the hot key** (broadcast-join the hot keys separately, normal join for the rest). AQE has a built-in skew-join optimization (`spark.sql.adaptive.skewJoin.enabled`, on by default, which splits oversized shuffle partitions); I tried to confirm it in this tiny dataset and **my check was inconclusive**, so I'm not claiming a demonstrated result.

### A5. Spark SQL  ✅ RAN
```python
txn.createOrReplaceTempView("txn"); users.createOrReplaceTempView("users")
spark.sql("""SELECT u.tier, COUNT(*) AS n, ROUND(SUM(t.amount),2) AS total FROM txn t JOIN users u USING (user_id)
             WHERE t.amount IS NOT NULL GROUP BY u.tier ORDER BY total DESC""").show()
spark.sql("SELECT * FROM users").filter("country = 'IN'").withColumn("tag", F.lit("india"))      # SQL and API compose freely
```
```
|  tier|  n|  total|
|silver| 14|3226.09|
|bronze| 16|3095.59|
|  gold| 12|3069.75|
mixed SQL + API -> 7 rows
```
Both compile to the same logical plan and the same optimizer, so there's **no performance difference**. **SQL is cleaner** for joins, CTEs, window-heavy reports, and for analysts. **The API is cleaner** for programmatic/dynamic logic (loops over columns, reusable functions, unit-testable transformations, type-safe composition). Temp views are session-scoped (`createGlobalTempView` for cross-session).

### A6. Reading & Writing Data  ✅ RAN (except JDBC/Delta)

**CSV options.** A semicolon-delimited, quoted, NA-containing file:
```
id;name;note;score
1;"Smith; John";"He said ""hi""";NA
2;Ann;multi
3;Bob;ok;7
```
```
defaults (comma delimiter)  -> 4 rows, columns: ['_c0']                                   <- everything in one column, header treated as data
header, sep=';', nullValue='NA', quote='"', escape='"', inferSchema -> 3 rows, id:int name:string note:string score:int
|id |name       |note        |score|
|1  |Smith; John|He said "hi"|NULL |           <- quoted delimiter kept; "" unescaped; NA -> NULL
|2  |Ann        |multi       |NULL |           <- short row: PERMISSIVE pads with NULL
|3  |Bob        |ok          |7    |
DROPMALFORMED: 3 rows | FAILFAST -> raises (Malformed CSV record: 2;Ann;multi)
```
Modes: `PERMISSIVE` (default: keep, NULL-fill, optionally route the raw line to `columnNameOfCorruptRecord`), `DROPMALFORMED` (silently drop; dangerous), `FAILFAST` (stop the job). For ingestion, use PERMISSIVE + corrupt-record column, then quarantine bad rows.

**JSON corrupt records & multiline**
```
|_corrupt        |id  |v   |
|NULL            |1   |a   |
|{"id":2,"v":    |NULL|NULL|      <- the broken line is preserved for inspection
|NULL            |3   |c   |
multiline=false -> ['_corrupt_record']   |   multiline=true -> ['id', 'tags']       (pretty-printed JSON needs multiLine=true)
```
**Parquet: column pruning + predicate pushdown** (2 M rows, 4 columns; real plan lines):
```
select * where grp=7         ReadSchema=struct<id,grp,val,pad>   PushedFilters=[IsNotNull(grp), EqualTo(grp,7)]
select val where id < 1000   ReadSchema=struct<id,val>           PushedFilters=[IsNotNull(id), LessThan(id,1000)]
read 4 columns 0.93s vs 1 column 0.34s
```
Spark reads only the needed columns and pushes filters into the Parquet reader (skipping row groups via min/max stats; see Chapter 10).
**Parquet schema evolution:** `default read columns: ['id','a','p']` vs `mergeSchema=true: ['id','a','newcol','p']`. Without `mergeSchema` Spark samples one file's schema and silently ignores `newcol`.

**Partitioned writes + pruning**
```python
txn.withColumn("year", F.year("txn_date")).withColumn("month", F.month("txn_date")).write.partitionBy("year", "month").parquet(path)
```
```
directories: ['year=2024/month=3', 'year=2024/month=4']
filter month=3 plan -> PartitionFilters: [isnotnull(month), (month = 3)]       <- only matching directories are read
```
> **COMMON MISTAKE:** partitioning by a high-cardinality column (`user_id`) creates millions of tiny files/directories. Partition by low-cardinality columns that queries filter on (date), keep partitions ≥ ~128 MB.

**Bucketing: pre-shuffled joins** (two 8-bucket tables on `user_id`, broadcast off):
```
bucketed tables : Exchange nodes = 0
plain DataFrames: Exchange nodes = 2          <- the shuffles bucketing removed
```
Bucketing pays the shuffle once at write time so repeated joins/aggregations on that key skip it. Needs the same bucket count/key on both sides and `saveAsTable` (metastore); less used now that AQE/Delta/Z-order cover many cases.

**Delta & JDBC** ⚠️ NOT RUN
```python
df.write.format("delta").mode("overwrite").saveAsTable("sales.txn")           # Delta details: Chapter 9
(spark.read.format("jdbc").option("url", "jdbc:postgresql://host/db").option("dbtable", "public.orders")
   .option("partitionColumn", "id").option("lowerBound", 1).option("upperBound", 50_000_000).option("numPartitions", 16)   # 16 parallel range queries
   .option("fetchsize", 10_000).load())
(df.write.format("jdbc").option("url", ...).option("dbtable", "tgt").option("batchsize", 10_000).mode("append").save())
```
Without `partitionColumn`/`numPartitions` a JDBC read is **one task on one connection**. `lowerBound/upperBound` only set the partition stride; they don't filter. Writes: tune `batchsize`, limit parallelism (each partition opens a DB connection; see pooling in Chapter 10), and prefer staging + MERGE for idempotency.

### A7. Performance Tuning

**Spark UI walkthrough** ⚠️ not run (conceptual). **Jobs** tab: one row per action; **Stages**: split at shuffles; click a stage → **Tasks** table. Look for: (1) task **duration/Input size distribution**: max ≫ median = skew; (2) **Shuffle Read/Write** sizes and **Spill (memory/disk)**: spill = not enough execution memory or too few partitions; (3) **GC time** > ~10% of task time; (4) failed/retried tasks and executor loss; (5) **SQL tab** plan with metrics ("number of output rows", "data size"); (6) **Storage** tab for cached data and **Executors** tab for memory/GC per executor.

**Key configs and when to change them**

| Config | Default | Change when |
|---|---|---|
| `spark.sql.shuffle.partitions` | 200 | Tune to ~2–3× total cores or target 100–200 MB per shuffle partition; with AQE on, set it high and let AQE coalesce |
| `spark.sql.autoBroadcastJoinThreshold` | 10 MB | Raise (e.g. 50–200 MB) if executors have memory; `-1` to disable when broadcast causes OOM |
| `spark.executor.memory` / `spark.executor.cores` | 1g / 1 (cluster-dependent) | ~4–5 cores/executor is a common sweet spot; more cores = more tasks sharing memory |
| `spark.driver.memory` | 1g | Raise if you `collect()`, broadcast big tables (the driver builds them), or have huge plans |
| `spark.sql.adaptive.enabled` | true (3.2+) | Leave on |
| `spark.memory.fraction` | 0.6 | Unified execution+storage memory; rarely tuned first |
| `spark.sql.files.maxPartitionBytes` | 128 MB | Controls input split size → number of read tasks |

**AQE (Adaptive Query Execution)** ✅ RAN: re-optimizes using **runtime statistics**: (1) **coalesce shuffle partitions**; (2) **switch join strategy** (sort-merge → broadcast, shown in A4); (3) **skew join** (split big partitions); (4) **dynamic partition pruning** (prune fact partitions using the dimension's filtered keys at runtime, e.g. a star-schema join filtering on `dim.region`).
```
AQE off: post-shuffle partitions = 200
AQE on : final plan contains AQEShuffleRead coalesced : True
```
AQE shrank 200 mostly-empty shuffle partitions into a few reasonable ones for a 50-group aggregation. (Look for `AQEShuffleRead` / `isFinalPlan=true` in `explain()` *after* running the query; the pre-execution plan shows the initial plan.)

**Data skew**: *detect:* Spark UI stage with a few tasks running far longer/processing far more bytes than the median; `groupBy(key).count().orderBy(desc)`. *Fix:* AQE skew join; **salting** ✅ (A4); broadcast the smaller side; isolate the hot key (filter → special-case join → union); pre-aggregate before the join; filter NULL keys (a classic hot key!).

**Small files: `coalesce` vs `repartition`** ✅ RAN
```
repartition(50) -> 50 files | coalesce(2) -> 2 files | repartition(2,'p').partitionBy('p') -> 4 files
repartition(4) -> 4 partitions ; coalesce(8) on it -> 4 (coalesce cannot increase)
```
| | `coalesce(n)` | `repartition(n)` |
|---|---|---|
| Shuffle | No (narrow, merges partitions) | Yes (full shuffle) |
| Increase partitions | No | Yes |
| Balance | Can be uneven | Even (round-robin/hash) |
| Use | Cheap reduction before write | Fix skew, increase parallelism, repartition by key |
**Causes of small files:** too many shuffle partitions (200 tasks each writing a file), streaming micro-batches, over-partitioned writes, many tiny source files. **Fixes:** `coalesce`/`repartition` before write, AQE coalescing, `maxRecordsPerFile`/target-size writes, scheduled **compaction** (Delta `OPTIMIZE`, Chapter 9).

**Caching** ✅ RAN
```
DataFrame.cache()    -> Disk Memory Deserialized 1x Replicated   (MEMORY_AND_DISK)
persist(MEMORY_ONLY) -> Memory Serialized 1x Replicated
persist(DISK_ONLY)   -> Disk Serialized 1x Replicated
persist(MEMORY_AND_DISK_2) -> Disk Memory Serialized 2x Replicated
one-time use: uncached 0.63 s vs cached 0.79 s  -> caching didn't help (it costs a pass to fill the cache)
```
`cache()` is `persist(MEMORY_AND_DISK)` for DataFrames. **Cache when** a DataFrame is reused by several actions/branches and is expensive to recompute (and fits). **Don't cache** one-pass pipelines, huge data that spills, or data that's cheap to re-read; always `unpersist()`. Caching is lazy: the first action fills it; cached data is lost with the executors.

**Catalyst optimizer** ✅ RAN: pipeline = **Parsed → Analyzed → Optimized logical → Physical plan** (cost-based choices like join strategy). Real output for `SELECT id, 1+2 AS c FROM (SELECT * FROM t WHERE id > 3) WHERE id < 8 AND 1 = 1`:
```
== Parsed Logical Plan ==              == Optimized Logical Plan ==       == Physical Plan ==
'Project ['id, (1 + 2) AS c]           Project [id, 3 AS c]               *(1) Project [id, 3 AS c]
+- 'Filter (('id < 8) AND (1 = 1))     +- Filter ((id > 3) AND (id < 8))  +- *(1) Filter ((id > 3) AND (id < 8))
   +- 'SubqueryAlias ...                  +- Range (0, 10, step=1)           +- *(1) Range (0, 10, step=1)
      +- 'Project [*]
         +- 'Filter ('id > 3)
```
The optimizer **constant-folded** `1+2` → `3`, **removed** `1 = 1`, **merged** two filters, and collapsed the subquery. Other rules: predicate/projection pushdown, join reordering, column pruning. **Reading `explain()`:** read bottom-up (data flows from the scan up); look for `Exchange` (shuffle), `BroadcastHashJoin` vs `SortMergeJoin`, `PushedFilters`, `PartitionFilters`, `BatchEvalPython` (UDF) and `*(n)` (whole-stage codegen).
**Tungsten:** off-heap/compact binary row format, cache-friendly memory layout, and **whole-stage code generation** (the `*(1)` markers: fuses operators into one JVM loop, avoiding virtual calls). Python UDFs break this fusion, one more reason they're slow.

**A slow job, optimized step by step**  ✅ RAN (1.5 M-row fact table with 8 columns + 5,000-row dimension; 120 days of data)

*BEFORE: five problems:*
```python
@udf("string")
def region_udf(c): return REGION.get(c, "OTHER")                                     # (3) Python UDF for a simple mapping
f = spark.read.parquet(facts); d = spark.read.parquet(dim)                          # (1) reads all 8 columns
j = f.join(d, "merchant_id")                                                         # (2) shuffle join, 200 partitions, no broadcast (threshold = -1)
j = j.withColumn("region", region_udf("country")).filter("txn_date >= '2024-03-01'") # (3)+(1) UDF over every row; filter late
agg = j.groupBy("region", "category", "risk").agg(F.sum("amount"), F.count("*"))
n = agg.count(); top = agg.orderBy(F.desc("total")).limit(3).collect()               # (4) 2 actions = full recompute twice
agg.repartition(200).write.parquet(out)                                              # (5) 200 tiny files, and a 3rd recompute
```
*AFTER:*
```python
f = spark.read.parquet(facts).select("merchant_id","txn_date","amount","category","country").filter("txn_date >= '2024-03-01'")  # prune + filter early
d = spark.read.parquet(dim).select("merchant_id", "risk")
j = f.join(F.broadcast(d), "merchant_id")                                            # broadcast the small side
region = F.create_map(*[x for kv in REGION.items() for x in (F.lit(kv[0]), F.lit(kv[1]))])
j = j.withColumn("region", F.coalesce(F.element_at(region, F.col("country")), F.lit("OTHER")))    # built-in, no Python
agg = j.groupBy(...).agg(...).cache()                                                # reused by 3 consumers
agg.coalesce(1).write.parquet(out)                                                   # 1 file
spark.conf.set("spark.sql.shuffle.partitions", "8")                                  # right-sized for the data
```
| Metric | BEFORE | AFTER |
|---|---|---|
| Wall time (1-core sandbox) | **153.9 s** | **3.9 s** (**~40×**) |
| Plan | `BatchEvalPython`, `SortMergeJoin`, 3 × `Exchange` | `BroadcastHashJoin`, no Python eval, filter pushed to Parquet (`PushedFilters: txn_date >= 2024-03-01`) |
| Output files | 72 (`repartition(200)` of an 84-row result: only 72 partitions were non-empty, each wrote its own tiny file) | 1 |
| Results | 84 groups, top (APAC, grocery) | identical (sum of totals equal: True) |
> **Caveats:** the 40× comes from *combining* all fixes on a tiny VM where 200 shuffle partitions and Python UDF overhead dominate; I didn't isolate each fix, so don't quote per-fix numbers. The *direction* and the plan changes are what generalize. AQE was disabled in the BEFORE run to reproduce the classic misconfiguration (with AQE on, several of those problems would be partly mitigated).

---

## Part B — Practice Problems (13)

*Try each 15 min first. ✅ = solution code was run.*

### Easy (1–5)

**1. [Amazon] Top countries.** Total non-NULL spend per country, highest first.
- *Solution:* `txn.na.drop(subset=["amount"]).groupBy("country").agg(F.sum("amount").alias("total")).orderBy(F.desc("total"))` ✅ (IN→US→DE→FR as shown in A3).
- *Follow-up:* "Why not `fillna(0)`?" → it would count NULLs in `avg`/`count`, skewing metrics; choose per metric.

**2. [Meta] Find orphans.** Which `user_id`s in transactions don't exist in users?
- *Solution:* `txn.join(users, "user_id", "left_anti").select("user_id").distinct()` ✅ → `[21, 22]`.
- *Follow-up:* "Why did my inner join lose rows?" → it dropped 7 orphan rows silently.

**3. [Google] Dedupe.** Remove exact duplicates and check the count. 
- *Solution:* `txn.dropDuplicates()` (51 → 50) or `dropDuplicates(["txn_id"])`. 
- *Follow-up:* "Which duplicate survives?" → arbitrary; use a window with an ordering to choose (Problem 9).

**4. [Uber] First/last event per user.** 
- *Solution:* `ev.groupBy("user_id").agg(F.min("event_timestamp"), F.max("event_timestamp"))`; for whole rows use `row_number()` over `partitionBy("user_id").orderBy(...)`.
- *Follow-up:* "Why not `first()`?" → non-deterministic without ordering.

**5. [Netflix] Running total per user.** 
- *Solution:* `F.sum("amount").over(Window.partitionBy("user_id").orderBy("txn_date","txn_id"))` ✅ (running 322.02 → 665.87 in A3).
- *Follow-up:* "Ties in `txn_date`?" → add `txn_id` as a tie-breaker or ties are summed together (RANGE frame).

### Medium (6–10)

**6. [Airbnb] Parse nested JSON events into a flat DataFrame.**
- *Solution:* `from_json` with an explicit schema, select struct fields, `explode_outer` the `items` array, then select `it.sku, it.qty` ✅ (79 rows with `explode_outer`; plain `explode` would produce 71 and lose 8 events).
- *Follow-up:* "Schema evolves; new keys appear." → store raw JSON in bronze, use a permissive schema + `_corrupt_record`, evolve explicitly; avoid `inferSchema` on production reads.

**7. [Stripe] Sessionization with window functions** ✅
```python
w = Window.partitionBy("user_id").orderBy("ts")
s = (e.withColumn("gap_min", (F.col("ts").cast("long") - F.lag("ts").over(w).cast("long")) / 60)
      .withColumn("new_session", F.when(F.col("gap_min").isNull() | (F.col("gap_min") > 30), 1).otherwise(0))
      .withColumn("session_id", F.sum("new_session").over(w.rowsBetween(Window.unboundedPreceding, Window.currentRow))))
s.groupBy("user_id", "session_id").agg(F.min("ts").alias("start"), F.max("ts").alias("end"), F.count("*").alias("events"))
```
```
|a|1|10:00:00|10:10:00|2|10.0   |      |b|1|09:00:00|09:29:59|2|29.98 |
|a|2|10:50:00|10:55:00|2|5.0    |      |b|2|10:00:00|10:00:00|1|0.0   |
|a|3|13:00:00|13:00:00|1|0.0    |
```
Technique: **lag → gap → flag new session (gap > 30 min or first) → cumulative sum = session id.** User b's 09:29:59 stays in session 1 (< 30 min gap); the 30-min boundary is `>`, so say which you mean. Spark also has a built-in `F.session_window("ts", "30 minutes")` ✅ (groups events and gives `start = first event`, `end = last event + 30 min`; works in streaming).
- *Complexity:* one shuffle by user + sort; O(n log n).
- *Follow-up:* "Do it in streaming." → `session_window` + watermark (Chapter 11).

**8. [Databricks] Join with heavy skew: salting** ✅ (A4: max partition 261,917 → 69,886 rows). Say *when not to*: if the dimension is small, broadcast it and there's no shuffle skew at all.
- *Follow-up:* "Skewed key is NULL." → filter NULLs out of the join, process them separately.

**9. [Amazon] Deduplicate keeping the latest version per key** ✅
```python
w2 = Window.partitionBy("id").orderBy(F.desc("updated_at"), F.desc("city"))     # explicit tie-breaker
v.withColumn("rn", F.row_number().over(w2)).filter("rn = 1").drop("rn")
```
```
|id| city  |updated_at         |
| 1|Mumbai |2024-03-01 00:00:00|     <- tie with Delhi on updated_at; the tie-breaker makes it deterministic
| 2|Austin |2024-02-01 00:00:00|
```
- *Follow-up:* "Billions of rows?" → `groupBy(key).agg(F.max(F.struct("updated_at", *cols)))` avoids a full sort per key; or Delta MERGE.

**10. [Uber] Incremental load: process only new files** ✅
```python
done = set(json.load(open(ctl))) if os.path.exists(ctl) else set()
new = [f for f in sorted(glob.glob(f"{B}/in/*.json")) if f not in done]
if new:
    df = spark.read.schema("id int, v int").json(new); df.write.mode("append").parquet(out)
    json.dump(sorted(done | set(new)), open(ctl, "w"))
```
```
processed 1 new file(s), 2 rows -> total in target: 2
no new files
processed 1 new file(s), 1 rows -> total in target: 3
```
**Weakness to call out:** a crash between the write and the control-file update re-processes the file → duplicates. Make it idempotent: write keyed by a natural id with MERGE, or use **Auto Loader / structured streaming with a checkpoint (`trigger(availableNow=True)`)** which tracks files for you.
- *Follow-up:* "Files arrive late/out of order, or are modified?" → track by path + modification time/etag, or ingest by event time with a reprocessing window.

### Hard (11–13)

**11. [Uber] Surge pricing: join requests with driver availability by geospatial proximity and time windows** ✅ (small illustrative data)
Approach: **avoid a cross join.** (1) Bucket coordinates into a grid (`floor(lon/0.01)`, ~1.1 km cells). (2) Explode each driver into its own cell + the 8 neighbours so an *equi-join* on `(cx, cy)` finds nearby candidates. (3) Add the time condition `abs(req.ts - drv.ts) <= 300 s`. (4) Apply the exact **haversine** distance filter on the (now small) candidate set. (5) Aggregate per cell/minute: requests ÷ available drivers → surge multiplier, clamped to [1, 3].
```
req_id | nearby_drivers          cx   | cy   | requests | ratio | surge
1 | 2   2 | 2   3 | 2   4 | 2     7759 | 1297 | 4        | 2.0   | 1.5
5 | 1                              7764 | 1299 | 1        | 1.0   | 1.0
```
Driver 103 (28 min away in time) was excluded by the time window; request 5 lives in its own far cell. A real system also dedups a driver counted for several requests (use `countDistinct`), uses **H3/S2 cells**, and handles cell-boundary effects with the neighbour explode (done here).
- *Follow-up:* "Skew: downtown cell has 80% of traffic." → salt that cell, use finer cells (higher H3 resolution), or broadcast the (small) driver snapshot.

**12. [Netflix] User engagement scoring pipeline** ✅
Aggregate watch history → completion flag (`watched ≥ 90% runtime`) → **rolling 7-calendar-day** metrics with `rangeBetween(-6, 0)` over a numeric day index → latest-row per user → blend into a score → `dense_rank`.
```
|user_id|min_7d|titles_7d|completion_rate|total_min|score|rank|
|u2     |   240|        3|            1.0|      240| 82.4|   1|
|u1     |    55|        2|            0.5|      135| 34.9|   2|
|u3     |     7|        2|            0.0|        7|  2.7|   3|
```
> **COMMON MISTAKE:** `rowsBetween(-6, 0)` means 7 **rows**, not 7 **days**: a user with gaps in activity gets a window reaching back much further. Use `rangeBetween` on a numeric day number (`datediff(day, epoch)`).
- *Follow-up:* "Rank within country." → `Window.partitionBy("country").orderBy(desc("score"))`. "Weights tuned by ML?" → compute the features here, hand them to the model (Chapter 18).

**13. [Databricks] Optimize a slow job with 5 issues** ✅ See A7: (1) read all columns + late filter → select/filter early (pushdown); (2) sort-merge join of a small dimension → `broadcast()`; (3) Python UDF → built-in `create_map`/`when`; (4) repeated actions recomputing → `cache()` the reused result; (5) 200 tiny output files + oversized `shuffle.partitions` → right-size partitions, `coalesce` before write. **153.9 s → 3.9 s, identical results.**
- *Follow-up:* "Which would you fix first in production?" → by measured impact: check the Spark UI for the stage with the most time. Usually shuffle volume/skew and spill, then UDFs, then file sizes.

---

## Part C — Interview Simulations

### Q1 [Databricks]: "A Spark job processing 2 TB/day takes 4 hours and fails with OOM. Walk me through debugging. Here are the UI metrics."

> **⚠️ The metrics below are ILLUSTRATIVE, made up to teach the method. They are not from a run.**

| Stage | Tasks | Median task | Max task | Shuffle read (max task) | Spill (disk) | GC time |
|---|---|---|---|---|---|---|
| 5 (join) | 200 | 1.1 min | **41 min** | **38 GB** (median 4 GB) | **610 GB** | 28% |
| 7 (agg) | 200 | 0.4 min | 0.6 min | 1.5 GB | 0 | 4% |
Failure: `ExecutorLostFailure ... exit code 137 (OOM-killed)` on stage 5; driver healthy.

**Think out loud**
1. **Localize first.** Only stage 5 is bad. Task duration max 41 min vs median 1.1 min and shuffle-read max 38 GB vs median 4 GB = **data skew** on the join key. Spill of 610 GB + 28% GC says tasks don't fit in execution memory.
2. **Why OOM?** The straggler task gets ~10× the data; sort-merge join buffers/sorts it; memory spills, GC thrashes, the container exceeds its limit → killed (exit 137 = memory limit by the container, vs `java.lang.OutOfMemoryError` heap).
3. **Confirm the key:** `df.groupBy(join_key).count().orderBy(desc("count")).show()` (a NULL or a default/"unknown" id is the usual culprit).
4. **Fixes in order of cost:** (a) **filter/handle NULL & default keys** before the join; (b) is one side small enough to **broadcast**? (c) enable/verify **AQE skew join** (`skewJoin.enabled`, tune `skewedPartitionFactor/ThresholdInBytes`); (d) **salt** or isolate the hot keys; (e) right-size the partitions: 2 TB of shuffle data at ~200 MB per partition is on the order of 10,000 partitions, so a default of 200 means ~10 GB per task. Raise `shuffle.partitions` and/or let AQE size them with `advisoryPartitionSizeInBytes`; (f) only then **bigger executors** (memory per core is the lever: fewer cores per executor = more memory per task).
5. **Other 4-hour contributors:** column pruning/early filters on the 2 TB read, small-file overhead on the input, wide `select *`, Python UDFs, `collect()`/`toPandas()` on the driver, unnecessary cache.
6. **Verify & prevent:** re-run, compare stage metrics (max/median ratio < ~3×, spill ≈ 0, GC < 10%); add alerts on task skew and spill, and a data-quality check on key distribution.

| Evaluator is looking for | Green flag | Red flag |
|---|---|---|
| Method | Localizes the failing stage from the UI before changing anything | "Increase executor memory" immediately |
| Root cause | Recognises skew from max/median and shuffle-read asymmetry | Treats it as a generic memory problem |
| Toolbox | Broadcast, AQE skew, salting, NULL-key handling, partition sizing | Only one fix |
| Verification | Defines how to confirm (metrics after the fix) | Declares victory |

### Q2 [Amazon]: "Write a PySpark pipeline that reads clickstream events from Kafka, sessionizes them, joins with user profiles, and writes to Delta partitioned by date. Handle late data and schema evolution."

⚠️ **NOT RUN as written** (needs Kafka + Delta). The streaming semantics it relies on (watermarks, `session_window`, dedup, checkpoint, stream-stream join) were verified in Chapter 11, and the sessionization logic ✅ above.

```python
from pyspark.sql import functions as F, types as T

event_schema = T.StructType([T.StructField("event_id", T.StringType()), T.StructField("user_id", T.StringType()),
                             T.StructField("event_type", T.StringType()), T.StructField("event_time", T.TimestampType()),
                             T.StructField("props", T.MapType(T.StringType(), T.StringType()))])   # map = tolerant to new keys

raw = (spark.readStream.format("kafka").option("kafka.bootstrap.servers", BROKERS).option("subscribe", "clickstream")
       .option("startingOffsets", "latest").option("maxOffsetsPerTrigger", 500_000).option("failOnDataLoss", "false").load())

# 1) Bronze: keep raw bytes + Kafka metadata (replay, audit, schema evolution safety net)
(raw.selectExpr("CAST(key AS STRING) k", "CAST(value AS STRING) raw", "topic", "partition", "offset", "timestamp")
    .writeStream.format("delta").option("checkpointLocation", "/chk/click_bronze").toTable("bronze.clicks"))

# 2) Silver: parse + dedupe + late-data policy
events = (spark.readStream.table("bronze.clicks")
          .select(F.from_json("raw", event_schema).alias("e"), "partition", "offset").select("e.*")
          .filter("event_id IS NOT NULL")
          .withWatermark("event_time", "15 minutes")                               # late-data tolerance
          .dropDuplicates(["event_id", "event_time"]))

# 3) Sessionize: 30-minute inactivity gap
sessions = (events.groupBy("user_id", F.session_window("event_time", "30 minutes").alias("w"))
            .agg(F.count("*").alias("events"), F.min("event_time").alias("session_start"), F.max("event_time").alias("session_end"))
            .select("user_id", "events", "session_start", "session_end", F.to_date("session_start").alias("session_date")))

# 4) Enrich with user profiles (static Delta table: stream-static join is fine, re-read each micro-batch)
profiles = spark.read.table("silver.user_profiles").select("user_id", "tier", "country")
enriched = sessions.join(F.broadcast(profiles), "user_id", "left")

# 5) Write: append mode emits a session once it is final (after watermark + gap)
(enriched.writeStream.format("delta").outputMode("append").partitionBy("session_date")
   .option("checkpointLocation", "/chk/click_sessions").trigger(processingTime="1 minute").toTable("silver.sessions"))
```
Points to make: **late data**: watermark 15 min bounds state; events later than that are dropped (Spark's lag nuance, Chapter 11), and a nightly batch recompute from bronze corrects them; **schema evolution**: bronze stores raw JSON, `props` is a map, Delta `mergeSchema` is opt-in (Chapter 9), contract/drift alerts on new keys; **exactly-once effect**: checkpoints + Delta transactional sink + dedup on `event_id`; **partitioning**: by `session_date` only (low cardinality), compaction via OPTIMIZE; **profile freshness**: stream-static joins read the table version at each batch (slowly changing profiles OK; fast-changing needs a stream-stream join or lookup service).
> **Subtle point to catch:** the profile join is a **left join with a broadcast of the static side**, so a missing profile yields NULL tier rather than dropping the session (the inner-join silent-drop problem from A4).

| Evaluator is looking for | Green flag | Red flag |
|---|---|---|
| Streaming correctness | Watermark, dedupe, checkpoints, output mode justified | No late-data story |
| Layering | Raw bronze + parsed silver | Parse directly from Kafka with no replay path |
| Joins | Stream-static, broadcast, left-join semantics | Joins streams without time bounds |
| Operability | Compaction, monitoring, schema drift handling | Only the happy path |

---

## Chapter 8 Cheat Sheet

- Action → **job**; shuffle → **stage** boundary; partition → **task**. `Exchange` in a plan = a shuffle.
- Prefer **built-ins ≫ pandas UDF > Python UDF** ✅; UDFs block Catalyst.
- Joins: broadcast (small side) > sort-merge (large×large) ; hint `broadcast()`; AQE can switch at runtime ✅. Check row counts after joins; NULL keys never match; `left_anti` finds orphans.
- `explode` drops empty arrays; use `explode_outer` ✅. NULL comparisons are NULL ✅.
- Read less: select columns early, filter early (pushdown ✅), partition by low-cardinality columns, bucket for repeated joins ✅.
- Skew: look at max/median task time; fix with AQE skew join, salting ✅, isolating hot/NULL keys, broadcasting.
- `shuffle.partitions` default 200: set for your data; AQE coalesces ✅. `coalesce` (no shuffle, reduce only) vs `repartition` (shuffle) ✅.
- `cache()` only reusable, expensive, fits-in-memory data ✅; `unpersist()`.
- Debugging method: Spark UI → failing stage → max vs median task, shuffle read, spill, GC → root cause → targeted fix → re-measure.

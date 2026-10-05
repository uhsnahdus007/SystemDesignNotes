# Chapter 9: Delta Lake & Lakehouse Architecture

> **Verification status — important.**
> The Delta Spark jars can't be downloaded in my sandbox, so I could **not** run `spark.sql("MERGE INTO ...")`. Instead I ran every behaviour against **delta-rs (`deltalake` 1.6.6, Python)**, a real, independent implementation of the Delta protocol (same `_delta_log`, same Parquet files). So:
> - ✅ **RAN (real Delta via delta-rs):** table creation, transaction log, schema enforcement/evolution, time travel/restore, MERGE (upsert, conditional, SCD1, SCD2), replaceWhere, concurrent writers/conflicts, OPTIMIZE, Z-ORDER, VACUUM, Change Data Feed. Also **Apache Iceberg** (`pyiceberg` 0.12) for the comparison.
> - ⚠️ **SQL / PySpark syntax shown below was NOT run.** It is standard Databricks/Delta-Spark syntax written from knowledge; the *semantics* are what I verified. Method names differ between delta-rs and Spark (noted).
> - ⚠️ **Not available at all:** Liquid Clustering, Auto-optimize and auto-compaction (Databricks features), and Apache Hudi. Those sections are explanation + syntax only.
> Output below is real unless tagged otherwise.

---

## Part A — Concept + Code

### A1. What is Delta Lake? (the mental model)

Delta = **Parquet files + a transaction log** (`_delta_log/`) on object storage. The log is an ordered list of JSON commits (plus periodic Parquet checkpoints). A table's state = replay the log. That gives a data lake **ACID transactions, schema enforcement, time travel, and efficient upserts**.

```
orders/
  _delta_log/
     00000000000000000000.json    <- commit 0: add file A
     00000000000000000001.json    <- commit 1: add file B
     00000000000000000010.checkpoint.parquet   <- compacted snapshot every ~10 commits
  part-0000-....snappy.parquet     <- data files (immutable)
```
Readers pick a log version → a consistent snapshot (file list). Writers add new files and commit by atomically creating the next numbered log file. Because files are **immutable**, an UPDATE writes new files and marks old ones removed in the log.

### A2. Creating a Delta table  ✅ RAN (DataFrame write) / ⚠️ SQL not run

```python
from deltalake import DeltaTable, write_deltalake
import pandas as pd
df0 = pd.DataFrame({"order_id": [1,2,3], "customer": ["asha","bob","chen"], "amount": [100.0,250.0,75.5], "status": ["new","new","paid"]})
write_deltalake("/tmp/dl/orders", df0)
dt = DeltaTable("/tmp/dl/orders")
print("version:", dt.version(), "| files:", len(dt.file_uris()), "| protocol:", dt.protocol().min_reader_version, "/", dt.protocol().min_writer_version)
print("_delta_log:", sorted(os.listdir("/tmp/dl/orders/_delta_log")))
```
```
version: 0 | files: 1 | protocol: 1 / 2
_delta_log: ['00000000000000000000.json']
schema: order_id long, customer string, amount double, status string
```
The three ways to create a table in Spark/Databricks ⚠️ NOT RUN:
```sql
-- 1) DDL
CREATE TABLE sales.orders (order_id BIGINT, customer STRING, amount DOUBLE, status STRING)
USING DELTA PARTITIONED BY (status) TBLPROPERTIES ('delta.enableChangeDataFeed' = 'true');
-- 2) CTAS (schema inferred from the query)
CREATE TABLE sales.orders_paid USING DELTA AS SELECT * FROM sales.orders WHERE status = 'paid';
```
```python
# 3) DataFrame writer
df.write.format("delta").mode("overwrite").partitionBy("status").saveAsTable("sales.orders")
df.write.format("delta").save("s3://bucket/path/orders")                       # path-based (unmanaged)
```
> **Partitioning advice:** Don't partition small/medium tables (< ~1 TB); use a low-cardinality column (date) if you do; over-partitioning creates the small-file problem (A8). Prefer Z-ORDER / Liquid Clustering (A10) for high-cardinality filters.

### A3. ACID Transactions & Concurrent Writes  ✅ RAN

Delta uses **optimistic concurrency control**: each writer reads a version, prepares its commit, and tries to create the next log file; if someone else got there first, the writer checks whether the other commit **conflicts** with what it read/wrote. No conflict → retry on the new version; conflict → fail (never silently overwrite).

**Test 1: 4 threads × 5 concurrent appends:**
```
rows = 21 (expected 21), version = 20, errors = []
log files: 21
```
Appends only add files and touch nothing the other writers read, so they **don't conflict** and all 20 commits serialize cleanly.

**Test 2: 4 threads each running `UPDATE v = v + 1 WHERE id = 1` on the same file:**
```
['CommitFailedError', 'CommitFailedError', 'CommitFailedError', 'committed']   final v for id=1 = 1
```
Only one commit won; the other three were rejected as conflicts, and **the value is exactly 1, not corrupted**. (delta-rs surfaces the conflict to the caller. Spark's Delta retries automatically when it can prove there's no logical conflict and otherwise throws `ConcurrentModificationException` subclasses such as `ConcurrentAppendException`/`ConcurrentDeleteReadException`.)

| Operation pair | Typical outcome |
|---|---|
| Append ‖ Append | OK |
| Update/Merge ‖ Append (disjoint partitions) | OK if predicates are partition-disjoint |
| Update/Merge ‖ Update/Merge touching the same files | One fails (conflict) |
| OPTIMIZE ‖ Append | OK |
| OPTIMIZE ‖ Update/Delete on the same files | Conflict |

> **In Real Production:** Make MERGE predicates **partition-aware** (`ON t.dt = s.dt AND t.id = s.id`, with a literal/partition filter) so concurrent jobs on different partitions don't conflict; wrap writes in a retry-on-conflict loop; on S3, a multi-writer setup needs a commit coordinator/DynamoDB lock (delta-rs) or Databricks' managed coordination, since S3 historically lacked atomic put-if-absent.

### A4. Schema Enforcement  ✅ RAN

Delta **rejects** writes whose schema doesn't match the table (protects downstream consumers from silent corruption):
```python
bad_extra = pd.DataFrame({"order_id":[4], "customer":["dia"], "amount":[10.0], "status":["new"], "coupon":["SAVE10"]})
write_deltalake(P, bad_extra, mode="append")
# REJECTED: Cannot cast schema, number of fields does not match: 5 vs 4
bad_type = pd.DataFrame({"order_id":["four"], ...})
write_deltalake(P, bad_type, mode="append")
# REJECTED: Cast error: Cannot cast string 'four' to value of Int64 type
```
```
table unchanged, version still 0
```
(Exact wording differs on Spark: `AnalysisException: A schema mismatch detected when writing to the Delta table`.) The failed write left **no partial data**: atomicity applies to schema violations too.

### A5. Schema Evolution  ✅ RAN

Opt in explicitly to let the new column through:
```python
write_deltalake(P, bad_extra, mode="append", schema_mode="merge")      # Spark: .option("mergeSchema", "true")
```
```
new schema: ['order_id', 'customer', 'amount', 'status', 'coupon'] | version 1
 order_id customer  amount status coupon
        1     asha   100.0    new    NaN
        2      bob   250.0    new    NaN
        3     chen    75.5   paid    NaN
        4      dia    10.0    new SAVE10
```
Old files weren't rewritten; old rows simply read `NULL` for the new column.
```sql
-- Spark/Databricks ⚠️ NOT RUN
ALTER TABLE sales.orders ADD COLUMNS (coupon STRING AFTER status);
SET spark.databricks.delta.schema.autoMerge.enabled = true;        -- also lets MERGE evolve the schema
ALTER TABLE sales.orders ALTER COLUMN amount TYPE DECIMAL(12,2);   -- type change: only some widenings are allowed in place (e.g. INT→BIGINT with type widening enabled)
```
| Change | Cost / support |
|---|---|
| Add column (incl. nested field) | Cheap, metadata-only |
| Rename / drop column | Needs **column mapping** (`delta.columnMapping.mode = 'name'`); then metadata-only |
| Change type (narrow or incompatible) | Requires rewriting the table (overwrite with `overwriteSchema`) |
| Nested struct change | `mergeSchema` adds new nested fields; you cannot remove/rename them without column mapping |

> **COMMON MISTAKE:** Turning on `autoMerge`/`mergeSchema` globally "to stop failures". It converts loud schema breaks into silent ones (a typo'd column becomes a new column). Evolve deliberately, and alert on drift.

### A6. Time Travel, History, RESTORE  ✅ RAN

```python
write_deltalake(P, pd.DataFrame({"order_id":[5], "customer":["eli"], "amount":[999.0], "status":["new"], "coupon":[None]}), mode="append")
dt = DeltaTable(P)
for h in dt.history()[:5]: print(f"v{h.get('version')}: {h['operation']:12s} {h.get('operationParameters')}")
print("rows now:", len(dt.to_pandas()), "| as of v0:", len(DeltaTable(P, version=0).to_pandas()), "| as of v1:", len(DeltaTable(P, version=1).to_pandas()))
dt.restore(0)
```
```
v2: WRITE        {'mode': 'Append'}
v1: WRITE        {'mode': 'Append'}
v0: WRITE        {'mode': 'ErrorIfExists'}
rows now: 5 | as of version 0: 3 | as of version 1: 4
after RESTORE to v0: rows = 3 | version = 3 (restore is itself a new commit) | op: RESTORE
```
```sql
-- Spark SQL ⚠️ NOT RUN
SELECT * FROM sales.orders VERSION AS OF 3;
SELECT * FROM sales.orders TIMESTAMP AS OF '2024-06-01 10:00:00';
DESCRIBE HISTORY sales.orders;
RESTORE TABLE sales.orders TO VERSION AS OF 3;           -- an accidental DELETE/overwrite? undo it
```
> **KEY INSIGHT:** RESTORE does **not** rewrite history: it appends a new version (3) that re-points to old files. That's why it's instant and safe. But time travel only works while the old files exist: **VACUUM deletes them** (A8).
> **Uses:** audit ("what did this table look like at 9 am"), reproducible ML training sets (pin `VERSION AS OF`), debugging, diffing two versions, undoing bad writes. Not a backup substitute (same storage, same account).

### A7. MERGE INTO: Complete Patterns  ✅ RAN (delta-rs `merge` builder)

**Pattern 1: simple upsert** (update matched, insert new). Target ids 1–3, source ids 2–4:
```python
(dt.merge(source=pa.Table.from_pandas(src), predicate="t.id = s.id", source_alias="s", target_alias="t")
   .when_matched_update_all().when_not_matched_insert_all().execute())
# metrics: num_source_rows=3, inserted=1, updated=2, num_target_rows_copied=1
```
```
 id name   city
  1 Asha   Pune        <- untouched
  2  Bob Dallas        <- updated
  3 Chen Berlin        <- "updated" (to identical values)
  4  Dia   Oslo        <- inserted
```
Spark SQL equivalent ⚠️ NOT RUN:
```sql
MERGE INTO customers t USING updates s ON t.id = s.id
WHEN MATCHED THEN UPDATE SET *
WHEN NOT MATCHED THEN INSERT *;
```
> **COMMON MISTAKE:** Row 3 counted as *updated* though nothing changed: a blind `WHEN MATCHED THEN UPDATE SET *` rewrites the file and bloats the log. Add a condition: `WHEN MATCHED AND (t.city <> s.city OR t.name <> s.name)` (use null-safe `<=>`/`IS DISTINCT FROM`).

**Pattern 2: conditional merge, multiple `WHEN` clauses.** Order matters: the first matching clause wins.
```python
(dt.merge(src, "t.id = s.id", source_alias="s", target_alias="t")
   .when_matched_delete(predicate="s.active = false")
   .when_matched_update(updates={"city": "s.city"}, predicate="s.active = true AND t.city <> s.city")
   .when_not_matched_insert_all().execute())
# metrics: inserted=1, updated=1, deleted=1, copied=2
```
Source: id1 unchanged, id2 city changed, id3 `active=false`, id5 new. Result:
```
 id name   city  active
  1 Asha   Pune    True      <- unchanged (no rewrite for it)
  2  Bob Dallas    True      <- updated
  4  Dia   Oslo    True      <- not in source: untouched
  5  Eli   Rome    True      <- inserted        (id 3 deleted)
```
```sql
MERGE INTO customers t USING updates s ON t.id = s.id
WHEN MATCHED AND s.active = false THEN DELETE
WHEN MATCHED AND s.active = true AND t.city <> s.city THEN UPDATE SET t.city = s.city
WHEN NOT MATCHED THEN INSERT *
WHEN NOT MATCHED BY SOURCE AND t.last_seen < current_date() - 90 THEN DELETE;     -- Spark 3.4+/DBR: remove rows missing from a FULL source
```
**Pattern 3: SCD Type 1** = Pattern 1 (overwrite attributes in place; no history).

**Pattern 4: SCD Type 2 (full implementation)**  ✅ RAN. Target: customers 1 Pune, 2 Austin, 3 Berlin (all current, valid from 2023-01-01). Incoming 2024-06-01: id1→Mumbai (changed), id3 Berlin (unchanged), id4 Delhi (new).
```python
# changed rows appear TWICE in the staged source: merge_key = id (closes old row) and merge_key = NULL (forces INSERT of new version)
stage = pa.Table.from_pandas(pd.concat([a_, b_]))      # a_ : merge_key=id for new+changed ;  b_ : merge_key=NULL for changed only
(dt.merge(stage, "t.id = s.merge_key AND t.is_current = true", source_alias="s", target_alias="t")
   .when_matched_update(updates={"valid_to": "date '2024-06-01' - interval '1 day'", "is_current": "false"}, predicate="s.city <> t.city")
   .when_not_matched_insert(updates={"id": "s.id", "city": "s.city", "valid_from": "date '2024-06-01'",
                                     "valid_to": "date '9999-12-31'", "is_current": "true"})
   .execute())
```
```
 id   city valid_from   valid_to  is_current
  1   Pune 2023-01-01 2024-05-31       False     <- closed
  1 Mumbai 2024-06-01 9999-12-31        True     <- new version
  2 Austin 2023-01-01 9999-12-31        True     (not in this incoming batch: untouched)
  3 Berlin 2023-01-01 9999-12-31        True     <- unchanged: no new row
  4  Delhi 2024-06-01 9999-12-31        True     <- new key
```
Spark SQL form ⚠️ NOT RUN:
```sql
MERGE INTO dim_customer t
USING (
  SELECT s.id AS merge_key, s.* FROM updates s
  UNION ALL
  SELECT NULL AS merge_key, s.* FROM updates s JOIN dim_customer d ON d.id = s.id AND d.is_current
  WHERE s.city <> d.city
) s
ON t.id = s.merge_key AND t.is_current = true
WHEN MATCHED AND s.city <> t.city THEN UPDATE SET t.is_current = false, t.valid_to = date_sub(current_date(), 1)
WHEN NOT MATCHED THEN INSERT (id, city, valid_from, valid_to, is_current) VALUES (s.id, s.city, current_date(), DATE'9999-12-31', true);
```
> **A real pitfall I hit:** My first attempt used `TIMESTAMP '9999-12-31'` for `valid_to` and the engine failed: `Overflow converting 9999-12-31 ... to Nanosecond (valid range 1677-2262)`. Pandas/DataFusion nanosecond timestamps can't represent it. Use a **`DATE`** column (as above), or a sentinel such as `2999-12-31`, and be consistent across engines.

**Pattern 5: delete + insert (replace a slice atomically)**  ✅ RAN via `replaceWhere`
```python
write_deltalake(P, new_rows_for_june_2, mode="overwrite", predicate="dt = '2024-06-02'")      # Spark: .option("replaceWhere", "dt = '2024-06-02'")
```
```
        dt  v
2024-06-01  1
2024-06-01  2
2024-06-02 30      <- partition 06-02 replaced
2024-06-02 40         (3 new rows instead of the old 2)
2024-06-02 50
```
One atomic commit: readers never see the partition half-replaced. This is the standard **idempotent backfill**: re-running yields the same result, unlike `append`.

### A8. VACUUM, OPTIMIZE, Z-ORDER

**The small-file problem + OPTIMIZE** ✅ RAN: 200 tiny appends (50 rows each):
```
files before: 200 | after OPTIMIZE: 1 | metrics: added=1 removed=200
full read: 176 ms -> 5 ms | rows unchanged: 10000
table version: 200  (OPTIMIZE is a normal commit; old files still exist until VACUUM; time travel to the pre-OPTIMIZE version still works)
```
Why it's slow before: per-file open/listing/metadata overhead, tiny Parquet row groups, huge logs/checkpoints. **OPTIMIZE** = bin-packing compaction toward a target file size (~1 GB on Databricks, 128 MB–1 GB typical). Readers during OPTIMIZE are unaffected (snapshot isolation).
```sql
OPTIMIZE sales.orders;                                    -- whole table
OPTIMIZE sales.orders WHERE dt >= '2024-06-01';           -- only recent partitions
OPTIMIZE sales.orders ZORDER BY (customer_id);
ALTER TABLE sales.orders SET TBLPROPERTIES (
  'delta.autoOptimize.optimizeWrite' = 'true',            -- write fewer, bigger files  (Databricks)
  'delta.autoOptimize.autoCompact'   = 'true');           -- small post-write compaction (Databricks)
```
**Z-ORDER** ✅ RAN: 8 files of random `user_id` (each file's min/max spans nearly the whole range, so min/max **data skipping** can't exclude any file):
```
before Z-ORDER: files that could contain user_id 5000..5100 (by min/max stats): 8 of 8
after  Z-ORDER BY user_id (target 1MB files)  : 1 of 6
```
Z-ORDER re-sorts data along a space-filling curve so rows with similar `user_id` land in the same files; the per-file min/max statistics then let queries **skip** most files. It helps *multi-column* filters on high-cardinality columns; it rewrites data (expensive), and you must re-run it as new data arrives. Limit to ~1–4 columns; effectiveness drops with more.

**VACUUM** ✅ RAN (after the 200-file table was compacted):
```
parquet files on disk: 201 | live in table: 1 | VACUUM dry run (0h retention) would delete: 200
VACUUM RETAIN 0 HOURS without override -> REFUSED (minimum retention is configured to be greater than ...)
after VACUUM (0h): files on disk: 1
time travel to version 5 after VACUUM -> FAILS: FileNotFoundError: Object at location .../part-00000-....snappy.parquet
time travel to latest still works: 10000 rows
```
VACUUM physically deletes data files **no longer referenced by the table and older than the retention period** (default 7 days). It does not delete the log.
> **DANGEROUS OPERATIONS:** (1) `VACUUM … RETAIN 0 HOURS` (override required) removes files that **concurrent readers/streams may still be reading**, so queries fail mid-flight, and it **destroys time travel** (shown above). (2) Retention shorter than your longest-running job, or than your streaming checkpoint lag, or than your CDF consumers' lag, breaks them. (3) GDPR deletes aren't complete until VACUUM runs after the DELETE. Always `VACUUM … DRY RUN` first.
```sql
VACUUM sales.orders DRY RUN;               -- list what would be deleted
VACUUM sales.orders RETAIN 168 HOURS;      -- default 7 days
```

### A9. Change Data Feed (CDF)  ✅ RAN (delta-rs `load_cdf`)

Enable it at creation (or `ALTER TABLE … SET TBLPROPERTIES (delta.enableChangeDataFeed = true)`; **only changes after enabling are recorded**), then do an UPDATE, a DELETE and an INSERT:
```python
write_deltalake(P, df, configuration={"delta.enableChangeDataFeed": "true"})
DeltaTable(P).update(predicate="id = 1", updates={"city": "'Mumbai'"})
DeltaTable(P).delete("id = 2")
write_deltalake(P, pd.DataFrame({"id":[4], "city":["Oslo"]}), mode="append")
cdf = pa.table(DeltaTable(P).load_cdf(starting_version=0).read_all()).to_pandas()
```
```
 id   city     _change_type  _commit_version
  1   Pune           insert                0
  2 Austin           insert                0
  3 Berlin           insert                0
  1 Mumbai update_postimage                1
  1   Pune  update_preimage                1
  2 Austin           delete                2
  4   Oslo           insert                3
```
**Downstream incremental build:** read versions after your last processed one, **drop `update_preimage`**, keep the latest change per key, then MERGE into the downstream table:
```
 id   city     _change_type  _commit_version
  1 Mumbai update_postimage                1
  2 Austin           delete                2
  4   Oslo           insert                3
```
```python
# Spark version ⚠️ NOT RUN
changes = (spark.read.format("delta").option("readChangeFeed", "true")
           .option("startingVersion", last_version + 1).table("silver.customers"))
latest = (changes.filter("_change_type != 'update_preimage'")
          .withColumn("rn", F.row_number().over(Window.partitionBy("id").orderBy(F.col("_commit_version").desc()))).filter("rn = 1"))
(DeltaTable.forName(spark, "gold.customers").alias("t").merge(latest.alias("s"), "t.id = s.id")
   .whenMatchedDelete(condition="s._change_type = 'delete'")
   .whenMatchedUpdate(condition="s._change_type != 'delete'", set={"city": "s.city"})
   .whenNotMatchedInsert(condition="s._change_type != 'delete'", values={"id": "s.id", "city": "s.city"}).execute())
```
> **COMMON MISTAKES:** reading the feed after VACUUM/`delta.deletedFileRetentionDuration` removed the change files; not persisting `last_processed_version`; treating preimages as real updates; enabling CDF on tables rewritten by `overwrite` every run (every row shows as delete+insert).

### A10. Liquid Clustering  ⚠️ NOT RUN (Databricks feature; explanation only)

**What:** A flexible replacement for **both partitioning and Z-ORDER**: you declare `CLUSTER BY (cols)`, and Databricks incrementally clusters data (via `OPTIMIZE`) along those columns without rewriting the whole table, and you can **change the clustering columns later** without a full rewrite.
```sql
CREATE TABLE sales.events (event_id BIGINT, user_id BIGINT, event_date DATE, country STRING)
CLUSTER BY (user_id, event_date);
ALTER TABLE sales.events CLUSTER BY (country, event_date);        -- change keys; new data/OPTIMIZE adopt them
OPTIMIZE sales.events;                                            -- clusters incrementally (only unclustered/changed files)
-- newer DBR versions also support CLUSTER BY AUTO (automatic key selection)
```
| | Partitioning | Z-ORDER | Liquid Clustering |
|---|---|---|---|
| Change keys later | Full rewrite | Re-run (rewrites) | `ALTER TABLE … CLUSTER BY` |
| High-cardinality columns | Bad (tiny files) | Works | Works |
| Incremental | n/a | Rewrites all in scope each run | Only unclustered data |
| Combine with partitioning | | Yes | **No** (not together on the same table) |
Use it for **new tables** with skewed/evolving access patterns; keep partitioning + Z-ORDER where legacy constraints demand it. Check your runtime version's feature support and limits (max clustering columns, supported types).

### A11. Delta vs Iceberg vs Hudi

I ran a small **Iceberg** experiment (pyiceberg, local SQL catalog) to compare behaviours:
```
snapshots: [(6249, 'append'), (6707, 'append')]
rows now: 3 | as of first snapshot: 2                             <- time travel by snapshot id
after add_column: ['id', 'city', 'coupon']
after rename:     ['id', 'town', 'coupon'] | old snapshot still readable: 2 rows    <- renames are safe: columns tracked by ID, not name
metadata layout: 00000-....metadata.json, 00001-...metadata.json ...    <- each commit = a new metadata file + manifests
```
| | **Delta Lake** | **Apache Iceberg** | **Apache Hudi** |
|---|---|---|---|
| Metadata | JSON log + Parquet checkpoints | Metadata JSON → manifest list → manifests (tree) | Timeline (`.hoodie/`) + metadata table |
| Transactions / time travel | ✅ version / timestamp | ✅ snapshot id / timestamp | ✅ instant time |
| Schema evolution | Add easily; rename/drop need column mapping | **Full, ID-based (rename/reorder safe)** ✅ verified | Supported |
| Partition evolution | Not natively (Liquid Clustering instead) | **Hidden partitioning + partition evolution** | Limited |
| Upserts | `MERGE` (copy-on-write; deletion vectors for merge-on-read) | MERGE; COW and MOR | **Native record-key upsert, COW/MOR, incremental pulls** (its core strength) |
| CDC / incremental reads | Change Data Feed | Incremental/changelog scans | Incremental queries (strong) |
| Ecosystem | Spark/Databricks first; delta-rs, Trino, Flink, etc. | Broadest engine neutrality (Spark, Flink, Trino, Snowflake, BigQuery, Athena...) | Spark/Flink, strong for streaming upserts |
| Governance | Unity Catalog | REST catalogs (Polaris, Glue, Nessie, etc.) | Hive/Glue |
| UniForm / interop | Delta can expose Iceberg/Hudi metadata (**UniForm**) | Reads via catalogs | |

**How to answer "which one?":** Databricks-centric → Delta (best integration, features like Liquid Clustering). Multi-engine/vendor-neutral lakehouse → Iceberg (partition evolution, catalog ecosystem). Heavy streaming upserts/CDC ingestion at record-key level → Hudi. In practice, formats are converging, so say that "the bigger decision is the **catalog** and governance model".

---

## Part B — Practice Problems (13)

*Try each for 15 min first. Format: scenario → hint → solution → follow-up. "✅" = behaviour verified above.*

### Easy (1–5)

**1. [Databricks] Accidental delete.** An engineer ran `DELETE FROM orders` (no WHERE) 10 minutes ago. Recover.
- *HINT:* Delta never edits files in place.
- *Solution:* `DESCRIBE HISTORY orders`, find the version before the DELETE, then `RESTORE TABLE orders TO VERSION AS OF n` ✅ (restore is a new commit; instant). Works only if files weren't vacuumed.
- *Follow-up:* "Not a Delta table / vacuum already ran?" → backups/snapshots of storage; this is why VACUUM retention matters.

**2. [Amazon] Schema error.** A write fails with a schema mismatch after an upstream team added `coupon`. Fix without breaking other writers.
- *Solution:* Prefer a deliberate `ALTER TABLE ADD COLUMNS`, then write; or use `mergeSchema` for this job only ✅. Avoid global `autoMerge`.
- *Follow-up:* "They renamed a column instead." → needs column mapping; a rename arrives as drop+add, so coordinate.

**3. [Stripe] Why did time travel fail?** `SELECT * FROM t VERSION AS OF 12` returns `FileNotFoundException`. Why?
- *Solution:* VACUUM removed files older than the retention window that version 12 references ✅. Increase `delta.deletedFileRetentionDuration`/`logRetentionDuration` if you need longer travel.
- *Follow-up:* "Cost?" → retained files cost storage; balance against audit needs.

**4. [Uber] Count appended rows.** Four jobs append to the same table concurrently. Is any write lost?
- *Solution:* No ✅. Appends don't conflict; each is an atomic commit (20 commits → 21 rows, version 20).
- *Follow-up:* "Two jobs `UPDATE` the same row?" → one fails with a concurrent-modification error and must retry ✅.

**5. [Google] VACUUM safety.** What is the risk of `VACUUM … RETAIN 0 HOURS`?
- *Solution:* Deletes files active readers/streams may need, and kills time travel ✅. It requires an explicit safety-check override for that reason.
- *Follow-up:* "What retention is safe?" → ≥ longest job/stream lag/CDF consumer lag (default 7 days).

### Medium (6–10)

**6. [Netflix] Idempotent daily load.** A job loads yesterday's partition and is retried on failure. Make retries safe.
- *Solution:* Use `replaceWhere dt = '<day>'` ✅ (atomic partition replace) or MERGE on a natural key, never plain `append` (retries duplicate).
- *Follow-up:* "Late updates to older days?" → widen the predicate or MERGE by key.

**7. [Airbnb] No-op updates bloating the table.** Nightly `MERGE … WHEN MATCHED THEN UPDATE SET *` on 1 B rows rewrites most files though 0.1% of rows really change.
- *Solution:* Add a change predicate (`WHEN MATCHED AND t.hash <> s.hash`) ✅ (the row-3 "updated to identical values" case), filter the source to changed rows, and make the ON clause include the partition column so the scan prunes files.
- *Follow-up:* "Still rewrites whole files for one changed row." → enable **deletion vectors** (merge-on-read style: mark rows deleted instead of rewriting the file) on supported runtimes.

**8. [Meta] Build an SCD2 dimension from daily snapshots.** Walk through the MERGE.
- *Solution:* Pattern 4 ✅: stage source as union of (merge_key = id) and (NULL for changed rows); `WHEN MATCHED AND changed → close`, `WHEN NOT MATCHED → insert new version`. Compare with a row hash over tracked columns; use `DATE` for `valid_to` (the 9999 timestamp pitfall).
- *Follow-up:* "Deleted source rows?" → add `WHEN NOT MATCHED BY SOURCE AND t.is_current THEN UPDATE SET is_current=false, valid_to=…` if the snapshot is a **full** extract.

**9. [Databricks] Pick the layout.** 3 TB table, filters on `event_date` (always) and `user_id` (often, 100 M distinct). Design.
- *Solution:* Partition by `event_date` (or use Liquid Clustering `CLUSTER BY (event_date, user_id)`); Z-ORDER by `user_id` within the active partitions if using classic layout ✅ (8 → 1 files in my test). Do **not** partition by `user_id`.
- *Follow-up:* "Z-ORDER takes 3 hours nightly." → restrict with `WHERE event_date >= today-7`, or move to Liquid Clustering for incremental clustering.

**10. [Amazon] Incremental downstream with CDF.** Build `gold.customers` from `silver.customers` without scanning silver.
- *Solution:* Enable CDF; each run read `startingVersion = last+1`, drop preimages, take latest per key, MERGE (delete/upsert) ✅; store the last processed version in a control table (or use a stream with a checkpoint).
- *Follow-up:* "Consumer was down for 10 days, CDF files vacuumed." → fall back to a full rebuild/snapshot diff; set retention ≥ worst-case downtime.

### Hard (11–13)

**11. [Uber] Concurrent MERGE conflicts.** Two hourly jobs MERGE into the same table (different cities) and one fails randomly with `ConcurrentAppendException`/conflict.
- *HINT:* Delta checks conflicts at the file/predicate level.
- *Solution:* Partition (or cluster) by `city` and put **`t.city = s.city` literal/partition predicates in the MERGE condition** so each job's read/write set is disjoint; add a bounded retry loop; stagger schedules; avoid OPTIMIZE colliding with updates on the same files ✅ (conflicting file-level updates fail, appends don't).
- *Follow-up:* "Why does it still fail with disjoint cities when not partitioned?" → both rewrite the same files; without partition/clustering alignment the read sets overlap.

**12. [Databricks] Design a 3-layer lakehouse CDC.** Source Postgres → Debezium → Kafka → Delta bronze/silver/gold with SCD2.
- *Solution:* Bronze: append-only raw events (CDF off, partitioned by ingest date). Silver: `MERGE` latest-per-key with an LSN guard + soft-delete flag; CDF **on**. Gold SCD2: driven by silver's CDF versions ordered by commit, with `DATE` validity columns; OPTIMIZE nightly, VACUUM weekly with retention ≥ 7 days > max consumer lag; monitor freshness and reconcile counts to source. (Full pipeline detail in Chapter 12.)
- *Follow-up:* "Backfill 3 days." → replay bronze for the range into a staging table, validate, then `replaceWhere`/MERGE into silver.

**13. [Snowflake/Databricks] Delta vs Iceberg decision under constraints.** Company uses Spark + Trino + Snowflake; wants one copy of data and the freedom to switch engines.
- *Solution:* Iceberg as the open table format with a REST/Glue catalog (broad native engine support, ID-based schema/partition evolution ✅), or Delta with **UniForm** to expose Iceberg metadata to non-Databricks readers if Databricks is primary. Decide on the catalog first; run a pilot table with all three engines; check the write path (which engine is allowed to write).
- *Follow-up:* "What breaks in a multi-writer scenario?" → commit coordination/locking differs by catalog; test conflict behaviour.

---

## Part C — Interview Simulations

### Sim 1 — "Design a complete CDC pipeline with Delta."

**Clarify:** source DB and volume, freshness (minutes?), need for history (SCD2), deletes, PII/erasure, number of tables, multi-region.

```
Postgres ─WAL─► Debezium ─► Kafka (key=PK) ─► Structured Streaming ─► BRONZE Delta (raw events, append-only, partition by ingest date)
                                                                         │ readStream / foreachBatch
                                                                         ▼
                                              SILVER (current state): MERGE latest-per-key, LSN guard, soft delete, CDF ON
                                                                         │ readChangeFeed (versions > last)
                                                                         ▼
                                              GOLD: dim SCD2 (valid_from/to DATE) + facts / aggregates
 Ops: OPTIMIZE (daily) · VACUUM (weekly, ≥7d) · reconciliation job · schema-drift alerts · DLQ for poison events
```
Talking points: bronze is the replay source; silver MERGE is **idempotent** (LSN/offset guard, dedupe latest per key before MERGE: duplicate source rows per key break MERGE semantics); CDF drives incremental gold; partition-aware MERGE predicates to avoid conflicts ✅; deletes = soft in silver, hard delete + VACUUM for erasure requests; schema evolution via bronze-as-JSON and controlled `mergeSchema`; `replaceWhere` for backfills ✅.

| Evaluator is looking for | Green flag | Red flag |
|---|---|---|
| Layering | Raw bronze kept for replay | MERGE straight from Kafka with no raw copy |
| Correctness | Dedup-before-MERGE, ordering key, idempotency | "Delta handles exactly-once" |
| Maintenance | OPTIMIZE/VACUUM cadence with retention > consumer lag | Never mentions small files/VACUUM |
| Concurrency | Partition-aware predicates, retries | Ignores conflicts |

### Sim 2 — "Optimize a Delta table with 1 million small files."

**Think out loud**
1. **Confirm the diagnosis:** `DESCRIBE DETAIL` → `numFiles`, `sizeInBytes` → avg file size (e.g. 10 GB / 1 M = **10 KB/file**). Symptoms: slow queries (listing + per-file overhead, I measured 176 ms → 5 ms for 200 → 1 file ✅), huge log/checkpoint, slow `VACUUM`.
2. **Find the cause (fix the source, not just the symptom):** streaming with short triggers and no Optimized Writes, over-partitioning (e.g. by `user_id`), many tiny batch appends, MERGE/UPDATE touching many files.
3. **Fix the data now:** `OPTIMIZE table` in **chunks** (by partition range: `OPTIMIZE t WHERE dt = '…'`) since a single huge OPTIMIZE is a long, costly job; run on a dedicated cluster; for a 1 M-file table, expect heavy listing, so consider processing partition by partition and in parallel. Z-ORDER later if filters need it ✅.
4. **Then reclaim storage:** after OPTIMIZE old files are unreferenced → `VACUUM` (dry run first ✅; honor retention ≥ longest reader/stream).
5. **Prevent recurrence:** `delta.autoOptimize.optimizeWrite = true` + `autoCompact = true`; bigger micro-batches (`trigger(processingTime='5 minutes')` / `availableNow`); re-partition coarser or move to Liquid Clustering; schedule OPTIMIZE; monitor avg file size and file count per partition.
6. **Risks to state:** OPTIMIZE vs concurrent UPDATE/DELETE on the same files can conflict ✅ (appends don't); time-travel history is preserved until VACUUM; compaction costs compute, so prioritize hot partitions.

| Evaluator is looking for | Green flag | Red flag |
|---|---|---|
| Diagnosis | Quantifies avg file size, finds root cause | Jumps to OPTIMIZE blindly |
| Execution | Incremental/chunked OPTIMIZE, VACUUM dry-run | One giant OPTIMIZE on the whole table in prod |
| Prevention | Optimized writes, partition strategy, scheduling | One-off fix |

---

## Chapter 9 Cheat Sheet

- Delta = Parquet + `_delta_log`; snapshot = replay the log; files are immutable.
- **Optimistic concurrency:** appends don't conflict, overlapping updates do (one wins, others fail/retry) ✅. Use partition-aware predicates.
- Schema enforcement is on by default; evolve deliberately (`mergeSchema`); rename/drop need column mapping.
- Time travel: `VERSION/TIMESTAMP AS OF`, `RESTORE` (a new commit). **VACUUM breaks time travel** for removed files ✅. Never `RETAIN 0 HOURS` in prod.
- MERGE: dedupe the source first; add change predicates; clause order matters; SCD2 via the (merge_key / NULL) union trick; use `DATE` for `9999-12-31`.
- Idempotent loads: `replaceWhere` or MERGE, not `append`.
- OPTIMIZE fixes small files; Z-ORDER/Liquid Clustering enable data skipping ✅ (8→1 files); VACUUM reclaims space.
- CDF: enable before you need it; drop preimages; persist last version; mind retention.
- Format choice: Delta (Databricks), Iceberg (multi-engine, ID-based evolution ✅), Hudi (record-level streaming upserts); catalog + governance matter most.

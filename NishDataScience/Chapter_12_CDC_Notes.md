# Chapter 12: Change Data Capture — The Bridge Between OLTP and OLAP

> **Verification status.** Blocks tagged ✅ **RAN** were executed (Python 3.12 + DuckDB 1.5) and the outputs are real. Blocks tagged ⚠️ **NOT RUN** need Postgres/Debezium/Kafka/Spark/Delta, which my sandbox doesn't have. I wrote them carefully, but run them yourself and trust your output over mine. DuckDB's `MERGE` supports the same `WHEN MATCHED AND <cond>` shape used on Delta, but syntax differs in places (noted inline).

---

## Part A — Concept + Code

### A1. What is CDC? Log-based vs Query-based  ✅ RAN

**What / why:** CDC captures *row-level changes* (insert/update/delete) from an operational database and delivers them downstream, so the lake/warehouse stays in sync without hammering the source with full extracts.

| | Query-based (polling `updated_at`) | Log-based (read WAL/binlog) | Snapshot diff |
|---|---|---|---|
| Captures deletes | ❌ | ✅ | ✅ (by absence) |
| Captures intermediate states | ❌ (only the latest) | ✅ | ❌ |
| Source load | Query load on tables | Minimal (reads log) | Heavy (full scans) |
| Latency | Poll interval | Sub-second to seconds | Hours/days |
| Needs schema change on source | `updated_at` column + index | DB config (e.g. `wal_level=logical`) | None |
| Operational risk | Missed rows if clocks/txns misbehave | WAL disk growth if consumer stalls | Cost |

The simulation below makes the difference concrete. A poller ran after events 1–2, then the source got: update to `SHIPPED` (after an intermediate `PAID`), a delete of row 2, and row 3 inserted-then-deleted.

```python
# Query-based poller:  WHERE updated_at > last_seen
polled = [(pk, r["status"]) for pk, r in source.items() if r["updated_at"] > poll_cursor]
# Log-based:  read every event after the cursor from the transaction log
```
**Output (real):**
```
query-based sees  : [(1, 'SHIPPED')]
log-based sees    : [('u', 1, 'PAID'), ('u', 1, 'SHIPPED'), ('d', 2, None), ('c', 3, 'NEW'), ('d', 3, None)]
```
> **KEY INSIGHT:** Polling saw *one* thing. It missed the `PAID` state, the **delete of row 2**, and row 3's whole life. A warehouse built from polling will still contain row 2 forever. If you need deletes or an audit trail, you need log-based CDC.

> **When query-based is still right:** small tables, no log access (SaaS/managed DB without logical replication), and "latest state is enough" use cases with soft deletes.

---

### A2. Manual CDC in SQL: Snapshot Comparison  ✅ RAN

**What / why:** With two snapshots (yesterday, today) a `FULL OUTER JOIN` classifies every row. Simple, no infrastructure, works with any source you can export.

```sql
CREATE TABLE snap_yesterday (id INT, name VARCHAR, city VARCHAR, tier VARCHAR);
CREATE TABLE snap_today     (id INT, name VARCHAR, city VARCHAR, tier VARCHAR);
INSERT INTO snap_yesterday VALUES (1,'Asha','Pune','Gold'),(2,'Bob','Austin','Silver'),(3,'Chen','Berlin',NULL),(4,'Dia','Pune','Gold'),(5,'Eli','Delhi','Silver');
INSERT INTO snap_today     VALUES (1,'Asha','Mumbai','Gold'),(2,'Bob','Austin','Silver'),(3,'Chen','Berlin','Gold'),(4,'Dia','Pune','Gold'),(6,'Fay','Oslo','Silver');

SELECT coalesce(t.id, y.id) AS id,
  CASE WHEN y.id IS NULL THEN 'INSERT'
       WHEN t.id IS NULL THEN 'DELETE'
       WHEN (t.name,t.city,t.tier) IS DISTINCT FROM (y.name,y.city,y.tier) THEN 'UPDATE'
       ELSE 'UNCHANGED' END AS change_type,
  y.city AS old_city, t.city AS new_city, y.tier AS old_tier, t.tier AS new_tier
FROM snap_today t FULL OUTER JOIN snap_yesterday y ON t.id = y.id ORDER BY 1;
```
```
id | change    | old_city | new_city | old_tier | new_tier
1  | UPDATE    | Pune     | Mumbai   | Gold     | Gold
2  | UNCHANGED | Austin   | Austin   | Silver   | Silver
3  | UPDATE    | Berlin   | Berlin   | None     | Gold
4  | UNCHANGED | Pune     | Pune     | Gold     | Gold
5  | DELETE    | Delhi    | None     | Silver   | None
6  | INSERT    | None     | Oslo     | None     | Silver
```

**WRONG vs RIGHT:** row 3's tier went `NULL → Gold`.
```
WRONG: `t.tier <> y.tier`        -> 0 rows detected (should be 1)   (NULL <> 'Gold' is NULL, which filters out)
RIGHT: `IS DISTINCT FROM`        -> 1 row detected
```
> **COMMON MISTAKE:** Using `<>` for change detection. Any NULL on either side makes the comparison `NULL`, so changes **from or to NULL are silently ignored**. Use `IS DISTINCT FROM` (Postgres, Snowflake, Spark SQL, DuckDB) or `<=>` null-safe equality (MySQL/Spark).

> **In Real Production:** For wide tables compare a precomputed row hash instead of every column. Keep the hash expression (column list + separators + NULL handling) in one shared macro so inserts and comparisons can never disagree.

---

### A3. Manual CDC in Python: a Detector Class  ✅ RAN

```python
import hashlib, json
from typing import Any

class CDCDetector:
    """Detects inserts/updates/deletes between two snapshots keyed by pk. Row hash keeps wide-table comparisons cheap."""
    def __init__(self, pk: str, ignore: tuple[str, ...] = ()):
        self.pk, self.ignore = pk, set(ignore)

    def _h(self, row: dict[str, Any]) -> str:
        payload = json.dumps({k: row[k] for k in sorted(row) if k not in self.ignore and k != self.pk},
                             sort_keys=True, default=str)
        return hashlib.md5(payload.encode()).hexdigest()

    def diff(self, old: list[dict], new: list[dict]) -> dict[str, list]:
        o = {r[self.pk]: r for r in old}; n = {r[self.pk]: r for r in new}
        if len(o) != len(old) or len(n) != len(new):
            raise ValueError("duplicate primary keys in snapshot")        # never diff dirty data silently
        out = {"insert": [], "update": [], "delete": [], "unchanged": []}
        for k, row in n.items():
            if k not in o: out["insert"].append(row)
            elif self._h(row) != self._h(o[k]):
                changed = {c: (o[k].get(c), row.get(c)) for c in row
                           if c != self.pk and c not in self.ignore and o[k].get(c) != row.get(c)}
                out["update"].append({self.pk: k, "changes": changed})
            else: out["unchanged"].append(k)
        out["delete"] = [o[k] for k in o if k not in n]
        return out
```
**Output (real), using the same two snapshots:**
```
{'update': [{'id': 1, 'changes': {'city': ('Pune', 'Mumbai')}}, {'id': 3, 'changes': {'tier': (None, 'Gold')}}]}
inserts: [6] deletes: [5] unchanged: [2, 4]
guard: duplicate primary keys in snapshot
```
Time **O(N + M)**, memory O(N + M) (two dicts). The `ignore` parameter lets you skip audit columns like `updated_at` that change without a business change.

---

### A4. Debezium: Architecture & Event Structure

```
 PostgreSQL ──WAL (logical decoding, pgoutput)──► Debezium (Kafka Connect source)
                                                         │  one topic per table
                                                         ▼
                                      Kafka: shop.public.customers  (key = primary key)
                                                         │
                                          Spark / Flink / S3 sink ──► Bronze (Delta)
```
**Postgres prerequisites:** `wal_level=logical`; a replication slot; a publication; a user with `REPLICATION`. For a useful **before** image on UPDATE/DELETE set `ALTER TABLE customers REPLICA IDENTITY FULL` (default only includes key columns).

> **COMMON MISTAKE / production incident #1:** A replication slot that nobody consumes **forces Postgres to keep WAL forever**, so the database disk fills and production goes down. Always alert on slot lag/WAL retained, set `max_slot_wal_keep_size`, and drop the slot when decommissioning a connector. The `heartbeat.interval.ms` setting also prevents idle-table slots from pinning WAL.

**Event envelope** ✅ (these were generated and parsed by the test script):

```json
INSERT  {"before": null,
         "after":  {"id":1,"name":"Asha","city":"Pune","tier":"Gold"},
         "op": "c", "ts_ms": 1700000000040,
         "source": {"connector":"postgresql","db":"shop","schema":"public","table":"customers",
                    "lsn":1000,"ts_ms":1700000000000,"snapshot":"false","txId":100}}

UPDATE  {"before": {"id":1,"name":"Asha","city":"Pune","tier":"Gold"},
         "after":  {"id":1,"name":"Asha","city":"Mumbai","tier":"Gold"},
         "op": "u", ...,"source": {"lsn":1100, ...}}

DELETE  {"before": {"id":1,"name":"Asha","city":"Mumbai","tier":"Gold"},
         "after": null, "op": "d", ..., "source": {"lsn":1200, ...}}
```
| Field | Meaning |
|---|---|
| `op` | `c` create, `u` update, `r` **read** (initial snapshot row), `d` delete |
| `before` / `after` | Row image before/after the change (null for create/delete respectively) |
| `source.lsn` | **Log position. Your ordering key.** (MySQL: `file`+`pos`; Oracle: `scn`) |
| `source.ts_ms` | Commit time in the *source DB* (use for SCD2 `valid_from`) |
| top-level `ts_ms` | When Debezium processed it (≥ source time) |
| `source.snapshot` | `true/last` during the initial snapshot |

After a `d` event Debezium emits a **tombstone** (same key, null value) so Kafka log compaction can eventually remove the key. Your consumer must tolerate null-value messages.

**Connector config (syntax-checked as valid JSON; ⚠️ NOT RUN against a real DB):**
```json
{
  "name": "shop-postgres-cdc",
  "config": {
    "connector.class": "io.debezium.connector.postgresql.PostgresConnector",
    "plugin.name": "pgoutput",
    "database.hostname": "${file:/secrets/pg.properties:host}",
    "database.port": "5432",
    "database.user": "${file:/secrets/pg.properties:user}",
    "database.password": "${file:/secrets/pg.properties:password}",
    "database.dbname": "shop",
    "topic.prefix": "shop",
    "schema.include.list": "public",
    "table.include.list": "public.customers,public.orders",
    "slot.name": "debezium_shop",
    "publication.name": "dbz_publication",
    "snapshot.mode": "initial",
    "tombstones.on.delete": "true",
    "decimal.handling.mode": "string",
    "heartbeat.interval.ms": "10000",
    "key.converter": "org.apache.kafka.connect.json.JsonConverter",
    "value.converter": "org.apache.kafka.connect.json.JsonConverter",
    "key.converter.schemas.enable": "false",
    "value.converter.schemas.enable": "false"
  }
}
```
Credentials come from a config provider (`${file:…}`), never plaintext in the JSON. Property names shift between Debezium versions (e.g. `topic.prefix` replaced `database.server.name` in 2.x), so check the docs for your version.

**Gotchas to mention in interviews:** TOASTed columns that didn't change arrive as a placeholder (`__debezium_unavailable_value`), so don't overwrite good data with it; `decimal.handling.mode` and timestamp precision settings affect downstream types; DDL changes need a schema-evolution plan.

---

### A5. Applying CDC to Delta Lake: Bronze → Silver  ✅ RAN (DuckDB logic)

**Layers:** *Bronze* = raw events, append-only, never modified (your replay source). *Silver* = current state of each row, produced by `MERGE`.

**The generic MERGE builder** (parse Debezium event schema into a MERGE) ✅
```python
def build_merge(table: str, cols: list[str], pk: str, hard_delete: bool = False) -> str:
    """Generate the MERGE that applies a staged 'latest-per-key' batch. Guard: only apply if incoming LSN is newer."""
    set_cols = ", ".join(f"{c}=s.{c}" for c in cols if c != pk)
    ins_cols = ", ".join(cols + ["is_deleted", "src_lsn", "src_ts_ms"])
    ins_vals = ", ".join([f"s.{c}" for c in cols] + ["s.op='d'", "s.lsn", "s.ts_ms"])
    delete_clause = ("WHEN MATCHED AND s.op='d' AND s.lsn > t.src_lsn THEN DELETE" if hard_delete else
                     "WHEN MATCHED AND s.op='d' AND s.lsn > t.src_lsn THEN UPDATE SET is_deleted=TRUE, src_lsn=s.lsn, src_ts_ms=s.ts_ms")
    return f"""MERGE INTO {table} t USING staged s ON t.{pk}=s.{pk}
      {delete_clause}
      WHEN MATCHED AND s.op<>'d' AND s.lsn > t.src_lsn THEN UPDATE SET {set_cols}, is_deleted=FALSE, src_lsn=s.lsn, src_ts_ms=s.ts_ms
      WHEN NOT MATCHED AND s.op<>'d' THEN INSERT ({ins_cols}) VALUES ({ins_vals})"""
```
Generated SQL (real output):
```sql
MERGE INTO silver_customers t USING staged s ON t.id=s.id
  WHEN MATCHED AND s.op='d' AND s.lsn > t.src_lsn THEN UPDATE SET is_deleted=TRUE, src_lsn=s.lsn, src_ts_ms=s.ts_ms
  WHEN MATCHED AND s.op<>'d' AND s.lsn > t.src_lsn THEN UPDATE SET name=s.name, city=s.city, tier=s.tier, is_deleted=FALSE, src_lsn=s.lsn, src_ts_ms=s.ts_ms
  WHEN NOT MATCHED AND s.op<>'d' THEN INSERT (id, name, city, tier, is_deleted, src_lsn, src_ts_ms) VALUES (s.id, s.name, s.city, s.tier, s.op='d', s.lsn, s.ts_ms)
```

**The staging step: collapse many changes per key into the latest.** This is the most important step. A MERGE with multiple source rows matching one target row is an **error** in Delta ("multiple source rows matched") and ambiguous elsewhere.
```sql
CREATE TEMP TABLE staged AS
SELECT * EXCLUDE (rn) FROM (
  SELECT payload->>'$.op' AS op,
         CAST(coalesce(payload->'$.after.id', payload->'$.before.id') AS INT) AS id,   -- deletes only have 'before'
         payload->>'$.after.name' AS name, payload->>'$.after.city' AS city, payload->>'$.after.tier' AS tier,
         CAST(payload->>'$.source.lsn' AS BIGINT) AS lsn, CAST(payload->>'$.source.ts_ms' AS BIGINT) AS ts_ms,
         row_number() OVER (PARTITION BY coalesce(payload->'$.after.id', payload->'$.before.id')
                            ORDER BY CAST(payload->>'$.source.lsn' AS BIGINT) DESC, kafka_offset DESC) AS rn
  FROM bronze_cdc WHERE kafka_offset BETWEEN :from AND :to) WHERE rn = 1;
```
(`->>` JSON syntax is DuckDB/Postgres style. In Spark use `from_json` or `get_json_object`.)

**Test results (real):**
```
batch1 (INSERT+UPDATE same key, one batch)  : [(1, 'Mumbai', False, 1100)]       <- collapsed; final state only
after stale+duplicate redelivery            : [(1, 'Mumbai', 1100)]               <- still Mumbai (LSN guard)
soft delete                                 : [(1, 'Mumbai', True)]
hard delete                                 : 0 rows left
born-and-died in one batch -> rows for id 9 : 0
```
What each case proves:
- **Same key twice in a batch:** collapsed to the latest by LSN; only one change reaches MERGE.
- **Out-of-order / duplicate delivery** (Kafka is at-least-once): an *older* event (LSN 1050) and a *repeat* of LSN 1100 were applied and changed nothing, because of `s.lsn > t.src_lsn`. This makes the whole pipeline **idempotent**, so replaying bronze is safe.
- **Soft vs hard delete:**

| | Soft delete (`is_deleted=true`) | Hard delete (`DELETE`) |
|---|---|---|
| Downstream analytics | Must filter `WHERE NOT is_deleted` (or expose a view) | Naturally correct |
| Audit / "undelete" | Easy | Only via bronze or time travel |
| Stale-event protection | ✅ the row keeps its `src_lsn`, so older events are still rejected | ❌ no row, so a *late older update* can **resurrect** a deleted row |
| GDPR erasure | Not sufficient alone (data still there) | Required (plus vacuum, see below) |

> **KEY INSIGHT:** The LSN guard only works while the target row exists. With hard deletes, keep a small **tombstone/high-water-mark table** (`id, deleted_lsn`) and reject events with `lsn <= deleted_lsn`.

> **COMMON MISTAKE:** Ordering by `ts_ms` (wall-clock). Two commits can share a millisecond and clocks differ across systems. Order by the log position (LSN/binlog pos/SCN), and keep `kafka_offset` only as a tie-break. **Key the Kafka topic by primary key** so all changes for a row land in one partition and keep their order.

**PySpark version of the apply step** ⚠️ NOT RUN
```python
from delta.tables import DeltaTable
from pyspark.sql import functions as F, Window

def upsert_to_silver(batch_df, batch_id: int):
    parsed = (batch_df
      .select(F.get_json_object("value", "$.payload.op").alias("op"),
              F.coalesce(F.get_json_object("value", "$.payload.after.id"),
                         F.get_json_object("value", "$.payload.before.id")).cast("int").alias("id"),
              F.get_json_object("value", "$.payload.after.name").alias("name"),
              F.get_json_object("value", "$.payload.after.city").alias("city"),
              F.get_json_object("value", "$.payload.source.lsn").cast("long").alias("lsn"),
              F.get_json_object("value", "$.payload.source.ts_ms").cast("long").alias("ts_ms"))
      .filter("op IS NOT NULL"))                                       # drop tombstones (null value)
    w = Window.partitionBy("id").orderBy(F.col("lsn").desc())
    latest = parsed.withColumn("rn", F.row_number().over(w)).filter("rn = 1").drop("rn")

    (DeltaTable.forName(spark, "silver.customers").alias("t")
      .merge(latest.alias("s"), "t.id = s.id")
      .whenMatchedUpdate(condition="s.op = 'd' AND s.lsn > t.src_lsn",
                         set={"is_deleted": "true", "src_lsn": "s.lsn", "src_ts_ms": "s.ts_ms"})
      .whenMatchedUpdate(condition="s.op <> 'd' AND s.lsn > t.src_lsn",
                         set={"name": "s.name", "city": "s.city", "is_deleted": "false",
                              "src_lsn": "s.lsn", "src_ts_ms": "s.ts_ms"})
      .whenNotMatchedInsert(condition="s.op <> 'd'",
                            values={"id": "s.id", "name": "s.name", "city": "s.city", "is_deleted": "false",
                                    "src_lsn": "s.lsn", "src_ts_ms": "s.ts_ms"})
      .execute())

(spark.readStream.format("kafka").option("subscribe", "shop.public.customers")
   .option("kafka.bootstrap.servers", BROKERS).option("startingOffsets", "earliest").load()
   .selectExpr("CAST(value AS STRING) AS value")
   .writeStream.foreachBatch(upsert_to_silver)
   .option("checkpointLocation", "/chk/silver_customers").trigger(processingTime="10 seconds").start())
```
> **In Real Production:** Also append the raw events to bronze in the same job (or via a separate sink) *before* applying, so silver can always be rebuilt. Partition silver sensibly and run `OPTIMIZE`; MERGE on a huge unpartitioned table rewrites many files (restrict with a partition predicate or use deletion vectors).

---

### A6. SCD Type 2 from CDC: Complete Pipeline  ✅ RAN

CDC makes SCD2 *better* than snapshot-diff SCD2: events carry the **source commit time**, so `valid_from` reflects when the change really happened, and intermediate changes are preserved.

Input events (key 1 changes city, then tier 5 seconds later, then city again; key 2 is deleted and later returns):
```sql
-- (lsn, op, id, name, city, tier, ts)
(1000,'c',1,'Asha','Pune','Gold','2024-01-01 09:00'), (1010,'c',2,'Bob','Austin','Silver','2024-01-01 09:05'),
(1100,'u',1,'Asha','Mumbai','Gold','2024-03-10 12:00'), (1110,'u',1,'Asha','Mumbai','Platinum','2024-03-10 12:00:05'),
(1200,'d',2,NULL,NULL,NULL,'2024-04-01 08:00'),
(1300,'u',1,'Asha','Delhi','Platinum','2024-06-01 10:00'), (1310,'c',2,'Bob','Austin','Silver','2024-07-01 11:00')
```
Set-based SCD2 build: each version ends where the next event for that key begins; delete events create no version but **terminate** the previous one.
```sql
CREATE TABLE dim_customer_scd2 AS
WITH ordered AS (
  SELECT *, lead(ts) OVER (PARTITION BY id ORDER BY lsn) AS next_ts FROM cdc_events),
versions AS (
  SELECT id, name, city, tier, ts AS valid_from, coalesce(next_ts, TIMESTAMP '9999-12-31') AS valid_to,
         (next_ts IS NULL) AS is_current
  FROM ordered WHERE op IN ('c','u'))
SELECT row_number() OVER (ORDER BY id, valid_from) AS surrogate_key, * FROM versions;
```
```
sk | id | city   | tier     | valid_from          | valid_to            | is_current
1  | 1  | Pune   | Gold     | 2024-01-01 09:00:00 | 2024-03-10 12:00:00 | False
2  | 1  | Mumbai | Gold     | 2024-03-10 12:00:00 | 2024-03-10 12:00:05 | False
3  | 1  | Mumbai | Platinum | 2024-03-10 12:00:05 | 2024-06-01 10:00:00 | False
4  | 1  | Delhi  | Platinum | 2024-06-01 10:00:00 | 9999-12-31 00:00:00 | True
5  | 2  | Austin | Silver   | 2024-01-01 09:05:00 | 2024-04-01 08:00:00 | False     <- closed by the delete
6  | 2  | Austin | Silver   | 2024-07-01 11:00:00 | 9999-12-31 00:00:00 | True      <- came back
```
**Design decisions to state aloud:**
1. **Micro-versions.** Row 2→3 are 5 seconds apart (two statements in one business action). Keep full fidelity, or collapse to the last change per day. The collapse variant (real output):
```
id | city   | tier     | valid_from
1 | Pune   | Gold     | 2024-01-01 09:00:00
1 | Mumbai | Platinum | 2024-03-10 12:00:05      <- two same-day changes collapsed
1 | Delhi  | Platinum | 2024-06-01 10:00:00
2 | Austin | Silver   | 2024-01-01 09:05:00
2 | Austin | Silver   | 2024-07-01 11:00:00
```
2. **Gap during deletion** (key 2: 2024-04-01 → 2024-07-01). A fact in that window has no dimension row. Decide: keep a gap (and let facts map to an "Unknown" member), or emit an `is_deleted=true` version covering it.
3. **Incremental running** (vs rebuilding from all history): for new events of a key, close the open version at the first new event's `ts`, then insert the new versions. Use the same two-step pattern from Chapter 15's SCD2 load, driven by events instead of snapshots. Idempotency again via the LSN guard.
4. Only track **business-relevant columns** for versioning; ignore columns like `last_login_at`, or the dimension explodes.

---

### A7. Delta Change Data Feed (CDF)  ⚠️ NOT RUN (Spark) / ✅ semantics simulated

**What / why:** Debezium captures changes from the *source* DB. **CDF captures changes inside a Delta table**, so downstream tables (silver → gold) can be updated incrementally without comparing full tables.

```python
spark.sql("ALTER TABLE silver.customers SET TBLPROPERTIES (delta.enableChangeDataFeed = true)")   # only records changes from now on

# Batch read of changes between versions
changes = (spark.read.format("delta").option("readChangeFeed", "true")
           .option("startingVersion", last_processed_version + 1).table("silver.customers"))
changes.select("id", "city", "_change_type", "_commit_version", "_commit_timestamp").show()

# Or continuously
stream = (spark.readStream.format("delta").option("readChangeFeed", "true").table("silver.customers"))
```
`_change_type` values: `insert`, `update_preimage`, `update_postimage`, `delete`.

I simulated the semantics with a plain table shaped like CDF output (versions 1–4) and ran the typical downstream read (versions after 1, drop preimages, keep latest change per key):
```sql
SELECT id, city, _change_type, _commit_version FROM (
  SELECT *, row_number() OVER (PARTITION BY id ORDER BY _commit_version DESC) rn
  FROM cdf WHERE _commit_version > 1 AND _change_type <> 'update_preimage') WHERE rn=1 ORDER BY id;
```
```
id | city  | _change_type     | _commit_version
1  | Delhi | update_postimage | 4
2  | Austin| delete           | 3
```
(Result is for the simulated table; real CDF has extra commit timestamp metadata.)

Build an **incremental downstream** by persisting `last_processed_version` (e.g. in a control table or via stream checkpoint), reading only newer versions, and `MERGE`-ing the collapsed changes into the gold table.

> **COMMON MISTAKES:** (1) Forgetting that CDF only records changes made **after** it's enabled; for history, use a snapshot/full load first. (2) `VACUUM` removes old files and **shortens how far back you can read the feed**; set retention ≥ your longest consumer downtime. (3) Treating `update_preimage` rows as real updates; filter them out. (4) Reading CDF on a table that is rebuilt with `overwrite` each run: CDF records everything as delete+insert, which defeats the purpose. Use MERGE-based updates.

---

### A8. Initial Load + Incremental  ✅ RAN

**The hard problem:** you need a full copy *and* the changes made while copying it, with no gaps and no double application.

**Algorithm (this is what Debezium's initial snapshot does):**
1. Record the current log position `P` (consistent point; in Postgres the slot gives you this).
2. Snapshot the tables as of `P`, emitting rows as `op='r'` with `lsn = P`.
3. Start streaming changes from `P`. Because snapshot reading can overlap writes, **some early events may already be reflected in the snapshot**. The `lsn > t.src_lsn` guard skips them.

Test (live table of 3 rows; snapshot at LSN 5000; stream includes an event at 4990 that predates the snapshot):
```sql
-- snapshot rows get src_lsn = 5000. Stream: (4990,u,2) (5010,u,1) (5020,c,4) (5030,d,3)
MERGE INTO tgt t USING stream_stg s ON t.id=s.id
  WHEN MATCHED AND s.op='d' AND s.lsn > t.src_lsn THEN DELETE
  WHEN MATCHED AND s.op<>'d' AND s.lsn > t.src_lsn THEN UPDATE SET name=s.name, city=s.city, src_lsn=s.lsn
  WHEN NOT MATCHED AND s.op<>'d' THEN INSERT VALUES (s.id, s.name, s.city, s.lsn);
```
```
id | name | city   | src_lsn
1  | Asha | Mumbai | 5010     <- update after snapshot applied
2  | Bob  | Austin | 5000     <- event 4990 skipped (already in snapshot)
4  | Dia  | Oslo   | 5020     <- insert applied
(id 3 deleted by event 5030)
```
> **COMMON MISTAKE:** Take the snapshot first and *then* note the log position. Changes committed in between are lost forever. Always capture the log position **first**.

> **In Real Production:** Large tables (billions of rows) need *chunked/incremental snapshots* (Debezium's signal-based incremental snapshot, or a parallel backfill by PK range) so the initial load doesn't hold a long transaction or block streaming. Also keep the ability to **re-snapshot a single table** without touching others (signal table / ad hoc snapshot).

---

## Part B — Practice Problems (10)

*Try each for 15 min before reading. Format: scenario → HINT → solution → follow-up.*

### Easy (1–3)

**1. [Stripe] Spot the gap.** A nightly job loads `WHERE updated_at > :last_run`. The warehouse still contains users who deleted their accounts months ago. Why?
- *HINT:* What does a deleted row look like to a `WHERE` clause?
- *Solution:* Deleted rows no longer exist, so no `updated_at` can match them. Query-based CDC cannot see deletes. Fix: soft deletes in the source, a periodic key reconciliation (`source ids` vs `target ids`), or log-based CDC.
- *Follow-up:* "How do you reconcile 2B keys cheaply?" → compare per-partition key counts/hashes first, then drill into mismatching partitions.

**2. [Amazon] Read the op code.** Given `{"op":"r","before":null,"after":{…}}` and `{"op":"d","before":{…},"after":null}`, what do they mean and what must a consumer do?
- *Solution:* `r` = snapshot read (treat as upsert, like `c`). `d` = delete: key comes from `before`; `after` is null. Consumers must read the key from `before` for deletes and tolerate the following tombstone.
- *Follow-up:* "Why do tombstones exist?" → so Kafka log compaction can drop a deleted key's history.

**3. [Google] NULL-safe change detection.** Rewrite `WHERE new.col <> old.col` so changes to/from NULL are caught.
- *Solution:* `WHERE new.col IS DISTINCT FROM old.col` (or `NOT (new.col <=> old.col)`).
- *Follow-up:* "How do you compare 80 columns?" → hash the row in one place (`md5(concat_ws('||', coalesce(col::text,'∅'), …))`) and compare hashes.

### Medium (4–7)

**4. [Uber] Out-of-order events.** Two updates to the same row arrive in the order LSN 120, then 110 (different Kafka partitions). Silver now shows the older value. Find the bug and fix it.
- *HINT:* What does the topic key determine?
- *Solution:* The topic isn't keyed by the primary key (or key changed), so changes to one row landed on different partitions. Fix: key by PK (Debezium does by default), and add `AND s.lsn > t.src_lsn` to the MERGE so late events can't overwrite newer state.
- *Follow-up:* "Repartitioning topics changes ordering during migration. How do you handle it?" → drain old topic fully before switching, or rely on the LSN guard.

**5. [Netflix] MERGE error.** Your MERGE fails with "multiple source rows matched". Why, and what's the fix?
- *Solution:* A batch contained several events for one key. Collapse to one row per key (latest LSN) before the MERGE (`row_number() … rn = 1`).
- *Follow-up:* "Is last-event-wins always right?" → For *state* tables yes. For SCD2 or audit tables, you must process every event in order.

**6. [Airbnb] Replication slot incident.** Production Postgres disk is 95% full; the culprit is WAL. Debezium was down for 2 days. What happened, how do you recover, how do you prevent it?
- *Solution:* The replication slot makes Postgres retain all WAL since the last confirmed LSN. Recover: restart the connector (it drains) or, if urgent, drop the slot and **re-snapshot** the affected tables. Prevent: alert on retained WAL/slot lag, `max_slot_wal_keep_size`, heartbeat events, runbook for re-snapshot.
- *Follow-up:* "Is dropping the slot safe?" → It loses the stream position, so you must re-snapshot to guarantee no gaps.

**7. [Meta] Schema change mid-stream.** The source team adds a `loyalty_points` column. What breaks and how do you design for it?
- *Solution:* Debezium picks up the new column from the log; parsers with fixed schemas drop or fail. Use bronze-as-raw-JSON (always lands), enable schema evolution in silver (`mergeSchema`/`autoMerge`), data contracts + alerts for breaking changes (type change, rename, drop). Renames look like drop+add, which is the dangerous case.
- *Follow-up:* "Column type narrowing (int → smallint)?" → treat as breaking; require coordination.

### Hard (8–10)

**8. [FinTech] Exactly-once effect.** Build a pipeline from Postgres to Delta where a crash at any step never loses or duplicates changes.
- *Solution:* Kafka is at-least-once → make writes **idempotent**: (a) bronze keeps `(topic, partition, offset)` as a unique key, (b) silver MERGE with an LSN guard, (c) streaming checkpoints so replays are bounded, (d) offsets committed only after the Delta commit (Spark structured streaming handles this). Result: at-least-once delivery + idempotent apply = **effectively once**.
- *Follow-up:* "What if the connector restarts from an *earlier* LSN?" → duplicates are rejected by the guard; confirm with a reconciliation count job.

**9. [Databricks] Deletes + GDPR.** A user invokes right-to-erasure. The data exists in the source, Kafka, bronze, silver, SCD2 history, and gold. Describe a complete flow.
- *Solution:* (1) Delete in the source; the `d` event propagates. (2) Hard-delete in silver and **all SCD2 versions** by key. (3) Bronze holds raw events with PII: either encrypt PII with a per-user key and **delete the key** (crypto-shredding), or rewrite bronze files containing the key. (4) Kafka: retention + compaction/tombstones. (5) `VACUUM` to physically remove old Delta files (time travel otherwise keeps data). (6) Log the erasure request and verification queries for audit.
- *Follow-up:* "Why is crypto-shredding attractive?" → deleting one key makes all copies unreadable without rewriting petabytes.

**10. [Stripe] 500-table rollout.** You must CDC 500 Postgres tables (largest 3B rows) to the lake with minimal source impact. Plan it.
- *Solution:* Group tables by criticality/size; use a **read replica** (logical decoding on replicas needs Postgres 16+ or use primary with limits) to avoid primary load; one connector per group with a shared Kafka cluster; **incremental snapshots** for the big tables (chunked by PK) while streaming continues; automate topic → bronze → silver generation from metadata (table config drives MERGE keys and soft-delete policy); monitoring: lag per table, slot WAL, DLQ counts, row-count reconciliation source vs silver daily; roll out in waves with a rollback.
- *Follow-up:* "One table gets 50× write spike." → isolate in its own connector/topic/partitions, scale consumers for it, cap batch size, and backpressure.

---

## Part C — Interview Simulation

### "Design a real-time CDC pipeline from PostgreSQL to a data lake with SCD Type 2."

**1. Clarify (2–3 min):** Which tables and how many changes/sec? Freshness target (seconds vs minutes)? Do analysts need *history* (SCD2) on all tables or just a few? Are deletes common? PII/compliance (erasure)? Is a read replica available? Can the source team set `wal_level=logical`?

**2. Brute force → better:**
- *Brute force:* nightly full dump + diff. Simple, but heavy source load, misses intermediate states, 24h latency.
- *Better:* query-based polling on `updated_at`. Cheaper, but misses deletes and intermediate changes.
- *Target:* log-based CDC (Debezium) → Kafka → streaming apply to Delta.

**3. Architecture**
```
 PostgreSQL (primary / replica)
   └─ WAL ► Debezium (Kafka Connect)  ── snapshot: initial / incremental
                  │ topic per table, key = PK
                  ▼
              Kafka (RF=3, retention 7d)  ── DLQ topic for poison messages
                  │
        ┌─────────┴───────────┐
   Raw sink (append)     Structured Streaming (foreachBatch)
   BRONZE Delta          dedupe latest-per-key, LSN guard, MERGE
   (replay source)              │
                          SILVER Delta  (current state, soft-delete flag)
                                │  Change Data Feed
                                ▼
                    SCD2 builder (per-event, ordered by LSN)
                                ▼
                    GOLD dim_*_scd2  (valid_from/valid_to from source ts)
 Orchestration/observability: lag, slot WAL, DQ checks, reconciliation job
```

**4. Key design decisions to state**
- **Ordering:** by LSN; topic keyed by PK; LSN guard in every write.
- **SCD2 source of truth:** events from bronze/CDF ordered by LSN, `valid_from = source commit ts`; late events handled by recomputing the affected key's version chain (a bounded rebuild by key, not a full table).
- **Deletes:** soft delete in silver; in SCD2 a delete closes the current version; policy for the gap documented.
- **Initial load:** capture LSN, snapshot, then stream with overlap guard (A8).
- **Schema evolution:** bronze as raw JSON; silver with controlled evolution; contract alerts.
- **Idempotency:** unique `(topic,partition,offset)` in bronze; MERGE guard; replay-safe jobs.
- **Backfill/repair:** rebuild silver/SCD2 from bronze for a key range with no source access.

**5. Failure modes:** connector down (WAL growth → alerts + `max_slot_wal_keep_size`); Spark job crash (checkpoint resume, idempotent MERGE); poison message (DLQ + alert, don't block the partition); source failover (slot loss → re-snapshot runbook); schema break (quarantine + page).

**6. Monitoring:** end-to-end freshness (`now - max(source ts)` per table), Kafka lag, slot retained WAL, events/sec vs baseline, DLQ counts, MERGE duration, **daily reconciliation** (row counts and checksums, source vs silver), DQ tests on SCD2 (no overlapping intervals per key, exactly one `is_current` per key).

| Interviewer is evaluating | Green flags | Red flags |
|---|---|---|
| Choice of mechanism | Compares polling/diff/log-based and justifies log-based | "Use Kafka" with no reasoning |
| Correctness | LSN ordering, idempotent MERGE, delete handling, initial-load overlap | Ignores deletes, ordering, duplicates |
| Operations | WAL/slot risk, re-snapshot runbook, reconciliation | No failure story |
| SCD2 depth | Uses source commit time, handles late events, tests for overlap/gaps | Uses load time as `valid_from` |
| Compliance | Erasure across bronze/SCD2/Delta history | Not mentioned |

---

## Chapter 12 Cheat Sheet

- Log-based CDC sees **deletes and intermediate states**; polling does not.
- Compare with `IS DISTINCT FROM`, never `<>` (NULL!).
- Debezium `op`: `c`/`u`/`d`/`r`; use `source.lsn` for ordering; key topics by PK; expect tombstones; `REPLICA IDENTITY FULL` for full before-images.
- **Collapse to one row per key before MERGE**; guard with `incoming_lsn > stored_lsn` → idempotent and out-of-order safe.
- Soft delete = safer and auditable; hard delete = clean but beware resurrection and GDPR/VACUUM.
- SCD2 from CDC: `valid_from` = source commit time; delete closes the version; document the gap policy.
- Delta CDF = incremental *within* the lakehouse; filter `update_preimage`; mind VACUUM retention.
- Initial load: **record the log position first**, snapshot, then stream with overlap guard.
- Unconsumed replication slot = disk-full outage. Monitor it.

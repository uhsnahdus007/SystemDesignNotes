# Chapter 10: Database Internals — What Every DE Must Know

> **Verification status.** Unlike most chapters, almost everything here ran against a **real PostgreSQL 16.15** server (two real sessions, real `EXPLAIN ANALYZE`, real crash recovery), plus Python 3.12 / pandas / PyArrow / fastavro / SQLAlchemy. Output is pasted as-is. **Timings are from a small sandbox VM and will differ on your machine; trust the *shape* (10×, 1000×), not the exact milliseconds.** Replication (6) and CAP (9) are Python *simulations* of the concepts, not real clusters. MySQL/other engines can differ (noted where I know).

---

## Part A — Concept + Code

### A1. ACID, with each property *breaking*  ✅ RAN (PostgreSQL)

Setup: `accounts(id, owner, balance numeric CHECK (balance >= 0))` with alice=100, bob=50.

**Atomicity** — move 70 from alice to bob; the process crashes between debit and credit.
```python
def transfer(use_txn, crash):
    c = conn("lab", autocommit=not use_txn)
    try:
        q(c, "UPDATE accounts SET balance = balance - 70 WHERE id=1")
        if crash: raise RuntimeError("process crashed")
        q(c, "UPDATE accounts SET balance = balance + 70 WHERE id=2")
        if use_txn: c.commit()
    except Exception:
        if use_txn: c.rollback()
```
```
WITHOUT transaction: [(1, 30), (2, 50)]   total = 80   <- 70 vanished
WITH transaction   : [(1, 100), (2, 50)]  total = 150  <- rolled back, all-or-nothing
```
**Consistency** — constraints stop invalid states:
```
UPDATE accounts SET balance = balance - 500 WHERE id=1
rejected: new row for relation "accounts" violates check constraint "accounts_balance_check"
```
**Isolation** — see A2. **Durability** — committed data survives a crash. I committed one row, left a second row *uncommitted*, then hard-stopped Postgres (`pg_ctlcluster 16 main stop -m immediate`: no checkpoint, no clean shutdown, like `kill -9`) and restarted it:
```
WAL position advanced 0/1C21A6C0 -> 0/1C21A778 (184 bytes) for one committed INSERT
after crash recovery (WAL replay): [(1, 'committed row')]            <- committed row survived, uncommitted row is gone
```
> **KEY INSIGHT:** Durability = **write-ahead log (WAL)**. At commit, the *log record* is flushed (sequential, cheap) before the commit returns; the data pages can be written later. Crash recovery replays the log. `synchronous_commit=off` trades a small durability window for speed. "Consistency" in ACID is mostly *your* constraints (the DB enforces them; it can't invent your business rules).

### A2. Isolation Levels & Anomalies  ✅ RAN (two live sessions, interleaved deterministically)

| Anomaly | What happens |
|---|---|
| Dirty read | Read another transaction's *uncommitted* data |
| Non-repeatable read | Re-read the same row, value changed (someone committed) |
| Phantom read | Re-run the same range query, *new rows* appear |
| Lost update | Two read-modify-write cycles overwrite each other |
| Write skew | Two txns read the same set, write *different* rows, jointly breaking an invariant |

Measured on Postgres:
```
dirty read     @READ UNCOMMITTED : B sees 100   (uncommitted 999 invisible; PG treats it as READ COMMITTED)
non-repeatable @READ COMMITTED   : A reads 100 then 200  -> ANOMALY
non-repeatable @REPEATABLE READ  : A reads 100 then 100  -> stable
phantom        @READ COMMITTED   : A counts 2 then 3     -> ANOMALY
phantom        @REPEATABLE READ  : A counts 2 then 2     -> stable      (PG's snapshot isolation also blocks phantoms)
lost update (read in app, write back): expected 130, got 120
atomic UPDATE balance = balance + x   : got 130 (correct)
write skew     @REPEATABLE READ  : still on call = 0  (both commits succeeded)   <- invariant "≥1 doctor on call" BROKEN
write skew     @SERIALIZABLE     : still on call = 1  (second commit REJECTED, serialization failure 40001 -> app must retry)
```
**Matrix: what the SQL standard allows vs what PostgreSQL does**

| Level | Dirty | Non-repeatable | Phantom | Write skew |
|---|---|---|---|---|
| READ UNCOMMITTED | Standard: possible. **PG: prevented** (acts like RC) | possible | possible | possible |
| READ COMMITTED (PG default) | prevented | **possible** | **possible** | possible |
| REPEATABLE READ | prevented | prevented | Standard: possible. **PG: prevented** | **possible** |
| SERIALIZABLE | prevented | prevented | prevented | prevented (errors → retry) |

> **COMMON MISTAKE:** `SELECT balance` → compute in app → `UPDATE … SET balance = :new` (the lost-update pattern above). Fixes, in order of preference: a single atomic statement (`balance = balance + x`), `SELECT … FOR UPDATE`, optimistic version column (`WHERE version = :v`), or SERIALIZABLE with retry.
> **Dialect note:** MySQL/InnoDB's REPEATABLE READ is the default and handles phantoms differently (gap locks); Oracle has no true REPEATABLE READ. Always say *which* engine in an interview.

### A3. Indexing  ✅ RAN (1,000,000-row `orders` table, 103 MB)

```sql
CREATE TABLE orders (id bigint PRIMARY KEY, customer_id int, status text, created_at timestamp, amount numeric(10,2), note text);
-- 1M random rows, customer_id in [0, 50000], created_at over 2024
```
**Before vs after** — `SELECT * FROM orders WHERE customer_id = 4242` (23 matching rows):
```
BEFORE (no index)                                   AFTER CREATE INDEX idx_orders_cust (customer_id)
Seq Scan on orders (rows=23)                        Bitmap Heap Scan on orders (rows=23)
  Filter: customer_id = 4242                          Recheck Cond: customer_id = 4242
  Rows Removed by Filter: 999977                      Heap Blocks: exact=23
  Buffers: shared hit=13146                           ->  Bitmap Index Scan on idx_orders_cust
Execution Time: 82.356 ms                                   Index Cond: customer_id = 4242
                                                    Execution Time: 0.118 ms     (~700× faster)
                                                    index size: 7.8 MB
```
Read the plan: 13,146 buffers touched vs ~26. *Rows Removed by Filter: 999977* is the smell of a missing index.

**Composite index — column order matters.** Two candidates: `(customer_id, created_at)` and `(created_at, customer_id)`.
```
WHERE customer_id=4242 AND created_at >= '2024-06-01'   -> Bitmap Index Scan on idx_ca (customer_id, created_at)    0.055 ms   equality col first, then range
WHERE customer_id=4242                                  -> Bitmap Index Scan on idx_ca                              0.041 ms   leading column alone: OK
WHERE created_at >= '2024-12-25'                        -> uses idx_ac (created_at, customer_id)                   15.6 ms
   ...drop idx_ac, keep only (customer_id, created_at):
WHERE created_at >= '2024-12-25'                        -> Seq Scan on orders                                      127.2 ms   non-leading column alone: index NOT usable
```
> **Rule of thumb ("leftmost prefix"):** put **equality** columns first, then the **range/sort** column; put the most selective/most-used filter earliest. An index on `(a, b)` serves `a`, and `a AND b`, but not `b` alone. (Some engines, e.g. recent MySQL/Oracle, can do "skip scans"; don't rely on it.)

**Covering index → index-only scan.** `SELECT customer_id, amount FROM orders WHERE customer_id = 4242`
```
CREATE INDEX idx_cov ON orders (customer_id) INCLUDE (amount);  VACUUM ANALYZE orders;
  Index Only Scan using idx_cov on orders (rows=23)           plain index on (customer_id) instead:
    Heap Fetches: 0                                             Bitmap Heap Scan on orders  (Heap Blocks: exact=23)
  Execution Time: 0.053 ms                                      Execution Time: 0.103 ms
```
`Heap Fetches: 0` means the table was never touched. **Index-only scans need an up-to-date visibility map, so they depend on `VACUUM`** (MVCC, A4). On a busy table with stale vacuum you'll see non-zero heap fetches.

**When the index is NOT used** (all `SELECT *`, same table, indexes on `customer_id`, `status`, `created_at`):
```
customer_id = 4242                       -> Bitmap Heap Scan     0.106 ms
customer_id + 0 = 4242                   -> Seq Scan            90.45 ms   expression on the column defeats the index
customer_id::text = '4242'               -> Seq Scan           170.97 ms   cast on the column defeats the index
created_at::date >= '2024-12-25'         -> Seq Scan           207.73 ms   (the sargable form  created_at >= '2024-12-25'  used the index: 66.7 ms)
status = 'paid'  (~25% of rows)          -> Bitmap Heap Scan    86.7 ms    vs forced Seq Scan 94.3 ms: the index buys almost nothing
```
> **COMMON MISTAKE (sargability):** Wrapping the indexed column in a function/cast (`WHERE date(created_at) = …`, `WHERE lower(email) = …`) disables the index unless you create an *expression index* (`CREATE INDEX ON t (lower(email))`). Low-selectivity columns (status flags, booleans) are poor index candidates; consider a *partial index* (`WHERE status = 'new'`) when you query the rare value.
> My first attempt at this experiment used `SELECT count(*)`, and Postgres happily used an index-only scan over the *entire* index even for `customer_id + 0 = 4242`, which made the index look "used". Test with the real column list. Writing bad experiments is a real hazard.

**Costs of indexes:** extra storage (7.8 MB here for one int column), slower INSERT/UPDATE/DELETE (every index is maintained), more vacuum work, and planner choices to reason about. Index what your queries filter/join/sort on, not everything.

### A4. MVCC (Multi-Version Concurrency Control)  ✅ RAN (PostgreSQL, `pageinspect`)

**Idea:** Writers don't overwrite rows; they create a *new version*. Each version has `xmin` (txn that created it) and `xmax` (txn that deleted/replaced it). A reader sees only versions visible to its **snapshot**, so **readers don't block writers and writers don't block readers**.

```
time ─►
T1(reader, REPEATABLE READ)  BEGIN; SELECT → sees v1 ─────────────────────────────── SELECT → still v1 ─ COMMIT
T2(writer)                              UPDATE v1→v2 ── COMMIT
New session after T2                                                  SELECT → sees v2
```
```sql
CREATE TABLE mv (id int PRIMARY KEY, v text) WITH (autovacuum_enabled=false); INSERT INTO mv VALUES (1,'v1');
SELECT ctid, xmin, xmax, v FROM mv;
-- reader opens REPEATABLE READ snapshot, then another session: UPDATE mv SET v='v2' WHERE id=1;
```
```
after INSERT                     [('(0,1)', xmin=772, xmax=0, 'v1')]
reader (old snapshot) sees       [('v1',)]   | new session sees [('v2',)]
physical tuples (heap page)      [(lp=1, t_xmin=772, t_xmax=773, t_ctid=(0,2)), (lp=2, t_xmin=773, t_xmax=0, t_ctid=(0,2))]
                                  ^ old version, "deleted" by txn 773          ^ new version
after VACUUM (reader gone)       [(lp=1, flags=2 (redirect)), (lp=2, flags=1 (normal))]
```
The update created a **second physical tuple**; the old one lingers until no snapshot needs it, then **VACUUM** reclaims it (lp 1 became a redirect to lp 2: a HOT-chain optimization).

> **Consequences you must know:** (1) *Bloat:* heavy UPDATEs leave dead tuples; if autovacuum falls behind, tables/indexes grow and scans slow. (2) A **long-running transaction** pins old versions and stops VACUUM from cleaning, which is a classic production incident ("idle in transaction"). (3) Other engines differ: InnoDB keeps old versions in *undo logs* (in-place update), Oracle likewise; Postgres keeps them in the *heap*.

### A5. Partitioning  ✅ RAN (PostgreSQL, 12 monthly range partitions, same 1M rows)

```sql
CREATE TABLE orders_p (id bigint, customer_id int, created_at timestamp NOT NULL, amount numeric(10,2)) PARTITION BY RANGE (created_at);
CREATE TABLE orders_p_2024_03 PARTITION OF orders_p FOR VALUES FROM ('2024-03-01') TO ('2024-04-01');   -- ×12
```
```
rows per partition (first 3): [('orders_p_2024_01', 85512), ('orders_p_2024_02', 79656), ('orders_p_2024_03', 84079)]

WHERE created_at >= '2024-03-01' AND created_at < '2024-04-01'     partitions scanned: 1 of 12   13.3 ms
WHERE amount > 400  (no partition key in filter)                    partitions scanned: 12 of 12  158.7 ms

EXPLAIN (COSTS OFF) ... March filter:
  Aggregate
    ->  Seq Scan on orders_p_2024_03 orders_p
          Filter: ((created_at >= '2024-03-01 ...') AND (created_at < '2024-04-01 ...'))
```
**Pruning** = the planner discards partitions that can't match. It only works if the query filters on the **partition key** with a compatible expression. Benefits: faster scans, cheap retention (`DROP`/detach an old partition instead of a giant `DELETE`), smaller indexes per partition, parallel maintenance.
> **COMMON MISTAKE:** Partitioning by a column queries don't filter on (no pruning, more overhead), or creating thousands of tiny partitions (planning time explodes). Partitioning is *not* sharding: all partitions live on one server.

### A6. Replication: leader–follower lag  ✅ RAN (Python simulation)

```python
class Cluster:  # leader applies writes immediately; follower applies after `lag` ticks (async) or before ack (sync)
    ...
```
```
user updates profile then reloads (read routed to follower): None   <- stale: 'read-your-writes' violated
3 ticks later (replication caught up): Asha
async failover: leader dies; follower has 1/5 writes -> 4 acknowledged writes LOST on promotion
sync replication: follower has 5/5 writes (no loss), but each write waited 5 ticks for the follower
```
| | Async | Semi-sync / sync |
|---|---|---|
| Write latency | Low | Higher (waits for ≥1 follower) |
| Data loss on leader failure | Possible (un-replicated tail) | None for acknowledged writes |
| Availability | Leader doesn't depend on followers | A slow/down follower can stall writes |

**Mitigations for lag anomalies:** read-your-writes (route a user's reads to the leader for N seconds after they write, or pass an LSN and wait on the follower), monotonic reads (stick a user to one replica), monitor `pg_stat_replication` / `replay_lag`. **Leaderless** systems (Cassandra/Dynamo) use quorums (`R + W > N`) instead.

### A7. Row vs Columnar storage  ✅ RAN

Query shape: `AVG(amount)` over 1,000,000 rows of 8 columns.
```
row layout (loop over tuples)     :   94.7 ms
column layout (python list sum)   :   20.1 ms   (4.7× faster)
column layout (numpy, vectorised) :    1.3 ms   (74× faster)
bytes to read for 1 of 8 columns: row store reads ALL 8; column store reads ~12.5%
RLE on a sorted low-cardinality column: 100,000 values -> 4 runs [('DE',25075),('FR',24984),('IN',24999),('US',24942)]
```
Why columnar wins for analytics: reads only needed columns; same-type values compress far better (RLE, dictionary, delta encoding); vectorized (SIMD) execution. Row storage wins for OLTP: fetch/modify one whole row by key. (Python timings exaggerate/blur the true gap; the principle and the I/O math are the point.)

### A8. File Formats: Parquet vs ORC vs Avro vs CSV  ✅ RAN (500,000 rows × 6 columns)

```
FILE SIZES                          READ ONE COLUMN ('amount')
CSV                    24.1 MB      CSV (parses every field)        191 ms
Avro (deflate, rows)    6.8 MB      Avro (must decode whole rows) 2,032 ms
ORC (columnar)         11.7 MB      ORC (column pruning)              3 ms
Parquet snappy         10.3 MB      Parquet (column pruning)         21 ms
Parquet zstd            7.9 MB
```
> **Honest reading:** Avro's file came out *smallest* here because this synthetic data has repetitive values and I used deflate (Parquet/ORC defaults are lighter codecs), so "columnar is always smaller" is **not** a rule. The robust differences are the **read patterns**: columnar can skip unneeded columns (3 ms vs 2,032 ms), and Avro is the right choice for *row-at-a-time streaming* (Kafka) with embedded schema, not for analytics scans. (Some of the Avro read time is my pure-Python `fastavro` loop, but the direction is real.)

**Parquet internals ✅ RAN:**
```
rows=500,000 row_groups=5 columns=6
row_group 0: rows=100,000 | col 'amount': min=-0.00 max=500.00 comp=SNAPPY compressed=89KB uncompressed=89KB
```
File → **row groups** (~128 MB typical) → **column chunks** → pages. Each column chunk stores min/max statistics. **Predicate pushdown:** with `order_id` sorted across 10 row groups, filtering `order_id = 120000` needs only **row group [2]** (others skipped by min/max):
```
with pushdown 10 ms  vs  read-all-then-filter 94 ms   (1 row returned)
```
(Unsorted data → every row group's min/max spans the whole range → nothing skipped. **Sorting/clustering by the filter column is what makes pushdown work.**)

| Format | Layout | Schema | Best for |
|---|---|---|---|
| CSV/JSON | Row, text | None/loose | Interchange, debugging; never for large analytics |
| Avro | Row, binary | In file header, strong evolution rules | Streaming (Kafka), row-oriented writes |
| Parquet | Columnar | Typed, nested | Default for lakes/warehouses (Spark, Trino, Snowflake) |
| ORC | Columnar | Typed | Hive ecosystem; excellent compression and indexes |

### A9. CAP Theorem  ✅ RAN (simulation)

During a **network partition (P)**, a distributed system must choose **C**onsistency *or* **A**vailability:
```
CP (etcd/ZooKeeper/Spanner-style): partition -> REJECTED (cannot reach quorum -> unavailable but consistent)
AP (Dynamo/Cassandra-style)      : partition -> client1: OK on A only | client2: OK on B only
     diverged state A='x' B='y'  (both available, inconsistent)
     on heal -> last-write-wins keeps one and DROPS the other; vector clocks / CRDTs can merge
```
```python
# CP pseudocode: refuse writes without a majority
def write(key, val):
    acks = replicate_to_all(key, val)
    if acks < majority: raise Unavailable("no quorum")      # safe, but this client sees downtime
# AP pseudocode: accept locally, reconcile later
def write(key, val):
    local[key] = (val, vector_clock.tick()); async_replicate()      # always succeeds; may conflict
```
> **Nuances interviewers like:** CAP is about behaviour **during partitions**; without a partition you can have both. **PACELC** adds: *Else* (no partition) you trade **L**atency vs **C**onsistency. "CA" systems don't exist for distributed networks (a single-node Postgres is "CA" only because it isn't distributed). Tunable systems (Cassandra `QUORUM`) let you pick per query.

### A10. Connection Pooling  ✅ RAN (real Postgres + SQLAlchemy)

```
300 trivial queries, new connection each time : 5,194 ms (17.31 ms/query)
300 trivial queries via SQLAlchemy QueuePool  :   155 ms ( 0.52 ms/query)  -> 33× faster
max_connections = 100
opened 98 connections, then: FATAL: sorry, too many clients already     (a few slots are reserved for superusers)
```
Each Postgres connection is a **process** (~MBs of RAM + auth/TLS handshake), so "one connection per request" collapses under load. The fix:
```python
from sqlalchemy import create_engine
engine = create_engine("postgresql+psycopg2://app:***@db/prod",
    pool_size=10,         # steady-state connections kept open
    max_overflow=5,       # temporary extra under burst
    pool_timeout=30,      # wait this long for a free connection, else TimeoutError
    pool_recycle=1800,    # replace connections older than 30 min (avoid stale/firewalled sockets)
    pool_pre_ping=True)   # cheap liveness check before use
```
Pool exhaustion behaviour (pool_size=2, max_overflow=1, timeout=1 s; 5 threads each hold a connection 2 s):
```
[(0,'ok'), (1,'ok'), (2,'ok'), (3,'TimeoutError waiting for a pooled connection'), (4,'TimeoutError waiting for a pooled connection')]
```
> **Sizing:** total connections = (instances × pool_size) must stay under server `max_connections`. Many app pods → use an external pooler (**PgBouncer**, transaction mode; RDS Proxy). Note that transaction-mode poolers break session state (temp tables, `SET`, advisory locks, prepared statements in some modes). **Always release connections** (`with engine.connect()`): leaked connections are the #1 pool-exhaustion cause.

---

## Part B — Practice Problems (13)

*Try each 15 min first. Format: scenario → hint → solution → follow-up.*

### Easy (1–5)

**1. [Amazon] Pick the index.** `SELECT * FROM orders WHERE customer_id = ? AND status = 'paid' ORDER BY created_at DESC LIMIT 20`.
- *HINT:* equality columns, then the sort column.
- *Solution:* `(customer_id, status, created_at DESC)`: two equalities, then the ordering column, so rows are read already sorted and the `LIMIT` stops early (no sort).
- *Follow-up:* "If `status` has 4 values, is it worth being in the index?" → Often put it last or use a partial index; measure with `EXPLAIN`.

**2. [Meta] Will this use the index?** For an index on `(email)`: (a) `WHERE email = 'a@x.com'` (b) `WHERE lower(email) = 'a@x.com'` (c) `WHERE email LIKE 'a%'` (d) `WHERE email LIKE '%x.com'`.
- *Solution:* (a) yes. (b) no, unless an expression index on `lower(email)`. (c) yes for B-tree *prefix* patterns (with a C collation or `text_pattern_ops` in Postgres). (d) no: leading wildcard (use trigram/GIN or reverse-index tricks).
- *Follow-up:* "How do you make (b) work?" → `CREATE INDEX ON users (lower(email))`.

**3. [Google] Which isolation level?** Pick for: (a) a reporting query needing a stable snapshot, (b) a bank transfer, (c) a seat-booking uniqueness rule.
- *Solution:* (a) REPEATABLE READ (snapshot). (b) READ COMMITTED + atomic updates/row locks is typically enough; SERIALIZABLE if invariants span rows. (c) A **unique constraint** (cheapest and strongest), not an isolation level.
- *Follow-up:* "Why prefer constraints?" → they hold under any isolation level and any concurrency.

**4. [Stripe] Format choice.** You're writing 5 GB/day of event logs for analysts running SQL on 3 columns. Format?
- *Solution:* **Parquet** (zstd or snappy), partitioned by date. Column pruning + stats pushdown + compression.
- *Follow-up:* "And the Kafka messages feeding it?" → Avro/Protobuf with a schema registry (row-oriented, evolvable).

**5. [Flipkart] ACID letter.** A transaction commits, the server loses power, and the row is missing after restart. Which property failed and what's the likely misconfiguration?
- *Solution:* **Durability.** Likely `fsync=off`, `synchronous_commit=off`, a write cache without battery/flush guarantees, or an async replica promoted after leader loss.
- *Follow-up:* "How does the DB normally guarantee it?" → WAL flushed (`fsync`) before acknowledging commit.

### Medium (6–10)

**6. [Uber] Deadlock.** Two transactions each update rows A then B, and B then A, and Postgres kills one with `deadlock detected`. Fix.
- *Solution:* Acquire locks in a **consistent global order** (e.g. sort by id), keep transactions short, and retry on `40P01`. Don't paper over with long lock timeouts.
- *Follow-up:* "How do you find them?" → `log_lock_waits`, `pg_locks` joined with `pg_stat_activity`.

**7. [Netflix] Phantom/write skew.** "A meeting room can't be double-booked." Two users check availability and insert overlapping bookings concurrently, and REPEATABLE READ allows both. How do you enforce it?
- *Solution:* A declarative **exclusion constraint** (`EXCLUDE USING gist (room WITH =, tstzrange(start_ts,end_ts) WITH &&)`, which needs the `btree_gist` extension; I did not run this one) enforces it atomically. Otherwise SERIALIZABLE + retry, or lock the room row (`SELECT … FOR UPDATE`) before checking.
- *Follow-up:* "Why not just check in app code?" → check-then-insert is a race by definition.

**8. [Airbnb] Slow query diagnosis.** `EXPLAIN ANALYZE` shows `Seq Scan … Rows Removed by Filter: 9,990,000`, estimated rows=10, actual rows=10,000,000 elsewhere in the plan. What are your two hypotheses?
- *Solution:* (1) **Missing/unusable index** (check sargability, composite order). (2) **Stale statistics** → bad estimates (`ANALYZE`; check `default_statistics_target`, correlated columns → extended statistics). Large estimate-vs-actual gaps cause wrong join orders/methods.
- *Follow-up:* "Estimate is 1, actual is 1M, and it picked a nested loop?" → Classic bad-statistics disaster; fix stats or rewrite.

**9. [Databricks] Partition key.** 5 B-row events table, queried mostly by `event_date` and sometimes by `user_id`. Partition by?
- *Solution:* `event_date` (daily). Not `user_id` (millions of tiny partitions). Add clustering/indexing by `user_id` within partitions (Z-order / sort keys / index).
- *Follow-up:* "Hot partition today?" → today's partition takes all writes; fine, but consider sub-partitioning or hash for write spread if it's a bottleneck.

**10. [Amazon] Read replica staleness.** After checkout, the "order confirmation" page (served from a read replica) says "order not found" 1% of the time. Cause and fixes?
- *Solution:* Replication lag (read-your-writes violation). Fixes: read the order from the **leader** right after writing, pass the commit LSN and wait for the replica to catch up, or return the created order in the write response.
- *Follow-up:* "Cost of sending everything to the leader?" → defeats scaling; apply it only to the user's own recent writes.

### Hard (11–13)

**11. [Stripe] Index design under mixed workload.** Table `payments(id, merchant_id, status, created_at, amount)`, 2 B rows. Queries: (a) merchant dashboard: last 50 payments by merchant; (b) finance: sum(amount) per day for a date range; (c) ops: all `status='failed'` in last hour. Design indexes/partitions.
- *Solution:* Partition by `created_at` (monthly/daily) → (b) and (c) prune. (a) index `(merchant_id, created_at DESC)` per partition (equality first, then sort; `LIMIT 50` reads 50 index entries). (c) **partial index** `ON (created_at) WHERE status='failed'` (tiny, because failures are rare). (b) is an analytics scan: send it to a columnar replica/warehouse rather than OLTP.
- *Follow-up:* "Write amplification?" → each index costs on insert; 3 small targeted indexes beat 8 speculative ones.

**12. [FinTech] Idempotent payments.** A client retries a charge after a network timeout. How do you guarantee it's applied once?
- *Solution:* Client sends an **idempotency key**; server stores it with a `UNIQUE` constraint in the *same transaction* as the ledger write; on duplicate key, return the original result. (Implemented in the ledger below: duplicate key rejected, safe retry.)
- *Follow-up:* "Key reuse with different payload?" → compare a request hash; reject mismatches with 422.

**13. [Google] Choosing a store.** Pick storage for (a) user profiles with 100K reads/s global low-latency, (b) multi-region inventory counts that must never oversell, (c) 50 TB clickstream for ad-hoc SQL.
- *Solution:* (a) leaderless/replicated KV or wide-column (Dynamo/Cassandra/Bigtable): AP, tunable consistency, eventual OK. (b) A CP system with strong consistency for decrement (Spanner/CockroachDB, or single-region Postgres + reservation pattern): correctness over availability. (c) Object storage + Parquet + lakehouse/warehouse engine.
- *Follow-up:* "How would (b) degrade during a partition?" → minority side refuses writes (unavailable), which is the CP trade-off.

---

## Part C — Interview Simulations

### Sim 1 — "Why is this query slow?"  (real plan from my run)

```sql
SELECT * FROM orders WHERE customer_id + 0 = 4242;       -- 1M rows, index exists on customer_id
```
```
Seq Scan on orders (actual rows=23 loops=1)
  Filter: ((customer_id + 0) = 4242)
  Rows Removed by Filter: 999977
  Buffers: shared hit=13146
Execution Time: 90.450 ms
```
**Think out loud**
1. **Read the plan, don't guess.** `Seq Scan` + `Rows Removed by Filter: 999,977` for 23 returned rows = scanning the whole table to keep 0.002%. Buffers = 13,146 pages.
2. **Why no index?** An index on `customer_id` exists, but the predicate is on `customer_id + 0`, an *expression*, so the index can't be used (not **sargable**). Same for casts/functions.
3. **Fix:** rewrite to `customer_id = 4242` (→ Bitmap Index Scan, 0.106 ms, **~850× faster**), or create an expression index if the expression is intentional.
4. **Then check the other usual suspects:** stale stats (`ANALYZE`; estimate vs actual rows), wrong composite order, `SELECT *` preventing index-only scans, implicit type casts (`customer_id::text = '4242'` also seq-scans: 171 ms), missing `LIMIT`, lock waits.
5. **Verify:** re-run `EXPLAIN (ANALYZE, BUFFERS)`, and compare production-like data volumes (a plan on 1,000 rows proves nothing).

| Interviewer is evaluating | Green flag | Red flag |
|---|---|---|
| Method | Reads node types, actual vs estimated rows, buffers | "Add an index" before reading the plan |
| Depth | Names sargability, stats, composite order, covering index | Only knows "indexes make things fast" |
| Trade-offs | Mentions write cost/size of indexes | Suggests indexing every column |
| Verification | Re-measures with `EXPLAIN ANALYZE` | Assumes the fix worked |

### Sim 2 — "Design the storage layer for a FinTech ledger system."  ✅ schema executed on PostgreSQL 16

**Requirements to confirm:** no money created/destroyed; full audit trail; idempotent operations; exactness (no floats); balances queryable fast; regulatory retention; multi-currency; throughput?

**Design principles:** (1) **Immutable, append-only double-entry ledger**: corrections are new *reversing* entries, never updates. (2) Every transaction's entries **sum to zero**. (3) Money as **integer minor units** (`bigint` cents), never floating point. (4) **Idempotency keys** on every write. (5) Balances are *derived* (and cached/materialized), the ledger is the source of truth.

```sql
CREATE TABLE ledger_txns (txn_id uuid PRIMARY KEY, idempotency_key text UNIQUE NOT NULL,
                          posted_at timestamptz NOT NULL DEFAULT now(), description text);
CREATE TABLE ledger_entries (
  entry_id bigserial PRIMARY KEY, txn_id uuid NOT NULL REFERENCES ledger_txns(txn_id),
  account_id bigint NOT NULL REFERENCES accounts_l(account_id),
  amount_minor bigint NOT NULL CHECK (amount_minor <> 0),          -- +credit / -debit, integer minor units
  currency char(3) NOT NULL, created_at timestamptz NOT NULL DEFAULT now());
CREATE INDEX ON ledger_entries (account_id, entry_id);

CREATE FUNCTION no_mutation() RETURNS trigger AS $$ BEGIN RAISE EXCEPTION 'ledger is append-only'; END $$ LANGUAGE plpgsql;
CREATE TRIGGER t_no_upd BEFORE UPDATE OR DELETE ON ledger_entries FOR EACH ROW EXECUTE FUNCTION no_mutation();

CREATE FUNCTION check_balanced() RETURNS trigger AS $$
BEGIN IF (SELECT sum(amount_minor) FROM ledger_entries WHERE txn_id = NEW.txn_id) <> 0
      THEN RAISE EXCEPTION 'txn % is unbalanced', NEW.txn_id; END IF; RETURN NULL; END $$ LANGUAGE plpgsql;
CREATE CONSTRAINT TRIGGER t_balanced AFTER INSERT ON ledger_entries
  DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION check_balanced();   -- checked at COMMIT
```
Tested behaviour (real output):
```
balanced txn: committed
unbalanced txn rejected: txn aaade31c-... is unbalanced
duplicate idempotency_key rejected (safe client retry)
mutation rejected: ledger is append-only
balances (derived, minor units): [(1, -1999), (2, 1999)]
global sum of all entries (must be 0): 0
```
**Engine & isolation choices:** a relational, ACID, strongly-consistent store (Postgres/MySQL; or Spanner/CockroachDB for multi-region). Use **SERIALIZABLE** (or row-level locks on the account) for operations that enforce non-negative balances; keep the posting transaction short. A DB-level `CHECK`/trigger can't cheaply enforce "balance ≥ 0" across entries, so maintain a **balance row per account** updated with `SET balance = balance + :x WHERE balance + :x >= 0` inside the same transaction, as an invariant the DB enforces, and reconcile it against the ledger sum.
**Scale:** partition `ledger_entries` by time; shard by `account_id` (a cross-shard transfer needs 2-phase commit or a saga with a *pending/hold* state; prefer keeping both legs of a transfer on one shard via design); read replicas for statements/reports, **never** for balance checks that gate money movement; stream the ledger via CDC to the warehouse for analytics.
**Durability/DR:** synchronous replication to a second AZ (RPO = 0), PITR backups, WAL archiving; periodic **reconciliation** (ledger vs balances vs bank/processor files); full audit access logging.
**Failure modes:** retry after timeout (idempotency key), partial failure across services (outbox pattern + idempotent consumers), clock skew (use DB time / monotonic ids), reversal flows (new entries referencing the original).

| Evaluator is looking for | Green flag | Red flag |
|---|---|---|
| Money correctness | Integer minor units, double-entry, sum-to-zero invariant | Floats / `balance` column as the only truth |
| Immutability & audit | Append-only + reversing entries | `UPDATE`ing history |
| Concurrency | Idempotency, isolation level reasoning, locking strategy | "Just use transactions" |
| Scale & DR | Sharding with cross-shard story, sync replica, reconciliation | Single node, no recovery plan |

---

## Chapter 10 Cheat Sheet

- **ACID:** atomic (txn/rollback), consistent (constraints), isolated (levels), durable (**WAL**).
- Anomalies: dirty < non-repeatable < phantom < write skew. Postgres: RC default; RR blocks phantoms; only SERIALIZABLE stops write skew (retry on 40001).
- Prefer **atomic UPDATE / unique & exclusion constraints** over app-level check-then-write.
- Index: equality columns first, then range/sort; leftmost-prefix; **sargable** predicates; covering (`INCLUDE`) → index-only scan (needs vacuum); every index costs writes.
- Read plans: node type, `Rows Removed by Filter`, estimated vs actual rows, buffers.
- **MVCC:** readers don't block writers; updates create new versions; VACUUM reclaims; long transactions cause bloat.
- Partition by what queries filter on; prune; drop old partitions instead of mass-DELETE.
- Replication: async = fast but can lose/lag; mitigate read-your-writes; sync = safe but slower.
- Columnar for analytics (prune columns, compress, vectorize); Parquet row groups + min/max stats → pushdown; Avro for streaming rows.
- CAP: under partition choose C or A; PACELC adds latency/consistency otherwise.
- Pool connections (size × instances < `max_connections`); PgBouncer for many clients.

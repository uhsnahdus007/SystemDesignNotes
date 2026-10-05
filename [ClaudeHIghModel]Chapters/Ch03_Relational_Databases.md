# Chapter 3: Relational Databases: Storing Truth

## Chapter card

| | |
|---|---|
| **Objective** | Understand why databases exist, how relational databases model and store data, how indexes make queries fast, how transactions and isolation keep data correct under concurrency, and how to model data from access patterns |
| **Why this chapter exists** | Data is the hardest thing to scale and the most expensive thing to get wrong. Nearly every later chapter (caching, replication, sharding, consistency, distributed transactions) is an answer to a problem you first meet *here*, on one machine |
| **Prerequisites** | Chapters 0–2 (especially the race-condition and idempotency ideas) |
| **Study time** | 12–16 hours including labs |
| **Tier** | `[MK]` unless marked `[GK]`/`[ADV]` |
| **Mastery criteria** | (1) Design a normalized schema with keys and constraints and know when to denormalize. (2) Write SELECT/JOIN/GROUP BY/UPDATE and reason about what they cost. (3) Explain B-tree indexes, composite index ordering, covering indexes, and read an EXPLAIN plan. (4) Explain ACID with the write-ahead log. (5) Name the concurrency anomalies (dirty read, non-repeatable read, phantom, lost update, write skew), map them to isolation levels, and prevent them. (6) Choose between pessimistic locking, optimistic concurrency, and atomic conditional updates. (7) Explain connection pooling and its limits. (8) Model data from access patterns |

---

## 3.1 The Story: "TicketRush"

You're building **TicketRush**, which sells concert tickets. Version 0 keeps everything in program memory:

```java
Map<Long, Event> events;  Map<Long, List<Ticket>> ticketsByEvent;  Map<String, Account> accounts;
```
Then reality arrives, one problem at a time:

1. **Restart.** You deploy a fix. Every sale vanishes (RAM is volatile; Ch 1).
2. **Two servers.** Each has its own maps; they disagree about what's sold (Ch 2: state in the app tier).
3. **Search.** "Show me all events in Mumbai next month under ₹2,000." Your code loops over 5 million objects for every request.
4. **The crash mid-purchase.** A purchase debits the buyer's wallet, then the server dies *before* it reserves the seat. The money is gone; no ticket exists.
5. **The last seat.** Two users click "Buy" for the last seat at the same millisecond. Both succeed. One seat, two owners.
6. **The report.** Finance asks for revenue per event per day. You write 200 lines of loops. It takes 4 minutes and gives a different answer from last time because sales happened while it ran.
7. **Bad data.** A bug writes an order with `user_id = 99999` (no such user). Nothing stops it.

### Pause & Predict 3.1
> For each of the seven problems, write one sentence: *what capability would a storage system need to have to prevent it?* (E.g., problem 1 needs **durability**.) Then check yourself against §3.2.

---

## 3.2 Why databases exist

### 3.2.1 Naive solution 1: keep it in memory
✘ Lost on restart; limited by RAM; can't be shared by multiple servers.

### 3.2.2 Naive solution 2: write files
```text
orders.csv:  1,42,7,2025-03-01,1500
             2,43,7,2025-03-01,1500
```
Better (survives restart) but:
- **Concurrent writers corrupt files.** Two processes appending/rewriting at once interleave or overwrite each other.
- **No atomicity.** A crash mid-write leaves half a record.
- **Search = scan everything.** Finding one order = reading the file end to end.
- **No integrity rules.** Nothing enforces "user must exist," "price ≥ 0," "email unique."
- **No safe multi-step updates.** Debit + reserve can't be made all-or-nothing.
- **Every program reinvents the wheel**, badly.

### 3.2.3 What a database gives you (and which problem each solves)
| Capability | Solves |
|---|---|
| **Durability**: committed data survives crashes | #1 |
| **Shared access** over a network by many app servers; one source of truth | #2 |
| **Indexes & query engine**: find rows fast without scanning | #3 |
| **Atomic transactions**: multiple changes succeed or fail together | #4 |
| **Concurrency control / isolation**: simultaneous operations don't corrupt each other | #5, #6 |
| **Declarative queries (SQL)**: describe *what* you want; the engine chooses *how* | #6 |
| **Constraints**: the DB rejects invalid data | #7 |

> **Definition.** A **database** is software specialized in storing data **durably**, retrieving it **efficiently**, and letting many clients modify it **concurrently and safely**. A **DBMS** is that software (PostgreSQL, MySQL, Oracle, SQL Server...). People say "database" for both.

### 3.2.4 Where the database sits
```text
App servers (stateless) ──SQL over TCP──▶ Database server(s)
                                            ├─ Buffer pool (cached pages in RAM)
                                            ├─ Write-ahead log (sequential, fsync'd)
                                            └─ Data files + indexes (on SSD/NVMe)
```
The database is **the** stateful heart of most systems. Its limits (single-machine capacity, failure, consistency) drive Chapters 8–15.

---

## 3.3 The relational model

### 3.3.1 Concepts `[MK]`
- **Table (relation)**: a set of rows with the same columns. **Row (tuple)**: one record. **Column (attribute)**: a named, typed field.
- **Schema**: the declared structure (tables, columns, types, constraints). Relational databases are **schema-on-write**: data must fit the schema.
- **Primary key (PK)**: uniquely identifies a row; not null; the stable identity.
- **Foreign key (FK)**: a column whose values must match a PK in another table, enforcing **referential integrity**.
- **Constraints**: `NOT NULL`, `UNIQUE`, `CHECK (price >= 0)`, `PRIMARY KEY`, `FOREIGN KEY`, `DEFAULT`.

```text
users                          events                             orders
┌────┬───────┬────────────┐   ┌────┬──────────┬────────┬────────┐  ┌────┬─────────┬──────────┬───────┬────────┐
│ id │ name  │ email (UQ) │   │ id │ title    │ city   │ starts │  │ id │ user_id │ event_id │ seats │ total  │
├────┼───────┼────────────┤   ├────┼──────────┼────────┼────────┤  ├────┼─────────┼──────────┼───────┼────────┤
│ 7  │ Priya │ p@x.com    │◀─┐│ 31 │ Rock Fest│ Mumbai │ 3/20   │◀┐│ 90 │ 7  (FK) │ 31 (FK)  │ 2     │ 3000   │
│ 8  │ Arjun │ a@x.com    │  │└────┴──────────┴────────┴────────┘ │└────┴─────────┴──────────┴───────┴────────┘
└────┴───────┴────────────┘  └────────────────── FKs ─────────────┘
```

### 3.3.2 Relationships
- **One-to-many (1:N)**: one user → many orders. Put the FK on the "many" side (`orders.user_id`).
- **Many-to-many (M:N)**: users ↔ events via tickets. Needs a **join/junction table** (`tickets(user_id, event_id, ...)`).
- **One-to-one (1:1)**: user ↔ profile; FK with a UNIQUE constraint, or split a wide table.

### 3.3.3 Data types and some traps `[MK]`
- **Money: never use floating-point.** `0.1 + 0.2 ≠ 0.3` in binary floats. Use **integer minor units** (paise/cents: `BIGINT`) or `NUMERIC/DECIMAL`.
- **Time:** store in **UTC** (`TIMESTAMPTZ` in Postgres); convert at the edge. Beware daylight-saving and "wall-clock" times for events (store the venue's timezone separately).
- **IDs:** `BIGINT` auto-increment (compact, ordered, but guessable and awkward to generate across shards), **UUID** v4 (random, globally unique, bad index locality), **time-ordered IDs** (UUIDv7/ULID/Snowflake: unique *and* roughly sorted by time, good for indexes; Ch 10).
- **Text:** mind collation/encoding (UTF-8), length limits.
- **NULL** means *unknown/absent* and has three-valued logic (`NULL = NULL` is not true). Many bugs live here.

### 3.3.4 Normalization (and why it exists) `[MK]`

**Problem.** Suppose you store each order as one wide row:
```text
order_id | user_name | user_email | user_city | event_title | event_city | event_date | seats | price
```
Anomalies:
- **Update anomaly**: Priya changes her email → must update *every* order row; miss one → inconsistent.
- **Insertion anomaly**: can't record a new event until someone buys a ticket.
- **Deletion anomaly**: deleting Priya's only order erases the fact that the event exists.
- **Wasted space and drift**: the same strings repeated millions of times.

**Fix: normalization**: store each fact **once**, in one place, and refer to it by key.
- **1NF**: atomic values (no lists in a cell, no repeating groups).
- **2NF**: every non-key column depends on the *whole* key (matters for composite keys).
- **3NF**: non-key columns depend *only* on the key ("the key, the whole key, and nothing but the key").
Practical rule: one table per entity; one fact in one place; use FKs.

**Denormalization `[MK]`**: deliberately duplicating data to avoid expensive joins or to precompute (e.g., storing `events.tickets_sold` or copying `event_title` into `orders` for history). **Trade-off:** faster reads vs. risk of inconsistency and more write work. It's *common at scale* (feeds, counters, read models), but be deliberate and keep a single *source of truth*.

> **Snapshot vs. reference.** Some data *should* be copied: an order's `unit_price_paid` must not change if the event's price changes later. That's not redundancy; it's historical truth.

---

## 3.4 SQL: the language of relational data

### 3.4.1 Why a declarative language?
You write **what** you want; the **query planner** decides **how** (which index, join order, algorithm). This lets the database get faster as data grows or indexes appear *without changing your query*.

### 3.4.2 A working schema (PostgreSQL syntax; works nearly unchanged elsewhere)
```sql
CREATE TABLE users (
  id          BIGSERIAL PRIMARY KEY,
  name        TEXT        NOT NULL,
  email       TEXT        NOT NULL UNIQUE,
  city        TEXT,
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE events (
  id          BIGSERIAL PRIMARY KEY,
  title       TEXT        NOT NULL,
  city        TEXT        NOT NULL,
  starts_at   TIMESTAMPTZ NOT NULL,
  price_paise BIGINT      NOT NULL CHECK (price_paise >= 0),
  capacity    INT         NOT NULL CHECK (capacity >= 0),
  sold        INT         NOT NULL DEFAULT 0 CHECK (sold >= 0 AND sold <= capacity)
);

CREATE TABLE orders (
  id          BIGSERIAL PRIMARY KEY,
  user_id     BIGINT      NOT NULL REFERENCES users(id),
  event_id    BIGINT      NOT NULL REFERENCES events(id),
  seats       INT         NOT NULL CHECK (seats > 0),
  total_paise BIGINT      NOT NULL,
  status      TEXT        NOT NULL DEFAULT 'PAID',
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
```
Notice the **`CHECK (sold <= capacity)`**: the database itself refuses to oversell. Remember this; it comes back in §3.7.

### 3.4.3 The core statements `[MK]`
```sql
-- CREATE
INSERT INTO users(name,email,city) VALUES ('Priya','p@x.com','Mumbai');

-- READ: filter, project, sort, limit
SELECT id, title, price_paise
FROM   events
WHERE  city = 'Mumbai' AND starts_at BETWEEN '2025-03-01' AND '2025-03-31'
       AND price_paise <= 200000
ORDER  BY starts_at
LIMIT  20;

-- UPDATE / DELETE (always with a WHERE; test with SELECT first!)
UPDATE orders SET status = 'CANCELLED' WHERE id = 90;
DELETE FROM orders WHERE status = 'CANCELLED' AND created_at < now() - interval '2 years';
```

### 3.4.4 Joins `[MK]`
A **join** combines rows from tables on related columns.
```sql
-- INNER JOIN: only matching rows
SELECT u.name, e.title, o.seats
FROM   orders o
JOIN   users  u ON u.id = o.user_id
JOIN   events e ON e.id = o.event_id
WHERE  e.city = 'Mumbai';

-- LEFT JOIN: keep all users, even those with no orders (NULLs for missing)
SELECT u.id, u.name, COUNT(o.id) AS orders_count
FROM   users u LEFT JOIN orders o ON o.user_id = u.id
GROUP  BY u.id, u.name;
```
```text
users A,B,C   orders: A,A,C
INNER JOIN → A,A,C         LEFT JOIN → A,A,B(null),C
```
**How joins execute (so you can reason about cost):**
- **Nested loop**: for each row on the left, look up matches on the right. Great with an index on the join column and small outer sets.
- **Hash join**: build a hash table from the smaller side, probe with the other. Good for large unsorted sets (needs memory).
- **Merge join**: both sides sorted on the join key; stream through.
Joins are cheap *on one machine with indexes*, and become **very expensive across machines** (sharding, Ch 10): a core reason for denormalization at scale.

### 3.4.5 Aggregation `[MK]`
```sql
SELECT e.id, e.title,
       COUNT(*)           AS orders,
       SUM(o.seats)       AS seats_sold,
       SUM(o.total_paise) AS revenue_paise
FROM   orders o JOIN events e ON e.id = o.event_id
WHERE  o.created_at >= date_trunc('day', now())
GROUP  BY e.id, e.title
HAVING SUM(o.total_paise) > 1000000      -- HAVING filters groups; WHERE filters rows
ORDER  BY revenue_paise DESC;
```
**Window functions** `[GK]` compute per-row values over a "window" of related rows without collapsing them:
```sql
SELECT id, event_id, total_paise,
       SUM(total_paise) OVER (PARTITION BY event_id ORDER BY created_at) AS running_revenue,
       ROW_NUMBER()    OVER (PARTITION BY user_id ORDER BY created_at DESC) AS nth_latest
FROM orders;
```

### 3.4.6 Query anti-patterns (common interview and production issues)
- **`SELECT *`**: over-fetches, breaks covering-index benefits, couples to schema.
- **N+1 queries**: loading 100 orders then issuing 100 separate queries for each order's user. Fix: join or batch (`WHERE id IN (...)`). (Each query = a network round trip ~0.5 ms *plus* parse/plan; Ch 1 latency math.)
- **Functions on indexed columns**: `WHERE lower(email) = 'x'` can't use an index on `email` (use an expression index or store normalized).
- **Leading wildcards**: `LIKE '%foo'` can't use a B-tree index.
- **Implicit type conversions** that defeat indexes.
- **Unbounded result sets** and **deep OFFSET** (Ch 2).
- **Long-running queries/transactions** that hold locks or snapshots (§3.7).

---

## 3.5 How a database actually stores and finds data

### 3.5.1 Pages, heap, buffer pool `[MK]`
Disks and RAM are accessed in blocks. Databases organize data into fixed-size **pages** (8 KB in PostgreSQL, 16 KB in InnoDB). A table is a collection of pages; each holds many rows.

```text
Disk:   [page 0][page 1][page 2] ... [page N]      ← table (heap) file
RAM:    Buffer pool = cache of recently used pages (+ OS page cache)
Query ─▶ need row X ─▶ in buffer pool? yes → microseconds ; no → read page from disk (SSD ~100 µs)
```
Therefore: **a query's cost ≈ number of pages touched** (and whether they're in RAM). The whole point of indexes is to touch *fewer pages*.

### 3.5.2 The problem indexes solve `[MK]`
```sql
SELECT * FROM users WHERE email = 'p@x.com';     -- 50 million rows
```
Without an index: **sequential scan**. Read all pages (50 M rows × ~100 B ≈ 5 GB ≈ hundreds of thousands of pages). Seconds to minutes, and it hammers I/O for everyone else.

### 3.5.3 Better: the B-tree index `[MK]`
A **B-tree** (really B+tree) is a balanced, sorted, multi-way tree stored in pages.
```text
                        [ root: 100 | 200 | 300 ]                      level 0 (1 page)
                       /       |        |       \
          [..keys..] [..keys..] [..keys..] [..keys..]                  level 1 (hundreds of pages)
          /   |   \
   [leaf: key→row pointer ..][leaf ...][leaf ...] ←→ linked leaves     level 2+ (sorted, doubly linked)
```
- Each node (page) holds **hundreds** of keys (**high fanout**, say ~300–500). So depth stays tiny: 3 levels ≈ 300³ = 27 M entries; **4 levels ≈ 8 billion**.
- A lookup reads **~3–4 pages** (the top levels are almost always cached in RAM) instead of 600,000: that's the **O(log N)** win, with an enormous base.
- Leaves are **sorted and linked**, so **range queries** (`BETWEEN`, `ORDER BY`, `>`), prefix matches, and ordered scans are efficient.
- Inserts/deletes keep the tree balanced by splitting/merging pages.

**Intuition:** the index at the back of a book, but one that's sorted so you can also find "everything from *M* to *P*."

### 3.5.4 Index trade-offs `[MK]`
| Benefit | Cost |
|---|---|
| Reads on indexed columns dramatically faster | **Every INSERT/UPDATE/DELETE must also update every index** (write amplification) |
| Sorted access, range scans | Extra disk and RAM (indexes compete for the buffer pool) |
| Enforces uniqueness (`UNIQUE` is an index) | Too many indexes slow writes and bloat storage |
| | Index maintenance (bloat, rebuilds) |

**Rule:** index what you *filter, join, and sort on frequently*; don't index everything. Measure.

### 3.5.5 Index types and techniques `[MK/GK]`
- **Primary key index.** In **InnoDB/MySQL** the table *is* a B-tree clustered on the PK (**clustered index**); secondary indexes point to PK values. In **PostgreSQL** tables are heaps and *all* indexes are secondary pointing to physical row locations.
  - Consequence: in clustered tables, **PK choice affects physical layout**. Random UUID PKs cause scattered inserts (page splits, poor locality); sequential/time-ordered keys append neatly.
- **Secondary index**: on other columns.
- **Composite (multi-column) index**: `CREATE INDEX ON events(city, starts_at)`.
  - **Leftmost-prefix rule**: usable for queries filtering `city`, or `city AND starts_at` (and `city ORDER BY starts_at`), but **not** `starts_at` alone. Column order matters: **equality columns first, then range/sort column**.
- **Covering index / index-only scan**: if the index contains all columns the query needs, the DB never touches the table. (`INCLUDE (...)` in Postgres.)
- **Partial index**: index only rows matching a predicate, e.g., `WHERE status = 'PENDING'` (tiny, hot subset).
- **Expression index**: on `lower(email)`.
- **Unique index**: enforces uniqueness.
- **Hash index**: equality only (rarely better than B-tree).
- **GIN/GiST/inverted/spatial (R-tree)** `[GK]`: full-text, JSONB, arrays, geospatial (PostGIS).
- **Selectivity & cardinality:** an index on a low-cardinality column (e.g., `is_active` with 95% `true`) is rarely useful, because the planner will scan anyway since most rows match.

**Example: choosing the index for the Mumbai query**
```sql
SELECT id, title, price_paise FROM events
WHERE city = 'Mumbai' AND starts_at BETWEEN :a AND :b AND price_paise <= :p
ORDER BY starts_at LIMIT 20;
```
Best: `CREATE INDEX ev_city_start ON events(city, starts_at) INCLUDE (title, price_paise);`
- `city` (equality) first → narrows to Mumbai.
- `starts_at` (range + ordering) second → the index delivers rows already sorted; `LIMIT 20` stops early.
- `INCLUDE` makes it covering. (`price_paise <= :p` is applied as a filter on index entries.)

### 3.5.6 Reading a query plan `[MK]`
```sql
EXPLAIN ANALYZE SELECT * FROM users WHERE email = 'p@x.com';
```
Things to look for:
- **Seq Scan** on a big table in a hot query → missing index or unusable predicate.
- **Index Scan / Index Only Scan / Bitmap Heap Scan**: using an index.
- **Rows estimate vs actual**: large mismatches mean stale statistics (`ANALYZE`) and bad plans.
- **Sort** with big memory/disk spills; **Nested Loop** with huge outer sets; **Hash Join** spilling to disk.
- **Buffers**: pages read from cache vs disk.
**Habit:** *every* slow-query investigation begins with `EXPLAIN (ANALYZE, BUFFERS)`.

### 3.5.7 Durability machinery: WAL `[MK]`
**Problem:** a transaction changes several pages (table, indexes). A crash mid-way leaves a corrupt mix. Writing every changed page to disk synchronously is slow (random I/O).
**Solution: write-ahead logging (WAL / redo log).**
1. Before modifying data pages, append a **log record** describing the change to the **WAL** (a sequential file).
2. On **COMMIT**, `fsync` the WAL (sequential, fast): now the transaction is **durable**.
3. Data pages are modified in RAM and flushed to the data files lazily (**checkpoints**).
4. After a crash, **replay the WAL** from the last checkpoint to redo committed changes (and undo/ignore uncommitted ones).
```text
 BEGIN ─▶ change in buffer pool + WAL record (RAM) ─▶ COMMIT: WAL fsync ✔ → return success
                                       data pages written later in background (checkpoint)
 crash ─▶ restart ─▶ replay WAL ─▶ consistent state restored
```
**Trade-offs:** `fsync` per commit limits commit rate (~ thousands/s on SSD); **group commit** batches many transactions' fsyncs; you can relax durability (`synchronous_commit=off`) and risk losing the last few ms of commits for speed. **The WAL is also what replication ships to replicas** (Ch 9) and what point-in-time recovery uses.

---

## 3.6 Transactions and ACID

### 3.6.1 The problem (TicketRush #4)
```text
Buy 2 seats:  1) debit wallet 3000   2) reserve seats   3) create order
Crash between 1 and 2 → money gone, no seats.
```

### 3.6.2 Definition `[MK]`
A **transaction** groups operations into one **logical unit**: either *all* take effect or *none* do.
```sql
BEGIN;
UPDATE wallets SET balance_paise = balance_paise - 300000 WHERE user_id = 7;
UPDATE events  SET sold = sold + 2 WHERE id = 31;
INSERT INTO orders(user_id,event_id,seats,total_paise) VALUES (7,31,2,300000);
COMMIT;      -- or ROLLBACK;
```

### 3.6.3 ACID `[MK]`
| | Guarantee | Meaning / mechanism |
|---|---|---|
| **A**tomicity | All-or-nothing | If any step fails or the server crashes, partial effects are undone (undo information / WAL) |
| **C**onsistency | Valid state → valid state | The database enforces declared rules (constraints, FKs, triggers). *Note:* "C" in ACID is *application-defined invariants*; it's different from the "C" in CAP (Ch 15) |
| **I**solation | Concurrent transactions don't interfere improperly | Each behaves *as if* it ran alone (to a chosen degree) |
| **D**urability | Committed = permanent | WAL fsync, replication |

**Autocommit:** by default each statement is its own transaction; use explicit `BEGIN…COMMIT` for multi-step logic.

### 3.6.4 Pitfalls with transactions
- **Keep them short.** Long transactions hold locks and old row versions (bloat), blocking others. Never wait for user input or call slow external APIs inside a transaction.
- **Retries:** serialization failures and deadlocks are *normal*; the app must **retry the whole transaction** (with backoff).
- **Side effects outside the DB** (emails, payment APIs) aren't rolled back; use the **outbox pattern** (Ch 12/17).
- **Transactions span a single database** (or cluster that supports them). Across services they don't exist, and you'll need sagas (Ch 17).

---

## 3.7 Concurrency: isolation, anomalies, locking `[MK → ADV]`

This is the heart of the chapter, and the most reliably tested topic.

### 3.7.1 The problem: TicketRush #5, the last seat
```text
Event 31: capacity 100, sold 99.
T1: SELECT sold → 99               T2: SELECT sold → 99
T1: if sold < 100 → UPDATE sold=100     T2: if sold < 100 → UPDATE sold=100
Both create orders. 101 tickets exist for 100 seats.
```
(The same *lost update / read-modify-write race* as `counter++` in Ch 1.)

**Isolation** is the database's answer: the illusion that transactions run one at a time. The *perfect* illusion is **serializability**: the result equals some serial order. It's expensive, so databases offer weaker, cheaper **isolation levels**, which permit specific **anomalies**.

### 3.7.2 The anomalies `[MK]`
| Anomaly | What happens | Example |
|---|---|---|
| **Dirty read** | Reading another transaction's *uncommitted* changes | T1 updates a balance (not committed); T2 reads it; T1 rolls back → T2 used data that never existed |
| **Non-repeatable read** | Reading the same row twice in a transaction and seeing different committed values | T1 reads price=100; T2 updates to 120 and commits; T1 reads again → 120 |
| **Phantom read** | Re-running a query returns a different *set* of rows | T1: `SELECT … WHERE city='Mumbai'` (5 rows); T2 inserts a Mumbai event; T1 repeats → 6 rows |
| **Lost update** | Two read-modify-write cycles overwrite each other | The last-seat bug above |
| **Write skew** | Two transactions read overlapping data, make *disjoint* writes, jointly violating an invariant | Two doctors both check "≥2 on call," both go off call → 0 on call |

**Write skew deserves emphasis:** no single row is updated by both transactions, so row-level conflict detection doesn't see it. It requires *serializable* isolation, or explicit locking of what was *read*.

### 3.7.3 Isolation levels `[MK]`
| Level | Dirty read | Non-repeatable | Phantom | Lost update | Write skew |
|---|---|---|---|---|---|
| Read Uncommitted | possible | possible | possible | possible | possible |
| **Read Committed** | ✘ | possible | possible | possible* | possible |
| **Repeatable Read** | ✘ | ✘ | (depends on engine) | detected/prevented in snapshot engines* | possible (in snapshot isolation) |
| **Serializable** | ✘ | ✘ | ✘ | ✘ | ✘ |

*Engine specifics matter:*
- **PostgreSQL**: default **Read Committed**. Its "Repeatable Read" is actually **snapshot isolation** (each transaction sees a consistent snapshot; conflicting updates to the same row cause a serialization error), no phantoms in practice, but **write skew is still possible**. **Serializable** uses **SSI** (serializable snapshot isolation): detects dangerous patterns and aborts transactions; you must retry.
- **MySQL/InnoDB**: default **Repeatable Read** using MVCC snapshots plus **gap/next-key locks** that block phantoms for locking reads; behaviors differ from Postgres in subtle ways.
- Oracle: Read Committed/Serializable (snapshot-based). SQL Server: Read Committed (locking by default; snapshot options).
**Don't assume**: read your engine's documentation. In interviews, state the engine when making claims.

### 3.7.4 How databases implement isolation `[GK]`
**Locking (2PL: two-phase locking).** Readers take shared locks, writers exclusive locks, held until commit. Safe but readers block writers.
**MVCC (multi-version concurrency control).** Writers create *new versions* of rows; readers see a **snapshot** of the data as of their start and **don't block** (and aren't blocked by) writers. Used by PostgreSQL, MySQL/InnoDB, Oracle. Costs: old versions must be cleaned up (**VACUUM** in Postgres; purge in InnoDB), so **long-running transactions prevent cleanup and cause bloat**.
```text
Row "sold" versions:  v1(99) created by T10  → v2(100) created by T25 (T10..T24 snapshots still see v1)
```

### 3.7.5 Preventing the last-seat bug: four correct techniques `[MK]`

**Technique 1: Atomic conditional update (usually the best).** Push the check into a single statement; the DB handles atomicity.
```sql
UPDATE events SET sold = sold + :n
WHERE  id = :event AND sold + :n <= capacity;
-- check rows affected: 1 = success, 0 = sold out (no oversell, no explicit lock in your code)
```
The row lock taken by UPDATE serializes concurrent buyers; the `WHERE` re-evaluates after the lock. Combined with the `CHECK (sold <= capacity)` constraint it's belt and braces.

**Technique 2: Pessimistic locking: `SELECT … FOR UPDATE`.**
```sql
BEGIN;
SELECT sold, capacity FROM events WHERE id = 31 FOR UPDATE;   -- lock the row; others wait
-- app logic: if sold + n > capacity → ROLLBACK
UPDATE events SET sold = sold + :n WHERE id = 31;
INSERT INTO orders ...;
COMMIT;
```
✔ Simple mental model; essential when logic between read and write is complex. ✘ Holds locks (contention), risk of deadlocks; hot rows serialize throughput (**a single hot event row becomes a bottleneck**: one writer at a time).

**Technique 3: Optimistic concurrency (version column).**
```sql
SELECT id, sold, version FROM events WHERE id = 31;          -- version = 7
UPDATE events SET sold = sold + :n, version = version + 1
WHERE  id = 31 AND version = 7;                              -- 0 rows → someone else changed it → retry
```
✔ No locks held while the user thinks; scales when conflicts are rare. ✘ Under high contention many retries (starvation); needs retry logic.

**Technique 4: Serializable isolation + retry.**
```sql
BEGIN ISOLATION LEVEL SERIALIZABLE; ...; COMMIT;   -- on error 40001 (serialization_failure) → retry transaction
```
✔ Strongest correctness with simple code (even for write skew). ✘ Aborts under contention; every client must handle retries; some performance cost.

| | Atomic UPDATE | FOR UPDATE | Optimistic | Serializable |
|---|---|---|---|---|
| Code complexity | Lowest | Low | Medium | Low (+retry) |
| Contention behavior | Serializes on row | Serializes on row | Retries | Aborts/retries |
| Works for multi-row invariants | Limited | Yes (lock all rows consistently) | Limited | Yes |
| Holds locks across user think-time? | No | Avoid! | No | n/a |

### 3.7.6 Write skew fix example
Invariant: *at least one doctor on call.*
```sql
-- Under Repeatable Read/snapshot: both transactions pass this check, then each sets own row off-call.
BEGIN;
SELECT count(*) FROM doctors WHERE on_call AND shift = 12;      -- reads 2
UPDATE doctors SET on_call = false WHERE id = :me;              -- disjoint rows: no conflict detected
COMMIT;
```
Fixes: (a) **SERIALIZABLE** (SSI aborts one); (b) lock the *set* read: `SELECT … WHERE on_call AND shift = 12 FOR UPDATE`; (c) **materialize the conflict**: a single `shifts` row per shift updated by both; (d) a constraint/trigger enforcing the invariant.
**Lesson:** any invariant spanning multiple rows ("sum ≤ limit," "at least one," "no overlapping bookings") needs deliberate protection.

### 3.7.7 Deadlocks `[MK]`
```text
T1: lock row A ... wants row B        T2: lock row B ... wants row A      → circular wait
```
The DB's **deadlock detector** aborts one transaction (error); the app must **retry**. Reduce them by: **acquiring locks in a consistent order** (e.g., always lower user_id first when transferring between two accounts), keeping transactions short, and using indexes (so updates lock fewer rows).

```sql
-- Safe transfer: lock accounts in id order
BEGIN;
SELECT * FROM accounts WHERE id IN (:a,:b) ORDER BY id FOR UPDATE;
UPDATE accounts SET bal = bal - :x WHERE id = :a;
UPDATE accounts SET bal = bal + :x WHERE id = :b;
COMMIT;
```

### 3.7.8 Hot rows and contention at scale `[GK]`
A single hot row (global counter, popular event inventory) caps throughput at roughly *1 / (lock hold time)*. For a 2 ms transaction: ~500 updates/s per row. Techniques: split the counter into N sub-counters (**sharded counters**, sum on read), reservations/tokens (pre-allocate inventory chunks), queues that serialize per key (a single consumer per event), or move the hot path to an in-memory store with atomic ops and reconcile durably. (We'll revisit in Ticketmaster, Ch 27.)

---

## 3.8 Connections and pooling `[MK]`

**Problem.** Each database connection is expensive: a process/thread, memory (several MB in Postgres), authentication, TLS. 2,000 app instances × 20 connections each = 40,000 connections: the DB collapses long before CPU is exhausted.
**Naive:** one connection per request → handshake cost every request (Ch 1) and connection storms.
**Better: connection pools** (HikariCP, pgBouncer, RDS Proxy). A small set of long-lived connections is **reused**; requests borrow and return them.

```text
App instance (pool of ~10–20) ──┐
App instance (pool)           ──┼──▶ [pgBouncer pool] ──▶ Postgres (100–300 real connections)
App instance (pool)           ──┘
```
- **Sizing:** more connections ≠ more throughput. A database does useful work in parallel only up to roughly its core count/I/O parallelism (rule of thumb: connections ≈ (cores × 2) + spindles for the *active* workload). Excess connections just add context switching and lock contention. By Little's Law, if each query takes 5 ms and you need 2,000 qps, you need only ~10 busy connections.
- **Exhaustion:** if queries get slow, in-flight requests hold connections longer → pool exhausted → app threads wait → timeouts → cascading failure (Ch 1, 18). Always set **pool acquire timeouts** and **query timeouts**.
- **Transaction-mode pooling** (pgBouncer) multiplexes many clients over few DB connections but breaks session-level features (session variables, advisory locks, prepared statements in some modes).
- **Idle-in-transaction** sessions are poison: they hold locks and block vacuum; set `idle_in_transaction_session_timeout`.

---

## 3.9 Data modeling from access patterns `[MK]`

**Method (use in every design interview):**
1. List the **entities** and relationships.
2. List the **queries/access patterns** with frequencies (reads vs writes; point lookup vs range vs aggregate).
3. Draft a normalized schema (3NF).
4. **Add indexes** for the hot queries.
5. **Denormalize** only where measurements/estimates show a join or aggregate is too slow.
6. Check **write paths and invariants** (constraints, transactions).
7. Check **growth** (how big in 1/5 years? what's the partition key someday? Ch 10).

### Worked example: modeling SnapShare (photo app)
**Entities:** users, photos, follows, likes, comments.
**Access patterns (with estimates):**
1. Open a user's profile with latest 30 photos: very frequent.
2. Home feed: latest photos from people I follow: *the* hot read.
3. Like/unlike a photo; show like count: very frequent writes/reads.
4. Show who liked a photo (first 20): occasional.
5. Follow/unfollow: moderate.

```sql
CREATE TABLE photos (
  id BIGINT PRIMARY KEY,                 -- time-ordered ID (Snowflake/UUIDv7)
  user_id BIGINT NOT NULL REFERENCES users(id),
  object_key TEXT NOT NULL,              -- location in object storage (not the file!)
  caption TEXT, created_at TIMESTAMPTZ NOT NULL,
  like_count INT NOT NULL DEFAULT 0      -- denormalized counter (deliberate)
);
CREATE INDEX photos_user_created ON photos(user_id, created_at DESC);   -- pattern 1

CREATE TABLE follows (
  follower_id BIGINT NOT NULL, followee_id BIGINT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (follower_id, followee_id)                                 -- uniqueness + "who do I follow"
);
CREATE INDEX follows_followee ON follows(followee_id, follower_id);      -- "who follows me"

CREATE TABLE likes (
  photo_id BIGINT NOT NULL, user_id BIGINT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (photo_id, user_id)                                        -- idempotent like! duplicates rejected
);
```
Observations:
- **The PK on `likes(photo_id, user_id)` makes "like" naturally idempotent** (`INSERT ... ON CONFLICT DO NOTHING`): double-taps and retries are harmless.
- **`like_count` is denormalized** because `COUNT(*)` over millions of likes per photo per view is too slow; update it in the same transaction (or asynchronously/approximately at scale: Ch 12). *Trade-off:* extra write + possible drift; repair with periodic recount.
- **The feed query** (pattern 2) `SELECT p.* FROM photos p JOIN follows f ON f.followee_id = p.user_id WHERE f.follower_id = :me ORDER BY p.created_at DESC LIMIT 30` works for small scale, but joins and sorts across everyone I follow become the bottleneck at large scale → fan-out/precomputed timelines (Ch 26). **Recognizing when a relational query stops scaling is a core skill.**
- **Files live in object storage**; the DB has only keys/metadata (Ch 11).

### Soft deletes, audit, and history
- `deleted_at` columns (soft delete) preserve data but complicate uniqueness and queries (partial unique indexes help); consider privacy/erasure requirements.
- **Append-only/audit tables** for money-like data; never `UPDATE` ledger rows, only add compensating entries.

### Schema migrations without downtime `[GK]`
Big tables can't be altered casually: `ALTER TABLE` may lock for minutes.
**Expand → migrate → contract:**
1. *Expand:* add the new nullable column/table/index (online/concurrently: `CREATE INDEX CONCURRENTLY`).
2. Deploy code that **writes both** old and new.
3. **Backfill** in small batches.
4. Switch reads to the new structure.
5. *Contract:* stop writing old, drop it later.
Never deploy code and an incompatible schema change in one step: during rolling deploys *old and new code run simultaneously*.

---

## 3.10 Failure modes and operational realities

| Failure/issue | Symptoms | Cause | Mitigation |
|---|---|---|---|
| **Slow query** | Latency spike, CPU/IO high | Missing index, bad plan, stale stats | EXPLAIN, index, `ANALYZE`, query timeouts, kill long queries |
| **Lock contention** | Many queries "waiting," throughput collapse | Hot rows, long transactions | Short txns, ordering, sharded counters, queues |
| **Deadlocks** | Sporadic errors | Inconsistent lock order | Order locks, retry |
| **Connection exhaustion** | "too many connections," pool timeouts | Slow queries, leaks, too many app instances | Pooling, timeouts, sizing, limits |
| **Disk full** | Writes fail; DB may crash/read-only | Growth, WAL accumulation (stuck replica/replication slot), logs | Monitoring/alerts at 70–80%, retention, archiving |
| **Table/index bloat** | Growing size, slower scans | Long transactions block vacuum; heavy updates | Tune autovacuum, avoid long txns, reindex |
| **Replication lag** | Stale reads on replicas | Heavy writes, slow replica | Ch 9 |
| **Crash/power loss** | Restart → recovery | — | WAL replay (durable if fsync honest); verify disks/controllers don't lie about fsync |
| **Corruption/accidental deletion** | Wrong/missing data | Bugs, bad migration, human error | Backups, PITR, tested restores, least-privilege |
| **Failover split-brain** | Two primaries | Faulty failover | Fencing, consensus-based orchestration (Ch 16) |
| **Runaway ORM** | N+1, huge joins | Lazy loading | Query logging, review, batch |

---

## 3.11 Capacity and limits (what one relational node can do) `[MK]`

Rough numbers for a well-tuned modern server (NVMe, 16–64 cores, 128–512 GB RAM); *treat as order-of-magnitude, workload-dependent*:
- **Simple indexed reads**: tens of thousands to >100k/s (if cached in RAM).
- **Simple writes** (single-row, indexed): thousands to tens of thousands/s, bounded by WAL fsync, lock contention, index count.
- **Data size**: single-digit to tens of TB manageable (backups/restores, vacuum, and recovery time become the real limit, not just size).
- **Connections**: hundreds of *active*, thousands idle with pooling.
Meaning: a relational database on one big machine takes you **very far** (millions of users) before sharding is necessary. **Don't shard prematurely.** When you do outgrow it, the order is: optimize queries/indexes → cache → read replicas → vertical scale → functional split → shard (Ch 8–10).

---

## 3.12 When to use a relational database (and when not) `[MK]`
**Great fit:** structured data with relationships; need for transactions/invariants (orders, payments, inventory, accounts); ad-hoc queries and reporting; moderate scale; mature tooling.
**Weak fit / consider alternatives:** massive write throughput with simple access by key (wide-column/KV), flexible nested documents with few relations (document store), graph traversals (graph DB), time-series at huge volume (TSDB), full-text relevance (search engine), blobs/files (object storage), ephemeral fast state (Redis). Details in Chapter 4.

---

## 3.13 Interview perspective
- Be ready to **justify the data store** from access patterns, consistency needs, and scale. "We'll use Postgres because we need transactions on orders and inventory; reads ~5k/s fit a primary with replicas."
- **Know your anomalies**: when asked "how do you prevent double booking?" present the atomic conditional update first, then alternatives.
- **Indexes:** state the exact index for a given query (column order, covering) and the write-cost trade-off.
- **N+1 and pagination** mentions signal experience.
- **Transactions across services**: say "no distributed ACID here; we'll use sagas/outbox" (Ch 17).
- Mention **connection pooling** and **timeouts** when discussing failure.

---

## 3.14 Labs

### Lab setup (Docker)
```bash
docker run --name pg -e POSTGRES_PASSWORD=pw -p 5432:5432 -d postgres:16
docker exec -it pg psql -U postgres        # open two terminals for concurrency labs
```

### Lab 1: Indexes make the difference
```sql
CREATE TABLE users_big (id BIGSERIAL PRIMARY KEY, email TEXT, city TEXT, created_at TIMESTAMPTZ DEFAULT now());
INSERT INTO users_big(email, city)
SELECT 'user'||g||'@x.com', (ARRAY['Mumbai','Delhi','Pune','Chennai'])[1+(g%4)]
FROM generate_series(1, 2000000) g;
ANALYZE users_big;

EXPLAIN (ANALYZE, BUFFERS) SELECT * FROM users_big WHERE email = 'user1234567@x.com';   -- Seq Scan, ~hundreds of ms
CREATE INDEX users_big_email ON users_big(email);
EXPLAIN (ANALYZE, BUFFERS) SELECT * FROM users_big WHERE email = 'user1234567@x.com';   -- Index Scan, <1 ms
```
**Questions:** Compare *Buffers* before/after. What happens to insert speed with 5 extra indexes? (Time a bulk insert before/after.) Try `WHERE lower(email)=...` and fix with an expression index. Try a composite index `(city, created_at)` and check which queries use it.

### Lab 2: Reproduce the lost update (two psql sessions)
```sql
CREATE TABLE seat_inventory (event_id INT PRIMARY KEY, sold INT, capacity INT);
INSERT INTO seat_inventory VALUES (31, 99, 100);
```
Naive read-then-write, interleaved by hand:
```text
Session A: BEGIN; SELECT sold FROM seat_inventory WHERE event_id=31;     -- 99
Session B: BEGIN; SELECT sold FROM seat_inventory WHERE event_id=31;     -- 99
Session A: UPDATE seat_inventory SET sold = 100 WHERE event_id=31; COMMIT;
Session B: UPDATE seat_inventory SET sold = 100 WHERE event_id=31; COMMIT;   -- both "succeeded" → oversell!
```
Fix 1 (atomic conditional update), run in both sessions:
```sql
UPDATE seat_inventory SET sold = sold + 1 WHERE event_id=31 AND sold + 1 <= capacity;
-- the second returns "UPDATE 0": sold out, no oversell
```
Fix 2: `SELECT ... FOR UPDATE` (watch Session B *block* until A commits).
Fix 3: add a `CHECK (sold <= capacity)` constraint and see the error.

### Lab 3: Isolation levels, anomalies
```sql
CREATE TABLE acct (id INT PRIMARY KEY, bal INT); INSERT INTO acct VALUES (1,100);
-- Non-repeatable read at READ COMMITTED (default):
A: BEGIN; SELECT bal FROM acct WHERE id=1;            -- 100
B: UPDATE acct SET bal=150 WHERE id=1;                -- autocommit
A: SELECT bal FROM acct WHERE id=1;                   -- 150  (changed within A!)
A: COMMIT;
-- Now repeat with: A: BEGIN ISOLATION LEVEL REPEATABLE READ; ...  → A keeps seeing 100.
```
**Write-skew lab:**
```sql
CREATE TABLE doctors (id INT PRIMARY KEY, on_call BOOL); INSERT INTO doctors VALUES (1,true),(2,true);
A: BEGIN ISOLATION LEVEL REPEATABLE READ; SELECT count(*) FROM doctors WHERE on_call;   -- 2
B: BEGIN ISOLATION LEVEL REPEATABLE READ; SELECT count(*) FROM doctors WHERE on_call;   -- 2
A: UPDATE doctors SET on_call=false WHERE id=1; COMMIT;
B: UPDATE doctors SET on_call=false WHERE id=2; COMMIT;    -- both succeed → 0 on call (write skew)
-- Repeat with ISOLATION LEVEL SERIALIZABLE → one fails: "could not serialize access"; retry it.
```

### Lab 4: Deadlock
```sql
CREATE TABLE a (id INT PRIMARY KEY, v INT); INSERT INTO a VALUES (1,0),(2,0);
S1: BEGIN; UPDATE a SET v=1 WHERE id=1;
S2: BEGIN; UPDATE a SET v=1 WHERE id=2;
S1: UPDATE a SET v=2 WHERE id=2;      -- waits
S2: UPDATE a SET v=2 WHERE id=1;      -- ERROR: deadlock detected (one is aborted)
```
Then rewrite so both sessions touch rows in ascending id order, which eliminates the deadlock.

### Lab 5: Java JDBC, retry on serialization failure (needs the PostgreSQL JDBC driver on the classpath)
```java
import java.sql.*;

public class BuyTicket {
    static final String URL = "jdbc:postgresql://localhost:5432/postgres";

    // Returns true if purchased, false if sold out. Retries on deadlock/serialization failures.
    static boolean buy(long eventId, int n) throws Exception {
        for (int attempt = 1; attempt <= 5; attempt++) {
            try (Connection c = DriverManager.getConnection(URL, "postgres", "pw")) {
                c.setAutoCommit(false);
                c.setTransactionIsolation(Connection.TRANSACTION_READ_COMMITTED);
                try (PreparedStatement ps = c.prepareStatement(
                        "UPDATE events SET sold = sold + ? WHERE id = ? AND sold + ? <= capacity")) {
                    ps.setInt(1, n); ps.setLong(2, eventId); ps.setInt(3, n);
                    if (ps.executeUpdate() == 0) { c.rollback(); return false; }   // sold out
                }
                // ... insert order row here, in the same transaction ...
                c.commit();
                return true;
            } catch (SQLException e) {
                String state = e.getSQLState();        // 40001 serialization_failure, 40P01 deadlock_detected
                if ("40001".equals(state) || "40P01".equals(state)) {
                    Thread.sleep((long) (Math.random() * (50L << attempt)));   // backoff + jitter (Ch 1)
                    continue;                          // retry the WHOLE transaction
                }
                throw e;
            }
        }
        throw new IllegalStateException("too many retries");
    }
}
```
**Experiment:** spawn 200 threads calling `buy(31, 1)` on an event with capacity 100; verify exactly 100 succeed and `sold = 100`. Then switch to the naive read-then-write version and observe oversell.

---

## 3.15 Common misconceptions

1. **"SQL doesn't scale."** A single node scales far; sharded SQL (and NewSQL) scales further. The cost is complexity, not impossibility.
2. **"NoSQL = fast, SQL = slow."** Speed depends on access pattern, indexes, and architecture.
3. **"An index always helps."** Indexes slow writes, consume memory, and low-selectivity indexes are ignored.
4. **"Transactions make everything safe."** Only to the chosen isolation level; default levels allow lost updates and write skew in some patterns.
5. **"ACID's C = CAP's C."** Different concepts (invariants vs linearizability).
6. **"`SELECT` then `UPDATE` is fine."** It's the classic race; use atomic updates/locks/constraints.
7. **"More DB connections = more throughput."** Past a point, it's less.
8. **"Foreign keys are slow, so drop them."** They protect integrity; at extreme scale some teams relax them *deliberately* with compensating checks.
9. **"Normalize everything" / "denormalize everything."** Normalize the source of truth; denormalize measured hot paths.
10. **"Use UUID v4 for everything."** Random keys hurt index locality and write performance (especially in clustered tables).
11. **"Floats are fine for money."** They are not.
12. **"Rollback undoes emails I sent."** Only database effects roll back.

---

## 3.16 Chapter summary

- Databases provide **durability, concurrency control, indexed querying, transactions, and integrity**: capabilities you can't cheaply build yourself.
- **Relational model**: tables/keys/constraints; **normalize** to avoid anomalies; **denormalize** deliberately for read performance.
- **SQL** is declarative; **joins** are cheap locally, costly when data is distributed.
- **Storage**: pages, buffer pool, **B-tree indexes** (high fanout → 3–4 page reads), composite index order and covering indexes, **EXPLAIN**.
- **WAL** gives durability and crash recovery (and underpins replication).
- **ACID** and **isolation levels**; anomalies (dirty/non-repeatable/phantom/lost update/write skew); **MVCC vs locking**; **atomic conditional updates, `FOR UPDATE`, optimistic versions, serializable+retry**; **deadlocks** and lock ordering; **hot rows**.
- **Connection pooling** and timeouts are essential reliability tools.
- **Model from access patterns**; know when a relational query will stop scaling (feeds, global counters).

### Mental models
| Concept | Mental model |
|---|---|
| Database | A meticulous librarian with a card catalog, a logbook, and rules |
| Index | The sorted index at the back of a book |
| B-tree | A phone book organized in 4 levels of tabs |
| WAL | Writing the plan in a notebook *before* rearranging furniture |
| Transaction | An envelope: everything inside goes through or nothing does |
| MVCC | Each reader gets a photocopy of the ledger as of their start time |
| Pessimistic lock | A "do not disturb" sign |
| Optimistic concurrency | Check the page number you saw is still current before writing |
| Pool | A taxi rank: few cabs serving many riders |

---

## 3.17 Exercises

### A. Conceptual
1. For each TicketRush problem 1–7, name the database capability that prevents it.
2. Why are CSV files a poor database? List five failures.
3. Define PK, FK, UNIQUE, CHECK; give a TicketRush example of each.
4. Explain 1NF/2NF/3NF informally and the three anomalies normalization prevents. Give a case where denormalization is justified.
5. Why must money be `BIGINT` minor units or `NUMERIC` and never float?
6. Explain the leftmost-prefix rule with index `(a,b,c)`: which of these can use it? `WHERE a=1`; `WHERE b=2`; `WHERE a=1 AND c=3`; `WHERE a=1 AND b>5 ORDER BY b`; `WHERE b=2 AND c=3`.
7. Why does a B-tree stay shallow? Compute the depth for 1 billion rows with fanout 400.
8. What does a covering index do and why is it faster?
9. When would the planner ignore an index on `is_active` (95% true)?
10. Explain how WAL provides durability and fast commits. What does `fsync` do?
11. Define atomicity, consistency (ACID), isolation, durability with one example each.
12. Define dirty read, non-repeatable read, phantom, lost update, write skew with a tiny example each.
13. Which anomalies remain under Read Committed in PostgreSQL? Under its Repeatable Read? Under Serializable?
14. Explain MVCC and why long transactions are harmful.
15. Compare the four ways to prevent oversell. Which is your default and why?
16. What is a deadlock? Give two prevention techniques.
17. Why can a single hot row cap throughput? Give three ways to relieve it.
18. Why do you need connection pooling? Use Little's Law to size a pool for 3,000 qps at 4 ms/query.
19. What are `idle in transaction` sessions and why are they dangerous?
20. Explain expand/migrate/contract for renaming a column on a 500 GB table.

### B. SQL practice (use the TicketRush schema)
21. List users with no orders.
22. Revenue per city per month, only cities exceeding ₹10 lakh.
23. For each user, their most recent order (hint: window function or `DISTINCT ON`).
24. Top 5 events by seats sold in the last 7 days.
25. Detect "oversold" events (sold > capacity) if the CHECK constraint were missing.
26. Rewrite an N+1 pattern (one query per order to fetch the user) as one query.

### C. Index design
27. Query: `SELECT id FROM orders WHERE user_id=? AND status='PAID' ORDER BY created_at DESC LIMIT 10`. Propose the best index and explain column order.
28. A table has 100 M rows; `status='PENDING'` is 0.1% of rows and queried constantly by a worker. Propose the cheapest effective index.
29. Query: `WHERE created_at > now() - interval '1 day'` on an append-only table. Which index type/shape and why? What alternative scales better (hint: partitioning, Ch 10)?

### D. Concurrency design
30. **Bank transfer** between accounts A and B: write the transaction safely (no lost update, no deadlock, no negative balance).
31. **Double booking** of a meeting room for overlapping time ranges: describe at least two correct approaches (hint: exclusion constraint / lock the room row / serializable).
32. **Unique username claim** under race: how does the database guarantee uniqueness, and how do you handle the loser?
33. **Inventory with reservations:** user holds seats for 10 minutes, then pays. Design tables and the expiry mechanism; address concurrency.
34. **Counter at scale:** a page-view counter receives 50k increments/s on one row. Propose two designs.

### E. Debugging
35. A service's p99 jumps to 6 s at peak; DB CPU 20%; many sessions `waiting` on locks. A recent change added a transaction that calls a payment API before commit. Diagnose and fix.
36. After a migration `ALTER TABLE orders ADD COLUMN x INT NOT NULL DEFAULT 0` the site froze for 4 minutes (older engines/versions rewrite tables). Explain and propose a safer path.
37. Users sometimes see their just-posted comment missing after refresh. The app reads from replicas. (Preview of Ch 9) What's happening and what fixes exist?
38. Disk usage on the primary is exploding though the data size is stable. List suspects.

### F. Interview-style
39. "Design the schema and concurrency control for a seat-booking system." (outline answer)
40. "Explain isolation levels to a junior engineer in 3 minutes."
41. "When would you denormalize? Give an example and the risks."

### G. Teach-back
42. Teach "why indexes speed up reads but slow down writes" to a friend using the book-index analogy; then add the B-tree details.

---

## 3.18 Full answer key

**1.** #1 durability; #2 shared, networked source of truth; #3 indexes/query engine; #4 atomic transactions; #5 isolation/concurrency control (+ constraints); #6 SQL aggregation with consistent snapshots (MVCC); #7 constraints/foreign keys.

**2.** Concurrent writers corrupt data; no atomic multi-record updates; crash leaves partial writes; every search scans everything; no integrity rules; no access control or backup tooling; hard to share over a network; every app reinvents it.

**3.** PK: `events.id` uniquely identifies an event. FK: `orders.user_id → users.id` ensures the user exists. UNIQUE: `users.email` prevents duplicates. CHECK: `price_paise >= 0` (or `sold <= capacity`).

**4.** 1NF: atomic cells, no repeating groups. 2NF: every non-key attribute depends on the whole (composite) key. 3NF: non-key attributes depend only on the key, not on other non-key attributes. They prevent update, insert, and delete anomalies. Denormalization is justified e.g. for `like_count` on a photo (avoid counting millions of rows per page view) or copying `unit_price_paid` into order lines (historical truth); risks: drift/inconsistency, extra write cost.

**5.** Binary floating point can't represent most decimals exactly (0.1+0.2≠0.3), causing rounding errors; integer minor units or fixed-precision decimals are exact.

**6.** Index (a,b,c): `a=1` ✔; `b=2` ✘ (no leading column); `a=1 AND c=3` partially (uses `a`, filters `c`); `a=1 AND b>5 ORDER BY b` ✔ (equality then range/sort on b); `b=2 AND c=3` ✘.

**7.** Fanout ~400 means each level multiplies by ~400: 400³ = 64 M; 400⁴ = 25.6 B. So 1 B rows ≈ **4 levels** (3.4 rounded up), hence a lookup reads ≈4 pages (top levels cached in RAM).

**8.** It contains every column the query needs, so the database answers from the index alone (index-only scan), avoiding lookups into the table pages (fewer random I/Os).

**9.** If 95% of rows match, using the index means reading most of the table via many random lookups; a sequential scan is cheaper, so the planner chooses it.

**10.** Changes are recorded in a sequential log before data pages; commit fsyncs the log (cheap sequential write), making the transaction durable; data pages flush later. After a crash, replaying the WAL restores committed changes. `fsync` forces buffered data to stable storage (so a power loss can't lose it).

**11.** Atomicity: debit and credit both happen or neither. Consistency: constraints keep `sold ≤ capacity`. Isolation: two concurrent buyers don't corrupt each other's view. Durability: committed order survives a crash.

**12.** Dirty read: read uncommitted balance later rolled back. Non-repeatable: row read twice gives different committed values. Phantom: re-run a range query, new rows appear. Lost update: two read-modify-writes overwrite each other. Write skew: two doctors each check ≥2 on call, both go off call.

**13.** Postgres Read Committed: non-repeatable reads, phantoms, lost updates (if read-then-write), write skew possible. Repeatable Read (snapshot isolation): no dirty/non-repeatable/phantom reads; concurrent updates to the same row raise serialization errors (prevents lost update); write skew still possible. Serializable (SSI): none of these (with retries on serialization failures).

**14.** MVCC keeps multiple row versions; readers see a snapshot and don't block writers. Old versions can't be removed while an old snapshot might need them, so long transactions cause bloat, slow scans, and vacuum lag.

**15.** (1) Atomic conditional UPDATE: default: simplest, lock-minimal, correct. (2) `SELECT … FOR UPDATE`: when logic is complex; holds locks. (3) Optimistic version: when conflicts are rare/user think time. (4) Serializable + retry: strongest for multi-row invariants; needs retry logic.

**16.** Circular lock waits; the DB aborts one transaction. Prevent via consistent lock ordering, short transactions, fewer/narrower locks (good indexes), lock timeouts, avoiding user interaction inside transactions; always retry on deadlock.

**17.** Updates to the same row serialize on its lock: throughput ≈ 1/(hold time). Relief: sharded/striped counters, reservation tokens/chunked allocation, per-key queues/single-writer consumers, in-memory atomic counters with periodic flush, approximate counters.

**18.** Pools avoid per-request connection setup and cap DB connection count, protecting the server. Sizing: 3,000 qps × 0.004 s = 12 concurrent queries on average → ~15–30 pooled connections (headroom for variance), not hundreds.

**19.** Sessions that began a transaction and went idle; they hold locks/snapshots, block vacuum, can block other writers, and consume connections. Set idle-in-transaction timeouts and fix application code paths.

**20.** Expand: add the new column (nullable, online). Write both columns in app code. Backfill in small batches (throttled). Switch reads to the new column. Contract: stop writing the old column, then drop it later. Use concurrent index builds; avoid long locks; rehearse on a copy.

**21.** 
```sql
SELECT u.* FROM users u LEFT JOIN orders o ON o.user_id=u.id WHERE o.id IS NULL;
-- or WHERE NOT EXISTS (SELECT 1 FROM orders o WHERE o.user_id=u.id)
```
**22.**
```sql
SELECT e.city, date_trunc('month', o.created_at) AS month, SUM(o.total_paise) AS revenue
FROM orders o JOIN events e ON e.id=o.event_id
GROUP BY e.city, date_trunc('month', o.created_at)
HAVING SUM(o.total_paise) > 100000000;      -- ₹10 lakh = 1,000,000 rupees = 100,000,000 paise
```
**23.**
```sql
SELECT DISTINCT ON (user_id) * FROM orders ORDER BY user_id, created_at DESC;
-- or ROW_NUMBER() OVER (PARTITION BY user_id ORDER BY created_at DESC) = 1
```
**24.**
```sql
SELECT event_id, SUM(seats) s FROM orders WHERE created_at >= now() - interval '7 days'
GROUP BY event_id ORDER BY s DESC LIMIT 5;
```
**25.**
```sql
SELECT e.id FROM events e WHERE e.sold > e.capacity;   -- or compare SUM(orders.seats) per event vs capacity
```
**26.** `SELECT o.*, u.name FROM orders o JOIN users u ON u.id=o.user_id WHERE o.id IN (...)` (one query), or batch-load users with `WHERE id IN (...)`.

**27.** `CREATE INDEX ON orders(user_id, status, created_at DESC)`: equality columns first (`user_id`, `status`), then the sort column; the index returns rows in order so `LIMIT 10` stops early (partial index `WHERE status='PAID'` also works).

**28.** A **partial index**: `CREATE INDEX ON jobs(created_at) WHERE status='PENDING'`: tiny, fast to scan and maintain, only includes the hot subset.

**29.** A B-tree on `created_at` (append-only data keeps it hot at the right edge). Better at scale: **time-based partitioning** so the query touches only the latest partition(s) and old partitions can be dropped cheaply.

**30.**
```sql
BEGIN;
SELECT id, bal FROM accounts WHERE id IN (:a,:b) ORDER BY id FOR UPDATE;     -- consistent order → no deadlock
UPDATE accounts SET bal = bal - :x WHERE id=:a AND bal >= :x;                -- 0 rows → insufficient funds → ROLLBACK
UPDATE accounts SET bal = bal + :x WHERE id=:b;
INSERT INTO ledger(...);                                                     -- audit trail
COMMIT;
```
(With a client-supplied idempotency key to dedupe retries.)

**31.** (a) PostgreSQL **exclusion constraint** on `(room_id WITH =, tstzrange(start,end) WITH &&)` makes overlaps impossible: the DB rejects the second insert. (b) Lock the room row (`SELECT … FOR UPDATE`), check for overlap, insert. (c) SERIALIZABLE and retry. Prefer the declarative constraint when available.

**32.** A `UNIQUE` constraint/index: concurrent inserts, one succeeds and the other gets a unique-violation; the app catches it and returns 409/"username taken." Never "check then insert."

**33.** Tables: `seats(event_id, seat_id, status)`, `holds(seat_id, user_id, expires_at)`. Hold: `UPDATE seats SET status='HELD', held_by=:u, hold_expires=now()+'10 min' WHERE seat_id=:s AND (status='AVAILABLE' OR hold_expires < now())` (atomic); rows affected = 1 means success. Pay: `UPDATE seats SET status='SOLD' WHERE seat_id=:s AND held_by=:u AND hold_expires > now()`. Expiry: lazy via the predicate above and/or a sweeper job; add idempotency keys for payment.

**34.** (a) Striped counters: N rows `counter(shard_id, count)`, increment a random shard, sum on read. (b) Buffer in memory/Redis (`INCR`) and flush batches to the DB every few seconds; accept small loss or use a durable stream. (c) Append events to a log/queue and aggregate asynchronously.

**35.** Holding a transaction (with row locks) open across a slow external call: the lock hold time inflates, others queue (lock waits), connections stay busy. Fix: do external calls outside transactions (reserve → commit → call → confirm, or use outbox/sagas), set lock/statement timeouts, shorten transactions.

**36.** Some engines/versions rewrite the table or take an exclusive lock while adding a NOT NULL column with a default; the table was locked. Safer: add a nullable column, backfill in batches, add the constraint/default later (or use versions that add defaults as metadata-only), use online schema-change tools (gh-ost/pt-osc), rehearse.

**37.** Replication lag: the write went to the primary and the read to a replica not yet updated. Fixes: read-your-writes routing (read from primary for N seconds after a write or for the user's own data), session stickiness to primary, track a replication position/version token, synchronous replication for critical paths.

**38.** Suspects: a stuck replication slot or a lagging replica retaining WAL; archiving failures; long-running transactions blocking vacuum (bloat); runaway temp files; log growth; unvacuumed dead tuples from heavy updates; large index bloat.

**39 (sketch).** Entities: events, seats (or capacity counters), holds, orders, payments. Hot path: atomic conditional update or per-seat row updates; holds with TTL; payment via idempotency key; constraints (unique seat sale); isolation: Read Committed with atomic updates, or Serializable for multi-seat selections; scale: partition by event, cache read-only seat maps, queue for flash sales (virtual waiting room); failure handling: expire holds, reconcile payments.

**40 (sketch).** Explain anomalies via a shared Google Doc analogy; levels as a dial between speed and strictness; default levels, MVCC snapshots; recommend atomic updates/constraints plus retries; mention engine differences.

**41 (sketch).** Denormalize when a join/aggregate is a measured bottleneck (like counts, timeline precomputation, order snapshots); risks: inconsistency, extra writes, drift; mitigations: single source of truth, transactions/async updates, reconciliation jobs.

**42.** Book index analogy: finding words fast via a sorted list (cost: you must update the index whenever the text changes). Technical: B-tree lookups touch 3–4 pages; each write must also insert/update every index entry (possibly splitting pages), hence write amplification.

---

## 3.19 Flashcards

| Front | Back |
|---|---|
| Database capabilities | Durability, shared access, indexes/query engine, atomic transactions, isolation, constraints |
| Normalization goal | Store each fact once; avoid update/insert/delete anomalies |
| When to denormalize | Measured hot reads where joins/aggregates are too slow (accept drift risk) |
| Money type | `BIGINT` minor units or `NUMERIC`: never float |
| B-tree depth | ~3–4 levels for billions of rows (fanout hundreds) |
| Leftmost-prefix rule | Composite index usable only from its leading columns; equality first, range/sort last |
| Covering index | Index contains all needed columns → index-only scan |
| Index cost | Slower writes, more storage/memory |
| WAL | Log changes before data pages; fsync on commit; replay after crash |
| ACID | Atomicity, Consistency (invariants), Isolation, Durability |
| Dirty read | Reading uncommitted data |
| Lost update | Two read-modify-writes clobber each other |
| Write skew | Disjoint writes jointly violating an invariant (snapshot isolation allows it) |
| Postgres default isolation | Read Committed (RR = snapshot isolation; Serializable = SSI) |
| MVCC | Multiple row versions; readers don't block writers; vacuum needed |
| Oversell fix (default) | `UPDATE … SET sold=sold+n WHERE sold+n<=capacity` and check rows affected |
| Deadlock prevention | Consistent lock order, short txns, retry |
| Pool sizing | Concurrency ≈ qps × latency (plus headroom), small |
| Idle in transaction | Holds locks/snapshots; dangerous |
| Safe migration | Expand → dual-write/backfill → switch → contract |
| Hot row limit | Throughput ≈ 1/lock-hold-time; use striped counters/queues |

## 3.20 Glossary additions

| Term | Simple Meaning | Technical Meaning | Why It Exists | Example |
|---|---|---|---|---|
| DBMS | Database software | System managing storage, queries, transactions | Reliable shared data | PostgreSQL |
| Schema | Data blueprint | Declared tables/columns/constraints | Integrity | `CREATE TABLE` |
| Primary key | Row identity | Unique, non-null identifier | Addressing rows | `users.id` |
| Foreign key | Reference to another row | Constraint ensuring referenced PK exists | Referential integrity | `orders.user_id` |
| Normalization | Store each fact once | Decompose to 1NF–3NF | Avoid anomalies | users/orders split |
| Denormalization | Deliberate duplication | Precomputed/copied data | Faster reads | `like_count` |
| Join | Combine tables | Relational operator on keys | Relate entities | orders ⋈ users |
| Index | Sorted lookup aid | Auxiliary structure (B-tree) | Fast search | index on email |
| B-tree | Balanced sorted tree | High-fanout page-based tree | O(log N) lookups, ranges | Default index |
| Clustered index | Table sorted by key | Row storage ordered by PK | Locality | InnoDB PK |
| Covering index | Index has all columns | Index-only scan | Skip table access | `INCLUDE (title)` |
| Selectivity | How narrowing a filter is | Fraction of rows matched | Index usefulness | `email` high |
| Query plan / EXPLAIN | How the DB will run it | Planner's chosen strategy | Diagnose performance | Seq Scan vs Index Scan |
| WAL | Notebook before changes | Write-ahead sequential log | Durability, replication | Postgres WAL |
| Checkpoint | Flush point | Persist dirty pages, bound recovery | Faster restart | periodic |
| Transaction | All-or-nothing unit | Atomic, isolated group of operations | Correctness | `BEGIN…COMMIT` |
| ACID | Transaction guarantees | Atomicity, Consistency, Isolation, Durability | Reliable data | Bank transfer |
| Isolation level | Strictness of illusion | Anomalies permitted | Speed vs safety | Read Committed |
| MVCC | Versioned rows | Snapshot-based concurrency | Non-blocking reads | Postgres |
| Lock (row) | Do-not-disturb | Mutual exclusion on rows | Prevent conflicts | `FOR UPDATE` |
| Optimistic concurrency | Check before write | Version/CAS conditional update | Avoid locks | `WHERE version=7` |
| Deadlock | Mutual waiting | Circular lock wait | Detected & aborted | A↔B |
| Write skew | Disjoint writes break rule | Snapshot-isolation anomaly | Needs serializable | On-call doctors |
| Connection pool | Reused connections | Bounded set of long-lived DB connections | Protect DB | HikariCP, pgBouncer |
| Vacuum | Cleanup of old versions | Reclaim dead tuples | Prevent bloat | Autovacuum |

---

**Next: Chapter 4, NoSQL and Storage Engines.** Why relational databases sometimes aren't the answer, the four-plus NoSQL families (key-value, document, wide-column, graph, time-series, search), how storage engines work (B-tree vs LSM-tree, memtables, SSTables, compaction, Bloom filters), data modeling for access-pattern-driven stores (DynamoDB/Cassandra style), and how to choose, with labs.

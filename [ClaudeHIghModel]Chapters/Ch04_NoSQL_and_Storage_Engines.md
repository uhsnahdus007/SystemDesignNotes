# Chapter 4: NoSQL and Storage Engines

## Chapter card

| | |
|---|---|
| **Objective** | Understand why non-relational and specialized data stores exist, how the storage engines underneath them work (B-tree vs LSM-tree), what each major family is good and bad at (key-value, document, wide-column, graph, time-series, search, distributed SQL, analytics), and how to *choose* from requirements rather than fashion |
| **Why this chapter exists** | Chapter 3 gave you the relational default. Real systems frequently use several stores, each chosen for an access pattern. Knowing the internals (not just the brand names) lets you predict behavior, limits, and failure modes |
| **Prerequisites** | Chapter 3 (indexes, WAL, transactions), Chapter 1 (latency ladder, sequential vs random I/O) |
| **Study time** | 12–16 hours including labs |
| **Tier** | Engines and the decision framework `[MK]`; each family `[MK]` at concept level, `[GK]` at modeling level; analytics/NewSQL `[GK]` |
| **Mastery criteria** | (1) Explain the LSM write/read path (WAL → memtable → SSTable → compaction, Bloom filters) and compare it with B-trees on read/write/space amplification. (2) Describe each store family: data model, strengths, weaknesses, typical uses. (3) Model data for a query-first store (Cassandra/DynamoDB style): partition key, clustering/sort key, hot partitions. (4) Decide among SQL, KV, document, wide-column, graph, time-series, search for a given requirement set and justify it. (5) Explain OLTP vs OLAP. (6) Explain why dual writes between stores are dangerous and what to use instead (CDC/outbox) |

---

## 4.1 The Story: Four Products, Four Problems

You're on a platform team. Four product teams come to you in the same week. Each wants "a database."

1. **ChatNow** (messaging): 500 M messages/day, 99% of reads are "latest 50 messages in conversation X." Never joined with anything. Writes are constant and enormous.
2. **ShopFront** (catalog): products have wildly different attributes (a laptop has RAM and CPU; a shirt has size and fabric). New categories appear weekly. The product page needs the *whole product* in one fetch.
3. **FleetTrack** (telematics): 200,000 sensor records per second, almost never updated, queried by device and time range ("speed for truck 77 between 9:00 and 9:15"). Old data should age out or be down-sampled.
4. **ConnectU** (social): "show friends-of-friends who work at my company and live in my city." Deep relationship traversal.

Plus two supporting needs:
5. **Search:** "wireless noise cancelling headphones under 5000" with typos tolerated and relevance ranking.
6. **Session/leaderboard state:** sub-millisecond access with expiry.

### Pause & Predict 4.1
> Using only what you learned in Chapter 3, what would you do for each? Where would a single PostgreSQL instance struggle, and *why specifically*? (Consider: write rate, data shape, join depth, ranking, latency.) Write answers before reading on; we'll revisit them in §4.12.

---

## 4.2 Why non-relational stores emerged

### 4.2.1 What relational databases are *not* optimized for
Relational databases are superb general-purpose tools. They pay for generality:
- **Write path with B-trees:** in-place updates cause random I/O; each index multiplies write work.
- **Scaling out is hard:** joins, multi-row transactions, and global constraints assume data lives in one place.
- **Rigid schema** (schema-on-write): changing it on huge tables is operationally heavy.
- **Single-primary writes:** one machine accepts writes.
- **General query power** (ad-hoc joins) is something many workloads never use.

### 4.2.2 The trade that NoSQL systems make
They typically **give up some generality** (joins, cross-row transactions, ad-hoc queries, strong global constraints) to gain one or more of:
1. **Horizontal scalability by design** (data partitioned from day one).
2. **Very high write throughput** (log-structured storage).
3. **Flexible or specialized data models** (documents, graphs, time-series, key-value).
4. **Predictable low latency** at scale for known access patterns.
5. **Availability across failures/regions** (often with relaxed consistency).

> **"NoSQL" is not one technology.** It's a label for *everything that deliberately isn't a classic relational database*. Calling a graph database and a key-value cache both "NoSQL" is like calling a bicycle and a submarine both "non-cars." Always discuss the *specific family* and its *specific trade-offs*.

### 4.2.3 The direction of travel (don't over-correct)
Modern relational systems add JSON columns, partitioning, replication; many "NoSQL" systems add SQL-like languages and transactions (MongoDB multi-document transactions, DynamoDB transactions, Cassandra lightweight transactions); "NewSQL" systems (Spanner, CockroachDB) combine SQL with horizontal scale. The line has blurred. **Choose by access pattern, consistency need, scale, and operational reality, not by label.**

---

## 4.3 Storage engines: how data actually lives on disk

Every database sits on a **storage engine**: the component that decides how bytes are organized on disk and in memory. Two dominant designs: **B-tree** (update-in-place) and **LSM-tree** (log-structured, append-and-merge). Understanding both explains most performance behavior of most databases.

### 4.3.1 Recap: B-tree engines (Chapter 3)
- Data and indexes in fixed-size pages, updated **in place**.
- **Read:** O(log N) page reads (~3–4); excellent point and range reads.
- **Write:** find the page, modify it (random I/O), plus WAL append for durability; page splits as pages fill.
- **Used by:** PostgreSQL, MySQL/InnoDB, Oracle, SQL Server, SQLite, and most traditional DBs.
- **Characteristics:** predictable read latency; in-place update means *random writes*; fragmentation/bloat; one place per key.

### 4.3.2 The problem LSM solves
**Observation (Chapter 1):** sequential I/O is dramatically faster than random I/O (HDD: ~100× difference; SSD: smaller but significant, also wear-friendly).
**Question:** can we make *all* writes sequential?
**Idea:** never update in place. **Append** changes to a log, keep recent writes sorted in memory, and periodically write them as immutable sorted files. Reads must then check several places; background work merges the files.

### 4.3.3 The LSM-tree (Log-Structured Merge-tree) `[MK]`

```text
WRITE PATH                                           READ PATH (newest → oldest)
 put(k,v)                                             get(k)
   │                                                    │
   ├─① append to WAL (sequential, durable)              ├─① memtable (RAM)            ← newest
   │                                                    ├─② immutable memtable(s)
   └─② insert into MEMTABLE (sorted, in RAM)            ├─③ SSTable L0 (newest files) ┐ each guarded by a
        │ when full (e.g., 64 MB)                       ├─④ SSTable L1 ...            │ Bloom filter + sparse
        ▼                                               └─⑤ SSTable L2 ... (oldest)   ┘ index to skip files
   FLUSH → new immutable SSTable file on disk
        │ many files accumulate
        ▼
   COMPACTION (background): merge sorted files → fewer, larger files; drop overwritten values and tombstones
```

**Components**
- **WAL (commit log):** sequential append, fsync'd; used only for crash recovery of the memtable.
- **Memtable:** in-memory sorted structure (skip list / red-black tree). All recent writes land here; reads check it first.
- **SSTable (Sorted String Table):** an immutable file of key-value pairs sorted by key, plus: a **sparse index** (every Nth key → file offset), a **Bloom filter** (fast "definitely not here"), and metadata/checksums.
- **Tombstone:** a delete is a *write* of a marker ("this key is deleted") shadowing older values; the data physically disappears only when compaction merges past it (and, in distributed systems, after a grace period; see failure modes).
- **Compaction:** merges SSTables, keeping only the newest value per key and discarding obsolete data. Two main strategies:
  - **Size-tiered:** merge several similarly sized files into a bigger one. Lower write amplification; higher read and space amplification (many overlapping files).
  - **Leveled:** files organized in levels (L1, L2…), each level ~10× larger, non-overlapping key ranges within a level. Lower read amplification (≈ one file per level); higher write amplification (data rewritten as it moves down levels).
  - (Also time-window compaction for time-series: group by time so whole windows can be dropped cheaply.)

**Why Bloom filters matter here.** A lookup for a key that doesn't exist (or exists only in an old file) could otherwise touch *every* SSTable. A Bloom filter (Chapter 20, but here's the gist) answers "might this file contain key k?" with **no false negatives** and a small false-positive rate (e.g., 1%), using a few bits per key. Most files are skipped without any disk read.

### 4.3.4 The three amplifications `[MK]`
| | Meaning | B-tree | LSM (leveled) | LSM (size-tiered) |
|---|---|---|---|---|
| **Write amplification** | Bytes physically written per byte of user data | Moderate (page rewrites, WAL, splits) | **High** (data rewritten level by level; e.g., 10–30×) | Lower (a few×) |
| **Read amplification** | Disk reads per logical read | Low (≈ tree depth) | Low-moderate (one file per level; Bloom filters help) | Higher (check many overlapping files) |
| **Space amplification** | Disk used vs live data | Moderate (fragmentation, free space in pages) | Low (~1.1×) | Higher (up to 2× during compaction) |

**Compaction debt:** if writes outpace compaction, files pile up, reads slow down, and the system must **throttle writes** (write stalls). Compaction I/O competes with foreground traffic: a classic production tuning topic.

### 4.3.5 B-tree vs LSM: decision guidance `[MK]`
| | B-tree | LSM-tree |
|---|---|---|
| Write throughput | Good | **Excellent** (sequential, batched) |
| Point read latency | Predictable, good | Good with Bloom filters/caches; tail latency can spike during compaction |
| Range scans | Excellent | Good (merge across files) |
| Update-heavy random workload | Fine | Write-amplification cost |
| Disk wear (SSD) | More random writes | Fewer random writes, but compaction rewrites |
| Space efficiency | OK | Better compression (immutable sorted blocks) |
| Operational tuning | Vacuum/bloat | Compaction strategy, stalls |
| Typical systems | Postgres, MySQL, Oracle | Cassandra, RocksDB/LevelDB, HBase, ScyllaDB, many embedded engines (and under Kafka-like logs, a related append-only idea) |

> **Rule of thumb:** *write-heavy, append-mostly, large-scale* → LSM; *read-heavy, transactional, mixed updates, rich queries* → B-tree.

### 4.3.6 Other engine ideas to recognize `[GK]`
- **In-memory stores** (Redis, Memcached): data lives in RAM; durability optional via snapshots/append-only logs; limited by RAM, extremely fast.
- **Columnar storage** (Parquet, ClickHouse, warehouses): stores each *column* contiguously; ideal for scanning a few columns over billions of rows with great compression (§4.11).
- **Append-only logs** (Kafka, Chapter 13): the log *is* the database.
- **Copy-on-write B-trees** (LMDB, Btrfs-style): never overwrite pages; crash-safe snapshots.

---

## 4.4 Key-value stores `[MK]`

### 4.4.1 Problem → naive → better
**Problem:** user sessions: "give me the blob for session ID `9f3c…`, expire it in 30 minutes."
**Naive:** a SQL table `sessions(id, data, expires_at)` + a cleanup job. Works at small scale, but every request pays query parsing/planning overhead, the table is hot with updates, and expiry cleanup competes with traffic.
**Better:** a store whose entire API is `get(key)`, `put(key, value)`, `delete(key)` (+ TTL), engineered for speed and partitioning.

### 4.4.2 The model
`key → value` (opaque bytes or a small structure). No schema, no joins, no secondary queries (unless added). Because the key determines everything, **partitioning is trivial**: hash the key to pick a node (Chapter 10).

### 4.4.3 Families and examples
| Type | Examples | Notes |
|---|---|---|
| **In-memory cache / data-structure server** | Redis, Memcached | Sub-ms; RAM-bound; Redis offers rich structures and optional persistence |
| **Durable distributed KV** | DynamoDB, Riak-style systems, etcd (small, consistent), Aerospike | Disk-backed, replicated, horizontally partitioned |
| **Embedded KV engines** | RocksDB, LevelDB, LMDB | Library inside another system |

### 4.4.4 Redis in more depth `[MK]`
Redis is a single-node (or clustered) in-memory server with **rich data structures** and atomic operations:
| Structure | Use |
|---|---|
| String | Cache values, counters (`INCR`), locks (`SET NX PX`) |
| Hash | Objects (`user:42` → fields) |
| List | Queues, recent items |
| Set | Tags, unique visitors, membership |
| Sorted set (ZSET) | **Leaderboards**, scheduled jobs, rate-limit windows (score = timestamp/rank) |
| Bitmap / HyperLogLog | Daily active flags; approximate distinct counts (12 KB per HLL) |
| Stream | Log-like append-only data with consumer groups |
| Geo | Radius queries |
| Pub/Sub | Fire-and-forget messaging |

Key properties:
- **Command execution is effectively single-threaded** (I/O may be multi-threaded in recent versions). Each command is **atomic**, which is great for counters and locks, but *one slow command (e.g., `KEYS *`, a huge `SORT`) blocks everyone*.
- **Persistence options:** RDB snapshots (periodic point-in-time dumps), AOF (append-only file of commands, fsync policy `always`/`everysec`/`no`). Trade-off: durability vs throughput. Default posture: *Redis is a cache/fast store; if it's your sole copy of critical data, you must design deliberately for loss*.
- **Replication:** asynchronous primary→replica; **failover via Sentinel** or built-in **Redis Cluster** (data sharded into **16,384 hash slots** across nodes).
- **Memory limits and eviction:** `maxmemory` + policy (`allkeys-lru`, `volatile-ttl`, `noeviction`...). Memory fragmentation and big keys are operational hazards.
- **Typical latency:** ~0.1–1 ms in-datacenter; 50–200k+ ops/s per node for simple commands.
- **Common failure modes:** hot keys (single key saturating one node), big-key blocking, memory exhaustion/eviction storms, failover data loss (async replication), cache stampede on expiry (Chapter 8).

### 4.4.5 DynamoDB-style durable key-value (and "key-value plus") `[GK]`
- Tables of **items** addressed by a **partition key** (and optional **sort key**); item size limit ~400 KB; automatic partitioning; single-digit-ms latencies at any scale.
- **Capacity model:** provisioned or on-demand read/write capacity units; throughput is divided among partitions, so a **hot partition** (one key getting a disproportionate share) can be throttled even if the table has spare capacity overall.
- **Secondary indexes:** GSIs (global; eventually consistent, separate partitioning), LSIs (local; share the partition).
- **Consistency:** eventually consistent reads by default; strongly consistent reads optional (at higher cost); transactions available with limits.
- **Modeling style:** design tables from access patterns; "single-table design" puts several entity types in one table with composite keys (§4.13).
- *(Verify current limits and pricing in provider documentation; they change.)*

### 4.4.6 Strengths, weaknesses `[MK]`
**Strengths:** simplicity, speed, trivial horizontal partitioning, predictable latency, TTL support.
**Weaknesses:** no joins/ad-hoc queries; secondary access paths must be built by you; value-level queries limited; consistency semantics vary; hot keys.
**Use for:** caches, sessions, rate-limit counters, feature flags, shopping carts, user profile lookups by ID, idempotency-key stores, leaderboards (Redis), high-scale lookup tables.
**Avoid for:** relational integrity, complex reporting, multi-entity transactions (unless the product supports them well).

---

## 4.5 Document stores `[MK]`

### 4.5.1 Problem → naive → better
**Problem (ShopFront):** a laptop and a T-shirt have totally different attributes.
**Naive relational approach A:** one wide `products` table with hundreds of mostly-NULL columns. ✘ Bloated, unmaintainable.
**Approach B: EAV (entity-attribute-value):** `product_attributes(product_id, name, value)`. ✘ Querying "laptops with ≥16 GB RAM and Intel CPU" becomes a many-way self-join; types lost; slow.
**Approach C:** per-category tables ✘ new category = new table + migration + code changes.
**Better:** store each product as a self-describing **document** (JSON/BSON), fetched whole in one read.

### 4.5.2 The model
A **document** is a nested, self-contained record, typically JSON-like. Documents live in **collections**; each has an `_id`. Schema is flexible (**schema-on-read**): documents in a collection can differ; the application (or optional schema validation) interprets them.

```json
{
  "_id": "p_8841",
  "category": "laptop",
  "title": "AeroBook 14",
  "price_paise": 8499900,
  "specs": {"ram_gb": 16, "cpu": "Intel i7", "storage": {"type": "SSD", "gb": 512}},
  "tags": ["ultrabook", "14-inch"],
  "reviews_summary": {"count": 1250, "avg": 4.4},
  "variants": [{"sku": "AB14-16-512", "color": "silver", "stock": 31}]
}
```

### 4.5.3 The central design decision: embed vs reference `[MK]`
| Embed (nest inside the document) | Reference (store ID, look up separately) |
|---|---|
| Data read together; one round trip | Data is large, unbounded, or shared by many parents |
| One-to-few relationships (an order's line items) | One-to-many/many-to-many at scale (a user's millions of posts) |
| Data changes together (atomic single-document update) | Data changes independently/frequently |
| Document size stays bounded (e.g., MongoDB's 16 MB cap; performance degrades long before) | Avoids document growth, write amplification |

**Anti-pattern:** unbounded arrays (a post's comments embedded in the post document) → documents grow forever, rewrite costs explode, hits size limits. Use a separate `comments` collection referencing the post, perhaps with a bounded "latest 20 comments" embedded copy.
**Rule:** *model for how data is read*; **duplicate** where it speeds reads, accepting update complexity (denormalization is the norm in documents).

### 4.5.4 Capabilities and engines
- **MongoDB** (BSON documents), **Couchbase**, **Firestore/Cosmos DB** (managed), **DocumentDB**. PostgreSQL's `jsonb` provides a hybrid.
- **Indexing:** secondary indexes on any field (including nested, arrays, compound), text, geospatial. As in SQL, index design is essential.
- **Atomicity:** operations on a **single document** are atomic (a major reason to embed). Multi-document ACID transactions exist in modern MongoDB (replica sets and sharded clusters) but cost performance; if you need them everywhere, reconsider the model/store.
- **Scaling:** replica sets (primary + secondaries; elections via a Raft-like protocol), **sharding** by a shard key (choose carefully; Chapter 10).
- **Consistency knobs:** write concern (how many replicas must acknowledge), read concern, read preference (primary vs secondary: stale reads possible).

### 4.5.5 Strengths, weaknesses `[MK]`
**Strengths:** flexible/evolving schemas; whole-aggregate reads in one round trip; natural fit for object-shaped data; easy iteration.
**Weaknesses:** joins are limited/expensive; duplication → update anomalies; unbounded growth traps; schema flexibility becomes schema *chaos* without discipline (versioning fields, validation); weaker cross-document guarantees historically.
**Use for:** product catalogs, content management, user profiles/preferences, event/log payloads, mobile backends with evolving shapes.
**Avoid for:** highly relational data with many-to-many joins and strict invariants (finance, inventory with multi-row constraints) unless carefully modeled.

---

## 4.6 Wide-column stores `[MK/GK]`

### 4.6.1 Problem (ChatNow)
500 M messages/day = ~5,800 writes/s average, much higher at peak. Reads: "latest 50 messages in conversation C." A B-tree RDBMS on one primary will saturate on writes and storage (100 B/message → 50 GB/day, 18 TB/year) and you'd have to shard manually.

### 4.6.2 The model (Bigtable/Cassandra lineage)
Despite the name, think of it as **a distributed, sorted map of maps**:
```text
(partition key) → { (clustering key 1) → columns..., (clustering key 2) → columns..., ... }
                   ↑ rows within a partition are stored contiguously, SORTED by clustering key
```
- **Partition key:** decides *which node(s)* hold the data (hash → token ring). All rows with the same partition key are co-located. **Queries must specify the partition key.**
- **Clustering (sort) key:** orders rows *within* a partition on disk; enables efficient range scans inside one partition (e.g., latest N).
- **Columns:** can vary per row; sparse.
- Storage engine: **LSM-tree** (hence massive write throughput).

### 4.6.3 Modeling ChatNow (Cassandra CQL) — *query-first design*
**Step 1: list the queries.**
- Q1: latest N messages in a conversation (paged backwards by time).
- Q2: messages since timestamp T (for sync).
**Step 2: design the table to answer exactly those, with one partition read.**
```sql
CREATE TABLE messages_by_conversation (
  conversation_id uuid,
  bucket          int,            -- e.g., yyyymm: bounds partition size (see below)
  message_id      timeuuid,       -- time-ordered unique ID (also the clustering key)
  sender_id       bigint,
  body            text,
  PRIMARY KEY ((conversation_id, bucket), message_id)
) WITH CLUSTERING ORDER BY (message_id DESC);

-- Q1:
SELECT * FROM messages_by_conversation WHERE conversation_id = ? AND bucket = 202603 LIMIT 50;
```
- **Composite partition key `(conversation_id, bucket)`**: prevents a long-running conversation from becoming one **unbounded partition**.
- **Clustering by `message_id DESC`**: the latest messages are physically first, so `LIMIT 50` reads one small contiguous slice.

**Step 3: accept duplication for other queries.** "Show my inbox sorted by last message" needs another table `conversations_by_user(user_id, last_message_at, conversation_id,...)`, written alongside the message (denormalization, with the application responsible for keeping them in sync, often via batch/idempotent writes or events).

### 4.6.4 Rules of thumb (Cassandra-family) `[GK]`
- **Model per query**, not per entity. Duplicate data across tables. Storage is cheap; cross-partition queries are expensive.
- **No joins, limited aggregations.** `WHERE` must use the partition key (and clustering prefix). Secondary indexes exist but are limited; avoid for high-cardinality or heavy use.
- **Partition size:** keep partitions bounded (rule of thumb: tens of MB, well under 100 MB, and not millions of cells); time-bucket if data grows without bound.
- **Hot partitions:** one celebrity conversation or one popular device overloads its replicas; spread with more granular partition keys or salting.
- **Tombstones:** deletes (and TTL expiries, and writing nulls) create tombstones that must be scanned over until compaction and a grace period (`gc_grace_seconds`, default 10 days) passes. Queue-like patterns (insert then delete) create "tombstone hell": reads time out scanning dead markers. *Avoid using Cassandra as a queue.*
- **Consistency is tunable per query:** replication factor N (commonly 3), write consistency W, read consistency R; with `R + W > N` reads see the latest acknowledged write (e.g., QUORUM/QUORUM), or choose `ONE` for speed/availability (Chapter 15). Lightweight transactions (compare-and-set, Paxos-based) exist but are slow, so use sparingly.
- **Multi-datacenter replication** is a first-class feature.
- **Operational reality:** repairs (anti-entropy using Merkle trees), compaction tuning, JVM/GC tuning (Scylla is a C++ reimplementation), capacity planning for rebalancing.

### 4.6.5 Systems
**Cassandra / ScyllaDB** (peer-to-peer, leaderless, AP-leaning, tunable), **HBase / Bigtable** (single-master-per-region architecture on a distributed file system, strongly consistent per row), **DynamoDB** (managed; wide-column-ish with partition+sort key).

### 4.6.6 Strengths, weaknesses `[MK]`
**Strengths:** massive write throughput; linear horizontal scale; multi-DC; predictable single-partition latency; high availability.
**Weaknesses:** rigid query-driven modeling; no joins or ad-hoc queries; operational complexity; eventual consistency subtleties; tombstones; expensive to change access patterns later.
**Use for:** messaging history, activity feeds/timelines, IoT/telemetry, time-bucketed event stores, large-scale user-state, write-heavy workloads with known queries.
**Avoid for:** ad-hoc analytics, strong multi-row transactions, small datasets (operational overhead not worth it), unknown/changing query patterns.

---

## 4.7 Graph databases `[GK]`

### 4.7.1 Problem (ConnectU)
"Friends-of-friends in my city who work at my company." In SQL that's a self-join of the `follows`/`friendships` table *per hop*:
```sql
SELECT DISTINCT f2.friend_id
FROM friendships f1 JOIN friendships f2 ON f1.friend_id = f2.user_id
JOIN users u ON u.id = f2.friend_id
WHERE f1.user_id = :me AND u.city = :c AND u.company = :co;
```
Two hops is OK with good indexes. Five hops ("6 degrees"), variable-length paths, or "shortest path" blows up: each hop multiplies rows (fan-out^hops).

### 4.7.2 The model
A **property graph**: **nodes** (entities, with labels and properties) and **edges/relationships** (typed, directed, with properties). Crucially, nodes store **direct pointers to their neighbors** ("index-free adjacency" in native graph DBs), so traversing an edge costs O(1) regardless of total graph size, versus an index lookup per hop in SQL.

```text
(Priya:Person {city:"Mumbai"}) -[:WORKS_AT]-> (Acme:Company)
(Priya) -[:FRIEND]-> (Arjun:Person) -[:FRIEND]-> (Meera:Person {city:"Mumbai"}) -[:WORKS_AT]-> (Acme)
```
**Query (Cypher, Neo4j):**
```cypher
MATCH (me:Person {id: $id})-[:FRIEND*2..2]-(fof:Person)-[:WORKS_AT]->(c:Company)<-[:WORKS_AT]-(me)
WHERE fof.city = me.city AND NOT (me)-[:FRIEND]-(fof) AND fof <> me
RETURN DISTINCT fof LIMIT 20;
```

### 4.7.3 Systems, strengths, weaknesses
**Systems:** Neo4j, Amazon Neptune, JanusGraph (on top of Cassandra/HBase), TigerGraph, ArangoDB (multi-model); graph processing frameworks for offline analytics.
**Strengths:** natural modeling of relationships; deep traversals, path finding, pattern matching, recommendations, fraud rings, knowledge graphs, access-control graphs.
**Weaknesses:** **hard to shard** (graphs have no natural partition boundary; cutting edges creates cross-machine traversals); bulk analytics and aggregates are not its strength; smaller ecosystem; supernodes (a celebrity with 50 M edges) cause trouble.
**Use for:** relationship-heavy queries where traversal depth matters. **Often a secondary index** of relationships fed from the primary store, not the system of record.
**Avoid for:** simple key lookups, massive append workloads, ordinary CRUD.

---

## 4.8 Time-series databases `[GK]`

### 4.8.1 Problem (FleetTrack)
200,000 records/s, each `(device_id, timestamp, speed, lat, lon, engine_temp, ...)`. Access pattern: write-once, append-mostly, mostly *recent* data, queries by device and time range, aggregation over windows ("avg speed per minute"), retention policies ("raw for 30 days, 1-minute rollups for 1 year").
General-purpose B-tree tables struggle: index bloat, random inserts, inability to cheaply drop old data (a `DELETE` of 10 B rows is a disaster; Chapter 3's vacuum story).

### 4.8.2 What TSDBs do differently `[GK]`
- **Time-partitioned storage:** data chunked by time windows (e.g., daily chunks/"hypertables"). Writes hit the newest chunk (hot, cached); **dropping old data = dropping whole chunks** (instant, no vacuum).
- **Compression:** time-series values change slowly. **Delta-of-delta encoding** for timestamps and **XOR/Gorilla-style compression** for floats achieve ~1–2 bytes per sample `[Public: Facebook's Gorilla paper]`: 10–20× smaller than raw.
- **Downsampling & continuous aggregates:** precompute rollups (1-min, 1-hour) and retain them longer than raw data.
- **Series model:** a **series** = metric name + set of tags (labels). `engine_temp{device="77", fleet="acme"}`.
- **Query language/features:** time-bucket aggregations, interpolation, gap filling, last-value queries.
- **Systems:** TimescaleDB (Postgres extension: SQL + chunking), InfluxDB, Prometheus (metrics, pull-based, local TSDB), VictoriaMetrics, M3, OpenTSDB, and cloud offerings (Timestream, Bigtable-based). Also ClickHouse is commonly used for event/time-series analytics.

### 4.8.3 The cardinality trap `[MK]`
**Cardinality** = number of distinct series (unique tag combinations). Cost scales with it. Putting a high-cardinality value (user_id, request_id, container ID in an autoscaling cluster) into a tag creates millions of series → memory/index explosion. *Tags are for bounded dimensions; unbounded identifiers belong in values, logs, or traces.* (Returns in Chapter 21.)

### 4.8.4 Telematics design preview
Ingest → durable stream (Chapter 13) → writes to: (a) a "latest state per device" KV store for live maps; (b) a TSDB/columnar store for recent history; (c) object storage in Parquet for cold history and analytics. Retention tiers and rollups control cost. (Full design in Chapter 27.)

---

## 4.9 Search engines `[GK]`

### 4.9.1 Problem
"wireless noise cancelling headphones under 5000", with typos ("wirless"), synonyms, relevance ranking, facets (brand counts), and sub-100 ms response over 100 M products.
SQL `LIKE '%headphones%'` can't use B-trees, doesn't rank, ignores morphology, and scans everything.

### 4.9.2 The inverted index `[MK concept]`
Instead of mapping document → words, map **word → list of documents** (a **postings list**):
```text
Doc1: "wireless noise cancelling headphones"
Doc2: "wired headphones"
Doc3: "wireless speaker"

Inverted index:
 wireless    → [Doc1, Doc3]
 headphones  → [Doc1, Doc2]
 noise       → [Doc1]
 speaker     → [Doc3]
Query "wireless headphones" → intersect lists → Doc1
```
Pipeline: **analysis** (tokenize, lowercase, remove stop words, stem/lemmatize, synonyms, n-grams for prefix/typo tolerance) at index *and* query time → **postings lists** with positions/frequencies → **scoring** (TF-IDF → BM25: terms rare across the corpus and frequent in the doc score higher; plus field boosts, recency, popularity, ML ranking).

### 4.9.3 Architecture of Elasticsearch/OpenSearch/Solr (Lucene-based)
- An **index** is split into **shards** (each a Lucene index), with **replicas** for availability and read scaling.
- Queries are **scatter-gather**: the coordinator sends the query to one copy of every shard, each returns its top-K, the coordinator merges. (Latency = slowest shard: the tail-latency issue of Chapter 5.)
- **Near-real-time:** newly indexed documents become searchable after a **refresh** (default ~1 s); durability via a translog.
- **Segments** are immutable (LSM-like); background merges.

### 4.9.4 Strengths, weaknesses, and the key rule `[MK]`
**Strengths:** full-text relevance, fuzzy matching, faceting/aggregations, geo search, log search (ELK stack), autocomplete.
**Weaknesses:** **not a system of record** (weaker durability/consistency guarantees; reindexing needed on mapping changes); eventual consistency with the source; expensive in RAM/disk; operational complexity (shard sizing, mapping explosions).
> **Rule:** the search index is a **derived, rebuildable view** of data owned by another store. Feed it asynchronously (CDC/events), and be able to rebuild from the source of truth.

---

## 4.10 Object/blob storage (pointer)
Images, videos, backups, logs, Parquet files: store in **object storage** (S3/GCS/Azure Blob): flat namespaces of immutable-ish blobs, extremely durable (replication/erasure coding), cheap, HTTP-accessed, with high throughput but higher per-operation latency. Databases hold **metadata + object keys**. Deep dive in Chapter 11.

---

## 4.11 Distributed SQL ("NewSQL") and analytics `[GK]`

### 4.11.1 Distributed SQL
**Problem:** you want SQL, transactions, *and* horizontal scale + multi-region resilience.
**Systems:** Google **Spanner** `[Public]` (uses synchronized clocks, TrueTime, for externally consistent transactions), **CockroachDB**, **YugabyteDB**, TiDB, Aurora (shared-storage architecture). Typically data is split into **ranges/tablets**, each replicated with **Raft/Paxos** (Chapter 16); transactions across ranges use 2PC-like protocols with consensus-backed coordinators.
**Trade-offs:** higher write latency (consensus round trips, cross-region RTT), operational/cost complexity, subtle performance cliffs with hot ranges, distributed joins still cost. Great when you truly need relational semantics at scale/region spread; overkill otherwise.

### 4.11.2 OLTP vs OLAP `[MK]`
| | **OLTP** (online transaction processing) | **OLAP** (online analytical processing) |
|---|---|---|
| Workload | Many small reads/writes touching few rows | Few large queries scanning many rows |
| Example | "Place order," "get profile" | "Revenue by region by month for 3 years" |
| Latency | ms | seconds–minutes |
| Storage layout | **Row-oriented** (rows contiguous) | **Column-oriented** (each column contiguous) |
| Systems | Postgres, MySQL, DynamoDB | Snowflake, BigQuery, Redshift, ClickHouse, Druid; Parquet on data lakes |
| Why layout matters | Row layout: fetch/modify one whole record fast | Column layout: read only the needed columns; excellent compression (similar values together); vectorized execution |

**Do not run heavy analytics on your OLTP primary.** Use replicas for light reporting, but for real analytics feed a warehouse/lake via CDC or batch ETL/ELT. This also isolates unpredictable analytical load from customer-facing latency.
**Data lake vs warehouse:** lake = cheap object storage with open file formats (Parquet/ORC), schema-on-read, any engine on top; warehouse = managed, structured, SQL-optimized. "Lakehouse" combines table formats (Iceberg, Delta, Hudi) with lake storage.

---

## 4.12 Choosing a store: a decision framework `[MK]`

### 4.12.1 Questions to ask (in order)
1. **What are the access patterns?** Point lookup by key? Range by time? Ad-hoc filters/joins? Graph traversal? Full-text? Aggregations over everything?
2. **What are the consistency and transaction needs?** Multi-row invariants (money, inventory)? Or per-item is enough?
3. **What are the scale numbers?** Reads/s, writes/s, data size, growth (Chapter 5). Does it fit a primary + replicas?
4. **What are the latency and availability targets?**
5. **How does the data change?** Immutable/appended? Updated in place? Schema stable or evolving?
6. **How is it queried by whom?** Product APIs (known queries) vs analysts (unknown queries).
7. **What are the operational realities?** Team skills, managed service availability, backup/restore, cost, lock-in.

### 4.12.2 Quick mapping
| Need | Likely fit |
|---|---|
| Transactions, relationships, ad-hoc queries, moderate scale | **Relational (Postgres/MySQL)**: the default |
| Cache, session, counter, leaderboard, rate limit; sub-ms | **Redis/Memcached** |
| Huge-scale simple lookups by key; managed; predictable latency | **DynamoDB-style KV** |
| Evolving nested aggregates read whole | **Document store** |
| Massive write throughput; query by partition + time/range; multi-DC | **Wide-column (Cassandra/Scylla/Bigtable)** |
| Deep relationship traversal | **Graph DB** |
| Metrics/sensor data with time-range queries & retention | **Time-series DB** |
| Full-text relevance, faceted search, log search | **Search engine** (derived view) |
| Files/media/backups | **Object storage** |
| Analytics over huge data | **Warehouse / lake / columnar** |
| SQL + horizontal scale + multi-region transactions | **Distributed SQL** (if justified) |

### 4.12.3 Revisiting the Four Products (answers to Pause & Predict 4.1)
1. **ChatNow:** wide-column (Cassandra/Scylla/Bigtable) or DynamoDB, keyed by `(conversation_id, time bucket)`; hot-conversation caching in Redis; metadata (users, group membership) in SQL; media in object storage. *Why not just Postgres?* ~6–30 k writes/s average-to-peak with multi-TB/year growth means manual sharding on a B-tree engine; an LSM-based partitioned store handles this natively.
2. **ShopFront:** document store (or Postgres `jsonb` if scale is moderate) for the product aggregate; a **search engine** for discovery; SQL for orders/inventory (transactional); cache for hot product pages.
3. **FleetTrack:** durable stream → time-series/columnar store for history (time-partitioned, compressed, tiered retention), KV store for latest-state, object storage for cold; SQL for fleet/vehicle metadata.
4. **ConnectU:** keep relationships in the primary store; for deep traversals add a **graph DB** (derived) or precompute (friends-of-friends caches/offline jobs) depending on query latency and depth.
5. **Search:** Elasticsearch/OpenSearch fed from the product store.
6. **Sessions/leaderboards:** Redis.

### 4.12.4 Polyglot persistence and its price `[MK]`
Using several stores gives each workload its best tool, but you pay:
- **Operational burden** (each store: monitoring, backups, upgrades, expertise).
- **Consistency across stores:** there is no transaction spanning Postgres and Elasticsearch.
- **Complexity of keeping copies in sync.**

**The dual-write trap.** Application code writes to the DB, then to the search index/cache:
```text
db.save(order)  ✔
search.index(order)  ✘ crashes/times out → DB and index disagree forever
```
No atomicity across systems. Options:
1. **Outbox pattern:** write the business row and an "event" row in the *same DB transaction*; a relay publishes events to a queue/stream; consumers update the other stores (at-least-once → consumers must be idempotent).
2. **Change Data Capture (CDC):** tail the database's WAL/binlog (e.g., Debezium) and stream changes downstream; the DB log is the single source of ordering truth.
3. **Rebuild/reconcile jobs** to repair drift.
Pick one **system of record** per piece of data; every other store is a *derived view*.

### 4.12.5 "Boring technology" rule `[MK]`
Start with one relational database and add specialized stores only when a *measured requirement* demands it. Each store is a long-term commitment. Many large systems run primarily on Postgres/MySQL for a long time (with replicas, caching, and eventually sharding).

---

## 4.13 Data modeling for query-first stores: worked examples `[GK]`

### 4.13.1 DynamoDB single-table design (e-commerce excerpt)
Access patterns:
- A1: get customer profile; A2: list a customer's orders (newest first); A3: get one order with its items; A4: get orders by status for a warehouse view.

One table `AppTable`: generic keys `PK`, `SK`, plus GSIs.
| PK | SK | attributes |
|---|---|---|
| `CUST#42` | `PROFILE` | name, email |
| `CUST#42` | `ORDER#2026-03-01#o_901` | status, total |
| `ORDER#o_901` | `ITEM#1` | sku, qty, price |
| `ORDER#o_901` | `ITEM#2` | sku, qty, price |
- A1: `GetItem(PK=CUST#42, SK=PROFILE)`.
- A2: `Query(PK=CUST#42, SK begins_with ORDER#)` with `ScanIndexForward=false` (newest first; the date in the SK gives sorting).
- A3: `Query(PK=ORDER#o_901)` returns the whole order + items in one request.
- A4: a **GSI** with `GSI1PK = STATUS#SHIPPED`, `GSI1SK = order timestamp`. (Beware: a low-cardinality GSI key like `STATUS#SHIPPED` is a hot partition; shard it, e.g., `STATUS#SHIPPED#<0-9>`.)
Lesson: the "schema" is a set of **key conventions designed around queries**, not entities. Great performance, rigid evolution.

### 4.13.2 Wide-column time-bucketing for telemetry
```sql
CREATE TABLE readings (
  device_id bigint, day date, ts timestamp,
  speed float, lat double, lon double,
  PRIMARY KEY ((device_id, day), ts)
) WITH CLUSTERING ORDER BY (ts DESC);
```
`(device_id, day)` bounds partition size (one device-day ≈ 17,280 samples at 5 s intervals); a time-range query for one device touches 1–2 partitions. Compare with partitioning by `device_id` alone (unbounded) or by `day` alone (all devices in one hot partition per day, the classic **time-series hotspot**).

### 4.13.3 Embedding vs referencing (MongoDB) — blog example
- Post with ≤ 20 recent comments displayed: embed `recent_comments` (bounded, capped array), store all comments in `comments` (referencing `post_id`) for pagination. Update the embedded cache on write (or asynchronously). Reads of the post page = one document fetch.

---

## 4.14 Failure modes and operational realities

| Store | Typical failure/issue | Mitigation |
|---|---|---|
| Redis | Memory exhaustion/evictions; big-key blocking; failover loses async-replicated writes; hot key | maxmemory/policies, avoid O(N) commands, cluster, key splitting, treat as cache unless designed otherwise |
| Document | Unbounded documents; slow queries on unindexed fields; stale secondary reads; poor shard key → jumbo chunks | Bounded embedding, indexes, read preference awareness, good shard key |
| Wide-column | Hot/large partitions; tombstone scans; compaction backlog/stalls; read repair storms; inconsistent replicas; long repairs | Partition design, avoid delete-heavy patterns, capacity headroom, monitoring pending compactions, scheduled repairs |
| Graph | Supernodes; unbounded traversals | Depth limits, caching, degree caps |
| TSDB | Cardinality explosion; late-arriving data | Tag discipline, ingestion limits, out-of-order windows |
| Search | Mapping explosion; heavy aggregations; split-brain (old versions); stale index; reindex storms | Controlled mappings, shard sizing, rebuild pipeline, circuit breakers |
| Distributed SQL | Cross-region latency; hot ranges; leaseholder movement | Locality-aware schema, tuned partitioning |
| Multi-store | Drift between systems; dual-write bugs | Outbox/CDC, reconciliation, single source of truth |
| All LSM | Compaction I/O interfering with latency; disk full during compaction (needs ~50% headroom for size-tiered) | Throttle compaction, provision headroom |

---

## 4.15 Interview perspective
- State the **access patterns first**, then pick the store: "reads are 'latest 50 messages per conversation,' writes are 100 k/s: partition by conversation+bucket in a Cassandra-like store."
- For each store, volunteer **one limitation** ("DynamoDB hot partitions," "Elasticsearch isn't the source of truth").
- Avoid "NoSQL because it scales" without specifics. Interviewers want requirement-driven reasoning.
- Mention **derived stores** and **CDC/outbox** for sync.
- Know **LSM vs B-tree** at a conceptual level; senior interviews probe write amplification, compaction, Bloom filters.
- Always mention **partition key choice and hot partitions** when a partitioned store appears.

---

## 4.16 Labs

### Lab 1: Build a tiny LSM-tree in Java (WAL, memtable, SSTables, Bloom filters, compaction)
This program is about 150 lines but contains the real ideas. Run it and read the printed statistics.

```java
import java.io.*;
import java.nio.charset.StandardCharsets;
import java.nio.file.*;
import java.util.*;

public class MiniLsm {
    static final String TOMBSTONE = "<<tombstone>>";
    static final int SPARSE = 16;
    static long diskReads = 0, sstBytesWritten = 0, userBytes = 0;

    // ---------- Bloom filter ----------
    static class Bloom {
        final BitSet bits; final int m, k;
        Bloom(int expected, double fpp) {
            m = Math.max(64, (int) (-expected * Math.log(fpp) / (Math.log(2) * Math.log(2))));
            k = Math.max(1, (int) Math.round((double) m / expected * Math.log(2)));
            bits = new BitSet(m);
        }
        private int idx(String s, int i) {
            int h1 = s.hashCode();
            int h2 = Integer.rotateLeft(h1 * 0x9E3779B1, 15) | 1;
            return Math.floorMod(h1 + i * h2, m);
        }
        void add(String s) { for (int i = 0; i < k; i++) bits.set(idx(s, i)); }
        boolean mightContain(String s) { for (int i = 0; i < k; i++) if (!bits.get(idx(s, i))) return false; return true; }
    }

    // ---------- Immutable sorted file ----------
    static class SsTable {
        final Path file; final Bloom bloom;
        final List<String> sparseKeys = new ArrayList<>();
        final List<Long> sparseOffsets = new ArrayList<>();
        SsTable(Path file, SortedMap<String, String> data) throws IOException {
            this.file = file;
            this.bloom = new Bloom(Math.max(1, data.size()), 0.01);
            long offset = 0; int i = 0;
            try (OutputStream out = new BufferedOutputStream(Files.newOutputStream(file))) {
                for (Map.Entry<String, String> e : data.entrySet()) {
                    if (i++ % SPARSE == 0) { sparseKeys.add(e.getKey()); sparseOffsets.add(offset); }
                    byte[] line = (e.getKey() + "\t" + e.getValue() + "\n").getBytes(StandardCharsets.UTF_8);
                    out.write(line); offset += line.length;
                    bloom.add(e.getKey());
                }
            }
            sstBytesWritten += offset;
        }
        /** Returns the stored value (possibly TOMBSTONE) or null if this file doesn't contain the key. */
        String get(String key) throws IOException {
            if (!bloom.mightContain(key)) return null;               // skip the file without touching disk
            int idx = Collections.binarySearch(sparseKeys, key);
            if (idx < 0) idx = -idx - 2;                             // floor entry
            if (idx < 0) return null;                                // key smaller than first key
            diskReads++;
            try (RandomAccessFile raf = new RandomAccessFile(file.toFile(), "r")) {
                raf.seek(sparseOffsets.get(idx));
                String line; int n = 0;
                while ((line = raf.readLine()) != null && n++ < SPARSE) {
                    int t = line.indexOf('\t');
                    int c = line.substring(0, t).compareTo(key);
                    if (c == 0) return line.substring(t + 1);
                    if (c > 0) return null;
                }
            }
            return null;
        }
        TreeMap<String, String> readAll() throws IOException {
            TreeMap<String, String> m = new TreeMap<>();
            for (String line : Files.readAllLines(file)) { int t = line.indexOf('\t'); m.put(line.substring(0, t), line.substring(t + 1)); }
            return m;
        }
    }

    // ---------- The LSM store ----------
    final Path dir; final int memLimit;
    TreeMap<String, String> mem = new TreeMap<>();
    final List<SsTable> tables = new ArrayList<>();   // oldest first, newest last
    BufferedWriter wal; int seq = 0;

    MiniLsm(Path dir, int memLimit) throws IOException {
        this.dir = dir; this.memLimit = memLimit;
        Files.createDirectories(dir);
        Path walPath = dir.resolve("wal.log");
        if (Files.exists(walPath)) {                                  // crash recovery: replay the WAL
            for (String line : Files.readAllLines(walPath)) { String[] p = line.split("\t", 2); mem.put(p[0], p[1]); }
        }
        wal = Files.newBufferedWriter(walPath, StandardOpenOption.CREATE, StandardOpenOption.APPEND);
    }

    void put(String k, String v) throws IOException {
        userBytes += k.length() + v.length();
        wal.write(k + "\t" + v + "\n"); wal.flush();                  // ① sequential append (a real system fsyncs)
        mem.put(k, v);                                                // ② memtable
        if (mem.size() >= memLimit) flush();
    }
    void delete(String k) throws IOException { put(k, TOMBSTONE); }   // a delete is just a write

    String get(String k) throws IOException {
        String v = mem.get(k);
        if (v == null) for (int i = tables.size() - 1; i >= 0 && v == null; i--) v = tables.get(i).get(k);
        return (v == null || v.equals(TOMBSTONE)) ? null : v;
    }

    void flush() throws IOException {
        if (mem.isEmpty()) return;
        tables.add(new SsTable(dir.resolve("sst-" + (seq++) + ".tbl"), mem));
        mem = new TreeMap<>();
        wal.close();                                                  // memtable is now durable in an SSTable
        wal = Files.newBufferedWriter(dir.resolve("wal.log"), StandardOpenOption.CREATE, StandardOpenOption.TRUNCATE_EXISTING);
    }

    void compactAll() throws IOException {                            // "major compaction"
        TreeMap<String, String> merged = new TreeMap<>();
        for (SsTable t : tables) merged.putAll(t.readAll());          // oldest→newest: newer values win
        merged.values().removeIf(v -> v.equals(TOMBSTONE));           // safe to drop tombstones: no older data remains
        for (SsTable t : tables) Files.deleteIfExists(t.file);
        tables.clear();
        tables.add(new SsTable(dir.resolve("sst-" + (seq++) + ".tbl"), merged));
    }

    public static void main(String[] args) throws Exception {
        Path dir = Files.createTempDirectory("minilsm");
        MiniLsm db = new MiniLsm(dir, 1000);
        int N = 20_000;
        for (int i = 0; i < N; i++) db.put(String.format("key%07d", i), "value-" + i);
        for (int i = 0; i < N; i += 4) db.put(String.format("key%07d", i), "updated-" + i);   // overwrites
        for (int i = 0; i < N; i += 10) db.delete(String.format("key%07d", i));              // deletes
        db.flush();
        System.out.println("SSTables on disk: " + db.tables.size());

        diskReads = 0;
        for (int i = 0; i < 2000; i++) db.get(String.format("missing%07d", i));              // keys that don't exist
        System.out.println("2000 lookups of MISSING keys -> disk reads: " + diskReads + "  (Bloom filters skipped the rest)");

        diskReads = 0;
        for (int i = 1; i < 2000; i += 2) db.get(String.format("key%07d", i));               // existing keys
        System.out.println("1000 lookups of existing keys -> disk reads: " + diskReads);
        System.out.println("get(key0000004)=" + db.get("key0000004") + "  get(key0000010)=" + db.get("key0000010") + " (deleted)");

        long before = sstBytesWritten;
        db.compactAll();
        System.out.println("After compaction SSTables: " + db.tables.size());
        diskReads = 0;
        for (int i = 1; i < 2000; i += 2) db.get(String.format("key%07d", i));
        System.out.println("1000 lookups of existing keys after compaction -> disk reads: " + diskReads);
        System.out.printf("Write amplification so far: %.2fx (SSTable bytes written / user bytes)%n",
                          (double) sstBytesWritten / userBytes);
        System.out.println("Compaction itself wrote " + (sstBytesWritten - before) + " more bytes.");
    }
}
```
**What to observe**
1. About 27 SSTables are created from the 1,000-entry flushes (your exact count may differ slightly). Without Bloom filters, each of the 2,000 **missing-key** lookups would touch every table: roughly 2,000 × 27 = **54,000** disk reads. With them you should see only about **~500** (≈ 2,000 × 27 × 1% false-positive rate). That ~100× reduction is the Bloom filters' job; the remaining reads are false positives. (Sample run: 527.)
2. Lookups of existing keys may check several files (**read amplification**); after compaction, one file → fewer disk reads.
3. **Deleted keys** vanish only after compaction (tombstones shadow older values until then).
4. **Write amplification** > 1: the same data is written to WAL, SSTable, then rewritten by compaction.
5. Experiments: change `memLimit`; change Bloom `fpp` to 0.5 and see disk reads rise; compare "overwrite-heavy" vs "insert-only" workloads; implement size-tiered compaction (merge only the 4 oldest tables) and compare write/read amplification.
*(Limitations: keys/values can't contain tabs/newlines; `RandomAccessFile.readLine` assumes Latin-1; no fsync, no concurrency, no checksums. It's a teaching toy.)*

### Lab 2: Redis in 20 minutes
```bash
docker run --name r -p 6379:6379 -d redis:7
docker exec -it r redis-cli
```
```text
# Strings, TTL, atomic counters
SET session:9f3c "user:42" EX 1800
TTL session:9f3c
INCR page:views ; INCRBY page:views 10

# Idempotency / lock pattern: set-if-not-exists with expiry
SET lock:order:77 owner1 NX PX 5000      # returns OK once; nil for the second caller within 5 s

# Hash for an object, set for tags
HSET user:42 name Priya city Mumbai ; HGETALL user:42
SADD tags:p1 ultrabook 14-inch ; SMEMBERS tags:p1

# Sorted set leaderboard
ZADD board 1500 priya 2100 arjun 1800 meera
ZREVRANGE board 0 2 WITHSCORES           # top 3
ZRANK board priya

# Sliding-window rate limit using a sorted set (score = timestamp)
# (script this: ZADD key now now ; ZREMRANGEBYSCORE key 0 now-60 ; ZCARD key)

# Approximate distinct counting
PFADD uv:2026-03-01 u1 u2 u3 ; PFCOUNT uv:2026-03-01

# See eviction behavior
CONFIG SET maxmemory 5mb
CONFIG SET maxmemory-policy allkeys-lru
```
**Questions:** What happens to `lock:order:77` if the holder crashes? (TTL expires — and what if the work takes longer than the TTL? → fencing tokens, Chapter 16.) Why is `KEYS *` dangerous in production? How would you shard the leaderboard across nodes if it had 100 M members? (Chapter 10.)

### Lab 3: Query-first modeling on paper (no software needed)
For each system, write (a) the list of queries, (b) the table(s)/key design, (c) partition-size analysis, (d) hot-partition risks:
1. **Notifications inbox** (user sees latest 50, mark as read).
2. **Order history** with "orders by customer" and "orders by day for ops."
3. **Telemetry** for 1 M devices at 1 sample/10 s.
4. **Leaderboard per game per week.**
Compare your answers with §4.13 patterns, then check the answer key.

---

## 4.17 Common misconceptions

1. **"NoSQL means no schema."** There's always a schema, in your application code or key conventions; the question is where it's enforced.
2. **"NoSQL scales, SQL doesn't."** Both scale, differently and at different costs; know the limits of each.
3. **"MongoDB/Cassandra can't do transactions."** Capabilities vary and evolve; they're narrower and costlier than relational norms.
4. **"Cassandra is just faster."** Faster for *specific* patterns (writes, key-based reads); slower or impossible for ad-hoc queries.
5. **"LSM is always better than B-tree."** It trades write speed for read/space/compaction complexity.
6. **"Redis is just a cache."** It's a data-structure server, but treat durability carefully.
7. **"Elasticsearch can be my main database."** It's a search index; keep a source of truth elsewhere.
8. **"One database for everything" and "a database per feature" are both wrong.** Choose per workload and pay the polyglot tax knowingly.
9. **"Denormalization is a smell in NoSQL."** It's the norm; the risk is keeping copies in sync.
10. **"A graph DB is needed for any relationships."** SQL handles most relationship needs; use graphs for deep, variable-depth traversals.
11. **"Deleting data frees space immediately."** In LSM stores deletes are writes; space returns after compaction and grace periods.
12. **"We'll dual-write to both stores."** Without outbox/CDC, you will eventually diverge.

---

## 4.18 Chapter summary

- Non-relational stores trade generality for **scale, write throughput, flexibility, or specialized queries**. "NoSQL" is a set of very different families.
- **Storage engines:** B-trees (update-in-place; good reads) vs **LSM-trees** (WAL → memtable → immutable SSTables → compaction; Bloom filters; tombstones; write/read/space amplification trade-offs).
- **Key-value** (Redis, DynamoDB): get/put by key; partitions trivially; hot keys are the risk.
- **Document** (MongoDB): flexible aggregates; embed vs reference; unbounded growth trap.
- **Wide-column** (Cassandra/Bigtable): query-first modeling, partition + clustering keys, time bucketing, tombstones, tunable consistency.
- **Graph**: deep traversals; hard to shard; usually a derived index.
- **Time-series**: time-chunking, compression, rollups, **cardinality** discipline.
- **Search**: inverted index; a **derived** view, not a system of record.
- **Distributed SQL** gives SQL + scale at the price of latency/complexity; **OLAP** uses columnar layouts and should be separated from OLTP.
- Decide from **access patterns, consistency, scale, operations**; start boring; every extra store costs you; **never dual-write** — use **outbox/CDC** and a single source of truth.

### Mental models
| Concept | Mental model |
|---|---|
| LSM-tree | Taking notes in a notebook (append fast), then periodically tidying them into sorted binders (compaction) |
| Bloom filter | A bouncer with a partial guest list: "definitely not on it" or "maybe" |
| Tombstone | A "this item is cancelled" sticky note that stays until the binder is rewritten |
| Wide-column partition | A filing cabinet drawer per partition key, with folders sorted inside |
| Document embed vs reference | Photocopy attached to the order vs a pointer to the original |
| Inverted index | The index at the back of a book, for *every* word |
| Cardinality | Number of distinct labels on the shelf; too many labels = shelf collapses |
| Polyglot persistence | A toolbox of specialized tools — each one needs maintenance |

---

## 4.19 Exercises

### A. Conceptual
1. Why can LSM-trees ingest writes faster than B-trees? What do they pay in return?
2. Walk through `put` then `get` in an LSM store; where is the data at each moment? What does the WAL protect?
3. What is a Bloom filter? Why do LSM stores use one per SSTable? What are its error characteristics?
4. Define write, read, and space amplification. Which compaction strategy favors which?
5. What's a tombstone and why can deletes make reads slower in Cassandra?
6. Compare Redis vs a durable KV like DynamoDB: durability, capacity, latency, use cases.
7. Why is Redis's single-threaded command execution both a feature and a hazard?
8. When should you embed vs reference in a document model? What's the unbounded array anti-pattern?
9. In a wide-column store, what do the partition key and clustering key each do? Why must queries specify the partition key?
10. Why is "model per entity" the wrong approach for Cassandra? What replaces it?
11. What is a hot partition and three ways to mitigate it?
12. What is cardinality in time-series databases and why can it destroy a TSDB?
13. What's an inverted index? Why is `LIKE '%term%'` poor for search?
14. Why is a search index a "derived view"? What process keeps it in sync?
15. OLTP vs OLAP: workload, layout, example systems. Why not run analytics on the OLTP primary?
16. What does a distributed SQL database add and cost compared with a single-node Postgres?
17. Explain the dual-write problem and two solutions.
18. State the "boring technology" rule and when to break it.

### B. Calculations
19. ChatNow: 500 M messages/day at 120 B each. (a) Average writes/s; (b) peak at 5×; (c) raw storage/year; (d) with replication factor 3.
20. FleetTrack: 200,000 samples/s, 40 B each raw; Gorilla-style compression achieves ~1.5 B/sample. Compare raw vs compressed per day. How many TB/year compressed (before replication)?
21. Bloom filter sizing: 10 M keys per SSTable, 1% false positive rate. Using m ≈ −n·ln(p)/(ln 2)², estimate bits per key and total MB.
22. A Cassandra partition per `(device_id, day)` with 5-second samples, 100 B/sample. Partition size? Is it reasonable? What if sampling is 100 ms?
23. DynamoDB-style hot partition: a table has 10 partitions each allowing ~1,000 writes/s. A single key receives 3,000 writes/s. What happens? Name two fixes.

### C. Design (query-first)
24. Design the key schema (DynamoDB or Cassandra) for: "Notifications: show a user's latest 50, mark one read, count unread."
25. Choose stores for a **food-delivery app**: restaurants/menus, orders, courier live locations, search, reviews, analytics. Justify each and name the system of record.
26. Model a **chat group with 100k members**: how do you store messages, membership, per-user read state?
27. **Leaderboard** for 50 M players with top-100 and "my rank" queries. Compare Redis ZSET vs SQL vs a sharded approach.
28. A document store holds blog posts with comments (up to 1M comments for viral posts). Design the schema.

### D. Debugging
29. A Cassandra read that used to take 5 ms now times out. The table is used as a job queue (insert, process, delete). Diagnose.
30. Elasticsearch shows products that were deleted from the DB an hour ago. Explain at least three causes and fixes.
31. A Redis instance's latency spikes every few minutes; CPU is fine. Hypothesize (AOF rewrite/fork, big keys, `KEYS`, swap, eviction).
32. A TSDB's memory grows steadily after a deploy that added `request_id` as a metric tag. Explain and fix.
33. A DynamoDB table is throttled although consumed capacity is 20% of provisioned. Explain.

### E. Interview-style
34. "SQL or NoSQL for a chat application?" Give a structured answer.
35. "How do you keep Elasticsearch in sync with MySQL?"
36. "Explain LSM-trees vs B-trees to a junior engineer in 3 minutes."

### F. Lab follow-ups
37. In Lab 1, change `fpp` to 0.5 and compare disk reads. Explain.
38. Implement size-tiered compaction in Lab 1 (merge the oldest 4 tables when ≥8 exist). Measure write amplification vs the major compaction approach.
39. In Lab 2, implement a sliding-window rate limiter with a ZSET using a Lua script; explain why Lua (atomicity).

### G. Teach-back
40. Explain "why deletes are writes in LSM stores" with an analogy, then technically.

---

## 4.20 Full answer key

**1.** LSM turns writes into sequential appends (WAL) plus in-memory inserts, flushing sorted batches; no random in-place updates or per-write index tree rebalancing. Cost: reads may check several files (read amplification), background compaction consumes I/O and rewrites data (write amplification), compaction can cause latency spikes/stalls.

**2.** `put`: append to WAL (durable) → insert into memtable (RAM). When full, flush to an immutable SSTable (data now on disk; WAL can be truncated). `get`: memtable → SSTables newest→oldest (Bloom filter and sparse index guide). WAL protects the memtable contents against a crash before flush.

**3.** A compact probabilistic set: k hash functions set/check bits. "Not present" is certain; "present" may be a false positive. Used per SSTable to avoid reading files that can't contain the key, which is crucial for missing-key lookups and keys in older files. No false negatives.

**4.** Write amp = physical bytes written ÷ logical bytes; read amp = disk reads per lookup; space amp = disk used ÷ live data. Leveled: low read/space amp, high write amp. Size-tiered: lower write amp, higher read/space amp.

**5.** A tombstone is a delete marker written as a new record. Reads must scan past tombstones (and across replicas) until compaction removes them after the grace period; many tombstones in a partition slow reads and can cause timeouts.

**6.** Redis: in-memory, sub-ms, capacity bound by RAM, optional/weak durability (async persistence/replication), used for caches/sessions/counters/leaderboards. DynamoDB: disk-backed, replicated, effectively unlimited capacity, single-digit-ms, durable, with throughput limits/partitions; used as a primary store for key-based access at scale.

**7.** Feature: commands are atomic, no locking complexity. Hazard: one slow command blocks all clients (e.g., `KEYS *`, big sorts, large-set operations), and one core limits per-node throughput.

**8.** Embed when data is read together, bounded, and changed together; reference when large, unbounded, shared, or independently updated. Unbounded arrays inside documents grow without limit, increasing rewrite cost and hitting size caps.

**9.** Partition key → hash → which nodes store the data (distribution and co-location). Clustering key → sort order inside the partition (efficient range scans). Queries must specify the partition key so the coordinator knows which node(s) to ask; without it, it would scan the entire cluster.

**10.** Entities are normalized and joined; in wide-column stores there are no joins and cross-partition queries are expensive. Replace with query-first modeling: one table per query pattern, duplicating data as needed.

**11.** A partition receiving disproportionate traffic/data, overloading its replicas. Mitigate: finer-grained partition keys (add bucket/salt), spread by hashing sub-keys, caching hot items, splitting hot entities, throttling/queueing writers.

**12.** Cardinality = distinct series (unique label combinations). Cost (memory, index, storage) scales with it; unbounded tags (user_id, request_id) create millions of series → OOM, slow queries.

**13.** Maps terms to postings lists of documents, enabling fast term lookups and set operations with relevance scoring. `LIKE '%term%'` can't use B-tree ordering, forces a scan, and provides no ranking/stemming/typo tolerance.

**14.** It's built from another store's data and can be rebuilt; it has eventual consistency and weaker guarantees. Keep in sync via outbox/CDC → stream → indexer (idempotent upserts/deletes), with periodic reconciliation/rebuild.

**15.** OLTP: many small transactions, row-oriented, ms latency (Postgres, DynamoDB). OLAP: large scans/aggregations, columnar layout, compression/vectorization (BigQuery, Snowflake, ClickHouse). Analytics on the primary would consume I/O/CPU/locks and harm user-facing latency; use a replica/warehouse via CDC/ETL.

**16.** Adds horizontal scale, SQL/transactions across nodes, multi-region resilience (via consensus-replicated ranges). Costs: higher write/commit latency (consensus + cross-region), cost, operational complexity, distributed joins, hotspots.

**17.** Dual-write: writing to two systems separately; a crash between writes leaves them inconsistent. Solutions: transactional outbox + relay; CDC from the DB log; (plus reconciliation).

**18.** Prefer proven, well-understood tech (often one relational DB) until measured requirements demand specialization; break the rule when a requirement can't be met (e.g., write throughput, full-text relevance, graph depth) or the operational cost is acceptable.

**19.** (a) 5×10⁸ / 10⁵ ≈ 5,000 writes/s (more precisely 5,787). (b) ≈ 25,000–29,000/s. (c) 5×10⁸ × 120 B = 60 GB/day → ×365 ≈ 21.9 TB/year. (d) ×3 ≈ 66 TB/year.

**20.** Raw: 200,000 × 40 B = 8 MB/s → 691 GB/day. Compressed: 200,000 × 1.5 B = 0.3 MB/s → ~26 GB/day → ≈ 9.5 TB/year (before replication). ~27× smaller.

**21.** m ≈ 10⁷ × ln(0.01)... compute: −ln(0.01) = 4.605; (ln 2)² = 0.4805; bits/key ≈ 4.605/0.4805 ≈ **9.6 bits/key**; total ≈ 9.6×10⁷ bits ≈ 96 Mbit ≈ **12 MB**.

**22.** 5 s samples → 17,280/day × 100 B ≈ **1.7 MB**: perfectly reasonable. At 100 ms: 864,000/day × 100 B = **86 MB**, near the upper guideline; consider hourly buckets `(device_id, hour)` (3.6 MB).

**23.** Requests to that partition beyond its ~1,000 writes/s are throttled (even though overall table capacity is unused). Fixes: write sharding/salting (append a random suffix 0–9 to the key and aggregate on read), reduce per-key write rate by batching/aggregating in a buffer before writing, redesign the key to spread load, or use adaptive/on-demand capacity.

**24.** DynamoDB: PK = `USER#<id>`, SK = `NOTIF#<descending-time-id>` (e.g., reverse timestamp or time-ordered ID with `ScanIndexForward=false`), attributes: type, text, `read` flag. Latest 50: Query PK with Limit 50. Mark read: UpdateItem on that key. Unread count: maintain a counter item `PK=USER#id, SK=UNREAD_COUNT` updated with atomic increments (transactionally or eventually) — counting by query is expensive. Cassandra equivalent: `PRIMARY KEY ((user_id), notif_id) WITH CLUSTERING ORDER BY (notif_id DESC)`, with a counter table (counters are tricky; or Redis).

**25.** Restaurants/menus: document store or SQL (read-heavy, moderate size) with cache; Orders: SQL (transactions, state machine) as system of record; Courier live locations: Redis geo / in-memory + stream, with history in TSDB; Search: Elasticsearch fed from the menu/restaurant store; Reviews: SQL or document store (counts denormalized); Analytics: warehouse fed via CDC. System of record: SQL for orders/restaurants/users; others are derived.

**26.** Messages: wide-column keyed by `(group_id, bucket)` clustered by time. Membership: SQL or KV keyed by `group_id` (paged) and reverse index `user_id → groups`. Read state: per-user `last_read_message_id` per group (small row per member: 100k rows per group; update lazily/batched, not per message). Avoid per-message-per-user receipts for large groups; fan-out on read, not on write.

**27.** Redis ZSET: `ZADD`, `ZREVRANGE 0 99`, `ZREVRANK` — O(log N); 50 M members ≈ a few GB, fits a big node. SQL: `ORDER BY score DESC LIMIT 100` with an index is OK; rank = `COUNT(*) WHERE score > x` expensive at 50 M (needs order-statistic trees). Sharded: partition by score range or by hash with per-shard top-K merged; "my rank" = sum of counts of higher-score shards + local rank; approximate ranks acceptable at scale.

**28.** `posts` collection (post fields, `comment_count`, `top_comments` capped array of ~10); `comments` collection with `post_id`, `created_at`, indexed `(post_id, created_at)` and cursor pagination. Never embed all comments; update denormalized fields asynchronously.

**29.** Using Cassandra as a queue: deletes create tombstones in the same partition; reads scan over many tombstones until compaction/grace-period. Fix: use a real queue (Kafka/SQS/RabbitMQ), or time-bucket partitions and drop whole partitions (TTL by window), avoid delete-then-read patterns.

**30.** Index lag (refresh interval / async pipeline backlog); deletes not propagated (missed events, no tombstone handling in the indexer); dual-write failure; reindex jobs resurrecting old docs; replicas/caches. Fix: CDC/outbox with delete events, idempotent versioned writes (external versioning to ignore stale updates), monitor lag, periodic reconciliation, soft-delete filters in queries.

**31.** Suspects: AOF rewrite or RDB fork copying memory (copy-on-write latency spikes), big keys / expensive commands (`KEYS`, `SMEMBERS` on huge sets), swapping, eviction storms, expiring many keys at once, slow disk fsync with `appendfsync always`, noisy neighbors. Use `SLOWLOG`, `LATENCY DOCTOR`, `INFO`.

**32.** Each unique `request_id` creates a new series → cardinality explosion → memory growth. Remove it from tags (keep in logs/traces), use bounded dimensions, enforce series limits, relabel/drop at ingest.

**33.** Hot partition(s): the throttled keys' partition limit is hit even though the table-wide consumption is low. Fix: spread keys (salting), cache hot reads, increase partition granularity, use on-demand/adaptive capacity, redesign access pattern.

**34 (sketch).** Start with the access patterns (latest N per conversation, writes dominate, append-mostly, little ad-hoc querying) → at large scale a partitioned wide-column/KV store (partition by conversation+time bucket) for messages; SQL for users/groups/metadata; Redis for presence/hot data; object storage for media. For small/medium scale Postgres suffices (note the numbers). Mention trade-offs: tunable consistency, no joins, denormalized inbox tables, ordering by time-based IDs.

**35 (sketch).** MySQL is the system of record; use CDC (Debezium reading the binlog) or an outbox table → Kafka → an indexing consumer performing idempotent upserts/deletes with version numbers; handle backfills/reindexing via a rebuild pipeline; monitor lag; periodic reconciliation by comparing counts/checksums; avoid dual-writes from the app.

**36 (sketch).** LSM: write fast (append, memtable), read from several places (Bloom filters), merge in background; B-tree: update in place, read fast, write slower; trade-offs: write amp, read amp, space amp, compaction stalls. Use analogies (notebook vs filing cabinet).

**37.** With fpp=0.5, the filter lets half of non-members through: disk reads for missing keys rise sharply (roughly N_sstables × 0.5 per lookup). Shows the filter's value is proportional to its accuracy; memory/accuracy trade-off.

**38 (guidance).** Merge the oldest 4 tables whenever ≥8 exist; track `sstBytesWritten` vs `userBytes`. Expect lower write amplification than repeated major compactions but more tables per lookup (higher read amplification).

**39 (sketch).** Lua (executed atomically in Redis): `redis.call('ZREMRANGEBYSCORE', KEYS[1], 0, now-window); local c = redis.call('ZCARD', KEYS[1]); if c < limit then redis.call('ZADD', KEYS[1], now, id); redis.call('PEXPIRE', KEYS[1], window); return 1 else return 0 end`. Atomicity avoids races between check and add from concurrent clients.

**40.** Analogy: in a notebook you can't erase old pages cheaply, so you write "cancelled: item X" on a new page; the real removal happens when you re-copy the notebook. Technically, immutable SSTables can't be modified, so a delete is a tombstone record shadowing older values until compaction merges and drops them.

---

## 4.21 Flashcards

| Front | Back |
|---|---|
| LSM write path | WAL append → memtable → flush to immutable SSTable → background compaction |
| LSM read path | Memtable → SSTables newest→oldest, guarded by Bloom filters/sparse index |
| Bloom filter guarantee | No false negatives; tunable false-positive rate |
| Tombstone | Delete marker (a write) removed during compaction after grace period |
| Write/read/space amplification | Physical writes per logical write / disk reads per read / disk used vs data |
| Leveled vs size-tiered | Lower read/space amp, higher write amp vs lower write amp, higher read/space amp |
| When LSM | Write-heavy, append-mostly, huge scale |
| When B-tree | Mixed/transactional, read-heavy, rich queries |
| Redis single thread | Atomic commands; slow commands block everyone |
| Redis persistence | RDB snapshots, AOF log; async replication → possible loss |
| Embed vs reference | Embed bounded, read-together data; reference unbounded/shared |
| Wide-column modeling | Query-first; partition key distributes, clustering key sorts |
| Time-bucketing | Add time bucket to partition key to bound partition size |
| Hot partition | Skewed key overloading one node; spread/salt |
| TSDB cardinality | # of distinct label combos; unbounded tags destroy it |
| Inverted index | Term → postings list of docs |
| Search index role | Derived, rebuildable view; not the source of truth |
| OLTP vs OLAP | Small transactions, row store vs big scans, column store |
| Dual-write fix | Outbox pattern or CDC; single source of truth |
| Polyglot cost | Ops burden + cross-store consistency |

## 4.22 Glossary additions

| Term | Simple Meaning | Technical Meaning | Why It Exists | Example |
|---|---|---|---|---|
| NoSQL | "Not a classic relational DB" | Diverse non-relational stores | Scale, flexibility, specialization | Cassandra, Redis |
| Storage engine | Disk layout manager | Component organizing data on disk/memory | Determines perf profile | InnoDB, RocksDB |
| LSM-tree | Append-and-merge storage | WAL + memtable + SSTables + compaction | Fast writes | Cassandra |
| Memtable | In-memory recent writes | Sorted in-RAM write buffer | Batch sequential flush | Skip list |
| SSTable | Sorted immutable file | Sorted key-value file with index/filter | Efficient merges & reads | `sst-17.tbl` |
| Compaction | Background merge | Merging SSTables, dropping obsolete data | Bound read cost/space | Leveled |
| Bloom filter | "Maybe/definitely not" | Probabilistic set membership | Skip useless reads | per SSTable |
| Tombstone | Delete marker | Record shadowing older values | Deletes in immutable files | Cassandra delete |
| Write amplification | Extra writes | Physical ÷ logical bytes written | Wear/throughput | 10–30× leveled |
| Key-value store | Dictionary service | `key → value` access | Simplicity, speed | Redis |
| TTL | Expiry timer | Auto-deletion after duration | Sessions, caches | `EX 1800` |
| Document store | Store JSON aggregates | Self-describing nested records | Flexible schemas | MongoDB |
| Embed / Reference | Nest vs point | Document modeling choice | Read/write trade-off | Order lines |
| Wide-column store | Partitioned sorted rows | Partition + clustering keys on LSM | Huge write scale | Cassandra, Bigtable |
| Partition key | Placement key | Determines node via hash | Distribution | `(device_id, day)` |
| Clustering key | In-partition order | Sort key within a partition | Range scans | `message_id DESC` |
| Graph database | Nodes & relationships | Property graph with traversals | Deep relationship queries | Neo4j |
| Time-series DB | Timestamped data store | Time-chunked, compressed, rollups | Metrics/sensors | TimescaleDB |
| Cardinality | Distinct label combos | Count of unique series/values | Memory/index cost | tags |
| Inverted index | Word → documents | Postings-list index | Full-text search | Lucene |
| BM25 | Relevance score | Probabilistic TF-IDF variant | Ranking | Elasticsearch |
| OLTP / OLAP | Transactions / analytics | Row-oriented small ops vs column-oriented scans | Different workloads | Postgres / BigQuery |
| Columnar storage | Store by column | Contiguous column values | Fast scans/compression | Parquet |
| Distributed SQL | SQL at scale | Consensus-replicated range-partitioned SQL | Scale + transactions | Spanner |
| Polyglot persistence | Many stores | Multiple stores by workload | Best tool per job | SQL+Redis+ES |
| Outbox pattern | Write event with data | Same-transaction event row relayed later | Avoid dual writes | `outbox` table |
| CDC | Stream DB changes | Reading the DB log to emit changes | Sync derived stores | Debezium |

---

**Next: Chapter 5: Performance and Capacity Estimation.** Latency vs throughput, queueing and why systems fall off a cliff near full utilization, tail latency, bottleneck analysis, and the back-of-envelope toolkit, with many worked estimations and a queue-simulation lab.

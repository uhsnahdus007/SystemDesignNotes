# Chapter 10: Partitioning and Sharding

## Chapter card

| | |
|---|---|
| **Objective** | Learn how to split data across machines so that *writes* and *data volume* scale: partitioning schemes (range, hash, directory), consistent hashing and virtual nodes, choosing a shard key, hot spots and skew, secondary indexes, cross-shard queries and transactions, ID generation, routing, rebalancing and online resharding, and when *not* to shard |
| **Why this chapter exists** | Replication (Ch 9) cannot scale writes or dataset size. Partitioning is the only general answer, and it is where most irreversible design decisions (the shard key) are made. It is also a favourite interview topic |
| **Prerequisites** | Chapters 3, 4, 5, 6, 8, 9 |
| **Study time** | 11–14 hours including labs |
| **Tier** | `[MK]` throughout; rebalancing internals and cross-shard transactions `[GK]` |
| **Mastery criteria** | (1) Explain what partitioning solves vs replication. (2) Compare range, hash, directory, and composite partitioning with their query trade-offs. (3) Explain why `hash mod N` is a trap and how consistent hashing, virtual nodes, rendezvous, and fixed-partition schemes avoid it. (4) Pick and defend a shard key for a given workload. (5) Diagnose and fix hot spots. (6) Explain local vs global secondary indexes and the cost of scatter-gather. (7) Design ID generation and routing for a sharded system. (8) Plan an online resharding with no downtime. (9) Say when sharding is premature |

---

## 10.1 The Story: The Table That Outgrew Its Machine

**ChatNow** stores every message in a single `messages` table on a PostgreSQL primary (with replicas, Ch 9). After two years:

- The table holds **8 TB**, growing 25 GB/day. Index maintenance and VACUUM take hours; backups take a day; restores would take longer.
- Peak writes are **60,000 inserts/s**. The primary's disk and CPU are at 90% *from writes alone*. Read replicas help reads but each replica must also apply all 60,000 writes/s, so they are nearly as busy.
- You already run the largest machine your cloud offers.

The team decides to **shard by `user_id`**: eight databases, `shard = user_id mod 8`. It works, for a while. Then:

1. Traffic grows; they need a 9th shard. Changing the formula to `mod 9` means **8 out of 9 rows change shard**. The migration is a multi-week project.
2. A celebrity's channel receives 40× the average traffic; its shard runs hot while the others idle.
3. The analytics team wants "all messages containing the word *refund* this week". Every query must hit all eight shards.
4. A feature needs to atomically move a message between two users on different shards. There is no cross-database transaction.
5. Message IDs from eight independent sequences collide.

### Pause & Predict 10.1
> For each of the five problems, name the general concept and one remedy (e.g., problem 1 is a *rebalancing* problem). Then explore which of these a *better shard key* could have avoided, and which are inherent costs of sharding.

---

## 10.2 What partitioning is, and what it solves

### 10.2.1 Definitions `[MK]`
- **Partitioning (sharding):** splitting a large dataset into smaller pieces (**partitions** or **shards**), each stored on (and served by) a different node. Each record belongs to exactly one partition.
- **Replication** (Ch 9) keeps *copies* of the same data; **partitioning** keeps *different data* on different nodes. They combine: each partition is itself replicated (e.g., 8 shards × 3 replicas = 24 nodes).
- Terminology varies by system: *shard* (MongoDB, MySQL/Vitess), *partition* (Kafka, Cassandra, DynamoDB), *region/tablet* (HBase, Bigtable, TiKV), *range* (CockroachDB), *vnode*, *bucket*, *slot* (Redis Cluster).

### 10.2.2 What it buys
| Benefit | How |
|---|---|
| **Write scalability** | Writes are spread over many primaries (one per shard) |
| **Data-size scalability** | Each node stores 1/N of the data |
| **Read scalability and parallelism** | Reads for different keys hit different nodes; large scans can be parallelized |
| **Fault isolation** | An outage affects only 1/N of the data/users (blast radius, Ch 6 cells) |
| **Locality** | Place a user's data near them; satisfy data-residency rules |
| **Operational manageability** | Smaller backups, faster rebuilds, smaller indexes |

### 10.2.3 What it costs
Sharding is the point where simple things become hard: cross-shard queries, joins, transactions, unique constraints, schema changes across N databases, ID generation, rebalancing, routing, backups, monitoring N systems, and a long-lived coupling between your data model and your shard key. **Do it when the numbers force you; design so you *can*.**

### 10.2.4 Three different things people call "partitioning" `[MK]`
| Kind | Splits | Example | Scales writes? |
|---|---|---|---|
| **Vertical partitioning** | Columns/tables: move rarely-used or large columns to another table/store | Move `profile_blob` out of `users` | Helps row width/IO, not row count |
| **Functional partitioning** | By feature/domain onto separate databases/services | Users DB, Orders DB, Messages DB | Yes, per domain (the first, easiest step) |
| **Horizontal partitioning (sharding)** | Rows of one logical table across nodes by key | `messages` by `user_id` | **Yes: the general solution** |
Also: **table partitioning within one database** (Postgres declarative partitions, MySQL partitions) splits a table into child tables on the *same* server: it helps maintenance (drop an old month instantly), pruning, and index size, but does not add machines.

---

## 10.3 Partitioning schemes `[MK]`

### 10.3.1 Range partitioning
Assign contiguous **key ranges** to partitions (A–F, G–M, …; or `user_id` 0–1M, 1M–2M, …; or dates).
```text
 shard 0: keys [0, 250)    shard 1: [250, 500)    shard 2: [500, 750)    shard 3: [750, 1000)
```
✔ **Efficient range scans** (sorted order within and across partitions: "all events 10:00–10:30", "usernames starting with 'jo'"); easy to reason about; easy to split a hot range.
✘ **Hot spots with monotonic keys**: timestamp or auto-increment keys send all new writes to the *last* partition.
**Measured (Lab 1, part 4):** writes with a time-ordered key land on a single shard:
```text
shard:           0  1  2  3  4  5  6  7  8  9
range  writes %: 0.0 0.0 0.0 0.0 0.0 100.0 0.0 0.0 0.0 0.0
hash   writes %: 10.0 10.0 10.0 10.0 10.1 10.0 10.0 10.0 9.9 10.0
```
Used by: HBase/Bigtable (tablets), CockroachDB/Spanner (ranges), MongoDB (range-sharded collections), TiKV, many time-series stores.

### 10.3.2 Hash partitioning
Compute `hash(key)` and assign by hash value (or hash range). Destroys key ordering but spreads load uniformly.
✔ **Even distribution** for any key distribution (except for individual hot keys); no monotonic-key hot spot.
✘ **Range queries scatter** to all partitions ("the last 30 minutes" must ask every shard); ordered iteration impossible without extra structure.
Used by: Cassandra/Dynamo (partition key hashed), Redis Cluster (CRC16 slots), MongoDB hashed sharding, Kafka's default partitioner (hash of key), DynamoDB.

### 10.3.3 Compound keys: the best of both `[MK]`
Hash the **first part** to choose the partition, keep the **rest sorted within** the partition:
- Cassandra: `PRIMARY KEY ((user_id), sent_at, message_id)`: partition key `user_id` (hashed), clustering columns sorted. "Latest 50 messages of user 42" is a **single-partition sorted range scan**.
- DynamoDB: partition key + sort key.
This is the standard pattern for social/chat/time-series workloads: **spread by entity, order by time inside the entity.**

### 10.3.4 Directory (lookup) partitioning
An explicit mapping table (key or key-range → shard), stored in a metadata service.
✔ Maximum flexibility: move a single tenant to a dedicated shard; rebalance arbitrarily; mix sizes. ✘ The directory is a critical, highly-available dependency; it must be cached by routers (and kept correct when entries move). Used for multi-tenant SaaS ("tenant → shard"), Vitess topology, Facebook's TAO-style shard maps.

### 10.3.5 Geographic / list partitioning
Partition by region, country, or tenant tier (all EU users on EU shards). Aligns with latency and **data residency** (GDPR), but sizes can be very uneven (US vs Liechtenstein).

### 10.3.6 Choosing a scheme
| Workload | Prefer |
|---|---|
| Point lookups, uniform; no range queries | **Hash** |
| Range scans on the key; sorted access | **Range** (watch hot spots) |
| Entity-centric with ordered sub-items (feeds, chats, events per user) | **Compound: hash(entity) + sort(time)** |
| Multi-tenant with huge size variance, need to relocate tenants | **Directory** |
| Residency/latency by geography | **Geo/list** (+ hash within region) |

---

## 10.4 The `hash mod N` trap, and the techniques that fix it `[MK]`

### 10.4.1 The problem
`shard = hash(key) mod N` is simple and balanced, but when N changes (add/remove a node), almost *every* key maps to a different node: the fraction that **stays** is about 1/(N+1), so **N/(N+1) of the data must move**.

**Measured (Lab 1, part 1):** add one node to a 10-node cluster, 1,000,000 keys (ideal = 1/11 = 9.09%):
```text
hash(key) mod N            :  90.9% of keys move
consistent hashing,   1 vnode(s) per node:   7.2% move
consistent hashing, 100 vnode(s) per node:   7.6% move
rendezvous (HRW) hashing   :   9.1% move
jump consistent hash       :   9.1% move
```
Moving 91% of a cluster's data is an outage-scale event (network saturation, cache-miss storm if these are cache nodes, hours of migration). The techniques below move only about the ideal 1/(N+1).

### 10.4.2 Consistent hashing with virtual nodes `[MK]`
**Idea (Karger et al., 1997):** hash both nodes and keys onto the same circular space (a **ring**). A key belongs to the first node clockwise from its position. When a node is added or removed, only the keys in the arc between it and its predecessor move.
```text
              key k1 → owned by B (next node clockwise)
        ┌───────────────────────────────────────────┐
       A ●─────────k1──────● B ───────────────● C ───┐
        └───────────────── ring wraps ────────────────┘
 Add node D between A and B: only keys in (A, D] move from B to D.
```
**Problem with one position per node:** arcs are random sizes: some nodes own huge arcs, others tiny (**imbalance**), and when a node leaves, *all* its load dumps onto one neighbour.
**Fix: virtual nodes (vnodes).** Each physical node takes *many* positions (e.g., 100–256). Load averages out, and a departing node's load spreads across many nodes.

**Measured (Lab 1, part 2):** 10 nodes, 1,000,000 keys:
```text
vnodes=   1  busiest node = 2.39x average, emptiest = 0.04x average
vnodes=  10  busiest node = 1.59x average, emptiest = 0.56x average
vnodes= 100  busiest node = 1.11x average, emptiest = 0.90x average
vnodes=1000  busiest node = 1.05x average, emptiest = 0.96x average
```
Rule of thumb: relative imbalance falls like 1/√(vnodes) (100 vnodes ≈ ±10%, 1,000 ≈ ±3%). More vnodes cost metadata and make repair/streaming more fragmented; heterogeneous machines get proportionally more vnodes (weights). Cassandra historically used 256 vnodes per node (newer versions default lower with smarter allocation) `[Public]`.

### 10.4.3 Rendezvous (highest-random-weight) hashing `[GK]`
For each key, compute `score(key, node)` for every node and pick the highest. Adding a node steals exactly the keys for which it now scores highest (≈1/(N+1)); removing a node moves only that node's keys. ✔ No ring, no vnodes, perfect balance, trivially supports weights. ✘ O(N) work per lookup (fine for tens of nodes; use hierarchy for thousands).

### 10.4.4 Jump consistent hash `[GK]`
A 5-line function (Lamping & Veach, Google) mapping `key → bucket in [0, N)` with minimal movement and perfect balance, no memory. ✘ Buckets must be numbered 0..N−1 and can only be added/removed at the end (no arbitrary node removal) — great for shard *numbers* in a stateless router, awkward for failing arbitrary nodes.

### 10.4.5 Fixed number of partitions (the pragmatic default) `[MK]`
Create **many more partitions than nodes** (e.g., 1,024 partitions for 8 nodes, or Redis's 16,384 slots). The key→partition mapping **never changes** (`hash(key) mod 1024`); only the *partition→node* assignment changes. Adding a node = move whole partitions to it (each is a self-contained unit that can be copied). Used by Kafka, Elasticsearch (primary shard count fixed at index creation), Couchbase, Riak, Redis Cluster, and effectively by Vitess/DynamoDB.
✔ Simple, predictable, easy to throttle (move one partition at a time). ✘ The partition count is a **commitment**: too few limits future scale (you can't have more nodes than partitions); too many adds per-partition overhead. Pick generously (e.g., 10–50× the expected node count).

### 10.4.6 Dynamic splitting
Range-partitioned systems start with few partitions and **split** a partition when it exceeds a size/load threshold (HBase regions at ~10 GB, MongoDB chunks at 128 MB (per version), CockroachDB ranges at 512 MB) and **merge** small ones. Splits are cheap (metadata) and follow the data. Pre-splitting is needed to avoid initial hot spots on an empty table.

---

## 10.5 Choosing the shard key: the most consequential decision `[MK]`

A bad shard key cannot be fixed by tuning; changing it means rewriting the data. Evaluate every candidate against these criteria:

| Criterion | Question | Failure if violated |
|---|---|---|
| **High cardinality** | Many distinct values? | Few values (country, boolean, status) → few shards usable |
| **Even distribution** | Do values receive similar load? | Skew → hot shards |
| **Matches query pattern** | Do the main queries include the key? | Scatter-gather on every request |
| **Locality for transactions/joins** | Is data touched together on one shard? | Cross-shard transactions |
| **Non-monotonic** (for hash/range-on-write) | Do new writes spread? | Last-shard hot spot |
| **Immutable (or rarely changes)** | Is it stable? | Changing the key means moving the row between shards |
| **Not user-controlled adversarially** | Can a client overload one shard? | Targeted overload |

### 10.5.1 Worked examples
| System | Candidate keys | Choice and reasoning |
|---|---|---|
| **Chat** | `message_id`, `sender_id`, `conversation_id`, `recipient_id` | **`conversation_id`** (compound with `sent_at`): "load a conversation" = one shard, ordered by time; `sender_id` would scatter every conversation across shards; `message_id` has no locality. Watch for giant group chats (hot) |
| **E-commerce orders** | `order_id`, `customer_id`, `seller_id` | **`customer_id`** if "my orders" dominates; seller dashboards then scatter (solve via a secondary/derived store keyed by seller). `order_id` random-hash spreads evenly but "my orders" scatters |
| **Social feed / timeline** | `user_id` | `user_id` + `created_at`: one user's timeline = one partition |
| **Multi-tenant SaaS** | `tenant_id` | Tenant isolation, per-tenant locality; handle whales with a directory (dedicated shards) |
| **IoT telemetry** | `device_id` + time bucket | `(device_id, day)` as partition key avoids unbounded partitions for chatty devices and hot "now" ranges |
| **URL shortener** | `short_code` | Hash of the code: uniform point lookups |
| **Ride sharing (driver location)** | geo cell (S2/geohash) | Geo-partitioned; cell sizes adapt to density (dense cities → smaller cells) |
| **Ticketing (flash sales)** | `event_id` | Locality for inventory transactions per event; but one hot event → dedicated handling/queues |

### 10.5.2 Compound and derived keys
- **Composite:** `(tenant_id, user_id)`; the first part gives locality, the second spreads within a tenant.
- **Salted/bucketed:** `(user_id, bucket)` where `bucket = hash(message_id) mod k` to split a huge partition into k sub-partitions; reads must query all k (a controlled scatter).
- **Time-bucketed:** `(entity, day)` bounds partition growth.
- **Avoid** low-cardinality, monotonic, or frequently-updated attributes (email address if users change it).

### 10.5.3 You may need more than one view
No single key serves all queries. Standard solution: shard the primary data by the main access key and maintain **derived copies** (denormalized tables, search indexes, materialized views) sharded by other keys, fed by CDC/events (Ch 4, 17). Accept eventual consistency between copies.

---

## 10.6 Skew and hot spots `[MK]`

### 10.6.1 Why even hashing isn't enough
Hash partitioning balances *keys* (data size), not *load*. Real traffic is skewed (Zipf, Ch 8): a celebrity, a viral item, a big tenant. The shard holding that key is overloaded no matter how many shards exist.

**Measured (Lab 1, part 3):** Zipf(1.1) traffic over 100,000 keys hashed onto 10 shards:
```text
plain hash(key)                       busiest shard = 2.16x average, emptiest = 0.47x average
top-10 hot keys split into 8 sub-keys busiest shard = 1.29x average, emptiest = 0.72x average
```
Even with a perfectly uniform hash, **one shard carries 2.16× the average load** because a handful of keys dominate. Splitting just the ten hottest keys into eight sub-keys each brings the peak to 1.29×.

### 10.6.2 Mitigation toolbox
| Technique | How | Trade-off |
|---|---|---|
| **Cache** hot keys | L1/L2 cache, CDN (Ch 8) | Reads only; staleness |
| **Key splitting (salting)** | Store `hotkey#0..k-1`; writes pick a random suffix; reads query all k and merge | Reads cost k× for hot keys; app complexity; apply only to *detected* hot keys |
| **Dedicated shard/replicas** for known whales | Directory maps whale → own shard; add read replicas | Operational overhead |
| **Aggregate before writing** | Buffer counters/likes and flush in batches (write-behind, Ch 8) | Slight delay; loss window |
| **Randomized or hierarchical counters** | Sharded counters (sum k sub-counters) | Reads sum k values |
| **Adaptive capacity / auto-splitting** | The store detects hot ranges and splits/moves them (DynamoDB adaptive capacity, CockroachDB load-based splitting) | Cannot split a single hot key |
| **Change the model** | Fan-out on read for celebrities, on write for normal users (Ch 26) | More logic |
| **Rate limiting / backpressure** | Protect shard from abuse | Reject some traffic |
Also: **monitor per-shard (and per-key) load**, not just cluster averages: the average hides the hot shard.

### 10.6.3 Time-series and monotonic keys
Prefix with a hash or bucket (`bucket = hash(device) mod 16` then time) so concurrent writers spread; or use compound keys `(device, time)`; or pre-split on the expected key distribution. Accept scatter for "all devices in the last minute" by maintaining a time-ordered secondary store.

### 10.6.4 Data skew vs load skew vs size skew
One shard may be hot (load), huge (data), or both; a few giant tenants can dominate storage while a few popular items dominate traffic. Track storage, QPS, and connection counts per shard separately; balancing one may worsen another.

---

## 10.7 Secondary indexes in a sharded world `[MK/GK]`

You sharded by `conversation_id`, but queries also ask "all messages by sender 7" or "messages containing *refund*".

### 10.7.1 Local (document-partitioned) indexes
Each shard maintains an index over **its own** data only.
```text
 query by sender=7 ─▶ ask EVERY shard ─▶ each searches its local index ─▶ merge results   (scatter/gather)
```
✔ Writes are cheap and local (one shard updates its own index atomically). ✘ Every query by the indexed attribute fans out to **all** shards; latency is the *slowest* shard (tail amplification, below). Used by MongoDB, Cassandra (local secondary indexes), Elasticsearch, most RDBMS shard setups.

### 10.7.2 Global (term-partitioned) indexes
One index over **all** the data, itself partitioned *by the indexed term* (e.g., `sender_id=7` lives on index shard 3).
```text
 query by sender=7 ─▶ hash(sender=7) → index shard 3 ─▶ list of (shard, row ids) ─▶ fetch rows
```
✔ A query for one term hits one index shard (efficient reads). ✘ A write updates **several** index shards (the row's shard plus one per indexed term) → distributed write; usually updated **asynchronously** (eventual consistency; the index lags). Used by DynamoDB global secondary indexes (async), Spanner/CockroachDB (synchronous, via distributed transactions), search systems.

### 10.7.3 Which?
| | Local index | Global index |
|---|---|---|
| Write cost | Low | High (multi-shard) |
| Read by indexed attribute | Scatter-gather (all shards) | Targeted |
| Consistency | Immediate within shard | Often eventual |
| Good when | Writes heavy, secondary queries rare or combined with the shard key | Read-heavy selective lookups on non-shard-key attributes |
**Practical pattern:** put the highest-volume queries on the shard key; serve everything else from purpose-built derived stores (search cluster, analytics warehouse).

---

## 10.8 Cross-shard queries `[MK]`

### 10.8.1 Scatter–gather and the tail-latency tax
A query that must hit all N shards waits for the slowest. If each shard's response exceeds its own p99 latency 1% of the time, then for fan-out N:
> P(query is slow) = 1 − 0.99^N

N = 10 → 9.6%; N = 100 → **63%**; N = 500 → 99.3%. (Chapter 5: tail latency amplification.) Mitigations: fewer shards per query (locality via the shard key), hedged requests, partial results with timeouts, caching, replicas to reduce per-shard variance, tight per-shard timeouts.

### 10.8.2 Typical hard queries
| Query | Difficulty | Approach |
|---|---|---|
| **Top-K / ordering across shards** | Each shard returns its top K; the router merges (K×N items) | OK for small K; avoid deep offsets |
| **Pagination with `OFFSET`** | `OFFSET 10,000 LIMIT 20` needs top 10,020 from *each* shard | Use **keyset (cursor) pagination** (`WHERE (ts,id) < last_seen ORDER BY ts DESC LIMIT 20`) |
| **Aggregations (COUNT, SUM, AVG)** | Partial aggregates per shard, combine at router | AVG needs (sum,count) pairs; `COUNT DISTINCT` needs sketches (HyperLogLog, Ch 20) |
| **Joins across shards** | Network-heavy | Co-locate joined tables on the same shard key; replicate small dimension tables to every shard (broadcast joins); denormalize; or do joins in an analytics system |
| **Global unique constraint** | Two shards can't see each other's rows | Make the unique attribute the shard key, or use a dedicated uniqueness table/service, or accept eventual detection |
| **Full-table analytics** | Scans everything | Offload to a warehouse/lake via CDC (Ch 4) |
Design rule: **choose the shard key so that the 90–99% hot-path queries touch exactly one shard.**

---

## 10.9 Cross-shard transactions and IDs

### 10.9.1 Transactions `[MK/GK]`
Within one shard you keep ACID. Across shards, atomic commit requires a protocol:
- **Avoid:** model so each business transaction fits one shard (co-locate by the key that transactions share: `customer_id`, `tenant_id`, `account pair`).
- **Two-phase commit (2PC):** a coordinator asks all participants to *prepare*, then *commit*. Provides atomicity but blocks if the coordinator fails at the wrong moment and adds latency/locks (details in Ch 17). Used inside NewSQL systems (Spanner, CockroachDB) with consensus-replicated participants to make it robust.
- **Sagas:** a sequence of local transactions with compensating actions on failure; eventual consistency; used across services (Ch 17).
- **Idempotent, ordered messaging** (outbox → queue) to keep two shards eventually consistent.
Example: transferring money between accounts on different shards: either (a) shard by a key so both accounts co-locate (impossible in general), (b) 2PC (slow, tricky), or (c) a saga with an intermediate "in-flight" ledger state and idempotent steps.

### 10.9.2 Generating unique IDs across shards `[MK]`
Per-shard auto-increment collides. Options:
| Method | Properties | Trade-offs |
|---|---|---|
| **UUIDv4** (random 128-bit) | No coordination | Not sortable → poor B-tree insert locality (random page splits, Ch 3/4); 16 bytes |
| **UUIDv7 / ULID** (timestamp prefix + randomness) | Roughly time-ordered, no coordination | Clock dependence; leaks creation time |
| **Snowflake-style** (e.g., 41 bits ms timestamp ∣ 10 bits worker/shard ID ∣ 12 bits sequence) | 64-bit, sortable, ≈4M IDs/s per worker (4,096 per ms), can embed shard ID | Needs unique worker IDs and monotonic clocks; clock rollback handling |
| **Ticket server / range allocation** | Each shard fetches blocks of IDs (e.g., 1,000) from a central allocator | Central dependency (cacheable, HA); gaps |
| **DB sequence with offsets** (shard i issues i, i+N, i+2N…) | Simple | Changing N breaks it; avoid |
| **Embed shard in ID** (`shard_id` bits + local counter) | The ID itself routes the request to the shard | Locks the ID format; resharding must keep IDs valid (use a *logical* shard number that maps to physical nodes) |
**Snowflake arithmetic:** 41 bits of milliseconds = 2^41 ms ≈ 69.7 years; 10 bits = 1,024 workers; 12 bits = 4,096 IDs per ms per worker.
**Routing by ID:** if the ID contains a logical shard number, any service can compute the right shard with no lookup. Use *many* logical shards (e.g., 4,096) mapped to fewer physical nodes so resharding changes the map, not the IDs.

---

## 10.10 Routing: how requests find the right shard `[MK]`

| Approach | How | Examples | Notes |
|---|---|---|---|
| **Client-side (partition-aware driver)** | Client library holds the shard map and routes directly | Cassandra drivers, Redis Cluster clients, Kafka producers | No extra hop; clients need map updates |
| **Routing proxy** | Stateless router/proxy translates logical queries to shards | Vitess `vtgate`, MongoDB `mongos`, ProxySQL, Envoy | Central place for policy; extra hop; can parse queries and scatter/gather |
| **Any-node forwarding** | Contact any node; it forwards to the owner | Cassandra coordinators, DynamoDB, Redis Cluster `MOVED` redirect | Simple clients; extra internal hop |
| **Directory service** | Look up key → shard in a metadata service (cached) | Many multi-tenant systems | Metadata HA and cache invalidation |
**Shard map consistency:** the map changes during rebalancing. Routers cache it; a request sent to the old owner must be **redirected** or rejected so clients refresh (`MOVED`/`ASK` in Redis Cluster; stale-epoch errors elsewhere). The authoritative map lives in a strongly consistent store (etcd/ZooKeeper, Ch 16) or in a consensus-replicated metadata range.
**Connection fan-out:** with N shards and M app instances, direct routing creates M×N connections; proxies/pools bound this (Ch 6 connection explosion).

---

## 10.11 Rebalancing and online resharding `[MK/GK]`

### 10.11.1 When to rebalance
Add capacity; remove or replace nodes; fix skew (hot or oversized shards); change replication placement. Rebalancing moves data over the network *while serving traffic*, which competes with production load.

### 10.11.2 Principles
1. **Move as little as possible** (consistent hashing / fixed partitions / split-merge).
2. **Move in small units** (partition at a time) so progress and rollback are incremental.
3. **Throttle** to protect foreground traffic (bandwidth, IO, compaction).
4. **Keep serving** reads and writes during moves (no downtime).
5. **Be careful with automation:** automatic rebalancing during an overload or failure can cascade (moving data from a struggling node loads it further, and a node falsely declared dead triggers a massive, unnecessary data shuffle). Many systems keep rebalancing human-confirmed or rate-limited and delay re-replication after a node failure (grace period).

### 10.11.3 Moving a partition online (generic recipe)
```text
1. Copy: take a consistent snapshot of partition P from source S, stream it to destination D.
2. Catch up: stream S's subsequent changes (log/CDC) to D until D is within milliseconds.
3. Cutover: briefly pause writes to P (or fence S), apply the final tail to D, flip the shard map: P → D.
4. Redirect: S rejects further requests for P with a redirect/stale-map error; routers refresh.
5. Verify (checksums/row counts), then delete P from S after a safety period.
```
Variants: dual-write to both for a period then backfill; "double the shards and split each in two" (easy with power-of-two logical shards: shard i splits into 2i and 2i+1 in place by hash bit, then each half moves to a new node).

### 10.11.4 Resharding an existing unsharded or hash-mod-N system (migration pattern)
1. Introduce a **routing layer** and **logical shards** (many) in the application; initially all map to the single DB.
2. Stand up new shard databases; **backfill** by copying logical shards (or using CDC/logical replication).
3. **Dual-write** or tail the log to keep copies in sync; verify with checksums/shadow reads.
4. **Cut over reads**, then writes, one logical shard at a time (feature flag per shard); keep the ability to roll back.
5. Decommission the old store after bake time.
Plan for **months**, not days; the cost is why picking logical shards and a routing layer early (while on one machine) is such cheap insurance.

### 10.11.5 Rebalancing in practice
Redis Cluster moves hash slots between nodes (`MIGRATE`, `ASK`/`MOVED` redirects). Kafka reassigns partitions (copy then switch leaders). Cassandra bootstraps a new node by streaming its token ranges. MongoDB's balancer migrates chunks. CockroachDB/Spanner move range replicas automatically with Raft (add learner replica → catch up → promote → remove old).

---

## 10.12 Sharding in real systems `[GK]`

| System | Approach |
|---|---|
| **Vitess (MySQL)** `[Public: YouTube, Slack, GitHub]` | Keyspaces, vindexes (shard-key functions), `vtgate` routing, online resharding with VReplication |
| **Citus (Postgres)** | Distributed tables sharded by distribution column; co-located tables; reference tables replicated everywhere |
| **MongoDB** | Range or hashed sharding on a shard key; config servers (replica set) hold the chunk map; `mongos` routers; balancer moves chunks; zones for geo placement |
| **Cassandra / ScyllaDB** | Hashed partition key onto a token ring with vnodes; compound primary keys; no cross-partition transactions (except LWT) |
| **DynamoDB** | Partition key hashed to partitions (sizes/throughput managed by the service, auto-splits and adaptive capacity), sort key for ordering within a partition; GSIs are global async indexes `[Public]` |
| **HBase / Bigtable** | Range-partitioned sorted tablets/regions, auto-split, hot-spot warnings for sequential keys `[Public]` |
| **Redis Cluster** | 16,384 hash slots over nodes; clients route by slot; hash tags force co-location |
| **Kafka** | Topic partitions chosen by key hash; fixed count (increase possible but changes key→partition mapping) |
| **Elasticsearch / OpenSearch** | Fixed primary shard count per index; document routing by `hash(_routing) mod shards`; replicas per shard |
| **Spanner / CockroachDB / TiDB** | Range partitions replicated by consensus; automatic splitting/rebalancing; distributed transactions |

---

## 10.13 When to shard, and when not to `[MK]`

**Exhaust the cheaper options first (Ch 6 ladder):** fix queries and indexes; add caching; scale up; read replicas; archive/purge old data; compress; table partitioning for manageability; **functional partitioning** (split off a heavy domain such as messages or logs); move append-heavy data to a scale-out store. Many companies run very large businesses on a single well-tuned primary plus replicas.

**Shard when**
- Write throughput or data size exceeds what one (largest) node can handle, after optimization (typical triggers: sustained primary write/IO saturation, storage approaching limits, backup/restore times unacceptable).
- You need isolation (blast radius) or residency (regional data).

**Costs you take on:** application complexity (routing, cross-shard logic), operational complexity (N systems), loss of some SQL features, harder analytics, resharding risk, and a long-lived dependency on the shard key.

**Design for sharding without doing it yet:** include the future shard key (e.g., `tenant_id`/`user_id`) on every table; use globally unique non-sequential IDs; avoid cross-entity joins and transactions on hot paths; route through a data-access layer; use many logical shards from the start.

---

## 10.14 Multi-tenancy patterns `[MK]`

| Pattern | Description | Pros | Cons |
|---|---|---|---|
| **Pooled (shared schema)** | All tenants share tables with a `tenant_id` column | Cheapest; simple ops | Noisy neighbors; harder per-tenant restore; whale tenants |
| **Shard-per-group (directory)** | Tenants mapped to shards by a directory; whales get dedicated shards | Balances cost and isolation; relocatable | Directory + migration tooling |
| **Database-per-tenant** | Each tenant has its own database | Strong isolation, easy per-tenant backup/restore, simple compliance | Many databases to manage; poor utilization for small tenants |
| **Silo (stack-per-tenant)** | Whole deployment per tenant | Maximum isolation/customization | Highest cost; operational sprawl |
Most B2B SaaS evolve from pooled → directory-sharded with whale isolation. Always enforce `tenant_id` filtering in a central layer (row-level security) to prevent cross-tenant leaks.

---

## 10.15 Failure modes of sharded systems

| Failure | Mechanism | Mitigation |
|---|---|---|
| **Hot shard** | Skewed keys/traffic | Key splitting, cache, dedicated shard, adaptive splitting |
| **Monotonic key hot spot** | All writes to newest range | Hash/bucket prefix, pre-splitting |
| **Scatter-gather tail latency** | Fan-out to N shards | Locality, hedging, timeouts, fewer shards per query |
| **Wrong shard key discovered late** | Queries don't include the key | Derived stores; costly migration; validate key against access patterns *before* launch |
| **Resharding outage** | Mass data movement, saturated network, cache storm | Fixed partitions/consistent hashing, throttled online moves |
| **Stale shard map** | Router has old routing | Epoch/redirect protocol, strongly consistent metadata |
| **Metadata store failure** | Directory/config servers down | Replicate with consensus; routers cache last-known-good |
| **Partial failure of a shard** | 1/N of users broken | Replication per shard, graceful degradation, clear error handling |
| **Cross-shard inconsistency** | Failed multi-shard operation | Idempotent sagas, reconciliation jobs |
| **ID collisions or clock rollback** | Bad worker IDs; clock skew | Unique worker allocation, monotonic clock guards |
| **Schema migration drift** | N shards at different versions | Orchestrated, backward-compatible migrations; version tracking |
| **Uneven shard sizes over time** | Growth differs by key range | Monitor; split/merge; directory moves |
| **Backup/restore complexity** | N backups need consistent point-in-time | Per-shard PITR with a coordinated marker; test restores |

---

## 10.16 Labs

### Lab 1: Consistent hashing, vnodes, skew, and range-vs-hash (Java 17+)
```java
import java.util.*;

public class ShardingLab {
    static long mix(long z) {                       // splitmix64 finalizer: a good, cheap 64-bit hash
        z += 0x9E3779B97F4A7C15L;
        z = (z ^ (z >>> 30)) * 0xBF58476D1CE4E5B9L;
        z = (z ^ (z >>> 27)) * 0x94D049BB133111EBL;
        return z ^ (z >>> 31);
    }

    // ---- consistent hashing ring with virtual nodes
    static class Ring {
        final TreeMap<Long, Integer> ring = new TreeMap<>();
        final int vnodes;
        Ring(int nodes, int vnodes) { this.vnodes = vnodes; for (int n = 0; n < nodes; n++) add(n); }
        void add(int node) { for (int v = 0; v < vnodes; v++) ring.put(mix(node * 1_000_003L + v), node); }
        int lookup(long keyHash) { Map.Entry<Long, Integer> e = ring.ceilingEntry(keyHash); return (e != null ? e : ring.firstEntry()).getValue(); }
    }
    // ---- rendezvous (highest random weight) hashing
    static int rendezvous(long key, int nodes) {
        int best = -1; long bw = Long.MIN_VALUE;
        for (int n = 0; n < nodes; n++) { long w = mix(key * 31 + n * 0x9E3779B1L); if (best < 0 || Long.compareUnsigned(w, bw) > 0) { bw = w; best = n; } }
        return best;
    }
    // ---- jump consistent hash (Lamping & Veach)
    static int jump(long key, int buckets) {
        long b = -1, j = 0;
        while (j < buckets) { b = j; key = key * 2862933555777941757L + 1; j = (long) ((b + 1) * ((double) (1L << 31) / (double) ((key >>> 33) + 1))); }
        return (int) b;
    }

    static void movement() {
        int keys = 1_000_000, n = 10;
        System.out.println("== 1) Keys that move when a node is ADDED (10 -> 11 nodes), 1,000,000 keys. Ideal = 1/11 = 9.09% ==");
        long[] kh = new long[keys]; for (int i = 0; i < keys; i++) kh[i] = mix(i);
        int moved = 0; for (long h : kh) if (Math.floorMod(h, (long) n) != Math.floorMod(h, (long) (n + 1))) moved++;
        System.out.printf("hash(key) mod N            : %5.1f%% of keys move%n", 100.0 * moved / keys);
        for (int vn : new int[]{1, 100}) {
            Ring a = new Ring(n, vn), b = new Ring(n, vn); b.add(n);
            moved = 0; for (long h : kh) if (a.lookup(h) != b.lookup(h)) moved++;
            System.out.printf("consistent hashing, %3d vnode(s) per node: %5.1f%% move%n", vn, 100.0 * moved / keys);
        }
        moved = 0; for (int i = 0; i < keys; i++) if (rendezvous(i, n) != rendezvous(i, n + 1)) moved++;
        System.out.printf("rendezvous (HRW) hashing   : %5.1f%% move%n", 100.0 * moved / keys);
        moved = 0; for (int i = 0; i < keys; i++) if (jump(kh[i], n) != jump(kh[i], n + 1)) moved++;
        System.out.printf("jump consistent hash       : %5.1f%% move%n", 100.0 * moved / keys);
    }

    static void balance() {
        int keys = 1_000_000, n = 10;
        System.out.println("\n== 2) Load balance on the ring vs virtual nodes (10 nodes, 1,000,000 keys; ideal = 100,000 each) ==");
        for (int vn : new int[]{1, 10, 100, 1000}) {
            Ring r = new Ring(n, vn); int[] c = new int[n];
            for (int i = 0; i < keys; i++) c[r.lookup(mix(i))]++;
            int max = Arrays.stream(c).max().getAsInt(), min = Arrays.stream(c).min().getAsInt();
            System.out.printf("vnodes=%4d  busiest node = %.2fx average, emptiest = %.2fx average%n", vn, max / 100000.0, min / 100000.0);
        }
    }

    static void hotKey() {
        int shards = 10, requests = 2_000_000, items = 100_000;
        System.out.println("\n== 3) Skew: Zipf(s=1.1) traffic over 100,000 keys hashed onto 10 shards ==");
        double[] cdf = new double[items]; double sum = 0;
        for (int k = 1; k <= items; k++) { sum += 1.0 / Math.pow(k, 1.1); cdf[k - 1] = sum; }
        Random r = new Random(5);
        for (int salt : new int[]{1, 8}) {
            long[] load = new long[shards];
            for (int i = 0; i < requests; i++) {
                double u = r.nextDouble() * sum; int lo = 0, hi = items - 1;
                while (lo < hi) { int mid = (lo + hi) >>> 1; if (cdf[mid] < u) lo = mid + 1; else hi = mid; }
                int k = lo; int sub = (lo < 10 && salt > 1) ? r.nextInt(salt) : 0;      // split only the 10 hottest keys into `salt` sub-keys
                load[(int) Math.floorMod(mix(k * 100L + sub), (long) shards)]++;
            }
            long max = Arrays.stream(load).max().getAsLong(), min = Arrays.stream(load).min().getAsLong();
            System.out.printf("%-34s busiest shard = %.2fx average, emptiest = %.2fx average%n",
                salt == 1 ? "plain hash(key)" : "top-10 hot keys split into 8 sub-keys", max / (requests / (double) shards), min / (requests / (double) shards));
        }
    }

    static void rangeVsHash() {
        int shards = 10, writes = 1_000_000;
        System.out.println("\n== 4) Time-ordered keys (e.g. timestamp prefix): range vs hash partitioning ==");
        // range partitioning: shard i owns an equal slice of the *key space* [0, 10 days). Writes arrive in time order, "now" advances slowly.
        long[] range = new long[shards], hash = new long[shards];
        double span = 10.0 * 24 * 3600;                         // key space = 10 days of seconds; we are on day 5.0 .. 5.1 (writes only touch "now")
        Random r = new Random(1);
        for (int i = 0; i < writes; i++) {
            double ts = 5.0 * 24 * 3600 + r.nextDouble() * 0.1 * 24 * 3600;          // a 2.4 hour window of writes
            range[(int) Math.min(shards - 1, ts / span * shards)]++;
            hash[(int) Math.floorMod(mix((long) (ts * 1000)), (long) shards)]++;
        }
        System.out.println("shard:           " + String.join(" ", java.util.stream.IntStream.range(0, shards).mapToObj(i -> String.format("%6d", i)).toArray(String[]::new)));
        System.out.println("range  writes %: " + String.join(" ", Arrays.stream(range).mapToObj(x -> String.format("%6.1f", 100.0 * x / writes)).toArray(String[]::new)));
        System.out.println("hash   writes %: " + String.join(" ", Arrays.stream(hash).mapToObj(x -> String.format("%6.1f", 100.0 * x / writes)).toArray(String[]::new)));
        System.out.println("A query for 'the last 30 minutes' touches 1 shard under range partitioning, but ALL 10 under hash partitioning.");
    }

    public static void main(String[] a) { movement(); balance(); hotKey(); rangeVsHash(); }
}
```
**Sample output**
```text
== 1) Keys that move when a node is ADDED (10 -> 11 nodes), 1,000,000 keys. Ideal = 1/11 = 9.09% ==
hash(key) mod N            :  90.9% of keys move
consistent hashing,   1 vnode(s) per node:   7.2% move
consistent hashing, 100 vnode(s) per node:   7.6% move
rendezvous (HRW) hashing   :   9.1% move
jump consistent hash       :   9.1% move

== 2) Load balance on the ring vs virtual nodes (10 nodes, 1,000,000 keys; ideal = 100,000 each) ==
vnodes=   1  busiest node = 2.39x average, emptiest = 0.04x average
vnodes=  10  busiest node = 1.59x average, emptiest = 0.56x average
vnodes= 100  busiest node = 1.11x average, emptiest = 0.90x average
vnodes=1000  busiest node = 1.05x average, emptiest = 0.96x average

== 3) Skew: Zipf(s=1.1) traffic over 100,000 keys hashed onto 10 shards ==
plain hash(key)                    busiest shard = 2.16x average, emptiest = 0.47x average
top-10 hot keys split into 8 sub-keys busiest shard = 1.29x average, emptiest = 0.72x average

== 4) Time-ordered keys (e.g. timestamp prefix): range vs hash partitioning ==
shard:                0      1      2      3      4      5      6      7      8      9
range  writes %:    0.0    0.0    0.0    0.0    0.0  100.0    0.0    0.0    0.0    0.0
hash   writes %:   10.0   10.0   10.0   10.0   10.1   10.0   10.0   10.0    9.9   10.0
A query for 'the last 30 minutes' touches 1 shard under range partitioning, but ALL 10 under hash partitioning.
```
(With 1 vnode the "7.2%" is luck: a single random arc; run with other seeds and it ranges widely. That unpredictability *is* the problem vnodes solve.)

**Experiments**
1. Remove a node instead of adding one. Using the ring with 1 vs 100 vnodes, measure how the departing node's keys spread across the survivors (how many receive keys, and how evenly?).
2. Add **weights**: give two nodes twice the vnodes. Verify they own ~2× the keys.
3. Implement **bounded-load consistent hashing** (cap each node at 1.25× average; spill to the next node) and measure balance and key movement.
4. In part 3, detect the hot keys automatically (count-min sketch or sampled counters) and apply splitting only to those; compare the cost of reads (k-way reads) vs the balance gain.
5. Implement `hash mod 1024` fixed partitions with a `partition → node` table; add a node by moving 1/(N+1) of partitions; count keys moved (should equal the ideal).
6. Add range-partitioning with **auto-splitting** (split a range when it holds >X keys) and show that sequential inserts still concentrate on one range until it splits; compute the write rate the "hot" range must absorb.

### Lab 2: Native table partitioning in PostgreSQL (needs a Postgres instance; not executed in the book's sandbox)
```sql
CREATE TABLE events (
  id bigint GENERATED ALWAYS AS IDENTITY, tenant_id int NOT NULL, ts timestamptz NOT NULL, payload jsonb,
  PRIMARY KEY (id, ts)
) PARTITION BY RANGE (ts);
CREATE TABLE events_2026_09 PARTITION OF events FOR VALUES FROM ('2026-09-01') TO ('2026-10-01');
CREATE TABLE events_2026_10 PARTITION OF events FOR VALUES FROM ('2026-10-01') TO ('2026-11-01');
INSERT INTO events(tenant_id, ts, payload) SELECT (random()*100)::int, now() - random()*interval '50 days', '{}' FROM generate_series(1,200000);
EXPLAIN SELECT count(*) FROM events WHERE ts >= '2026-10-01' AND ts < '2026-10-08';   -- partition pruning: only events_2026_10 scanned
DROP TABLE events_2026_09;                                                          -- instant retention: no giant DELETE, no VACUUM
```
Questions: Why must the partition key be part of the primary key? What does *pruning* save? Why doesn't this scale writes across machines, and what would you add (Citus, application sharding)?

### Lab 3: Design the shard key (paper)
For each system pick the partitioning scheme and shard key, state the 3 most important queries, and identify the resulting hot-spot and cross-shard risks: (a) ride-sharing trips, (b) a multi-tenant CRM with a few huge customers, (c) a global leaderboard, (d) an email provider storing mailboxes, (e) a clickstream analytics store.

---

## 10.17 Interview perspective

- Never open with "we'll shard." First show the numbers (writes/s, TB) that require it and the cheaper options exhausted.
- State the **shard key and why**, the **top queries** and how many shards each touches, and the **hot-key plan**.
- Mention **consistent hashing/virtual nodes or fixed partitions** and why `mod N` fails.
- Describe **routing** (proxy/driver + shard map in a consistent store), **IDs** (Snowflake/UUIDv7), and **rebalancing** (online move: copy, catch-up, cutover).
- Call out **cross-shard queries/transactions** as costs and say how you avoid them (derived stores, denormalization, sagas).
- Combine with replication: "each shard has a primary + 2 replicas across AZs."
- Be ready for: "what if one customer is 40% of traffic?" (directory + dedicated shard) and "how do you add shards without downtime?"

---

## 10.18 Common misconceptions

1. **"Replication scales writes."** Only partitioning does.
2. **"Hash partitioning balances load."** It balances keys; hot keys still overload one shard.
3. **"Just shard by `id`."** An `id` shard key makes every business query scatter.
4. **"`hash mod N` is fine; we'll add servers later."** It moves ~(N/(N+1)) of the data.
5. **"Consistent hashing gives perfect balance."** Not without virtual nodes (and even then, only for keys, not load).
6. **"We can change the shard key later."** That is a full data migration.
7. **"Auto-increment IDs are fine across shards."** They collide or leak ordering; use coordinated or unique schemes.
8. **"Global secondary indexes are free."** They make writes multi-shard and usually asynchronous.
9. **"Sharding gives ACID across the cluster."** Cross-shard transactions need 2PC or sagas.
10. **"More shards = better."** More shards = more fan-out, more tail latency, more ops.
11. **"Auto-rebalancing is always safe."** It can cascade during incidents.
12. **"Range partitioning is always bad for writes."** It's excellent when keys aren't monotonic or are pre-split/bucketed.

---

## 10.19 Chapter summary

- **Partitioning** scales writes and data volume (replication cannot); each shard is itself replicated.
- **Schemes:** range (good scans, hot spots on monotonic keys: 100% of writes to one shard in the lab), hash (even keys, scattered ranges), compound (hash entity + sort time), directory (flexible), geo.
- **`hash mod N` moves ~91% of keys** when going 10→11 nodes; **consistent hashing (with vnodes), rendezvous, jump hash, and fixed partitions** move ≈ the ideal 9%. Vnodes bring imbalance from 2.4× down to 1.05× (1 → 1,000 per node).
- The **shard key** is the most consequential decision: high cardinality, even load, matches the dominant queries, enables single-shard transactions, immutable, non-monotonic.
- **Skew is load, not keys:** Zipf traffic gave 2.16× on the busiest shard even with uniform hashing; splitting the 10 hottest keys cut it to 1.29×.
- **Secondary indexes:** local (cheap writes, scatter reads) vs global (targeted reads, multi-shard async writes). **Scatter-gather** has a tail tax: 1−0.99^N (63% at N=100). Use keyset pagination.
- **Cross-shard transactions** are avoided by design, or handled with 2PC/sagas; **IDs** via Snowflake/UUIDv7/range allocation, ideally encoding a logical shard.
- **Routing** via proxy/driver + strongly consistent shard map with redirects; **rebalancing** via small, throttled, online moves (copy → catch-up → cutover → verify).
- **Shard late, but design early:** shard key on every table, unique IDs, a routing layer, many logical shards.

### Mental models
| Concept | Mental model |
|---|---|
| Sharding | Splitting a library's books across several buildings by catalogue range |
| `hash mod N` | Renumbering every house on every street when one house is added |
| Consistent hashing | Houses on a circular road; a new house takes over a short stretch from its neighbour |
| Virtual nodes | Each warehouse owns many scattered plots so no one gets an unlucky big or small chunk |
| Shard key | The address system you can never rename |
| Hot key | One shop on a street that gets all the customers |
| Scatter-gather | Asking every office the same question and waiting for the slowest reply |
| Online resharding | Moving house while still living in it |

---

## 10.20 Exercises

### A. Conceptual
1. Explain how partitioning differs from replication and why most large systems use both.
2. Distinguish vertical, functional, and horizontal partitioning, and table partitioning within one database. Which scale writes across machines?
3. Compare range and hash partitioning on range queries, hot spots, and rebalancing. Give a workload for each.
4. Why does a timestamp-prefixed key create a hot spot in range partitioning? Give three fixes.
5. Explain what a compound key like `(user_id, sent_at)` gives you in Cassandra/DynamoDB.
6. Why does `hash(key) mod N` fail when N changes? What fraction of keys moves from N to N+1?
7. Explain consistent hashing, then virtual nodes, then why vnodes are needed.
8. Compare consistent hashing, rendezvous hashing, jump hash, and fixed-partition schemes. When use each?
9. What are the shard-key selection criteria? Evaluate `country` as a shard key for a global app.
10. For a chat system, compare shard keys `message_id`, `sender_id`, `conversation_id`. Which queries are single-shard under each?
11. Explain hot keys vs hot shards. List five mitigations.
12. Local vs global secondary indexes: write and read costs, consistency. Give an example of each in a real system.
13. Why does scatter-gather latency rise with the number of shards? Derive 1−0.99^N.
14. What is keyset pagination, and why is `OFFSET` pagination hard across shards?
15. How can you do a join across shards? List three strategies.
16. Why are cross-shard transactions hard? Outline 2PC and sagas in two sentences each.
17. Compare UUIDv4, ULID/UUIDv7, Snowflake, and range-allocated IDs.
18. How can an ID encode its shard? What problem does a *logical shard* layer solve?
19. List four routing approaches and the failure mode of each.
20. Describe an online partition move in five steps. What happens to requests during cutover?
21. Why can automatic rebalancing be dangerous during an incident?
22. When should you NOT shard? List five alternatives to try first.
23. Compare the four multi-tenancy patterns.
24. What does DynamoDB's partition key + sort key model imply about queries?

### B. Calculations
25. 40 TB of data, 120,000 writes/s peak. One node comfortably holds 2 TB and sustains 6,000 writes/s. Minimum shards for each constraint? With 50% headroom and 2 years of 60% annual growth, how many shards should you plan?
26. Going from 24 to 25 shards with `mod N`: what fraction of keys move? With consistent hashing? With 2,048 fixed partitions (how many partitions move)?
27. Scatter-gather: shard p99 = 40 ms; p50 = 8 ms. A query fans out to 64 shards. What is the probability the query exceeds 40 ms? If you hedge after 30 ms with a second replica request (assume each attempt exceeds 40 ms with 1% chance, independently), what is the new probability per shard and per query?
28. Snowflake IDs: 41-bit ms timestamp since a custom epoch, 10-bit worker, 12-bit sequence. How long until the timestamp bits overflow? Peak IDs/s for the whole cluster of 1,024 workers? What happens if one worker needs 6,000 IDs in a single millisecond?
29. vnodes: relative std. dev. ≈ 1/√v. For 5% balance (1σ) how many vnodes per node? For a 100-node cluster with 256 vnodes, how many ring entries and approximately how large is the metadata (assuming 24 bytes each)?
30. A hot key receives 30,000 writes/s; each shard can take 8,000 writes/s. Into how many sub-keys must it be split? What does a read cost then?
31. Rebalancing time: moving 2 TB to a new node over a throttled 200 MB/s link (leaving 50% for foreground). How long? If 12 nodes each give up 1/13 of their data (total ~ 1/13 of 24 TB of cluster data) to a 13th node, what is the minimum time with the new node's 200 MB/s ingest limit?
32. Local secondary index: 30 shards, each index lookup takes 4 ms at p50 with 1% of lookups at 60 ms. Estimate the probability that a query hits at least one 60 ms lookup. How does adding 3 replicas to hedge help?

### C. Design
33. Design sharding for a messaging app (1 B users): shard key, partitioning scheme, ID scheme, handling group chats with 1 M members, search, and cross-device sync.
34. Design the move from a single 6 TB Postgres to sharded Postgres without downtime. Include routing, backfill, dual writes, verification, and rollback.
35. Design a multi-tenant SaaS data layer where 5 tenants account for 60% of traffic. Include placement, noisy-neighbour protection, and tenant migration.
36. Design shard keys and secondary access paths for an e-commerce platform (orders, products, sellers, customers, inventory).
37. Design a time-series store ingesting 2 M points/s from 5 M devices with queries by device and time range, and by tenant over time.
38. Choose the number of partitions for a Kafka topic or fixed-partition store you expect to grow from 10 to 200 nodes over three years. Justify.

### D. Debugging
39. One shard is at 95% CPU while the others are at 25%. Give five hypotheses and how to distinguish them.
40. After adding a node, p99 latency doubled for two hours. Explain and prevent.
41. A "get user's orders" query got slower after sharding by `order_id`. Why? Fix.
42. Reports of duplicate user IDs after launching a 2nd shard. Diagnose.
43. A router sends writes to the old shard after a partition move, and data "disappears". Explain what protocol bug allowed this.
44. Search by email takes 600 ms after sharding by `user_id`; the email index is local. Explain and fix.

### E. Interview-style
45. "Design the data layer for Twitter-scale tweets: how do you shard it?"
46. "Your shard key turned out to be wrong. What do you do?"
47. "How do you add capacity to a sharded database without downtime?"

### F. Teach-back
48. Explain consistent hashing to a non-engineer using a circular road and post offices.

---

## 10.21 Full answer key

**1.** Replication copies the same data to several nodes (availability, read scaling); partitioning splits different data across nodes (write and size scaling). Each shard needs its own replicas for availability, so both are used.

**2.** Vertical: split columns (row width/IO). Functional: split domains/features onto different DBs/services. Horizontal (sharding): split rows by key across nodes. Table partitioning: child tables in one DB (maintenance/pruning). Functional and horizontal scale writes across machines; table partitioning and vertical do not.

**3.** Range: efficient scans, hot spots with monotonic keys, easy to split; hash: even distribution, scattered ranges, rebalancing needs fixed partitions/consistent hashing. Range for time-series/ordered scans (with bucketing); hash for point lookups on user IDs.

**4.** New keys always sort last, so all inserts hit the last range. Fixes: hash/bucket prefix, compound keys `(entity, time)`, pre-splitting plus load-based splitting.

**5.** `user_id` (hashed) picks the partition for even spread; `sent_at` sorts rows inside the partition, so "latest N for a user" is a single-partition ordered read.

**6.** The assignment depends on N, so almost every key's `hash mod N` changes. Fraction moved = N/(N+1) (91% at N=10).

**7.** Consistent hashing maps nodes and keys on a ring, key to the next clockwise node; adding a node moves only the arc it takes over. With one point per node the arcs are uneven and a failure dumps load on one neighbour; vnodes give each node many points to average out and spread failover load.

**8.** Consistent hashing: dynamic membership, node lookups O(log N) with a ring. Rendezvous: simple, weights, O(N) per lookup. Jump hash: stateless, numbered buckets with changes at the end. Fixed partitions: simplest operationally; mapping key→partition never changes; move partitions between nodes.

**9.** Criteria: cardinality, even distribution, query match, transaction locality, non-monotonic, immutable, not adversarial. `country`: low cardinality (~200), highly skewed (US/IN/CN), so poor.

**10.** `message_id`: no locality (loading a conversation scatters). `sender_id`: sent messages by a user are single-shard, but a conversation is split across senders. `conversation_id`: loading a conversation is single-shard (best for the dominant query); user-level listing needs a secondary view.

**11.** Hot key: one key with huge traffic; hot shard: the shard holding it (or several heavy keys). Mitigations: caching, key splitting, dedicated shard, write aggregation, adaptive splitting, rate limiting.

**12.** Local: each shard indexes its own rows; cheap writes; reads scatter (MongoDB, Elasticsearch, Cassandra local indexes). Global: index partitioned by term; targeted reads; writes touch multiple shards, usually async (DynamoDB GSI, Spanner).

**13.** The query waits for the slowest of N shards. If each shard is slow 1% of the time, P(no slow shard) = 0.99^N, so P(slow) = 1 − 0.99^N (63% at N=100).

**14.** Keyset: `WHERE (ts,id) < (last_ts,last_id) ORDER BY ts DESC, id DESC LIMIT 20` using the last seen key. `OFFSET k` needs the top k+20 from every shard then a merge-and-discard.

**15.** Co-locate joined tables by the same shard key; replicate small dimension tables to every shard (broadcast join); denormalize data into the main table; or join in an analytics system / application layer with batching.

**16.** Atomic commit across shards needs coordination. 2PC: coordinator collects "prepared" votes, then commits all or aborts all; blocks on coordinator failure. Saga: a chain of local transactions with compensations; eventually consistent.

**17.** UUIDv4: no coordination, unordered (bad index locality). ULID/UUIDv7: time-prefixed, mostly ordered. Snowflake: compact 64-bit, ordered, worker IDs required. Range allocation: blocks from a central allocator; central dependency, gaps.

**18.** Reserve bits for a logical shard number; any service can route from the ID. A logical shard layer maps many logical shards to fewer physical nodes, so resharding edits the map without changing IDs.

**19.** Driver-side (stale maps), proxy (extra hop/SPOF unless replicated), any-node forwarding (extra hop, forwarding storms), directory (metadata dependency/cache staleness).

**20.** Snapshot copy → stream changes → brief pause/fence and final catch-up → flip the shard map → old owner redirects/rejects and is later cleaned. During cutover writes to that partition pause briefly or are rejected/retried.

**21.** Moving data from an overloaded or "failed" node adds IO and network load, worsening the situation; false-positive failure detection can trigger large unnecessary re-replication. Use throttling, delays, and human approval.

**22.** Alternatives: query/index tuning, caching, vertical scaling, read replicas, archiving/purging, functional split, table partitioning, scale-out specialized stores.

**23.** Pooled: cheapest, noisy neighbours; directory-sharded: balance + flexibility; database-per-tenant: strong isolation, many DBs; silo: maximum isolation, highest cost.

**24.** Efficient queries must specify the partition key (equality), and can range-query on the sort key within it; other access paths need GSIs or scans.

**25.** Data: 40/2 = 20 shards. Writes: 120,000/6,000 = 20 shards. Both → 20 minimum. With 50% headroom: 30. Growth: 1.6² = 2.56× in two years → 30 × 2.56 ≈ 77, so plan ~80 shards (or design for adding), using fixed partitions (e.g., 1,024) to allow growth.

**26.** mod N: 24/25 = **96%** move. Consistent hashing: ≈ 1/25 = **4%**. Fixed 2,048 partitions: 2,048/25 ≈ **82 partitions** move (4%).

**27.** P(exceed 40 ms) = 1 − 0.99^64 = 1 − 0.5256 = **47.4%**. With hedging: a shard is slow only if both attempts exceed: 0.01² = 0.0001 per shard; per query: 1 − 0.9999^64 ≈ **0.64%**. (Real hedges aren't fully independent and add load.)

**28.** 2^41 ms = 2.199×10^12 ms ≈ **69.7 years**. Per worker 4,096 per ms = 4.096 M/s; cluster 1,024 × 4.096 M ≈ **4.19 billion IDs/s**. A worker needing 6,000 per ms must wait for the next millisecond (sequence overflows at 4,096) → throttled to ~4.1 M/s.

**29.** v = 400 for 5% (1/√400 = 0.05). Ring entries: 100 × 256 = 25,600; ×24 B ≈ **614 KB**: trivial.

**30.** 30,000/8,000 = 3.75 → at least 4 sub-keys; use 6–8 for headroom. Reads must query all k sub-keys and merge (k× read amplification) or read from a cached merged copy.

**31.** At 200 MB/s × 50% = 100 MB/s, 2 TB = 2,000,000 MB / 100 = 20,000 s ≈ **5.6 h**. Moving 24 TB/13 ≈ 1.85 TB at the new node's 200 MB/s ingest (all cluster bandwidth available): 1.85 TB / 0.2 GB/s ≈ 9,230 s ≈ **2.6 h** minimum (more if throttled).

**32.** P(at least one slow among 30) = 1 − 0.99^30 = **26%**. With hedged requests to replicas (slow only if both slow, 0.01% per shard): 1 − 0.9999^30 ≈ **0.3%**.

**33 (sketch).** Shard by `conversation_id` (hash) with compound `(conversation_id, message_ts, message_id)`; Snowflake IDs with logical-shard bits; big groups: split partitions by time bucket `(conv_id, day)`; membership via separate store; search via an external index fed by CDC; sync via per-device cursors (offsets); user inbox as a derived view `(user_id, last_activity)`.

**34 (sketch).** Add a routing layer and many logical shards; stand up shard DBs; logical-replicate/backfill; verify with checksums and shadow reads; enable dual writes (or CDC), then flip reads per logical shard, then writes; keep rollback (reverse replication) for a bake period; migrate sequences to unique IDs first.

**35 (sketch).** Directory maps `tenant_id` → shard; 5 whales get dedicated shards/replicas; others pooled; per-tenant rate and connection limits; row-level security; migration tool (copy, catch-up, cutover) driven by the directory; monitoring per tenant.

**36 (sketch).** Orders by `customer_id` (+ time); seller view from a derived store keyed by `seller_id` (CDC); products by `product_id` hash (cached heavily), search in Elasticsearch; inventory by `sku`/`product_id` with atomic decrements on one shard (and reservation pattern); customers by `customer_id`.

**37 (sketch).** Partition key `(tenant_id? no) device_id` hashed + day/hour bucket, clustering by time; writes batched; ingest through Kafka partitioned by device; tenant-level time queries served from rollups (pre-aggregated per tenant-minute) in a separate store; downsample older data.

**38 (sketch).** Choose well above the maximum consumer/node count: 200 nodes → at least 400–1,000 partitions (several per node for balance and parallelism), remembering partition overhead and that increasing later changes key→partition mapping. Pick ~1,024.

**39.** Hot key (check per-key QPS), data/size skew, expensive queries (slow log), replica/primary imbalance (reads routed wrongly), background tasks (compaction/vacuum/backup on that node), hardware issue (noisy neighbour, throttled disk). Distinguish via per-key metrics, query logs, host metrics.

**40.** Rebalancing traffic (streaming, compaction) competed with production; caches cold on moved partitions; routers refreshing. Prevent: throttle moves, move off-peak, pre-warm, move one partition at a time, monitor latency and auto-pause.

**41.** Orders for one customer are scattered over all shards (`order_id` hash), so the query fans out. Fix: shard by `customer_id` (compound with `order_id`), or maintain a `customer_orders` derived index.

**42.** Per-shard auto-increment sequences both starting at 1 (collision). Fix: Snowflake/UUIDv7/range-allocated IDs; migrate existing IDs or namespace by shard.

**43.** The protocol lacked fencing/epochs: the old owner kept accepting writes after cutover, and the router's map was stale. Fix: epoch/versioned shard map, old owner rejects requests for moved partitions, routers must refresh on redirect.

**44.** A local index on email needs scatter to all shards. Fix: maintain a global lookup (`email → user_id`) sharded by email (async or via a lookup table) or a search index; then route to the user's shard.

**45 (sketch).** Tweets: shard by `author_id` with Snowflake IDs (time-sortable); timelines are derived (fan-out on write for normal users, on read for celebrities), stored in Redis/KV sharded by `user_id`; search via separate index; counters aggregated; hot tweets cached; replication per shard.

**46 (sketch).** Build the new view/shard key in parallel (CDC to a new store), dual-read/shadow-compare, migrate traffic gradually, keep the old store until verified; communicate and budget months; avoid by validating the key against actual query logs before launch.

**47 (sketch).** Fixed partitions or consistent hashing; add nodes; move partitions online (copy → catch-up → cutover), throttled; update shard map atomically; verify; remove from old nodes; monitor latency.

**48.** A circular road with post offices at various spots; each house belongs to the next post office clockwise. When a new office opens, it only takes the houses between itself and the office before it, so everyone else keeps their post office. Each company owns several scattered branch offices so no one gets an unlucky long stretch.

---

## 10.22 Flashcards

| Front | Back |
|---|---|
| Partitioning vs replication | Different data per node vs copies of the same data |
| Scales writes | Sharding (not replicas) |
| Range partitioning | Good scans; hot spot with monotonic keys (100% to one shard in lab) |
| Hash partitioning | Even keys; scattered ranges |
| Compound key | hash(entity) + sorted(time) inside |
| `mod N` problem | N/(N+1) of keys move (91% at 10→11) |
| Consistent hashing | Ring; ~1/(N+1) keys move; use vnodes |
| vnodes effect | 1 → 2.39× max; 100 → 1.11×; 1000 → 1.05× |
| Rendezvous / jump | Highest score wins / stateless numbered buckets |
| Fixed partitions | Many partitions; move whole partitions between nodes |
| Shard key criteria | Cardinality, even load, query match, immutability, non-monotonic |
| Hot key fixes | Cache, split keys, dedicated shard, aggregate writes |
| Skew lab | Uniform hash still 2.16× on busiest shard; splitting top-10 keys → 1.29× |
| Local index | Cheap writes, scatter reads |
| Global index | Targeted reads, async multi-shard writes |
| Scatter-gather tax | P(slow) = 1 − 0.99^N |
| Keyset pagination | Use last seen key, not OFFSET |
| Cross-shard txn | Avoid; else 2PC or sagas |
| Snowflake ID | 41b ms ∣ 10b worker ∣ 12b seq (≈69.7 yrs, 4096/ms/worker) |
| Routing | Driver / proxy / any-node / directory + versioned shard map |
| Online move | Copy → catch-up → cutover → verify → delete |
| Don't shard yet | Index, cache, replicas, archive, functional split first |

## 10.23 Glossary additions

| Term | Simple Meaning | Technical Meaning | Why It Exists | Example |
|---|---|---|---|---|
| Partition / shard | A slice of data | Subset of rows on one node | Scale writes/size | `messages` shard 3 |
| Shard key | What decides the slice | Attribute used to assign rows | Routing and locality | `conversation_id` |
| Range partitioning | By key ranges | Contiguous key intervals per shard | Ordered scans | HBase regions |
| Hash partitioning | By hash value | `hash(key)` selects shard | Even spread | Cassandra |
| Consistent hashing | Stable assignment | Ring mapping minimizing key movement | Elastic clusters | Dynamo |
| Virtual node (vnode) | Many ring positions | Multiple tokens per physical node | Balance | 256 tokens |
| Rendezvous hashing | Highest score wins | HRW node selection | Simple minimal movement | CDN routing |
| Fixed partitions | Pre-split into many | Constant key→partition map | Easy rebalancing | Redis 16,384 slots |
| Hot spot / hot key | Overloaded spot | Skewed load on one partition/key | Failure mode | Celebrity |
| Salting | Spread a key | Append a suffix to split a hot key | Mitigate hot keys | `key#3` |
| Local index | Per-shard index | Index over a shard's own rows | Cheap writes | Elasticsearch |
| Global index | Cross-shard index | Index partitioned by term | Targeted reads | DynamoDB GSI |
| Scatter–gather | Ask all, merge | Fan-out query and merge | Non-key queries | Search |
| Keyset pagination | Page by last key | Cursor over sort key | Efficient deep paging | `WHERE id < ?` |
| Co-location | Keep related data together | Same shard key for joined tables | Local joins/transactions | `customer_id` |
| Snowflake ID | Sortable unique ID | Timestamp+worker+sequence | Coordination-free IDs | Twitter |
| Logical shard | Virtual shard number | Fixed ID→shard layer mapped to nodes | Resharding without ID change | 4,096 shards |
| Directory | Lookup table | Key/tenant → shard map | Flexibility | Vitess topo |
| Resharding | Change shard layout | Moving data to new partitioning | Growth | Split shard |
| Online migration | Move without downtime | Copy, catch-up, cutover | Availability | VReplication |
| Rebalancing | Even out load/data | Move partitions between nodes | Capacity changes | Redis slot move |
| Cell / silo | Isolated unit | Self-contained stack per group | Blast radius | Tenant stack |
| Noisy neighbour | Tenant hogging resources | One tenant degrading others | Multi-tenancy issue | Whale tenant |

---

**Next: Chapter 11: CDNs and Object Storage.** Edge caching and origin shielding, cache-key design, invalidation and purging, large-file delivery and range requests, object-store architecture and durability (replication vs erasure coding with the math), consistency, pre-signed URLs and direct uploads, multipart upload, lifecycle tiers, and a lab simulating edge/shield hit ratios and durability.

# Chapter 13: Event Streaming and Kafka in Depth

## Chapter card

| | |
|---|---|
| **Objective** | Understand the distributed commit log as a core data structure and Kafka as its flagship implementation: partitions, offsets, segments, replication and the ISR, producer and consumer mechanics, consumer groups and rebalancing, retention and compaction, exactly-once semantics, stream processing, schema evolution, capacity planning, and operations |
| **Why this chapter exists** | Event streams are the backbone of modern data architecture: integration between services, change data capture, analytics pipelines, audit trails, and event-driven systems. “Use Kafka” is a very common answer in design interviews; this chapter makes sure you can say *why*, size it, and describe how it fails |
| **Prerequisites** | Chapters 4 (log-structured storage, CDC), 9 (replication, quorums, ISR preview), 10 (partitioning), 12 (queues, delivery semantics, idempotency) |
| **Study time** | 12–15 hours including labs |
| **Tier** | `[GK]` overall; log abstraction, partitions, consumer groups, delivery semantics `[MK]`; replication internals, transactions, stream processing `[ADV]` |
| **Mastery criteria** | (1) Explain the log abstraction and why sequential append logs are fast. (2) Describe topics, partitions, segments, offsets, and how a consumer finds a message. (3) Explain replication with leader, followers, ISR, high watermark, `acks`, and `min.insync.replicas`, and choose settings for durability vs availability. (4) Explain the producer pipeline (batching, compression, idempotence). (5) Explain consumer groups, offset commits, rebalancing, and lag. (6) Choose retention vs compaction. (7) Explain what Kafka’s exactly-once does and does not cover. (8) Describe stream processing concepts: windows, event time, watermarks, state. (9) Size a cluster from throughput and retention. (10) Know when not to use Kafka |

---

## 13.1 The Story: Twelve Systems, One Spaghetti Diagram

**ShopStream** has an Orders service, Payments, Inventory, Search, Recommendations, Fraud, Email, Analytics, a Data Warehouse, a Customer-Support tool, a Mobile Push service, and a Finance ledger. Over the years each pair that needed data got a point-to-point integration: an API call, a nightly CSV, a cron job, a webhook. Problems accumulate:

1. **N × M integrations.** Twelve systems with ~40 integrations, each with its own format, retry logic, and owner. Adding a 13th system means negotiating with five teams.
2. **Staleness.** The warehouse sees yesterday’s orders; fraud scoring uses data from an hourly batch, too late to stop a bad order.
3. **No replay.** A bug in the Recommendations consumer corrupts three days of derived data. The queue (Ch 12) deleted the messages on ack. To rebuild, they must re-extract from source systems, which no longer have the history.
4. **No single source of truth for “what happened.”** Support asks “what events preceded this order?” and nobody can answer.
5. **Dual writes** everywhere: services write to their DB and then call others; a crash between the two leaves systems inconsistent (Ch 12 outbox).
6. **Throughput limits.** The existing queue handles 5,000 msg/s; Clickstream alone produces 80,000 events/s.

### Pause & Predict 13.1
> If every system *published* what happened to one durable, ordered, replayable record of events, and every other system *read* from that record at its own pace and position, what would change for each of the six problems? What properties must that record have (durability, ordering, throughput, retention, multiple independent readers)? What would be hard?

---

## 13.2 The log abstraction `[MK]`

### 13.2.1 What a log is
A **log** is an **append-only, totally ordered sequence of records**. Each record gets a sequential number, its **offset**. Records are **immutable**; you only append at the end and read from some offset onward.
```text
 offset:   0     1     2     3     4     5     6     7   ...
         ┌─────┬─────┬─────┬─────┬─────┬─────┬─────┬─────┐
 write → │ r0  │ r1  │ r2  │ r3  │ r4  │ r5  │ r6  │ r7  │ ← append only at the head
         └─────┴─────┴─────┴─────┴─────┴─────┴─────┴─────┘
              ▲ consumer A at offset 2        ▲ consumer B at offset 6   (each reader owns its position)
```
We have met logs before: the **WAL** of a database (Ch 3), the **replication log** (Ch 9), the **LSM-tree’s** sequential writes (Ch 4). Kafka exposes this structure as a first-class, distributed, durable service.

### 13.2.2 Log vs queue (recap and sharpening)
| | Queue (Ch 12) | Log (Kafka) |
|---|---|---|
| Message after consumption | Deleted | Stays until retention expires |
| Who tracks position | Broker (per-message ack) | **Consumer** (an offset) |
| Replay | No | **Yes: seek to any offset** |
| Many independent consumers | Fan-out copies | Free: each has its own offset |
| Ordering | Queue-wide | Per partition |
| Per-message retry/delay | Native | Awkward (retry topics) |
| Throughput | Moderate | **Very high** (sequential I/O) |
| Data lifetime | Transient | Durable record of history |

### 13.2.3 Why logs are so fast `[MK]`
1. **Sequential I/O.** Appending to the end of a file and reading forward uses the disk’s (and SSD’s) best access pattern; random I/O is 10–1000× slower (Ch 1/4).
2. **OS page cache.** Kafka writes into the OS cache and lets the kernel flush; recently written data is served to consumers directly from RAM. Brokers use small JVM heaps and leave RAM to the page cache.
3. **Batching everywhere.** Producers batch many records per request; brokers write batches; consumers fetch batches. Per-record overhead is amortized.
4. **Zero-copy transfer.** For consumers reading from the page cache, the broker uses `sendfile` to copy bytes from the cache to the network socket without passing through user space.
5. **Compression** per batch (LZ4, Zstd, Snappy, gzip) reduces network and disk, applied once at the producer and kept compressed through the broker to the consumer.
6. **Durability by replication, not per-record fsync.** Kafka typically doesn’t `fsync` every message; it relies on replicas on other machines (the data lives in at least two memories/disks before ack with `acks=all`).

**Measured (Lab 1, part 4).** Appending 100-byte records to one partition file on the sandbox disk:
```text
fsync every 10,000 records :   999,557 records/s   (109 MB/s)
fsync after EVERY record   :     4,995 records/s
```
**A 200× gap** from batching the durability barrier. This single measurement explains Kafka’s design: batch writes, amortize flushes, and get durability from replicas. (Your absolute numbers depend on hardware; the ratio is the lesson.)

---

## 13.3 Kafka architecture `[MK/GK]`

### 13.3.1 The pieces
```text
        Producers                    Kafka cluster (brokers)                         Consumers
   ┌───────────────┐         ┌──────────────────────────────────────┐          ┌─────────────────────┐
   │ Orders svc    │──┐      │ Broker 1   Broker 2   Broker 3       │      ┌──▶│ Group "fraud"       │
   │ Clickstream   │──┼────▶ │ topic "orders"                       │ ─────┤   │  c1(p0,p1) c2(p2,p3)│
   │ CDC connector │──┘      │  p0: L@b1  F@b2  F@b3                │      │   └─────────────────────┘
   └───────────────┘         │  p1: F@b1  L@b2  F@b3                │      └──▶ Group "warehouse-etl"
                             │  p2: F@b1  F@b2  L@b3  (L = leader)  │           (independent offsets)
                             │  p3: L@b1  F@b2  F@b3                │
                             └──────────────────────────────────────┘
                              Controller quorum (KRaft) or ZooKeeper: metadata, leader election
```
- **Broker:** a server storing partitions and serving produce/fetch requests.
- **Topic:** a named stream of records (e.g., `orders`, `page-views`).
- **Partition:** a topic is split into *P* partitions; each partition is an independent log (ordered, with its own offsets). The partition is the unit of **parallelism, ordering, and replication**.
- **Replication:** each partition has a **leader** (serves all reads/writes) and **followers** (replicate the leader). Replication factor (RF) is typically 3.
- **Controller:** manages cluster metadata and partition leader election. Modern Kafka uses **KRaft** (a Raft-based metadata quorum) instead of ZooKeeper `[Public]`.
- **Producers** append records; **consumers** read records in **consumer groups**.
- **Offsets** for consumer groups are stored in an internal topic (`__consumer_offsets`).

### 13.3.2 A record
`key` (optional bytes), `value` (bytes), `headers` (metadata, e.g., trace ID), `timestamp` (create or log-append time), and assigned `offset`. Keys determine partition; values are opaque to Kafka (JSON/Avro/Protobuf by convention).

### 13.3.3 On-disk layout: segments `[GK]`
A partition is a directory of **segment** files:
```text
/var/kafka/orders-0/
   00000000000000000000.log        ← records offset 0 .. 1,048,575   (e.g., up to 1 GiB or 7 days)
   00000000000000000000.index      ← sparse offset → file position index
   00000000000000000000.timeindex  ← timestamp → offset index
   00000000000001048576.log        ← next segment named by its base offset
   ...                              (only the newest segment is “active” for writes)
```
- **Append** goes to the active segment; when it reaches `segment.bytes` or `segment.ms`, it **rolls** to a new file.
- **Lookup of offset N:** binary-search segment base offsets to find the segment, binary-search the sparse index for the nearest position ≤ N, then scan forward a few records. Reads are therefore O(log n) to locate, then sequential.
- **Retention deletes whole segments** (cheap: delete files), not individual records.
- Lab 1 reproduces the essentials: records appended to a file, an in-memory `offset → file position` index, and reads from any offset.

### 13.3.4 Request flow
**Produce:** producer → leader of the target partition → appended to the leader’s log → followers fetch and append → leader advances the **high watermark** when enough replicas have it → ack to the producer (depending on `acks`).
**Fetch:** consumer → leader (or a follower via “follower fetching”, for locality) → returns a batch of records from the requested offset, only up to the **high watermark** (so consumers never see data that might be lost on leader failover).

---

## 13.4 Partitions `[MK]`

### 13.4.1 What partitions give
- **Scalability:** a topic’s throughput scales with partitions spread over brokers.
- **Parallel consumption:** each partition is consumed by **at most one** consumer within a group, so a group can have up to P active consumers.
- **Ordering:** records within a partition are strictly ordered; **there is no ordering across partitions**.

### 13.4.2 Key → partition
Default partitioner: `partition = hash(key) mod P` (murmur2). Same key → same partition → per-key order. Null keys are spread (sticky batches/round-robin).

**Measured (Lab 1, part 1).** 100,000 events, 1,000 keys, 4 partitions:
```text
records per partition: [24900, 24900, 25100, 25100]   per-key ordering violations: 0  (a key always maps to one partition)
```
Per-key order is preserved; the total order across keys is not defined.

### 13.4.3 Choosing the number of partitions
Constraints:
1. **Throughput:** `P ≥ target_throughput / per-partition_throughput`. Per-partition producer throughput is limited by a single leader’s disk/network and batching (often 10–50 MB/s achievable; plan conservatively, e.g., 10 MB/s). Consumer side: `P ≥ target_throughput / per_consumer_throughput`.
2. **Consumer parallelism:** the maximum number of active consumers in a group is P.
3. **Growth:** partitions can be **increased** later but never decreased, and increasing changes `hash(key) mod P`, breaking per-key ordering across the change and key-based state in stream processors (Ch 10 fixed-partition lesson).
4. **Overhead:** each partition = files/handles, replication fetchers, memory for index/buffers, leader election work after broker failure. Clusters commonly keep thousands (not hundreds of thousands) of partitions per broker; more partitions → longer failover recovery and more end-to-end latency.
5. **Hot keys:** a hot key lives on one partition; partitions don’t fix key skew.
**Heuristic:** estimate peak throughput and growth, divide by a conservative per-partition rate and per-consumer rate, take the max, multiply by 2–3 for growth, round to a convenient number (e.g., 48, 96).

---

## 13.5 Replication, durability, and availability `[GK/ADV]`

### 13.5.1 Leader, followers, and the ISR
Each partition has one **leader**; followers pull data from the leader (like a consumer). The **In-Sync Replica set (ISR)** is the leader plus all followers that are caught up (within `replica.lag.time.max.ms`). A follower that falls behind or stalls is removed from the ISR; when it catches up, it rejoins.
- **High watermark (HW):** the highest offset replicated to *all* ISR members. Only records ≤ HW are visible to consumers and considered committed.
- **Leader failure:** the controller elects a new leader **from the ISR**; because every ISR member has all committed records, **no committed record is lost**. **Leader epochs** prevent stale leaders from truncating or serving wrongly (the same fencing idea as Ch 9).

### 13.5.2 Producer acknowledgements (`acks`)
| `acks` | Meaning | Durability | Latency |
|---|---|---|---|
| `0` | Don’t wait | May lose data silently (even before reaching the broker) | Lowest |
| `1` | Leader appended (not replicated) | Lose acked data if leader dies before followers copy (async replication window, Ch 9) | Low |
| `all` (`-1`) | All **current ISR** members have it | No loss while at least one ISR member survives | Higher |

### 13.5.3 `min.insync.replicas` and the durability/availability dial
With `acks=all`, the broker also requires `ISR size ≥ min.insync.replicas` or it **rejects writes** (`NotEnoughReplicas`). This prevents `acks=all` from silently degrading to “leader only” when the ISR has shrunk to 1.

For **RF=3**:
| Setting | Tolerates | Behavior |
|---|---|---|
| `acks=all`, `min.insync.replicas=2` | **1 broker failure with no data loss and writes continue** | The standard production choice. If two replicas are down, writes are rejected (availability sacrificed for durability) |
| `acks=all`, `min.insync.replicas=1` | Writes continue with only the leader alive; **data loss possible** if that leader then dies | Availability over durability |
| `acks=1` | Fast; may lose acked writes on leader failure | Metrics/logs where loss is tolerable |
| `min.insync.replicas=3` | Any single failure blocks writes | Rarely desirable |
Rule of thumb: **RF=3, min.insync.replicas=2, acks=all** (+ idempotent producer) for important data; spread replicas across racks/AZs (`broker.rack`).

### 13.5.4 Unclean leader election
If **all ISR members are gone** (e.g., the only caught-up replica dies), the partition is offline. `unclean.leader.election.enable=true` lets an **out-of-sync replica** become leader: availability returns but records that replica lacked are **lost** and consumers may see offsets move backward. Default (and recommended for durable data): `false`. This is exactly the Ch 9 failover trade-off (promote a lagging replica = lose acked writes).

### 13.5.5 Compared with consensus replication (Ch 9/16)
Kafka’s ISR approach is “primary–backup with a dynamic membership set” (as in PacificA/Microsoft research designs): writes need acks from the *current ISR*, not a fixed majority. It tolerates `RF−1` failures for committed data (versus `⌊RF/2⌋` for majority quorums) at the cost of relying on a separate, strongly consistent metadata quorum (the controller) to track the ISR and elect leaders. KRaft brings Kafka’s own metadata onto Raft.

### 13.5.6 Failure walk-through (RF=3, min.isr=2, acks=all)
1. All three replicas in sync: writes ack after 3 copies.
2. Follower B stalls → removed from ISR (ISR={A,C}); writes still succeed (2 ≥ min.isr); **under-replicated partition** alert fires.
3. Leader A crashes → controller elects C (in ISR) as leader; all committed data present; producers retry to the new leader; brief unavailability (seconds).
4. ISR={C} (size 1 < 2) → writes rejected until A or B returns and catches up; reads still work. No data loss.

---

## 13.6 Producers `[MK/GK]`

### 13.6.1 The pipeline
`send(record)` → serialize → choose partition → append to a per-partition **batch** in the record accumulator → a sender thread ships batches (grouped per broker) when `batch.size` is reached or `linger.ms` expires → broker acks → callback/future completes.

Key settings:
| Setting | Effect |
|---|---|
| `linger.ms` | Wait up to this long to fill batches (0 = send immediately; 5–20 ms improves throughput substantially at small latency cost) |
| `batch.size` | Max bytes per partition batch (e.g., 64–256 KB) |
| `compression.type` | lz4/zstd/snappy/gzip; compress whole batches; big win for JSON |
| `acks` | See §13.5 |
| `enable.idempotence` | De-duplicate retries (below) |
| `max.in.flight.requests.per.connection` | Pipelining; ≤5 with idempotence preserves order |
| `retries`, `delivery.timeout.ms` | Retry window (default ~2 minutes) |
| `buffer.memory`, `max.block.ms` | Producer backpressure: if the buffer is full, `send()` blocks then fails |
Batching arithmetic: 2 KB records, `batch.size=64 KB` → ~32 records per batch; at 100,000 records/s ≈ 3,100 batches/s instead of 100,000 requests/s.

### 13.6.2 Idempotent producer `[GK]`
Without idempotence, a retry after a timeout (the first request actually succeeded) **duplicates** the record; with `max.in.flight > 1`, a retry can also **reorder** records. With `enable.idempotence=true` the broker assigns the producer a **producer ID (PID)** and the producer attaches a per-partition **sequence number** to each batch. The leader rejects duplicates (sequence already seen) and out-of-order sequences. Result: **exactly-once, in-order writes per partition for the life of the producer session**. (Default in recent Kafka versions.)

### 13.6.3 Partitioning and key design
Pick keys that preserve the ordering you need (`order_id`, `account_id`) and spread load. Beware: null key + sticky partitioner spreads by batch; `Unknown` or constant keys create hot partitions; low-cardinality keys (country) skew.

### 13.6.4 Error handling
Retriable errors (leader changed, network) → library retries within `delivery.timeout.ms`. Non-retriable (record too large, serialization error) → surface to the app. **Always handle the send callback**; fire-and-forget loses errors. For critical events from a database transaction use the **outbox/CDC** (Ch 12) instead of dual writes.

---

## 13.7 Consumers and consumer groups `[MK/GK]`

### 13.7.1 The consume loop
```java
consumer.subscribe(List.of("orders"));
while (true) {
    ConsumerRecords<K, V> records = consumer.poll(Duration.ofMillis(500));
    for (var r : records) process(r);          // must be idempotent (at-least-once)
    consumer.commitSync();                      // commit offsets AFTER processing → at-least-once
}
```
The consumer **pulls**: it controls rate and position (natural backpressure).

### 13.7.2 Offsets and delivery semantics `[MK]`
The consumer’s position is stored per (group, topic, partition) in `__consumer_offsets` when it **commits**. Where you commit relative to processing sets the semantics (exactly the crash windows of Ch 12):
| Strategy | Semantics | Crash behavior |
|---|---|---|
| Commit **before** processing | At-most-once | Crash after commit → records skipped (lost) |
| Commit **after** processing | **At-least-once** (default choice) | Crash before commit → records reprocessed (duplicates) |
| Commit **with the output in one transaction** (Kafka transactions, or same DB transaction storing offset + effect) | Exactly-once effect | Both or neither |
Auto-commit (`enable.auto.commit=true`, every 5 s) commits what was *polled*, not necessarily processed: it can lose or duplicate unpredictably; production code usually commits manually.

**Measured (Lab 1, part 2).** A consumer for partition 2 processes in batches of 500 and commits after each; it dies after processing up to offset 13,000 but had committed only 12,500:
```text
partition 2: consumer crashes after processing up to offset 13000, but the committed offset is only 12500
             restart resumes at offset 12500 -> 500 records are processed again
records processed (incl. replays): 100500  vs unique records: 100000  -> duplicates: 500
```
The replay window equals the uncommitted batch. Smaller batches or more frequent commits shrink duplicates but add overhead; idempotent processing makes the window harmless.

### 13.7.3 Consumer groups `[MK]`
Consumers sharing a `group.id` **divide the partitions** of subscribed topics among themselves: each partition is assigned to exactly one consumer in the group. Different groups are completely independent (each has its own offsets): this is how Kafka acts as a queue (one group) *and* as pub/sub (many groups).
```text
 topic orders (4 partitions)         group "fraud" (2 consumers)      group "warehouse-etl" (1 consumer)
   p0 ─────────────────────────────▶ c1                                c1 (all four partitions)
   p1 ─────────────────────────────▶ c1
   p2 ─────────────────────────────▶ c2
   p3 ─────────────────────────────▶ c2
```
**Measured (Lab 1, part 2):** 4 partitions with 3 members → `[[0, 3], [1], [2]]`; with 5 members → `[[0], [1], [2], [3], []]`: the fifth consumer sits idle. **Parallelism is capped by the partition count.**

### 13.7.4 Rebalancing `[GK]`
When group membership changes (consumer joins, leaves, crashes, or misses heartbeats) or partitions change, the **group coordinator** (a broker) triggers a **rebalance** to reassign partitions.
- **Eager rebalance (older):** all consumers give up all partitions, then get new assignments: a **stop-the-world pause** for the whole group.
- **Cooperative (incremental) rebalance:** only the partitions that move are revoked; others keep processing.
- **Static membership** (`group.instance.id`) lets a restarting consumer reclaim its partitions without triggering a rebalance (rolling restarts).
- **Timeouts:** `session.timeout.ms` (no heartbeat → declared dead; heartbeats are sent by a background thread), `max.poll.interval.ms` (if the consumer takes longer than this between `poll()` calls, it is considered stuck and kicked out). Slow processing > `max.poll.interval.ms` → consumer evicted → rebalance → partition reassigned → duplicate processing → can trigger a **rebalance storm** (everyone slow, everyone evicted).
- **Mitigations:** keep per-poll work bounded (`max.poll.records`), process asynchronously with care, raise `max.poll.interval.ms` for slow handlers, use cooperative-sticky assignor, static membership, avoid long GC pauses, scale gradually.

### 13.7.5 Lag
**Consumer lag** = (log end offset − committed offset) per partition. It is the main health metric. Lag growing ⇒ consumers slower than producers.
*Example:* lag 600,000 messages; consumers process 12,000/s while producers add 8,000/s ⇒ net drain 4,000/s ⇒ 150 s to catch up. In *time*: lag ÷ consumption rate.

### 13.7.6 Replay and new consumers
Because the log is retained, you can: start a **new group at the earliest offset** to rebuild a derived store; **seek to a timestamp** (e.g., 2 hours ago) to reprocess after a bug; **reset offsets** for a group (with the group stopped).

**Measured (Lab 1, part 3):** a brand-new group reads partition 0 from offset 0 while the old group’s committed offset stays at 24,900 of 24,900; the data was not consumed away. This is the fix for Story problem 3.
```text
offset 0  key=user-985  value=event-0
offset 1  key=user-978  value=event-0
offset 2  key=user-592  value=event-0
old group committed offset for partition 0 is still 24900 of 24900
```

---

## 13.8 Retention and compaction `[MK/GK]`

### 13.8.1 Retention
Kafka keeps records until a policy removes whole segments: `retention.ms` (e.g., 7 days), `retention.bytes`, or **infinite** (with **tiered storage** moving old segments to object storage, Ch 11). A consumer that falls behind **more than the retention window** loses unread data (its offset points to deleted records: `OffsetOutOfRange`), so alert on lag relative to retention.

### 13.8.2 Log compaction
Instead of time-based deletion, a **compacted topic** keeps **at least the latest record for each key**. A background cleaner removes older records with the same key. A record with a key and a **null value** (**tombstone**) means “delete this key” and is itself removed after `delete.retention.ms`.
```text
before: (k1,v1) (k2,v1) (k1,v2) (k3,v1) (k2,null) (k1,v3)
after : (k3,v1) (k1,v3)          [(k2,null) kept briefly as a tombstone then removed]
```
Uses: **changelog/table semantics** (the topic is the latest state of each entity: user profiles, product catalog, configuration), restoring caches or state stores by replaying the topic, CDC streams, Kafka Streams state-store backing. Properties: ordering of retained records preserved; consumers reading from the beginning get the *latest value per key* (plus some older ones not yet cleaned). Compaction is not instant; don’t depend on exact cleanup timing.
*Sizing:* 10 M keys × 1 KB latest values ≈ 10 GB compacted, regardless of whether the topic received 10 B updates.

### 13.8.3 The stream–table duality `[GK]`
A **stream** of changes can be folded into a **table** (latest value per key); a **table**’s changes form a **stream** (CDC). Compacted topics sit exactly on this boundary. It underlies Kafka Streams/Flink “KTable” semantics and CQRS/event-sourced projections (Ch 17).

---

## 13.9 Exactly-once semantics (EOS) in Kafka `[ADV]`

Kafka offers three layers:
1. **Idempotent producer:** no duplicate or reordered writes per partition per producer session (§13.6.2).
2. **Transactions:** a producer can atomically write to **multiple partitions/topics** and **commit consumer offsets** in one transaction (`initTransactions`, `beginTransaction`, `send…`, `sendOffsetsToTransaction`, `commitTransaction`). Each producer has a `transactional.id` so the broker can fence **zombie** instances (an old instance after a rebalance is rejected via epochs). Consumers with `isolation.level=read_committed` see only committed transactional records (aborted records are filtered).
3. **Streams EOS (`processing.guarantee=exactly_once_v2`):** consume → process → produce → commit offsets atomically in Kafka Streams.

**Scope and limits (be precise in interviews):** exactly-once holds for **Kafka-to-Kafka** read-process-write. The moment you perform an external side effect (HTTP call, database write, email), you are back to at-least-once and need **idempotent effects** (Ch 12) or to write the effect and offset together in the same external transaction. Transactions add latency (coordination, markers) and throughput overhead (typically modest), and `read_committed` consumers wait on open transactions.

---

## 13.10 Stream processing `[GK]`

### 13.10.1 Why
Process events continuously as they arrive: fraud detection, real-time aggregation (orders per minute), enrichment (join clicks with user profile), alerting, materialized views, ETL without nightly batches.

### 13.10.2 Core concepts
| Concept | Meaning |
|---|---|
| **Stateless operations** | map, filter, flatMap: no memory of past events |
| **Stateful operations** | aggregate, count, join, dedup: need **state** per key |
| **State store** | Local embedded store (RocksDB) per task, **backed by a changelog topic** in Kafka for fault tolerance and rebuild |
| **Parallelism** | One task per input partition; scale by partitions; state is local to the partition’s key range |
| **Repartitioning** | If you need a different key (group by `product_id`), re-key and write to an intermediate topic (a shuffle) |
| **Event time vs processing time** | When it *happened* (in the record) vs when the processor *sees* it; mobile events can arrive minutes late |
| **Windows** | Tumbling (fixed, non-overlapping: 1-minute buckets), hopping/sliding (overlapping), session (gaps of inactivity) |
| **Watermark** | The processor’s estimate that no events older than T will still arrive; windows close when the watermark passes |
| **Late data** | Events arriving after the watermark: drop, side-output, or update previous results (allowed lateness) |
| **Joins** | Stream–stream (windowed), stream–table (enrich with latest value), table–table |
| **Exactly-once state updates** | State and output committed atomically with offsets |
**Frameworks:** Kafka Streams (library, no cluster), ksqlDB (SQL), Apache Flink (strong event-time/state, large scale), Spark Structured Streaming (micro-batch/continuous), Beam (portable model).

### 13.10.3 Architectures
- **Lambda:** a batch layer (accurate, slow) plus a speed layer (fast, approximate) with a serving merge; duplicated logic.
- **Kappa:** everything is a stream; reprocessing = replay the log through new code; one codebase. Requires long retention/tiered storage and idempotent, deterministic processing.
- **Streaming ETL + lakehouse:** Kafka → (Flink/Connect) → object storage tables (Iceberg/Delta) for analytics (Ch 11).

---

## 13.11 Schemas and evolution `[MK/GK]`

Events are long-lived contracts between teams. Without discipline, one producer change breaks every consumer (including future replays of old data!).
- **Serialization:** prefer compact, schema-based formats (Avro, Protobuf) over ad-hoc JSON; they enable evolution and smaller payloads.
- **Schema registry:** a service storing versioned schemas per subject; messages carry a schema ID; producers/consumers fetch schemas; the registry enforces **compatibility** on registration.
- **Compatibility modes:** *Backward* (new schema can read old data: upgrade consumers first), *Forward* (old consumers can read new data: upgrade producers first), *Full* (both), plus transitive variants across all versions.
- **Safe changes:** add optional fields with defaults; remove optional fields; never change a field’s meaning or type; in Protobuf never reuse field numbers; use enums carefully (add `UNKNOWN`).
- **Topic versioning:** for breaking changes, publish a new topic (`orders.v2`) and dual-publish during migration.
- **Poison pills:** a record that cannot be deserialized will block a partition if the consumer throws and retries forever. Strategies: catch, log, send to a **dead-letter topic**, and continue; validate at the producer; schema registry checks.
- **PII and governance:** classify fields, encrypt or tokenize sensitive data, remember that logs are retained (right-to-erasure requires tombstones/compaction design or crypto-shredding).

---

## 13.12 Patterns `[MK/GK]`

| Pattern | Idea |
|---|---|
| **Event backbone / data hub** | Services publish domain events; any number of consumers subscribe independently (solves N × M) |
| **Change data capture (CDC)** | Debezium reads the DB’s WAL and publishes row changes to Kafka (Ch 4): reliable replication of DB state without dual writes |
| **Transactional outbox → Kafka** | Business state + outbox row in one DB transaction; CDC publishes it (Ch 12) |
| **Event sourcing** | Store state as an immutable log of events; rebuild state by replay (Ch 17) |
| **CQRS projections** | Consumers build read-optimized views (search index, cache, materialized tables) from the log |
| **Retry and dead-letter topics** | Non-blocking retries: failed record → `orders.retry.1m` → `retry.10m` → `orders.dlq`, so one bad message doesn’t block a partition |
| **Compacted topic as reference data** | Latest product catalog/config for stream joins and cache warm-up |
| **Log-based integration for ML/analytics** | Feature pipelines and warehouse loads from the same stream |
| **Backfill / reprocessing** | Reset offsets or start a new group with new code to rebuild derived data |
| **Request/reply** | Possible with correlation IDs, but usually a poor fit; use direct RPC |

---

## 13.13 Capacity planning and operations `[GK]`

### 13.13.1 Sizing method
Given: ingress *W* MB/s, retention *T*, replication factor *RF*, consumer groups *G*.
- **Disk** = W × T × RF × (1 + overhead 25–30%) (compression reduces W on disk).
- **Network:** producer ingress W; **replication traffic** W × (RF−1); **consumer egress** W × G (each group reads everything; page-cache hits help disk but not network). Total broker egress ≈ W × (RF − 1 + G).
- **Partitions:** from throughput and consumer parallelism (§13.4.3).
- **Brokers:** enough for disk, network (plan ≤ ~50–60% NIC utilization), CPU (compression, TLS), and partition count per broker (a few thousand), plus headroom for losing one broker/AZ.
- **Memory:** modest JVM heap (6–8 GB) and *lots* of RAM for page cache (so recent data serves from memory).

### 13.13.2 Worked example
ShopStream peak ingress 50 MB/s (after compression), retention 7 days, RF=3, 4 consumer groups.
- Disk: 50 MB/s × 604,800 s = 30.2 TB per copy × 3 = **90.7 TB**; with 30% headroom ≈ **118 TB**. With 12 TB usable per broker → ≥10 brokers by disk.
- Network: replication 50 × 2 = 100 MB/s; consumer egress 50 × 4 = 200 MB/s; cluster-wide ≈ 350 MB/s in+replication+out ≈ 2.8 Gbit/s total, spread over ≥10 brokers: trivial per node.
- Partitions: suppose one hot topic carries 200 MB/s at peak. At 10 MB/s per partition that needs 20; at 5 MB/s per consumer it needs 40; with ×3 growth → **120 partitions** for that topic.
- Result: ~10–12 brokers across 3 AZs, RF=3 with rack awareness, min.isr=2.

### 13.13.3 Monitoring that matters
| Metric | Meaning / action |
|---|---|
| **Under-replicated partitions** (should be 0) | A follower is behind or a broker is down; investigate immediately |
| **ISR shrink/expand rate** | Flapping = slow disks/network/GC |
| **Offline partitions / active controller count** (must be 1) | Availability emergencies |
| **Consumer lag per group/partition** (and time lag) | Consumers behind; capacity or poison messages |
| **Request latency** (produce/fetch p99), request queue time | Broker saturation |
| **Disk usage and log flush/IO wait** | Capacity and hardware issues |
| **Network and CPU** per broker | Compression/TLS cost, imbalance |
| **Partition skew / leader skew** | Rebalance (Cruise Control) |
| **Producer error/retry rates; record-send latency** | Client health |
| **GC pauses** | Cause ISR shrinks and session timeouts |
| **Rebalance rate per group** | Instability |

### 13.13.4 Operations
- **Rolling upgrades/restarts:** one broker at a time, wait for ISR to recover (controlled shutdown moves leaders first).
- **Adding brokers** does not move data automatically: **reassign partitions** (use Cruise Control; throttle replication).
- **Quotas** per client/user to prevent noisy neighbors; **ACLs, TLS, SASL** for security.
- **Multi-cluster / multi-region:** MirrorMaker 2 / cluster linking replicate topics asynchronously (offset translation is tricky); stretch clusters across AZs are fine, across distant regions suffer latency; active–active needs conflict/loop avoidance.
- **Disaster recovery:** define RPO (async replication lag) and consumer offset sync; test failover.
- **Topic governance:** naming, ownership, retention defaults, schema review, auto-create disabled.

---

## 13.14 Alternatives and when not to use Kafka `[MK]`

| Option | Notes |
|---|---|
| **Apache Pulsar** | Separates stateless brokers from BookKeeper storage; native multi-tenancy, geo-replication, per-message ack, tiered storage |
| **Amazon Kinesis / Google Pub/Sub / Azure Event Hubs** | Managed streaming/pub-sub; less ops, different limits and semantics |
| **Redpanda** | Kafka-API-compatible, C++ single binary, no ZooKeeper/JVM |
| **RabbitMQ (queues/streams)** | Rich routing, per-message ack/priority/TTL; lower throughput; RabbitMQ Streams add log semantics |
| **NATS JetStream** | Lightweight, low latency |
| **SQS/SNS** | Simplest managed work queues and fan-out |
| **Database + CDC** | For modest scale and strong transactional needs |
**Don’t use Kafka for:** simple task queues with per-message retry/delay/priorities (use SQS/RabbitMQ), tiny scale where operational overhead outweighs benefits, request/response RPC, very large messages (>1–10 MB; use claim-check), or when nobody can operate a distributed cluster (use managed).

---

## 13.15 Failure modes

| Failure | Mechanism | Mitigation |
|---|---|---|
| **Consumer lag grows** | Slow consumers, poison message loop, downstream slowdown | Scale consumers (≤ partitions), fix handler, bounded retries + DLQ topic, alert on lag/time-lag |
| **Rebalance storm** | Handlers exceed `max.poll.interval.ms`, GC pauses, flapping pods | Cooperative-sticky, static membership, bound poll work, tune timeouts |
| **Under-replicated partitions / ISR flapping** | Slow disk/network/GC on a broker | Fix hardware/GC, rack-aware placement, replica fetch tuning |
| **Data loss after leader failure** | `acks=1`, `min.isr=1`, or unclean election | `acks=all`, `min.isr=2`, RF=3, unclean election off |
| **Producer duplicates/reordering** | Retries without idempotence | `enable.idempotence=true`, `max.in.flight ≤ 5` |
| **Hot partition** | Skewed key | Better key, salted keys, more partitions (with care), separate topic |
| **Retention expires before consumption** | Long outage or lag > retention | Longer retention/tiered storage, lag alerts |
| **Disk full** | Retention too long, traffic spike, compaction lag | Capacity headroom, quotas, alerts at 70/80%, retention tuning |
| **Too many partitions** | Overhead, slow elections, memory | Plan counts; consolidate topics; hardware |
| **Poison pill (deserialization)** | Bad record blocks partition | Dead-letter topic; schema registry; tolerant deserializer |
| **Large messages** | Exceed `max.message.bytes`, memory pressure | Claim-check pattern |
| **Zombie producers/consumers** | Old instance after rebalance writes/commits | Transactional IDs and epochs; fence; idempotent effects |
| **Offset commit ahead of processing** | Async/auto-commit bugs | Commit after processing; handle rebalance callbacks |
| **Schema break** | Incompatible change | Registry compatibility enforcement; versioned topics |
| **Cluster-wide controller/metadata issues** | Quorum loss | 3 or 5 controllers across AZs; monitoring |
| **Cross-region replication lag/duplication** | Async mirroring | Idempotent consumers; offset sync; clear RPO |
| **Security gaps** | Open listeners, no ACLs | TLS, SASL, ACLs, network policy |
| **Treating Kafka as a database** | Unbounded retention without compaction/tiering, ad-hoc queries | Use compaction/tiering deliberately; materialize views in proper stores |

---

## 13.16 Labs

### Lab 1: A mini Kafka-style commit log (Java 17+)
```java
import java.io.IOException;
import java.nio.ByteBuffer;
import java.nio.channels.FileChannel;
import java.nio.file.*;
import java.util.*;

/** A tiny Kafka-style partitioned commit log: append-only files, offsets, keyed partitioning, consumer groups with committed offsets. */
public class MiniLog {
    // ------------------------------------------------------------ one partition = one append-only file + offset->position index
    static class Partition implements AutoCloseable {
        final FileChannel ch; final ArrayList<Long> positions = new ArrayList<>(); long end = 0;
        Partition(Path p) throws IOException { ch = FileChannel.open(p, StandardOpenOption.CREATE, StandardOpenOption.READ, StandardOpenOption.WRITE, StandardOpenOption.TRUNCATE_EXISTING); }
        long append(byte[] key, byte[] val) throws IOException {                          // returns the record's OFFSET (0,1,2,...)
            ByteBuffer b = ByteBuffer.allocate(8 + key.length + val.length);
            b.putInt(key.length).putInt(val.length).put(key).put(val).flip();
            positions.add(end);
            while (b.hasRemaining()) end += ch.write(b, end);
            return positions.size() - 1;
        }
        void fsync() throws IOException { ch.force(false); }
        long endOffset() { return positions.size(); }
        List<String[]> read(long offset, int max) throws IOException {                    // sequential read starting at an offset
            List<String[]> out = new ArrayList<>();
            for (long o = offset; o < positions.size() && out.size() < max; o++) {
                ByteBuffer h = ByteBuffer.allocate(8); ch.read(h, positions.get((int) o)); h.flip();
                int kl = h.getInt(), vl = h.getInt(); ByteBuffer d = ByteBuffer.allocate(kl + vl); ch.read(d, positions.get((int) o) + 8); d.flip();
                byte[] k = new byte[kl], v = new byte[vl]; d.get(k).get(v); out.add(new String[]{new String(k), new String(v), String.valueOf(o)});
            }
            return out;
        }
        public void close() throws IOException { ch.close(); }
    }

    // ------------------------------------------------------------ topic = N partitions; producer picks partition by hash(key)
    static class Topic implements AutoCloseable {
        final Partition[] parts;
        Topic(Path dir, int n) throws IOException { Files.createDirectories(dir); parts = new Partition[n]; for (int i = 0; i < n; i++) parts[i] = new Partition(dir.resolve("p" + i + ".log")); }
        int partitionFor(String key) { return Math.floorMod(key.hashCode() * 0x9E3779B1, parts.length); }
        void send(String key, String value) throws IOException { parts[partitionFor(key)].append(key.getBytes(), value.getBytes()); }
        public void close() throws IOException { for (Partition p : parts) p.close(); }
    }

    // ------------------------------------------------------------ consumer group: committed offset per partition; partitions divided among members
    static class Group {
        final long[] committed; Group(int partitions) { committed = new long[partitions]; }
        static List<List<Integer>> assign(int partitions, int members) {
            List<List<Integer>> a = new ArrayList<>(); for (int m = 0; m < members; m++) a.add(new ArrayList<>());
            for (int p = 0; p < partitions; p++) a.get(p % members).add(p); return a;
        }
    }

    public static void main(String[] args) throws Exception {
        Path dir = Files.createTempDirectory("minilog");

        // ---- 1) per-key order is preserved inside a partition; there is NO global order across partitions
        try (Topic t = new Topic(dir.resolve("orders"), 4)) {
            int keys = 1000, perKey = 100; int[] nextSeq = new int[keys]; Random r = new Random(1);
            for (int i = 0; i < keys * perKey; i++) { int k = r.nextInt(keys); while (nextSeq[k] == perKey) k = (k + 1) % keys; t.send("user-" + k, "event-" + nextSeq[k]++); }
            int violations = 0, total = 0; Map<String, Integer> last = new HashMap<>(); long[] sizes = new long[4];
            for (int p = 0; p < 4; p++) { sizes[p] = t.parts[p].endOffset(); for (String[] rec : t.parts[p].read(0, Integer.MAX_VALUE)) { int seq = Integer.parseInt(rec[1].substring(6)); Integer prev = last.put(rec[0], seq); if (prev != null && seq != prev + 1) violations++; total++; } }
            System.out.println("== 1) Keyed partitioning: " + total + " events, 1000 keys, 4 partitions ==");
            System.out.println("records per partition: " + Arrays.toString(sizes) + "   per-key ordering violations: " + violations + "  (a key always maps to one partition)");

            // ---- 2) consumer groups, committed offsets, crash and replay
            System.out.println("\n== 2) Consumer group with committed offsets (3 members share 4 partitions) ==");
            Group g = new Group(4); List<List<Integer>> asg = Group.assign(4, 3);
            System.out.println("assignment with 3 members: " + asg + "   |  with 5 members: " + Group.assign(4, 5) + "  (members beyond partition count sit idle)");
            long processed = 0; int batch = 500;
            for (int p = 0; p < 4; p++) {                                              // each partition is consumed in batches; commit AFTER processing (at-least-once)
                List<String[]> all = t.parts[p].read(0, Integer.MAX_VALUE);
                int pos = (int) g.committed[p]; boolean crashed = false;
                while (pos < all.size()) {
                    int end = Math.min(all.size(), pos + batch);
                    processed += end - pos;                                            // (1) do the work for this batch
                    if (p == 2 && !crashed && end > all.size() / 2) {                  // consumer dies after the work but BEFORE committing the offset
                        crashed = true;
                        System.out.printf("partition 2: consumer crashes after processing up to offset %d, but the committed offset is only %d%n", end, g.committed[p]);
                        pos = (int) g.committed[p];                                    // a replacement consumer resumes from the COMMITTED offset
                        System.out.printf("             restart resumes at offset %d -> %d records are processed again%n", pos, end - pos);
                        continue;
                    }
                    g.committed[p] = end; pos = end;                                   // (2) commit the offset
                }
            }
            System.out.printf("records processed (incl. replays): %d  vs unique records: %d  -> duplicates: %d%n", processed, total, processed - total);

            // ---- 3) the log is not destroyed by reading: a new group replays from offset 0
            System.out.println("\n== 3) Replay: a brand-new consumer group reads partition 0 from offset 0 (the old group's progress is unaffected) ==");
            List<String[]> first = t.parts[0].read(0, 3);
            for (String[] rec : first) System.out.println("offset " + rec[2] + "  key=" + rec[0] + "  value=" + rec[1]);
            System.out.println("old group committed offset for partition 0 is still " + g.committed[0] + " of " + t.parts[0].endOffset());
        }

        // ---- 4) why sequential append + batched fsync is fast
        System.out.println("\n== 4) Durability cost: appending 100-byte records to ONE partition file ==");
        try (Partition p = new Partition(dir.resolve("bench.log"))) {
            byte[] key = "k".getBytes(), val = new byte[100]; int n = 1_000_000;
            long t0 = System.nanoTime(); for (int i = 0; i < n; i++) { p.append(key, val); if (i % 10_000 == 9_999) p.fsync(); } long t1 = System.nanoTime();
            System.out.printf("fsync every 10,000 records : %,9.0f records/s   (%.0f MB/s)%n", n / ((t1 - t0) / 1e9), n * 109.0 / 1e6 / ((t1 - t0) / 1e9));
        }
        try (Partition p = new Partition(dir.resolve("bench2.log"))) {
            byte[] key = "k".getBytes(), val = new byte[100]; int n = 3_000;
            long t0 = System.nanoTime(); for (int i = 0; i < n; i++) { p.append(key, val); p.fsync(); } long t1 = System.nanoTime();
            System.out.printf("fsync after EVERY record   : %,9.0f records/s%n", n / ((t1 - t0) / 1e9));
        }
    }
}
```
**Sample output**
```text
== 1) Keyed partitioning: 100000 events, 1000 keys, 4 partitions ==
records per partition: [24900, 24900, 25100, 25100]   per-key ordering violations: 0  (a key always maps to one partition)

== 2) Consumer group with committed offsets (3 members share 4 partitions) ==
assignment with 3 members: [[0, 3], [1], [2]]   |  with 5 members: [[0], [1], [2], [3], []]  (members beyond partition count sit idle)
partition 2: consumer crashes after processing up to offset 13000, but the committed offset is only 12500
             restart resumes at offset 12500 -> 500 records are processed again
records processed (incl. replays): 100500  vs unique records: 100000  -> duplicates: 500

== 3) Replay: a brand-new consumer group reads partition 0 from offset 0 (the old group's progress is unaffected) ==
offset 0  key=user-985  value=event-0
offset 1  key=user-978  value=event-0
offset 2  key=user-592  value=event-0
old group committed offset for partition 0 is still 24900 of 24900

== 4) Durability cost: appending 100-byte records to ONE partition file ==
fsync every 10,000 records :   999,557 records/s   (109 MB/s)
fsync after EVERY record   :     4,995 records/s
```
**Experiments**
1. Change the number of partitions to 8 and the number of keys to 10. How does the per-partition distribution change, and why do few keys limit useful parallelism?
2. Implement **segment rolling**: roll to a new file every 10,000 records, name segments by base offset, and keep a sparse index (one entry per 100 records). Implement `read(offset)` using binary search over segments and the sparse index, then scanning forward.
3. Implement **retention**: delete segments whose last record is older than T. What happens to a consumer whose committed offset is before the earliest remaining offset?
4. Implement **log compaction** over a segment set: keep the latest value per key and honor tombstones. Verify the stream→table fold.
5. Implement **consumer-group rebalancing** with member join/leave and compare eager (stop all) vs cooperative (move only affected partitions) by counting how many partitions pause.
6. Add **replication**: each partition has a leader file and one follower file; the follower copies from the leader’s offset in a background thread. Add `acks=all` semantics (wait for follower), then kill the “leader” and promote the follower. Count lost records with `acks=1` vs `acks=all`. (This reproduces Ch 9’s semi-sync lesson in log form.)
7. Add an **idempotent producer** with a producer ID and sequence numbers; inject retries after “lost acks” and show duplicates with and without idempotence.

### Lab 2: Real Kafka (needs Docker; not executed in the book’s sandbox)
```bash
# single-node KRaft broker (Kafka 3.7+/4.x image; adjust to what you have)
docker run -d --name kafka -p 9092:9092 apache/kafka:latest
docker exec -it kafka bash -lc '
  /opt/kafka/bin/kafka-topics.sh --bootstrap-server localhost:9092 --create --topic orders --partitions 4 --replication-factor 1
  /opt/kafka/bin/kafka-topics.sh --bootstrap-server localhost:9092 --describe --topic orders
  # produce keyed messages (key:value)
  /opt/kafka/bin/kafka-console-producer.sh --bootstrap-server localhost:9092 --topic orders --property parse.key=true --property key.separator=:
  # in two other terminals start two consumers in the same group and observe partition split:
  /opt/kafka/bin/kafka-console-consumer.sh --bootstrap-server localhost:9092 --topic orders --group g1 --property print.key=true --property print.partition=true --property print.offset=true
  /opt/kafka/bin/kafka-consumer-groups.sh --bootstrap-server localhost:9092 --describe --group g1     # partitions, offsets, LAG
'
```
**Experiments:** (1) send keys `a:1 b:1 a:2 b:2 a:3` and verify same-key messages land on the same partition in order; (2) stop one consumer and watch the rebalance and reassignment; (3) start a consumer in a *new* group with `--from-beginning` to replay; (4) create a compacted topic (`--config cleanup.policy=compact --config min.cleanable.dirty.ratio=0.01 --config segment.ms=1000`), write repeated keys, wait, and read from the beginning; (5) with a 3-broker docker-compose cluster, create a topic with RF=3, `min.insync.replicas=2`, produce with `acks=all`, kill a broker and then two, and record the exact errors; (6) use `kafka-producer-perf-test.sh` with different `linger.ms`, `batch.size`, and compression, and plot throughput.

### Lab 3: Capacity plan (paper)
Plan a cluster for 120 MB/s peak ingress (compressed), 14-day retention, RF=3, 6 consumer groups, one critical topic needing strict per-key ordering for 20 M active keys, and a requirement to survive an AZ loss. Compute disk, network, partitions, brokers; specify `acks`, `min.insync.replicas`, rack awareness, monitoring thresholds, and consumer group sizing.

---

## 13.17 Interview perspective

- Use Kafka when you need **durable, replayable, high-throughput, ordered-per-key event streams with many independent consumers**. Say *why not a plain queue*: replay, multiple consumers, throughput, retention.
- Know the core model cold: **topics → partitions → offsets**, **consumer groups**, **ordering per partition only**, **key → partition**, **parallelism ≤ partitions**.
- Durability sentence: **“RF=3, `acks=all`, `min.insync.replicas=2`, idempotent producer, unclean leader election disabled.”** Explain what that tolerates.
- Delivery semantics: commit after processing = at-least-once; make consumers idempotent; Kafka EOS covers Kafka-to-Kafka only.
- Mention **lag monitoring**, **rebalancing**, **DLQ/retry topics**, **schema registry**, **compaction vs retention**, **outbox/CDC** for DB integration.
- Provide **numbers**: ingress, retention × RF for disk, partitions from throughput, brokers across AZs.
- Be ready to discuss hot partitions, reprocessing/backfills, and multi-region replication trade-offs.

---

## 13.18 Common misconceptions

1. **“Kafka is just a faster queue.”** It is a retained, replayable log with consumer-owned offsets.
2. **“Kafka guarantees ordering.”** Only within a partition.
3. **“More consumers = more throughput.”** Only up to the partition count.
4. **“Kafka provides exactly-once, so I don’t need idempotency.”** Only Kafka-to-Kafka; external effects need idempotence.
5. **“`acks=all` means no data loss.”** Not without `min.insync.replicas ≥ 2` and no unclean elections.
6. **“Kafka stores messages in memory.”** It writes to disk (sequential) and uses the OS page cache for speed.
7. **“Increasing partitions is harmless.”** It remaps keys and breaks ordering/state assumptions.
8. **“Consumers are pushed messages.”** They pull.
9. **“Offsets are per topic.”** Per (group, topic, partition).
10. **“Compaction deletes old data immediately.”** It is asynchronous and keeps at least the latest per key.
11. **“Auto-commit is safe.”** It can lose or duplicate on crashes.
12. **“Kafka is a database.”** It can store data durably and compacted, but lacks ad-hoc querying and general transactions across arbitrary data.

---

## 13.19 Chapter summary

- The **log** (append-only, ordered, immutable, consumer-owned offsets) unifies integration, replay, and history. It is fast because of **sequential I/O, page cache, batching, zero-copy, compression, and replica-based durability** (lab: 1.0 M rec/s with batched fsync vs 5 k rec/s fsync-per-record, a 200× gap).
- **Architecture:** brokers, topics, **partitions** (unit of parallelism/ordering/replication), segments with sparse indexes, controller (KRaft), producers, consumer groups.
- **Partitions:** key → partition preserves per-key order (0 violations in the lab), cap consumer parallelism (5 consumers on 4 partitions → one idle), and are hard to change; size by throughput, consumer capacity, and growth.
- **Replication:** leader + followers, **ISR**, **high watermark**; **RF=3, `acks=all`, `min.insync.replicas=2`** survives one broker failure with no loss and continued availability; **unclean election** trades loss for availability.
- **Producers:** batching (`linger.ms`, `batch.size`), compression, **idempotent producer** (PID + sequence numbers). **Consumers:** pull, commit **after** processing for at-least-once (lab: 500 reprocessed records after a crash), groups, **rebalancing** hazards, **lag** as the key metric, replay from any offset.
- **Retention vs compaction**, stream–table duality; **EOS** via idempotence + transactions (Kafka-to-Kafka only).
- **Stream processing:** state stores with changelogs, windows, event time and watermarks, joins; Lambda vs Kappa.
- **Schemas:** registry + compatibility rules; dead-letter topics for poison pills.
- **Operate:** size disk = W×T×RF, network = W×(RF−1+G); monitor under-replicated partitions, ISR churn, lag; rolling restarts; partition reassignment.

### Mental models
| Concept | Mental model |
|---|---|
| Log | A ledger you only append to, with page numbers (offsets) |
| Consumer offset | A bookmark that each reader keeps in their own copy of the page numbering |
| Partition | One lane of a multi-lane highway; order holds within a lane, not across lanes |
| Consumer group | A team splitting the lanes among its members |
| ISR | The set of scribes who are fully caught up; only they can become head scribe |
| High watermark | The page number everyone in the ISR has copied; readers only see up to it |
| Compaction | Keeping only the latest version of each document in the archive |
| Rebalance | Reassigning lanes when team members come and go |

---

## 13.20 Exercises

### A. Conceptual
1. Contrast a log and a queue on consumption, replay, multiple consumers, ordering, and typical throughput. Give one use case where each is the better fit.
2. Explain five reasons sequential-log systems like Kafka achieve high throughput.
3. Define topic, partition, offset, segment, broker, and consumer group. Draw how they relate.
4. How does a broker find the record at offset N? Why are retention deletions cheap?
5. How does the default partitioner assign records? What guarantees result and what do not?
6. List the factors for choosing the number of partitions and why it’s hard to change later.
7. Define ISR and high watermark. Why do consumers only see records up to the high watermark?
8. Compare `acks=0/1/all`. What additional setting makes `acks=all` meaningful, and why?
9. RF=3, `min.insync.replicas=2`, `acks=all`: describe behavior with 0, 1, 2, and 3 brokers down for a partition. When do writes fail? Is data ever lost?
10. What is unclean leader election and when would you enable it?
11. How does the idempotent producer prevent duplicates and reordering? What is its scope?
12. Explain consumer offset commit strategies and the delivery semantics each yields.
13. What triggers a consumer group rebalance? Explain eager vs cooperative rebalancing and static membership.
14. Why can a slow handler cause a rebalance storm? List three mitigations.
15. What is consumer lag, how do you compute it, and what is “time lag”?
16. When would you use log compaction rather than time-based retention? Explain tombstones.
17. What does Kafka’s exactly-once guarantee cover? Give an example where it does not help.
18. Explain event time, processing time, windows, and watermarks. Why do late events matter?
19. Explain how state stores and changelog topics make stateful stream processing fault tolerant.
20. What are schema compatibility modes (backward/forward/full) and which consumers/producers upgrade first in each?
21. Describe Lambda vs Kappa architectures.
22. List five situations where Kafka is the wrong choice.
23. How do you reprocess three days of data after fixing a consumer bug?

### B. Calculations
24. Ingress 80 MB/s (compressed), retention 10 days, RF=3. Compute disk with 30% headroom. How many brokers if each has 16 TB usable disk, and what is the per-broker network egress if 5 consumer groups read the full stream across 12 brokers?
25. Target 300 MB/s on a topic. Per-partition producer throughput is 12 MB/s; per-consumer processing 6 MB/s. Growth 2.5×. How many partitions? What is the maximum consumer group size?
26. Lag is 2.4 M messages. Producers add 15,000 msg/s; consumers process 21,000 msg/s. When does lag clear? What if a rebalance pauses consumption for 40 s?
27. A producer sends 2 KB records at 80,000 rec/s with `batch.size` = 128 KB and `linger.ms` = 5. About how many records per batch (assuming the batch fills before the timer) and how many requests/s per partition if there are 20 partitions evenly used?
28. `retention.ms` = 3 days; a consumer is down for 4 days. What happens when it restarts with `auto.offset.reset=latest` vs `earliest` vs no default? What alert would have caught this?
29. A compacted topic has 50 M keys; average value 800 B; key 40 B; header/overhead 60 B per record. Estimate steady-state size. If the cleaner lags and the dirty ratio is 50%, how big is the topic?
30. Transactional produce adds ~10 ms per transaction commit; you commit every 100 ms. What fraction of time is spent committing, and how does throughput change if you commit every 500 ms? What is the cost in end-to-end latency?
31. 6 brokers, RF=3, 600 partitions: how many replicas per broker on average? How many partitions does each broker lead? What happens to the leader count on each survivor when one broker fails (ideal case)?
32. Network: 60 MB/s ingress, RF=3, 4 consumer groups. Compute total bytes in/out for the cluster and the load on a 10 Gbit NIC per broker if spread over 8 brokers.

### C. Design
33. Design the event backbone for ShopStream: topics, keys, partition counts, retention/compaction, schemas, consumer groups, DLQ/retry topics.
34. Design a clickstream pipeline (500 k events/s) from web/mobile to Kafka to real-time dashboards and a data lake, including late events and replay.
35. Design an order-processing pipeline using Kafka transactions for consume–process–produce and discuss where idempotency is still required.
36. Design multi-region active–passive replication of Kafka topics with RPO < 60 s: tool choice, offset translation, consumer failover, failback.
37. Design the retry/DLQ topic topology for a payment-notification consumer that calls a flaky partner API.
38. Design a CDC pipeline from a sharded Postgres to Kafka to search and cache, handling schema changes and ordering per row.

### D. Debugging
39. Consumer lag keeps growing on one partition while others are fine. List four causes and checks.
40. After a broker restart, producers get `NotEnoughReplicasException`. Explain and fix.
41. A consumer group rebalances every few minutes and throughput collapses. Diagnose and list fixes.
42. After increasing partitions from 12 to 24, a stream-processing job’s per-key state looks wrong. Explain.
43. Duplicated records appear downstream even though the producer uses `acks=all`. List possible causes.
44. A topic with `cleanup.policy=compact` still shows several records for a key. Why?

### E. Interview-style
45. “Design a system for real-time fraud detection on payments using Kafka.”
46. “Kafka vs RabbitMQ vs SQS: how do you choose?”
47. “How does Kafka achieve durability and what happens when a broker fails?”

### F. Teach-back
48. Explain consumer groups and partitions to a new teammate using a highway and toll-booth analogy.

---

## 13.21 Full answer key

**1.** Log: records retained, consumers keep offsets, replay and many independent readers, per-partition order, very high throughput (event backbone, CDC, analytics). Queue: records deleted on ack, broker tracks per-message state, per-message retry/delay/priority, moderate throughput (task distribution like image resizing, emails).

**2.** Sequential appends/reads; OS page cache serving recent data; batching of produce/fetch/disk writes; zero-copy `sendfile`; batch compression kept end to end; durability via replication rather than per-record fsync.

**3.** Topic: named stream. Partition: ordered log shard of a topic. Offset: sequential record number within a partition. Segment: file chunk of a partition. Broker: server storing partitions. Consumer group: consumers sharing a `group.id` that divide partitions, each with committed offsets. Relationship: topic → partitions → segments → records (offsets); partitions are replicated across brokers; groups assign partitions to consumers.

**4.** Binary-search the segment base offsets, then the sparse offset index to the nearest file position ≤ N, then scan forward. Retention deletes whole segment files, no per-record work.

**5.** `hash(key) mod P` (murmur2); null keys spread by sticky/round-robin. Guarantees: same key → same partition → per-key order (while P unchanged). No ordering across partitions or keys.

**6.** Throughput per partition, consumer processing rate and desired parallelism, growth, per-partition overhead (handles, replication, election time), key skew. Hard to change because increasing changes key→partition mapping (breaking order and keyed state) and decreasing is unsupported.

**7.** ISR: replicas (including leader) caught up within the lag limit. High watermark: highest offset replicated to all ISR members. Consumers see only committed records (≤ HW) so that data visible to them cannot disappear on leader failover.

**8.** `acks=0`: no wait (can lose); `1`: leader only (loss if leader dies before replication); `all`: all ISR have it. `min.insync.replicas` ensures the ISR is big enough; otherwise `acks=all` could succeed with only the leader.

**9.** 0 down: normal. 1 down: ISR=2 ≥ min.isr, writes OK, no loss. 2 down: ISR=1 < 2 → writes rejected (`NotEnoughReplicas`), reads continue; no acknowledged data lost. 3 down: partition offline (no reads/writes) until an ISR member returns; no data loss unless unclean election/ disk loss.

**10.** Allows an out-of-sync replica to become leader when no ISR member is available, restoring availability at the cost of losing records it lacked and possibly rewinding offsets. Enable only for data where availability matters more than completeness (metrics/logs).

**11.** Broker-assigned producer ID + per-partition sequence numbers per batch; the leader rejects duplicates and out-of-order sequences, so retries don’t duplicate or reorder. Scope: per partition, per producer session (PID), writes into Kafka only.

**12.** Commit before processing → at-most-once; commit after processing → at-least-once; commit offsets and outputs atomically (transaction/same DB transaction) → exactly-once effect. Auto-commit is unpredictable.

**13.** Triggers: member join/leave/crash, missed heartbeats, exceeding `max.poll.interval.ms`, subscription or partition count changes. Eager: everyone revokes all partitions then reassigns (stop-the-world). Cooperative: only moved partitions are revoked. Static membership: restarting instance keeps its assignment without a rebalance within the session timeout.

**14.** If processing between polls exceeds `max.poll.interval.ms`, the consumer is evicted and a rebalance starts; partitions move, work is repeated, other consumers slow down, causing more evictions. Mitigate: reduce `max.poll.records`, increase the interval, process faster/async with care, cooperative assignor, static membership, fix GC.

**15.** Lag = log end offset − committed offset per partition. Time lag = how old the oldest unprocessed record is (or lag ÷ consumption rate). Time lag expresses user impact.

**16.** Compaction for changelog/table semantics (latest value per key, reference data, state restoration); retention for event history. Tombstone = key with null value, meaning delete; retained for `delete.retention.ms` then removed.

**17.** Covers consume–process–produce within Kafka (idempotent producer + transactions + atomic offset commit). Doesn’t cover external side effects such as sending an email or HTTP call, which can still repeat; use idempotent effects.

**18.** Event time is when it occurred; processing time is when processed; windows aggregate over time ranges; watermarks estimate completeness. Late events arrive after their window closed and need allowed lateness/side outputs/updates or are dropped.

**19.** State is stored locally (RocksDB) per task; every update is also written to a changelog topic. After failure, a new task rebuilds the state by replaying the changelog (or restoring a snapshot), then continues from the committed offset.

**20.** Backward: new schema reads old data → upgrade consumers first. Forward: old consumers read new data → upgrade producers first. Full: both directions; any upgrade order. Transitive variants check against all previous versions.

**21.** Lambda: parallel batch (accurate) and speed (real-time) layers merged at serving; duplicated logic. Kappa: a single streaming pipeline; reprocessing by replaying the log through new code.

**22.** Simple task queues with per-message delay/priority; small scale; RPC/request-response; very large payloads; teams unable to operate or buy managed streaming; ad-hoc query workloads (use a database).

**23.** Fix and deploy the consumer; either reset the group’s offsets to 3 days ago (timestamp) or start a new group from that point; ensure idempotent writes or clear/rebuild the derived store; monitor lag while catching up; retention must cover 3 days.

**24.** Per copy: 80 MB/s × 864,000 s = 69.1 TB; × RF 3 = 207 TB; +30% = **269 TB**. Brokers: 269/16 ≈ 16.8 → **17** (plus spare). Egress: consumers 80 × 5 = 400 MB/s plus replication 160 MB/s = 560 MB/s total; per broker (12): ≈ **47 MB/s** (~0.37 Gbit/s).

**25.** By producer: 300/12 = 25; by consumer: 300/6 = 50; max = 50; × 2.5 growth = 125 → choose **128** (or 120/144). Max group size: 128 active consumers.

**26.** Net drain = 21,000 − 15,000 = 6,000/s → 2.4 M / 6,000 = **400 s**. A 40 s consumption pause means lag grows by 40 × 15,000 = 600,000 first (to 3.0 M), then drains at 6,000/s for 500 s: **540 s** total.

**27.** A full batch holds 128 KB / 2 KB = **64 records**. Each of the 20 partitions receives 80,000 / 20 = 4,000 rec/s, so a 64-record batch would take 16 ms to fill, longer than `linger.ms` = 5 ms. The linger timer therefore fires first: batches carry about 4,000 × 0.005 = **20 records** and each partition sends ≈ **200 batches/s** (4,000 batches/s in total, though a single request to a broker can carry batches for several partitions). To get full 64-record batches, raise `linger.ms` to ~16–20 ms (accepting that much extra latency) or reduce the partition count.

**28.** Data older than 3 days is deleted; the committed offset is before the earliest offset → `OffsetOutOfRange`. `auto.offset.reset=latest` skips to the end (loses 4 days unprocessed silently), `earliest` jumps to the oldest remaining (still loses day 1), `none` throws an error. Alert: lag/time-lag approaching retention, consumer-down alerts.

**29.** Record ≈ 40 + 800 + 60 = 900 B × 50 M = **45 GB**. With dirty ratio 50%, the log holds up to ~2× live data ≈ **90 GB** until cleaned.

**30.** 10 ms every 100 ms = 10% overhead; every 500 ms = 2%. Throughput improves with longer intervals; end-to-end latency for read_committed consumers rises to roughly the commit interval (+ commit time), so ~500 ms added visibility delay.

**31.** Total replicas = 600 × 3 = 1,800 → **300 per broker**; leaders = 600 / 6 = **100 per broker**. If one fails, its 100 leaders are redistributed to survivors: each gets ~20 more (100/5), so **120** leaders each (ideal).

**32.** Ingress 60 MB/s; replication 60 × 2 = 120 MB/s; consumer egress 60 × 4 = 240 MB/s. Cluster network total ≈ 420 MB/s. Per broker (8): ≈ 52.5 MB/s ≈ 0.42 Gbit/s, about 4% of a 10 Gbit NIC; it’s not the constraint (disks/partition counts are).

**33 (sketch).** Topics by domain event: `orders.v1` (key order_id, 48 partitions, 7-day retention), `payments.v1`, `inventory.v1` (key sku), `customers.v1` (compacted, key customer_id), `clickstream.v1` (key session_id, high partitions, 3 days). Avro + registry (backward compatible). Groups: fraud, search-indexer, warehouse-loader, email. Retry/DLQ topics per consumer. CDC via Debezium from DBs with outbox.

**34 (sketch).** Edge collectors → Kafka (key by session/user, 200+ partitions, lz4); Flink job with event-time windows (1-min tumbling, 5-min allowed lateness) → dashboard store; Kafka Connect sink to object storage (Parquet, partitioned by hour); replay by resetting offsets; handle late events via side outputs/updates.

**35 (sketch).** Read orders, process, write results and offsets in a transaction with `transactional.id` per consumer instance; `read_committed` downstream; external calls (payment capture) still need idempotency keys; zombie fencing through epochs.

**36 (sketch).** MirrorMaker 2 (or cluster linking) active→passive, consumer group offset sync, monitored replication lag (< 60 s), failover runbook switching bootstrap servers and group offsets, idempotent consumers to tolerate replays, failback by reversing replication after reconciliation.

**37 (sketch).** Main topic → consumer; on transient failure publish to `retry-1m`, then `retry-10m`, `retry-1h` consumers that wait until the record’s eligible time; after max attempts → `dlq`; idempotency key per notification; separate topic keeps the main partition flowing; alert on DLQ.

**38 (sketch).** Debezium per shard → topic per table keyed by primary key (ordering per row); schema registry with compatibility; consumers upsert into search/cache idempotently using row version; handle DDL via schema evolution; initial snapshot then streaming; monitor connector lag.

**39.** Hot key/partition skew; slow poison message in that partition; consumer instance with GC/IO issues assigned to it; downstream dependency slow for that key range. Checks: per-partition throughput, handler logs, consumer metrics, key distribution.

**40.** Brokers restarting dropped the ISR below `min.insync.replicas` (2) with RF=3 and `acks=all`; writes are rejected until replicas rejoin. Fix: restart one broker at a time and wait for ISR recovery; ensure rack-aware replica placement; adequate RF.

**41.** Handlers exceed `max.poll.interval.ms`, GC pauses, or unstable pods cause evictions. Fix: lower `max.poll.records`, raise the interval, speed up processing, cooperative-sticky assignor, static membership, fix GC/resources.

**42.** The key→partition mapping changed (hash mod 24), so existing keys now route to different partitions/tasks, but their state lives in old partitions/changelogs; per-key order broke across the change. Fix: create a new topic with 24 partitions and migrate/reprocess, or plan partition counts ahead.

**43.** Producer retries without idempotence after ambiguous failures; consumer reprocessing after rebalance or crash before commit; replay/reset offsets; upstream publishes duplicates; multiple instances; mirrored topics. Fix with idempotent producer and idempotent consumers.

**44.** Compaction is asynchronous and doesn’t touch the active segment; `min.cleanable.dirty.ratio`/timing control when cleaning occurs; older duplicates remain until the cleaner runs.

**45 (sketch).** Payments → `payments` topic keyed by account; stream processor (Flink/Streams) with state per account (velocity features, windows), joins with a compacted `customer-risk` table; scores to `decisions` topic consumed by payment service; latency budget < 100 ms for synchronous path (or async hold); replay for model updates; exactly-once Kafka-to-Kafka plus idempotent decisions; DLQ; monitoring lag.

**46 (sketch).** Kafka: streams/high throughput/replay/multiple consumers; RabbitMQ: complex routing, per-message ack/priority/TTL, moderate throughput; SQS: simplest managed work queue/fan-out with SNS. Choose by semantics (replay, ordering), throughput, ops capacity.

**47 (sketch).** Replicated partitions, ISR, `acks=all`, `min.isr=2`, HW, leader election from ISR, controller, rack awareness; broker failure → leader election in seconds, no committed data lost; producers retry; under-replicated alert; follower catch-up.

**48.** A highway with P lanes (partitions): cars (records) in a lane never pass each other. A toll team (consumer group) assigns each lane to one booth (consumer): more booths than lanes means some sit idle. If a booth closes, its lane is handed to another (rebalance). Each company (group) keeps its own tally of the last car it processed (offset).

---

## 13.22 Flashcards

| Front | Back |
|---|---|
| Log | Append-only, ordered, immutable; offsets; consumer-owned position |
| Why fast | Sequential I/O, page cache, batching, zero-copy, compression, replication-based durability |
| Batched fsync lab | ~1.0 M rec/s vs ~5 k rec/s per-record fsync (≈200×) |
| Partition | Unit of parallelism, ordering, replication |
| Ordering | Only within a partition; key → partition |
| Max consumer parallelism | Number of partitions (extra consumers idle) |
| ISR / HW | In-sync replicas / highest offset on all ISR (consumer-visible) |
| Durable config | RF=3, acks=all, min.isr=2, unclean election off |
| `acks=1` risk | Loss if leader dies before replication |
| Idempotent producer | PID + sequence numbers → no duplicates/reorder per partition |
| Commit after processing | At-least-once (lab: 500 replayed) |
| Consumer group | Divide partitions; groups independent |
| Rebalance | Triggered by join/leave/timeouts; cooperative reduces pauses |
| `max.poll.interval.ms` | Max gap between polls before eviction |
| Lag | End offset − committed offset |
| Retention vs compaction | Time/size deletion vs latest-per-key |
| Tombstone | Null value = delete key |
| Kafka EOS | Kafka-to-Kafka only (idempotence + transactions) |
| Stream processing state | RocksDB + changelog topic |
| Schema compatibility | Backward: consumers first; forward: producers first |
| Disk sizing | W × T × RF × 1.3 |
| Under-replicated partitions | Must be 0; investigate immediately |

## 13.23 Glossary additions

| Term | Simple Meaning | Technical Meaning | Why It Exists | Example |
|---|---|---|---|---|
| Commit log | Append-only record list | Ordered immutable sequence with offsets | Durable event history | Kafka partition |
| Offset | Record number | Sequential position in a partition | Positioning/replay | 12,500 |
| Topic | Named stream | Logical category of records | Organization | `orders` |
| Partition | Lane of a topic | Independent ordered log shard | Parallelism, scale | `orders-3` |
| Broker | Kafka server | Process storing partitions | Storage/serving | broker-2 |
| Segment | Log file chunk | Partition file rolled by size/time | Cheap retention | `0000…log` |
| Leader / follower | Primary / copy | Replica roles per partition | Replication | L@b1 |
| ISR | Caught-up set | In-sync replicas eligible to lead | Safe failover | {1,2} |
| High watermark | Committed point | Highest offset on all ISR | Consistent reads | HW=1,023 |
| `acks` | Write confirmation level | Replication level required before ack | Durability tuning | `all` |
| `min.insync.replicas` | Min safe copies | Minimum ISR size to accept `acks=all` | Prevent silent downgrade | 2 |
| Unclean election | Promote a laggard | Out-of-sync replica becomes leader | Availability over loss | Off by default |
| Idempotent producer | No duplicate writes | PID + sequence dedupe | Safe retries | Default on |
| Consumer group | Team of readers | Consumers sharing partitions | Scale-out reads | `fraud` |
| Rebalance | Reassign partitions | Group membership change processing | Elastic consumers | Pod restart |
| Lag | How far behind | End offset − committed offset | Health metric | 600k |
| Compaction | Keep latest per key | Cleaner removes older same-key records | Table semantics | `customers` |
| Tombstone | Delete marker | Null-valued record for a key | Deletion in logs | `(k, null)` |
| Transaction (Kafka) | Atomic multi-write | Atomic produce + offset commit | EOS | `transactional.id` |
| State store | Local stream state | RocksDB with changelog | Stateful processing | Count by key |
| Watermark | Completeness estimate | Event-time progress marker | Window closing | T−5 min |
| Schema registry | Contract store | Versioned schemas + compatibility checks | Safe evolution | Avro subject |
| CDC | DB changes as events | Read DB log and publish row changes | Integration | Debezium |
| MirrorMaker | Cluster copier | Cross-cluster topic replication | DR/multi-region | MM2 |

---

**Batch 3 complete.** Part II now covers Ch 6–13 (scaling, load balancing, caching, replication, sharding, CDN/object storage, queues, Kafka).

**Next: Batch 4 (Part III, distributed systems core): Chapters 14–17.** Ch 14 Distributed Systems Fundamentals (partial failure, networks, clocks, FLP/two generals, failure detectors), Ch 15 Consistency Models and CAP/PACELC, Ch 16 Consensus, Leader Election and Coordination (Raft in depth with a runnable simulation), Ch 17 Distributed Transactions, Sagas, Event Sourcing and CQRS.

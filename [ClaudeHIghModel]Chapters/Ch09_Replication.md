# Chapter 9: Replication

## Chapter card

| | |
|---|---|
| **Objective** | Understand why and how data is copied across machines: single-leader, multi-leader, and leaderless replication; synchronous vs asynchronous trade-offs; replication lag and the user-visible anomalies it causes; failover, split-brain, and fencing; conflict handling; quorums; and how real databases replicate |
| **Why this chapter exists** | Replication is the mechanism behind high availability, read scaling, disaster recovery, and geographic latency. It is also where consistency gets complicated. Everything in Chapters 10 (sharding), 14–17 (distributed systems, CAP, consensus, transactions) builds on it |
| **Prerequisites** | Chapters 3 (WAL, transactions), 4 (storage engines, outbox/CDC), 5 (percentiles), 6 (scaling ladder), 8 (caching, staleness) |
| **Study time** | 11–14 hours including labs |
| **Tier** | `[MK]` single-leader, lag anomalies, failover, quorums; `[GK]` multi-leader conflict resolution, anti-entropy; `[ADV]` consensus-based replication (previewed; full in Ch 16) |
| **Mastery criteria** | (1) Explain why replication is used and what it does *not* solve. (2) Describe how a leader ships changes (statement, WAL, logical) and how a new follower joins. (3) Compare sync, async, and semi-sync and quantify durability vs latency. (4) Walk through failover and name its hazards (lost writes, split-brain, wrong promotion) and mitigations (fencing). (5) Identify read-your-writes, monotonic-reads, and consistent-prefix violations and design fixes. (6) Explain quorum math (N, W, R), sloppy quorums, hinted handoff, read repair, anti-entropy. (7) Choose between single-leader, multi-leader, and leaderless for a scenario. (8) Distinguish replicas from backups |

---

## 9.1 The Story: The Sunday Disk Failure and the Missing Comment

TicketRush (Chapter 3) runs on one PostgreSQL primary. It grows, and three things happen in one quarter:

1. **Read overload.** Event pages and "my tickets" views generate 90% of queries; the primary is at 85% CPU, mostly reading.
2. **The Sunday disk failure.** A disk controller dies at 02:10 on Sunday. The team restores from last night's backup: the site is down 3 hours and **everything since 23:00 is gone**, including ~14,000 purchases customers have confirmation emails for.
3. **A European launch.** European users see 180 ms round trips to the US database; every page makes 6 queries.

The team sets up two **read replicas** that continuously copy the primary's changes. Problems:

- A user buys a ticket, is redirected to "My Tickets"… and the ticket isn't there. (They read from a replica that hasn't received the write yet.)
- A user refreshes a page and sees *more* comments than a moment ago, then *fewer* on the next refresh. (They hit two different replicas at different lag.)
- A month later, the primary's host fails. The replica is promoted, but 22 acknowledged purchases are missing, and for 40 seconds *both* machines accepted writes.

### Pause & Predict 9.1
> For each of the three problems, write the *name* of the underlying phenomenon (you may not know the formal term yet) and one idea for a fix. Then watch for the formal names: read-your-writes, monotonic reads, lost acknowledged writes, split-brain.

---

## 9.2 Why replicate, and what replication does *not* solve

### 9.2.1 The reasons `[MK]`
| Goal | How replication helps |
|---|---|
| **High availability** | If one machine fails, another has the data and can take over (failover) |
| **Read scalability** | Spread read queries over many copies (Ch 6 scaling ladder, rung 4) |
| **Latency** | Place copies near users (geo-replication); reads become local |
| **Durability** | Survive loss of a disk/host/AZ/region; the data exists in multiple places |
| **Offloading** | Run analytics, backups, and reports on a replica without slowing the primary |
| **Maintenance** | Upgrade or migrate with a replica takeover and minimal downtime |
| **Disconnected operation** | Offline-capable clients keep local copies that sync later (multi-leader-ish) |

### 9.2.2 What replication does *not* give you `[MK]`
- **Write scaling.** In single-leader systems every write still goes to one machine, and every replica must *apply* every write; replicas add read capacity, not write capacity (Ch 10 sharding addresses writes).
- **Data size scaling.** Each replica stores a full copy; replication does not let you store more than one machine can hold.
- **Protection from logical errors.** A bad `DELETE` or a buggy deploy is replicated faithfully, instantly. **Replicas are not backups** (§9.12).
- **Free consistency.** Copies diverge in time; you must decide what staleness is acceptable (the rest of the chapter).

### 9.2.3 The fundamental difficulty
If data never changed, replication would be trivial: copy it. The entire difficulty is **keeping copies in sync as data changes, while machines and networks fail**. Three families of approaches exist:
1. **Single-leader** (primary–replica, master–slave): one node accepts writes; others follow.
2. **Multi-leader**: several nodes accept writes and exchange them.
3. **Leaderless**: any node accepts writes; clients/coordinators use quorums to read and write.
(A fourth, **consensus-based replication**, is single-leader with strongly agreed logs; Chapter 16.)

---

## 9.3 Single-leader replication

### 9.3.1 The idea `[MK]`
```text
                writes                      replication stream (ordered log of changes)
 Clients ───────────────▶ [ LEADER / PRIMARY ] ─────────────┬─────────────▶ [ FOLLOWER 1 ]
                                  │                         ├─────────────▶ [ FOLLOWER 2 ]
 Clients (reads) ◀────────────────┴───── reads can go to ───┴─────────────▶ [ FOLLOWER 3 ]
```
- Exactly **one leader** accepts writes; this gives a single, total order of changes (no conflicts to resolve).
- The leader records each change in an **ordered replication log** and ships it to **followers**, which apply the changes **in the same order**.
- Reads can be served by the leader or by followers (with the staleness caveats of §9.5).
- Used by PostgreSQL, MySQL, Oracle, SQL Server, MongoDB (replica sets), Redis (replicaof), Kafka (per partition), and many more.

**Mental model:** an author (leader) dictates a book to several scribes (followers) who copy every sentence in order. Readers can read from any scribe's copy, but a scribe may be a few sentences behind.

### 9.3.2 What exactly is shipped? (replication log formats) `[MK/GK]`
| Method | Description | Pros | Cons |
|---|---|---|---|
| **Statement-based** | Ship the SQL statements (`UPDATE ... SET x = NOW()`) | Compact | Non-deterministic functions (`NOW()`, `RAND()`), auto-increment ordering, side effects diverge replicas; mostly abandoned or restricted |
| **Write-ahead log (physical/WAL) shipping** | Ship the storage engine's low-level WAL (byte changes to pages) | Exact copy; simple and fast; (Postgres streaming replication) | Tied to storage format and version (leader and follower must run compatible versions); can't replicate to a different engine or partial tables |
| **Logical (row-based) log** | Ship row-level changes: "table T row key K: old → new values" | Decoupled from storage format; works across versions/engines; usable for CDC, selective replication | Larger for bulk statements; needs primary keys for rows |
| **Trigger/application-level** | Application or triggers record changes | Flexible (custom conflict logic) | Overhead, more bugs |
Modern systems usually use **physical** replication for high-fidelity HA replicas and **logical** replication for upgrades, migrations, CDC, and feeding other systems (Ch 4's outbox/CDC).

### 9.3.3 Synchronous vs asynchronous replication `[MK]`
When does the leader tell the client "committed"?
```text
 ASYNC:      client ─write▶ leader ─commit┐                     (leader acks immediately)
                                           └─ack▶ client
                                  leader ─ship▶ follower        (happens later; may lag or be lost)

 SYNC:       client ─write▶ leader ─ship▶ follower ─ack─▶ leader ─commit/ack▶ client

 SEMI-SYNC:  as SYNC for ONE designated follower (others async); if it's slow/dead, another takes over the role
```
| Mode | Write latency | Durability on leader loss | Availability impact |
|---|---|---|---|
| **Async** | Lowest (local commit only) | **Can lose acknowledged writes** (those not yet shipped) | Leader doesn't depend on followers |
| **Sync (all followers)** | Highest (slowest follower) | No acknowledged write lost | **One slow/dead follower blocks writes** (bad) |
| **Semi-sync (1 sync follower)** | + one network round trip to nearest follower | No loss if the sync follower survives and is promoted | Tolerates one failure; if the sync follower dies, promote another to sync |
| **Quorum/majority (consensus)** | + round trip to majority | No loss while majority survives | Tolerates minority failure; the basis of Raft/Paxos (Ch 16) |

**Key insight:** *asynchronous replication trades durability for latency and availability.* Most production relational deployments use async or semi-sync because fully sync across all replicas is fragile.

### 9.3.4 Measured: how much does async lose on failover? (Lab 1, part 3)
Simulation: 1,000 acknowledged writes/second, two replicas whose lag is usually ~40 ms but occasionally 1–5 s (1% of writes: GC pauses, slow disk, large transaction). The primary crashes; we promote the **most up-to-date** replica.
```text
async replication                                writes LOST on failover: avg= 20.6  p99=  32  worst=  40
semi-sync (1 replica must ack before client ack) writes LOST on failover: avg=  0.0  p99=   0  worst=   0
```
**Interpretation.** Async loses (rate × lag) acknowledged writes: ~20 writes at 1,000 writes/s with ~20 ms of best-replica lag. Customers hold confirmation for these writes; they have *vanished*. Semi-sync loses none as long as the synchronous replica survives, at the cost of one extra network round trip per commit (~0.5 ms within an AZ; ~1–2 ms across AZs). **Choose per data class:** payments and orders warrant semi-sync/quorum; page-view counters don't.

### 9.3.5 Follower setup and catch-up `[MK]`
**Adding a new follower** (you can't simply copy files from a live, changing database):
1. Take a **consistent snapshot** of the leader (e.g., `pg_basebackup`, filesystem snapshot at a known log position).
2. Copy it to the new follower.
3. Record the snapshot's **log position** (LSN in Postgres, binlog coordinates/GTID in MySQL, offset in Redis/Kafka).
4. The follower requests all changes **after** that position and applies them in order, until it **catches up** to the leader's current position.
5. It then streams continuously.

**Follower failure and recovery:** a follower remembers the last log position it applied; after a crash/restart it reconnects and resumes from that position. If the leader has already discarded needed log segments (retention), the follower must be rebuilt from a new snapshot.

**Replication slots and the disk-fill hazard `[MK]`:** to guarantee followers can resume, the leader retains log segments until every registered follower has consumed them (Postgres **replication slots**). If a follower is dead or lags permanently, **the leader's WAL accumulates until its disk fills and the primary itself crashes**: a replica problem becomes a primary outage. Mitigation: monitor slot lag in *bytes*, set `max_slot_wal_keep_size`, alert, and drop dead slots promptly.

### 9.3.6 Leader failure and failover `[MK]`
**Failover** = promote a follower to leader and redirect clients. It can be manual or automatic. Steps:
1. **Detect failure:** heartbeats/timeouts (e.g., no response for 10–30 s). *Too short ⇒ false positives* (a slow leader or network blip triggers an unnecessary failover, maybe during a load spike, making things worse). *Too long ⇒ longer outage.*
2. **Choose a new leader:** usually the **most up-to-date follower** (highest log position), via election among replicas or by an external coordinator (consensus group, Orchestrator, Patroni with etcd/Consul, cloud control plane).
3. **Reconfigure:** clients/proxies/DNS send writes to the new leader; other followers re-point to it; the old leader, if it comes back, must **step down** and become a follower.

**Hazards of failover**
| Hazard | What happens | Mitigation |
|---|---|---|
| **Lost writes** | With async replication, the new leader lacks the latest acknowledged writes (§9.3.4); when the old leader returns with those writes, they conflict | Semi-sync/quorum for important data; reconcile or discard (and *tell someone*) the orphaned writes; never reuse auto-increment IDs that were already given out |
| **Split brain** | Two nodes both believe they are leader and accept writes (old leader was partitioned, not dead) ⇒ divergent data that is hard/impossible to merge | **Fencing**: ensure the old leader cannot write: STONITH ("shoot the other node in the head": power it off), revoke its storage/network access, **fencing tokens** (monotonically increasing epoch numbers that storage/followers check and reject stale ones, Ch 16), consensus-based leader leases |
| **Wrong choice of leader** | Promoting a lagging replica loses more data; promoting one with different config/version breaks things | Pick highest-LSN; check version/compat; have a runbook |
| **Cascading overload** | After failover the new leader is cold (caches, plans) and must absorb 100% of load | Keep replicas warm (they apply writes, but their caches may reflect only replay), capacity headroom, gradual traffic shift |
| **Inconsistent downstream state** | Caches or other systems (Redis, search index, Kafka) saw writes that the new leader doesn't have; auto-increment IDs reissued; external systems (payment processor) have records of transactions the database lost | Treat promotion as a data-reconciliation event; use idempotency keys and reconcile with external systems |
| **Timeout tuning dilemma** | Detection too aggressive = flapping failovers; too lax = long downtime | Use consensus-based failure detection or require multiple observers; tune with real incident data |
| **Client behavior** | Clients keep sending to the old leader (cached DNS/connections) | Short DNS TTLs, proxies/virtual IPs, drivers that discover the topology (e.g., `targetServerType=primary`) |

> **A true story pattern `[Public: several widely-discussed provider post-mortems follow this shape]`:** a network partition isolates the primary; an automated tool promotes a replica in another data center; for a window, writes land on both; the databases diverge; operators then spend many hours reconciling data and restoring service. The lesson is not "automation bad" but "automation needs fencing, careful timeouts, and tested runbooks."

### 9.3.7 Replication in the read path: read/write splitting `[MK]`
Applications route writes to the leader and (some) reads to followers.
- **Where the routing lives:** in the application (separate connection pools/data sources), the ORM/driver (e.g., read-replica routing), or a **proxy** (ProxySQL, pgpool-II, RDS Proxy reader endpoint, Vitess).
- **Per-query decisions:** `SELECT` in a transaction that also writes → leader. Reads right after the user's own write → leader (or a caught-up replica). Analytics/reports → a dedicated replica. Non-critical lists/search → any replica.
- **Capacity planning:** if the leader handles all writes (say 20% of work) and each follower must apply those same writes, then 5 followers don't give 5× read capacity: each replica spends part of its capacity applying the replicated write stream. Replication apply can also be **single-threaded** (MySQL historically; Postgres replays WAL in one process) and become the bottleneck: lag grows if the leader's write rate exceeds the replica's single-thread apply rate.

---

## 9.4 Replication lag and the consistency anomalies it creates `[MK]`

With asynchronous replication, followers are **eventually consistent**: if writes stop, all replicas converge. But "eventually" has no bound: typical lag is milliseconds, yet during load spikes, large transactions, network trouble, or vacuum/maintenance on a replica it can reach seconds or minutes.

Three anomalies matter in applications.

### 9.4.1 Read-your-writes (read-after-write) consistency
**Violation:** a user writes, then immediately reads from a lagging replica and doesn't see their own write ("My Tickets" is empty after purchase).
**Fixes**
| Fix | How | Trade-off |
|---|---|---|
| **Read from the leader for data the user may have just modified** | e.g., "a user's own profile always from the leader" | Easy but loads the leader |
| **Time window** | After a write, read from the leader for the next N seconds | Simple; but see below |
| **Version/position token** | The write returns a log position (LSN/GTID/timestamp); the client/session carries it; a replica serves the read only if it has applied at least that position (else wait briefly or use another replica/leader) | Precise and efficient; needs support or a proxy that tracks positions |
| **Sticky replica per session** | Always the same replica for a user | Doesn't fix it (that replica may lag) but helps monotonic reads |
| **Synchronous replication for the replica you read** | — | Expensive |
| **Cross-device consistency** | If the user writes on a phone and reads on a laptop, the "last write time" must be shared server-side, and requests from different devices may be routed to different regions | Needs centralization of tokens/timestamps |

**Measured (Lab 1, part 1).** Two async replicas; lag usually ~40 ms with rare 1–5 s spikes; the user reads back about 300 ms (mean) after writing:
```text
always read a random replica                       stale reads: 12.67%   reads served by primary:   0.0%
read-your-writes: primary for 1 s after a write    stale reads:  0.03%   reads served by primary:  96.4%
version token: replica only if caught up           stale reads:  0.00%   reads served by primary:  12.7%
```
**Read this carefully.** (1) Blindly using replicas gives **~1 in 8 reads stale** right after writes: a visible bug at any scale. (2) The *time-window* fix works for correctness but, since reads typically follow writes quickly, **96% of those reads hit the primary** and the replicas give almost no read offloading: it defeats the purpose. (3) The *version-token* approach is both correct and efficient: only 12.7% of reads fall back to the primary (those that actually found the replica behind). Precision beats a blunt time window.

### 9.4.2 Monotonic reads
**Violation:** a user reads from replica A (up to date, sees comment C), then their next request goes to replica B (behind) and C disappears: time appears to move backwards.
**Fix:** ensure each user reads from the **same replica** (hash of user ID → replica), or track the last-seen position in the session and require replicas to have caught up. (If that replica dies, the user moves to another; positions guard against going backward.)

### 9.4.3 Consistent prefix reads
**Violation:** causally related writes are seen in the wrong order: an answer appears before the question it answers, because the two writes live on different partitions that replicate at different speeds.
```text
 Mr. X: "How far into the future can you see, Mrs. Cake?"   (partition 1)
 Mrs. Cake: "About ten seconds, usually."                    (partition 2)
 Observer (reads from lagging partition 1, up-to-date partition 2) sees:
   Mrs. Cake: "About ten seconds, usually."
   Mr. X: "How far..."?? (arrives later)
```
**Fix:** keep causally related writes in the **same partition**, or use mechanisms that preserve causal order (version vectors, causal consistency, a global sequence) (Ch 15, 20).

### 9.4.4 Causes of lag, and how to monitor it `[MK]`
Causes: replica CPU/IO saturation; single-threaded apply; long-running queries on the replica that conflict with replay (Postgres hot standby conflicts); large batch writes/transactions; network latency or bandwidth (cross-region); replica doing backups/vacuum/index builds; leader write bursts.
Measure lag as **both**: (a) **time lag** (seconds behind: e.g., `now() − pg_last_xact_replay_timestamp()`, MySQL `Seconds_Behind_Source`; beware this stays at 0 when idle and misleads when the stream is stalled) and (b) **byte/position lag** (LSN difference: `pg_stat_replication`, `replay_lag`). Alert on both; put lag on the app's routing decision (stop sending reads to replicas beyond a threshold).

### 9.4.5 Designing for lag: product-level decisions
Do not treat lag as only a database problem. Ask product questions: *Which screens may be stale, by how much? Which must be fresh?* Then map: (a) "my data right after I changed it" → read-your-writes; (b) feeds/lists/recommendations → stale is fine; (c) money/inventory decisions → leader or strongly consistent; (d) analytics → replicas, explicitly stale.

---

## 9.5 Multi-leader replication `[GK]`

### 9.5.1 The idea
More than one node accepts writes; each leader also acts as a follower of the others.
```text
   Region US                 Region EU                 Region ASIA
  [ Leader US ] ◀───────▶ [ Leader EU ] ◀───────▶ [ Leader ASIA ]
   local writes             local writes              local writes
```

### 9.5.2 Why (use cases)
- **Multi-region deployments:** each region accepts writes locally (low latency); inter-region replication is asynchronous; if a region fails, others continue. In single-leader, all writes cross to the one leader region (180 ms for EU users in our story).
- **Offline-capable clients:** each device is effectively a leader (calendar apps, note apps, mobile apps syncing later).
- **Collaborative editing:** every participant edits a local copy; changes merge (Google Docs-style, Ch 20 CRDTs/OT).
- **Tolerance of inter-region network failure:** regions continue independently.

### 9.5.3 The price: write conflicts `[GK]`
Two leaders can modify the same data concurrently. The conflict is only discovered later, when changes cross.
```text
 User A (US leader):   title = "B"            ─┐
                                               ├─ replicate to each other ─▶ conflict: "B" or "C"?
 User B (EU leader):   title = "C"            ─┘
```
Single-leader has a total order so conflicts can't occur; multi-leader makes conflict handling a core part of the design.

**Strategies**
| Strategy | How | Notes |
|---|---|---|
| **Avoid conflicts** | Route all writes for a given record to the same leader (e.g., each user "homed" in a region; user→region assignment) | The best strategy where possible. Fails during failover/rehoming or if a record is written from multiple regions |
| **Last-write-wins (LWW)** | Pick the write with the highest timestamp | Simple but **silently discards concurrent writes**; clock skew can make an *older* write win; deleted data resurrects. Acceptable only for idempotent/overwrite-semantics data (cache entries, heartbeat timestamps, "last seen") |
| **Highest replica ID / arbitrary but deterministic winner** | Pick by node ID | Same loss problem |
| **Merge values** | Combine (union of sets, sum of counters; e.g., shopping cart items) | Needs data-type-aware logic |
| **Record all versions ("siblings") and let the application/user resolve** | Store both; return both on read | Flexible; pushes work to application (Dynamo's shopping cart, Riak) |
| **Conflict-free replicated data types (CRDTs)** | Data structures whose merge is commutative, associative, idempotent (counters, sets, registers, sequences) | Automatic convergence without losing intent (preview in Ch 20) |
| **Operational transformation (OT)** | Transform concurrent edits against each other | Collaborative text editors |
| **Application-specific resolution code** | Run custom logic on conflict | Powerful, but complex to get right |
| **Version vectors / vector clocks** | Track causality to tell "A happened before B" from "A and B are concurrent" | Detects true concurrency so you don't blindly overwrite (Ch 20) |

### 9.5.4 Topologies
- **All-to-all:** each leader sends to every other (robust; messages can arrive out of causal order and need causal tracking).
- **Circular (ring):** each node forwards to the next (fewer links; single node failure interrupts flow).
- **Star:** a central node relays (central SPOF).
Include **origin IDs** in replicated writes to prevent infinite loops.

### 9.5.5 Pitfalls
- **Auto-increment IDs collide** across leaders ⇒ use UUIDs/ULIDs/Snowflake IDs or partition the ID space.
- **Constraints** (unique usernames, inventory ≥ 0) cannot be enforced across leaders without coordination; two leaders may both accept "alice" or both sell the last ticket. Use per-key home leader, reservations, or coordination for those keys.
- **Triggers, secondary indexes, and integrity constraints** behave unpredictably.
- **Causal anomalies:** an update arrives before the insert it modifies at some replicas (needs causal ordering/version vectors).
- **Operational complexity:** debugging divergent data is hard; many tools handle it poorly.
**Rule of thumb:** *avoid multi-leader if you can; if you must, route conflicting writes to a single home leader and use CRDTs/merge for the rest.*

### 9.5.6 Examples
Multi-datacenter MySQL/Postgres setups with BDR/Galera-like tools (with caveats), CouchDB/PouchDB sync, Active Directory/LDAP, Google Docs/Figma (OT/CRDT), mobile offline sync, DynamoDB global tables (multi-region, last-writer-wins) `[Public]`, Cosmos DB multi-region writes (configurable conflict resolution) `[Public]`.

---

## 9.6 Leaderless (Dynamo-style) replication `[MK/GK]`

### 9.6.1 The idea
No leader. A client (or a coordinator node) sends each write to **several replicas** in parallel and each read to **several replicas**, comparing responses. Popularized by Amazon's Dynamo paper `[Public]`; used by Cassandra, Riak, ScyllaDB, Voldemort.
```text
         write(k, v) ──▶ replica A ✔ ack
 client  ───────────────▶ replica B ✔ ack          W = 2 acks required (of N = 3) → write succeeds
         ───────────────▶ replica C ✘ (down)
         read(k) ───────▶ replica B  (v, ver 7)
                 ───────▶ replica C  (v_old, ver 6)  ← stale replica returns older version
         R = 2 responses → client/coordinator picks the newest (ver 7) and repairs C (read repair)
```

### 9.6.2 Quorums: N, W, R `[MK]`
- **N** = number of replicas for each key (commonly 3).
- **W** = replicas that must acknowledge a write; **R** = replicas that must respond to a read.
- **If W + R > N**, the write set and read set must overlap in at least one replica, so a read sees at least one copy of the latest successful write (when the "latest" can be identified by version).

**Measured (Lab 1, part 2).** N = 3; each replica is unreachable 5% of the time; replication delay to a replica ~30 ms (mean); the read happens immediately after the write is acknowledged:
```text
 W  R  W+R>N?   writes failing   reads failing   STALE reads (given both succeeded)
 1  1   no          0.01%            0.01%          64.70%
 1  2   no          0.01%            0.73%          31.37%
 2  1   no          0.74%            0.01%          32.32%
 2  2   yes         0.74%            0.73%           0.00%
 3  1   yes        14.22%            0.01%           0.00%
 1  3   yes         0.01%           14.28%           0.00%
```
**Read this carefully.** (1) With **W=1, R=1** nearly two-thirds of immediate reads are stale (the write sits on one replica, the read lands on another). (2) **W+R>N eliminates staleness** in this model. (3) But **availability drops**: W=3 fails 14% of writes (any single unavailable replica blocks the write; 1−0.95³ = 14.3%); R=3 fails 14% of reads. (4) The balanced **W=2, R=2** gets zero staleness with <1% failures: the standard "quorum" configuration. (5) Choose the *asymmetry* by workload: read-heavy ⇒ small R, larger W; write-heavy ⇒ small W, larger R.

### 9.6.3 What quorums do *not* guarantee `[GK]`
Even with W+R>N, anomalies can occur:
- **Sloppy quorums** (below) let writes go to nodes outside the key's home set, so overlap isn't guaranteed.
- **Concurrent writes** have no single order; you need conflict resolution (LWW or siblings with version vectors).
- **Partial write failures:** a write that reached fewer than W replicas is reported as failed *but is not rolled back*; some replicas keep it, and later reads may return it.
- **Clock-based LWW** discards data under skew.
- A read concurrent with a write may return old or new value, and a later read may return the old one (non-linearizable, Ch 15).
- If a node with a new value fails and is restored from stale data, the number of replicas holding the new value can drop below W.
Conclusion: quorums give **tunable, probabilistic freshness**, not linearizability; for strong guarantees use consensus (Ch 16) or Cassandra's lightweight transactions (Paxos) for specific operations.

### 9.6.4 Mechanisms that repair divergence
| Mechanism | When it runs | How |
|---|---|---|
| **Read repair** | During a read | If the coordinator sees different versions among the R responses, it writes the newest to stale replicas. Good for hot keys; cold keys stay stale |
| **Hinted handoff** | During a write while a replica is down | Another node temporarily stores the write as a "hint" and delivers it when the replica returns. Improves availability (**sloppy quorum**) but hinted writes don't count toward the "true" replica set until delivered |
| **Anti-entropy (background sync)** | Periodically | Replicas compare data via **Merkle trees** (hash trees over key ranges: compare roots, descend only into differing subtrees) and exchange differences. Fixes cold-key divergence cheaply |
| **Gossip** | Continuously | Nodes exchange membership/state (Ch 20) |

### 9.6.5 Tuning knobs in real systems
- **Cassandra:** replication factor RF per keyspace/data center (e.g., RF=3 per DC), consistency levels per operation (`ONE`, `QUORUM`, `LOCAL_QUORUM`, `EACH_QUORUM`, `ALL`, `LOCAL_ONE`). `LOCAL_QUORUM` gives quorum within the local data center (avoid cross-DC latency) with async replication to others.
- **DynamoDB:** (internally Paxos-based leader per partition, but) offers eventually consistent or strongly consistent reads; global tables replicate multi-region with last-writer-wins `[Public]`.
- **Riak:** siblings + vector clocks/dotted version vectors, CRDT data types.

### 9.6.6 Failure modes
- **Hot partitions / uneven repair load.**
- **Tombstone accumulation** (deletes are writes; compaction/GC grace issues cause "zombie" data if a replica is offline longer than the tombstone retention).
- **Clock skew** with LWW.
- **Repair traffic storms** after a long outage.
- **Cross-DC partitions** with `EACH_QUORUM` ⇒ unavailable; with `LOCAL_QUORUM` ⇒ divergence across DCs.

---

## 9.7 Consensus-based replication (preview) `[ADV]`

Single-leader replication with an **agreed-upon log**: the leader appends an entry only after a **majority** of replicas have stored it; leader election and log agreement are handled by a consensus protocol (Raft, Multi-Paxos, Zab). Properties: no split-brain (at most one leader per term/epoch, enforced by majority votes); no acknowledged-write loss while a majority survives; automatic failover; linearizable operations (reads via the leader or with read leases). Cost: majority round trip per write, write throughput bounded by leader, minimum 3 nodes (tolerates 1 failure), 5 (tolerates 2).
Used by etcd, ZooKeeper, Consul, CockroachDB (per range), TiKV, Spanner (Paxos groups), MongoDB (Raft-like for replica sets), Kafka KRaft metadata, Aurora (quorum storage, §9.9). Chapter 16 covers the protocol in depth; here, remember it as **"semi-sync done properly, with safe automatic failover."**

---

## 9.8 Choosing a replication strategy `[MK]`

| Requirement | Prefer |
|---|---|
| Simple, strong semantics, mostly reads, single region | **Single-leader, async/semi-sync** with read replicas |
| No acknowledged-write loss, automatic failover | **Consensus-based** (Raft/Paxos) or semi-sync + proper orchestration |
| Writes close to users in multiple regions, conflicts rare/mergeable | **Multi-leader** (with home-region routing or CRDTs) |
| Always-writeable under partition, massive scale, tolerates eventual consistency | **Leaderless** (Dynamo-style) |
| Strict uniqueness/inventory constraints | Single-leader (or consensus) for those keys |
| Offline-first clients | Multi-leader/CRDT |
| Analytics from OLTP | **Logical replication/CDC** to a warehouse |
Always pair the choice with: **RPO** (how much data loss is acceptable) and **RTO** (how long can we be down)? RPO≈0 ⇒ sync/quorum; RPO of seconds ⇒ async is acceptable.

---

## 9.9 Replication in real systems `[MK/GK]`

| System | Model | Highlights |
|---|---|---|
| **PostgreSQL** | Single-leader; **streaming (physical) replication** of WAL; async by default, `synchronous_commit` / `synchronous_standby_names` for sync/quorum (`ANY k (...)`); **logical replication** (publications/subscriptions) | Hot standbys serve reads (with query-vs-replay conflicts); replication slots; cascading replicas; failover via Patroni/repmgr/cloud managed services; `pg_rewind` to reattach a former primary |
| **MySQL** | Single-leader via **binlog** (row-based recommended); GTIDs; async, **semi-sync** plugin; **Group Replication / InnoDB Cluster** (Paxos-based, multi-primary optional) | Multi-threaded appliers reduce lag; `Seconds_Behind_Source` caveats; Orchestrator/ProxySQL ecosystem |
| **MongoDB** | **Replica sets** with automatic elections (Raft-like); oplog replication; write concern (`w: 1`, `w: "majority"`, `j: true`), read concern (`local`, `majority`, `linearizable`), read preference (primary, secondary, nearest) | `w: "majority"` + `readConcern: "majority"` gives durability and avoids rolled-back reads |
| **Redis** | Single-leader async replication (`replicaof`); Sentinel or Cluster for failover | Can lose acknowledged writes on failover; `WAIT n timeout` blocks until n replicas ack (still not strict consensus) |
| **Cassandra / ScyllaDB** | Leaderless, RF + tunable consistency, hinted handoff, read repair, anti-entropy repair, LWW timestamps | Multi-DC awareness (`NetworkTopologyStrategy`, `LOCAL_QUORUM`) |
| **DynamoDB** | Managed multi-AZ replication (3 replicas per partition, consensus-based leader), optional strongly consistent reads; **global tables** for multi-region with LWW `[Public]` | Eventually consistent reads cost half the read capacity |
| **Kafka** | Per-partition leader with followers; **ISR (in-sync replica) set**; `acks=all` + `min.insync.replicas=2` for durability; `unclean.leader.election.enable=false` to avoid promoting out-of-sync replicas (Ch 13) | Replication of a log is the core of Kafka |
| **Amazon Aurora** `[Public]` | Compute separated from a **shared distributed storage layer**: 6 copies across 3 AZs, quorum writes (4 of 6) and reads (3 of 6); "replicas" read from the same storage with very low lag | Failover in seconds; replication lag ~tens of ms; avoids shipping full pages between instances |
| **Google Spanner / CockroachDB / TiDB** | Range/partition-level consensus groups (Paxos/Raft) with leaders and followers | Synchronous majority replication; strongly consistent; cross-region latency costs (Ch 16) |
| **Elasticsearch / OpenSearch** | Primary and replica shards per index; replicas serve reads | Primary-backup per shard |
| **etcd / ZooKeeper / Consul** | Raft/Zab-replicated state machines | 3 or 5 nodes; small data; strongly consistent |
| **HDFS / object stores (S3-like)** | Block/object replication (3 copies) or erasure coding across racks/AZs | Durability-focused, immutable data (Ch 11) |

---

## 9.10 Geo-replication and latency `[MK/GK]`

Round trips across continents cost 70–200+ ms (Ch 1). Options:
| Design | Writes | Reads | Failure behavior | Notes |
|---|---|---|---|---|
| **Single region, read replicas elsewhere** | Cross-region to the leader (slow for far users) | Local, stale-able | Regional disaster = failover to another region (RPO >0 with async) | Simple; common |
| **Multi-leader (regional writes)** | Local | Local | Regions independent; conflicts possible | Needs conflict strategy; home-region routing |
| **Synchronous multi-region consensus** | One cross-region round trip per commit (tens–hundreds of ms) | Local reads (with leases/follower reads) | Survives region loss, zero RPO | Spanner/CockroachDB style; latency cost |
| **Per-user home region** | Local for the user's region | Local | Rehoming on failure | Good for data with user locality; data residency compliance |
**Data residency/sovereignty (GDPR etc.):** replication across borders may be restricted; design partitioning by region (Ch 10, 22).
**Latency math example:** US–EU RTT = 90 ms. A page needing 6 sequential DB queries from EU to a US primary = 540 ms of pure network time. With a local EU replica: 6 × ~1 ms; writes still pay 90 ms to the US leader (acceptable if infrequent). Batching and caching (Ch 8) also help.

---

## 9.11 Failover design and runbooks `[MK]`

### 9.11.1 Design checklist
1. **Define RPO/RTO** per data class.
2. **Choose sync level** (async/semi-sync/quorum) accordingly.
3. **Automate detection and promotion** *only if* you have fencing, a trustworthy failure detector (preferably through a consensus store), and tested procedures; otherwise semi-automatic with human confirmation.
4. **Fence** the old leader (STONITH, storage/lease revocation, epoch checks).
5. **Redirect clients** reliably (virtual IP, DNS with low TTL, proxy, driver topology discovery); ensure retries are idempotent (Ch 2).
6. **Reconcile**: after failover, find writes present on the old leader but not the new one (`pg_rewind`, binlog compare), decide what to do (export to a quarantine table, replay manually, or discard), and **notify downstream/external systems**.
7. **Rebuild redundancy** (new replica) as soon as possible; run in "degraded N−1" state with alerts.
8. **Test**: game days; kill the leader in staging and production-like environments regularly (chaos engineering, Ch 18).

### 9.11.2 Runbook skeleton: primary DB failure
```text
1. Confirm: is it really down? (multiple vantage points; not just monitoring blip)
2. Freeze: stop writers or enable maintenance mode if automation is uncertain
3. Select candidate: highest replay LSN among healthy replicas; verify not too far behind
4. Fence old primary: power off / revoke access / set epoch
5. Promote candidate (pg_ctl promote / orchestrator); verify read-write
6. Repoint: proxy/VIP/DNS; restart or reset connection pools
7. Verify: write/read smoke tests; replication lag of remaining replicas; error rates
8. Reattach old primary as replica (pg_rewind) or rebuild
9. Reconcile lost writes; notify downstream systems; incident review
```

### 9.11.3 Monitoring checklist
Replication lag (time **and** bytes) per replica; replica state (streaming/catching up/stopped); slot retained WAL size; apply rate vs generation rate; conflicts and cancellations on replicas; leader write rate and long transactions; failover events; sync-standby availability (if no sync standby is available, writes **block**: alert before it hurts); disk space on leader (WAL accumulation).

---

## 9.12 Replicas are not backups `[MK]`

| | Replica | Backup |
|---|---|---|
| Protects against hardware/AZ failure | ✔ | ✔ |
| Protects against `DELETE FROM orders;` or a bad migration | ✘ (replicated instantly) | ✔ (point-in-time restore) |
| Protects against ransomware/malicious admin | ✘ | ✔ (immutable, separate account) |
| Recovery time | Seconds–minutes (failover) | Minutes–hours (restore) |
| Recovery point | Seconds of lag | Backup interval / PITR granularity |
**Backups still needed:** periodic base backups + continuous WAL archiving for **point-in-time recovery (PITR)**; stored in a different account/region, immutable (object lock), with **restore tests** ("a backup you haven't restored is a hope, not a backup").
**Delayed replicas:** deliberately apply changes with a delay (e.g., 1 hour), giving a window to stop replication before a destructive change reaches it. A useful complement, not a replacement.
**Snapshots** (storage-level) are fast but must be crash-consistent or coordinated.

---

## 9.13 Failure modes of replicated systems

| Failure | Mechanism | Mitigation |
|---|---|---|
| **Replica lag spike** | Heavy writes, slow replica, long queries | Monitoring; stop routing to lagging replicas; token-based routing |
| **Stale reads after write** | Reading from lagging replica | Read-your-writes strategies |
| **Lost acknowledged writes** | Async + leader failure | Semi-sync/quorum; reconciliation |
| **Split brain** | Two leaders | Fencing, consensus leader election |
| **Failover to a lagging replica** | Wrong candidate | Highest-LSN selection; promote only within lag bounds |
| **Failover flapping** | Aggressive timeouts | Tuned detection; multi-observer; cooldown |
| **Disk fill from replication slot** | Dead/slow consumer | Slot monitoring, `max_slot_wal_keep_size` |
| **Replica conflicts/cancellations** | Hot standby queries vs replay | `max_standby_streaming_delay`, dedicated analytics replica |
| **Config drift / version skew** | Replica differs from leader | Standardized images, compat checks |
| **Sync standby unavailable → writes block** | Synchronous config without fallback | Quorum sync (`ANY 1`), alerts, auto-promote another standby |
| **Write conflicts (multi-leader)** | Concurrent updates | Home-leader routing, CRDTs, resolution rules |
| **Zombie/resurrected data (leaderless)** | Deletes not propagated before tombstone GC | Repair within GC grace; monitoring |
| **Cross-region partition** | WAN failure | Decide CP vs AP behavior up front (Ch 15) |
| **Backup assumed from replica** | Logical error replicated | Real backups with PITR; delayed replica |
| **Correlated failure** | All replicas in one rack/AZ/same bug | Spread across AZs/regions; diverse versions during rollout |
| **Reading your own cache + replica** | Cache populated from lagging replica | Populate caches from leader or version-check (Ch 8) |

---

## 9.14 Labs

### Lab 1: Replication lag, quorums, and failover loss (Java 17+)
```java
import java.util.*;

public class ReplicationLab {
    static final Random R = new Random(11);
    static double exp(double mean) { return -Math.log(1 - R.nextDouble()) * mean; }
    /** replication lag of one replica for one write: usually ~40 ms, but 1% of the time it is 1-5 seconds (GC, slow disk, big txn) */
    static double lagMs() { return R.nextDouble() < 0.01 ? 1000 + R.nextDouble() * 4000 : exp(40); }

    // ------------------------------------------------------------------ 1) read-your-writes
    static void readYourWrites() {
        int trials = 1_000_000;
        System.out.println("== 1) Async replication, 2 replicas. A user writes, then reads back ~300 ms later (mean). ==");
        String[] names = {"always read a random replica", "read-your-writes: primary for 1 s after a write", "version token: replica only if caught up"};
        long[] stale = new long[3], primaryReads = new long[3];
        for (int i = 0; i < trials; i++) {
            double readAfter = exp(300);                                   // time between the user's write and their next read
            double lag = lagMs();                                          // lag of the randomly chosen replica
            boolean replicaHasIt = lag <= readAfter;
            if (!replicaHasIt) stale[0]++;                                 // policy 0: unconditionally use the replica
            if (readAfter < 1000) primaryReads[1]++; else if (!replicaHasIt) stale[1]++;   // policy 1: primary within 1 s, else replica
            if (!replicaHasIt) primaryReads[2]++;                          // policy 2: fall back to primary when replica is behind
        }
        for (int p = 0; p < 3; p++)
            System.out.printf("%-50s stale reads: %5.2f%%   reads served by primary: %5.1f%%%n", names[p], 100.0 * stale[p] / trials, 100.0 * primaryReads[p] / trials);
    }

    // ------------------------------------------------------------------ 2) quorums
    static void quorums() {
        int N = 3, trials = 400_000; double pDown = 0.05;
        System.out.println("\n== 2) Leaderless replication N=3, each replica unreachable 5% of the time, delivery delay ~30 ms. Read right after write ack. ==");
        System.out.println(" W  R  W+R>N?   writes failing   reads failing   STALE reads (given both succeeded)");
        int[][] combos = {{1, 1}, {1, 2}, {2, 1}, {2, 2}, {3, 1}, {1, 3}};
        for (int[] c : combos) {
            int W = c[0], Rr = c[1]; long wFail = 0, rFail = 0, stale = 0, both = 0;
            for (int t = 0; t < trials; t++) {
                double[] d = new double[N]; List<Double> up = new ArrayList<>();
                for (int i = 0; i < N; i++) { d[i] = exp(30); if (R.nextDouble() >= pDown) up.add(d[i]); else d[i] = Double.POSITIVE_INFINITY; }
                if (up.size() < W) { wFail++; continue; }
                Collections.sort(up); double ackTime = up.get(W - 1);                 // write is acknowledged when W replicas have it
                List<Integer> avail = new ArrayList<>(); for (int i = 0; i < N; i++) if (R.nextDouble() >= pDown) avail.add(i);
                if (avail.size() < Rr) { rFail++; continue; }
                Collections.shuffle(avail, R); boolean sawNew = false;
                for (int j = 0; j < Rr; j++) if (d[avail.get(j)] <= ackTime + 1) sawNew = true;   // that replica already applied the write
                both++; if (!sawNew) stale++;
            }
            System.out.printf(" %d  %d   %-7s   %6.2f%%          %6.2f%%         %6.2f%%%n", W, Rr, (W + Rr > N) ? "yes" : "no", 100.0 * wFail / trials, 100.0 * rFail / trials, both == 0 ? 0 : 100.0 * stale / both);
        }
    }

    // ------------------------------------------------------------------ 3) failover data loss
    static void failoverLoss() {
        System.out.println("\n== 3) Primary crashes. 1,000 acknowledged writes/s; 2 replicas; promote the most up-to-date replica. ==");
        int trials = 20_000;
        for (String mode : new String[]{"async replication", "semi-sync (1 replica must ack before client ack)"}) {
            int[] lost = new int[trials];
            for (int t = 0; t < trials; t++) {
                int count = 0;
                // consider writes acked during the last 6 seconds before the crash (anything older has long since replicated)
                double now = 0, start = -6000;
                for (double w = start + exp(1); w < now; w += exp(1)) {            // Poisson arrivals, mean 1 ms apart
                    double l1 = lagMs(), l2 = lagMs();
                    double lagToBest = Math.min(l1, l2);                           // best surviving replica
                    if (mode.startsWith("semi")) lagToBest = Math.min(lagToBest, 0);   // one replica applied it BEFORE the ack
                    if (w + lagToBest > now) count++;                              // not yet at the best replica when the primary died
                }
                lost[t] = count;
            }
            Arrays.sort(lost); double avg = Arrays.stream(lost).average().orElse(0);
            System.out.printf("%-48s writes LOST on failover: avg=%5.1f  p99=%4d  worst=%4d%n", mode, avg, lost[(int) (trials * 0.99)], lost[trials - 1]);
        }
    }

    public static void main(String[] a) { readYourWrites(); quorums(); failoverLoss(); }
}
```
**Sample output**
```text
== 1) Async replication, 2 replicas. A user writes, then reads back ~300 ms later (mean). ==
always read a random replica                       stale reads: 12.67%   reads served by primary:   0.0%
read-your-writes: primary for 1 s after a write    stale reads:  0.03%   reads served by primary:  96.4%
version token: replica only if caught up           stale reads:  0.00%   reads served by primary:  12.7%

== 2) Leaderless replication N=3, each replica unreachable 5% of the time, delivery delay ~30 ms. Read right after write ack. ==
 W  R  W+R>N?   writes failing   reads failing   STALE reads (given both succeeded)
 1  1   no          0.01%            0.01%          64.70%
 1  2   no          0.01%            0.73%          31.37%
 2  1   no          0.74%            0.01%          32.32%
 2  2   yes         0.74%            0.73%           0.00%
 3  1   yes        14.22%            0.01%           0.00%
 1  3   yes         0.01%           14.28%           0.00%

== 3) Primary crashes. 1,000 acknowledged writes/s; 2 replicas; promote the most up-to-date replica. ==
async replication                                writes LOST on failover: avg= 20.6  p99=  32  worst=  40
semi-sync (1 replica must ack before client ack) writes LOST on failover: avg=  0.0  p99=   0  worst=   0
```
**Experiments**
1. Change the think time between write and read (mean 300 ms) to 50 ms, then to 3 s. How does the stale-read rate under "always replica" change? What does that say about *which* user flows are at risk?
2. In part 1, add a fourth policy: **sticky replica per user + monotonic read guarantee** (remember the highest version seen; if the chosen replica is behind, fall back). Show it fixes monotonic reads but not read-your-writes unless you also track the user's write version.
3. In part 2, add **hinted handoff / sloppy quorum**: when a replica is down, the write goes to a stand-in node. Show how write failures drop, and explain why reads may now be stale even with W+R>N.
4. In part 3, change the lag distribution (e.g., make 5% of writes lag 1–5 s). What happens to p99/worst losses? Compute expected loss = rate × E[min lag across replicas] and compare.
5. Add a **quorum-sync** option (`ANY 1 of 2 replicas must ack`), losing nothing as long as one of the two acked; measure the added commit latency using exponential ack times (the minimum of two exponentials is faster than either alone).

### Lab 2: Real PostgreSQL streaming replication (needs Docker; not executed in the book's sandbox)
```bash
# Primary + replica using the bitnami/postgresql image (or two postgres containers with pg_basebackup)
docker network create pgnet
docker run -d --name pg-primary --network pgnet -e POSTGRESQL_REPLICATION_MODE=master \
  -e POSTGRESQL_REPLICATION_USER=repl -e POSTGRESQL_REPLICATION_PASSWORD=replpw \
  -e POSTGRESQL_USERNAME=app -e POSTGRESQL_PASSWORD=apppw -e POSTGRESQL_DATABASE=demo bitnami/postgresql:16
docker run -d --name pg-replica --network pgnet -e POSTGRESQL_REPLICATION_MODE=slave \
  -e POSTGRESQL_MASTER_HOST=pg-primary -e POSTGRESQL_REPLICATION_USER=repl \
  -e POSTGRESQL_REPLICATION_PASSWORD=replpw -e POSTGRESQL_PASSWORD=apppw bitnami/postgresql:16
```
```sql
-- on the primary
CREATE TABLE t(id serial primary key, v text);
INSERT INTO t(v) SELECT 'row ' || g FROM generate_series(1, 100000) g;
SELECT client_addr, state, sent_lsn, write_lsn, flush_lsn, replay_lsn,
       pg_wal_lsn_diff(sent_lsn, replay_lsn) AS byte_lag, replay_lag
FROM pg_stat_replication;
-- on the replica
SELECT pg_is_in_recovery(), now() - pg_last_xact_replay_timestamp() AS time_lag;
SELECT count(*) FROM t;                   -- can writes happen? try INSERT: "cannot execute INSERT in a read-only transaction"
```
**Experiments:** (1) generate a burst of writes while watching `byte_lag`; (2) stop the replica for 2 minutes and watch `pg_replication_slots.restart_lsn` and WAL size on the primary; (3) switch to `synchronous_commit = on` with `synchronous_standby_names = 'ANY 1 (*)'`, pause the replica (`docker pause`), and observe that commits **block**; (4) promote the replica (`pg_ctl promote`), and then try to start the old primary as a standby (`pg_rewind`).

### Lab 3: Failover tabletop exercise (paper)
Scenario: primary in AZ-a, a semi-sync replica in AZ-b, an async replica in AZ-c, an application with connection pools of 100 per instance (50 instances) and a cache. At 14:02 AZ-a's network becomes unreachable (the primary is *alive* but isolated). Walk through: detection timeline, who gets promoted, what happens to the old primary and its clients, which writes might exist only on the old primary, how clients discover the new leader, what the cache holds, and what you tell customers. List every point where a missing fencing mechanism would produce split-brain.

---

## 9.15 Interview perspective

- When you add a database to a design, say **"primary with N replicas, async replication, semi-sync for the standby that will take over, across AZs"** and state RPO/RTO.
- Immediately pair read replicas with **"read-your-writes handled via X"** (routing the user's own reads to the primary for a window, or version tokens) and mention **replication lag monitoring**.
- Discuss **failover**: detection, promotion of the most up-to-date replica, **fencing**, client redirection, and reconciliation of lost writes.
- For global systems, compare **single-leader with regional read replicas** vs **multi-leader/home-region** vs **consensus-based multi-region**, with conflict handling.
- Use quorum language correctly: **W + R > N**, sloppy quorums, hinted handoff, read repair; note it isn't linearizable.
- Mention that **replicas are not backups** (PITR, immutable backups, restore tests).
- Tie to CAP/consistency (Ch 15) only after describing concrete behavior.

---

## 9.16 Common misconceptions

1. **"Replicas give me write scaling."** They don't; every write still hits the leader, and every replica applies it.
2. **"Replicas are my backup."** They replicate mistakes instantly.
3. **"Async replication is fine, lag is milliseconds."** Typically yes; but spikes happen and failover can lose acknowledged writes.
4. **"Synchronous replication makes everything safe."** Fully sync to *all* followers makes availability fragile; use semi-sync/quorum.
5. **"Automatic failover is a solved problem."** Without fencing, timeouts, and tests, it produces split-brain and data loss.
6. **"Multi-master is just more availability."** It adds write conflicts that require explicit resolution.
7. **"LWW resolves conflicts."** It silently drops writes and is clock-sensitive.
8. **"W + R > N means strongly consistent."** Not linearizable; sloppy quorums, concurrent writes, partial failures matter.
9. **"Seconds_Behind_Master = 0 means in sync."** Not if the stream is stalled or idle; measure positions too.
10. **"A read replica always has the same data as the leader."** Only eventually.
11. **"Caches populated from replicas are fine."** They may cache stale data and keep it for the TTL.
12. **"More replicas always more reliable."** More replicas = more lag sources, more WAL retention, more failover candidates to choose from.

---

## 9.17 Chapter summary

- **Replication** copies data for **availability, read scaling, latency, durability, offloading**; it does **not** scale writes or data size, and does not replace backups.
- **Single-leader**: leader ships an ordered log (statement/WAL/logical) to followers; new followers join via snapshot + log catch-up; **async** (fast, may lose acknowledged writes: ~20 writes at 1,000 writes/s in our simulation), **semi-sync** (no loss while the sync follower survives), **quorum/consensus** (no loss while a majority survives).
- **Failover**: detect, choose the most up-to-date follower, reconfigure; hazards: **lost writes, split-brain (needs fencing), wrong candidate, flapping, inconsistent downstream state, replication-slot disk fill.**
- **Lag anomalies**: **read-your-writes** (use version tokens or leader reads; a blunt 1 s window sent 96% of reads to the primary), **monotonic reads** (sticky replica/positions), **consistent prefix** (keep causally related writes together). Measure lag in time and bytes.
- **Multi-leader**: local writes and regional independence at the cost of **conflicts**; prefer **home-leader routing**, CRDTs, or merges; LWW loses data.
- **Leaderless**: quorums **W+R>N** (W=R=2 with N=3 ⇒ 0% stale in simulation, <1% failures; W=1,R=1 ⇒ 65% stale), sloppy quorums + hinted handoff, read repair, Merkle-tree anti-entropy; not linearizable.
- **Real systems**: Postgres/MySQL (log shipping), MongoDB (replica sets, write concerns), Redis (async), Cassandra (tunable), Kafka (ISR), Aurora (shared quorum storage), Spanner/Cockroach (consensus per range).
- **Operate**: monitor lag, slots, sync standby availability; rehearse failover; maintain real backups (PITR) and possibly a delayed replica.

### Mental models
| Concept | Mental model |
|---|---|
| Single-leader replication | An author dictating to scribes who copy in order |
| Replication lag | Scribes a few sentences behind; a reader might see an old page |
| Semi-sync | The author waits until one trusted scribe has the sentence before announcing "done" |
| Split-brain | Two people both believing they're the author, writing different books |
| Fencing | Taking the old author's pen away before a new one starts |
| Quorum | Votes: a decision stands if the majority of both writers and readers overlap |
| Replica ≠ backup | A photocopier faithfully copies a torn page; an archive keeps old editions |

---

## 9.18 Exercises

### A. Conceptual
1. List five reasons to replicate and three problems replication does *not* solve.
2. Describe how a new follower joins a running leader with no downtime.
3. Compare statement-based, physical (WAL), and logical replication. When is each appropriate?
4. Explain async, sync, and semi-sync replication in terms of commit latency, durability, and availability.
5. What are the steps of failover? Which step is most dangerous and why?
6. Define split-brain and explain three fencing techniques.
7. Why can a replication slot take down the *primary*? How do you prevent it?
8. Define read-your-writes, monotonic reads, and consistent prefix reads with a concrete user scenario each.
9. Why is "read from the primary for 1 second after a write" a poor general solution? What is better?
10. What causes replication lag? How do you measure it, and why measure both time and bytes?
11. When is multi-leader replication appropriate? What new class of problem does it introduce?
12. Compare conflict strategies: avoidance, LWW, merge, siblings, CRDTs. Give a use case for each.
13. In a leaderless system with N=5, choose W and R for (a) read-heavy, (b) write-heavy, (c) balanced; give failures tolerated for each.
14. Explain sloppy quorum, hinted handoff, read repair, and anti-entropy. Which handles cold data?
15. List four reasons W+R>N does not make a leaderless system linearizable.
16. Explain "replicas are not backups" with three scenarios.
17. What is a delayed replica and when is it useful?
18. How does Kafka use ISR and `min.insync.replicas` to provide durability? What is unclean leader election and why is it dangerous?
19. Why is Aurora's replication different from classic Postgres streaming replication?
20. Explain RPO and RTO and map async, semi-sync, and consensus replication to typical RPO values.

### B. Calculations
21. A primary generates 8 MB/s of WAL. A replica applies 6 MB/s (single-threaded). After 10 minutes, how far behind is it (MB and seconds of lag)? When would it catch up if the write rate falls to 3 MB/s?
22. 1,500 acknowledged writes/s, async replication with expected best-replica lag 35 ms. Expected writes lost on failover? How does semi-sync change this, and what is the added commit latency if RTT to the sync standby is 1.2 ms?
23. A system has N=3, W=2, R=2, each replica is independently unavailable with probability 2%. Compute P(write fails), P(read fails), and P(read quorum has at least one stale replica *given* only 2 of 3 replicas received the write).
24. Read replicas: the primary serves 5,000 reads/s and 1,000 writes/s at 70% CPU. Writes cost 3 CPU-units each and reads 1 unit; capacity per node 12,000 units/s. How many replicas are needed to serve 20,000 reads/s (each replica also applies 1,000 writes/s costing 1.5 units each; target 70% utilization)? Does the leader need to serve reads?
25. Cross-region: 120 ms RTT. A request makes 4 sequential reads and 1 write. Compute latency for (a) all in one region; (b) EU user with local replica for reads and remote leader for writes; (c) EU user with remote leader for all.
26. Quorum read after sloppy write: W=2 of N=3, but one home replica is down and a stand-in node holds the hint. Then the stand-in node fails before delivering. State what R=2 reads can return and whether the latest write could be missed.
27. Failover timeout tuning: heartbeats every 2 s; failover triggered after k missed heartbeats. If the leader pauses for GC 8 s with probability 1%/day, and genuine failures take 10 s of detection time being acceptable, choose k and justify. What false failover rate do you accept?
28. Merkle-tree anti-entropy: two replicas each hold 1 billion keys, 0.001% differ (10,000 keys). With a tree of depth 20 (branching 2, leaves cover ~1,000 keys each), estimate how many hash comparisons are needed vs comparing all keys.

### C. Design
29. Design the replication architecture for a payments ledger: RPO=0, RTO < 30 s, two regions. Specify sync level, number of replicas, failover/fencing, and client behavior.
30. Design read-your-writes for a web app with 3 replicas, using LSN tokens. Where is the token stored? How do replicas expose their position? What if no replica has caught up within 200 ms?
31. A collaborative to-do app works offline on phones. Design replication/conflict handling. What data types would be CRDTs? How would you handle deleted items?
32. A global SaaS stores user data with EU/US residency requirements. Choose a replication/partitioning approach and describe failure behavior and rehoming.
33. Design a monitoring and alerting plan for a Postgres primary + 2 replicas + 1 delayed replica. List metrics, thresholds, and automated actions.
34. Design the runbook and automation for promoting a replica when the primary's AZ fails, including fencing, DNS/proxy changes, application pool resets, and post-incident reconciliation.

### D. Debugging
35. After a failover, customers report "missing" orders they have confirmation emails for. Explain and describe the recovery process.
36. Users intermittently see items disappear and reappear in their cart list. Diagnose and fix.
37. The primary's disk filled overnight and the database crashed; the only change was adding a new analytics replica last week. Diagnose and fix.
38. Two nodes both accepted writes for 40 seconds after a network partition. List what evidence you'd collect and how to reconcile.
39. Replication lag spikes to 5 minutes every night at 02:00. List five hypotheses and how to test each.
40. A "synchronous" setup suddenly stopped accepting writes when a single replica went down. Why, and how should it be reconfigured?

### E. Interview-style
41. "How would you design the database layer for a read-heavy application with a 99.95% availability target?"
42. "A user updates their profile and sees the old data. Why, and how do you fix it?"
43. "Explain how quorum reads and writes work in Cassandra. What consistency level would you use for X?"

### F. Teach-back
44. Explain split-brain and fencing to a non-engineer using a ship with two captains.

---

## 9.19 Full answer key

**1.** Reasons: availability, read scaling, latency, durability, offloading analytics/backups/maintenance (also offline operation). Not solved: write scaling, dataset size beyond one node, protection against logical errors (bad deletes), and free consistency.

**2.** Take a consistent snapshot and record its log position; copy it to the follower; the follower requests all log entries after that position and applies them in order until caught up; then streams. No downtime because the leader keeps serving; the log retention must cover the copy time.

**3.** Statement-based: ship SQL; fragile with non-deterministic functions; rarely used now. Physical/WAL: exact byte-level changes; best for HA standbys of the same version/engine. Logical/row-based: row changes decoupled from storage; for upgrades, selective replication, CDC, heterogeneous targets.

**4.** Async: ack after leader commit; lowest latency; may lose acknowledged writes; leader unaffected by follower failure. Sync: wait for follower(s); higher latency; no loss; availability fragile if all must ack. Semi-sync: wait for one follower; moderate latency; no loss while that follower survives; tolerates others failing.

**5.** Detect → choose candidate → reconfigure/redirect (+ fence old leader, reconcile). Most dangerous: promotion without fencing (split-brain) and choosing a lagging replica (data loss); also detection tuning (false positives).

**6.** Split-brain: two nodes act as leader and accept writes. Fencing: STONITH (power off), revoke network/storage access (cloud API, SAN reservations), fencing tokens/epoch numbers verified by storage/followers, leader leases through consensus.

**7.** The leader retains WAL until the slot's consumer has read it; a dead/lagging consumer causes unbounded WAL growth → disk full → primary crash. Prevent: monitor slot lag bytes, set `max_slot_wal_keep_size`, drop abandoned slots, alert.

**8.** Read-your-writes: post a comment, refresh, it's missing. Monotonic reads: see a comment, refresh via another replica, it's gone. Consistent prefix: see a reply before the question it replies to.

**9.** Because reads typically follow writes immediately, a time window routes nearly all of them to the primary (96% in the lab), destroying read offloading. Better: version/position tokens so only reads that actually hit a lagging replica fall back (12.7%).

**10.** Causes: CPU/IO saturation on replica; single-threaded apply; long queries/conflicts; bulk writes; network; maintenance. Measure time lag and byte/position lag, because time lag can read 0 when idle or misleading when stalled, while bytes show backlog.

**11.** Multi-region writes with local latency, offline clients, collaborative editing, regional independence. Introduces write conflicts, causal ordering issues, uniqueness/constraint problems, and ID collisions.

**12.** Avoidance: home-leader per user. LWW: overwrite-semantics data (last-seen). Merge: shopping carts (union). Siblings: Dynamo-style app resolution. CRDTs: counters, sets, collaborative text.

**13.** N=5: read-heavy: W=5,R=1 (reads tolerate 4 failures, writes none) or W=4,R=2; write-heavy: W=1,R=5 (writes tolerate 4 failures, reads none) or W=2,R=4; balanced: W=3,R=3 (tolerates 2 failures for both). All satisfy W+R>N (6>5, etc.).

**14.** Sloppy quorum: accept writes at stand-in nodes if home replicas are unreachable. Hinted handoff: stand-in holds the write and delivers later. Read repair: fix stale replicas upon reads. Anti-entropy: background Merkle-tree comparison/sync; it handles cold data.

**15.** Sloppy quorums break overlap; concurrent writes lack a single order; failed partial writes remain on some replicas; LWW with clock skew drops data; reads concurrent with writes can go backward; restoring replicas from stale data reduces holders of the latest value.

**16.** Accidental `DELETE`/bad migration (replicated instantly); ransomware or malicious admin; application bug corrupting data; need for point-in-time recovery to a past moment; regulatory retention.

**17.** A replica that applies the log with an intentional delay (e.g., 1 hour), giving time to stop replication before a destructive change reaches it and recover from it quickly. Useful complement to PITR.

**18.** Topic partitions have a leader and followers; ISR = followers sufficiently caught up. With `acks=all` and `min.insync.replicas=2`, a write is acknowledged only after it's on at least 2 ISR replicas, so losing the leader doesn't lose it. Unclean leader election promotes an out-of-sync replica, losing messages; dangerous for durability.

**19.** Aurora separates compute from a distributed storage layer (6 copies across 3 AZs with quorum writes); replicas read from the same storage, so no full WAL stream replay of pages between instances; lag is tens of ms and failover is faster.

**20.** RPO = acceptable data loss; RTO = acceptable downtime. Async: RPO = seconds (lag) >0; semi-sync: RPO≈0 if the sync standby survives; consensus/quorum: RPO=0 with majority surviving. RTO is determined by detection, promotion, and client redirection.

**21.** Net backlog growth = 8 − 6 = 2 MB/s × 600 s = **1,200 MB** behind. Measured in leader activity that is 1,200 / 8 = **150 s** of lag (the replica shows the database as it was 150 s ago); draining it at the apply rate alone would take 1,200 / 6 = 200 s. After the write rate drops to 3 MB/s, the replica gains 6 − 3 = 3 MB/s, so it catches up in 1,200 / 3 = **400 s** (~6.7 min).

**22.** Expected loss = 1,500 × 0.035 = **52.5 writes**. Semi-sync: ~0 (assuming the sync standby survives); added commit latency ≈ RTT + standby fsync ≈ 1.2 ms + ~0.2–1 ms of fsync ⇒ roughly 1.5–2.5 ms.

**23.** P(write fails) = P(<2 of 3 up) = P(≥2 down) = 3·0.02²·0.98 + 0.02³ = 0.001176 + 0.000008 = **0.00118 (0.118%)**. Same for reads: **0.118%**. Given only 2 of 3 replicas have the write: P(a read of 2 from 3 hits ≥1 up-to-date replica): any 2-of-3 set includes at least one of the 2 up-to-date replicas ⇒ probability 1 ⇒ **stale-only probability = 0** (that's the quorum overlap). (If the read set could include only the stale replica plus an unavailable one, the read fails instead of returning stale data.)

**24.** Per replica capacity: 12,000 × 0.7 = 8,400 units/s; apply writes cost 1,000 × 1.5 = 1,500 → 6,900 units/s left for reads ⇒ 6,900 reads/s per replica. For 20,000 reads/s: 20,000/6,900 = 2.9 → **3 replicas** (leader serves none). Leader load: 1,000 × 3 = 3,000 units (25% of capacity): it could also serve some reads, but keeping it for writes and read-your-writes fallbacks is wise.

**25.** (a) all local: ~5 round trips ≈ 5 × ~1–2 ms ≈ 10 ms. (b) 4 local reads (~4–8 ms) + 1 write to the remote leader (120 ms) ≈ **~125 ms**. (c) all remote: 5 × 120 ms = **600 ms**.

**26.** The latest write is held on one home replica and the stand-in hint (which counted toward W=2 under a sloppy quorum). If the stand-in fails before delivering and the home replica that had the other copy isn't among the R=2 responders (or is down), the read can return the *old* value; overlap isn't guaranteed because the stand-in isn't in the key's home set. So the latest write can be missed (and can later be lost entirely if both copies are lost). That is why sloppy quorums weaken guarantees.

**27.** An 8-second GC pause at 2 s heartbeats means 4 missed beats, so k must be at least 5 to avoid a false failover on a pause (k=5 triggers at 10 s, which also just meets the 10 s detection budget). The margin is thin: any pause slightly longer than 10 s still triggers a false failover (that is the exposure you are accepting). Better: reduce GC pauses, add a multi-observer check, or add a short confirmation probe before promotion. Accept occasional false failovers only if failover itself is safe (fenced, tested).

**28.** 1 billion keys at ~1,000 keys per leaf is ~1 million leaves, i.e. a tree of depth 20. With 10,000 differing keys scattered randomly, almost every one lands in its own leaf, so ~10,000 leaves differ. The comparison walks down only differing branches: the top ~13 levels (about 2^14 ≈ 16,000 nodes) almost all differ, then each differing leaf adds a private path of ~7 more nodes (≈ 70,000). That is on the order of **10^5 hash comparisons**, versus **10^9** key comparisons for a naive scan: roughly four orders of magnitude fewer. The replicas then exchange only the ~10,000 differing leaf ranges (~10 million keys), not all 1 billion.

**29 (sketch).** Single-leader with consensus (Raft/Paxos) or synchronous quorum across ≥3 AZs in region A; second region via async replica (RPO>0 for region-loss) or sync consensus across 3 regions if RPO=0 across regions is required (latency cost). Auto failover with consensus-based leader election and fencing tokens; clients use idempotency keys and retry; ledger entries immutable; periodic reconciliation with external processors; PITR backups.

**30 (sketch).** On write commit, return the commit LSN to the app; store it in the user's session/cookie (or server-side session store); on read, send the LSN to the router; the router picks a replica whose `replay_lsn ≥ token` (replicas report positions via `pg_stat_replication`/heartbeats). If none within 200 ms, read from the primary. Handle expiry of tokens (after a few seconds they're irrelevant).

**31 (sketch).** Each device holds a replica; use CRDTs: a per-item LWW-register for fields (or a better merge by field), an OR-Set (add-wins) for the item collection, a counter for completion counts, and tombstones for deletes (with garbage collection when all devices have acknowledged). Sync via server relay with version vectors; resolve edit/delete conflicts by policy (e.g., edits win over deletes, or delete wins with undo).

**32 (sketch).** Partition by tenant/user home region (EU/US); each region has its own leader and local replicas; cross-region replication limited to non-regulated metadata; global directory maps user→region; failover within the region across AZs; DR region within the same jurisdiction (e.g., EU-West and EU-Central); rehoming via migration with consent/process.

**33 (sketch).** Metrics: replay lag (seconds/bytes), replica state, slot retained bytes, WAL generation rate, sync-standby count, long-running transactions, conflict cancellations, disk usage, TPS, failover events; thresholds: lag >5 s warn, >30 s page; slot retained WAL >10 GB warn; no sync standby → page; delayed replica lag deviating from configured delay → alert. Automation: remove lagging replicas from the read pool; drop abandoned slots after approval.

**34 (sketch).** See §9.11.2: confirm → freeze → candidate selection → fence → promote → repoint → reset pools → verify → reattach/rebuild → reconcile. Include automated checks and manual approvals, and drills.

**35.** The async replica promoted was missing the last acknowledged writes (including orders whose confirmation emails were sent). Recovery: identify the old primary's WAL/binlog beyond the promoted position (once accessible), extract missing transactions, replay or manually reconcile (check payments/provider records), notify affected customers; prevent via semi-sync/quorum for the orders path and idempotency keys with external reconciliation.

**36.** Monotonic reads violation: requests are routed to different replicas with different lag. Fix: session-sticky replica selection or position tokens; or read from the primary for that screen.

**37.** An unused/slow replication slot (from the new analytics replica, which disconnected or lags) retained WAL until the disk filled. Fix: free space, drop the slot or reconnect the replica, set `max_slot_wal_keep_size`, monitor retained bytes.

**38.** Evidence: logs of both nodes (term/epoch, promotion events), WAL/binlog positions at divergence, writes accepted by each side (identify by time ranges), application logs, external system records. Reconcile: choose the authoritative branch (usually the new leader), extract the other branch's unique transactions, replay compatible ones (watch unique/ID conflicts), manually resolve the rest, notify affected users; add fencing.

**39.** Hypotheses: nightly batch writes/ETL; a vacuum/reindex on the replica; backup running on the replica; long analytical queries causing replay conflicts; cron-driven bulk deletes; network contention with backups; cache refresh jobs. Test via logs/metrics around 02:00, `pg_stat_activity` on replica, IO and network stats, disabling one job at a time.

**40.** Setting required acknowledgements from all/one named standby; when it went down, commits blocked waiting for a sync ack. Reconfigure with `ANY 1 (standby1, standby2)` (quorum sync) or automatic fallback with alerts; ensure at least two standbys so one failure doesn't block writes.

**41 (sketch).** Primary + 2 replicas across AZs (semi-sync to one), managed failover with fencing, connection proxy, read/write splitting with read-your-writes tokens, cache layer, PITR backups; compute availability: AZ-level redundancy gives >99.95% when failover <~20 s; load-test failover.

**42 (sketch).** Read-after-write violation from lagging replica or stale cache; fix with leader reads/tokens/cache invalidation or write-through; verify that CDN or browser caches aren't serving stale responses.

**43 (sketch).** RF=3; QUORUM = 2: writes to 3 replicas, wait for 2 acks; reads ask 2 and return the newest by timestamp; with RF=3 and `LOCAL_QUORUM`, multi-DC with local latency; for lightweight transactions use SERIAL. Choose by access pattern: user profile → `LOCAL_QUORUM` read/write; clickstream → `ONE`/`LOCAL_ONE` writes.

**44.** A ship loses radio contact; the first mate (backup captain) assumes command because he thinks the captain is gone, but the captain is alive and still steering: two captains giving contradictory orders (split-brain). Fencing is taking the first captain's keys/wheel before the second takes charge, so only one hand steers.

---

## 9.20 Flashcards

| Front | Back |
|---|---|
| Replication solves | Availability, read scaling, latency, durability (not writes, data size, or bad deletes) |
| Single-leader | One writer; ordered log shipped to followers |
| Log formats | Statement (fragile), WAL/physical (exact), logical/row (flexible) |
| Async | Fast; may lose acked writes on failover (~20 at 1,000 w/s in lab) |
| Semi-sync | Wait for 1 follower; no loss while it survives |
| Failover steps | Detect → choose most up-to-date → reconfigure → fence |
| Split-brain | Two leaders; prevent with fencing/consensus |
| Replication slot hazard | Dead consumer → WAL fills primary's disk |
| Read-your-writes | See your own writes; use leader or LSN tokens |
| Monotonic reads | Never go back in time; sticky replica/positions |
| Consistent prefix | Preserve causal order across partitions |
| Time window RYW cost | 96% of reads to primary in lab; tokens: 12.7% |
| Multi-leader | Local writes everywhere; conflicts need resolution |
| LWW | Simple; drops concurrent writes; clock-sensitive |
| CRDT | Merge-by-design data types (Ch 20) |
| Quorum rule | W + R > N for overlap |
| Lab: W=1,R=1 | 64.7% stale; W=2,R=2: 0% stale, ~0.7% failures |
| Hinted handoff | Stand-in holds writes for a down replica |
| Read repair / anti-entropy | Fix on read / background Merkle-tree sync |
| Replica vs backup | Replicas copy mistakes; backups+PITR undo them |
| Delayed replica | Apply with delay to survive a bad change |
| Kafka durability | `acks=all`, `min.insync.replicas=2`, no unclean election |
| Aurora | Shared 6-copy quorum storage; fast replicas |
| RPO / RTO | Data loss tolerance / downtime tolerance |

## 9.21 Glossary additions

| Term | Simple Meaning | Technical Meaning | Why It Exists | Example |
|---|---|---|---|---|
| Replication | Copying data | Maintaining copies across nodes | HA, scale, latency | Primary + replica |
| Leader/primary | The writer | Node accepting writes | Total order | Postgres primary |
| Follower/replica | The copier | Node applying the leader's log | Reads, failover | Hot standby |
| Replication log | Change list | Ordered record of changes | Sync followers | WAL, binlog |
| LSN / GTID | Log position marker | Identifier of a point in the log | Catch-up, tokens | `0/3000148` |
| Async / sync / semi-sync | Ack timing | Whether commit waits for followers | Durability vs latency | `synchronous_commit` |
| Replication lag | How far behind | Delay between leader commit and follower apply | Staleness | 40 ms typical |
| Failover | Switch leaders | Promote a follower after leader failure | Availability | Patroni |
| Split-brain | Two leaders | Concurrent leaders accepting writes | Divergence hazard | Partitioned primary |
| Fencing / STONITH | Disable the old leader | Prevent a stale leader from acting | Safety in failover | Epoch tokens |
| Replication slot | Reserved log retention | Leader keeps logs for a consumer | Resumable replication | `pg_replication_slots` |
| Read-your-writes | See own updates | Session guarantee | UX correctness | Profile edit |
| Monotonic reads | No going back | Reads never return older state than prior reads | UX correctness | Comments list |
| Consistent prefix | Causal order | Reads see writes in causal order | Meaning preservation | Q before A |
| Multi-leader | Many writers | Several nodes accept writes | Regional writes | Multi-region DB |
| Conflict | Competing writes | Concurrent updates to same data | Multi-writer cost | Title edits |
| LWW | Latest wins | Timestamp-based resolution | Simplicity | DynamoDB global tables |
| Version vector | Causality tracker | Per-replica counters to detect concurrency | Detect conflicts | Riak |
| CRDT | Auto-merging data type | Convergent replicated data type | Conflict-free merge | OR-Set |
| Quorum | Majority-style overlap | W+R>N acks/reads | Tunable consistency | W=2,R=2,N=3 |
| Sloppy quorum | Borrowed replicas | Writes accepted by stand-ins | Availability | Dynamo |
| Hinted handoff | Held for later | Stand-in stores write for a down replica | Availability | Cassandra hints |
| Read repair | Fix on read | Update stale replicas found during reads | Convergence | Coordinator |
| Anti-entropy / Merkle tree | Background sync | Hash-tree comparison to find diffs | Cold-data repair | Cassandra repair |
| ISR | In-sync replicas | Followers caught up with the leader | Kafka durability | `min.insync.replicas` |
| PITR | Rewind in time | Restore from base backup + WAL to a moment | Undo mistakes | Before bad DELETE |
| RPO / RTO | Loss / downtime budget | Recovery point/time objectives | Requirements | 0 s / 30 s |

---

**Batch 2 complete.** Part II so far: Ch 6 (scalability), 7 (load balancing), 8 (caching), 9 (replication).

**Next: Chapter 10: Partitioning and Sharding.** The answer to the problem replication cannot solve: scaling writes and data size. Shard keys, range vs hash partitioning, consistent hashing with virtual nodes, hot spots, secondary indexes across shards, rebalancing, cross-shard queries and transactions, and routing; with a working consistent-hashing lab.

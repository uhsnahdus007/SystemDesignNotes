# Chapter 15: Consistency Models and the CAP Theorem

## Chapter card

| | |
|---|---|
| **Objective** | Understand precisely what “consistent” can mean in a replicated system: the spectrum from linearizability to eventual consistency, session guarantees, how to test a system against a model, the CAP theorem and PACELC stated correctly, how consistency interacts with transactions and isolation, and how to choose the weakest model that keeps your application correct |
| **Why this chapter exists** | “Make it consistent” is the most common vague requirement in system design, and “CP vs AP” the most commonly misused slogan. This chapter gives you exact definitions, measured behavior of real quorum designs, and a decision procedure you can use in interviews and in production |
| **Prerequisites** | Chapters 3 (transactions, isolation), 9 (replication, quorums), 14 (partial failure, clocks, ordering) |
| **Study time** | 11–14 hours including labs |
| **Tier** | `[MK]` session guarantees, CAP/PACELC, choosing models; linearizability, causal consistency, checkers `[GK]`; theory `[ADV]` |
| **Mastery criteria** | (1) Define linearizability, sequential, causal, and eventual consistency and place them on a spectrum. (2) Decide whether a small concurrent history is linearizable. (3) Explain why `W + R > N` quorums are not automatically linearizable and what fixes it. (4) State CAP precisely (what C, A, and P mean), and explain why “CA” is not a real choice. (5) State PACELC and classify real systems. (6) Distinguish consistency models from transaction isolation levels. (7) Choose the consistency level per feature and design around weaker models (session guarantees, CRDTs, compensation). (8) Explain cross-region latency costs of strong consistency with numbers |

---

## 15.1 The Story: Four Bugs, Four Different Meanings of “Consistent”

**SocialSphere** replicates its data across three data centers. Four bug reports arrive in one week:

1. **“My post disappeared.”** A user publishes a post, is redirected to their profile, and the post isn’t there. Refresh: it appears.
2. **“The conversation makes no sense.”** Alice asks a question; Bob answers. A third user sees Bob’s *answer* before Alice’s *question*.
3. **“Two people got the same username.”** Two users registered `skyfire` within the same second in different regions; both got success.
4. **“I saw a like count go down.”** A user refreshes and sees 1,204 likes, then 1,198, then 1,204 again.

Engineering’s first reaction: “we need stronger consistency.” But these are four *different* problems: (1) read-your-writes, (2) causal ordering, (3) a uniqueness constraint that needs linearizable coordination, (4) monotonic reads. Each has a different, cheaper-than-total fix. The skill this chapter builds is **naming the guarantee you need**, and paying only for that.

### Pause & Predict 15.1
> For each bug, write the weakest guarantee that would prevent it (session? causal? linearizable?). Which of the four truly needs coordination across data centers on every write? Which could be fixed purely at the client or routing layer?

---

## 15.2 What a consistency model is `[MK]`

### 15.2.1 Definition
A **consistency model** is a contract: given the history of operations (reads and writes, possibly concurrent, across many clients and replicas), which results may a read return? Stronger models restrict the system more (fewer surprising results, but more coordination, higher latency, lower availability). Weaker models allow more anomalies (but permit local, fast, available operation).

### 15.2.2 A word of warning: “consistency” means different things
| Term | Domain | Meaning |
|---|---|---|
| **C in ACID** | Transactions (Ch 3) | The database moves from one *valid* state to another, preserving application invariants (it’s mostly the application’s job) |
| **Isolation levels** | Transactions (Ch 3) | What concurrent *transactions* may observe of each other (read committed, snapshot, serializable) |
| **Consistency models** | Replicated objects (this chapter) | What *reads of replicated data* may return relative to writes (linearizable, causal, eventual) |
| **C in CAP** | Replicated objects | Specifically **linearizability** of a single object |
| **Cache consistency** | Caches (Ch 8) | Staleness bound between cache and source |
Always ask: *consistent with respect to what?*

### 15.2.3 The spectrum (strongest to weakest) `[MK]`
```text
 STRONGER (more coordination, higher latency, less available under partition)
   strict serializability / external consistency ← transactions + real-time order (Spanner)
   linearizability                 ← single-copy illusion, real-time order, per object
   sequential consistency          ← one global order consistent with each client's program order (no real-time)
   causal consistency              ← causally related operations seen in order by everyone
   PRAM / FIFO consistency         ← each client's writes seen in order
   session guarantees              ← read-your-writes, monotonic reads, monotonic writes, writes-follow-reads
   eventual consistency            ← if writes stop, replicas converge (no ordering promise)
 WEAKER (little coordination, low latency, available under partition)
```
We’ll define each and the guarantees you can buy at each level.

---

## 15.3 Linearizability `[GK]`

### 15.3.1 The idea
A system is **linearizable** if every operation appears to take effect **instantaneously at some point between its invocation and its response**, and all operations appear to be executed in a **single total order** that respects real-time order: if operation A *completes* before operation B *begins*, then A is ordered before B. It gives the illusion of **one copy** of the data, atomically updated.

**Mental model:** there is one up-to-date copy of a variable; every client, no matter which replica it talks to, behaves as if reading and writing that single copy, with the added rule that once any client sees a new value, no client later sees the old one.

### 15.3.2 Timeline examples (register x, initially 0)
```text
 Valid (linearizable):
   Writer:  |------ write(1) ------|
   Reader1:        |-- read → 1 --|
   Reader2:                          |-- read → 1 --|      (after the write finished, all reads return 1)

 Valid too (the write overlaps both reads; reads may be before/after its linearization point):
   Writer:  |------------- write(1) -------------|
   Reader1:   |- read → 0 -|
   Reader2:                       |- read → 1 -|

 NOT linearizable (new/old inversion):
   Writer:  |------------- write(1) -------------|
   Reader1:      |- read → 1 -|                          ← saw the new value
   Reader2:                         |- read → 0 -|       ← started AFTER Reader1 finished, but saw the OLD value
 (Once someone has seen 1, a later read must not return 0.)

 NOT linearizable (stale read after completed write):
   Writer:  |-- write(1) --|
   Reader:                     |-- read → 0 --|          ← started after write completed
```

### 15.3.3 Why linearizability matters
- **Locks and leader election:** “who is the leader/lock holder” must be single-valued; a stale read could elect two leaders (Ch 14/16).
- **Uniqueness constraints:** claiming a username or booking the last seat requires that two concurrent claims are ordered (one wins).
- **Cross-channel timing:** a user uploads a file (write to storage) and a message to a queue says “file ready”; a consumer reading the storage after receiving the message must see the file (read-after-write across channels).
- **Correctness of coordination services** (etcd, ZooKeeper, Consul) that other systems use to avoid split-brain.

### 15.3.4 What it costs
- **Latency:** a linearizable operation must, in general, involve a **quorum round trip** (or the leader plus synchronous replication). Across regions that is tens to hundreds of ms (Attiya–Welch lower bounds relate latency to network delay).
- **Availability:** during a partition, nodes that cannot reach a quorum **cannot** serve linearizable operations (CAP, §15.7).
- **Throughput:** a single leader or consensus group sequences all operations on the object.
- **Not composable cheaply:** linearizability is local (a system of independently linearizable objects is linearizable per object), but multi-object atomicity needs transactions (strict serializability).

### 15.3.5 Linearizability vs serializability `[MK]`
| | Linearizability | Serializability |
|---|---|---|
| Subject | Single-object operations (register, key) | **Transactions** over many objects |
| Guarantee | Operations ordered consistently with **real time** | Transactions equivalent to *some* serial order; **no real-time constraint** |
| Typical | Distributed systems / replication | Databases / isolation |
| Combined | **Strict serializability** (= serializable + linearizable) | Spanner, FaunaDB, CockroachDB (by default: serializable; with caveats) |
A database can be serializable but allow a transaction to read a state older than a transaction that already committed (no real-time guarantee).

### 15.3.6 How linearizable systems are built
1. **Single leader, all reads and writes through the leader** (with the leader verifying it’s still the leader: via lease/quorum read; a stale leader that serves reads breaks it, Ch 14).
2. **Consensus-replicated state machines** (Raft/Paxos): etcd, ZooKeeper (writes linearizable; plain reads can be stale unless `sync`), Consul, Spanner (Paxos groups).
3. **Quorum with read write-back (ABD protocol):** read from a quorum, then write the value back to a quorum before returning (§15.6).
4. Not by: plain asynchronous replication with replica reads; or leaderless quorums without write-back.

---

## 15.4 Weaker models you can actually use `[MK/GK]`

### 15.4.1 Sequential consistency
All operations appear in some single total order, and each client’s operations appear in that order *as the client issued them* (program order), but the total order need **not** respect real time. Weaker than linearizability: a reader may see stale values as long as everyone agrees on one history. Rarely offered by distributed databases directly; it is the memory model of many multiprocessors (with extra rules).

### 15.4.2 Causal consistency `[GK]`
If operation A **causally precedes** B (A → B: same client order; B read a value written by A; transitively; see happens-before, Ch 14), then **every** replica and client must observe A before B. Operations that are concurrent (not causally related) may be observed in different orders by different clients.
- Fixes Bug 2 (“answer before question”): the answer causally depends on the question, so no one sees the answer without the question.
- **Available under partition:** each partition side can continue serving causally consistent reads and writes; no global coordination required. Mahajan, Alvisi & Dahlin showed causal consistency (with convergence) is among the **strongest models achievable while remaining available** `[Public: “Consistency, Availability, and Convergence”]`.
- **Implementation:** attach dependency metadata (version vectors, dependency lists, or hybrid logical timestamps) to writes; a replica delays making a write visible until all of its dependencies are visible there. Systems: COPS/Eiger research systems, MongoDB causal sessions (with `readConcern: majority` + `writeConcern: majority`), Cosmos DB “session”/“consistent prefix” options, Azure/Cassandra extensions, many CRDT/local-first systems.
- Cost: metadata size, dependency checking, delayed visibility; still can’t enforce uniqueness constraints (Bug 3), because concurrent writes are unordered.

### 15.4.3 PRAM / FIFO consistency
Writes from a single client are seen in the order issued by that client; writes from different clients may be interleaved differently by different observers. Cheap but offers few cross-client guarantees.

### 15.4.4 Session guarantees `[MK]`
Defined by Terry et al. (Bayou), they apply **within one client session** and are exactly the fixes for most user-visible bugs:
| Guarantee | Meaning | Fixes |
|---|---|---|
| **Read-your-writes** | A session’s reads reflect its own earlier writes | Bug 1 (post disappeared) |
| **Monotonic reads** | A session never sees data go back in time | Bug 4 (like count dropping) |
| **Monotonic writes** | A session’s writes are applied in the order issued | Out-of-order edits |
| **Writes-follow-reads** | A write is ordered after the reads that influenced it (causal prefix) | Bug 2 within a session (reply after seeing the question) |
**Implementation** (Ch 9): sticky routing to a replica, session tokens carrying the highest version/LSN seen (replicas serve a read only if they have applied at least that), client-side caches, reading from the leader for the user’s recent writes. These cost little and don’t need global coordination.

### 15.4.5 Eventual consistency `[MK]`
If no new updates are made, all replicas eventually converge to the same value. That’s the *only* promise: no bound on time, no ordering guarantees, reads can return any past value, concurrent writes need a **conflict-resolution** rule (LWW, merge, CRDT). Useful when availability and latency dominate and the data tolerates temporary divergence (counters shown to users, social feeds, caches, DNS, shopping carts with merge).
- **Strong eventual consistency (SEC):** replicas that have received the same set of updates are in the same state, guaranteed by data types whose merge is commutative, associative, and idempotent (**CRDTs**, Ch 20). Convergence without coordination.
- **Bounded staleness:** a stronger-than-eventual promise: reads lag the latest write by at most K versions or T seconds (Cosmos DB’s “bounded staleness”, Spanner stale reads). Quantifying “eventually” turns it into an engineering contract.
- **Probabilistically bounded staleness (PBS):** Bailis et al. measured that real Dynamo-style stores usually converge within tens of ms `[Public]`; the Chapter 9 simulation reproduced the idea: stale-read probability falls rapidly with the time since the write.

---

## 15.5 Testing a system against a model `[GK]`

You cannot argue a system into being linearizable; you **test histories**. A **history** is the list of operations with invocation and response times and values. A **checker** searches for a legal total order consistent with real time and with sequential register semantics (Wing & Gong; Knossos and Elle in Jepsen `[Public]`). The problem is NP-complete in general, but practical for small histories.

**Algorithm (what Lab 1 implements):** maintain the set of operations already linearized and the current register value. Repeatedly pick an operation that is *minimal* (no un-linearized operation finished before it started) and consistent with the register (a read must return the current value; a write sets it). Backtrack on failure; memoize `(set, value)` states. If all operations can be placed, the history is linearizable.

Jepsen-style testing = concurrent clients + a **nemesis** injecting faults (partitions, crashes, clock skew, pauses) + a checker over the recorded history. It has found violations in many well-known databases `[Public: jepsen.io analyses]`.

---

## 15.6 Measured: are quorums linearizable? (Lab 1) `[GK]`

We simulate a 3-replica register with one writer (4 sequential writes) and three readers (3 reads each) over a network with random message delays (2 ms + exponential mean 15 ms per hop), generate 3,000 random histories per configuration, and check each with the linearizability checker. Reads return the highest version among the replies they wait for.
```text
configuration                       W+R>N   histories NOT linearizable   reads that missed a COMPLETED write   monotonic-read violations
W=1,R=1 (fast, sloppy)              no       31.17%                       29.07%                                5.60%
W=2,R=2 (majority quorums)          yes       0.53%                        0.00%                                0.17%
W=3,R=1                             yes      10.87%                        0.00%                                6.20%
W=1,R=3                             yes       0.00%                        0.00%                                0.00%
W=2,R=2 + read write-back (ABD)     yes       0.00%                        0.00%                                0.00%
```
**Read this carefully; it overturns a common belief.**
1. **W=1, R=1 (no quorum overlap):** 31% of histories are not linearizable; in 29% some read began *after a write had completed* and still returned the old value (a plain stale read).
2. **W=2, R=2 (overlapping majority quorums):** reads that start *after* a write completes **never** miss it (0.00% stale-after-complete): that’s what W+R>N buys. **But 0.53% of histories are still not linearizable**: the new/old inversion. A write is in flight, applied on one replica; reader 1’s quorum includes that replica and returns the new value; reader 2 starts afterwards, queries the *other two* replicas, which have not received the write, and returns the old value.
```text
  replicas A,B,C  (write(1) in flight: has reached A only)
  Reader1: asks {A,B} → A has 1 → returns 1      completes
  Reader2:  starts later, asks {B,C} → both have 0 → returns 0     ← time went backwards
```
3. **W=3, R=1:** after the write *completes* every replica has it, so completed writes are never missed, but R=1 reads can flip between a new and an old replica *while the write is in flight*: 10.87% non-linearizable, 6.2% with a client seeing time go backwards.
4. **W=1, R=3:** reading *all* replicas and taking the max is linearizable in this failure-free model: any read that starts after another read finished will see at least what it saw (the replica that had the value still has it).
5. **ABD (read write-back):** the reader writes the value it is about to return back to a write quorum before returning. This forces the new value to become visible to every later read quorum, closing the inversion: 0 of 3,000 histories violate linearizability even at W=R=2.

**Takeaway:** *“W+R>N” makes reads fresh with respect to completed writes, but alone does not make a Dynamo-style system linearizable.* Linearizability needs either a leader/consensus (Raft, Paxos) or extra coordination (read write-back, quorum-leased reads). This is why Cassandra documents that `QUORUM/QUORUM` is not linearizable and offers lightweight transactions (Paxos) for specific operations `[Public]`.
(This simulation excludes node failures and uses a single writer; with multiple writers, ABD adds a timestamp-discovery read phase before writes.)

---

## 15.7 The CAP theorem `[MK/GK]`

### 15.7.1 Statement (Gilbert & Lynch, 2002; conjecture by Brewer, 2000)
In an **asynchronous network**, a read/write data object cannot simultaneously provide:
- **C: Consistency** = **linearizability** (every read sees the latest completed write),
- **A: Availability** = every request received by a **non-failing node** eventually gets a (non-error) response, and
- **P: Partition tolerance** = the system continues to operate despite the network dropping arbitrarily many messages between nodes.
**Stated as a choice:** *when a network partition occurs*, you must give up either consistency or availability.

### 15.7.2 Proof sketch
Two nodes A and B hold a register (value 0). A partition separates them. A client writes 1 to A. Another client reads from B.
- If B **answers** (available), it can only return 0 (it cannot know about the write): the read is stale → **not linearizable**.
- If B **refuses to answer** (waits or errors) until the partition heals, it is **not available**.
Either way you lose one. Therefore a linearizable, always-available system cannot tolerate partitions.

### 15.7.3 What the letters really mean (and the misconceptions)
- **P is not optional.** Networks do partition (Ch 14). A system that “gives up P” must assume the network never partitions, i.e., it is a single node or it breaks arbitrarily when partitions occur. So the practical choice is **CP vs AP *during a partition***. “CA” systems are single-node databases (or systems that become inconsistent/unavailable in unspecified ways under partitions).
- **CAP says nothing about normal operation.** When there is no partition you can have C and A (but you pay in latency: PACELC, §15.8).
- **“Consistency” in CAP is linearizability,** not ACID-C and not “eventual”.
- **“Availability” in CAP is a strong, total property:** *every* request to *any* non-failing node must succeed. Real systems’ “high availability” (e.g., “most requests succeed via a majority”) is weaker. A CP system with a majority side is still **available to clients who can reach the majority**.
- **Not binary per system:** different operations, tables, or configurations choose differently (Cassandra `QUORUM` vs `ONE`; MongoDB write concerns; DynamoDB strongly consistent vs eventually consistent reads).
- **Partial partitions and failures** (A–B broken, but both reach C) complicate the picture; real behavior depends on the protocol.
- **Kleppmann’s critique:** labels “CP” and “AP” are too coarse; better to describe exact guarantees (e.g., “linearizable writes via Raft, stale reads from followers allowed”).

### 15.7.4 Behavior in a partition: what the choices look like (Lab 1, part 2)
5 nodes split 3 | 2; each side receives 500 writes over 5,000 keys:
```text
CP (majority quorum) : 500 writes accepted (majority side), 500 writes REJECTED (minority side is unavailable); after healing 0 conflicting keys
AP (accept anywhere) : 1000 writes accepted (both sides), 0 rejected; but 42 keys were updated on BOTH sides -> after healing, last-write-wins silently drops one acknowledged update per conflicting key
```
- **CP** keeps a single history: the minority side refuses writes (clients there see errors or must retry elsewhere), so there is nothing to reconcile.
- **AP** keeps accepting everywhere, then must **reconcile** divergent histories. With LWW, 42 acknowledged updates were silently lost; with CRDTs or siblings, none are lost but the application must merge. The 42 conflicts out of 1,000 writes depend on workload overlap; hot keys conflict far more.

### 15.7.5 Examples
| System / configuration | Under partition | Notes |
|---|---|---|
| etcd, ZooKeeper, Consul (Raft/Zab) | **CP**: minority side cannot write or do linearizable reads | Reads can be configured stale for availability |
| Spanner, CockroachDB | **CP** (with very high practical availability due to replication) | Strictly serializable transactions |
| HBase, MongoDB (majority writes) | **CP**-leaning | A failed primary triggers an election; writes unavailable for seconds |
| Cassandra, Riak, DynamoDB (default eventual reads) | **AP**-leaning | Sloppy quorums, hinted handoff, LWW or siblings |
| Single PostgreSQL primary | “CA” = no distribution; availability = the node | Replicas async |
| Redis Cluster | Neither cleanly; minority partitions stop but acked writes can be lost on failover | Async replication |

---

## 15.8 PACELC: the rest of the story `[MK/GK]`

Daniel Abadi (2012): CAP only discusses partitions, which are rare. The everyday trade-off is **latency vs consistency**:
> **If Partition: Availability or Consistency; Else (normal operation): Latency or Consistency.**

Replication requires communication; synchronizing replicas before acknowledging costs round trips. Even with no partition, a system either (a) responds after local work (low latency, replicas may lag: **EL**) or (b) waits for coordination (consistent, slower: **EC**).

| System | If partition | Else | Label |
|---|---|---|---|
| DynamoDB (eventual), Cassandra (default), Riak | A | L | **PA/EL** |
| Spanner, VoltDB, HBase, FaunaDB | C | C | **PC/EC** |
| MongoDB (defaults) | A (or C per config) | C (primary reads) | **PA/EC** (config-dependent) |
| PNUTS (Yahoo) | C | L | **PC/EL** |
| MySQL/Postgres with async replicas | (single leader) | L for replica reads | typically **PC/EL** |

### 15.8.1 Latency numbers (cross-region)
Typical inter-region RTTs `[Public/approximate]`: same AZ 0.5 ms; cross-AZ ~1–2 ms; US-East ↔ US-West ~60–70 ms; US-East ↔ EU ~80–90 ms; US ↔ Asia 150–250 ms.
*Example.* Three replicas in us-east (leader), us-west, and eu. A linearizable write needs a majority = leader + one other: the nearest, us-west at 65 ms RTT. So **each write costs ≈ 65 ms** plus local work, regardless of how fast the hardware is. A user in Europe writing to the us-east leader pays ~85 ms to reach the leader **plus** the 65 ms replication round trip: ~150 ms. A system that acknowledges after the local write and replicates asynchronously answers in ~2 ms but can lose acknowledged writes on failover (Ch 9) and serve stale reads elsewhere. **Strong consistency across regions costs at least one wide-area RTT per write.**
Mitigations: **leader placement near the writers** (follower reads for read-heavy), **per-region data ownership** (partition by user region so writes are local), **bounded-staleness reads from local replicas**, **hierarchical consistency** (strong within a region, async across regions), **commit wait** vs **Paxos leader leases**.

---

## 15.9 Consistency and transactions `[GK]`

Transactions add multi-object atomicity and isolation; combine them with replication consistency to get a full picture.
```text
 strict serializability  (serializable + real-time order; Spanner, FaunaDB)
   ├─ serializability  (equivalent to a serial order; e.g., Postgres SERIALIZABLE, CockroachDB default)
   └─ snapshot isolation (each txn reads a consistent snapshot; allows write skew)
        └─ repeatable read / cursor stability / read committed / read uncommitted (weaker; more anomalies)
```
Anomalies by level (Ch 3 recap): dirty reads, non-repeatable reads, phantoms, **lost update**, **write skew** (two transactions each read an overlapping set and write disjoint items, violating an invariant: two doctors on call both go off-call).
In distributed databases, snapshot isolation is cheap to provide with multi-version storage and timestamps; full serializability needs conflict detection across nodes (certification, locking, or timestamp ordering) and, for external consistency, bounded-uncertainty clocks or consensus. Read the vendor’s documentation: “ACID” on the box may mean snapshot isolation, and “strong consistency” may mean only per-key linearizability.

---

## 15.10 Designing with weaker consistency `[MK]`

### 15.10.1 A decision procedure
For each feature, ask:
1. **What invariant must never be violated?** (unique username, non-negative balance, no double-booking, no negative stock.)
2. **Can the invariant be preserved without coordination?** (**I-confluence**: Bailis et al. showed that many invariants, such as non-negative *counters that only increment*, foreign keys under insert-only, can be maintained without coordination, but **uniqueness** and **bounded decrements** (balance ≥ 0) generally require it.) `[Public]`
3. **How stale may a read be, for whom, and what is the cost of being wrong?** (a stale “likes” count is harmless; a stale “seat available” leads to a failed purchase or double-booking.)
4. **What is the weakest model satisfying 1–3?** (Session guarantees? Causal? Bounded staleness? Linearizable only for specific operations?)

### 15.10.2 Matrix for SocialSphere
| Feature | Requirement | Mechanism |
|---|---|---|
| Show my own post after publishing | Read-your-writes | Session token / read from leader for N s |
| Comments thread order | Causal order | Causal metadata or single-partition per thread (leader order) |
| Unique username | Linearizable claim | Consensus-based or single-owner registry; unique index on one linearizable store |
| Like counters | Monotonic reads; approximate counts OK | CRDT counter or sharded counters + sticky sessions |
| Payments and balances | Linearizable + transactional | Single-leader/consensus DB; serializable transactions |
| Feed | Eventual with bounded staleness | Async replicas, caches with TTL |
| Presence (“online now”) | Best effort, ephemeral | AP, TTL-based |
| Inventory at checkout | Linearizable decrement or reservation | Transactional DB; reservations with expiry |
| Notifications | At-least-once, idempotent | Queue + dedupe |

### 15.10.3 Compensation: “memories, guesses, and apologies” `[Public: Pat Helland]`
When coordination is too costly, let operations proceed on local knowledge (a *guess*), and **repair violations afterwards** with business processes: overbook flights then compensate; oversell and issue refunds; allow overdrafts and charge fees; detect duplicates and merge. This trades guaranteed invariants for availability and speed, and is how many real businesses work. Requirements: detect violations, have a compensation procedure, and bound the damage (limits on overbooking).

### 15.10.4 CALM and monotonicity `[GK]`
The **CALM theorem** (Hellerstein): a program has a consistent, coordination-free distributed implementation **if and only if it is monotonic** (outputs only grow as inputs arrive; no need to retract earlier conclusions). Add-only sets, counters that only increment, and unions are monotonic; “is this set empty?”, “count is exactly N”, and “first writer wins” are not and need coordination. Design tip: **structure data to be monotonic** (append events, tombstone deletes) and push the non-monotonic decision to the few places that need coordination.

### 15.10.5 Practical patterns
- **Route reads to the right place:** leader for recently-written data; replicas for older data.
- **Idempotent and commutative operations** (set-add, max, counters per node) to make retries and reordering harmless.
- **Single-writer principle:** assign each object to one writer (home region/shard); it’s linearizable locally with no cross-region coordination.
- **Reservations/escrow:** split a bounded resource (stock=1,000) into per-region budgets (500/500); each region decrements locally without coordination; rebalance occasionally.
- **Make the unavoidable coordination small:** coordinate only on the uniqueness claim (a tiny linearizable service), not on every write.

---

## 15.11 What real systems promise `[GK]`

| System (default / option) | Guarantee | Notes |
|---|---|---|
| **PostgreSQL / MySQL, single primary** | Serializable/snapshot per isolation level; **replica reads are stale** (async) | Read-your-writes needs routing/tokens (Ch 9) |
| **MongoDB** | `writeConcern: majority` + `readConcern: linearizable` for linearizable reads on primary; causal consistency sessions; default reads may be stale/rolled back | Configuration decides |
| **Cassandra / ScyllaDB** | Tunable: `ONE`, `QUORUM`, `LOCAL_QUORUM`, `ALL`; **not linearizable** except via lightweight transactions (Paxos) | QUORUM/QUORUM gives read-after-completed-write, not linearizability (Lab 1) |
| **DynamoDB** | Eventually consistent reads (default), strongly consistent reads (leader), transactions; global tables LWW | |
| **Google Spanner** | External consistency (strict serializability) via TrueTime; stale reads for latency | |
| **CockroachDB / YugabyteDB / TiDB** | Serializable (default for Cockroach); HLC; follower reads for bounded staleness | |
| **etcd / ZooKeeper / Consul** | Linearizable writes; etcd reads linearizable by default; ZooKeeper reads can be stale unless `sync()` | |
| **Redis (replica / cluster)** | Async replication: stale reads, lost acked writes on failover | `WAIT` improves durability, not linearizability |
| **Kafka** | Per-partition order; committed (HW) reads; consumers may lag | Not a linearizable register store |
| **Amazon S3** | Strong read-after-write for objects and LIST (since 2020) `[Public]` | Cross-region replication is async |
| **DNS, CDNs, caches** | Eventual / TTL-bounded | |
**Always verify against current documentation and, for critical use, run a Jepsen-style test.**

---

## 15.12 Failure modes of consistency assumptions

| Failure | Mechanism | Mitigation |
|---|---|---|
| **Stale read after write** | Reading async replica or stale cache | Read-your-writes tokens, leader reads, cache invalidation |
| **Time going backwards** | Reads from different replicas | Monotonic reads via sticky sessions/tokens |
| **Causality violation** | Replies visible before questions | Causal metadata or per-thread partition |
| **Duplicate uniqueness claims** | Concurrent registrations in separate regions | Linearizable registry; reservation; unique index on one store |
| **Lost updates (LWW)** | Concurrent writers, skewed clocks | Version vectors, CRDTs, optimistic CAS |
| **Write skew** | Snapshot isolation | Serializable isolation or explicit locking/constraints |
| **Split brain** | Two leaders in a partition | Majority quorums, epochs, fencing (Ch 9, 14, 16) |
| **Quorum misconception** | Assuming `W+R>N` is linearizable | Use consensus, or ABD-style write-back, or LWT |
| **Stale leader reads** | Old leader serving reads after demotion | Lease-based or quorum-checked reads |
| **Cross-region latency surprise** | Linearizable writes pay WAN RTT | Regional ownership, async replication where allowed |
| **Silent downgrade** | Failover to async/stale replica | Monitor replica state; fail closed for critical paths |
| **Inconsistent caches** | Cache and DB disagree | Invalidation discipline, TTLs, versioned keys (Ch 8) |
| **Misreading vendor claims** | “Strong consistency” = per-key only | Read the docs; test with Jepsen/Elle |

---

## 15.13 Labs

### Lab 1: Linearizability checker and quorum registers; CAP behavior (Java 17+)
```java
import java.util.*;

public class ConsistencyLab {
    static final Random R = new Random(77);
    static double exp(double mean) { return -Math.log(1 - R.nextDouble()) * mean; }
    static double hop() { return 2 + exp(15); }                                   // one-way network delay in ms

    static class Op { boolean write; int value; double start, end; int client; int replies; int best = 0; boolean done; int phase; }
    record Ev(double t, long seq, Runnable run) {}

    static class Sim {
        final int N = 3, W, Rq; final boolean writeBack;
        final int[] replicaVer = new int[N];                                       // each replica stores the highest version it has seen (value == version)
        final PriorityQueue<Ev> q = new PriorityQueue<>(Comparator.comparingDouble(Ev::t).thenComparingLong(Ev::seq));
        final List<Op> history = new ArrayList<>(); long seq = 0; double now = 0;
        Sim(int w, int r, boolean wb) { W = w; Rq = r; writeBack = wb; }
        void at(double t, Runnable r) { q.add(new Ev(t, seq++, r)); }

        void startWrite(Op op, Runnable next) {                                    // write: send to all replicas, complete after W acks
            op.start = now;
            for (int i = 0; i < N; i++) { final int ri = i; at(now + hop(), () -> { replicaVer[ri] = Math.max(replicaVer[ri], op.value);
                at(now + hop(), () -> { if (!op.done && ++op.replies == W) { op.done = true; op.end = now; next.run(); } }); }); }
        }
        void startRead(Op op, Runnable next) {                                     // read: ask all replicas, after R replies return the highest version seen
            op.start = now;
            for (int i = 0; i < N; i++) { final int ri = i; at(now + hop(), () -> { int v = replicaVer[ri];
                at(now + hop(), () -> { if (op.phase != 0) return; op.best = Math.max(op.best, v);
                    if (++op.replies == Rq) { op.phase = 1; op.value = op.best;
                        if (!writeBack) { op.done = true; op.end = now; next.run(); }
                        else { op.replies = 0; for (int j = 0; j < N; j++) { final int rj = j; final int val = op.value;      // ABD: write the value back to a quorum first
                            at(now + hop(), () -> { replicaVer[rj] = Math.max(replicaVer[rj], val);
                                at(now + hop(), () -> { if (!op.done && ++op.replies == W) { op.done = true; op.end = now; next.run(); } }); }); } } } }); }); }
        }
        void client(int id, boolean writer, int count, double firstStart) {
            int[] n = {0};
            Runnable[] step = new Runnable[1];
            step[0] = () -> {
                if (n[0] >= count) return;
                Op op = new Op(); op.client = id; op.write = writer; if (writer) op.value = n[0] + 1; n[0]++; history.add(op);
                Runnable after = () -> at(now + exp(25), step[0]);
                if (writer) startWrite(op, after); else startRead(op, after);
            };
            at(firstStart, step[0]);
        }
        List<Op> run() { while (!q.isEmpty()) { Ev e = q.poll(); now = e.t(); e.run().run(); } return history; }
    }

    /** Wing & Gong style search: is there a total order of the operations that respects real time and register semantics? */
    static boolean linearizable(List<Op> h) {
        int n = h.size(); Set<Long> bad = new HashSet<>();
        return dfs(h, 0, 0, n, bad);
    }
    static boolean dfs(List<Op> h, int mask, int reg, int n, Set<Long> bad) {
        if (mask == (1 << n) - 1) return true;
        long key = ((long) mask << 8) | reg; if (bad.contains(key)) return false;
        for (int i = 0; i < n; i++) {
            if ((mask & (1 << i)) != 0) continue;
            Op a = h.get(i); boolean minimal = true;
            for (int j = 0; j < n && minimal; j++) if (j != i && (mask & (1 << j)) == 0 && h.get(j).end < a.start) minimal = false;   // j must come before i
            if (!minimal) continue;
            if (a.write) { if (dfs(h, mask | (1 << i), a.value, n, bad)) return true; }
            else if (a.value == reg) { if (dfs(h, mask | (1 << i), reg, n, bad)) return true; }
        }
        bad.add(key); return false;
    }

    static void registers() {
        int histories = 3000;
        System.out.println("== 1) Is a replicated register linearizable? N=3 replicas, 1 writer (4 writes), 3 readers (3 reads each), random message delays. ==");
        System.out.println("   Checked by brute-force search for a legal total order (Wing-Gong). " + histories + " random histories per configuration.");
        System.out.println("configuration                       W+R>N   histories NOT linearizable   reads that missed a COMPLETED write   monotonic-read violations");
        Object[][] cfgs = {{"W=1,R=1 (fast, sloppy)", 1, 1, false}, {"W=2,R=2 (majority quorums)", 2, 2, false}, {"W=3,R=1", 3, 1, false}, {"W=1,R=3", 1, 3, false}, {"W=2,R=2 + read write-back (ABD)", 2, 2, true}};
        for (Object[] c : cfgs) {
            int w = (Integer) c[1], r = (Integer) c[2]; boolean wb = (Boolean) c[3]; int nonLin = 0, staleHistories = 0, monoHistories = 0;
            for (int k = 0; k < histories; k++) {
                Sim s = new Sim(w, r, wb); s.client(0, true, 4, 20);
                for (int c2 = 1; c2 <= 3; c2++) s.client(c2, false, 3, R.nextDouble() * 60);
                List<Op> h = s.run();
                if (!linearizable(h)) nonLin++;
                boolean stale = false, mono = false;
                for (Op a : h) if (!a.write) {
                    for (Op b : h) if (b.write && b.end < a.start && a.value < b.value) stale = true;                        // read began after write finished but returned older value
                    for (Op b : h) if (!b.write && b.client == a.client && b.end < a.start && a.value < b.value) mono = true;    // same client went back in time
                }
                if (stale) staleHistories++; if (mono) monoHistories++;
            }
            System.out.printf("%-35s %-6s  %6.2f%%                      %6.2f%%                              %6.2f%%%n", c[0], (w + r > 3) ? "yes" : "no", 100.0 * nonLin / histories, 100.0 * staleHistories / histories, 100.0 * monoHistories / histories);
        }
    }

    static void partition() {
        System.out.println("\n== 2) CAP in one picture. 5 nodes; a partition splits them 3 | 2. Each side receives 500 writes to random keys out of 5,000. ==");
        Random r = new Random(5); int keys = 5000, writesPerSide = 500;
        // CP: a write needs a majority (3 of 5), so the 2-node side cannot commit anything
        // AP: both sides accept every write; after the partition heals, conflicting keys are resolved last-write-wins
        Set<Integer> touchedA = new HashSet<>(), touchedB = new HashSet<>();
        for (int i = 0; i < writesPerSide; i++) { touchedA.add(r.nextInt(keys)); touchedB.add(r.nextInt(keys)); }
        int conflicts = 0; for (int k : touchedA) if (touchedB.contains(k)) conflicts++;
        System.out.printf("CP (majority quorum) : %d writes accepted (majority side), %d writes REJECTED (minority side is unavailable); after healing 0 conflicting keys%n", writesPerSide, writesPerSide);
        System.out.printf("AP (accept anywhere) : %d writes accepted (both sides), 0 rejected; but %d keys were updated on BOTH sides -> after healing, last-write-wins silently drops one acknowledged update per conflicting key%n", 2 * writesPerSide, conflicts);
    }

    public static void main(String[] a) { registers(); partition(); }
}
```
**Sample output**
```text
== 1) Is a replicated register linearizable? N=3 replicas, 1 writer (4 writes), 3 readers (3 reads each), random message delays. ==
   Checked by brute-force search for a legal total order (Wing-Gong). 3000 random histories per configuration.
configuration                       W+R>N   histories NOT linearizable   reads that missed a COMPLETED write   monotonic-read violations
W=1,R=1 (fast, sloppy)              no       31.17%                       29.07%                                5.60%
W=2,R=2 (majority quorums)          yes       0.53%                        0.00%                                0.17%
W=3,R=1                             yes      10.87%                        0.00%                                6.20%
W=1,R=3                             yes       0.00%                        0.00%                                0.00%
W=2,R=2 + read write-back (ABD)     yes       0.00%                        0.00%                                0.00%

== 2) CAP in one picture. 5 nodes; a partition splits them 3 | 2. Each side receives 500 writes to random keys out of 5,000. ==
CP (majority quorum) : 500 writes accepted (majority side), 500 writes REJECTED (minority side is unavailable); after healing 0 conflicting keys
AP (accept anywhere) : 1000 writes accepted (both sides), 0 rejected; but 42 keys were updated on BOTH sides -> after healing, last-write-wins silently drops one acknowledged update per conflicting key
```
**Experiments**
1. Reproduce the table. Then raise the number of reads per reader (to 5) and shrink the network delay to 1 ms + exp(3 ms). How do the non-linearizable percentages change for W=R=2?
2. Hand-craft three small histories (like §15.3.2) as `Op` lists and run the checker on them; confirm it reports valid/invalid as you expect.
3. Add **multiple writers** (two writer clients with values from disjoint sets). A single version counter no longer works; implement timestamped writes `(counter, writerId)` with a timestamp-discovery phase (ABD’s first write phase) and re-run the checker with a register that stores `(ts, value)` pairs.
4. Add **failures**: each message lost with probability 3% and operations time out (excluded from history, or recorded as “unknown outcome” operations whose effect may or may not have happened). How does the checker treat pending operations?
5. Add the **session-guarantee tracking** from §15.4.4: each reader remembers the highest version it has seen and refuses to return a lower one (re-reads). Measure the fraction of reads that must retry and verify monotonic-read violations drop to 0 without making the system linearizable.
6. In part 2, implement **CRDT** values (a grow-only set of ids per key) for the AP side. After healing, show that no acknowledged update is lost, and compare with LWW.

### Lab 2: A Jepsen-style test (needs Docker; not executed in the book’s sandbox)
Start a 3- or 5-node etcd cluster (`docker compose`), run concurrent clients doing reads, writes, and compare-and-set on a single key while a script partitions nodes with `iptables`/Toxiproxy, record the history (invoke/ok/fail/info with timestamps), and run it through a checker (Knossos/Elle, or port the Lab 1 checker). Then repeat with a Cassandra cluster at `QUORUM`/`QUORUM` and observe (rarely) non-linearizable histories. **Questions:** Which operations must be treated as “unknown” after a timeout? Why are crashed clients’ operations tricky for checkers?

### Lab 3: Classify and choose (paper)
For each system pick the weakest sufficient model and mechanism; state the invariant: (a) airline seat map, (b) collaborative document editing, (c) bank transfer, (d) ride-hailing driver location, (e) “unread messages” badge, (f) a feature-flag service, (g) a distributed rate limiter, (h) leaderboards.

---

## 15.14 Interview perspective

- Replace “consistent” with the **exact guarantee**: *read-your-writes, monotonic reads, causal, linearizable, bounded staleness.*
- State CAP correctly: **“in a partition, choose linearizability or availability; P isn’t optional.”** Add PACELC: **“otherwise, you trade latency for consistency.”**
- Give **per-feature choices**: money and uniqueness → linearizable/serializable; feeds, counters → eventual with session guarantees.
- Know that **W + R > N is not linearizable**; mention leader-based or consensus-based approaches for linearizability.
- Quantify the **cross-region cost**: a linearizable write pays at least a WAN round trip to the nearest majority.
- Mention **compensation** and **idempotency** as ways to avoid coordination, and **CRDTs** for convergent merges (Ch 20).
- When asked “CP or AP?”, answer with behavior: *“Writes need a majority (Raft); the minority side rejects writes; reads may be served stale from followers if allowed.”*

---

## 15.15 Common misconceptions

1. **“CAP: pick any two.”** P isn’t optional; the choice is C vs A *during a partition*.
2. **“Consistency in CAP is the same as ACID’s C.”** It’s linearizability.
3. **“My database is CP/AP.”** It depends on configuration and operation.
4. **“`W+R>N` gives strong consistency.”** It gives freshness for completed writes, not linearizability (0.53% non-linearizable histories in the lab).
5. **“Eventual consistency means data is wrong.”** It means temporarily divergent; with the right types it converges without loss.
6. **“Strong consistency is always better.”** It costs latency (a WAN RTT per write) and availability.
7. **“Serializable = linearizable.”** Different dimensions (transactions vs real-time single-object order).
8. **“Causal consistency requires global coordination.”** It is achievable without it, even under partitions.
9. **“Availability in CAP means 99.99% uptime.”** It means every request to a live node gets a response.
10. **“Reading from the leader is always linearizable.”** Not if the leader is stale (needs lease or quorum check).
11. **“If there is no partition, there is no trade-off.”** PACELC: latency vs consistency remains.
12. **“Last-write-wins is a consistency model.”** It’s a conflict-resolution policy that can lose data.

---

## 15.16 Chapter summary

- **Consistency models** define what reads may return; they form a spectrum: strict serializability / linearizability → sequential → causal → PRAM → session guarantees → eventual. Stronger means more coordination, more latency, less availability.
- **Linearizability** = single-copy illusion respecting real time; needed for locks, leader election, uniqueness; costs a quorum round trip (WAN RTT across regions) and availability under partition. **Serializability** is a different axis (transactions); **strict serializability** combines them.
- **Causal consistency** and **session guarantees** (read-your-writes, monotonic reads/writes, writes-follow-reads) fix most user-visible bugs cheaply and remain available under partition.
- **Eventual consistency** only promises convergence; add **CRDTs**/bounded staleness to make it useful and quantifiable.
- **Quorums:** with overlapping majorities (W=R=2, N=3) reads never missed a completed write, yet **0.53% of histories were not linearizable** (new/old inversion); W=1,R=1 was non-linearizable 31%; **read write-back (ABD)** fixed it (0%). Linearizability needs consensus or extra coordination.
- **CAP:** during a partition choose linearizability (CP: minority side rejects 500 writes) or availability (AP: all 1,000 accepted, 42 keys diverged and LWW dropped updates). **PACELC:** even without partitions, trade latency against consistency.
- **Design with the weakest sufficient guarantee:** identify the invariant (I-confluence), use monotonic structures (CALM), compensation (apologies), single-writer ownership, escrow, and small islands of coordination.
- Verify vendor claims by reading docs and **testing histories** (Jepsen/Elle/Knossos).

### Mental models
| Concept | Mental model |
|---|---|
| Linearizability | One whiteboard in the room; whoever walks in sees the latest writing, and nobody ever sees an erased message reappear |
| Sequential consistency | Everyone agrees on the order of the minutes, but the minutes may be old |
| Causal consistency | You never read the answer before the question |
| Eventual consistency | Gossip: everyone eventually hears everything, in no promised order |
| Read-your-writes | Your own mailbox always shows letters you just posted |
| CAP | If the phone line between two offices is cut, either stop answering questions or answer from possibly old files |
| PACELC | Even with the line working, asking the other office takes time |
| ABD write-back | Before telling anyone what you read, make sure a quorum has also seen it |

---

## 15.17 Exercises

### A. Conceptual
1. Place these in order from strongest to weakest: eventual, linearizable, causal, sequential, read-your-writes. Give a one-line definition of each.
2. Explain the four meanings of “consistency” in the table of §15.2.2.
3. Define linearizability using the “point between invocation and response” idea. Draw a valid and an invalid history.
4. Why is linearizability required for leader election and uniqueness constraints but not for like counters?
5. Compare linearizability and serializability. Give an example database behavior that is serializable but not linearizable.
6. What is causal consistency and why can it be implemented without global coordination?
7. Name and define the four session guarantees; match each to a user-visible bug.
8. What does “strong eventual consistency” add to eventual consistency? What kind of data types are needed?
9. Explain the new/old inversion for W=R=2 quorums using a diagram. How does read write-back fix it?
10. State the CAP theorem precisely and give the proof sketch. What do C, A, and P mean?
11. Why is “CA” not a meaningful choice for a distributed system?
12. Give three things CAP does *not* say.
13. State PACELC and classify Cassandra (default), Spanner, and MongoDB (default).
14. Why does a cross-region linearizable write cost at least one WAN RTT? Give mitigations.
15. What is I-confluence and which invariants typically require coordination?
16. Explain CALM in plain words with an example monotonic and a non-monotonic computation.
17. What is compensation (apology-oriented design)? Give two real-world examples.
18. How does a Jepsen-style test work? What is the role of the nemesis and the checker?
19. Why can stale leader reads break linearizability, and how do systems prevent it?
20. What is bounded staleness? How would you implement “reads at most 5 seconds stale”?

### B. Calculations / Reasoning
21. For each history decide linearizable or not (register x, initial 0). Intervals are `[start,end]` in ms.
   (a) W1: write(1) [0,10]; R1: read→1 [12,15]; R2: read→0 [20,25].
   (b) W1: write(1) [0,30]; R1: read→1 [5,10]; R2: read→0 [15,20].
   (c) W1: write(1) [0,30]; R1: read→1 [5,10]; R2: read→0 [2,4]; R3: read→1 [12,14].
   (d) W1: write(1) [0,10]; W2: write(2) [5,15]; R1: read→2 [20,25]; R2: read→1 [30,35].
   (e) W1: write(1) [0,10]; W2: write(2) [20,30]; R1: read→1 [12,18]; R2: read→2 [35,40].
22. N=5. List (W,R) pairs with W+R>N and, for each, the number of node failures tolerated for writes and for reads. Which pair is best for 90% reads? 90% writes?
23. Three regions: us-east (leader), us-west (RTT 65 ms to east), eu (RTT 85 ms to east, 140 ms to west). Compute the commit latency of a linearizable write at the leader with a majority quorum. What if the leader moves to eu? What if you require all three (W=3)?
24. A system serves 20,000 reads/s. Moving from async-replica reads (1 ms, stale) to leader-quorum reads (65 ms) changes concurrency needs by Little’s Law. How many concurrent in-flight reads in each case? What does this imply for the connection/thread pool?
25. Two doctors on call; invariant “at least one on call”. Under snapshot isolation both transactions read both rows (on call), each sets themselves off-call, both commit. Show the write-skew sequence with timestamps and two ways to prevent it.
26. Escrow: inventory 1,000 across 4 regions with per-region escrow 250. Region sales: R1 330, R2 120, R3 200, R4 90 in a day. Which regions need coordination and how much stock must move?
27. With a PBS-style model, replica lag after a write is exponential with mean 40 ms. What is the probability a read at 100 ms after the write is stale (single random replica)? At 500 ms? At what time is staleness < 0.1%?
28. A causal-consistency implementation attaches a vector clock with one entry per datacenter (3). Each write adds 24 bytes of metadata. At 50,000 writes/s, how much extra metadata per second? Compare to a scheme with one entry per client (10 M clients) in the worst case.

### C. Design
29. For SocialSphere’s four bugs, specify the minimal mechanism for each and its cost.
30. Design username uniqueness across three regions. Compare (a) global consensus registry, (b) hash username → home region, (c) optimistic claim with later conflict resolution. Latency, availability, and user experience of each.
31. Design a “last read position” (read receipts) feature that must never go backward for a user across devices, without linearizable storage.
32. Design a shopping cart that is available under partition and loses no item additions. Specify the data type, merge, and how deletes are handled.
33. Choose the consistency configuration for a Cassandra-backed order history and a Cassandra-backed session store; justify `LOCAL_QUORUM` vs `ONE` and when LWT is needed.
34. Design the multi-region strategy for a bank ledger: consistency requirements, replication, failure behavior, and latency targets.

### D. Debugging
35. After a failover, a client reads a value that was previously overwritten. List three ways this can happen with a “consistent” database and the fix for each.
36. Two services use a Redis lock with TTL for “exactly one worker”; occasionally both run. Explain with timing and propose a fix (link to Ch 14).
37. A cache-aside system shows a user’s old email for minutes after update on one app server but not others. Which consistency guarantee is violated, and what are the options?
38. A team claims their Cassandra QUORUM reads are linearizable. Design a test that could disprove it.
39. A dashboard counter shows numbers going up and down on refresh. Which guarantee is missing and what is the cheapest fix?
40. A system promised “read committed” but an audit shows that a customer’s balance computed in two reads within a transaction was inconsistent. Which anomaly is this, and what isolation level fixes it?

### E. Interview-style
41. “Explain CAP with an example and tell me where Cassandra and Spanner sit.”
42. “How would you design a system that is strongly consistent but available in multiple regions?”
43. “What is the difference between eventual consistency and causal consistency?”

### F. Teach-back
44. Explain linearizability to a non-engineer with a shared whiteboard analogy, including the “never goes backwards” rule.

---

## 15.18 Full answer key

**1.** Strongest to weakest: linearizable → sequential → causal → read-your-writes (a session guarantee) → eventual. Linearizable: single-copy illusion respecting real time. Sequential: one total order consistent with each client’s order (no real-time). Causal: causally related ops seen in order. Read-your-writes: a session sees its own writes. Eventual: replicas converge if writes stop.

**2.** ACID-C: invariants preserved by transactions. Isolation: what concurrent transactions may observe. Consistency models: what reads of replicated data may return. CAP-C: linearizability. (Cache consistency: staleness relative to source.)

**3.** An operation takes effect atomically at some instant between its start and end, and the instants form a total order consistent with real time. Valid: write(1) [0,10]; read→1 [12,15]. Invalid: read→1 [5,10] then a later-starting read→0 [15,20] while write(1) still pending (time goes backwards).

**4.** Leader election/uniqueness need all participants to agree on a single winner; a stale read or concurrent claims create two winners. Like counters can be approximate and converge, so weak consistency suffices.

**5.** Linearizability: single-object, real-time order. Serializability: transactions behave as some serial order with no real-time constraint. Example: a serializable database may return a snapshot older than a transaction that already committed (the serial order places the reader first), violating real-time order.

**6.** Operations that are causally related are seen in order; concurrent ones may differ. Each replica can enforce this locally with dependency metadata, delaying visibility until dependencies are present; no global agreement on a total order is needed, so partitions don’t block it.

**7.** Read-your-writes (post disappears), monotonic reads (like count goes down), monotonic writes (edits applied out of order), writes-follow-reads (reply appears before the question/causal prefix).

**8.** SEC guarantees that replicas that received the same set of updates are in the same state (deterministic convergence), using CRDTs or commutative/associative/idempotent merge operations (counters, sets, registers with merge).

**9.** Diagram in §15.6: write(1) reached only A; Reader1 queries {A,B}, returns 1; Reader2 later queries {B,C}, returns 0 → time goes backwards. With write-back, Reader1 writes 1 to a quorum (e.g., {A,B}) before returning, so any later read quorum {B,C} intersects at B and sees 1.

**10.** In an asynchronous network, a read/write register can’t be linearizable (C), always respond at every non-failing node (A), and tolerate arbitrary message loss (P) simultaneously. Proof: partitioned nodes A,B; write to A, read from B; B either returns stale (violates C) or doesn’t return (violates A).

**11.** Partitions can happen in any real network, so the system must tolerate them (P). Dropping P means assuming a perfectly reliable network, which is not available outside a single node. So the real choice is C vs A during partitions.

**12.** CAP doesn’t say: there is a trade-off in normal operation (that’s PACELC); that systems are permanently CP or AP (it’s per operation/config); that ACID-C or eventual consistency is what C means; that availability equals uptime; it doesn’t cover latency.

**13.** PACELC: if partition, A or C; else, L or C. Cassandra (default): PA/EL. Spanner: PC/EC. MongoDB (default): PA/EC (config-dependent; writes via primary).

**14.** A majority quorum must include at least one other region for a 3-region deployment; the leader must wait for that round trip. Mitigations: leader near writers, per-region ownership, local follower reads (bounded stale), async cross-region replication where allowed, fewer cross-region quorums (hierarchical), Paxos leader leases for reads.

**15.** I-confluence: an invariant is maintained under all concurrent execution without coordination. Typically requiring coordination: uniqueness constraints, bounded decrements (balance ≥ 0, stock ≥ 0), some foreign-key deletes; not requiring: insert-only counters, add-only sets, foreign keys on insert (with ordering).

**16.** CALM: monotonic computations (outputs never retracted as inputs grow) can run coordination-free and still converge. Monotonic: union of sets, increment-only counters. Non-monotonic: “is the set empty?”, “exactly N items”, “first claim wins”.

**17.** Let operations proceed with local information and repair violations later through business processes: airlines overbook and compensate with vouchers; banks allow overdrafts and charge fees; e-commerce cancels or refunds oversold orders.

**18.** Concurrent clients issue operations; a nemesis injects faults (partitions, kills, pauses, clock skew); every invocation/response is logged into a history; a checker (Knossos/Elle) verifies the history against a model (linearizable, serializable); violations reveal bugs.

**19.** A demoted leader that doesn’t know it was replaced serves stale reads. Prevent by leader leases with bounded clock drift, or by checking with a quorum before replying (ReadIndex in Raft), or by routing reads through the log.

**20.** Bounded staleness: reads reflect all writes older than a bound (T or K versions). Implement with read timestamps (`now − 5 s`) served by replicas that have applied up to that timestamp (follower reads/stale reads), or by tracking replication lag and routing away from replicas lagging more than 5 s.

**21.** (a) **Not linearizable**: write(1) completed at 10, R2 started at 20 and returned the old value 0. (b) **Not linearizable**: the write is pending until 30, so R1 may legitimately see 1; but R2 started at 15, after R1 had finished at 10 having seen 1, and returned 0: a new/old inversion. (c) **Linearizable**: R2 [2,4] read 0 and finished before R1 [5,10] started, so the order R2, write(1), R1, R3 works. (d) **Not linearizable**: both writes completed by 15, and R1’s value 2 implies W2 was ordered after W1; R2 later (30–35) reads 1, which would require W1 to be the last write. (e) **Linearizable**: write(1), R1→1, write(2), R2→2.

**22.** N=5: (W,R) = (1,5), (2,4), (3,3), (4,2), (5,1). Write failures tolerated = N − W; read failures tolerated = N − R: (1,5): 4/0; (2,4): 3/1; (3,3): 2/2; (4,2): 1/3; (5,1): 0/4. For 90% reads choose small R (R=1, W=5 or R=2, W=4 balancing availability); for 90% writes choose small W (W=1,R=5 or W=2,R=4).

**23.** Leader in us-east: majority = leader + nearest = us-west at 65 ms → ≈65 ms (plus local). Leader in eu: nearest = us-east at 85 ms → ≈85 ms. W=3: wait for the farthest: from us-east leader the farthest is eu 85 ms → ≈85 ms (and any region outage blocks writes).

**24.** By Little’s Law: concurrency = rate × latency: 20,000 × 0.001 = **20** in flight; 20,000 × 0.065 = **1,300** in flight. The latter needs much larger connection/thread pools or async I/O and more memory.

**25.** T1 and T2 both read {A: on-call, B: on-call} at snapshot S. T1 sets A=off; T2 sets B=off; no write-write conflict (different rows) so both commit under SI → both off-call, invariant violated. Prevent via SERIALIZABLE isolation (SSI detects the rw-dependencies), or lock the shared “on-call” predicate (`SELECT … FOR UPDATE` on both rows) or a constraint maintained via a single row (count) updated by both (creating a write-write conflict).

**26.** Demand vs escrow 250: R1 330 (needs +80), R2 120 (surplus 130), R3 200 (surplus 50), R4 90 (surplus 160). R1 must request 80 units from R2 or R4 (coordination for transfer); the others stay local. Total demand 740 < 1,000, so no oversell if transfers occur.

**27.** P(stale at t) = P(lag > t) = e^(−t/40). At 100 ms: e^(−2.5) = **8.2%**. At 500 ms: e^(−12.5) = **3.7×10⁻⁶**. P < 0.1% → t > 40 × ln(1000) = **276 ms**.

**28.** DC-vector: 24 B × 50,000 = 1.2 MB/s. Per-client vectors worst case 10 M entries × 8 B = 80 MB per write (!), clearly infeasible; hence systems use per-datacenter/replica vectors, dependency pruning, or HLC.

**29 (sketch).** (1) RYW: session token/leader reads for 5 s; (2) causal order: keep comments of a thread in one partition (single-leader order) or attach causal dependencies; (3) username uniqueness: linearizable registry (consensus) or home-region hash; (4) monotonic reads: sticky replica or version token. Costs: small except (3) cross-region coordination on registration only.

**30 (sketch).** (a) Global consensus registry (e.g., Spanner/etcd): correct, ~100–150 ms registration latency, unavailable in a minority partition; (b) hash(username) → home region: local latency for same-region users, others pay a WAN RTT; unavailable if home region down (replicated within region); (c) optimistic claim: instant UX, later conflict → one user must rename: poor UX and security risks (impersonation); use only with reservations.

**31 (sketch).** Store per-user per-conversation read position as a max-register (CRDT-like): merge = max; devices send updates; reads show max seen; monotone and convergent without coordination; session token ensures a device doesn’t display an older value than it already showed.

**32 (sketch).** Cart as an OR-Set (observed-remove set) or map from item → PN-counter quantities, merged by union; adds never lost; delete removes only observed add tags so a concurrent add survives; reconcile on checkout with a coordinated step (inventory check).

**33 (sketch).** Order history: `LOCAL_QUORUM` read/write for read-your-writes within a region; session store: `LOCAL_ONE` OK if sessions can be re-created; LWT (Paxos) only for operations needing compare-and-set such as unique claims or state transitions (`IF NOT EXISTS`).

**34 (sketch).** Single-leader (or consensus) with synchronous replication to at least one other AZ and region for RPO≈0; strict serializable transactions for postings; local reads allowed only at bounded staleness for display; regional failover with fencing; accept write latency of ~1 WAN RTT for cross-region durability.

**35.** (a) Failover to a lagging replica that lacks the latest committed state (async replication); (b) stale leader read before demotion is noticed; (c) client reading from a replica after being redirected to a different one without a session token. Fixes: semi-sync/consensus, lease-checked or quorum reads, session tokens/monotonic reads.

**36.** The lock expired during a pause or the Redis failover lost the lock; both workers believe they hold it. Fix: fencing tokens validated by the resource or a linearizable lock service (etcd/ZooKeeper), plus idempotent critical sections.

**37.** Violates read-your-writes/monotonic reads across app servers: local caches not invalidated. Options: shared cache/invalidations via pub/sub, versioned keys, short TTLs, route the user’s reads to the instance holding fresh data, or write-through.

**38.** Run concurrent reads/writes of a single key at `QUORUM` while injecting delays/partitions, record histories, and check with a linearizability checker; look for new/old inversion (as in Lab 1) and concurrent-write anomalies. Failure to find violations doesn’t prove correctness; a single violation disproves it.

**39.** Monotonic reads is missing (reads from different replicas with different lag). Cheapest fix: sticky session to one replica, or pass a version token and enforce `max(version)` on the client.

**40.** Non-repeatable read (read committed allows it). Use REPEATABLE READ/SNAPSHOT or SERIALIZABLE for the transaction.

**41 (sketch).** CAP: under partition choose linearizable (CP) or available (AP). Cassandra: AP-leaning with tunable consistency (QUORUM gives freshness, not linearizability); Spanner: CP with TrueTime and Paxos, minority partitions unavailable but practically high availability.

**42 (sketch).** Use per-region replicas with consensus across ≥3 regions, accept WAN RTT per write; allow local stale reads (bounded) and local linearizable operations when objects are home-region-owned; regional failover with fencing; use leader leases/follower reads to reduce latency.

**43 (sketch).** Eventual: converge eventually, no ordering promise; causal: causally related events seen in order everywhere (e.g., answers never before questions) while concurrent ones may differ; causal is stronger and still available under partition.

**44.** A shared whiteboard in one room: whenever you look, you see the latest thing anyone has finished writing, and once anyone has read a message, nobody later sees an older version. Copies of the whiteboard in different rooms must therefore consult each other before showing you anything.

---

## 15.19 Flashcards

| Front | Back |
|---|---|
| Linearizability | Single-copy illusion; real-time order; atomic at a point between start and end |
| Serializability | Transactions equivalent to some serial order; no real-time requirement |
| Strict serializability | Serializable + linearizable |
| Sequential consistency | One total order respecting each client's program order; not real time |
| Causal consistency | Causally related operations seen in order; available under partition |
| Session guarantees | RYW, monotonic reads, monotonic writes, writes-follow-reads |
| Eventual consistency | Converge if writes stop; no ordering |
| SEC | Same updates ⇒ same state (CRDTs) |
| W+R>N | Reads see completed writes; **not** linearizable (lab: 0.53% violations) |
| ABD | Read, then write back to a quorum before returning ⇒ linearizable |
| CAP | In a partition choose linearizability or availability |
| “CA” | Single node / not a real distributed choice |
| PACELC | Else: latency vs consistency |
| PA/EL examples | Dynamo, Cassandra (default) |
| PC/EC examples | Spanner, VoltDB, HBase |
| Cross-region cost | ≥ 1 WAN RTT per linearizable write (e.g., 65 ms) |
| I-confluence | Invariants safe without coordination; uniqueness & bounded decrement are not |
| CALM | Monotonic ⇒ coordination-free |
| Compensation | Allow, detect, repair (apologies) |
| Jepsen | Clients + nemesis + checker |
| Write skew | Snapshot isolation anomaly on disjoint writes |

## 15.20 Glossary additions

| Term | Simple Meaning | Technical Meaning | Why It Exists | Example |
|---|---|---|---|---|
| Consistency model | Rules for what reads may see | Contract on allowed histories | Reason about replicas | Linearizable |
| Linearizability | One up-to-date copy | Real-time-ordered atomic operations | Coordination primitives | etcd |
| Serializability | Transactions in some order | Equivalence to serial execution | Isolation | Postgres SERIALIZABLE |
| Strict serializability | Both of the above | Serializable + real-time order | Strongest | Spanner |
| Sequential consistency | Agreed order, maybe stale | Total order respecting program order | Memory models | CPUs |
| Causal consistency | No answers before questions | Preserves happens-before | Available strong-ish model | MongoDB sessions |
| Session guarantee | Per-client sanity | RYW/MR/MW/WFR | UX correctness | Session token |
| Eventual consistency | Converges later | Convergence if updates stop | Availability | DNS |
| Bounded staleness | Not older than T | Staleness limit | Predictable stale reads | 5 s |
| CRDT | Auto-merging data | Convergent replicated data type | SEC | G-Counter |
| History | Operation log | Invocations/responses with times | Testing | Jepsen |
| Nemesis | Fault injector | Component injecting failures | Test resilience | Partition |
| New/old inversion | Time going backwards | Later read returns older value | Quorum anomaly | Lab 1 |
| ABD | Quorum read-write-back | Atomic register protocol | Linearizable quorums | Attiya et al. |
| CAP | C vs A in partitions | Impossibility theorem | Frame trade-offs | CP/AP |
| PACELC | CAP + latency | Else-case trade-off | Everyday choices | PA/EL |
| I-confluence | Safe without coordination | Invariant preserved under merge | Coordination avoidance | Increment-only counter |
| CALM | Monotonic = coordination-free | Consistency as logical monotonicity | Design principle | Set union |
| Write skew | Disjoint writes break invariant | Snapshot isolation anomaly | Isolation choice | On-call doctors |
| Escrow | Pre-split budget | Local resource allocation | Avoid coordination | Per-region stock |
| Compensation | Fix afterwards | Business-level repair | Availability | Refund |
| Read repair / write-back | Fix on read | Propagate seen value | Convergence | ABD |

---

**Next: Chapter 16: Consensus, Leader Election, and Coordination.** How a group of unreliable machines agrees on one value: Paxos intuition, Raft in depth (elections, log replication, safety, membership, snapshots), leader leases and read paths, ZooKeeper/etcd/Chubby-style coordination services, and a full Raft simulator with chaos testing and mutation testing of the protocol.

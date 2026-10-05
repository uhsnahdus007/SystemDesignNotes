# Chapter 14: Distributed Systems Fundamentals

## Chapter card

| | |
|---|---|
| **Objective** | Build the mental model that every later distributed-systems topic rests on: partial failure, unreliable networks, why timeouts are ambiguous, failure detection and its limits, clocks (physical, logical, hybrid), causality and ordering, process pauses and fencing, system and failure models, impossibility results (Two Generals, FLP), and the arithmetic of availability |
| **Why this chapter exists** | Parts I–II treated machines as mostly reliable. From here on, we design for a world where *any* component can fail, be slow, or lie by omission, and where no node has a trustworthy view of the whole. Every rule of thumb in Chapters 15–20 (quorums, consensus, idempotency, sagas, CRDTs) is a response to the facts in this chapter |
| **Prerequisites** | Chapters 1, 5, 9, 12 (networks, tail latency, replication, idempotency) |
| **Study time** | 10–13 hours including labs |
| **Tier** | `[MK]` throughout; clocks and impossibility results `[GK]` |
| **Mastery criteria** | (1) Explain partial failure and why distributed systems are fundamentally different from single machines. (2) Explain why a timeout cannot tell you whether an operation happened, and design for it. (3) Explain the failure-detector trade-off and tune one. (4) Distinguish time-of-day and monotonic clocks; explain clock-skew bugs. (5) Use Lamport clocks and vector clocks to reason about causality. (6) Explain process pauses, leases, and fencing tokens. (7) State the Two Generals and FLP results and what they mean in practice. (8) Compute availability for series/parallel designs and failure frequency at scale. (9) Choose a system model for a design |

---

## 14.1 The Story: Four Incidents, One Root Cause

**LedgerCo** runs a payments platform on forty servers. In one quarter:

1. **The double charge.** A payment request to the card processor times out after 2 seconds. The service retries. Both requests were executed; the customer is charged twice. The postmortem notes: “the first request had succeeded; the *response* was lost.”
2. **The failover storm.** The monitoring system pings each cache node every second and declares it dead after 3 missed pings. During a brief network congestion event, 9 of 12 cache nodes miss pings. They are declared dead, traffic reroutes to the database, and the database collapses. The cache nodes were healthy the whole time.
3. **The two lock holders.** A worker acquires a distributed lock (expires in 30 s), then pauses for 40 seconds in a garbage-collection stop. The lock expires; a second worker acquires it. The first worker wakes, believes it still holds the lock, and writes to the shared file. The file is corrupted by two concurrent writers.
4. **The vanished update.** Two services update the same customer record; the database resolves conflicts by “latest timestamp wins.” Service B updates the record *after* reading Service A’s update, yet A’s older update wins, because B’s server clock is 80 ms behind A’s.

### Pause & Predict 14.1
> For each incident, what did the system *assume* that turned out to be false? (Hint: they are about the network, the failure detector, execution timing, and clocks.) For each, what design change would make the system correct *without* making those assumptions true?

---

## 14.2 What makes distributed systems different `[MK]`

### 14.2.1 Partial failure
On a single machine, failures are mostly **total**: the process crashes or the machine dies; operations either happen or they don’t, and errors are reported deterministically. In a distributed system, **parts fail independently while others keep running**. A request may have been executed by a remote node whose reply you never received; a node may be alive but unreachable; some nodes may think a peer is dead while others think it is alive. You must write programs whose correctness holds under *any combination of partial failures*, including ones you cannot observe directly.

> **The defining fact:** *you cannot, in general, tell the difference between a slow node, a dead node, a lost message, and a lost reply.*

### 14.2.2 Three sources of difficulty
1. **Unreliable networks:** messages can be lost, delayed, duplicated, or reordered.
2. **Unreliable clocks:** every node has its own clock; they drift and disagree, and none is “correct.”
3. **Unreliable execution:** a process can pause arbitrarily (GC, VM migration, swap, CPU starvation) *between any two instructions* without noticing.
There is no shared memory and no global state; knowledge about the system is always old, local, and possibly wrong.

### 14.2.3 The eight fallacies of distributed computing `[MK]`
Peter Deutsch’s list (with James Gosling) of false assumptions programmers make:
1. The network is reliable. 2. Latency is zero. 3. Bandwidth is infinite. 4. The network is secure. 5. Topology doesn’t change. 6. There is one administrator. 7. Transport cost is zero. 8. The network is homogeneous.
Each fallacy maps to a design rule: timeouts and retries (1), batching and locality (2–3), authentication and encryption (4), service discovery (5), configuration and ownership boundaries (6), cost awareness (7), versioned protocols (8).

### 14.2.4 Why not just avoid distribution?
If one machine suffices, **don’t distribute** (Ch 6 and 10: scale up and simplify first). We distribute for *capacity* (more than one machine can handle), *availability* (survive machine/AZ loss), *latency* (be near users), and *isolation*. Those benefits cost you everything in this chapter.

---

## 14.3 System models `[GK]`

To reason about correctness, we state assumptions explicitly as a **system model**: assumptions about timing and about failures. An algorithm is correct *relative to a model*.

### 14.3.1 Timing models
| Model | Assumption | Realism |
|---|---|---|
| **Synchronous** | Known upper bounds on message delay, process speed, and clock drift | Unrealistic for the internet; roughly valid within a tightly controlled data center only for some purposes |
| **Asynchronous** | No bounds on delay or speed; no clocks | Very pessimistic; allows proving impossibility results (FLP) |
| **Partially synchronous** | The system is asynchronous *for unknown periods* but eventually behaves synchronously (bounds exist but are unknown, or hold after an unknown time) | **The practical model**: networks usually behave, occasionally don’t |

### 14.3.2 Failure models
| Model | Meaning |
|---|---|
| **Crash-stop** | A node fails by stopping and never returns |
| **Crash-recovery** | A node may crash and later restart, retaining stable storage (disk) but losing memory |
| **Omission** | A node fails to send or receive some messages |
| **Timing (performance) failure** | A node responds too late (includes long pauses) |
| **Byzantine (arbitrary)** | A node may behave arbitrarily or maliciously: lie, send conflicting messages |

### 14.3.3 What real systems assume
Most production systems (databases, Kafka, etcd, Raft/Paxos implementations) assume **partial synchrony, crash-recovery, no Byzantine faults**. Byzantine tolerance (BFT: PBFT, Tendermint, HotStuff) is used where participants distrust each other (blockchains, some aviation/space systems) and costs 3f+1 nodes to tolerate f faults versus 2f+1 for crash faults, plus heavy message overhead. **Be explicit about your model**; saying “crash-recovery with a partially synchronous network” is a mark of understanding.

---

## 14.4 The network `[MK]`

### 14.4.1 What can go wrong
A message may be: **lost** (congestion, buffer overflow, failures), **delayed** (queueing, retransmits, routing changes), **duplicated** (retransmission after a lost ack), **reordered** (multiple paths), **corrupted** (rare; checksums), or the **link may partition** (some nodes cannot reach others, or can reach only one direction). Studies of production networks show partitions are not rare events `[Public: Bailis & Kingsbury, “The Network is Reliable”]`: switch failures, misconfigurations, NIC bugs, cable faults, and overloaded links produce them regularly. Even **asymmetric** failures occur (A hears B, B doesn’t hear A).

### 14.4.2 TCP does not save you
TCP gives an ordered, retransmitted byte stream over a single connection. It does *not* guarantee that the application on the other side processed your data, nor that you’ll be told promptly if the peer vanished (half-open connections can linger for minutes without keepalives). Delivery to a kernel buffer is not execution. Application-level acknowledgements and idempotency remain essential (the **end-to-end argument**: reliability must be implemented by the endpoints, not assumed from the middle).

### 14.4.3 The timeout ambiguity `[MK]`
When a request times out, three worlds are possible:
```text
 (a) the request never arrived        → the operation did NOT happen
 (b) the request arrived and executed, → the operation DID happen, but the reply was lost or late
     but the reply was lost/late
 (c) the node crashed during execution → the operation happened PARTIALLY or not at all
```
From the client’s side these are indistinguishable. **Measured (Lab 1, part 1)** with 1.5% slow responses, 0.5% lost requests and 0.5% lost replies:
```text
client timeout   timeouts   of which the server actually EXECUTED the operation
    50 ms       12.72%     96.1% of timeouts (a blind retry would run the operation twice)
   100 ms        3.33%     84.8% of timeouts
   500 ms        2.50%     80.1% of timeouts
  1000 ms        2.17%     77.3% of timeouts
  3000 ms        0.99%     51.0% of timeouts
```
**Reading it:** most timeouts are *not* failures: with an aggressive 50 ms timeout, 96% of the “failed” requests had in fact been executed. Even at 3 s, half the timeouts hid an executed operation (lost replies). The only safe policy for non-idempotent operations is: **make them idempotent** (idempotency keys, Ch 2/12), or **query for the outcome** before retrying, or accept at-most-once semantics knowingly. This is the double charge in Incident 1.

### 14.4.4 The Two Generals problem `[GK]`
Two armies on opposite hills must attack simultaneously to win; they can communicate only by messengers who may be captured. General A sends “attack at dawn”; to be sure, A needs an acknowledgement; B, sending the ack, needs to know the ack arrived… No finite exchange of messages over an unreliable channel can give both generals *certainty* of agreement. This proves that **no protocol can guarantee that two parties agree on a fact over a lossy link in bounded time with certainty**. Practical consequences: exactly-once *delivery* is impossible (Ch 12); atomic commit protocols can block (Ch 17); we rely on retries, timeouts, idempotency, and probabilistic confidence.

### 14.4.5 Coping strategies (summary)
Timeouts on every remote call (deadline propagation); retries with exponential backoff and jitter, **only** for idempotent operations; idempotency keys; sequence numbers and acknowledgements; heartbeats and keepalives; deduplication; hedged requests for tail latency (Ch 5); circuit breakers and bulkheads (Ch 18); explicit handling of “unknown outcome” as a first-class state in the code and in the data model (e.g., `PENDING_UNKNOWN` payments reconciled by querying the processor).

---

## 14.5 Failure detection `[MK/GK]`

### 14.5.1 The problem
A node must decide whether a peer is *alive*. The only evidence is messages (or silence). Detectors are judged by:
- **Completeness:** every crashed node is eventually suspected by every correct node.
- **Accuracy:** correct nodes are not suspected (perfect accuracy is impossible in asynchronous systems; **eventual accuracy** is the realistic goal).
- **Speed:** how soon after a crash do we notice.
- **Overhead:** messages and CPU used.
Chandra and Toueg showed that *unreliable failure detectors* (which may make mistakes for a while) are enough to build consensus in partially synchronous systems.

### 14.5.2 Heartbeats and timeouts
Each node sends a heartbeat every *h*; a monitor suspects it if none arrives within timeout *T*. **Trade-off:** small *T* detects crashes quickly but suspects slow-but-alive nodes (false positives → needless failovers); large *T* is accurate but slow (long outages).

**Measured (Lab 1, part 2).** A perfectly healthy node sends 86,400 heartbeats (one per second for 24 hours) over a network where 1% of heartbeats are delayed 0.2–2 s (GC pauses, congestion) and 0.05% are stalled 5–10 s:
```text
detector                 false suspicions in 24 h   time to detect a REAL crash (avg)
fixed timeout   1.5 s        692                      1.02 s
fixed timeout   2.0 s        476                      1.52 s
fixed timeout   3.0 s         61                      2.52 s
fixed timeout   5.0 s         54                      4.52 s
fixed timeout  11.0 s          0                     10.52 s
phi-accrual, phi>=1         1854                      0.76 s
phi-accrual, phi>=2          967                      0.95 s
phi-accrual, phi>=3          760                      1.09 s
phi-accrual, phi>=4          677                      1.20 s
phi-accrual, phi>=8          528                      1.55 s
```
**Interpretation.** (1) A 1.5 s timeout produced **692 false alarms a day** (one every ~2 minutes) on a healthy node: this is Incident 2. (2) To get **zero** false alarms against this network’s tail (a 10 s stall) the timeout must exceed 10 s, so detection takes ~10 s. Speed and accuracy trade directly. (3) The **phi-accrual detector** (Hayashibara et al.; used by Cassandra and Akka) adapts its threshold to the observed mean and standard deviation of heartbeat gaps instead of using a fixed constant; it outputs a suspicion *level* φ. In this experiment it did **not** beat a well-chosen fixed timeout, because our delay distribution has a heavy, non-normal tail and the detector models gaps as roughly normal. Adaptive detectors shine when *typical* latency drifts (load changes, cross-AZ vs same-AZ); they cannot conjure information that the signal doesn’t contain. **The model matters.**

### 14.5.3 Better detectors and practices
- **Indirect probing (SWIM):** if A doesn’t hear from B, A asks k other nodes to ping B; B is suspected only if all fail. Reduces false positives from A’s own network problems. SWIM also **gossips** membership changes and uses a *suspect* state with a refutation window `[Public: SWIM paper; used by Consul/Serf, memberlist]`.
- **Multiple observers and quorum:** declare death by majority of observers, not by one monitor.
- **Application-level health:** a node that answers pings but cannot serve requests is not healthy; pings that bypass the app path give false confidence (Ch 7 health checks).
- **Lease-based liveness:** a node proves liveness by renewing a time-limited lease; expiry equals suspected failure; cooperative with fencing (§14.8).
- **Separate detection from action:** a *suspicion* should trigger a cheap, reversible reaction first (e.g., route around), and only a *confirmed or fenced* failure triggers an expensive one (data re-replication, leader change).
- **Hysteresis and rate limits:** avoid failover storms (Ch 10: don’t rebalance automatically on every suspicion).

---

## 14.6 Time and clocks `[MK/GK]`

### 14.6.1 Two kinds of physical clocks on a computer
| Clock | What it is | Properties | Use for |
|---|---|---|---|
| **Time-of-day (wall-clock)** | Seconds since the epoch, synchronized with NTP/PTP | Can **jump** forward or backward (NTP corrections, leap seconds, manual changes, VM migration); not monotonic; differs across machines | Timestamps shown to humans, expiry dates across systems *with tolerance* |
| **Monotonic** | Counter from an arbitrary start (e.g., `System.nanoTime()`, `CLOCK_MONOTONIC`) | Never goes backwards; meaningless across machines or reboots | Measuring **durations** and timeouts on one machine |
**Rule:** measure elapsed time with a monotonic clock; never compute timeouts from wall-clock differences.

### 14.6.2 Drift, skew, and sync accuracy
- **Drift:** crystal oscillators run fast or slow, typically 10–200 ppm (parts per million). At 100 ppm, a clock gains or loses **8.64 seconds per day** if unsynchronized.
- **NTP** over the public internet achieves tens of milliseconds; in a data center with good local servers, ~1 ms or better; **PTP** (hardware timestamping) can reach microseconds; cloud providers offer time sync services with sub-millisecond bounds in some regions `[Public]`.
- **Skew:** the difference between two nodes’ clocks at the same real instant. Always non-zero, usually bounded in practice, occasionally large (misconfigured NTP, VM pauses, leap-second bugs).
- **Leap seconds:** a minute with 61 seconds; handled by stepping (dangerous) or **smearing** (spreading the extra second over hours; Google/AWS style). Systems have crashed on leap seconds.

### 14.6.3 What goes wrong with timestamps as ordering `[MK]`
Using wall-clock timestamps to decide “which event happened later” (last-write-wins, ordering logs from different machines, cache expiry coordination) silently assumes tight synchronization. It breaks under skew, and **it can drop causally later writes**.

**Measured (Lab 1, part 3).** Server A writes x=1. Five milliseconds later, *after hearing about that write*, server B writes x=2 (B’s write is causally later and should win). Each server stamps writes with its own wall clock, which is off by a random offset within ±skew:
```text
clock skew between servers (each clock off by up to +/-)   LWW keeps the WRONG (older) write   Lamport/logical clock
   +/-    0 ms                                                0.00%                           0.00%
   +/-    1 ms                                                0.00%                           0.00%
   +/-    5 ms                                               12.50%                           0.00%
   +/-   10 ms                                               28.13%                           0.00%
   +/-   50 ms                                               45.11%                           0.00%
   +/-  200 ms                                               48.81%                           0.00%
```
With skew larger than the causal gap, last-write-wins approaches a coin flip (48.8% wrong at ±200 ms) and the data loss is **silent**: no error, just the wrong value. Logical clocks (below) never get the causal order wrong. This is Incident 4.

### 14.6.4 Lamport clocks `[MK]`
Leslie Lamport (1978) defined the **happens-before** relation (→): (a) if events a and b occur in the same process and a precedes b, a → b; (b) if a is the sending of a message and b its receipt, a → b; (c) transitivity. Events not ordered by → are **concurrent**.

**Lamport timestamp algorithm:** each process keeps a counter *C*. Before each local event, `C = C + 1`. On sending a message, attach C. On receiving a message with timestamp *t*: `C = max(C, t) + 1`.
**Guarantee:** if a → b then C(a) < C(b). **The converse does not hold:** C(a) < C(b) does not imply a → b (they might be concurrent). To get a *total* order, break ties by process ID: (C, pid).
Uses: ordering events consistently with causality (distributed logs, total-order multicast, last-writer-wins that respects causality when writers read before writing), mutual-exclusion protocols. Limitation: cannot detect concurrency.

*Worked example.* P1: a(1), send m→P2 (2). P2: b(1), receive m: C = max(1,2)+1 = 3, c(3). P1 event d(3). Compare c(3) and d(3): concurrent (no causal path) but equal timestamps → tie-break by pid. a→c holds (1<3) ✓.; b and a are concurrent though C(b)=1=C(a): timestamps alone can’t tell.

### 14.6.5 Vector clocks `[GK]`
A vector clock keeps one counter per process: `V = [v1, v2, …, vn]`. On a local event at process *i*: `V[i]++`. On send: attach V. On receive of V': `V[j] = max(V[j], V'[j])` for all j, then `V[i]++`.
**Comparison:** V(a) ≤ V(b) iff every component of V(a) ≤ that of V(b); a → b iff V(a) < V(b) (≤ and not equal). If neither V(a) ≤ V(b) nor V(b) ≤ V(a), the events are **concurrent**.
```text
 P1: [1,0,0]──send──────────────┐                   [2,0,0]
 P2:                  [0,1,0]   ▼ receive → [1,2,0]  → ...
 P3:  [0,0,1]
 [1,0,0] vs [0,0,1]: neither ≤ other → CONCURRENT.   [1,0,0] → [1,2,0]: ≤ → causally BEFORE.
```
Use: detect conflicting concurrent writes (Dynamo, Riak: siblings), causal consistency (Ch 15), CRDT metadata (Ch 20). Cost: O(n) space per timestamp (dotted version vectors and interval tree clocks reduce growth).

### 14.6.6 Hybrid logical clocks (HLC) `[GK]`
HLC combines physical time with a logical counter: timestamp = (physical time *l*, counter *c*). On events, take `l' = max(l, wall_clock)`; if `l' == l` increment `c`, else reset `c = 0`; on receive take the max with the message’s timestamp. Properties: monotonic and causality-respecting like Lamport clocks, **but close to real time** (within the clock-sync error), so timestamps are human-meaningful and usable for “read as of time T.” Used by CockroachDB, MongoDB, YugabyteDB `[Public]`.

### 14.6.7 TrueTime: embracing uncertainty `[GK]`
Google Spanner’s **TrueTime** API returns an interval `[earliest, latest]` guaranteed to contain the true time (uncertainty ε of a few ms, achieved with GPS receivers and atomic clocks in each data center). A transaction chooses a commit timestamp *s* and then **waits out the uncertainty** (“commit wait”: until `TT.after(s)` is true) so that its timestamp order matches real-time order across the globe. This gives **external consistency** (linearizability for transactions) using clocks, at the cost of ~ε latency per commit `[Public: Spanner paper]`. The lesson: if you can bound the clock error, you can use time safely; if you can’t, don’t.

### 14.6.8 Practical rules about time
1. Never use wall-clock timestamps to order concurrent writes unless you accept data loss under skew (or you bound skew like TrueTime).
2. Use **monotonic clocks** for timeouts/durations.
3. Use **logical/hybrid clocks or version vectors** for ordering and conflict detection.
4. Treat leases and TTLs as **approximate**; include safety margins for drift (§14.8).
5. Monitor clock offsets; alert on large skew; use authoritative time sources.
6. Beware of **timestamp-based IDs** and ordering at distributed scale (Snowflake clock rollback, Ch 10).

---

## 14.7 Causality and ordering `[GK]`

- **Total order vs causal order:** a *total order* puts all events in one sequence everyone agrees on (needs coordination/consensus); a *causal order* only orders events that are causally related, leaving concurrent ones unordered (cheaper, available under partition).
- **Total order broadcast** (atomic broadcast): all nodes deliver the same messages in the same order. It is **equivalent to consensus**: if you can solve one you can solve the other. This is what replicated state machines use (Ch 16).
- **Sequence numbers from a single leader** give a total order cheaply at the cost of a single point of coordination (the log in Ch 9/13); Lamport timestamps give an order consistent with causality without a central point, but a node can’t know the order is *final* until it has heard from everyone.
- **Why it matters:** uniqueness constraints (claiming a username), exactly-once effects, and leader election all reduce to agreeing on an order of events.

---

## 14.8 Process pauses, leases, and fencing `[MK/GK]`

### 14.8.1 Pauses can happen anywhere
A process can be suspended for seconds between two instructions: **stop-the-world GC**, VM live migration or steal time, SIGSTOP, swapping, page faults on slow storage, CPU throttling in containers, laptop sleep. From the outside it is indistinguishable from a crash; from the inside, **the process doesn’t know time has passed**. Code like
```java
if (lock.isHeldByMe()) {          // check
    // <--- 40 s pause here
    writeToSharedFile();          // use: but the lock expired 10 s ago
}
```
is unsafe no matter how short the check-to-use window looks. That’s Incident 3.

### 14.8.2 Leases
A **lease** is a time-limited grant of a right (lock, leadership, ownership of a partition). The holder must stop acting before it expires; the grantor won’t re-grant until it has expired. Leases avoid waiting on a possibly-dead holder forever. But leases **assume bounded clock drift and no unbounded pauses**.
*Safe margin example.* Lease duration 10 s; clock drift ≤ 0.1% (1 ms/s); the holder should stop using the lease after `10 × (1 − 0.001) − processing_slack` seconds measured by its monotonic clock, while the grantor waits `10 × (1 + 0.001)` before re-granting. Even so, a pause *after* the check can violate it, hence fencing.

### 14.8.3 Fencing tokens `[MK]`
Every time a lock/lease is granted, the lock service issues a monotonically increasing **fencing token** (33, 34, …). The holder sends the token with every write to the protected resource; the **resource rejects any write with a token lower than the highest it has seen**.
```text
 Worker 1 gets lock, token 33 ── pauses (GC) ──────────────────────── wakes, writes with token 33 ──✗ REJECTED
 Worker 2 gets lock after expiry, token 34 ── writes with token 34 ─✓ (storage records max token = 34)
```
This makes correctness independent of timing: even a zombie holder cannot corrupt data, as long as the **storage layer enforces the check**. Requirements: the lock service must be linearizable (Ch 16, e.g., etcd/ZooKeeper, which provide revision numbers/zxids usable as tokens); the resource must support conditional writes (compare-and-set on version/epoch). Real examples: Kafka producer epochs and leader epochs, Raft terms, HDFS NameNode fencing, “generation IDs” in distributed file systems. Redis-based locks (Redlock) are debated precisely because they lack fencing tokens and depend on timing assumptions `[Public: Kleppmann vs Antirez]`.

### 14.8.4 STONITH and other fencing
Hardware-level fencing (power off the other node, revoke SAN reservations, cloud API to detach the volume) is used for databases and clusters (Ch 9). Whatever the mechanism, the rule is: **never rely on the old holder’s cooperation to stop**.

---

## 14.9 Impossibility results `[GK]`

### 14.9.1 FLP (Fischer–Lynch–Paterson, 1985)
In a **fully asynchronous** system where even **one** process may crash, **no deterministic algorithm can guarantee consensus** (agreement, validity, termination) in bounded time: because you can’t distinguish a crashed process from a slow one, there is always a schedule in which the algorithm waits forever or decides unsafely.
**What it does not say:** consensus is *impossible in practice*. Real systems escape FLP by relaxing an assumption: **partial synchrony** (timeouts: Raft/Paxos make progress when the network is “well-behaved enough,” and remain *safe* always), or **randomization** (random election timeouts, Ben-Or style), or failure detectors. The practical reading: *safety is never compromised, but liveness requires luck with timing*. Lab 3 in Chapter 16 shows this: remove randomization from election timeouts and the cluster never elects a leader.

### 14.9.2 CAP preview
During a network partition a system must choose between **consistency** (linearizability) and **availability** (every non-failing node responds). Formalized by Gilbert and Lynch (2002). The deep treatment, including PACELC and why “CA” isn’t a real option, is Chapter 15.

### 14.9.3 Other lower bounds worth knowing
- **Consensus needs a majority** (2f+1 nodes to tolerate f crash faults); Byzantine: 3f+1.
- **No algorithm can implement an atomic register with fewer than a majority alive** (in the asynchronous model).
- **Latency lower bounds:** a linearizable write needs at least one round trip to a quorum; geo-distributed consensus pays the RTT to the nearest majority.

---

## 14.10 Truth, majority, and knowledge `[MK]`

- A node’s belief (“I am the leader,” “B is dead,” “I hold the lock”) is **just a belief** and may be stale. Systems define truth by **majority decision** (quorums) with generation numbers (terms/epochs) to detect staleness.
- **Quorums** intersect: any two majorities share at least one node, so a decision made by one majority cannot be silently contradicted by another (Ch 9, 16).
- A node that learns it is in a minority partition should **step down** (stop serving writes) rather than assume the majority is dead. If it cannot talk to a majority, it cannot know the system’s state.
- **Byzantine tolerance** adds signatures and extra replicas to handle liars; most enterprise systems assume nodes are honest-but-faulty and secure the perimeter with authentication/authorization (Ch 22).

---

## 14.11 Design principles for living with partial failure `[MK]`

1. **Assume every remote call can fail, hang, or succeed silently.** Timeouts, deadlines, retries (with backoff + jitter + budgets), idempotency.
2. **Make operations idempotent and commutative where possible** so retries and reordering are harmless.
3. **Prefer eventual consistency with convergent data types** where the product allows (Ch 20).
4. **Use leases + fencing**, never bare locks, for mutual exclusion.
5. **Separate detection from reaction;** make reactions reversible or rate-limited.
6. **Reduce shared state and coordination;** the fastest distributed operation is one that needs no coordination.
7. **Bound everything:** queues, retries, timeouts, concurrency, blast radius (bulkheads, cells).
8. **Design for degraded modes:** serve stale data, shed load, disable features, rather than all-or-nothing.
9. **Make failures visible:** structured logs, metrics, traces, and explicit unknown-outcome states.
10. **Test failure for real:** fault injection (Jepsen-style network partitions and clock faults), chaos engineering (kill instances in production-like environments), **deterministic simulation testing** (FoundationDB’s approach: run the whole system in a single-threaded simulator with injected faults and replay any failure from a seed) `[Public]`. Chapter 16’s Raft simulator uses exactly this idea.
11. **Specify before you code:** TLA+/PlusCal models find protocol bugs early (used at AWS, Azure, MongoDB, Elastic) `[Public]`.

---

## 14.12 The arithmetic of failure and availability `[MK]`

### 14.12.1 Failures at scale
If one disk has an annual failure rate (AFR) of 2%, then a fleet of *N* disks has an expected *0.02N* failures per year:
- 1,000 disks → 20 failures/year ≈ one every 18 days.
- 10,000 disks → 200/year ≈ one every 1.8 days.
- 100,000 disks → 2,000/year ≈ more than five **per day**.
At scale, failure is a **routine event**, not an exception. Systems must repair themselves continuously (Ch 11 repair, Ch 9 failover).

### 14.12.2 Availability basics
Availability *A* = MTBF / (MTBF + MTTR), or uptime fraction. “Nines” and allowed downtime per year:
| Availability | Downtime per year | per month | per day |
|---|---|---|---|
| 99% (two nines) | 3.65 days | 7.3 h | 14.4 min |
| 99.9% | 8.76 h | 43.8 min | 1.44 min |
| 99.95% | 4.38 h | 21.9 min | 43 s |
| 99.99% | 52.6 min | 4.38 min | 8.6 s |
| 99.999% | 5.26 min | 26 s | 0.86 s |

### 14.12.3 Series and parallel `[MK]`
- **Series (all must work):** `A = A1 × A2 × … × An`. Five components at 99.9% each in a request path: 0.999⁵ = **99.50%** (≈ 1.8 days of downtime per year). *Every added hard dependency lowers availability.* Fan-out of *k* parallel calls that all must succeed behaves like *k* in series.
- **Parallel (any one suffices):** `A = 1 − (1−A1)(1−A2)…`. Two independent replicas at 99%: 1 − 0.01² = **99.99%**. Three at 99%: 99.9999%.
- **Caveats:** failures are **correlated** (shared power, software bug in all replicas, a bad deploy, the load balancer, the DNS, the config service), so real availability is lower than the independent-failure math. The weakest *shared* component dominates (Ch 6/18). Redundancy helps only if failover itself works (tested!).
- **MTTR matters as much as MTBF:** cutting recovery time from 60 to 6 minutes improves availability tenfold in downtime terms; hence investment in automated failover, rollbacks, and fast deploy pipelines.
- **SLOs and error budgets:** an SLO of 99.9% gives 43.8 min/month of budget to spend on risk (deploys, experiments) (Ch 18/21).

### 14.12.4 Tail at scale
A request that fans out to *n* backends is slow if any is slow: P(slow) = 1 − (1−p)ⁿ (Ch 5/10). With p = 1% and n = 100 → 63%. Distributed systems magnify rare events.

---

## 14.13 Failure modes (the general catalogue)

| Failure | Mechanism | Typical defense |
|---|---|---|
| **Double execution** | Timeout + retry; at-least-once delivery | Idempotency keys, dedup, queries for outcome |
| **Lost update / silent data loss** | LWW with skewed clocks; concurrent writes | Version vectors, causal ordering, CRDTs, single-writer designs |
| **False failure detection** | Short timeouts; congestion; GC | Indirect probes, multiple observers, hysteresis, rate-limited reaction |
| **Failover storm** | Mass false positives; thundering herd | Rate limits, jitter, capacity for degraded mode |
| **Split-brain / zombie leader** | Partition or pause + stale belief | Majority quorum, terms/epochs, fencing tokens, leases with margins |
| **Clock jump** | NTP step, leap second, VM resume | Monotonic clocks for durations, slewing/smearing, skew alerts |
| **Message reordering/duplication** | Multipath, retransmission | Sequence numbers, idempotent handlers, per-key ordering |
| **Partial partition (asymmetric)** | One-way link failure | Treat reachability as per-pair; quorum rules; bidirectional heartbeats |
| **Correlated failure** | Shared dependency, bug, bad config push | Diversity, cells, staged rollouts, config canaries |
| **Metastable failure** | Retry/queue amplification persists after trigger | Load shedding, retry budgets, circuit breakers (Ch 18) |
| **Slow node (gray failure)** | Degraded but responding | Latency-aware routing, outlier ejection, hedging |
| **Cascading failure** | Overload spreads via retries/timeouts | Bulkheads, backpressure, timeouts, shedding |
| **Unknown outcome** | Crash mid-operation | Reconciliation jobs, state machines with explicit unknown/pending states |
| **Byzantine behavior** | Bugs, corruption, malice | Checksums, validation, authentication; BFT only when needed |

---

## 14.14 Labs

### Lab 1: Timeouts, failure detectors, and clock skew (Java 17+)
```java
import java.util.*;

public class FailureLab {
    static final Random R = new Random(21);
    static double exp(double mean) { return -Math.log(1 - R.nextDouble()) * mean; }

    // ------------------------------------------------------------------ 1) a timeout does not tell you whether the operation happened
    static void timeoutAmbiguity() {
        int trials = 1_000_000;
        System.out.println("== 1) Timeout ambiguity. Server latency: 98.5% ~ 5+exp(20) ms, 1.5% slow (0.5-3 s). Also 0.5% of requests are lost on the way in,");
        System.out.println("      and 0.5% of replies are lost on the way out (the server DID execute those). ==");
        System.out.println("client timeout   timeouts   of which the server actually EXECUTED the operation");
        for (int timeout : new int[]{50, 100, 500, 1000, 3000}) {
            long timeouts = 0, executedAnyway = 0;
            for (int i = 0; i < trials; i++) {
                boolean requestLost = R.nextDouble() < 0.005, replyLost = R.nextDouble() < 0.005;
                double latency = R.nextDouble() < 0.015 ? 500 + R.nextDouble() * 2500 : 5 + exp(20);
                boolean executed = !requestLost;                                  // the operation is performed if the request arrived
                boolean replyArrivedInTime = executed && !replyLost && latency <= timeout;
                if (!replyArrivedInTime) { timeouts++; if (executed) executedAnyway++; }
            }
            System.out.printf("%6d ms      %6.2f%%    %5.1f%% of timeouts (a blind retry would run the operation twice)%n", timeout, 100.0 * timeouts / trials, 100.0 * executedAnyway / timeouts);
        }
    }

    // ------------------------------------------------------------------ 2) failure detectors: speed vs false alarms
    static void failureDetectors() {
        int beats = 86_400;                                                       // one heartbeat per second for 24 hours
        double interval = 1000; double[] arrival = new double[beats]; double prev = 0;
        for (int i = 0; i < beats; i++) {
            double delay = 20 + exp(15);                                           // usual network delay
            double x = R.nextDouble();
            if (x < 0.01) delay += 200 + R.nextDouble() * 1800;                    // 1%: GC pause / congestion: +0.2-2 s
            else if (x < 0.0105) delay += 5000 + R.nextDouble() * 5000;            // 0.05%: a nasty 5-10 s stall
            arrival[i] = i * interval + delay;
        }
        // arrivals may overtake each other; the detector only cares when the next heartbeat shows up after the previous one
        for (int i = 1; i < beats; i++) if (arrival[i] < arrival[i - 1]) arrival[i] = arrival[i - 1] + 1;
        double[] gap = new double[beats - 1]; for (int i = 1; i < beats; i++) gap[i - 1] = arrival[i] - arrival[i - 1];

        System.out.println("\n== 2) Failure detector: the node is perfectly HEALTHY for 24 h (86,400 heartbeats, 1/s); count false alarms. ==");
        System.out.println("detector                 false suspicions in 24 h   time to detect a REAL crash (avg)");
        for (double timeout : new double[]{1500, 2000, 3000, 5000, 11000}) {
            int fp = 0; for (double g : gap) if (g > timeout) fp++;
            System.out.printf("fixed timeout %5.1f s     %6d                     %5.2f s%n", timeout / 1000, fp, (timeout - interval / 2) / 1000 + 0.02);
        }
        // phi-accrual-style adaptive detector: threshold = mean + z*stddev of a sliding window of recent gaps
        double[][] phi = {{1, 1.2816}, {2, 2.3263}, {3, 3.0902}, {4, 3.719}, {8, 5.612}};
        for (double[] p : phi) {
            ArrayDeque<Double> w = new ArrayDeque<>(); double sum = 0, sumsq = 0; int fp = 0; double detSum = 0; int detN = 0;
            for (double g : gap) {
                if (w.size() >= 50) {
                    double mu = sum / w.size(), sd = Math.sqrt(Math.max(1, sumsq / w.size() - mu * mu)), thr = mu + p[1] * sd;
                    if (g > thr) fp++; detSum += thr; detN++;
                }
                w.addLast(g); sum += g; sumsq += g * g; if (w.size() > 200) { double o = w.removeFirst(); sum -= o; sumsq -= o * o; }
            }
            System.out.printf("phi-accrual, phi>=%-2.0f      %6d                     %5.2f s%n", p[0], fp, (detSum / detN - interval / 2) / 1000 + 0.02);
        }
    }

    // ------------------------------------------------------------------ 3) physical clocks vs causality
    static void clocks() {
        int trials = 1_000_000; double causalGap = 5;                              // B's write happens 5 ms after A's write became visible to B
        System.out.println("\n== 3) Last-write-wins by wall-clock. A writes x=1; 5 ms later (after hearing about it) B writes x=2. B is causally LATER and should win. ==");
        System.out.println("clock skew between servers (each clock off by up to +/-)   LWW keeps the WRONG (older) write   Lamport/logical clock");
        for (double skew : new double[]{0, 1, 5, 10, 50, 200}) {
            long wrong = 0;
            for (int i = 0; i < trials; i++) {
                double offA = (R.nextDouble() * 2 - 1) * skew, offB = (R.nextDouble() * 2 - 1) * skew;
                double tsA = 1000 + offA, tsB = 1000 + causalGap + offB;           // wall-clock timestamps stamped on the two writes
                if (tsB < tsA) wrong++;                                            // LWW picks the larger timestamp -> picks A's older write
            }
            System.out.printf("   +/-%5.0f ms                                              %6.2f%%                           0.00%%%n", skew, 100.0 * wrong / trials);
        }
    }

    public static void main(String[] a) { timeoutAmbiguity(); failureDetectors(); clocks(); }
}
```
**Sample output**
```text
== 1) Timeout ambiguity. Server latency: 98.5% ~ 5+exp(20) ms, 1.5% slow (0.5-3 s). Also 0.5% of requests are lost on the way in,
      and 0.5% of replies are lost on the way out (the server DID execute those). ==
client timeout   timeouts   of which the server actually EXECUTED the operation
    50 ms       12.72%     96.1% of timeouts (a blind retry would run the operation twice)
   100 ms        3.33%     84.8% of timeouts (a blind retry would run the operation twice)
   500 ms        2.50%     80.1% of timeouts (a blind retry would run the operation twice)
  1000 ms        2.17%     77.3% of timeouts (a blind retry would run the operation twice)
  3000 ms        0.99%     51.0% of timeouts (a blind retry would run the operation twice)

== 2) Failure detector: the node is perfectly HEALTHY for 24 h (86,400 heartbeats, 1/s); count false alarms. ==
detector                 false suspicions in 24 h   time to detect a REAL crash (avg)
fixed timeout   1.5 s        692                      1.02 s
fixed timeout   2.0 s        476                      1.52 s
fixed timeout   3.0 s         61                      2.52 s
fixed timeout   5.0 s         54                      4.52 s
fixed timeout  11.0 s          0                     10.52 s
phi-accrual, phi>=1         1854                      0.76 s
phi-accrual, phi>=2          967                      0.95 s
phi-accrual, phi>=3          760                      1.09 s
phi-accrual, phi>=4          677                      1.20 s
phi-accrual, phi>=8          528                      1.55 s

== 3) Last-write-wins by wall-clock. A writes x=1; 5 ms later (after hearing about it) B writes x=2. B is causally LATER and should win. ==
clock skew between servers (each clock off by up to +/-)   LWW keeps the WRONG (older) write   Lamport/logical clock
   +/-    0 ms                                                0.00%                           0.00%
   +/-    1 ms                                                0.00%                           0.00%
   +/-    5 ms                                               12.50%                           0.00%
   +/-   10 ms                                               28.13%                           0.00%
   +/-   50 ms                                               45.11%                           0.00%
   +/-  200 ms                                               48.81%                           0.00%
```
**Experiments**
1. In part 1, make the request-loss and reply-loss probabilities 0 and the slow-response probability 0. What fraction of timeouts were executed now, and why does it change with the timeout?
2. Add **idempotency**: give each operation an ID; the server remembers executed IDs; a retried request returns the stored result. Show that the *double-execution rate* is now 0 regardless of the timeout, while the timeout rate is unchanged.
3. In part 2, change the delay model to normal (mean 40 ms, σ 10 ms) with no tail. Now compare fixed vs phi-accrual detectors: which wins and why? Then add a regime shift (delays double at noon) and see which adapts.
4. Implement a **SWIM-style indirect probe**: when a heartbeat is missing, ask 3 random peers to probe; suspect only if all fail (assume independent 1% loss per path plus the node’s own tail delays). Measure the reduction in false positives.
5. In part 3, add **hybrid logical clocks** and show they never choose the wrong write while staying within the clock error of real time. Add **vector clocks** and detect when two writes are truly concurrent (no causal order).
6. Implement a simple **Lamport clock** library with `tick()`, `send()`, `receive(ts)` and test it on a scripted three-process scenario from §14.6.4.

### Lab 2: Fencing tokens (paper + code)
Write a toy lock service (single process, linearizable via `synchronized`) that issues increasing tokens, a storage class that rejects stale tokens, and two worker threads where worker 1 “pauses” (`Thread.sleep`) after acquiring the lock past its expiry. Run with and without token checks and show file/state corruption in the unfenced case and rejection in the fenced case.

### Lab 3: Fault injection on a real stack (needs Docker/Linux; not executed in the book’s sandbox)
Use **Toxiproxy** or `tc netem` to inject latency (`tc qdisc add dev eth0 root netem delay 200ms 50ms`), loss (`loss 5%`), duplication, reordering, and partitions (`iptables -A INPUT -s <peer> -j DROP`) between a client and a service from earlier chapters (TaskApi from Ch 2, MiniLb from Ch 7). Record: client timeouts, duplicate creates, how the idempotency key behaves, and what a health checker does under 5% loss. Try the Jepsen-style workflow: concurrent clients + nemesis + checker (the Ch 15 linearizability checker).

---

## 14.15 Interview perspective

- Use the vocabulary: **partial failure, timeout ambiguity, idempotency, at-least-once, fencing token, lease, majority quorum, partial synchrony**.
- When a design has a retry, say **“idempotency key, because a timeout doesn’t tell us whether it executed.”**
- When a design has a leader/lock, say **“lease + fencing token; I won’t rely on timing.”**
- When ordering matters, say **“logical or hybrid clocks, not wall-clock LWW”** and explain the skew risk.
- When you detect failures, mention **false-positive/latency trade-off, indirect probes, multiple observers, rate-limited reaction**.
- Quote availability math: **series multiplies, parallel improves, correlated failure limits it**.
- Mention **FLP** only to explain *why* consensus protocols use timeouts/randomization and guarantee safety always but liveness only when the network behaves.

---

## 14.16 Common misconceptions

1. **“A timeout means the request failed.”** It means you don’t know.
2. **“TCP guarantees my message was processed.”** It guarantees bytes reach the peer’s kernel (if at all).
3. **“We use NTP so clocks are synchronized.”** Within some error bound, which can be large and can jump.
4. **“Last-write-wins is fine.”** It silently loses causally later writes under skew.
5. **“A distributed lock guarantees mutual exclusion.”** Not without fencing: pauses and clock issues break it.
6. **“If the heartbeat stopped, the node is dead.”** It may be slow, partitioned, or paused.
7. **“Redundancy means 99.9999%.”** Only for independent failures; shared dependencies dominate.
8. **“FLP means consensus is impossible.”** It means impossible *deterministically in a fully asynchronous model*; practical protocols stay safe and are live when timing cooperates.
9. **“Partitions are rare.”** They’re routine in large networks (and partial ones are common).
10. **“Exactly-once delivery is just hard engineering.”** It’s impossible in general (Two Generals); exactly-once *effects* are achievable.
11. **“Monotonic = wall-clock but safer.”** They’re different clocks for different purposes.
12. **“Test in staging; failures are rare.”** Failures must be injected deliberately.

---

## 14.17 Chapter summary

- Distributed systems have **partial failure, unreliable networks, unreliable clocks, and unreliable execution**; you cannot distinguish slow, dead, and lost. The fallacies of distributed computing each map to a design rule.
- **Models:** the practical one is **partially synchronous, crash-recovery, non-Byzantine**; Byzantine tolerance needs 3f+1 nodes.
- **Timeouts are ambiguous:** in our experiment 96% of 50 ms “failures” had executed; the remedy is idempotency/outcome queries, not longer timeouts. **Two Generals** shows certainty is impossible over lossy links.
- **Failure detectors** trade speed against false positives (1.5 s timeout → 692 false alarms/day; zero false alarms needed ≥ 10 s); adaptive detectors help only when the model fits; use indirect probes, multiple observers, and gentle reactions.
- **Clocks:** wall-clock vs monotonic; drift (100 ppm = 8.64 s/day), skew, jumps; LWW with ±200 ms skew picks the wrong write 48.8% of the time. Use **Lamport/vector/hybrid logical clocks**; TrueTime shows how to use time safely when uncertainty is bounded.
- **Pauses, leases, and fencing:** a paused process can’t know time passed; **fencing tokens enforced by the resource** make mutual exclusion safe.
- **FLP:** deterministic consensus is impossible in fully asynchronous systems with a crash fault; practical protocols are always safe and live under partial synchrony.
- **Availability math:** series multiplies (five 99.9% components → 99.5%); parallel improves (two 99% → 99.99%) only if independent; at scale failure is routine (10,000 disks ≈ 1.8 days between failures).
- **Design for failure:** bounded everything, idempotency, leases + fencing, reversible reactions, degraded modes, and **fault-injection / deterministic simulation testing**.

### Mental models
| Concept | Mental model |
|---|---|
| Partial failure | A relay race where any runner may vanish without telling the others |
| Timeout ambiguity | A letter with no reply: it was lost, ignored, or answered in a lost return letter |
| Failure detector | Deciding someone is dead because they haven’t called: maybe their phone is off |
| Wall-clock vs monotonic | A wall clock someone may reset vs a stopwatch |
| Lamport clock | Numbering letters so replies always have higher numbers than what they answer |
| Vector clock | Each person tracking how many letters they have seen from every other person |
| Lease + fencing | A hotel key card that expires, plus a lock that ignores old cards |
| Series availability | A chain: any broken link breaks it |

---

## 14.18 Exercises

### A. Conceptual
1. Define partial failure and give two examples of how it differs from single-machine failure.
2. List five of the eight fallacies and the design practice each implies.
3. Define synchronous, asynchronous, and partially synchronous models. Which do real systems assume and why?
4. Compare crash-stop, crash-recovery, omission, and Byzantine failures. How many nodes are needed to tolerate f faults in the crash and Byzantine cases?
5. Explain the three possible worlds behind a timeout. Why can’t the client distinguish them?
6. State the Two Generals problem and one practical consequence.
7. Define completeness and accuracy for failure detectors. Why is perfect accuracy impossible in asynchronous systems?
8. Explain how SWIM reduces false positives.
9. Why should measurements of duration use monotonic clocks?
10. What is clock drift vs skew? Compute drift per day for 50 ppm.
11. What is the happens-before relation? Define Lamport timestamps and the guarantee they provide.
12. Why can’t Lamport timestamps detect concurrency? How do vector clocks?
13. Compare Lamport, vector, and hybrid logical clocks on size, causality, and closeness to real time.
14. Explain Spanner’s commit wait and what it buys.
15. Describe the GC-pause lock incident and how fencing tokens prevent it. What must the storage layer do?
16. What does FLP prove, and how do practical consensus systems circumvent it?
17. Why are leases dependent on bounded clock drift? What margin protocol is used?
18. Explain why failures are correlated and how this limits parallel-redundancy calculations.
19. Why does MTTR matter as much as MTBF? Give an example.
20. List five testing techniques for distributed systems and what each finds.

### B. Calculations
21. A request path has 6 sequential hard dependencies, each 99.95% available. Compute path availability and annual downtime. What if two of them are replaced by 2-way redundant pairs (each replica 99.9%, independent)?
22. 20,000 disks at 1.5% AFR: expected failures per year and mean time between failures (days). If each disk holds 8 TB and is re-replicated at 100 MB/s, how long does one rebuild take, and how many rebuilds are in progress on average?
23. Heartbeats every 500 ms; timeout 2.5 s. Gaps follow: 99% ~ 500±50 ms; 1% add a random 0–3 s delay. Estimate the probability that a single gap exceeds the timeout, and the expected false suspicions per day per monitored node.
24. Lamport clocks: P1 events: a, b (send m1 to P2), c. P2: d, e (receive m1), f (send m2 to P3). P3: g, h (receive m2). Assign Lamport timestamps. Which pairs are concurrent?
25. Vector clocks (3 processes). Given V(x)=[2,1,0], V(y)=[1,2,0], V(z)=[2,2,1]. Which pairs are ordered, and which concurrent?
26. A lease is 15 s; maximum drift 0.5%; the renewal RPC takes up to 200 ms. When should the holder stop using the lease and when may the grantor re-grant, in terms of local clocks?
27. Quorum arithmetic: a cluster of 7 nodes uses majority quorums. How many failures can it tolerate for reads+writes? If two data centers hold 4 and 3 nodes, which DC outage is survivable?
28. A client retries on timeout up to 3 times with each attempt independently failing (request or reply lost) with probability 4%. Probability the operation executes at least once? Probability it executes more than once if executed replies are lost with probability 2%?
29. An NTP step moves a server’s clock back 300 ms. List three timeout/ordering bugs this can cause if wall clock is used, and how monotonic clocks avoid them.
30. Compute commit wait cost: TrueTime ε = 6 ms; 10,000 commits/s. How much total waiting is added per second? Does throughput drop (assume independent concurrent transactions)?

### C. Design
31. Redesign LedgerCo’s payment call so a timeout never causes a double charge. Include idempotency keys, state machine states, reconciliation job, and what the user sees.
32. Design a safe distributed lock service usage for “only one worker updates file F.” Specify the lock service guarantees, token flow, and storage-side check.
33. Design failure detection for a 500-node cluster: protocol, parameters, and how you avoid failover storms.
34. Design a conflict-handling approach for a profile service updated from two regions with skewed clocks. Compare LWW, version vectors, and single-writer-per-field.
35. Design a fault-injection test plan for a service: faults to inject, invariants to check, and tools.
36. Choose a system model for a new internal consensus-based service in one cloud region; list assumptions, what you will test, and which assumptions are the riskiest.

### D. Debugging
37. Users in Region B occasionally see older data after updating; logs show the update’s timestamp precedes the stale record’s timestamp even though the update was later. Diagnose.
38. A monitoring system marks hosts dead during every nightly backup window; the failover automation then causes an outage. List causes and fixes.
39. A job runs on two nodes simultaneously, although you use a distributed lock with a 30 s TTL. Give two ways this can happen and fix both.
40. Metrics show negative request durations occasionally. What is the bug?
41. A client retries a timed-out “create order” and gets two orders. What design elements were missing?
42. Heartbeats sometimes arrive out of order. How does a detector that assumes monotone arrival misbehave? How should it handle it?

### E. Interview-style
43. “How do you handle a request that times out?” (Give a layered answer.)
44. “Why can’t we use timestamps to order events across servers?”
45. “How do you implement a distributed lock safely?”

### F. Teach-back
46. Explain why you can’t tell a dead server from a slow one, to a non-engineer, using a phone-call analogy.

---

## 14.19 Full answer key

**1.** Partial failure: some components fail while others continue, possibly unobserved. On a single machine, a crash typically kills the whole process and errors are deterministic; in a distributed system a remote operation may have executed although the reply was lost, or a peer may be alive but unreachable from some nodes.

**2.** Examples: network reliable → timeouts, retries, idempotency; latency zero → batching, locality, avoid chatty calls; bandwidth infinite → compression, avoid over-fetching; network secure → TLS/auth; topology changes → service discovery; one administrator → config/versioning; transport cost zero → cost awareness; homogeneous network → protocol versioning and interoperability.

**3.** Synchronous: known bounds on delays, drift; asynchronous: no bounds; partially synchronous: bounds exist but unknown or hold eventually. Real systems assume partial synchrony: networks are usually timely but sometimes not; protocols stay safe regardless and live when timely.

**4.** Crash-stop: halts forever; crash-recovery: restarts with disk intact; omission: drops messages; Byzantine: arbitrary behavior. To tolerate f faults: 2f+1 nodes (crash), 3f+1 (Byzantine).

**5.** (a) request lost (not executed); (b) executed but reply lost/late; (c) crashed mid-execution. The client only observes silence, which is identical in all three.

**6.** Two parties communicating over a lossy channel cannot reach certainty of agreement in finitely many messages, because the last message could always be lost. Consequence: exactly-once delivery is impossible; rely on idempotent operations and acknowledgments with uncertainty (and commit protocols that may block).

**7.** Completeness: crashed nodes are eventually suspected by all correct nodes. Accuracy: correct nodes aren’t suspected. In asynchronous systems a slow node is indistinguishable from a crashed one, so any detector that is complete will sometimes suspect correct nodes.

**8.** Before declaring a node dead, the suspecting node asks k others to probe it; only if all fail is it suspected. This filters out failures of the monitor’s own path, and a suspect state allows refutation.

**9.** Wall clocks can jump (NTP, leap seconds, manual changes) producing negative or huge durations; monotonic clocks never go backward.

**10.** Drift: a clock’s rate error vs true time. Skew: the difference between two clocks at an instant. 50 ppm = 50 µs/s × 86,400 s = **4.32 s/day**.

**11.** a → b if same process earlier, or a=send and b=receive, or transitively. Lamport timestamps: increment on events, attach to messages, receiver sets max+1. Guarantee: a → b implies C(a) < C(b).

**12.** Because C(a) < C(b) doesn’t imply causality; concurrent events may be ordered arbitrarily. Vector clocks compare component-wise; if neither dominates, the events are concurrent.

**13.** Lamport: one integer, consistent with causality, no concurrency detection, no relation to real time. Vector: n integers, exact causality and concurrency detection, no real time. HLC: physical-time-based plus counter; consistent with causality, near real time, compact, but doesn’t detect concurrency exactly.

**14.** Commit wait: the transaction waits out the TrueTime uncertainty (ε) before releasing locks/acknowledging, ensuring its timestamp is in the past everywhere; gives externally consistent timestamp order at ~ε latency per commit.

**15.** Worker 1 pauses past lease expiry; worker 2 gets the lock; worker 1 wakes and writes. Fencing: lock service gives increasing tokens; writes carry them; storage rejects tokens lower than the max seen. The storage layer must perform the check atomically (conditional write).

**16.** FLP: no deterministic consensus in an asynchronous system with even one crash fault. Practical systems use timeouts/failure detectors (partial synchrony) and randomization (election timeouts) to make progress, keeping safety unconditional.

**17.** A lease is judged by clocks; if holder and grantor clocks run at different rates or one pauses, they can disagree. Protocol: holder stops using the lease early (duration×(1−drift)−slack, using monotonic time), grantor waits duration×(1+drift) before re-granting.

**18.** Replicas share power, racks, software versions, config, networks, deployment pipelines, so failures co-occur. The independent-failure formula overestimates availability; the shared components bound it.

**19.** Availability = MTBF/(MTBF+MTTR). Cutting MTTR from 60 to 6 minutes at the same failure rate reduces downtime tenfold. Automation (failover, rollback) lowers MTTR.

**20.** Unit/integration tests (logic), fault injection (partitions/latency/crash), Jepsen-style history checking (linearizability violations), chaos engineering (resilience in production-like systems), deterministic simulation (reproducible rare interleavings), model checking/TLA+ (protocol design bugs), property-based tests.

**21.** Path = 0.9995⁶ = 0.99700 → **99.70%**, ≈ 26.3 h/year downtime. Replace two components with 2-way redundant pairs of 99.9% replicas: pair = 1 − 0.001² = 0.999999. New path = 0.9995⁴ × 0.999999² ≈ 0.998 × 0.999998 ≈ **99.80%** (0.9995⁴ = 0.99800); improvement modest; the remaining four components dominate.

**22.** Failures = 20,000 × 0.015 = **300/year** ≈ one per 1.2 days. Rebuild: 8 TB / 100 MB/s = 80,000 s ≈ **22.2 h**. Concurrent rebuilds = failure rate × duration = (300/8760 per hour) × 22.2 h ≈ **0.76** on average, so there is nearly always a rebuild in progress. This is why repair speed matters (Ch 11).

**23.** Gap > 2.5 s requires the 1% delayed case with an added delay > ~2.0 s (since base ~0.5 s): P(add > 2.0 of U[0,3]) = 1/3 → ≈ 0.01 × 1/3 = **0.33%** per gap. Heartbeats per day = 172,800 → ≈ **576** suspicions/day per node. (Tune the timeout above the tail.)

**24.** P1: a=1, b=2 (send m1), c=3. P2: d=1, e=max(1,2)+1=3 (receive m1), f=4 (send m2). P3: g=1, h=max(1,4)+1=5 (receive m2). Causal order: a→b→c; b→e→f→h; d→e (so d→h too); g→h. Concurrent pairs: a∥d, a∥g, b∥d, b∥g, c∥d, c∥e, c∥f, c∥g, c∥h, d∥g, e∥g, f∥g. Equal or smaller timestamps never prove causality (a, d and g all have timestamp 1 but are mutually concurrent).

**25.** x=[2,1,0], y=[1,2,0]: neither ≤ other → concurrent. x vs z=[2,2,1]: x ≤ z and ≠ → x → z. y vs z: y ≤ z → y → z.

**26.** Measure from the moment the holder *sent* the request (so the RPC latency is automatically covered): the holder stops using the lease after 15 × (1 − 0.005) = **14.925 s** on its monotonic clock; the grantor, measuring from when it *granted*, waits at least 15 × (1 + 0.005) = **15.075 s** before re-granting. The 0.15 s gap absorbs drift; pauses longer than that still require fencing.

**27.** Majority = 4; tolerates 3 failures. DC with 4 nodes down leaves 3 (no quorum) → outage; DC with 3 nodes down leaves 4 → survives. So only the 3-node DC loss is survivable.

**28.** Per attempt: the request is lost with 2% (not executed); otherwise it executes (98%), and its reply is lost with 2%. The client sees a timeout with probability 0.02 + 0.98 × 0.02 ≈ 4%. P(executed at least once in 3 attempts) = 1 − 0.02³ ≈ **99.9992%**. P(executed more than once) ≈ P(first attempt executed but reply lost) × P(retry executes) = 0.98 × 0.02 × 0.98 ≈ **1.9%**: roughly 1 in 50 operations would be duplicated by blind retries, which is why idempotency keys are mandatory for non-idempotent operations.

**29.** Examples: timeouts computed as `now() − start` become negative or too large; cache entry expiry and token validity miscomputed; event ordering by timestamp puts later events earlier; scheduled jobs fire twice. Monotonic clocks aren’t affected by wall-clock steps.

**30.** By Little’s Law the number of transactions sitting in commit wait at any moment is 10,000/s × 0.006 s = **60**. If the system has enough concurrency, throughput is not reduced, but each commit’s latency rises by about 6 ms and locks are held 6 ms longer, so a single hot row can then sustain at most about 1/(hold + 0.006 s) updates per second.

**31 (sketch).** Client generates `payment_id` (idempotency key) before calling; store `payments(id, state)` with states `CREATED → SENT → CONFIRMED | FAILED | UNKNOWN`; call processor with the idempotency key; on timeout → `UNKNOWN`, then reconciliation job queries the processor by key and resolves; user sees “processing”; never retry without key; webhooks for async confirmations.

**32 (sketch).** Linearizable lock service (etcd/ZooKeeper) grants lock with TTL and returns a fencing token (revision); worker includes the token in each write; storage does compare-and-set on `max_token`; worker renews lease via keepalive; on failure to renew, stops; correctness doesn’t depend on timing.

**33 (sketch).** SWIM-style gossip membership with indirect probes (k=3), suspicion timeout scaled with log(N), incarnation numbers to refute false suspicion, piggybacked dissemination; separate data-plane reaction (route around on suspicion; re-replicate only after N minutes); rate-limit concurrent re-replications.

**34 (sketch).** LWW loses updates under skew; version vectors detect concurrency and keep siblings for merge; single-writer-per-field (home region) avoids conflicts; combine: per-field ownership + HLC for tiebreaks + CRDT/set merges for collections.

**35 (sketch).** Inject latency/loss/partition/clock skew/crash/pause (SIGSTOP); invariants: no lost acknowledged writes, no duplicate effects, linearizable histories for registers, bounded recovery time; tools: Toxiproxy, tc/netem, Jepsen, chaos mesh, custom simulators.

**36 (sketch).** Model: partially synchronous network, crash-recovery nodes with durable logs, no Byzantine faults, clocks used only for timeouts (no correctness dependence). Riskiest: assumption that disks honor fsync, bounded clock drift for leases, and operational config consistency; test them with fault injection and storage-failure tests.

**37.** Clock skew with LWW (or timestamp conflict resolution): Region B’s clock is behind, so its later update has an earlier timestamp and loses. Fix: logical/hybrid clocks, version vectors, or route writes to a single leader.

**38.** The backup spikes IO/CPU, delaying heartbeats beyond the timeout; failure detection fires. Fixes: heartbeat on a separate thread/priority, longer timeout or adaptive detector, throttle backups, require multiple observers, rate-limit failover and verify via application health before acting.

**39.** (a) The lock holder paused beyond TTL (GC/VM), another acquired it; (b) clock/TTL mismatch or lock service failover lost the lock state. Fixes: fencing tokens checked by the resource; use a linearizable lock service; renew leases; keep critical sections idempotent.

**40.** Using wall-clock (`currentTimeMillis`) for durations; NTP step backward. Use monotonic `nanoTime`.

**41.** Missing idempotency key/unique request ID, server-side dedup, and a safe retry policy (or query-before-retry).

**42.** A detector that computes gap as `now − lastArrival` assuming monotone sequence numbers may treat an old late heartbeat as fresh evidence of liveness (or compute negative gaps). Handle with sequence numbers: ignore heartbeats with seq ≤ last, update liveness only on newer ones.

**43 (sketch).** Layered: set deadlines and timeouts; classify idempotent vs not; retry idempotent calls with backoff/jitter and budgets; use idempotency keys for non-idempotent; if unknown outcome, query status or reconcile; circuit-break; surface “pending” to the user; alert on rising timeouts.

**44 (sketch).** Clocks drift/skew and jump; timestamps from different machines aren’t comparable within the causal gap; use logical clocks, hybrid clocks, version vectors, or a single sequencer; if using time (TrueTime), bound the uncertainty.

**45 (sketch).** Use a linearizable lock service (lease with keepalive) + fencing tokens enforced by the resource; handle pause/partition; avoid Redlock-style timing assumptions; keep critical sections idempotent; design to survive lock loss.

**46.** If you call a friend and they don’t pick up, you can’t tell whether their phone is off (dead), they’re busy (slow), or the call dropped (network). You only know you got no answer, so you decide how long to wait before acting, and acting early can be wrong.

---

## 14.20 Flashcards

| Front | Back |
|---|---|
| Partial failure | Some parts fail while others run; unobservable |
| Practical model | Partially synchronous, crash-recovery, non-Byzantine |
| BFT vs crash | 3f+1 vs 2f+1 nodes |
| Timeout means | Unknown outcome (lab: 96% of 50 ms timeouts had executed) |
| Two Generals | No certainty of agreement over lossy links |
| Failure detector trade-off | Speed vs false positives (1.5 s → 692 false alarms/day) |
| SWIM | Indirect probes + gossip + suspect state |
| Monotonic clock | Durations; never backward; not comparable across machines |
| Drift | 100 ppm = 8.64 s/day |
| LWW + skew | Picks wrong write; 48.8% at ±200 ms in lab |
| Lamport clock | max(C,t)+1; a→b ⇒ C(a)<C(b) |
| Vector clock | Per-process counters; detects concurrency |
| HLC | Physical time + counter; causal and near real time |
| TrueTime | Uncertainty interval + commit wait |
| Fencing token | Increasing number; resource rejects stale tokens |
| Lease | Time-limited right; needs margins + fencing |
| FLP | No deterministic async consensus with 1 crash; use timeouts/randomization |
| Series availability | Multiply (5×99.9% = 99.5%) |
| Parallel availability | 1−(1−A)ⁿ if independent |
| Failure at scale | 10,000 disks @2% AFR ≈ 1 failure / 1.8 days |
| Deterministic simulation | Reproducible fault injection (FoundationDB style) |

## 14.21 Glossary additions

| Term | Simple Meaning | Technical Meaning | Why It Exists | Example |
|---|---|---|---|---|
| Partial failure | Some parts down | Independent component failures in a running system | Core difficulty | Lost reply |
| System model | Stated assumptions | Timing + failure assumptions for proofs | Define correctness | Crash-recovery |
| Partial synchrony | Mostly timely | Eventually bounded delays | Practical protocols | Raft |
| Byzantine fault | Lying node | Arbitrary or malicious behavior | Adversarial settings | Blockchain |
| Timeout ambiguity | Unknown outcome | Inability to tell loss vs slowness | Retry design | Double charge |
| Two Generals | Agreement over loss | Impossibility of certain agreement | Limits of protocols | Ack of ack |
| Failure detector | Liveness judge | Component suspecting crashed nodes | Failover | Heartbeat |
| Phi-accrual | Adaptive detector | Suspicion level from heartbeat stats | Tune to network | Cassandra |
| SWIM | Gossip membership | Indirect probe protocol | Scalable detection | memberlist |
| Wall-clock | Time of day | Epoch-based clock, syncable and steppable | Human time | `currentTimeMillis` |
| Monotonic clock | Stopwatch | Non-decreasing local counter | Durations | `nanoTime` |
| Clock skew / drift | Disagreement / rate error | Offset between clocks / frequency error | Ordering bugs | 80 ms |
| Happens-before | Causal order | Partial order of events by messages and program order | Reasoning | a → b |
| Lamport clock | Logical counter | Scalar clock respecting causality | Order events | (C, pid) |
| Vector clock | Causality tracker | Per-node counters detecting concurrency | Conflict detection | Dynamo |
| HLC | Hybrid clock | Physical + logical timestamp | Causal + wall-time | CockroachDB |
| TrueTime | Bounded-uncertainty time | Interval API + commit wait | External consistency | Spanner |
| Lease | Expiring right | Time-limited grant of authority | Avoid deadlock on dead holders | Leader lease |
| Fencing token | Stale-proof ticket | Monotonic token checked by resource | Safe locks | Epoch |
| STONITH | Kill the old node | Hardware fencing | Prevent split-brain | Power off |
| FLP | Impossibility theorem | No deterministic async consensus with a crash | Why timeouts/randomness | Raft design |
| MTBF / MTTR | Time between failures / to repair | Reliability metrics | Availability math | 20 days / 5 min |
| Gray failure | Slow-but-alive | Degraded node that passes liveness checks | Hard to detect | Slow disk |

---

**Next: Chapter 15: Consistency Models and the CAP Theorem.** From linearizability to eventual consistency: the spectrum of guarantees, what each costs, CAP and PACELC precisely, quorum systems revisited with a *measured* linearizability checker (spoiler: majority quorums alone are not linearizable), and how real databases label their guarantees.

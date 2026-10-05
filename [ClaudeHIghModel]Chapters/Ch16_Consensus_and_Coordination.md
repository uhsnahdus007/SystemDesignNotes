# Chapter 16: Consensus, Leader Election, and Coordination

## Chapter card

| | |
|---|---|
| **Objective** | Understand how a group of unreliable machines agrees on a single sequence of decisions: the consensus problem, Paxos intuition, Raft in depth (elections, log replication, safety, membership, snapshots, read paths), leader election and leases, coordination services (ZooKeeper, etcd, Chubby), cluster sizing and latency, consensus at scale, and how to *test* a protocol (chaos and mutation testing with a runnable Raft simulator) |
| **Why this chapter exists** | Consensus is the foundation under every strongly consistent distributed system: replicated databases, metadata services, lock services, Kubernetes, Kafka’s controller, Spanner, CockroachDB. You rarely implement it, but you constantly *use* it, and you must know what it guarantees, what it costs, and how it fails |
| **Prerequisites** | Chapters 9 (replication, failover, fencing), 14 (partial failure, FLP, clocks, leases), 15 (linearizability, CAP) |
| **Study time** | 14–18 hours including labs |
| **Tier** | `[ADV]` for protocol internals; `[MK]` for what consensus gives you, sizing, and usage patterns |
| **Mastery criteria** | (1) State the consensus problem and its safety/liveness properties. (2) Explain why majority quorums give safety and what they cost. (3) Explain single-decree Paxos and the leap to Multi-Paxos. (4) Explain Raft’s roles, terms, election rules, log replication, commit rule, and the five safety properties; explain the “current-term commit” rule (Figure 8). (5) Explain linearizable reads in Raft (ReadIndex, leases). (6) Explain membership change and snapshots. (7) Size a consensus cluster and compute commit latency. (8) Use etcd/ZooKeeper-style recipes (lock with fencing, leader election, config). (9) Describe how to test a consensus implementation and what testing cannot prove |

---

## 16.1 The Story: The Scheduler That Elected Itself Twice

**TaskForge** runs a fleet of workers that must be coordinated by **exactly one scheduler** at a time. The first design: three scheduler processes; each periodically checks a file on a shared network drive; the one whose hostname is alphabetically first among those that “look healthy” is the leader.

What happens in production:

1. A network blip isolates `sched-a` from the shared drive but not from the workers. `sched-b` and `sched-c` see `sched-a` as dead and elect `sched-b`. `sched-a` still thinks it is leader (it can reach the workers). **Both dispatch jobs**; every job runs twice.
2. Operators add a “just use a database row as a lock” fix. The database fails over to an async replica that has an older copy of the row. Now the lock says nobody holds it, and a third scheduler takes over while the previous two are still running.
3. The team writes a replicated configuration store with homemade logic: “write to all nodes, succeed if most respond.” After a partial failure, two nodes hold different values for the same key. Nobody can say which is correct.

### Pause & Predict 16.1
> What would a trustworthy way to choose a single leader look like? Think about: how many nodes must agree; what happens to a node that is cut off from the others; how a new leader can be sure it knows everything the old leader *committed*; and what the old leader should do when it realizes it has been replaced.

The answer to all of these is **consensus**.

---

## 16.2 The consensus problem `[MK/GK]`

### 16.2.1 Definition
A set of nodes, each proposing a value, must **decide on a single value** such that:
- **Agreement (safety):** no two nodes decide differently.
- **Validity (safety):** the decided value was proposed by some node (no inventing values).
- **Integrity:** each node decides at most once.
- **Termination (liveness):** every non-faulty node eventually decides.
Safety must hold **always**; liveness may require the network to behave (FLP, Ch 14).

### 16.2.2 Consensus is the universal building block
Many problems are equivalent to consensus or reduce to it:
| Problem | How consensus solves it |
|---|---|
| **Leader election** | Nodes agree on who the leader is (and in which epoch) |
| **Replicated state machine / total-order broadcast** | Repeatedly agree on the *next* command in a log; every replica applies the same commands in the same order, so all replicas compute the same state |
| **Distributed lock / lease service** | Agree on who holds the lock, with a fencing epoch |
| **Atomic commit** | Agree whether to commit or abort (with caveats, Ch 17) |
| **Unique constraint / name registration** | Agree on the order of competing claims |
| **Configuration and membership** | Agree on the current set of nodes and settings |
**Replicated state machine (RSM):** if every replica starts in the same state and applies the same deterministic commands in the same order, they stay identical. Consensus provides “the same order.” This is how etcd, ZooKeeper, Consul, Spanner, CockroachDB, TiKV, and Kafka’s controller work.

### 16.2.3 Why majorities
Any two majorities of *n* nodes intersect in at least one node. If a decision is acknowledged by a majority, any later majority (for example the voters in a new leader election) includes at least one node that knows the decision. **That intersection is the source of safety in every consensus protocol.**
- To tolerate *f* crash failures you need **n ≥ 2f + 1** nodes (majority = f + 1).
- 3 nodes tolerate 1 failure, 5 tolerate 2, 7 tolerate 3. **Even sizes waste hardware:** 4 nodes need a majority of 3 and also tolerate only 1 failure; 6 nodes tolerate 2, same as 5.
- If a majority is unavailable, the system **stops making progress** (it is CP, Ch 15) but never decides inconsistently.

---

## 16.3 Paxos: the idea `[ADV]`

Leslie Lamport’s Paxos (1989/1998) is the classical consensus protocol. You do not need to memorize it, but understanding its structure explains Raft, Zab, and Multi-Paxos.

### 16.3.1 Roles
**Proposers** (propose values), **acceptors** (vote; the fault-tolerant memory), **learners** (find out the outcome). In practice, one process plays all three roles.

### 16.3.2 Single-decree Paxos (agree on ONE value)
Each proposal has a unique, increasing **ballot number** *b*.
```text
 Phase 1 (Prepare):   proposer picks ballot b, sends PREPARE(b) to acceptors.
                      An acceptor that has not promised a higher ballot PROMISES not to accept anything < b,
                      and replies with the highest-ballot value it has already ACCEPTED (if any).
 Phase 2 (Accept):    if the proposer gets promises from a MAJORITY, it must propose the value with the highest ballot
                      among the replies (if any), otherwise its own value. It sends ACCEPT(b, value).
                      An acceptor accepts unless it promised a higher ballot. When a MAJORITY accept, the value is CHOSEN.
```
**Why it is safe:** once a value is chosen by a majority, any later proposer’s phase 1 reaches at least one acceptor that accepted it (majority intersection), so the later proposer is *forced* to re-propose that same value. Different ballots can never choose different values.
**Liveness problem:** two proposers can keep pre-empting each other with ever-higher ballots (“dueling proposers”), so no value is chosen. Fix: elect a **distinguished proposer (leader)** and use randomized back-off.

### 16.3.3 Multi-Paxos
To build a log, run one Paxos instance per log slot. With a **stable leader**, phase 1 can be done once for all future slots, so each command needs only the Accept round: **one round trip to a majority**. That is the steady-state cost of all leader-based consensus (Raft, Zab, Multi-Paxos, Viewstamped Replication). Paxos famously leaves many practical details underspecified (leader election, log gaps, membership change, snapshots), which is why real implementations diverge and why Raft was designed to specify everything.

---

## 16.4 Raft in depth `[ADV]`

Raft (Ongaro & Ousterhout, 2014) is consensus designed for understandability. It decomposes the problem into **leader election**, **log replication**, and **safety**, and fully specifies membership changes and log compaction.

### 16.4.1 State and roles
- Each server is a **follower**, **candidate**, or **leader**.
- **Time is divided into terms** (monotonically increasing integers). Each term begins with an election; at most one leader per term. Terms act as a logical clock: a server with a smaller term is stale.
- **Persistent state** (must be fsynced *before* replying to RPCs): `currentTerm`, `votedFor`, and the `log[]` of entries `(term, command)`.
- **Volatile state:** `commitIndex`, `lastApplied`; on leaders: `nextIndex[]`, `matchIndex[]` per follower.
```text
 ┌────────────┐  election timeout     ┌─────────────┐  wins majority   ┌────────────┐
 │  FOLLOWER  │ ────────────────────▶ │  CANDIDATE  │ ───────────────▶ │   LEADER   │
 │            │ ◀──────────────────── │             │                  │            │
 └────────────┘  discovers current    └─────────────┘                  └────────────┘
       ▲         leader / higher term        │ split vote → new election        │
       └──────────────── discovers higher term (any RPC) ─────────────────────┘
```

### 16.4.2 Leader election
1. A follower that hears nothing from a leader for an **election timeout** (randomized, e.g. 150–300 ms) becomes a **candidate**: increments `currentTerm`, votes for itself, resets its timer, sends **RequestVote(term, candidateId, lastLogIndex, lastLogTerm)** to all others.
2. A server grants its vote if: (a) the candidate’s term ≥ its own, (b) it has **not already voted** in this term for someone else, and (c) the candidate’s **log is at least as up-to-date** as its own (higher last term, or equal last term and ≥ length).
3. A candidate with votes from a **majority** becomes leader and immediately sends heartbeats (empty AppendEntries) to establish authority.
4. If it hears from a leader with term ≥ its own, it reverts to follower. If no one wins (split vote), timeouts fire again and a new term begins.
**Randomized timeouts** make split votes rare and quickly resolved: one server usually times out first and wins before others even become candidates. Rule of thumb: `broadcastTime ≪ electionTimeout ≪ MTBF` (e.g., 1–10 ms, 150–500 ms, months).

**Measured (Lab 1, E2).** 5 nodes start together on a clean network (delay 5–25 ms), 2,000 runs per row:
```text
timeout window (ms)    runs with no leader after 10 s   time to first leader: avg / p99 (ms)   elections started per run
[150, 151)             100.0%                                 n/a                                330.00
[150, 160)               0.0%                             1435 /   5103                          39.68
[150, 200)               0.0%                              241 /    693                          3.20
[150, 300)               0.0%                              210 /    420                          1.56
[150, 600)               0.0%                              253 /    450                          1.18
```
With **no randomization** ([150, 151) ms), every node times out at the same instant, votes for itself, and rejects everyone else: **100% of runs never elected a leader** (330 elections, all split). This is FLP in miniature: a deterministic symmetric protocol cannot break symmetry; randomness does. A narrow 10 ms window still works but needs ~40 election rounds; a 150 ms-wide window averages 1.56 elections per run and elects within ~210 ms; widening to 450 ms barely reduces splits but delays the first election (253 ms average). **The window should be several times the network delay.** A back-of-envelope: with *n* nodes and one-way delay *d* in a window of width *W*, the chance that the two earliest timeouts fall within *d* of each other is 1 − (1 − d/W)ⁿ; for n = 5, d = 15 ms, W = 150 ms that is 41%, the same order of magnitude as the lab’s 1.56 elections per run (0.56 extra elections per run on average).

### 16.4.3 Log replication
The leader appends client commands to its log and replicates them with **AppendEntries(term, leaderId, prevLogIndex, prevLogTerm, entries[], leaderCommit)**.
- **Log Matching check:** a follower accepts new entries only if its log contains an entry at `prevLogIndex` with term `prevLogTerm`; otherwise it rejects and the leader **decrements `nextIndex`** for that follower and retries until logs match, then overwrites the follower’s conflicting suffix. (Real implementations speed this up by returning a conflict hint.)
- **Commit rule:** an entry is **committed** once the leader has stored it on a **majority** of servers *and* it is from the leader’s **current term**. The leader advances `commitIndex`, tells followers via `leaderCommit`, and all servers **apply** committed entries to their state machines in order.
- **Log Matching Property:** if two logs contain an entry with the same index and term, then the logs are identical in all entries up to that index.
```text
 index:     1   2   3   4   5   6   7   8   9  10
 leader S1: 1   1   1   4   4   5   5   6   6   6      ← term of each entry
 S2:        1   1   1   4   4   5   5   6   6          (behind by one: will catch up)
 S3:        1   1   1   4                              (far behind)
 S4:        1   1   1   4   4   5   5   6   6   6   6  (has an extra UNCOMMITTED entry from a deposed leader: will be overwritten)
 S5:        1   1   1   2   2   2   3   3   3   3   3  (diverged long ago: leader will walk nextIndex back to index 3 and overwrite)
```

### 16.4.4 The safety properties `[ADV]`
Raft guarantees, at all times:
1. **Election Safety:** at most one leader per term. (Each server votes once per term; a majority is required; two majorities intersect.)
2. **Leader Append-Only:** a leader never overwrites or deletes its own entries.
3. **Log Matching:** same index + term ⇒ same prefix.
4. **Leader Completeness:** if an entry is committed in some term, it is present in the logs of leaders of all higher terms. (Ensured by the vote rule (c): a candidate must have a log at least as up-to-date as a majority, and any committed entry is on a majority, so some voter has it and would reject a candidate lacking it.)
5. **State Machine Safety:** if a server has applied the entry at an index, no other server ever applies a different entry at that index.

### 16.4.5 The current-term commit rule (Figure 8) `[ADV]`
Subtle trap: a new leader may find entries from *previous* terms on a majority of servers. It may **not** declare them committed by counting replicas alone, because a later leader (elected with a different log) could still overwrite them. The scenario (from the Raft paper’s Figure 8): S1 (term 2) replicates entry at index 2 to S2 only, then crashes; S5 wins term 3 with votes from S3, S4, S5 and writes a different entry at index 2 (not replicated); S1 returns as leader in term 4 and replicates its old term-2 entry to S3 (now on a majority!) but crashes before committing a term-4 entry; S5 can still win term 5 (its last term 3 beats S2/S3’s last term 2) and overwrite index 2 everywhere. If S1 had *committed* the term-2 entry by counting, a committed entry would be lost. **Raft’s rule: only entries from the leader’s current term are committed by counting replicas; earlier entries get committed indirectly** when a current-term entry after them is committed (Log Matching). Practical implementations have a new leader append a **no-op entry** at the start of its term so earlier entries can commit promptly.

### 16.4.6 Interacting with clients `[ADV]`
- Clients send commands to the leader (followers redirect). If the leader crashes before replying, the client **retries**, which could duplicate the command. Solution: clients attach a **unique session ID and sequence number**; the state machine remembers the latest sequence number per client and returns the saved response for duplicates (exactly-once *effect*, Ch 12).
- **Linearizable reads** cannot simply be served from the leader’s state machine: a deposed leader (partitioned away) might not know it has been replaced. Options:
  1. Put the read through the **log** (correct, expensive).
  2. **ReadIndex:** the leader records its current `commitIndex`, confirms it is still leader by exchanging heartbeats with a majority, waits until its state machine has applied up to that index, then serves the read (no log write).
  3. **Leader leases:** after a heartbeat round acknowledged by a majority, the leader may serve reads locally for a lease duration shorter than the election timeout, relying on bounded clock drift (Ch 14). Faster, but timing-dependent.
  4. **Follower reads:** a follower asks the leader for the current commit index, waits to apply it, then serves (offloads leader CPU).
  5. **Stale reads** from any replica (explicitly non-linearizable).

### 16.4.7 Cluster membership changes `[ADV]`
Changing the server set is dangerous: if different servers switch configurations at different moments, two disjoint majorities (old and new) could exist, so two leaders could be elected in the same term.
- **Joint consensus** (original Raft): first commit a transitional configuration C(old,new) in which decisions require majorities of *both* old and new sets; once committed, commit C(new). At no time can old and new decide independently.
- **Single-server changes** (Ongaro’s thesis; used by etcd and many libraries): add or remove **one server at a time**; any majority of the old config and any majority of the new differ by one server, so they always intersect. Simpler, with a known subtlety around leader election immediately after a config change.
- **Learners / non-voting members:** new servers first join as learners to catch up (so they do not slow commit or count toward the quorum), then are promoted.

### 16.4.8 Log compaction (snapshots) `[ADV]`
Logs grow without bound. Each server periodically writes a **snapshot** of its state machine covering all entries up to `lastIncludedIndex/Term` and discards the log prefix. A leader that must send entries a follower needs but has already discarded sends an **InstallSnapshot** RPC instead. Cost: snapshot creation (fork/copy-on-write or incremental), transfer time for slow followers, interaction with `nextIndex` logic.

### 16.4.9 Performance and engineering `[ADV]`
- **Latency of a commit** ≈ leader’s local fsync in parallel with (time for the *median* follower to receive, fsync, and reply): essentially **one round trip to the (f+1)-th fastest server plus a disk flush**.
- **Throughput** is limited by the leader: batching many commands per AppendEntries and per fsync (group commit), pipelining multiple outstanding AppendEntries, and parallel writes to disk and network are standard.
- **Durability rules matter:** a server must **persist** its vote and log entries before acknowledging. A disk that lies about fsync (volatile write cache) silently breaks the guarantees.
- **Prevote and check-quorum:** a server that was partitioned keeps incrementing its term and, on rejoining, forces the healthy leader to step down (“disruptive server”). **PreVote** has a candidate ask whether it *could* win before incrementing its term; **CheckQuorum** makes a leader step down if it cannot hear from a majority; **leader stickiness** (refusing to vote while a leader is known) reduces churn.
- **Timeouts:** set election timeouts well above the 99.9th percentile heartbeat delay, including GC pauses and fsync stalls; too low → leader churn and lost throughput; too high → slow failover.

---

## 16.5 Testing a consensus protocol: a Raft simulator `[ADV]`

Reading about Raft is not understanding it; **breaking it** is. Lab 1 is a discrete-event simulator (deterministic given a seed; about 300 lines) implementing election and log replication with: random message delay and loss, node crashes and restarts (state persists, volatile state lost), partitions that isolate a node, a client that proposes a command every 30 ms to whichever node thinks it is leader (including stale leaders), and a **global safety checker** that verifies, at every step:
- **Election Safety:** never two leaders in the same term.
- **State Machine Safety:** no two servers apply different entries at the same index.
- **Committed-entry durability:** a committed entry is never truncated, and **Leader Completeness**: every new leader’s log contains all entries committed so far.
This is **deterministic simulation testing** (Ch 14): any failing run is reproduced exactly from its seed.

### 16.5.1 E1: chaos testing
3,000 random 10-second runs, each with 0–20% message loss, random delays, up to 4 crashes (some permanent) and up to 2 partitions:
```text
leaders elected: 6,546   elections started: 36,969   client commands committed: 902,195   uncommitted entries truncated: 20,565   runs that never got a leader: 0
SAFETY VIOLATIONS (two leaders in a term / conflicting commits / lost committed entries / divergent state machines): 0
```
Interpretation: across ~37,000 elections, 902,195 committed commands, and 20,565 truncations of uncommitted entries (the machinery working: deposed leaders’ unreplicated entries were correctly overwritten), **no safety property was ever violated**. Elections are frequent under chaos (about 12 per run), but liveness holds: every run elected a leader.

### 16.5.2 Mutation testing: does the checker actually catch bugs?
A test that never fails proves little. We deliberately break the protocol in three classic ways and rerun 1,500 chaos runs for each:
```text
correct Raft                                                   runs with a safety violation:    0 / 1500
BUG 1: grant votes without the 'log is up to date' check       runs with a safety violation:  763 / 1500   e.g. t=6796 leader 0 (term 10) is missing committed entry 147
BUG 2: leader commits OLD-term entries by counting replicas    runs with a safety violation:    0 / 1500
BUG 3: a node may vote for several candidates in one term      runs with a safety violation:  762 / 1500   e.g. t=2740 two leaders in term 10: 1 and 2
```
- **BUG 1** (drop the up-to-date vote rule) lets a stale server win and **lose committed entries**: caught in 51% of runs (“leader 0 is missing committed entry 147”).
- **BUG 3** (multiple votes per term) produces **two leaders in one term**: caught in 51% of runs.
- **BUG 2** (the Figure 8 bug) was **not caught in 1,500 runs**. This is the most instructive result. The scenario needs a very specific interleaving of crashes, elections, and replication (with four distinct terms), and our leaders append a no-op on election, which makes the window narrower. **Random testing gives evidence, not proof**: absence of a failure in N runs says nothing about subtle interleavings. This is exactly why protocol designers also use **model checkers** (TLA+/TLC exhaustively explore small configurations; Raft and Paxos variants have TLA+ specs) and **proofs**, and why Jepsen-style testing targets specific known-hard scenarios. A good exercise (Lab 1, experiment 4) is to write a *directed* test that reproduces Figure 8 and watch BUG 2 fail.

### 16.5.3 E3: availability vs failures
5-node cluster; k nodes crash at t = 2 s and never return; commands every 30 ms; count commands committed from t = 4 s to 10 s (200 offered):
```text
crashed nodes   commands committed in t=4s..10s   cluster state
      0                       200                         has a majority: 5 of 5 alive -> works
      1                       200                         has a majority: 4 of 5 alive -> works
      2                       200                         has a majority: 3 of 5 alive -> works
      3                         0                         NO majority: 2 of 5 alive -> cannot commit
```
Exactly the 2f + 1 rule: f = 2 failures tolerated; the third failure halts progress (but, per the safety checker, never corrupts state).

### 16.5.4 E4: the isolated leader
The leader is cut off from the other four for 3 s (t = 3–6 s), then the partition heals; 500 runs:
```text
500 runs: new leader elected 225 ms after the partition began (avg); commands the stale old leader accepted but could never commit and later TRUNCATED: avg 54.6 per run; committed entries lost or conflicting: 0
```
The majority side elects a new leader in ~225 ms and keeps committing. The isolated old leader keeps accepting client commands (it does not yet know it is deposed): ~55 per run, **none of which could ever commit** (it cannot reach a majority). When the partition heals it sees the higher term, steps down, and its uncommitted suffix is truncated and replaced. Clients that were told “accepted” by the stale leader but never “committed” must retry: consensus gives safety for **committed** entries, not for requests that were merely accepted. Zero committed entries were lost.

---

## 16.6 Cluster sizing and latency `[MK]`

| Nodes (n) | Quorum | Failures tolerated | Commit waits for |
|---|---|---|---|
| 1 | 1 | 0 | Local fsync |
| 3 | 2 | 1 | Fastest 1 follower (+ leader) |
| 5 | 3 | 2 | 2nd fastest follower |
| 7 | 4 | 3 | 3rd fastest follower |
| 4 | 3 | 1 | 2nd fastest follower (**no better than 3**) |
| 6 | 4 | 2 | 3rd fastest follower (**no better than 5**) |
- **Larger clusters are not faster or higher-throughput:** more replicas mean more replication traffic from the leader and a later fastest-majority; use 3 or 5 (7 rarely). Scale reads via follower reads/learners and scale data via sharding (many small consensus groups, §16.10).
- **Commit latency** ≈ RTT to the (f+1)-th closest member (counting the leader as the first) + fsync. Example, 5 nodes with RTTs from the leader of 1, 2, 40, 80 ms: the leader plus 2 followers form a majority (3 of 5), so commit waits for the second-fastest follower (2 ms) → ≈ 2 ms + fsync, not 80 ms.
- **Geo-distribution:** 3 regions with one or two nodes each: every commit pays the RTT to the nearest *other* region; placing 2 nodes in region A, 2 in B, 1 in C (n = 5) lets any region that holds the leader commit with its in-region peer plus one remote ack. Think in terms of **which sets of nodes form a majority and what their RTTs are**.
- **Failure domains:** place nodes in different racks/AZs; with 3 nodes in 3 AZs you survive an AZ loss; with 2 AZs you can’t tolerate losing the AZ holding the majority.
- **Disaster recovery:** losing a majority permanently means data may be unrecoverable from the cluster alone; keep snapshots/backups, and know the (dangerous) procedure to force a new cluster from the surviving node’s data (it may lack committed entries).

---

## 16.7 Leader election and leases in practice `[MK]`

Most applications do not implement Raft; they use a coordination service to elect a leader among *their own* processes.

### 16.7.1 Pattern: lease in a linearizable store
```text
 candidate: PUT /service/leader = myId  with lease TTL=10s, only if key does not exist   (compare-and-set)
 if success → I am leader (epoch/revision = fencing token); keep the lease alive with periodic keepalive
 if exists  → watch the key; when it is deleted/expires, try again
 leader must stop acting if it fails to renew within TTL (using a monotonic clock), and attach the fencing token to writes
```
- The **store** (etcd/ZooKeeper/Consul) gives linearizability and a monotonically increasing revision that becomes the **fencing token** (Ch 14).
- **Kubernetes** controllers and schedulers use exactly this: a `Lease` object updated by the holder; others watch.
- **ZooKeeper recipe:** each candidate creates an **ephemeral sequential znode** under `/election`; the lowest sequence number is leader; each other candidate **watches only its immediate predecessor** (avoids the herd effect when the leader dies).
- **Pitfalls:** TTL too short → needless failovers; too long → slow recovery; clock/pause issues (fence!); a leader that loses its session must stop immediately; renewing through a busy event loop that can stall; thundering herd of candidates on expiry (use jitter/predecessor watch).

---

## 16.8 Coordination services `[MK/GK]`

Consensus is packaged as a small, strongly consistent, highly available store for **coordination data**.

| Service | Model | Highlights |
|---|---|---|
| **ZooKeeper** (Zab) `[Public]` | Hierarchical znodes (like a file system); persistent, **ephemeral** (deleted when the client session ends), **sequential** nodes; **watches** (one-shot notifications); sessions with timeouts | Linearizable writes; reads served by any server may be stale unless `sync()` precedes; battle-tested (Hadoop, Kafka (legacy), HBase, Solr) |
| **etcd** (Raft) | Flat key-value with **MVCC revisions**; **leases** with keepalives; **watches** (streaming, from a revision, not lossy); **transactions** (compare-and-swap with multi-key conditions) | Linearizable reads by default (`serializable` option for stale); backbone of Kubernetes |
| **Consul** (Raft) | KV + service catalog + health checks + sessions | Service discovery focus; multi-datacenter federation |
| **Chubby** (Paxos) `[Public]` | Coarse-grained lock service with file-like API, advisory locks, sequencers (fencing tokens), cached by clients | Google’s original; influenced ZooKeeper |

### 16.8.1 What to store
**Small, critical, slowly changing metadata:** leader identity, shard maps (Ch 10), cluster membership, feature flags, configuration, locks/leases, schema versions. **Not** application data, high-rate counters, or anything large: every write is a consensus round through one leader, with all data kept in memory/replicated to every member (etcd recommends a few GB; ZooKeeper znodes ≤ 1 MB). Overloading the coordination service with chatty clients, huge watch sets, or high-churn ephemeral nodes is a classic way to take down an entire platform.

### 16.8.2 Recipes (all rely on linearizable compare-and-set + watches/leases)
- **Distributed lock with fencing:** create key with lease; use the creation revision/zxid as the fencing token; resource rejects stale tokens.
- **Leader election:** §16.7.
- **Service discovery / membership:** ephemeral nodes or leased keys; watchers see joins/leaves.
- **Configuration distribution:** watch a prefix; apply updates atomically by version; keep last-known-good locally (fail static).
- **Barrier / rendezvous:** wait until N participants have registered.
- **Queues:** possible but discouraged; use a real queue (Ch 12).
- **Unique ID/ sequence allocation:** atomic counters or ranges.

### 16.8.3 Failure behavior to design for
- **Session/lease expiry:** all ephemeral state (locks, memberships) of a client vanishes when its session expires (GC pause, network blip). Clients must handle “connection lost” vs “session expired” differently and **stop acting** when unsure.
- **Quorum loss:** writes fail; reads may continue if stale reads are allowed; applications must **fail static** (keep running with last known config) instead of crashing.
- **Watch semantics:** ZooKeeper watches are one-shot and may miss intermediate changes; etcd watches stream events by revision (resume after reconnect).
- **Herd effects:** thousands of clients watching one key all wake at once.
- **Version skew** during rolling upgrades.

---

## 16.9 The consensus family `[GK]`

| Protocol | Notes |
|---|---|
| **Paxos / Multi-Paxos** | The original; Chubby, Spanner, many internal systems |
| **Raft** | Understandable; etcd, Consul, CockroachDB, TiKV, RethinkDB, Kafka KRaft |
| **Zab** | ZooKeeper’s atomic broadcast; primary-order guarantees |
| **Viewstamped Replication** | Predecessor to Raft; view changes ≈ terms |
| **EPaxos** | Leaderless for commutative commands; lower WAN latency; complex |
| **Flexible Paxos** | Quorums for phase 1 and phase 2 need only intersect each other (not each be a majority): trade-offs for latency |
| **Chain replication / CRAQ** | Strongly consistent replication along a chain; high throughput; not majority-based `[Public]` |
| **PBFT, Tendermint, HotStuff** | Byzantine fault tolerance: 3f + 1 nodes; used in blockchains |
| **Paxos vs Raft** | Same safety core; Raft prescribes a strong leader and a complete log/membership design; Paxos gives more flexibility (and ambiguity) |

---

## 16.10 Consensus at scale `[GK]`

One consensus group handles one log: limited by one leader’s CPU, disk, and network, and holds all data on every member. Large systems run **many small groups**:
- **Multi-Raft / per-range consensus** (CockroachDB, TiKV, YugabyteDB, Spanner): the key space is split into ranges of ~64–512 MB, each its own Raft/Paxos group (3–5 replicas) with its own leader; leaders are spread across nodes for balance; thousands of groups per node share heartbeats and I/O.
- **Cross-group operations** (transactions touching two ranges) need an atomic commit protocol on top (2PC across groups, Ch 17); consensus within each group makes each participant fault tolerant, which largely removes 2PC’s classic blocking problem.
- **Metadata** (which node leads which range, range boundaries) is itself stored in a small consensus group.
- **Leaseholder reads:** one replica (usually the leader) holds a lease and serves reads locally; **follower reads** serve bounded-stale reads; **geo-partitioning** places ranges’ leaders near their users.
- **Hardware matters:** NVMe fsync of 100 µs vs 2–10 ms on network disks changes commit latency and throughput by an order of magnitude.

---

## 16.11 Failure modes and operational pitfalls

| Failure | Mechanism | Mitigation |
|---|---|---|
| **fsync lies / disk write cache** | Votes/log lost after power failure → violated guarantees | Enterprise disks/battery-backed cache, verify with power-loss tests |
| **Slow disk on leader** | Commit latency and election timeouts | Dedicated fast disks, monitor `fsync` p99, leadership transfer |
| **Leader churn / election storms** | Timeouts too low, GC pauses, noisy network | Tune timeouts > p99.9 heartbeat latency; PreVote; CheckQuorum |
| **Disruptive rejoining server** | Isolated node’s high term deposes leader | PreVote; leader stickiness |
| **Stale-leader reads** | Deposed leader answering reads | ReadIndex or lease-checked reads |
| **Quorum loss** | Majority of nodes down | Spread across AZs; alert at n-1; documented recovery; backups |
| **Wrong cluster size** | 2 or 4 nodes (no benefit), 1 node (no tolerance) | Use 3/5; automate membership with learners |
| **Membership-change bugs** | Two disjoint majorities | Joint consensus or one-at-a-time changes, never replace many at once |
| **Snapshot/transfer overload** | Follower catch-up saturates leader or network | Throttle, incremental snapshots, learner stage |
| **Log/state growth** | No compaction; big values | Snapshots, size limits (etcd quotas), TTL cleanup |
| **Coordination service as a database** | High write rate, large values, huge watch sets | Keep data small; move bulk data elsewhere; quota alerts |
| **Session expiry storms** | Network blip expires thousands of sessions | Longer session timeouts with fencing; avoid recreating state in lockstep |
| **Cross-region cluster with high RTT** | Every commit pays WAN latency; timeouts mis-tuned | Regional clusters + async replication, or deliberate geo-consensus with tuned timeouts |
| **Clock-dependent leases** | Clock jumps/drift/pauses | Monotonic clocks, safety margins, fencing tokens |
| **Unverified implementation** | Subtle protocol bugs (e.g., Figure 8) | Use well-tested libraries; TLA+/model checking; Jepsen; deterministic simulation |
| **Upgrades** | Mixed versions, protocol changes | Rolling, one node at a time, respect compatibility matrix |

---

## 16.12 Labs

### Lab 1: Raft simulator with chaos and mutation testing (Java 17+)
Run with `java RaftSim.java` (takes about a minute; fully deterministic).
```java
import java.util.*;

/** A compact discrete-event simulator of Raft (leader election + log replication) with fault injection and safety checking. */
public class RaftSim {
    static int BUG = 0;   // 0 = correct Raft. 1 = skip the 'candidate log is up to date' vote check; 2 = commit old-term entries by counting replicas; 3 = allow voting for several candidates per term
    enum Role { FOLLOWER, CANDIDATE, LEADER }
    record Entry(int term, int cmd) {}
    static class Msg {
        String type; int from, to, term;
        int lastIdx, lastTerm; boolean granted;                                   // RequestVote / reply
        int prevIdx, prevTerm, leaderCommit; List<Entry> entries = List.of();       // AppendEntries
        boolean success; int matchIdx;                                            // AppendEntries reply
    }
    static class Cfg {
        int n = 5, electionMin = 150, electionMax = 300, heartbeat = 50, netMin = 5, netMax = 25;
        double drop = 0; long endTime = 10_000; int proposalEvery = 30;
        List<long[]> crashes = new ArrayList<>();         // {node, crashAt, restartAt (-1 = never)}
        List<long[]> partitions = new ArrayList<>();      // {node isolated, from, to}
    }
    static class Node {
        int id; boolean alive = true; int term = 0; Integer votedFor = null; Role role = Role.FOLLOWER;
        List<Entry> log = new ArrayList<>(List.of(new Entry(0, 0)));               // index 0 is a sentinel
        int commitIndex = 0, lastApplied = 0, votes = 0; int[] nextIndex, matchIndex; long electionGen = 0, hbGen = 0;
    }
    record Ev(long t, long seq, Runnable r) {}

    static class Result {
        List<String> violations = new ArrayList<>(); long firstLeaderAt = -1; int electionsStarted = 0, leadersElected = 0, committed = 0, truncatedUncommitted = 0, proposalsOnStaleLeader = 0;
        List<long[]> leaderLog = new ArrayList<>();                                // {time, term, node}
    }

    static class Cluster {
        final Cfg c; final Random rnd; final Node[] nodes; final PriorityQueue<Ev> q = new PriorityQueue<>(Comparator.comparingLong(Ev::t).thenComparingLong(Ev::seq));
        long now = 0, seq = 0; int nextCmd = 1; final Result res = new Result();
        final Map<Integer, Integer> leaderOfTerm = new HashMap<>(); final Map<Integer, Entry> appliedGlobal = new HashMap<>(), committedGlobal = new HashMap<>();

        Cluster(Cfg c, long seed) { this.c = c; this.rnd = new Random(seed); nodes = new Node[c.n]; for (int i = 0; i < c.n; i++) { nodes[i] = new Node(); nodes[i].id = i; } }
        void at(long t, Runnable r) { q.add(new Ev(t, seq++, r)); }
        boolean blocked(int a, int b) { for (long[] p : c.partitions) if (now >= p[1] && now < p[2] && ((a == p[0]) != (b == p[0]))) return true; return false; }
        void violation(String s) { if (res.violations.size() < 5) res.violations.add("t=" + now + " " + s); }

        void send(int from, int to, Msg m) {
            m.from = from; m.to = to;
            if (rnd.nextDouble() < c.drop || blocked(from, to)) return;
            long delay = c.netMin + rnd.nextInt(c.netMax - c.netMin + 1);
            at(now + delay, () -> { if (nodes[to].alive && !blocked(from, to)) handle(nodes[to], m); });
        }
        void resetElectionTimer(Node n) {
            long gen = ++n.electionGen; long timeout = c.electionMin + (c.electionMax > c.electionMin ? rnd.nextInt(c.electionMax - c.electionMin) : 0);
            at(now + timeout, () -> { if (n.alive && n.electionGen == gen && n.role != Role.LEADER) startElection(n); });
        }
        void startElection(Node n) {
            n.role = Role.CANDIDATE; n.term++; n.votedFor = n.id; n.votes = 1; res.electionsStarted++; resetElectionTimer(n);
            for (Node o : nodes) if (o != n) { Msg m = new Msg(); m.type = "RV"; m.term = n.term; m.lastIdx = n.log.size() - 1; m.lastTerm = n.log.get(n.log.size() - 1).term(); send(n.id, o.id, m); }
        }
        void stepDown(Node n, int term) { n.term = term; n.votedFor = null; if (n.role != Role.FOLLOWER) { n.role = Role.FOLLOWER; n.hbGen++; } }

        void handle(Node n, Msg m) {
            if (m.term > n.term) stepDown(n, m.term);
            switch (m.type) {
                case "RV" -> {
                    boolean upToDate = BUG == 1 || m.lastTerm > n.log.get(n.log.size() - 1).term() || (m.lastTerm == n.log.get(n.log.size() - 1).term() && m.lastIdx >= n.log.size() - 1);
                    boolean grant = m.term == n.term && (n.votedFor == null || n.votedFor == m.from || BUG == 3) && upToDate;
                    if (grant) { n.votedFor = m.from; resetElectionTimer(n); }
                    Msg r = new Msg(); r.type = "RVR"; r.term = n.term; r.granted = grant; send(n.id, m.from, r);
                }
                case "RVR" -> { if (n.role == Role.CANDIDATE && m.term == n.term && m.granted && ++n.votes > c.n / 2) becomeLeader(n); }
                case "AE" -> {
                    Msg r = new Msg(); r.type = "AER"; r.term = n.term;
                    if (m.term < n.term) { r.success = false; send(n.id, m.from, r); return; }
                    if (n.role != Role.FOLLOWER) { n.role = Role.FOLLOWER; n.hbGen++; }
                    resetElectionTimer(n);
                    if (m.prevIdx >= n.log.size() || n.log.get(m.prevIdx).term() != m.prevTerm) { r.success = false; send(n.id, m.from, r); return; }
                    for (int i = 0; i < m.entries.size(); i++) {
                        int idx = m.prevIdx + 1 + i;
                        if (idx < n.log.size() && n.log.get(idx).term() != m.entries.get(i).term()) {                  // conflict: truncate this and everything after
                            for (int k = idx; k < n.log.size(); k++) {
                                Entry old = n.log.get(k);
                                if (old.equals(committedGlobal.get(k))) violation("node " + n.id + " truncated COMMITTED entry at index " + k); else if (old.cmd() >= 0) res.truncatedUncommitted++;
                            }
                            n.log.subList(idx, n.log.size()).clear();
                        }
                        if (idx >= n.log.size()) n.log.add(m.entries.get(i));
                    }
                    int lastNew = m.prevIdx + m.entries.size();
                    if (m.leaderCommit > n.commitIndex) { n.commitIndex = Math.min(m.leaderCommit, lastNew); apply(n); }
                    r.success = true; r.matchIdx = lastNew; send(n.id, m.from, r);
                }
                case "AER" -> {
                    if (n.role != Role.LEADER || m.term != n.term) return;
                    if (m.success) { n.matchIndex[m.from] = Math.max(n.matchIndex[m.from], m.matchIdx); n.nextIndex[m.from] = n.matchIndex[m.from] + 1; advanceCommit(n); }
                    else { n.nextIndex[m.from] = Math.max(1, n.nextIndex[m.from] - 1); sendAppend(n, m.from); }
                }
            }
        }
        void becomeLeader(Node n) {
            n.role = Role.LEADER; res.leadersElected++; if (res.firstLeaderAt < 0) res.firstLeaderAt = now; res.leaderLog.add(new long[]{now, n.term, n.id});
            Integer prev = leaderOfTerm.putIfAbsent(n.term, n.id); if (prev != null && prev != n.id) violation("two leaders in term " + n.term + ": " + prev + " and " + n.id);
            for (Map.Entry<Integer, Entry> e : committedGlobal.entrySet())                                              // Leader Completeness
                if (e.getKey() >= n.log.size() || !n.log.get(e.getKey()).equals(e.getValue())) violation("leader " + n.id + " (term " + n.term + ") is missing committed entry " + e.getKey());
            n.nextIndex = new int[c.n]; n.matchIndex = new int[c.n]; Arrays.fill(n.nextIndex, n.log.size());
            n.log.add(new Entry(n.term, -1));                                                                           // no-op so earlier-term entries can be committed
            long gen = ++n.hbGen; sendHeartbeats(n); n.electionGen++;
            scheduleHeartbeat(n, gen);
        }
        void scheduleHeartbeat(Node n, long gen) { at(now + c.heartbeat, () -> { if (n.alive && n.role == Role.LEADER && n.hbGen == gen) { sendHeartbeats(n); scheduleHeartbeat(n, gen); } }); }
        void sendHeartbeats(Node n) { for (Node o : nodes) if (o != n) sendAppend(n, o.id); }
        void sendAppend(Node n, int f) {
            Msg m = new Msg(); m.type = "AE"; m.term = n.term; int next = Math.min(n.nextIndex[f], n.log.size()); m.prevIdx = next - 1; m.prevTerm = n.log.get(m.prevIdx).term();
            m.entries = new ArrayList<>(n.log.subList(next, Math.min(n.log.size(), next + 20))); m.leaderCommit = n.commitIndex; send(n.id, f, m);
        }
        void advanceCommit(Node n) {
            for (int N = n.log.size() - 1; N > n.commitIndex; N--) {
                if (n.log.get(N).term() != n.term && BUG != 2) break;
                int count = 1; for (int j = 0; j < c.n; j++) if (j != n.id && n.matchIndex[j] >= N) count++;
                if (count > c.n / 2) {
                    for (int k = n.commitIndex + 1; k <= N; k++) { Entry e = n.log.get(k); Entry old = committedGlobal.putIfAbsent(k, e); if (old != null && !old.equals(e)) violation("conflicting COMMIT at index " + k); }
                    n.commitIndex = N; apply(n); sendHeartbeats(n); break;
                }
            }
        }
        void apply(Node n) {
            while (n.lastApplied < n.commitIndex) { n.lastApplied++; Entry e = n.log.get(n.lastApplied); Entry old = appliedGlobal.putIfAbsent(n.lastApplied, e);
                if (old != null && !old.equals(e)) violation("State Machine Safety: node " + n.id + " applied " + e + " at index " + n.lastApplied + " but another node applied " + old); }
        }
        void crash(Node n) { n.alive = false; n.hbGen++; n.electionGen++; }
        void restart(Node n) { n.alive = true; n.role = Role.FOLLOWER; n.commitIndex = 0; n.lastApplied = 0; resetElectionTimer(n); }

        Result run() {
            for (Node n : nodes) resetElectionTimer(n);
            for (long[] cr : c.crashes) { at(cr[1], () -> crash(nodes[(int) cr[0]])); if (cr[2] >= 0) at(cr[2], () -> restart(nodes[(int) cr[0]])); }
            for (long t = c.proposalEvery; t < c.endTime; t += c.proposalEvery) at(t, () -> {                          // clients send a command to whoever believes it is leader
                List<Node> leaders = new ArrayList<>(); for (Node n : nodes) if (n.alive && n.role == Role.LEADER) leaders.add(n);
                if (leaders.isEmpty()) return; Node l = leaders.get(rnd.nextInt(leaders.size()));
                if (leaders.size() > 1 && l.term < leaders.stream().mapToInt(x -> x.term).max().getAsInt()) res.proposalsOnStaleLeader++;
                l.log.add(new Entry(l.term, nextCmd++)); sendHeartbeats(l);
            });
            while (!q.isEmpty() && q.peek().t() <= c.endTime) { Ev e = q.poll(); now = e.t(); e.r().run(); }
            res.committed = (int) committedGlobal.values().stream().filter(e -> e.cmd() >= 0).count();
            return res;
        }
    }

    // ------------------------------------------------------------------------------------------------------------
    static Cfg chaosCfg(Random r) {
        Cfg c = new Cfg(); c.drop = r.nextDouble() * 0.2; c.netMin = 2 + r.nextInt(10); c.netMax = c.netMin + 5 + r.nextInt(60);
        int crashes = r.nextInt(5); for (int i = 0; i < crashes; i++) { long at = 300 + r.nextInt(7500); c.crashes.add(new long[]{r.nextInt(5), at, r.nextInt(4) == 0 ? -1 : at + 200 + r.nextInt(1800)}); }
        int parts = r.nextInt(3); for (int i = 0; i < parts; i++) { long from = 300 + r.nextInt(7000); c.partitions.add(new long[]{r.nextInt(5), from, from + 500 + r.nextInt(2500)}); }
        return c;
    }

    public static void main(String[] args) {
        // ---------------------------------------------------------------- E1: chaos testing for safety
        int runs = 3000; long leaders = 0, elections = 0, committed = 0, viol = 0, truncated = 0, noLeaderRuns = 0; Random meta = new Random(1);
        for (int s = 1; s <= runs; s++) {
            Cfg c = chaosCfg(meta); Result r = new Cluster(c, s).run();
            leaders += r.leadersElected; elections += r.electionsStarted; committed += r.committed; truncated += r.truncatedUncommitted; viol += r.violations.size(); if (r.firstLeaderAt < 0) noLeaderRuns++;
            if (!r.violations.isEmpty()) System.out.println("VIOLATION in run " + s + ": " + r.violations);
        }
        System.out.println("== E1) Chaos test: " + runs + " random 10-second runs (5 nodes; 0-20% message loss; random delays; up to 4 crashes incl. permanent; up to 2 partitions) ==");
        System.out.printf("leaders elected: %,d   elections started: %,d   client commands committed: %,d   uncommitted entries truncated: %,d   runs that never got a leader: %d%n", leaders, elections, committed, truncated, noLeaderRuns);
        System.out.printf("SAFETY VIOLATIONS (two leaders in a term / conflicting commits / lost committed entries / divergent state machines): %d%n", viol);

        // ---------------------------------------------------------------- mutation test: does the safety checker actually catch broken protocols?
        System.out.println("\n== Mutation test: break Raft on purpose and see whether the checker notices (1,500 chaos runs each) ==");
        String[] names = {"correct Raft", "BUG 1: grant votes without the 'log is up to date' check", "BUG 2: leader commits OLD-term entries by counting replicas", "BUG 3: a node may vote for several candidates in one term"};
        for (int b = 0; b <= 3; b++) {
            BUG = b; int bad = 0; long total = 0; Random mm = new Random(1); String example = "";
            for (int sd = 1; sd <= 1500; sd++) { Result r = new Cluster(chaosCfg(mm), sd).run(); if (!r.violations.isEmpty()) { bad++; total += r.violations.size(); if (example.isEmpty()) example = r.violations.get(0); } }
            System.out.printf("%-62s runs with a safety violation: %4d / 1500   e.g. %s%n", names[b], bad, example.isEmpty() ? "-" : example);
        }
        BUG = 0;

        // ---------------------------------------------------------------- E2: randomized election timeouts
        System.out.println("\n== E2) Why election timeouts are randomized. 5 nodes start together on a clean network (delay 5-25 ms); 2,000 runs per row; 10 s horizon. ==");
        System.out.println("timeout window (ms)    runs with no leader after 10 s   time to first leader: avg / p99 (ms)   elections started per run");
        for (int[] w : new int[][]{{150, 151}, {150, 160}, {150, 200}, {150, 300}, {150, 600}}) {
            int none = 0; long elec = 0; List<Long> times = new ArrayList<>();
            for (int s = 0; s < 2000; s++) { Cfg c = new Cfg(); c.electionMin = w[0]; c.electionMax = w[1]; c.endTime = 10_000; Result r = new Cluster(c, 10_000 + s).run();
                elec += r.electionsStarted; if (r.firstLeaderAt < 0) none++; else times.add(r.firstLeaderAt); }
            Collections.sort(times);
            System.out.printf("[%3d, %3d)             %5.1f%%                           %s                          %.2f%n", w[0], w[1], 100.0 * none / 2000,
                times.isEmpty() ? "      n/a      " : String.format("%6.0f / %6d", times.stream().mapToLong(x -> x).average().getAsDouble(), times.get((int) (times.size() * 0.99) - 1)), elec / 2000.0);
        }

        // ---------------------------------------------------------------- E3: how many failures can we survive?
        System.out.println("\n== E3) Availability vs number of crashed nodes (5-node cluster; nodes crash at t=2 s and never return; commands sent every 30 ms). ==");
        System.out.println("crashed nodes   commands committed in t=4s..10s   cluster state");
        for (int k = 0; k <= 3; k++) {
            Cfg base = new Cfg(); Cluster probe = new Cluster(base, 99); Cfg c = new Cfg();
            for (int i = 0; i < k; i++) c.crashes.add(new long[]{i, 2000, -1});                  // crash nodes 0..k-1 (node IDs are arbitrary)
            Cluster cl = new Cluster(c, 99); long[] before = {0};
            cl.at(4000, () -> before[0] = cl.committedGlobal.values().stream().filter(e -> e.cmd() >= 0).count());
            Result r = cl.run(); long after = cl.committedGlobal.values().stream().filter(e -> e.cmd() >= 0).count();
            System.out.printf("      %d                     %5d                         %s%n", k, after - before[0], (5 - k) > 2 ? "has a majority: " + (5 - k) + " of 5 alive -> works" : "NO majority: " + (5 - k) + " of 5 alive -> cannot commit");
        }

        // ---------------------------------------------------------------- E4: the isolated leader
        System.out.println("\n== E4) The leader is partitioned away from the other 4 nodes for 3 s (t=3s..6s), then the partition heals. ==");
        int ok = 0, trials = 500; long staleAccepted = 0, truncatedTotal = 0, committedLost = 0, reElect = 0, reElectN = 0;
        for (int s = 0; s < trials; s++) {
            Cfg warm = new Cfg(); warm.endTime = 3000; Cluster w = new Cluster(warm, 500 + s); w.run(); int oldLeader = -1; for (Node n : w.nodes) if (n.role == Role.LEADER) oldLeader = n.id; if (oldLeader < 0) continue;
            Cfg c = new Cfg(); c.endTime = 10_000; c.partitions.add(new long[]{oldLeader, 3000, 6000}); Cluster cl = new Cluster(c, 500 + s); Result r = cl.run();
            truncatedTotal += r.truncatedUncommitted; staleAccepted += r.proposalsOnStaleLeader; committedLost += r.violations.size(); ok++;
            for (long[] ll : r.leaderLog) if (ll[0] > 3000 && ll[2] != oldLeader) { reElect += ll[0] - 3000; reElectN++; break; }
        }
        System.out.printf("%d runs: new leader elected %.0f ms after the partition began (avg); commands the stale old leader accepted but could never commit and later TRUNCATED: avg %.1f per run; committed entries lost or conflicting: %d%n",
            ok, reElect / (double) reElectN, truncatedTotal / (double) ok, committedLost);
    }
}
```
**Sample output**
```text
== E1) Chaos test: 3000 random 10-second runs (5 nodes; 0-20% message loss; random delays; up to 4 crashes incl. permanent; up to 2 partitions) ==
leaders elected: 6,546   elections started: 36,969   client commands committed: 902,195   uncommitted entries truncated: 20,565   runs that never got a leader: 0
SAFETY VIOLATIONS (two leaders in a term / conflicting commits / lost committed entries / divergent state machines): 0

== Mutation test: break Raft on purpose and see whether the checker notices (1,500 chaos runs each) ==
correct Raft                                                   runs with a safety violation:    0 / 1500   e.g. -
BUG 1: grant votes without the 'log is up to date' check       runs with a safety violation:  763 / 1500   e.g. t=6796 leader 0 (term 10) is missing committed entry 147
BUG 2: leader commits OLD-term entries by counting replicas    runs with a safety violation:    0 / 1500   e.g. -
BUG 3: a node may vote for several candidates in one term      runs with a safety violation:  762 / 1500   e.g. t=2740 two leaders in term 10: 1 and 2

== E2) Why election timeouts are randomized. 5 nodes start together on a clean network (delay 5-25 ms); 2,000 runs per row; 10 s horizon. ==
timeout window (ms)    runs with no leader after 10 s   time to first leader: avg / p99 (ms)   elections started per run
[150, 151)             100.0%                                 n/a                                330.00
[150, 160)               0.0%                             1435 /   5103                          39.68
[150, 200)               0.0%                              241 /    693                          3.20
[150, 300)               0.0%                              210 /    420                          1.56
[150, 600)               0.0%                              253 /    450                          1.18

== E3) Availability vs number of crashed nodes (5-node cluster; nodes crash at t=2 s and never return; commands sent every 30 ms). ==
crashed nodes   commands committed in t=4s..10s   cluster state
      0                       200                         has a majority: 5 of 5 alive -> works
      1                       200                         has a majority: 4 of 5 alive -> works
      2                       200                         has a majority: 3 of 5 alive -> works
      3                         0                         NO majority: 2 of 5 alive -> cannot commit

== E4) The leader is partitioned away from the other 4 nodes for 3 s (t=3s..6s), then the partition heals. ==
500 runs: new leader elected 225 ms after the partition began (avg); commands the stale old leader accepted but could never commit and later TRUNCATED: avg 54.6 per run; committed entries lost or conflicting: 0
```
**Experiments**
1. **Read the code first** and trace one election and one replication by hand using `System.out` prints in `startElection`, `becomeLeader`, and the `AE` handler for a single clean run.
2. Add a **client-visible commit acknowledgement**: when a leader’s `commitIndex` passes a client command’s index, record the command as “acknowledged.” At the end of each run, verify that every acknowledged command appears in the final applied log. Explain why commands that were merely *accepted* by a stale leader (but never acknowledged) are not a violation.
3. Implement **PreVote** (a candidate first sends a non-binding RequestVote with `term+1` without incrementing its own term). Reproduce the disruptive-server problem (isolate a follower for 3 s so its term inflates; on rejoin the healthy leader is deposed), then show PreVote prevents it.
4. Write a **directed test for Figure 8**: script crashes and message drops to reproduce the four-step scenario with 5 nodes, then enable `BUG = 2` and watch the safety checker fire. What minimum number of terms is required?
5. Add **snapshots/log compaction** and the `InstallSnapshot` path; test with a follower that is down long enough for the leader to discard its needed entries.
6. Implement **membership change** (single-server add/remove) and design a chaos test that changes membership while crashing nodes.
7. Add a **linearizable read path** (ReadIndex) and a **lease-based read path**; inject clock drift and a partitioned old leader and show the lease version returning stale reads when drift exceeds the margin while ReadIndex never does.
8. Measure the **commit latency distribution** as a function of the network delay and of cluster size (3, 5, 7), and confirm it follows the (f+1)-th fastest follower.

### Lab 2: etcd in practice (needs Docker; not executed in the book’s sandbox)
```bash
# three-node etcd cluster (docker compose recommended), then:
etcdctl --endpoints=... endpoint status -w table          # find the leader, raft term, raft index
etcdctl put /config/feature_x on
etcdctl get /config/feature_x -w json                     # note mod_revision (a fencing-token candidate)
etcdctl lease grant 10                                     # TTL lease
etcdctl put /locks/job1 worker-A --lease=<leaseID>
etcdctl txn -i                                             # compare create_revision==0 then put: lock acquisition as a transaction
etcdctl watch --prefix /config                             # streaming watch
etcdctl move-leader <memberID>                             # leadership transfer
# kill the leader container and watch re-election time; kill a second node and observe that writes fail but linearizable reads fail too
etcdctl get /config/feature_x --consistency=s              # serializable (stale-allowed) read still works on a minority node
```
**Questions:** How long did election take after killing the leader? What did clients see during the gap? Which reads worked without a quorum and why? How would you implement a lock with a fencing token using `create_revision`?

### Lab 3: Design a leader-election-based scheduler (paper)
Redesign TaskForge’s scheduler using etcd or ZooKeeper: lease TTL, renewal strategy, what the scheduler does when it loses the lease, how workers reject commands from a stale scheduler (fencing token in every command), what happens during a coordination-service outage (fail static: continue existing assignments, stop new ones?), and which metrics/alerts you add.

---

## 16.13 Interview perspective

- Say **why** you need consensus (single leader, linearizable config/locks, replicated log) and **what it costs** (majority round trip; unavailable without a majority).
- Explain **Raft in one minute:** terms, randomized election timeouts, up-to-date vote rule, AppendEntries with prev-log check, commit on majority + current term, apply in order.
- Know **sizing:** 3 or 5 nodes; 2f+1; commit latency = (f+1)-th fastest; even sizes pointless.
- Know **leader reads** need ReadIndex or leases; mention **fencing tokens** from the log index/term.
- Use consensus for **small metadata** (etcd/ZooKeeper), not bulk data; scale data with **many small consensus groups** (CockroachDB/Spanner/TiKV).
- Mention **testing**: Jepsen, TLA+, deterministic simulation; and that you would use a mature library rather than write your own.
- Avoid claims like “Raft prevents all split-brain”; say “Raft prevents two leaders from *committing* in the same term; stale leaders may exist briefly but cannot commit.”

---

## 16.14 Common misconceptions

1. **“Consensus means every node agrees at the same time.”** It means a majority decides and the rest catch up.
2. **“Raft guarantees there is never more than one leader.”** At most one leader per *term*; a deposed leader may briefly believe it leads but cannot commit.
3. **“More nodes = faster/more available.”** Beyond 5 you mostly add latency and traffic; even counts add nothing.
4. **“A leader can serve reads locally.”** Not safely without ReadIndex or a lease.
5. **“Consensus works over two data centers.”** With 2 sites you cannot survive losing the site with the majority; you need three failure domains.
6. **“The log index is enough to say an entry is committed.”** Committed = replicated on a majority *and* from the current term (or followed by one).
7. **“Paxos and Raft are different guarantees.”** Same safety; different structure.
8. **“fsync is an optimization detail.”** Skipping or faking it voids the proof.
9. **“ZooKeeper/etcd are fine as general databases.”** They are for small coordination data.
10. **“If tests pass for thousands of random runs, the protocol is correct.”** (BUG 2 in the lab was never found by 1,500 chaos runs.)
11. **“Timeouts fix FLP.”** They make progress likely; safety never depends on them.
12. **“A heartbeat from the leader means it is healthy.”** It only means it is alive in the term; it may be partitioned from a majority (CheckQuorum).

---

## 16.15 Chapter summary

- **Consensus** lets unreliable nodes agree on a value/sequence; safety (agreement, validity) always, liveness under partial synchrony. It underlies leader election, replicated state machines, locks, and metadata stores.
- **Majorities intersect** ⇒ safety; **n ≥ 2f + 1**; 3 or 5 nodes; even sizes are wasteful; losing a majority halts progress but never corrupts state.
- **Paxos:** prepare/promise then accept; proposals must adopt previously accepted values; **Multi-Paxos** with a stable leader costs one majority round trip per command.
- **Raft:** terms, randomized election timeouts (with none: **100% of runs never elected a leader**), vote only for up-to-date candidates, AppendEntries with prev-log consistency check, commit on majority **in the current term**, five safety properties; clients need session IDs/sequence numbers; linearizable reads via **ReadIndex or leases**; membership via joint consensus or one-server-at-a-time; snapshots for compaction; PreVote/CheckQuorum for stability.
- **Measured:** 3,000 chaos runs (~37k elections, 902k commits, 20.6k truncations) produced **0 safety violations**; deliberately broken variants were caught in **51%** of runs (two bugs) but the subtle Figure-8 bug **escaped 1,500 random runs**: tests give evidence, not proof; use model checking and directed tests.
- **Availability:** 5 nodes survive 2 crashes (200/200 commits) and stop at 3 (0 commits); an isolated leader accepted ~55 commands it could never commit; the majority elected a new leader in ~225 ms; no committed entry lost.
- **Use** consensus through etcd/ZooKeeper/Consul for small coordination data; **leases + fencing tokens** for leaders and locks; **many small groups** for scale; mind fsync, timeouts, session expiry, and quotas.

### Mental models
| Concept | Mental model |
|---|---|
| Consensus | A jury that must reach one verdict, even if some jurors leave the room |
| Majority intersection | Any two juries of 3 out of 5 share at least one juror who remembers the verdict |
| Term | A numbered reign; an old king’s orders are ignored once a later reign is known |
| Randomized timeouts | People standing up to speak at random moments so two don’t talk at once |
| Log replication | A court clerk’s ledger that every juror copies, line by line, in order |
| Commit rule | A line is final once most jurors have copied it in the current reign |
| ReadIndex | Before answering, the judge checks that the majority still recognizes them |
| Fencing token | The reign number stamped on every order so old orders bounce |

---

## 16.16 Exercises

### A. Conceptual
1. State the consensus problem with its safety and liveness properties. Which can FLP take away, and how do real systems cope?
2. Why does a majority quorum guarantee safety? Prove that two majorities of n nodes intersect.
3. A cluster has 4 nodes. How many failures can it tolerate? Is it better than 3 nodes? Than 5?
4. Explain what a replicated state machine is and why determinism is required.
5. Describe single-decree Paxos in your own words, including why a later proposer must adopt an earlier accepted value.
6. What does a stable leader change in Multi-Paxos? What is the steady-state message cost?
7. In Raft, what is a term? What happens when a server sees a higher term in any message?
8. Describe Raft’s vote-granting conditions and explain why the log up-to-date condition is needed.
9. Why are election timeouts randomized? What happens if they’re identical (cite the lab)?
10. Explain the AppendEntries consistency check and how a leader repairs a diverged follower.
11. State the Raft commit rule. Why is counting replicas not enough for entries from earlier terms? Walk through Figure 8.
12. What is the purpose of a leader no-op entry at the start of a term?
13. List Raft’s five safety properties and which mechanism ensures each.
14. Why can’t a Raft leader serve linearizable reads from its local state machine? Compare ReadIndex and leases.
15. How does a client avoid executing a command twice after a leader crash?
16. Explain why changing cluster membership by directly switching configurations can create two leaders. Describe joint consensus and single-server changes.
17. What are snapshots/InstallSnapshot for?
18. What are PreVote and CheckQuorum? What problems do they solve?
19. List four things you would store in etcd/ZooKeeper and four you would not.
20. Describe the ZooKeeper ephemeral-sequential leader election recipe and why followers watch only their predecessor.
21. How would you implement a lock with a fencing token using etcd?
22. What is multi-Raft and why do large databases use it?
23. How does the mutation test show that a checker can catch bugs? Why did BUG 2 survive?
24. What are the risks of running a consensus cluster across only two data centers?

### B. Calculations / Reasoning
25. 5-node Raft; RTTs from the leader: S2 1 ms, S3 3 ms, S4 45 ms, S5 90 ms; fsync 2 ms everywhere. Estimate commit latency. What if S2 and S3 are down? What does this imply about where the leader should sit?
26. 7-node cluster across three regions (3 + 2 + 2). Which region losses are survivable? What is the minimum number of regions needed to survive any single-region loss with majority quorum?
27. Compute the probability that the two earliest election timeouts among 5 servers fall within 20 ms of each other for windows of 100, 200, and 400 ms (use 1 − (1 − d/W)ⁿ). Compare trade-offs with failover delay.
28. Raft logs (term per index): S1 (leader, term 6): 1 1 1 4 4 5 5 6 6 6; S2: 1 1 1 4 4 5 5 6 6; S3: 1 1 1 4; S4: 1 1 1 4 4 5 5 6 6 6 6; S5: 1 1 1 2 2 2 3 3 3 3 3. (a) Which index is the highest committed one from S1’s point of view? (b) If S1 crashes, which servers could win an election? (c) What happens to S5’s entries?
29. A leader with 3 followers replicates 10,000 commands/s, each 200 bytes, to a 5-node cluster. How much outbound replication bandwidth does the leader need? What if batching is 20 commands per AppendEntries, and per-message overhead is 100 bytes?
30. fsync takes 5 ms; the leader batches commands during each fsync. Estimate the maximum committed throughput if each batch can hold up to 1,000 commands and replication adds 1 ms. What if fsync is 0.1 ms?
31. A lease-based read optimization uses election timeout 300 ms and lease duration 200 ms from the heartbeat send time; max clock drift 1%. Is it safe? How long can a partitioned old leader keep serving reads after losing contact?
32. Disaster recovery: a 3-node cluster loses 2 nodes permanently. The lone survivor has data up to index 9,500; the last committed index might have been 9,520. What data may be lost, and what are the operational steps and risks of forcing a new cluster?

### C. Design
33. Design a metadata service for a sharded database using a 5-node Raft group: what is stored, the read paths (linearizable and stale), caching in routers, and failure behavior.
34. Design leader election for a 20-instance job scheduler on etcd, including lease TTL, renewal, loss-of-lease behavior, and fencing.
35. Design a global configuration service with strong consistency in writes and low-latency reads in 3 regions.
36. Choose between a single 5-node cross-region Raft cluster and three regional clusters with async replication for a service with 100 ms RTT between regions and strict RPO=0 for 1% of its data.
37. Plan a safe rolling upgrade and a membership replacement (swap a failed node) for a 5-node etcd cluster.
38. Design a test plan for a custom Raft implementation: properties, fault injection, tools, and acceptance criteria.

### D. Debugging
39. Your etcd cluster logs “leader changed” every 30 seconds under load. List causes and checks.
40. After a network partition healed, a stale node still serves reads that are minutes old. What setting or code path caused it?
41. Two schedulers acted as leader for 20 seconds. The system uses a Redis `SET NX EX` lock. Explain and fix.
42. A Raft cluster is stuck: nodes keep timing out and no leader is elected, though all nodes are healthy and connected. List four causes.
43. Writes slow down dramatically when a follower is added in another region. Why? How do you fix it?
44. After restoring etcd from an old snapshot on one node, the cluster rejects it. Why?

### E. Interview-style
45. “Explain how Raft elects a leader and ensures a new leader has all committed entries.”
46. “How would you build a distributed lock service? What are the pitfalls?”
47. “Why do we use odd-sized consensus clusters?”

### F. Teach-back
48. Explain Raft to a friend using a classroom with a teacher who is replaced if absent, a shared notebook, and “most of the class must have written it down” as the rule for finality.

---

## 16.17 Full answer key

**1.** Agreement (no two nodes decide differently), validity (the decision was proposed), integrity (decide once) are safety; termination (all correct nodes decide) is liveness. FLP says deterministic asynchronous protocols can’t guarantee termination with a crash; real systems keep safety unconditional and obtain liveness through timeouts (partial synchrony) and randomization.

**2.** Two sets of size > n/2 must overlap: |A| + |B| > n implies |A ∩ B| ≥ 1. Therefore a value accepted by a majority is known to at least one member of any later majority, forcing later decisions to respect it.

**3.** Majority of 4 is 3, so it tolerates 1 failure: the same as 3 nodes but needs more hardware and a bigger quorum, so it’s strictly worse than 3 for availability per node and no better than 5 for tolerance (5 tolerates 2).

**4.** A state machine whose replicas apply the same commands in the same order from the same initial state to reach the same state. Determinism (no local randomness/time/IO side effects in apply) is required, otherwise replicas diverge.

**5.** The proposer picks a ballot and asks a majority to promise not to accept lower ballots, learning any value already accepted; it must propose the highest-ballot accepted value among replies (else its own); a majority accepting makes it chosen. Because any later majority intersects the accepting majority, later proposers learn and adopt the chosen value.

**6.** The leader runs phase 1 once for all future slots; each command then needs only the accept phase: one round trip to a majority.

**7.** A term is a numbered epoch with at most one leader. A server that sees a higher term updates its term and becomes a follower (clearing its vote); one that sees a lower term rejects the message.

**8.** Grant if candidate term ≥ own, not already voted for someone else in this term, and candidate’s log is at least as up-to-date (higher last term, or equal last term and ≥ index). The log condition ensures a new leader has all committed entries (Leader Completeness).

**9.** To break symmetry so one server usually times out first and wins before others start. With identical timeouts (lab: [150,151)), every node becomes a candidate at once, votes for itself, rejects the others: 100% of runs had no leader (330 elections each).

**10.** Each AppendEntries carries prevLogIndex/prevLogTerm; the follower rejects if it lacks a matching entry. The leader decrements nextIndex and retries until the logs match, then overwrites the follower’s conflicting entries and appends new ones.

**11.** An entry is committed when stored on a majority and from the leader’s current term (earlier entries are committed indirectly). Counting replicas of an old-term entry isn’t enough because a candidate with a different log can still win and overwrite it (Figure 8: index 2 term 2 on S1,S2,S3 gets overwritten by S5’s term-3 entry after S5 wins term 5). Committing by counting would then lose a “committed” entry.

**12.** A no-op in the new term replicates quickly and, once committed, indirectly commits all previous entries, giving the leader an up-to-date commit index and letting it serve reads.

**13.** Election Safety (one vote per term + majority); Leader Append-Only (leader code never deletes its entries); Log Matching (prev-log check); Leader Completeness (vote rule + majority intersection); State Machine Safety (follows from commit + Leader Completeness).

**14.** A deposed leader may not know it’s deposed and could return stale data. ReadIndex: leader records commit index, confirms leadership with a heartbeat majority, waits for apply, then reads; no log entry. Lease: after majority acknowledgment, the leader serves reads locally for a lease shorter than the election timeout; faster but depends on bounded clock drift.

**15.** Clients attach a unique client ID and monotonically increasing sequence number; the state machine caches the last response per client and returns it for duplicates, so retries are idempotent.

**16.** If servers switch at different times, there can be disjoint majorities of old and new configs, each electing a leader. Joint consensus requires majorities of both configs during the transition; single-server changes ensure any majority of the old and new configs intersect because they differ by at most one node.

**17.** Compacting the log by storing a snapshot of the state machine; InstallSnapshot lets a leader bring a far-behind follower up to date when the needed log entries have been discarded.

**18.** PreVote: a candidate checks it could win before incrementing its term, so partitioned/rejoining nodes don’t disrupt a healthy leader. CheckQuorum: a leader steps down if it can’t contact a majority, preventing stale-leader behavior.

**19.** Store: leader identity, shard map, feature flags, configuration, locks/leases, membership. Don’t store: user data, large blobs, high-write counters, event streams.

**20.** Each candidate creates an ephemeral sequential znode; the lowest number is leader; each other candidate watches the znode with the next lower number; when the predecessor disappears, the watcher checks whether it now has the lowest number. Avoids a herd of all candidates waking at once.

**21.** Create `/locks/x` with a lease using a transaction “if create_revision == 0 then put”; on success use the key’s create/mod revision as fencing token; keep the lease alive; the protected resource rejects tokens lower than the highest seen; delete the key or let the lease expire to release.

**22.** Many small Raft groups (per data range) with leaders spread across nodes: scales throughput and data size, localizes failures; transactions across groups need 2PC.

**23.** Mutations 1 and 3 produced violations in about half the runs, showing the checker detects “missing committed entry” and “two leaders in a term.” BUG 2 needs a rare multi-term interleaving, and no-op entries narrow the window; random runs didn’t hit it, showing the limits of random testing.

**24.** One data center holds the majority; if it fails, the cluster loses quorum even though the other site is healthy; if the link fails, the minority site is unavailable; cannot tolerate any single-site loss. Use three failure domains.

**25.** Majority of 5 = 3 (leader + 2 followers). The fastest followers are S2 (1 ms) and S3 (3 ms); commit waits for the *second*-fastest ack: 3 ms RTT + ~2 ms fsync ≈ **5 ms**. If S2 and S3 are down, the leader needs two of {S4 at 45 ms, S5 at 90 ms} → it must wait for the slower one: 90 ms + fsync ≈ **92 ms**. Implication: commit latency is set by the distance to the nearest *majority*, so place the leader (and a majority of voters) close together; far-away members should not be needed for the quorum in the normal case.

**26.** 3+2+2: majority = 4. Losing the 3-node region leaves 4 (survives); losing a 2-node region leaves 5 (survives). So any single region loss is survivable. Minimum regions: 3.

**27.** n=5, d=20: W=100 → 1 − 0.8⁵ = **67%**; W=200 → 1 − 0.9⁵ = **41%**; W=400 → 1 − 0.95⁵ = **23%**. Wider windows reduce split votes but increase failover delay (average first timeout shifts later).

**28.** (a) Replication counts: index 9: S1, S2, S4 (S4’s index 9 term 6) → 3 of 5; index 10: S1, S4 → 2. Index 9 is term 6 (current), so **commitIndex = 9**. (b) Candidates: S4 (most up-to-date) wins; S2 can win with votes from itself, S3, S5 (their logs are less up-to-date than S2’s); S3 and S5 cannot win (S2/S4 would reject them). (c) S5’s entries from index 4 onward are conflicting and will be overwritten by the new leader after nextIndex back-off to index 3.

**29.** Payload 10,000 × 200 B = 2 MB/s per follower × 4 followers = 8 MB/s (64 Mbit/s). With batching 20 and overhead 100 B: 500 messages/s per follower each carrying 4,000 B + 100 B = 4.1 KB → 2.05 MB/s per follower → 8.2 MB/s; overhead is small; without batching: 10,000 msgs/s × 300 B = 3 MB/s per follower.

**30.** One batch per (fsync + replication) cycle: 5 ms + 1 ms = 6 ms → 166 batches/s × 1,000 = **~166,000 commands/s** (if the leader’s CPU/network keep up); with fsync 0.1 ms: cycle 1.1 ms → 909 batches/s × 1,000 = ~909,000/s (practically bounded by CPU/network).

**31.** Lease 200 ms from heartbeat send time with election timeout 300 ms leaves 100 ms margin; with 1% drift, 200 ms of lease could stretch by 2 ms: safe. A partitioned old leader can keep serving reads until its lease expires (≤200 ms after the last majority-acknowledged heartbeat), while the majority can’t elect a new leader before 300 ms (minimum election timeout) → no overlap.

**32.** Entries 9,501–9,520 (potentially committed) may be missing from the survivor: committed data loss possible. Procedure: back up the survivor’s data dir, start a new single-node cluster from it with “force new cluster” (etcd `--force-new-cluster`), add new members one at a time as learners; risks: lost recent commits, stale state, clients that observed later state see regression; revoke leases/locks and re-sync dependent systems; communicate the incident.

**33 (sketch).** Store shard map, node membership, schema versions in a 5-node Raft group across 3 AZs; routers cache the shard map with version numbers and re-fetch on `StaleEpoch` errors; linearizable reads for changes (ReadIndex), stale reads for routine lookups; writes are rare; on quorum loss routers keep serving with last-known map (fail static) and reject topology changes.

**34 (sketch).** Key `/scheduler/leader` with a 10 s lease, keepalive every 3 s; compare-and-put on create; watchers retry on delete; leader attaches `lease revision` as a fencing token to every job dispatch; workers reject lower tokens; if keepalive fails twice, the leader stops dispatching immediately; jittered retry; alert on leader changes > N per hour.

**35 (sketch).** 5-node consensus cluster spanning 3 regions (2+2+1) for writes (accept ~RTT to nearest other region); local read-only learners/followers in each region for bounded-stale reads with version numbers; clients cache and watch; for linearizable reads, use ReadIndex via the leader.

**36 (sketch).** Single cross-region Raft: every commit pays ≥100 ms RTT, RPO=0 for all data; three regional clusters with async replication: low latency but RPO>0. Use the hybrid: regional clusters for most data; a small cross-region consensus store (or synchronous replication) only for the 1% needing RPO=0.

**37 (sketch).** Upgrade one node at a time (followers first, leader last with leadership transfer), check health and raft index between steps; replace a failed node by removing the dead member, adding a new member as a learner, waiting for catch-up, then promoting; never change more than one voting member at a time.

**38 (sketch).** Properties: election safety, log matching, leader completeness, state machine safety, linearizability of client ops, liveness under stable network. Tests: deterministic simulation with fault injection from seeds, Jepsen with nemeses (partitions, clock skew, pauses, crashes), TLA+ model checking of the spec, mutation testing, power-loss tests for storage, long soak with random membership changes. Acceptance: zero violations over millions of seeds and clean checker results.

**39.** Causes: slow fsync/disk, CPU starvation or GC pauses, network jitter or packet loss, election timeout too low relative to heartbeat jitter, noisy neighbors, large snapshots, overloaded leader with huge watch/write load. Check disk latency metrics (`wal_fsync_duration`), network RTT, heartbeat intervals, leader CPU, leader change logs.

**40.** The node served serializable/stale reads (non-linearizable read mode) or a cache; a stale leader without CheckQuorum/lease protection served reads. Fix: linearizable reads (ReadIndex), CheckQuorum, correct client consistency flag.

**41.** `SET NX EX` isn’t a fencing mechanism: the first holder may pause past the TTL (or Redis failover loses the key), allowing another holder; the old holder continues. Fix: linearizable lock service with fencing tokens validated by the resource, and a leader that stops when it can’t renew.

**42.** Identical or too-narrow timeouts; an election timeout shorter than the round-trip/fsync time so candidates always time out before votes return; persistent split votes due to even cluster size with symmetric partitions; votes rejected because logs differ unexpectedly or nodes have inconsistent configuration (cluster ID/peer lists); clocks irrelevant but timer starvation from GC.

**43.** Quorum latency now includes the far follower if it’s among the fastest majority (or if the cluster is small); with 3 nodes adding a remote 4th changes quorum to 3, requiring a remote ack. Fix: keep voting members local, add the remote node as a non-voting learner, or use a larger odd cluster with local majority.

**44.** The restored node’s raft state (term/log) is older and conflicts with the cluster’s cluster ID/membership/commit history; it cannot rejoin as an old identity. Fix: remove the member and add a fresh member to sync from the leader (or restore the entire cluster from the snapshot with a new cluster ID).

**45 (sketch).** Election: timeout → candidate, term+1, vote self, RequestVote with last log index/term; voters grant if they haven’t voted this term and candidate’s log is at least as up-to-date; majority wins. Any committed entry is on a majority, so at least one voter in any majority has it, and it would reject a candidate lacking it, so the winner has all committed entries.

**46 (sketch).** Use a linearizable store with leases and fencing tokens; handle expiry and pauses; don’t depend on timing; resource checks tokens; avoid locks if idempotent operations suffice; monitor lock holders; handle quorum loss (fail closed or open per risk).

**47 (sketch).** Odd sizes give the same fault tolerance as the next even size with fewer nodes (and less replication cost); 2f+1 is minimal for f failures; even sizes can also create symmetrical partitions with no majority.

**48.** A classroom where the teacher (leader) dictates; if the teacher is absent, students vote for a new one (the one with the most complete notes, so nothing is forgotten). A line in the notebook counts as final once most of the class has written it down during the current teacher’s reign. A teacher locked out of the room may keep talking, but no one counts it as final.

---

## 16.18 Flashcards

| Front | Back |
|---|---|
| Consensus properties | Agreement, validity, integrity (safety); termination (liveness) |
| Why majorities | Any two intersect ⇒ safety; n ≥ 2f+1 |
| Odd sizes | 3 tolerates 1, 5 tolerates 2; 4 and 6 add nothing |
| Paxos phases | Prepare/promise, accept/accepted; adopt highest accepted value |
| Multi-Paxos | Stable leader, 1 RTT per command |
| Raft roles | Follower, candidate, leader |
| Term | Epoch with ≤ 1 leader; higher term wins |
| Vote rule | One vote/term; candidate log at least as up-to-date |
| Randomized timeouts | Break symmetry (identical → 100% no leader in lab) |
| AppendEntries check | prevLogIndex/prevLogTerm; leader backs off nextIndex |
| Commit rule | Majority replicated + current term |
| Figure 8 | Old-term entries can be overwritten; hence current-term rule |
| No-op on election | Commits previous-term entries indirectly |
| Linearizable read | ReadIndex or lease; not plain leader read |
| Client dedup | Session ID + sequence number |
| Membership change | Joint consensus or one server at a time |
| Snapshot | Compacts log; InstallSnapshot for laggards |
| PreVote / CheckQuorum | Avoid disruptive terms / stale leaders |
| Commit latency | Leader fsync ∥ (f+1)-th fastest follower RTT + fsync |
| Chaos result | 3,000 runs: 0 violations; buggy variants caught ~51%; Figure-8 bug missed |
| Coordination data | Small metadata, leases, locks; not bulk data |
| Fencing token | Revision/term attached to writes |
| Multi-Raft | One group per data range |

## 16.19 Glossary additions

| Term | Simple Meaning | Technical Meaning | Why It Exists | Example |
|---|---|---|---|---|
| Consensus | Agree on one decision | Agreement + validity + termination among faulty nodes | Foundation for strong consistency | Raft |
| Replicated state machine | Same commands, same order | Deterministic machine fed by a consensus log | Fault-tolerant services | etcd |
| Quorum | Enough votes | Majority subset | Safety by intersection | 3 of 5 |
| Paxos | Classic consensus protocol | Ballot-based two-phase agreement | Theory/practice | Chubby |
| Ballot / term | Round number | Monotonic epoch identifying a leadership attempt | Detect staleness | Term 7 |
| Leader | Single decider | Node sequencing commands in a term | Efficiency | Raft leader |
| Election timeout | Wait before running | Randomized follower timer | Failure detection | 150–300 ms |
| AppendEntries | Log copy message | Raft replication/heartbeat RPC | Replication | RPC |
| Commit index | Final line | Highest log index known committed | Applying safely | 9 |
| No-op entry | Empty command | Entry appended on election | Commit older entries | Leader start |
| ReadIndex | Safe read | Confirm leadership via quorum before reading | Linearizable reads | etcd |
| Leader lease | Time-bound authority | Period where no other leader can exist | Fast reads | 200 ms |
| Joint consensus | Two-config overlap | Transitional configuration needing both majorities | Safe membership change | C(old,new) |
| Snapshot | State checkpoint | Compact state replacing log prefix | Bounded log | InstallSnapshot |
| Learner | Non-voting member | Replica catching up without voting | Safe adds | etcd learner |
| PreVote | Trial election | Check electability before term bump | Reduce disruption | Raft extension |
| CheckQuorum | Leader self-check | Step down without majority contact | Avoid stale leaders | Raft extension |
| Znode / ephemeral node | ZK node / session-bound node | ZooKeeper data nodes | Coordination | `/election/n_00001` |
| Lease (etcd) | Expiring key attachment | TTL with keepalive | Liveness-bound ownership | 10 s |
| Multi-Raft | Many groups | One consensus group per range | Scale | CockroachDB |
| Model checking (TLA+) | Exhaustive protocol checking | Explore all states of a small model | Find rare bugs | Raft spec |
| Mutation testing | Break it on purpose | Inject bugs to validate tests | Test the tests | BUG 1–3 |

---

**Next: Chapter 17: Distributed Transactions, Sagas, Event Sourcing, and CQRS.** Atomic commit with two-phase commit (and its blocking problem, measured), sagas and compensation (measured against naive designs), the transactional outbox revisited, event sourcing with snapshots and optimistic concurrency (measured), CQRS projections, and how NewSQL systems combine consensus and 2PC.

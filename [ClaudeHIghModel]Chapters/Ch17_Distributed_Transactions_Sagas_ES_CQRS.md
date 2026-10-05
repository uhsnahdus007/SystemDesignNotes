# Chapter 17: Distributed Transactions, Sagas, Event Sourcing, and CQRS

## Chapter card

| | |
|---|---|
| **Objective** | Learn how systems keep multi-step, multi-service, multi-database operations consistent: atomic commit and its blocking problem (two-phase commit, measured), how consensus-replicated systems (Spanner, CockroachDB) fix it, distributed isolation, sagas and compensation (measured against naive designs), the TCC pattern, event sourcing (snapshots and optimistic concurrency, measured), CQRS, and how to choose among them |
| **Why this chapter exists** | The moment data lives in more than one database or service, the comfortable ACID transaction of Chapter 3 disappears. Most real outages in business systems (double charges, lost orders, stuck inventory) are failed multi-step operations. This chapter gives you the patterns and the vocabulary to design them correctly and to explain trade-offs in interviews |
| **Prerequisites** | Chapters 3 (ACID, isolation), 10 (sharding, cross-shard transactions), 12 (idempotency, outbox), 14 (partial failure), 15 (consistency), 16 (consensus) |
| **Study time** | 13–17 hours including labs |
| **Tier** | `[MK]` sagas, outbox, idempotency, choosing an approach; `[GK]` 2PC internals, event sourcing, CQRS; `[ADV]` NewSQL transaction protocols |
| **Mastery criteria** | (1) Explain why atomic commit across nodes is hard. (2) Walk through 2PC, its failure cases, and the blocking problem. (3) Explain how consensus-replicated participants and coordinators remove blocking. (4) Design a saga with compensations, pivot step, idempotent steps, and a durable saga log; compare orchestration and choreography. (5) Describe saga isolation anomalies and countermeasures. (6) Explain event sourcing: events, aggregates, expected-version concurrency, snapshots, projections, upcasting. (7) Explain CQRS, its consistency implications, and when it is overkill. (8) Choose among local transaction, 2PC/XA, NewSQL, saga/TCC, and reconciliation for a scenario |

---

## 17.1 The Story: The Order That Was Half Placed

**ShipIt** is an e-commerce platform split into services, each with its own database: **Orders**, **Payments**, **Inventory**, **Shipping**. Placing an order runs four steps:

```text
1. Orders:     create order #8841 (status PENDING)
2. Inventory:  reserve 2 × SKU-77
3. Payments:   charge $120 to card
4. Shipping:   create shipment
5. Orders:     mark order CONFIRMED
```
Real incidents in one month:
- **Charged, no order.** Step 3 succeeded; the process crashed before step 5. The customer has a charge and an order stuck in PENDING.
- **Ghost stock.** Inventory was reserved at step 2; step 3 failed (card declined) and nobody released the reservation. 1,300 units of a popular SKU became “unavailable” over a week.
- **Double charge.** Step 3 timed out; the retry charged again (Chapter 14: timeout ambiguity).
- **The two-phase commit experiment.** An architect proposes XA/2PC across all four databases. In staging it works. In production a coordinator host reboots between “prepare” and “commit”; four databases hold row locks on hot SKUs for 40 minutes; checkout stalls site-wide.
- **The auditor’s question.** “Show me every change to order #8841 and what the balance looked like on March 3.” Nobody can, because state was overwritten in place.
- **The dashboard.** The reporting team’s heavy queries slow checkout, so they read replicas, which are minutes stale.

### Pause & Predict 17.1
> For each incident, name the underlying problem (atomicity, isolation, idempotency, blocking, history, read/write load conflict) and sketch one remedy. Which of these could be avoided by *not splitting* the data across services in the first place?

---

## 17.2 Why distributed transactions are hard `[MK]`

### 17.2.1 What we want: ACID across boundaries
- **Atomicity:** either all steps take effect or none.
- **Consistency:** invariants hold (no negative stock, payments match orders).
- **Isolation:** concurrent operations don’t see each other’s intermediate states.
- **Durability:** committed effects survive crashes.
On one database all four are provided by the engine (Ch 3). Across databases or services, each local commit is independent; failures between commits leave partial results.

### 17.2.2 Why atomic commit is fundamentally hard
Participants A and B must both commit or both abort. A coordinator can ask each “can you commit?” and then tell them to commit, but:
- If the coordinator crashes after telling A to commit but before telling B, B doesn’t know the outcome.
- Messages can be lost (Two Generals, Ch 14), so there is always a window of uncertainty.
- A participant that has promised to commit must keep its locks until it learns the outcome (it cannot unilaterally abort).
Thus **atomic commit trades availability for atomicity**: some failure patterns block progress (and hold locks) until recovery.

### 17.2.3 First, try not to need one `[MK]`
The cheapest distributed transaction is none. Techniques (Ch 10):
1. **Keep related data in one database/shard** (shard by `customer_id`, `tenant_id`; put order lines and the order in the same shard).
2. **Draw service boundaries around transactional units** (an aggregate/bounded context owns its invariants; don’t split Orders from Order Lines).
3. **Merge services** that constantly need atomic updates (a “distributed monolith” is worse than a monolith).
4. **Redefine the requirement**: do you need atomicity, or is “eventually consistent with compensation” acceptable (Ch 15)?
5. **Use idempotent messaging + outbox** to propagate effects reliably (Ch 12).
Only when these fail do you reach for the tools below.

---

## 17.3 Two-phase commit (2PC) `[GK]`

### 17.3.1 The protocol
One **coordinator** (transaction manager) and *n* **participants** (resource managers: databases).
```text
 Phase 1: PREPARE (voting)
   coordinator ── PREPARE ──▶ every participant
   each participant: does the work, takes locks, writes a durable PREPARED record to its log (force-write),
                     and replies VOTE-COMMIT (promising it CAN commit, and will not unilaterally abort),
                     or VOTE-ABORT (and aborts locally).
 Phase 2: COMMIT / ABORT (decision)
   if ALL voted commit:  coordinator force-writes COMMIT to its log (THE commit point), sends COMMIT to all
   else:                  coordinator writes ABORT, sends ABORT to all
   participants apply the decision, release locks, ack.
```
**Properties:** atomic outcome (all commit or all abort); the **commit point** is the coordinator’s durable decision record. Cost: **two network round trips** (prepare, commit) and **two forced log writes** per participant (prepared, commit) plus one at the coordinator. Intra-datacenter: roughly 2 RTT (~1–2 ms) + 3 fsyncs ≈ 5–10 ms; cross-region: ≥ 2 WAN RTTs.

### 17.3.2 Failure cases `[GK]`
| Failure | Outcome | Notes |
|---|---|---|
| Participant crashes **before** voting | Coordinator times out → ABORT | Safe |
| Participant crashes **after** voting commit (prepared) | On restart it finds PREPARED in its log, asks the coordinator for the outcome, keeps locks until told | Safe; locks held across the crash |
| Coordinator crashes **before** deciding | Participants that haven’t voted can abort; those already prepared **must wait** | In doubt |
| Coordinator crashes **after** deciding, before telling everyone | Recovery: re-read its log, re-send decision | Participants prepared and waiting are **blocked** until it returns |
| Coordinator **and** a participant that knows the decision both crash | Other prepared participants can’t learn the outcome | Blocked; can’t unilaterally decide (heuristic commit/abort is dangerous) |
| Message loss | Retransmit; coordinator timeouts | Participants must be idempotent about repeated COMMIT |

### 17.3.3 The blocking problem, measured (Lab 1, part 1)
Model: 200 distributed transactions/s for 10 minutes; each locks one of 100 hot rows on each of two shards; normal lock hold is 10 ms. With probability *p* = 0.1% the coordinator dies after all participants prepared, so both rows stay locked (“in doubt”) until a coordinator recovers; any transaction needing those rows waits.
```text
scenario                                            txns that waited >1 s     p50 / p99 / p99.9 latency (ms)     worst (s)
no coordinator failures                                   0                        10 /      10 /       10                    0.0
p=0.1%, coordinator back in ~30 s (single node)       12950                        10 /   75520 /   109945                  138.1
p=0.1%, coordinator replicated: new leader in ~1 s      201                        10 /      10 /     1549                    4.6
```
**Reading it.** A coordinator that fails just **one transaction in a thousand** and takes ~30 s to recover leaves **12,950 transactions stuck for more than a second** (worst case 138 s; the p99 is 75 seconds!), because each stuck transaction locks two rows on hot tables and every later transaction touching them queues behind it. The median is unaffected (10 ms) — an average hides the damage. When the coordinator is replicated (consensus: a new leader takes over in ~1 s), only 201 transactions are delayed and the p99 stays at 10 ms. **Blocking is the cost of an unreplicated coordinator**, which is why classic XA across independent databases is rarely used on hot paths at scale.

### 17.3.4 Three-phase commit and why it isn’t used
3PC adds a “pre-commit” phase so participants can decide on their own if the coordinator fails, making it non-blocking **under a synchronous network with bounded delays and crash failures only**. Under real (partially synchronous, partitionable) networks it can violate atomicity, so it is a theoretical result rather than a practice. The practical non-blocking solution is **consensus-based atomic commit** (below).

### 17.3.5 Making commit non-blocking with consensus `[ADV]`
Atomic commit can be reduced to consensus: “commit or abort for transaction T” is a single decision that must be durably agreed on despite failures. **Paxos Commit** (Gray & Lamport) replaces the single coordinator’s decision record with a Paxos instance among 2f+1 acceptors, so the decision survives f failures and any node can finish the protocol. In practice:
- **Spanner:** each shard is a **Paxos group** (so each “participant” is itself fault tolerant and never really disappears); one participant group’s leader acts as the 2PC coordinator, and its coordinator state is also replicated, so a failed leader is replaced within seconds and the transaction proceeds. Commit timestamps from **TrueTime** with commit wait provide external consistency (Ch 14).
- **CockroachDB / TiDB / YugabyteDB:** transaction status records live in Raft-replicated ranges; the commit decision is a single replicated write; “parallel commits” overlap prepare and commit to cut a round trip; other transactions that encounter an in-doubt intent can *resolve* it by consulting the replicated transaction record (no waiting for a specific coordinator process).
- **Result:** 2PC semantics without the classic blocking problem, but with the cost of consensus on every participant plus extra round trips (cross-region commits cost 1–2 WAN RTTs).

### 17.3.6 XA and standards
**XA** (X/Open) standardizes 2PC between a transaction manager (e.g., Java EE JTA, Atomikos, Narayana) and resource managers (databases, message brokers). Postgres exposes it as `PREPARE TRANSACTION 'id'` / `COMMIT PREPARED 'id'`; MySQL as `XA START/END/PREPARE/COMMIT`. Caveats: prepared transactions hold locks and must be resolved (orphaned prepared transactions block vacuum/DDL in Postgres), heuristic completion is dangerous, throughput is poor under contention, not supported by most NoSQL stores and SaaS APIs (payment providers do not participate in your 2PC!).

---

## 17.4 Isolation across nodes `[GK/ADV]`

Atomic commit gives all-or-nothing; **isolation** controls what concurrent transactions see.
- **Two-phase locking (2PL)** across nodes: locks held until commit (strict 2PL); with 2PC, locks span the whole protocol; deadlocks across nodes need detection (wait-for graphs) or timeouts/wound-wait/wait-die.
- **MVCC with global timestamps:** each transaction reads a consistent snapshot at timestamp *T* and writes versions at commit timestamp. Timestamps come from a **TrueTime-like** bounded-uncertainty clock (Spanner), **hybrid logical clocks** (CockroachDB, YugabyteDB), or a **timestamp oracle** (Percolator, TiDB’s PD) `[Public]`.
- **Percolator** (Google, built on Bigtable): client-driven 2PC with a *primary lock* per transaction; the commit of the primary row is the commit point; secondaries are resolved lazily by anyone who encounters their locks. It brought snapshot-isolation transactions to a NoSQL store.
- **Calvin / deterministic databases:** agree on a global transaction order *before* execution (via a sequencer replicated by consensus), then every partition executes deterministically; no 2PC needed, at the cost of requiring read/write sets to be known up front.
- **OCC (optimistic concurrency control):** execute without locks, validate at commit; fails under contention.
- **Anomalies:** snapshot isolation allows **write skew** (Ch 15); serializable snapshot isolation (SSI) detects dangerous structures; strict serializability adds real-time order.
**Practical guidance:** if you need serializable multi-row, multi-node transactions at scale, use a NewSQL system built for it (Spanner, CockroachDB, YugabyteDB, TiDB, FoundationDB) rather than assembling your own.

---

## 17.5 Sagas `[MK]`

When a business process spans services and you cannot (or should not) hold a distributed lock, use a **saga** (Garcia-Molina & Salem, 1987): a sequence of **local transactions** T1…Tn, each committed independently, with a **compensating transaction** Ci for each Ti that semantically undoes it. If step k fails, run Ck−1, …, C1 in reverse order.

### 17.5.1 Example: ShipIt’s order saga
| Step | Local transaction | Compensation | Notes |
|---|---|---|---|
| T1 | Orders: create order `PENDING` | Mark order `CANCELLED` | Compensatable |
| T2 | Inventory: reserve stock (with expiry) | Release reservation | Compensatable |
| T3 | Payments: **authorize** card (hold) | Void authorization | Compensatable (prefer authorize/capture over charge/refund) |
| T4 | **Pivot:** Payments: capture (money moves) | Refund (costly, partial semantics) | The point of no easy return |
| T5 | Shipping: create shipment | Cancel shipment (if not dispatched) | Retriable; must eventually succeed |
| T6 | Orders: mark `CONFIRMED` | — | Retriable |
Terminology (Richardson): **compensatable transactions** (before the pivot), the **pivot transaction** (the go/no-go step that, once it succeeds, the saga will run to completion), and **retriable transactions** (after the pivot; must be made to succeed by retrying; they are idempotent). Order steps so that the riskiest/most likely-to-fail ones run before the pivot.

### 17.5.2 Compensation is not rollback
A rollback undoes uncommitted changes invisibly. A compensation is a **new forward action** after the original has committed and possibly been **observed** by others: a refund is not “the charge never happened”; customers saw the charge on their statement; an email “your order shipped” cannot be unsent. Compensation rules:
- Must be **idempotent** and **retriable until it succeeds** (it cannot be allowed to fail permanently).
- Must tolerate being invoked **when the original didn’t happen or half-happened** (timeout ambiguity): “cancel reservation 77 if it exists.”
- Should be **commutative with late arrivals**: a compensation may arrive before the delayed original request; the participant must record the cancellation (tombstone) so the late original is rejected.
- Not everything is compensatable (sending an email, physical shipment, regulated actions); make those the pivot or later, or add delay (“hold the email until the saga completes”).

### 17.5.3 Orchestration vs choreography `[MK]`
| | Orchestration | Choreography |
|---|---|---|
| Control | A central **orchestrator** (saga coordinator) tells each participant what to do and tracks state | Each service reacts to **events** and emits the next event; no central controller |
| Visibility | Whole flow in one place; easy to monitor, test, change | Flow is implicit across services; hard to see and debug (“event spaghetti”) |
| Coupling | Participants coupled to the orchestrator’s commands | Participants coupled to event schemas and each other’s behavior |
| Failure handling | Orchestrator drives compensation | Each service must know when to compensate; cyclic dependencies possible |
| Good for | Complex flows with branches, timeouts, compensations (most business processes) | Simple, linear, few-step flows; highly autonomous services |
| Tools | Temporal, Cadence, AWS Step Functions, Camunda, Netflix Conductor, Axon Sagas | Kafka/SNS events with consumers |
Orchestrators persist saga state (a **saga log**: current step, data, history) in a database or workflow engine, so a crashed orchestrator resumes where it left off.

### 17.5.4 Measured: what makes a saga actually safe? (Lab 1, part 2)
200,000 orders through reserve → charge → ship, with business failures (2% / 5% / 3%), 2% “executed but reply lost” and 1% “request lost” per call, and an orchestrator that crashes after 1% of steps. Up to 4 attempts per call.
```text
design                                                 consistent   charged-no-goods   double-charged   stock leaked   other
A: no compensation, blind retries, no saga log          85.05%        7384               3787           11764        6962
B: compensation + blind retries, no saga log            92.22%        2091               3706            2321        7452
C: compensation + idempotent steps + durable saga log  100.00%           0                  0               0           0
```
**Interpretation.**
- **A (naive):** ~15% of orders end inconsistent: customers charged with no goods (7,384), **double-charged (3,787)** from blind retries, stock reserved forever (11,764), and abandoned half-finished sagas.
- **B (add compensation, still blind retries and no log):** consistency improves (92.2%) but **double charges remain (3,706)** because retries are not idempotent, and orders abandoned when the orchestrator crashed (no durable state) still leave debris (“other” 7,452).
- **C (idempotent steps + durable saga log + compensation of unknown-outcome steps):** **0 inconsistent orders in 200,000.** All three ingredients are needed: *idempotency* removes duplicate effects from retries; the *saga log* lets a restarted orchestrator resume or compensate; *compensation* (including for the step whose outcome is unknown) restores the invariant. 
**Caveats:** the simulation assumes participants are otherwise correct (no bugs), compensations are always eventually deliverable, and no human-visible intermediate state matters. In production, “stuck sagas” still occur (a compensation permanently rejected, a service gone) and need alerts, dead-letter handling, and operator tooling.

### 17.5.5 Saga isolation problems and countermeasures `[GK]`
Sagas give atomicity (eventually) and durability, but **not isolation**: other transactions can see intermediate states (reserved stock, pending order) and act on them.
| Anomaly | Example | Countermeasure |
|---|---|---|
| **Lost update** | Saga overwrites a value changed by another transaction between its steps | Version checks / optimistic locking; commutative updates |
| **Dirty read** | Another process reads a reservation that will be compensated | Show states as “pending”; semantic locks |
| **Non-repeatable read** | A saga reads a price in step 1 and sees a different price in step 3 | Re-read, or carry the value (“by value”) |
Countermeasures (Richardson): **semantic lock** (an application-level flag such as `status = PENDING` that other operations must respect or wait on); **commutative updates** (increment/decrement rather than set); **pessimistic view** (reorder steps to minimize dirty-read risk); **reread value** (verify before overwriting); **version file** (record operation order to resolve out-of-order arrivals); **by value** (choose concurrency mechanism by business risk).

### 17.5.6 Design checklist for a saga
1. Identify the invariant and the pivot; order steps (risky first).
2. For each step: local transaction, compensation, idempotency key, timeout, retry policy, and ownership.
3. Durable **saga state machine** (including `UNKNOWN`/`COMPENSATING`/`STUCK` states) and a unique saga ID propagated in every message and log line.
4. Every message handler **idempotent** (inbox/dedup table, Ch 12); every outgoing command via the **outbox**.
5. Handle **late/duplicate/out-of-order** messages (tombstones, versions).
6. Timeouts and deadlines for the whole saga; escalation (alerts, manual review queue).
7. User-visible states (“Order received”, “Payment pending”), and what happens on partial failure.
8. Reconciliation job comparing systems of record (orders vs payments vs shipments) to catch what everything else missed.

### 17.5.7 TCC: Try–Confirm–Cancel `[GK]`
A variant for resource reservation: **Try** reserves resources tentatively (holds funds, reserves a seat, with an expiry), **Confirm** makes it permanent (idempotent), **Cancel** releases it (idempotent). Compared with sagas of already-committed steps, TCC makes intermediate states explicit and *reserved*, so others see “held” rather than “spent.” Participants must implement all three operations and handle expiry. Common in payments and travel booking.

---

## 17.6 The transactional outbox and reliable messaging, again `[MK]`

Every saga/choreography step needs “update my database **and** reliably tell others.” Revisit Chapter 12’s outbox: write the state change and an outbox row in one local transaction; a relay (polling or CDC) publishes to the broker with at-least-once delivery; consumers are idempotent. For inbound commands use the **inbox** table (message ID unique constraint) in the same transaction as the effect. Together they provide **exactly-once effects** between services without distributed transactions. For request/response with third parties (payment providers) use their **idempotency keys** and a state machine with `UNKNOWN` outcomes reconciled by querying (Ch 14).

---

## 17.7 Event sourcing `[GK]`

### 17.7.1 The idea
Instead of storing the **current state** of an entity and overwriting it, store the **sequence of events** that happened to it (`OrderPlaced`, `ItemAdded`, `PaymentAuthorized`, `OrderShipped`). Current state is **derived** by folding (replaying) the events. The event log is the source of truth; any state, view, or table is a cache of it.
```text
 Event stream for account A-17 (append-only, ordered by version):
   v1  AccountOpened{ owner: Maya }
   v2  Deposited{ amount: 1000 }
   v3  Withdrawn{ amount: 250 }
   v4  Deposited{ amount: 40 }           state = fold(events) → balance 790
```
Related terms: **aggregate** (consistency boundary whose invariants are enforced when deciding new events), **stream** (events of one aggregate), **command** (request that may be accepted → produces events, or rejected), **event** (a fact that happened, named in the past tense, immutable).

### 17.7.2 The write path: commands, decisions, and optimistic concurrency
```text
 handle(command):
   events = store.load(streamId)                     // or snapshot + tail
   state  = fold(events)                             // current state, version = n
   newEvents = decide(state, command)                // enforce invariants; may reject
   store.append(streamId, expectedVersion = n, newEvents)    // atomic; fails if someone else appended since
   on version conflict → reload and retry the decision
```
The `expectedVersion` check is **optimistic concurrency control**: it prevents two commands from deciding against the same stale state.

**Measured (Lab 1, part 3b).** 8 threads fire 4,000 commands each (withdraw 50 or deposit 40) at one account that starts with 1,000; the business rule is “balance must never go negative”:
```text
no version check (blind append)    events=29,027  rule violated (balance went negative): true   lowest balance ever: -350   retries after conflict: 0   rejected (insufficient funds): 2,974
optimistic concurrency (expectedVersion) events=29,029  rule violated (balance went negative): false  lowest balance ever: 0   retries after conflict: 196,764   rejected (insufficient funds): 2,972
```
Without the version check, concurrent decisions on stale balances overdraw the account (lowest balance **−350**). With `expectedVersion`, the rule held (lowest balance 0), but at a price: **~197,000 retries for ~32,000 commands (about 6 retries each)** because eight writers hammer one hot aggregate. Two lessons: (1) concurrency control is mandatory; (2) a **hot aggregate** is a bottleneck — keep aggregates small and rarely contended, or serialize commands per aggregate (an actor/mailbox per aggregate, or partitioned command queues) instead of retry-looping. (Exact counts vary by run and by hardware; the pattern doesn’t.)

### 17.7.3 Snapshots
Replaying a long history is slow. A **snapshot** stores the folded state at version *v*; loading = snapshot + events after *v*.

**Measured (Lab 1, part 3a):** 5,000,000 in-memory events:
```text
replay all 5,000,000 events: 46.3 ms   |   load snapshot (taken every 100,000 events) + replay the tail: 0.003 ms   |  same balance: true
```
Even in memory, replay costs milliseconds per million events; against a real event store (disk/network, deserialization, upcasting) it becomes seconds or minutes, whereas snapshot + tail is bounded by the snapshot interval. Policy: snapshot every N events (e.g., 100–1,000) or on a schedule; keep events (snapshots are optimizations, not truth); make snapshots versioned with the code that interprets them.

### 17.7.4 Projections and read models
Consumers subscribe to the event stream and build **projections**: a denormalized “orders by customer” table, a search index, a cache, analytics aggregates. Projections are disposable and **rebuildable by replaying** all events — a major advantage (new reports from old data). They are **eventually consistent** with the event store (lag), and must be idempotent (at-least-once delivery) and tolerate ordering rules (per-stream order only).

### 17.7.5 Event schema evolution `[GK]`
Events are permanent; the code that reads them changes. Strategies:
- **Additive changes only** (new optional fields with defaults) where possible.
- **Upcasting:** transform old event versions to the current shape at read time (`OrderPlaced_v1 → v2`), keeping stored data untouched.
- **Versioned event types** (`OrderPlacedV2`) and handlers for each.
- **Copy-and-transform migration** into a new stream/store for big changes (expensive, replays).
- **Weak schemas** (JSON with tolerant readers) vs **strict schemas** (Avro/Protobuf with a registry; Ch 13).
Avoid events that expose internal database structure; design them as **business facts** (`ShippingAddressChanged`, not `UserTableRowUpdated`).

### 17.7.6 Deleting data (GDPR) and privacy
An immutable log conflicts with the “right to erasure.” Techniques: keep personal data **out of events** (store references to a separate deletable store); **crypto-shredding** (encrypt personal fields per subject with a key stored separately; deleting the key renders the data unreadable); redacting/rewriting streams as a last resort.

### 17.7.7 Benefits, costs, and when to use `[MK]`
**Benefits:** complete **audit trail** and temporal queries (“state on March 3”), replay for debugging and new read models, natural fit with event-driven integration (the stream is the integration), and decoupling of write model from read models, rich domain modeling for complex business rules.
**Costs:** significant complexity (learning curve, tooling), eventual consistency of read models, event-schema evolution, difficulty with ad-hoc queries on current state without projections, snapshots/replay performance, harder “simple CRUD”, operational concerns (event store scaling, storage growth), and GDPR handling.
**Use it when:** audit/history is a core requirement (finance, healthcare, compliance), the domain is rich in business events, you need multiple evolving read models, or temporal analysis matters. **Avoid** for plain CRUD, small teams without the discipline, or when only an audit log is wanted (use an audit table or CDC instead).
**Event sourcing ≠ event-driven architecture ≠ using Kafka.** Kafka can carry events but is not a good per-aggregate event store (no efficient load-by-aggregate-id with optimistic concurrency, topic/partition counts); purpose-built stores (EventStoreDB, Axon Server, Marten on Postgres, DynamoDB/relational tables with `(stream_id, version)` uniqueness) fit better.

---

## 17.8 CQRS: Command Query Responsibility Segregation `[GK]`

### 17.8.1 The idea
Use **different models for writing and for reading**.
```text
          commands                                 queries
 Client ───────────▶ [ Command side ] ──events/changes──▶ [ Read model(s) ] ◀────────── Client
                       validates, enforces invariants,        denormalized for the query:
                       writes the source of truth             SQL table, search index, cache, graph…
```
- The **write model** is optimized for correctness (aggregates, invariants, transactions).
- The **read model(s)** are optimized for queries (denormalized, indexed, precomputed), updated asynchronously from the write side via events/CDC.
CQRS is independent of event sourcing: you can have CQRS with a plain relational write model and CDC-fed read models; event sourcing just makes the event stream the natural feed.

### 17.8.2 Why
- **Different load shapes:** 100:1 reads vs writes need different scaling (Ch 6/9).
- **Different data shapes:** the write model is normalized for consistency; reads want joined, pre-aggregated documents.
- **Multiple read technologies:** relational + search (Elasticsearch) + cache + analytics warehouse from one source.
- **Isolation of reporting load** from transactional load (Story: dashboard slowing checkout).

### 17.8.3 Consequences you must design for
- **Eventual consistency:** after a command succeeds, the read model lags. Users see stale data (“I placed an order and it’s not in my list”). Remedies from Ch 9/15: return the new state in the command response, **read-your-writes tokens** (read model must have processed version ≥ token), optimistic UI, polling/websocket updates, and routing the user’s next read to a fresher path.
- **Projection failures and rebuilds:** idempotent handlers, poison-event handling, ability to rebuild from scratch, versioned projections (blue/green).
- **Duplicated logic and drift:** keep business rules on the write side; read models are dumb views.
- **Operational complexity:** more moving parts; monitor projection lag (age of last processed event vs head).
- **Over-engineering risk:** many applications do not need CQRS; a single model with a read replica and a few indexes is simpler (Ch 9).

---

## 17.9 NewSQL and distributed SQL: consensus + 2PC + MVCC `[ADV]`

Systems like **Spanner**, **CockroachDB**, **YugabyteDB**, **TiDB**, and **FoundationDB** package the pieces of this chapter and Chapters 10 and 16:
1. **Sharding** into ranges, each a **consensus group** (Ch 10, 16).
2. **MVCC** with global timestamps (TrueTime or HLC) for snapshot reads and serializable isolation.
3. **Distributed transactions** via a commit protocol over consensus-replicated transaction records (no blocking coordinator).
4. **SQL layer** on top with secondary indexes (global indexes are updated transactionally).
Costs and tactics:
- **Latency:** a multi-range write transaction needs consensus on each touched range plus the commit protocol; in a single region ~5–20 ms, across regions 100–400 ms unless data is **placed** to keep a transaction’s ranges and leaseholders in one region (geo-partitioning, “interleaved” or co-located tables, survival goals).
- **Hot ranges and contention:** serializable transactions on hot keys cause retries (as in the event-sourcing measurement); schema design avoids hot keys.
- **Stale reads:** follower reads at a bounded-stale timestamp trade freshness for locality.
- **Operational model:** multi-node clusters with rebalancing; understand “survive zone failure” vs “survive region failure” settings.
If you need cross-entity ACID at scale and can adopt such a database, it removes most need for sagas *within* that database; sagas remain necessary across independent services and third-party APIs.

---

## 17.10 Choosing an approach `[MK]`

| Situation | Recommended approach |
|---|---|
| All data in one database (or one shard) | **Local ACID transaction** |
| Two tables/aggregates that must be atomic but are in different services | Reconsider boundaries; **merge** or co-locate; else saga |
| Cross-database within one organization, low volume, same datacenter, need strict atomicity, XA supported | **2PC/XA** (carefully: replicated TM, timeouts, orphan cleanup) — rarely on hot paths |
| Need serializable transactions across many rows/shards at scale | **NewSQL** (Spanner/CockroachDB/…) |
| Business process across services/third parties (payments, shipping) | **Saga** (orchestrated) with idempotent steps, outbox, durable saga state |
| Resource reservation with expiry (seats, funds) | **TCC** / reservations |
| Simple linear event flow, autonomous teams | **Choreography** with events (watch for complexity) |
| Need history/audit and multiple read models | **Event sourcing** (+ CQRS) |
| Heavy reads vs writes, multiple query shapes | **CQRS** (with or without ES) |
| “Good enough” consistency, rare anomalies acceptable | **Eventual consistency + reconciliation + compensation (apologies)** |
Examples:
- **Transfer between two accounts in the same bank database:** one local transaction.
- **Transfer between accounts at different banks:** a saga-like protocol with idempotent messages, holds, confirmations, and reconciliation (never 2PC across banks).
- **Marketplace order across inventory, payment provider, and logistics:** orchestrated saga with authorization-then-capture pivot.
- **Ledger with regulatory audit:** event sourcing (immutable entries) with CQRS balances.

---

## 17.11 Failure modes

| Failure | Mechanism | Mitigation |
|---|---|---|
| **In-doubt transactions block locks** | 2PC coordinator failure after prepare | Replicate coordinator via consensus, short timeouts, orphan-prepared cleanup, avoid XA on hot paths |
| **Heuristic decisions diverge** | Operators force commit/abort on a participant | Strict runbooks; use coordinator logs to resolve |
| **Orphaned prepared transactions** | Crashed clients leave `PREPARE TRANSACTION` unresolved | Monitoring (`pg_prepared_xacts`), automated resolver |
| **Duplicate side effects** | Retries without idempotency | Idempotency keys, inbox table, deduplicating consumers |
| **Stuck saga** | Missing compensation, permanently failing step | Durable state, DLQ/alerts, manual intervention tools, saga timeouts |
| **Lost saga on orchestrator crash** | State only in memory | Persist saga log; workflow engines |
| **Compensation arrives before action** | Reordering, timeouts | Tombstones; participants reject late originals |
| **Non-compensatable action taken too early** | Email/shipment before pivot | Order steps; delay irreversible actions |
| **Saga isolation anomalies** | Others see intermediate states | Semantic locks, commutative updates, versioning, pending states |
| **Cascade of compensations** | A compensation itself fails | Make compensations retriable & idempotent; escalate |
| **Hot aggregate contention** | Many writers on one stream | Smaller aggregates, serialize per aggregate, batching |
| **Event schema break** | Old events unreadable | Upcasters, additive changes, version tests with real history |
| **Projection lag/drift** | Slow/failed consumers | Lag alerts, rebuild tooling, idempotent handlers |
| **Read-your-writes violations** | CQRS lag | Tokens, command responses return state, optimistic UI |
| **Snapshot staleness/incompatibility** | Code changes | Version snapshots, rebuild from events |
| **GDPR conflicts** | Immutable personal data | Crypto-shredding, external PII store |
| **Cross-region commit latency** | WAN RTTs in commit protocol | Data placement, regional ownership, bounded-stale reads |
| **Over-engineering** | Event sourcing/CQRS for CRUD | Start simple; adopt when requirements justify |

---

## 17.12 Labs

### Lab 1: 2PC blocking, sagas under failure, and event sourcing (Java 17+)
```java
import java.util.*;
import java.util.concurrent.*;
import java.util.concurrent.atomic.*;

public class TxLab {
    static double exp(Random r, double mean) { return -Math.log(1 - r.nextDouble()) * mean; }

    // ------------------------------------------------------------------ 1) two-phase commit: the blocking problem
    static void twoPhaseCommit() {
        System.out.println("== 1) Two-phase commit blocking. 200 distributed txns/s for 10 min; each locks 1 of 100 hot rows on each of 2 shards; normal lock hold = 10 ms. ==");
        System.out.println("   With probability p the COORDINATOR dies after all participants voted 'prepared' but before announcing the decision: both rows stay locked ('in doubt')");
        System.out.println("   until a coordinator recovers. Any transaction needing one of those rows must wait. (Waiting txns hold no other locks; normal 10 ms queueing is ignored.)");
        System.out.println("scenario                                            txns that waited >1 s     p50 / p99 / p99.9 latency (ms)     worst (s)");
        record Sc(String name, double pCrash, double recoveryMeanSec) {}
        for (Sc sc : new Sc[]{new Sc("no coordinator failures", 0, 0), new Sc("p=0.1%, coordinator back in ~30 s (single node)", 0.001, 30), new Sc("p=0.1%, coordinator replicated: new leader in ~1 s", 0.001, 1)}) {
            Random r = new Random(11); double[] blockedA = new double[100], blockedB = new double[100]; List<Double> lat = new ArrayList<>(); double t = 0; int over1s = 0; double worst = 0;
            while (t < 600) {
                t += exp(r, 1.0 / 200); int a = r.nextInt(100), b = r.nextInt(100);
                double start = Math.max(t, Math.max(blockedA[a], blockedB[b]));                    // wait until neither row is held by an in-doubt transaction
                if (r.nextDouble() < sc.pCrash()) { double until = start + 0.010 + exp(r, sc.recoveryMeanSec()); blockedA[a] = until; blockedB[b] = until; }
                double latency = (start - t) + 0.010; lat.add(latency * 1000); if (latency > 1) over1s++; worst = Math.max(worst, latency);
            }
            Collections.sort(lat);
            System.out.printf("%-52s %6d                    %6.0f / %7.0f / %8.0f                 %6.1f%n", sc.name(), over1s, lat.get(lat.size() / 2), lat.get((int) (lat.size() * 0.99)), lat.get((int) (lat.size() * 0.999)), worst);
        }
    }

    // ------------------------------------------------------------------ 2) sagas under failure
    enum Out { OK, FAIL, TIMEOUT_EXECUTED, TIMEOUT_LOST }
    /** One remote call. If `alreadyExecuted` and the service is idempotent, it just replays the stored result (OK) instead of deciding again. */
    static Out call(Random r, double pFail, boolean alreadyExecutedAndIdempotent) {
        if (alreadyExecutedAndIdempotent) return r.nextDouble() < 0.03 ? Out.TIMEOUT_EXECUTED : Out.OK;      // dedup: same answer as the first time (reply can still be lost)
        double x = r.nextDouble();
        if (x < pFail) return Out.FAIL;                                           // business rejection: no effect
        if (x < pFail + 0.02) return Out.TIMEOUT_EXECUTED;                        // executed, reply lost
        if (x < pFail + 0.03) return Out.TIMEOUT_LOST;                            // request lost, not executed
        return Out.OK;
    }
    static void sagas() {
        int orders = 200_000; double[] pFail = {0.02, 0.05, 0.03};                // inventory, payment, shipping
        System.out.println("\n== 2) Order saga: reserve inventory -> charge payment -> create shipment. " + orders + " orders. Business failures 2% / 5% / 3%;");
        System.out.println("      every call has 2% 'executed but reply lost' and 1% 'request lost'; the orchestrator process crashes after 1% of steps. Up to 4 attempts per call. ==");
        System.out.println("design                                                 consistent   charged-no-goods   double-charged   stock leaked   other");
        for (String design : new String[]{"A: no compensation, blind retries, no saga log", "B: compensation + blind retries, no saga log", "C: compensation + idempotent steps + durable saga log"}) {
            boolean compensate = !design.startsWith("A"), idempotent = design.startsWith("C"), durable = design.startsWith("C");
            Random r = new Random(3); int consistent = 0, chargedNoGoods = 0, doubleCharged = 0, leaked = 0, other = 0;
            for (int o = 0; o < orders; o++) {
                int[] eff = new int[3];                                           // net effect on: inventory reserved, payment charged, shipment created
                int lastTouched = -1; boolean failed = false, abandoned = false, definitiveNoEffect = false;
                for (int step = 0; step < 3 && !failed && !abandoned; step++) {
                    int attempts = 0; boolean done = false; boolean executed = false; lastTouched = step;
                    while (!done && attempts < 4) {
                        attempts++; Out out = call(r, pFail[step], executed && idempotent);
                        switch (out) {
                            case OK -> { if (!executed || !idempotent) eff[step] = idempotent ? 1 : eff[step] + 1; executed = true; done = true; }
                            case TIMEOUT_EXECUTED -> { if (!executed || !idempotent) eff[step] = idempotent ? 1 : eff[step] + 1; executed = true; }       // effect happened but we don't know -> retry
                            case TIMEOUT_LOST -> { }
                            case FAIL -> { failed = true; done = true; definitiveNoEffect = true; }
                        }
                    }
                    if (!done && !failed) failed = true;                          // gave up after 4 attempts: outcome of the last step is UNKNOWN
                    if (!failed && !durable && step < 2 && r.nextDouble() < 0.01) abandoned = true;   // orchestrator crash: without a log nobody resumes the saga
                }
                if (failed && compensate) {                                       // undo in reverse order; also the step whose outcome is unknown (cancel is idempotent in design C)
                    int top = definitiveNoEffect ? lastTouched - 1 : lastTouched;
                    for (int s = top; s >= 0; s--) { int attempts = 0; boolean done = false; boolean undone = false;
                        while (!done && attempts < 4) { attempts++; Out out = call(r, 0, false);
                            if (out == Out.OK || out == Out.TIMEOUT_EXECUTED) { if (!undone || !idempotent) eff[s] = idempotent ? 0 : eff[s] - 1; undone = true; } if (out == Out.OK) done = true; } }
                }
                boolean success = !failed && !abandoned;
                boolean allClear = eff[0] == 0 && eff[1] == 0 && eff[2] == 0, allDone = eff[0] == 1 && eff[1] == 1 && eff[2] == 1;
                if ((success && allDone) || (failed && allClear)) consistent++;
                else if (eff[1] >= 2) doubleCharged++;
                else if (eff[1] == 1 && eff[2] == 0) chargedNoGoods++;
                else if (eff[0] >= 1 && eff[1] == 0) leaked++;
                else other++;
            }
            System.out.printf("%-54s %6.2f%%      %6d             %6d          %6d      %6d%n", design, 100.0 * consistent / orders, chargedNoGoods, doubleCharged, leaked, other);
        }
    }

    // ------------------------------------------------------------------ 3) event sourcing
    record Event(boolean deposit, int amount) {}
    static class Store {
        final ArrayList<Event> events = new ArrayList<>();
        synchronized int version() { return events.size(); }
        synchronized boolean append(int expectedVersion, Event e) { if (expectedVersion >= 0 && expectedVersion != events.size()) return false; events.add(e); return true; }   // -1 = no check
        synchronized List<Event> since(int v) { return new ArrayList<>(events.subList(v, events.size())); }          // the caller advances its version by the size of what it received
    }
    static void eventSourcing() throws Exception {
        System.out.println("\n== 3a) Event sourcing: rebuilding state by replaying events, with and without snapshots ==");
        int total = 5_000_000; Event[] log = new Event[total]; Random r = new Random(1); for (int i = 0; i < total; i++) log[i] = new Event(r.nextInt(3) != 0, 1 + r.nextInt(100));
        long t0 = System.nanoTime(); long bal = 0; for (Event e : log) bal += e.deposit() ? e.amount() : -e.amount(); long t1 = System.nanoTime();
        long[] snap = new long[total / 100_000 + 1]; long running = 0; for (int i = 0; i < total; i++) { running += log[i].deposit() ? log[i].amount() : -log[i].amount(); if (i % 100_000 == 99_999) snap[i / 100_000] = running; }
        long t2 = System.nanoTime(); int from = (total / 100_000) * 100_000; long bal2 = from == 0 ? 0 : snap[from / 100_000 - 1]; for (int i = from; i < total; i++) bal2 += log[i].deposit() ? log[i].amount() : -log[i].amount(); long t3 = System.nanoTime();
        System.out.printf("replay all %,d events: %.1f ms   |   load snapshot (taken every 100,000 events) + replay the tail: %.3f ms   |  same balance: %b%n", total, (t1 - t0) / 1e6, (t3 - t2) / 1e6, bal == bal2);

        System.out.println("\n== 3b) Concurrent commands on ONE account (8 threads x 4,000 commands: withdraw 50 or deposit 40; opening balance 1,000; rule: balance must never go negative) ==");
        for (boolean optimistic : new boolean[]{false, true}) {
            Store store = new Store(); store.append(-1, new Event(true, 1000)); AtomicLong conflicts = new AtomicLong(), rejected = new AtomicLong();
            ExecutorService ex = Executors.newFixedThreadPool(8); CountDownLatch go = new CountDownLatch(1); List<Future<?>> fs = new ArrayList<>();
            for (int th = 0; th < 8; th++) { final int seed = th; fs.add(ex.submit(() -> { Random rr = new Random(seed); int version = 0; long balance = 0; try { go.await(); } catch (InterruptedException e) { return; }
                for (int i = 0; i < 4_000; i++) {
                    boolean deposit = rr.nextBoolean();
                    while (true) {
                        List<Event> fresh = store.since(version); for (Event e : fresh) balance += e.deposit() ? e.amount() : -e.amount(); version += fresh.size();   // catch up on events we haven't seen
                        java.util.concurrent.locks.LockSupport.parkNanos(30_000);                                                                    // pretend to do 30 microseconds of business logic
                        if (!deposit && balance < 50) { rejected.incrementAndGet(); break; }                                                    // business rule on a (possibly stale) state
                        Event ev = deposit ? new Event(true, 40) : new Event(false, 50);
                        if (store.append(optimistic ? version : -1, ev)) break;                                                                   // optimistic: only if nobody else appended since we read
                        conflicts.incrementAndGet();                                                                                              // lost the race -> reload and re-decide
                    }
                } })); }
            go.countDown(); for (Future<?> f : fs) f.get(); ex.shutdown();
            long bal3 = 0, min = Long.MAX_VALUE; for (Event e : store.events) { bal3 += e.deposit() ? e.amount() : -e.amount(); min = Math.min(min, bal3); }
            System.out.printf("%-34s events=%,d  rule violated (balance went negative): %-5s  lowest balance ever: %,d   retries after conflict: %,d   rejected (insufficient funds): %,d%n",
                optimistic ? "optimistic concurrency (expectedVersion)" : "no version check (blind append)", store.events.size(), min < 0, min, conflicts.get(), rejected.get());
        }
    }

    public static void main(String[] a) throws Exception { twoPhaseCommit(); sagas(); eventSourcing(); }
}
```
**Sample output** (parts 3a/3b involve threads and wall-clock timing: exact numbers vary run to run)
```text
== 1) Two-phase commit blocking. 200 distributed txns/s for 10 min; each locks 1 of 100 hot rows on each of 2 shards; normal lock hold = 10 ms. ==
   With probability p the COORDINATOR dies after all participants voted 'prepared' but before announcing the decision: both rows stay locked ('in doubt')
   until a coordinator recovers. Any transaction needing one of those rows must wait. (Waiting txns hold no other locks; normal 10 ms queueing is ignored.)
scenario                                            txns that waited >1 s     p50 / p99 / p99.9 latency (ms)     worst (s)
no coordinator failures                                   0                        10 /      10 /       10                    0.0
p=0.1%, coordinator back in ~30 s (single node)       12950                        10 /   75520 /   109945                  138.1
p=0.1%, coordinator replicated: new leader in ~1 s      201                        10 /      10 /     1549                    4.6

== 2) Order saga: reserve inventory -> charge payment -> create shipment. 200000 orders. Business failures 2% / 5% / 3%;
      every call has 2% 'executed but reply lost' and 1% 'request lost'; the orchestrator process crashes after 1% of steps. Up to 4 attempts per call. ==
design                                                 consistent   charged-no-goods   double-charged   stock leaked   other
A: no compensation, blind retries, no saga log          85.05%        7384               3787           11764        6962
B: compensation + blind retries, no saga log            92.22%        2091               3706            2321        7452
C: compensation + idempotent steps + durable saga log  100.00%           0                  0               0           0

== 3a) Event sourcing: rebuilding state by replaying events, with and without snapshots ==
replay all 5,000,000 events: 46.3 ms   |   load snapshot (taken every 100,000 events) + replay the tail: 0.003 ms   |  same balance: true

== 3b) Concurrent commands on ONE account (8 threads x 4,000 commands: withdraw 50 or deposit 40; opening balance 1,000; rule: balance must never go negative) ==
no version check (blind append)    events=29,027  rule violated (balance went negative): true   lowest balance ever: -350   retries after conflict: 0   rejected (insufficient funds): 2,974
optimistic concurrency (expectedVersion) events=29,029  rule violated (balance went negative): false  lowest balance ever: 0   retries after conflict: 196,764   rejected (insufficient funds): 2,972
```
**Experiments**
1. In part 1, vary the coordinator crash probability (0.01%, 0.1%, 1%) and recovery time (1 s, 10 s, 60 s). Plot the fraction of transactions delayed >1 s. Find the point at which the system effectively becomes unavailable.
2. In part 1, add **participant timeouts with unilateral abort before voting** and **cooperative termination** (prepared participants ask each other: if any participant never voted or has the decision, the others can learn it). In which cases does this still block?
3. In part 2, drop the saga log from design C but keep idempotency and compensation. How many inconsistencies come back, and why? Then keep the log but remove idempotency of *compensations*.
4. In part 2, add the **pivot**: make the payment step irreversible (no compensation) and move it after the risky shipping check. How does the “charged-no-goods” count change?
5. Add **late-arriving original requests** (a compensation reaches a participant before the delayed reserve request). Implement tombstones and show the leak disappears.
6. In part 3b, replace the retry loop with a **single-writer-per-aggregate queue** (one thread processes commands for the account sequentially). Compare throughput and retry counts with optimistic concurrency under 8 producers.
7. Add **upcasting**: define `Deposited` v1 (amount in dollars, int) and v2 (amount in cents, long, with currency); write an upcaster and replay a mixed-version log to the same balance.

### Lab 2: Real two-phase commit in PostgreSQL (needs Postgres with `max_prepared_transactions > 0`; not executed in the book’s sandbox)
```sql
-- two databases/sessions simulating two participants (or two Postgres instances)
-- Participant A
BEGIN; UPDATE accounts SET balance = balance - 100 WHERE id = 1; PREPARE TRANSACTION 'tx-8841-A';
-- Participant B
BEGIN; UPDATE accounts SET balance = balance + 100 WHERE id = 2; PREPARE TRANSACTION 'tx-8841-B';
SELECT gid, prepared, owner FROM pg_prepared_xacts;      -- locks are still held on both rows even though no session is open
-- another session: UPDATE accounts SET balance = 0 WHERE id = 1;   <-- blocks!
-- coordinator decides:
COMMIT PREPARED 'tx-8841-A'; COMMIT PREPARED 'tx-8841-B';   -- or ROLLBACK PREPARED
```
**Experiments:** (1) “crash” the coordinator after preparing and observe the blocked updater; (2) restart Postgres while a transaction is prepared and confirm the prepared state survives the restart; (3) leave an orphaned prepared transaction and observe its effect on `VACUUM` (old xmin) and locks; (4) write a small resolver that lists prepared transactions older than N minutes and decides based on a coordinator log table.

### Lab 3: Design a travel-booking saga (paper)
Flight, hotel, and car rental must all be booked or none. Third-party APIs have idempotency keys and cancel endpoints with fees. Specify: step order, pivot, compensations (and their costs), timeouts, TCC/hold semantics, saga state machine, how you handle the hotel confirming after you already cancelled (late arrival), user-visible states, observability, and a reconciliation job.

---

## 17.13 Interview perspective

- Start with **“avoid distributed transactions: choose service/shard boundaries so invariants live in one place.”**
- For cross-service flows say **saga (orchestrated)** with **compensations, idempotent steps, outbox, durable saga state**, and name the **pivot**.
- Explain **2PC** and its **blocking problem**, then **how Spanner/CockroachDB avoid it** (consensus-replicated participants and transaction records).
- Mention **idempotency keys and unknown outcomes** for third parties; **authorize then capture** for payments.
- For audit/history requirements, bring up **event sourcing** with **optimistic concurrency, snapshots, projections**, and the costs (complexity, schema evolution, eventual consistency); add **CQRS** only when read/write shapes differ.
- Discuss **saga isolation** (semantic locks, pending states) and **reconciliation** as the final safety net.
- Quantify: 2PC = 2 RTT + 3 fsyncs; blocking lock hold times; saga steps × failure probability; replay cost vs snapshot interval.

---

## 17.14 Common misconceptions

1. **“Just use 2PC/XA across services.”** It blocks, holds locks, and third parties don’t support it.
2. **“Sagas give ACID.”** They give atomicity (eventually) and durability, not isolation.
3. **“A compensation is a rollback.”** It’s a new forward action on already-visible state.
4. **“Retries make sagas reliable.”** Without idempotency they cause double effects (lab: 3,787 double charges).
5. **“Event sourcing is Kafka.”** An event store needs per-stream ordering, optimistic concurrency, and load-by-stream.
6. **“CQRS requires event sourcing (or vice versa).”** They’re independent.
7. **“Event sourcing means we can never delete data.”** Crypto-shredding and external PII stores exist.
8. **“Snapshots replace events.”** They’re caches; events remain the truth.
9. **“Optimistic concurrency is free.”** Hot aggregates cause heavy retries (~6 per command in the lab).
10. **“Choreography is simpler than orchestration.”** Simple for 2–3 steps; opaque for complex flows.
11. **“Eventual consistency is a bug.”** It’s a design choice when invariants allow (Ch 15).
12. **“If the saga fails, the system rolls itself back.”** Only if you designed, built, and tested every compensation.

---

## 17.15 Chapter summary

- **Atomic commit across nodes is hard** because of partial failure and the Two Generals problem; first **avoid it** by co-locating data and drawing boundaries around invariants.
- **2PC:** prepare (vote, durable PREPARED, locks held) then commit/abort; commit point is the coordinator’s log; **blocking**: a coordinator crash after prepare leaves participants in doubt. Measured: a 0.1% coordinator failure rate with 30 s recovery left **12,950 transactions waiting >1 s (p99 75 s)**; with a replicated coordinator only **201** (p99 10 ms). 3PC doesn’t work in real networks; **consensus-based commit** (Spanner, CockroachDB) removes blocking at the cost of extra round trips.
- **Distributed isolation** uses MVCC with global timestamps (TrueTime/HLC/oracles), Percolator-style client-driven 2PC, or deterministic ordering (Calvin); write skew and strictness vary by system.
- **Sagas** = local transactions + compensations; orchestration vs choreography; pivot and retriable steps; compensation ≠ rollback; need **idempotent steps, durable saga log, and compensation of unknown-outcome steps**: lab went from **85.05%** consistent (naive) to **92.22%** (compensation only) to **100.00%** (all three). Sagas lack isolation: use semantic locks, commutative updates, versions. **TCC** makes reservations explicit.
- **Outbox/inbox + idempotency keys** give exactly-once effects without distributed transactions.
- **Event sourcing:** store events, derive state; **expectedVersion** concurrency (blind appends overdrew an account to −350; optimistic concurrency kept it ≥ 0 at ~6 retries/command on a hot aggregate); **snapshots** cut a 46 ms replay of 5 M events to ~0.003 ms; projections are rebuildable and eventually consistent; handle schema evolution (upcasting) and GDPR (crypto-shredding).
- **CQRS** separates write and read models; costs: eventual consistency, projection management; solve read-your-writes with tokens/optimistic UI; don’t adopt without need.
- **Choose** the simplest mechanism that preserves the invariant: local transaction → NewSQL → saga/TCC → eventual consistency with reconciliation.

### Mental models
| Concept | Mental model |
|---|---|
| 2PC | A wedding: “Do you? — I do. — Then you are married,” but if the officiant faints after the vows, the couple waits in limbo |
| Blocking | Everyone holding their breath until the one person who knows the verdict returns |
| Saga | A trip itinerary where every booking has a cancellation plan, and a travel agent keeps the checklist |
| Compensation | A refund and an apology letter, not a time machine |
| Pivot | The point of no return, like boarding the plane |
| Event sourcing | An accountant’s ledger: never erase, only add lines; balance is the sum |
| Snapshot | A bookmark with the running total so you don’t re-add from page one |
| CQRS | The kitchen (write) and the menu board (read) are different things, updated by messengers |

---

## 17.16 Exercises

### A. Conceptual
1. Why can’t a distributed system simply commit each step independently and hope? Describe the “half placed order” problem.
2. List four ways to avoid distributed transactions.
3. Walk through 2PC for three participants, naming the log records written by each party and the commit point.
4. For each case, state what participants do: (a) participant crashes before voting; (b) participant crashes after voting commit; (c) coordinator crashes before decision; (d) coordinator crashes after decision.
5. Explain the 2PC blocking problem. Why can’t a prepared participant just abort?
6. Why isn’t 3PC used in practice?
7. How do Spanner and CockroachDB make atomic commit non-blocking?
8. What is XA? Give three practical problems with using it in microservices.
9. Define a saga, a compensating transaction, a pivot transaction, and a retriable transaction.
10. Why is a compensation not a rollback? Give two examples where compensation is imperfect.
11. Compare orchestration and choreography across visibility, coupling, and failure handling. When choose each?
12. List the properties compensations must have.
13. Explain saga isolation anomalies and three countermeasures.
14. What is TCC and how does it differ from a saga?
15. Explain the transactional outbox and inbox. How do they yield exactly-once effects?
16. Define event sourcing, aggregate, stream, command, event. What is expectedVersion for?
17. What are snapshots and when do you take them? Why aren’t they the source of truth?
18. How do you evolve event schemas? Explain upcasting.
19. How can event sourcing coexist with the right to erasure?
20. What is CQRS? Does it require event sourcing?
21. List the consequences of CQRS on consistency and how to mitigate them.
22. When would you not use event sourcing or CQRS?
23. How would you pick between saga, 2PC, NewSQL, and eventual consistency for (a) same-bank transfer, (b) marketplace order, (c) inter-bank transfer?

### B. Calculations / Reasoning
24. 2PC latency: RTT 1 ms within a datacenter, fsync 2 ms; compute the commit latency for 3 participants (assume parallel messaging). Repeat with RTT 80 ms between regions. How does hold time limit throughput on a hot row (1/hold-time)?
25. A hot row is part of 2PC transactions. Lock hold time is 8 ms in the happy path. With 0.2% of transactions stuck for 20 s (coordinator recovery), compute the expected number of queued transactions if arrival at that row is 50/s.
26. Saga success probability: 5 steps with business-failure probabilities 1%, 3%, 2%, 5%, 1%. What fraction of sagas complete? Expected number of compensations executed per saga (steps already done when a failure occurs)?
27. A saga step times out 3% of the time; half of those executed. 500,000 sagas/day, 4 steps. How many duplicate effects/day without idempotency (assuming one blind retry)?
28. Event store: 2 million events per aggregate per year on a hot account; replay at 1 µs per event in memory but 50 µs per event from disk with deserialization. Time to rebuild state with and without snapshots every 10,000 events?
29. Optimistic concurrency: 4 writers contend on one aggregate; each command takes 5 ms between read and append. Estimate the conflict probability and expected attempts (hint: another writer must append during your 5 ms window; approximate each other writer’s commit rate as 1 per 5 ms).
30. Projection lag: events arrive at 2,000/s; the projection handles 1,500/s. How long until lag reaches 60 s? How many projection workers (assuming linear scaling, per-stream ordering needs key partitioning) are required to catch up in 10 minutes after a 5-minute burst at 2,000/s?
31. Idempotency window: 50 M messages/day, dedup entries kept for 7 days at 60 B each: storage? What if retries can arrive after 30 days?
32. A cross-region NewSQL transaction touching two ranges with leaseholders in us-east and eu-west (RTT 85 ms) and a commit protocol of 2 round trips. Estimate latency, and describe one data-placement change that cuts it to a single-region commit.

### C. Design
33. Design ShipIt’s order saga: steps, compensations, pivot, saga state machine, messages, idempotency, timeouts, reconciliation.
34. Design a payment flow with a third-party provider supporting authorize/capture/void/refund and webhooks; handle unknown outcomes and duplicates.
35. Design an event-sourced shopping cart or bank account: events, aggregate boundaries, snapshots, projections, and read-your-writes.
36. Design a CQRS read model for “order history with search” fed by CDC from Postgres: consistency expectations, lag monitoring, rebuild strategy.
37. Decide whether to use Postgres + sagas or CockroachDB for a marketplace with orders, inventory, and payouts across regions; give criteria.
38. Design the compensation and reconciliation strategy for the airline-style overbooking case.

### D. Debugging
39. Customers are charged twice about 0.4% of the time after timeouts. Identify the missing mechanisms and fix.
40. Inventory shows negative stock after a promotion; the system uses a saga with “reserve stock” steps. List three possible causes.
41. After a coordinator restart, 12 transactions remain “prepared” forever and DDL on the table hangs. Diagnose and fix.
42. An event-sourced service is slow to start because it replays 80 M events per aggregate type. Options?
43. After deploying a new version, replaying old events crashes. What went wrong and what is the right pattern?
44. A saga compensation “cancel shipment” fails with “already dispatched.” How do you handle this in design?

### E. Interview-style
45. “Design a checkout across inventory, payment, and shipping microservices.”
46. “Explain the difference between 2PC and saga. When would you use each?”
47. “What is event sourcing? What problems did it solve and what problems did it create for you?”

### F. Teach-back
48. Explain the saga pattern to a non-engineer using a vacation booking with cancellation policies.

---

## 17.17 Full answer key

**1.** Independent commits mean a crash between them leaves some effects applied and others not (charged but no order), with no automatic way back. There’s no atomic “all or nothing” across separate stores without a protocol.

**2.** Co-locate related data in one DB/shard; set service boundaries around invariants (aggregates); merge chatty services; relax to eventual consistency with idempotent messaging and compensation.

**3.** Coordinator sends PREPARE; each participant does work, force-writes PREPARED, votes. Coordinator, if all yes, force-writes COMMIT (commit point), sends COMMIT; participants force-write COMMIT, release locks, ack; coordinator writes END. If any no/timeout: ABORT.

**4.** (a) Coordinator times out and aborts. (b) On recovery the participant finds PREPARED and asks the coordinator, holding locks until told. (c) Prepared participants wait (blocked); non-voted ones may abort. (d) On recovery the coordinator resends the decision from its log; meanwhile prepared participants wait.

**5.** After voting commit a participant promised it can commit and the coordinator may already have decided commit (or others voted accordingly); aborting unilaterally could contradict a commit decision. It must wait while holding locks.

**6.** 3PC assumes a synchronous bounded-delay network and crash-only failures; under partitions or delays it can violate atomicity, and adds a round trip. Consensus-based commit is the practical solution.

**7.** Participants are replicated consensus groups (never “disappear”), and the coordinator/transaction record is replicated, so a failed leader is replaced and any node can resolve in-doubt intents from the replicated record; no single process holds the only copy of the decision.

**8.** XA is a standard 2PC interface between a transaction manager and resource managers. Problems: locks held across the protocol, blocking on coordinator failure, poor throughput under contention, orphaned prepared transactions, unsupported by most services (HTTP APIs, SaaS, NoSQL), operational complexity, heuristics.

**9.** Saga: a sequence of local transactions with compensations. Compensating transaction: semantic undo of a committed step. Pivot: the go/no-go step after which the saga will run to completion. Retriable: steps after the pivot that are retried until success.

**10.** The original effect already committed and may have been observed or triggered external actions; the compensation is a new action. Imperfect: refund fees and statement entries remain; an email or push notification can’t be unsent; a shipped package can’t be unshipped.

**11.** Orchestration: central coordinator with visible flow, easy compensation logic, more coupling to orchestrator; choreography: events between autonomous services, loosely coupled but flow is implicit and hard to debug. Use orchestration for complex flows with compensations/timeouts; choreography for simple linear flows.

**12.** Idempotent, retriable until success, tolerate the original not having happened or having partially happened, commutative with late arrivals (tombstones), and eventually succeed.

**13.** Saga lacks isolation: dirty reads (others see intermediate states), lost updates (another transaction overwrites between steps), non-repeatable reads. Countermeasures: semantic locks, commutative updates, version checks/rereads, pessimistic view, by-value decisions.

**14.** TCC: Try (tentative reservation with expiry), Confirm (finalize), Cancel (release). Sagas commit each step and compensate; TCC keeps resources in a reserved state until confirmation, making intermediate states explicit.

**15.** Outbox: write state and outgoing message in one local transaction; a relay publishes at-least-once. Inbox: store processed message IDs in the same transaction as the effect with a unique constraint. Together: at-least-once delivery + idempotent processing = exactly-once effect.

**16.** Event sourcing stores immutable events and derives state by replay. Aggregate: consistency boundary; stream: events of one aggregate; command: intention that may produce events; event: past-tense fact. expectedVersion enforces optimistic concurrency: append only if the stream’s version is unchanged since the decision.

**17.** Snapshots store the folded state at a version to bound replay time. Take them every N events or periodically. They’re caches (derivable from events), can become incompatible with code, so events remain the truth.

**18.** Prefer additive changes; use versioned event types; upcasters transform old versions to the current shape at read time; for large changes, migrate into a new stream.

**19.** Keep PII outside events (reference IDs), or encrypt personal fields per subject and delete the key (crypto-shredding); as a last resort rewrite streams.

**20.** CQRS uses separate models for commands and queries; read models are fed asynchronously. No, it can use a normal DB with CDC; event sourcing is just a convenient feed.

**21.** Eventual consistency lag, projection failures, duplication of logic. Mitigations: return fresh state in the command response, read-your-writes tokens, optimistic UI, idempotent/rebuildable projections, lag monitoring.

**22.** Simple CRUD, small domains, teams unfamiliar with the patterns, strong immediate consistency needs on reads, or where an audit table suffices.

**23.** (a) Local ACID transaction. (b) Orchestrated saga with authorize/capture pivot. (c) Saga-like protocol with idempotent messages, holds, confirmations, and reconciliation (not 2PC).

**24.** Intra-DC: PREPARE (0.5 ms one-way + participant fsync 2 ms) + vote back (0.5) ≈ 3 ms; coordinator fsync 2 ms; COMMIT (0.5) + participant fsync 2 + ack (0.5) ≈ 3 ms → ≈ **8 ms** (about 2 RTT + 3 fsyncs). Cross-region (RTT 80): 80 + 2 + 2 + 80 + 2 ≈ **166 ms**. Hold time on a hot row ≈ transaction duration + commit: 8 ms → max ~125 updates/s per row; 166 ms → ~6 updates/s per row.

**25.** Each stuck transaction blocks every arrival on that row for 20 s: 50/s × 20 s = **1,000 queued transactions** per stuck event. And stuck events are frequent: 0.2% × 50/s = 0.1 per second, i.e., one every 10 s, each lasting about 20 s, so on average two in-doubt holds overlap and the row is **effectively permanently blocked**. A 0.2% coordinator failure rate on a hot row is therefore catastrophic, not a 0.2% problem.

**26.** Completion = 0.99 × 0.97 × 0.98 × 0.95 × 0.99 = 0.99×0.97 = 0.9603; ×0.98 = 0.94109; ×0.95 = 0.89404; ×0.99 = 0.88510 → **88.5%** complete. Expected compensations: failure at step k triggers k−1 compensations: P(fail at 1) = 0.01 (0 comps), at 2 = 0.99×0.03 = 0.0297 (1 comp), at 3 = 0.9603×0.02 = 0.019206 (2), at 4 = 0.94109×0.05 = 0.047055 (3), at 5 = 0.89404×0.01 = 0.00894 (4). Expected = 0.0297 + 2×0.019206 + 3×0.047055 + 4×0.00894 = 0.0297 + 0.03841 + 0.14117 + 0.03576 ≈ **0.245 compensations per saga**.

**27.** Executed-but-timed-out per call: 3% × 0.5 = 1.5%. Calls/day = 500,000 × 4 = 2,000,000 → 30,000 duplicate effects/day (each such timeout is retried and executed again).

**28.** Without snapshots: from memory 2,000,000 × 1 µs = 2 s; from disk 2,000,000 × 50 µs = **100 s**. With snapshots every 10,000 events: load snapshot + at most 10,000 events × 50 µs = **0.5 s** (worst case).

**29.** Window 5 ms; each of the other 3 writers commits about once per 5 ms in steady contention, so the expected number of appends during my window ≈ 3 → conflict probability ≈ 1 − e^(−3) ≈ **95%** per attempt; the system degrades to roughly one success per round with many retries; expected attempts per command is high (several). The remedy is serialization per aggregate or splitting aggregates.

**30.** Lag grows 500/s → 60 s lag means 60 s × 2,000/s = 120,000 events behind; at +500 events/s net: 240 s ≈ **4 min**. After a 5-minute burst at 2,000/s with capacity 1,500/s: backlog 150,000 events; to drain within 10 minutes while arrivals are (say) 1,000/s: need capacity ≥ 1,000 + 150,000/600 = 1,250/s → one worker (1,500/s) suffices; if arrivals continue at 2,000/s you need ≥ 2,000 + 250 = 2,250/s → **2 workers** (partitioned by stream key).

**31.** 50 M × 7 days = 350 M entries × 60 B = **21 GB**. For 30 days: 1.5 B entries → **90 GB** (or use time-partitioned tables and tiered storage; consider whether such late retries are possible; add idempotent business keys to bound the window).

**32.** Two round trips × 85 ms ≈ **170 ms** plus consensus latencies. Placement change: pin both ranges’ leaseholders/voters in one region (geo-partitioning by customer region or co-locating related tables) so the transaction commits within a region (~5–20 ms), serving users from their home region.

**33 (sketch).** State machine: PENDING → INVENTORY_RESERVED → PAYMENT_AUTHORIZED → PAYMENT_CAPTURED (pivot) → SHIPMENT_CREATED → CONFIRMED; failures before capture compensate (void auth, release stock, cancel order); after capture retry shipping until success; all commands carry saga ID and idempotency key; orchestrator state persisted; timeouts per step; outbox for commands; inbox at services; reconciliation compares orders/payments/shipments hourly.

**34 (sketch).** Create `payment_id` locally; call provider with idempotency key `authorize`; on timeout mark `UNKNOWN` and query provider by key; capture only after inventory reserved; handle webhooks idempotently (dedupe on provider event ID); void on failure; refunds as compensations; reconcile daily with provider statements.

**35 (sketch).** Events: `CartCreated`, `ItemAdded`, `ItemRemoved`, `CheckedOut`; aggregate = one cart; snapshots every 200 events; projections: cart view, abandoned-cart analytics; command response returns new cart view for read-your-writes; optimistic concurrency per cart with retry/merge (add-item commutes).

**36 (sketch).** Debezium CDC from Postgres → Kafka → projector → Elasticsearch (search) + Postgres read table; consistency: seconds lag; monitor event age; read-your-writes via returning created order in response + token; rebuild by re-snapshotting and replaying; idempotent upserts by (order_id, version).

**37 (sketch).** Criteria: cross-region transactional needs, team expertise, operational maturity, latency budgets, scale. Postgres + sagas: simpler infra, strong tooling, explicit saga logic, regional sharding; CockroachDB: serializable multi-region transactions, built-in resilience, higher latency/cost, SQL compatibility caveats.

**38 (sketch).** Allow bounded overbooking (statistical limit); detect at check-in; compensate with rebooking, vouchers, compensation payments; track costs; limit overbooking per flight; reconciliation job identifies conflicts early; communicate transparently.

**39.** Missing idempotency key/dedupe at the payment call and in the retry path; no `UNKNOWN` state with query-before-retry; no reconciliation. Fix: idempotency key per payment attempt, provider-side dedupe, state machine, nightly reconciliation.

**40.** (a) Non-atomic check-then-reserve race (no conditional update), (b) compensations double-releasing (not idempotent), (c) reservations never expiring or released twice; also overselling via caches. Fix with atomic conditional decrement, idempotent release, reservation expiry, and audits.

**41.** Orphaned prepared transactions hold locks and block DDL/vacuum. Find via `pg_prepared_xacts`, decide with the coordinator’s log, then `COMMIT PREPARED` or `ROLLBACK PREPARED`; add monitoring and an automated resolver; reduce reliance on XA.

**42.** Snapshots; lazy loading per aggregate (not full replay at startup); projections for read paths; caching hot aggregates; partitioned startup; archive old streams.

**43.** The new code can’t interpret old event shapes (breaking change). Right pattern: backward-compatible event schemas, upcasters, tests that replay a corpus of real historical events in CI.

**44.** Design the saga so non-cancelable steps occur after the pivot or are guarded by a hold period; if cancellation fails, escalate to a manual/alternative compensation (e.g., return flow, partial refund), model the state `COMPENSATION_FAILED` with alerts.

**45 (sketch).** Orchestrated saga with reservation → authorization → capture pivot → shipment; outbox/inbox; idempotency keys; durable saga log; timeouts; compensations; reconciliation; user-visible states; metrics.

**46 (sketch).** 2PC: atomic across resources, blocking, locks, XA, suitable for tightly coupled, same-org, low-volume cases; saga: eventual atomicity via compensations, no cross-service locks, suitable for microservices and third parties; NewSQL for atomic multi-shard SQL.

**47 (sketch).** Solved: auditability, time travel, multiple projections, decoupled read/write; created: schema evolution, replay performance, eventual consistency of views, steep learning curve, handling privacy.

**48.** You book flights, hotel, and car separately. Each booking has its own cancellation rule. If the hotel falls through, the travel agent (orchestrator) cancels the flight and car according to those rules, possibly paying small fees (compensation). You never get a magic “undo everything instantly,” but you end up with a consistent itinerary or none.

---

## 17.18 Flashcards

| Front | Back |
|---|---|
| Avoid distributed txns | Co-locate data, align boundaries with invariants |
| 2PC | Prepare (vote, PREPARED, locks) → commit/abort; commit point = coordinator log |
| 2PC cost | 2 RTT + ~3 fsyncs |
| 2PC blocking | Coordinator crash after prepare ⇒ participants in doubt, locks held |
| Lab 2PC | p=0.1%, 30 s recovery: 12,950 txns >1 s, p99 75 s; replicated: 201, p99 10 ms |
| Non-blocking commit | Consensus-replicated participants and transaction records (Spanner, CockroachDB) |
| XA | Standard 2PC interface; orphaned prepared txns hurt |
| Saga | Local txns + compensations |
| Pivot / retriable | Point of no return / steps retried to completion |
| Compensation | Forward semantic undo; idempotent, retriable, handles late arrivals |
| Orchestration vs choreography | Central controller vs event reactions |
| Saga safety recipe | Idempotent steps + durable saga log + compensation (lab: 85% → 92% → 100%) |
| Saga isolation | None: use semantic locks, commutative updates, versions |
| TCC | Try (reserve) – Confirm – Cancel |
| Outbox / inbox | Atomic state+message; atomic dedupe+effect |
| Event sourcing | Store events; state = fold(events) |
| expectedVersion | Optimistic concurrency on append |
| Hot aggregate | Retries explode (~6/command in lab) |
| Snapshot | Cache of folded state; 46 ms → 0.003 ms in lab |
| Upcasting | Convert old event versions on read |
| CQRS | Separate write and read models; eventual consistency |
| Crypto-shredding | Delete the key to erase personal data in immutable logs |

## 17.19 Glossary additions

| Term | Simple Meaning | Technical Meaning | Why It Exists | Example |
|---|---|---|---|---|
| Distributed transaction | Multi-node atomic operation | Atomic commit across independent stores | Cross-store consistency | Transfer |
| Two-phase commit | Vote then decide | Prepare/commit atomic commit protocol | Atomicity | XA |
| Coordinator / participant | Decider / voters | Roles in 2PC | Structure | TM / DB |
| In-doubt (prepared) | Waiting for verdict | Voted commit, outcome unknown | The blocking state | `pg_prepared_xacts` |
| Paxos Commit | Fault-tolerant commit | Commit decision via consensus | Non-blocking | Spanner |
| Saga | Chain with undo steps | Local txns + compensations | Cross-service processes | Order saga |
| Compensation | Semantic undo | Forward action reversing business effect | Atomicity (eventual) | Refund |
| Pivot | Point of no return | Step after which saga completes | Design | Capture payment |
| Orchestrator | Saga conductor | Central saga coordinator | Visibility | Temporal |
| Choreography | Event reactions | Decentralized saga | Autonomy | Event chain |
| TCC | Try/Confirm/Cancel | Reservation-based pattern | Explicit holds | Seat hold |
| Semantic lock | App-level lock flag | Status preventing conflicting operations | Saga isolation | `PENDING` |
| Outbox / inbox | Reliable send / dedupe receive | Transactional message tables | Exactly-once effects | `outbox` |
| Event sourcing | Store the history | Events as source of truth | Audit/replay | Ledger |
| Aggregate | Consistency boundary | Entity cluster enforcing invariants | DDD | Order |
| Expected version | Concurrency guard | Append only if stream version matches | Optimistic concurrency | `v=42` |
| Snapshot | State checkpoint | Folded state at a version | Fast loading | Every 1,000 events |
| Projection | Derived view | Read model built from events | Queries | Orders table |
| Upcaster | Old→new event converter | Read-time schema upgrade | Schema evolution | v1→v2 |
| CQRS | Split read/write models | Command/query segregation | Scale/shape | Search index |
| Crypto-shredding | Erase by key deletion | Per-subject encryption keys | GDPR | KMS key delete |
| NewSQL | Scalable SQL | Consensus + MVCC + distributed txns | ACID at scale | CockroachDB |
| Percolator | Client-driven 2PC | Snapshot-isolation txns on a KV store | NoSQL transactions | TiDB |

---

**Batch 4 complete.** Part III so far: Ch 14 (fundamentals), 15 (consistency/CAP), 16 (consensus), 17 (transactions/sagas/event sourcing/CQRS).

**Next: Batch 5, Chapters 18–20 + 21–23.** Ch 18 Reliability Engineering and Resilience Patterns, Ch 19 Microservices and Architecture Styles, Ch 20 Probabilistic Data Structures, Clocks Revisited, CRDTs and Gossip; then Part IV: Ch 21 Observability, Ch 22 Security, Ch 23 Cloud and Containers.

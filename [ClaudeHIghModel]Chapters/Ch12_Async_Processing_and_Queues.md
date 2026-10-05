# Chapter 12: Asynchronous Processing and Queues

## Chapter card

| | |
|---|---|
| **Objective** | Learn to decouple work in time: queues vs pub/sub, delivery semantics and why “exactly-once” is really idempotency, acknowledgements and visibility timeouts, retries/backoff/dead-letter queues, backpressure and load leveling, ordering, and the standard patterns (outbox, inbox, claim-check, delayed jobs, workflows) |
| **Why this chapter exists** | Almost every large system moves slow, unreliable, or bursty work off the request path. Queues turn spikes into smooth load and isolate failures, but they introduce duplicates, reordering, and eventual consistency. This chapter teaches you to use them safely |
| **Prerequisites** | Chapters 2 (idempotency), 3 (transactions), 4 (outbox/CDC), 5 (queueing theory), 6, 8 |
| **Study time** | 10–13 hours including labs |
| **Tier** | `[MK]` throughout; workflow engines and broker internals `[GK]` |
| **Mastery criteria** | (1) Decide what belongs on the synchronous path and what to make async. (2) Contrast work queues, pub/sub, and logs. (3) Explain at-most-once, at-least-once, and “exactly-once” with the exact crash windows. (4) Design idempotent consumers. (5) Design retry, backoff, and DLQ policies and tune visibility timeouts. (6) Reason about backpressure with numbers (arrival vs service rate, queue age). (7) Preserve per-entity ordering while scaling consumers. (8) Apply the outbox, inbox, claim-check, and delayed-job patterns. (9) Choose a broker for a scenario |

---

## 12.1 The Story: One Button, Seven Side Effects

**OrderFlow**’s “Place order” handler does everything inline:

```text
POST /checkout
  1. charge the card            (payment provider, ~400 ms, p99 2.5 s)
  2. decrement inventory        (DB)
  3. send confirmation email    (email provider, ~300 ms, sometimes down)
  4. render a PDF invoice       (CPU, ~700 ms)
  5. notify the warehouse       (partner API, flaky)
  6. record analytics events    (warehouse DB)
  7. award loyalty points       (another service)
```
Symptoms:
- Checkout p50 = 2.1 s, **p99 = 8 s**: the sum of seven dependencies (Ch 5: latencies add, tails compound).
- When the email provider has a 20-minute outage, **checkouts fail** even though the order and payment succeeded.
- During a flash sale, request threads are all blocked on slow partners; the site stops accepting orders at 1,200 req/s although CPU is at 15%.
- Users who see a timeout press “Place order” again: **two orders, two charges**.
- A deploy restarts servers mid-request: some orders are charged but the warehouse is never notified.

### Pause & Predict 12.1
> Which of the seven steps *must* complete before you tell the customer “order placed”? Which could happen later without the customer noticing? What would you need to guarantee that the “later” steps eventually happen exactly as intended, even through crashes, retries, and duplicate button presses?

---

## 12.2 Why asynchronous processing `[MK]`

### 12.2.1 What we gain
| Benefit | Mechanism |
|---|---|
| **Lower user latency** | Respond after the critical steps; defer the rest |
| **Failure isolation** | An email outage delays emails; it doesn’t fail checkouts |
| **Load leveling (buffering)** | A spike fills a queue; consumers process at a steady rate (Ch 5: protect the knee) |
| **Independent scaling** | Scale workers for slow jobs separately from the web tier |
| **Retry and resilience** | Failed jobs are retried later without the user waiting |
| **Decoupling** | Producers don’t know consumers; add new consumers (analytics, fraud) without touching the producer |
| **Smoothing costs** | Run heavy jobs on cheaper capacity, off-peak, or on spot instances |

### 12.2.2 What we pay
- **Eventual consistency:** the system is briefly in an in-between state (order placed, email not yet sent).
- **Duplicates and reordering** (the rest of this chapter).
- **Harder debugging:** a request’s work is spread over time and services; need correlation IDs and tracing across hops.
- **New failure modes:** queues fill, consumers fall behind, poison messages loop forever, DLQs fill unnoticed.
- **Operational burden:** brokers to run, monitor, and capacity-plan.
- **UX complexity:** “pending” states, notifications, and idempotent user actions.

### 12.2.3 What to make async: critical-path analysis `[MK]`
Ask of each step: *Does the user’s next screen depend on its result? Is it safe to fail and retry later? Can it be eventually consistent?*
| Step | Sync? | Why |
|---|---|---|
| Charge the card | **Sync** (result decides success) — or sync call + async confirmation if provider supports it | The customer needs to know it succeeded |
| Reserve/decrement inventory | **Sync** (the oversell guard, Ch 3) | Correctness on the critical path |
| Persist the order | **Sync** | Source of truth |
| Confirmation email | Async | Delay is invisible |
| PDF invoice | Async | CPU-heavy, not needed immediately |
| Warehouse notification | Async (outbox, retried) | Partner flaky; must eventually succeed |
| Analytics events | Async | Never block users |
| Loyalty points | Async | Eventually consistent is fine |
The synchronous path shrinks from seven steps to three; the other four ride on a durable queue.

---

## 12.3 Core concepts `[MK]`

### 12.3.1 Components
- **Producer:** creates messages. **Consumer/worker:** processes them. **Broker:** stores and delivers messages (the queue).
- **Message:** an immutable record: **ID**, **payload** (small, or a reference to a large blob: claim-check, §12.9), **headers/metadata** (type, version, correlation/trace ID, timestamps, retry count), optionally **key** (for ordering/partitioning).

### 12.3.2 Messaging models
| Model | Semantics | Use |
|---|---|---|
| **Work queue (point-to-point, competing consumers)** | Each message is processed by **one** consumer among many | Distributing tasks (emails, resizing) |
| **Publish/subscribe (topic)** | Each message is delivered to **every** subscriber (each with its own queue/offset) | Events: “OrderPlaced” → email, analytics, fraud |
| **Log-based streaming** (Ch 13) | Append-only partitioned log; consumers track offsets; messages retained and replayable | High-throughput events, multiple independent readers, replay |
| **Request/reply** | Request message carries reply-to + correlation ID | RPC-ish over queues (use sparingly) |
Common combination: **pub/sub fan-out into per-consumer work queues** (e.g., SNS → multiple SQS queues; RabbitMQ exchange → queues).

### 12.3.3 Queue vs log
| | Traditional queue (SQS, RabbitMQ) | Log (Kafka, Pulsar, Kinesis) |
|---|---|---|
| After consumption | Message **deleted** (acked) | Message **retained** until retention expires |
| Replay | No (unless you copy) | Yes: reset the offset |
| Multiple independent consumers | Via fan-out copies | Native: each group has its own offset |
| Ordering | Per queue (FIFO variants) | Per partition |
| Per-message ack/retry/delay | Rich | Offset-based (retry topics for per-message) |
| Throughput | Moderate–high | Very high |
Choose by need: task distribution with per-message retries/delays → queue; event streams with replay and many readers → log (Ch 13).

### 12.3.4 Push vs pull consumers
- **Pull:** consumer asks for messages when ready (SQS long polling, Kafka). Natural backpressure; consumer controls rate.
- **Push:** broker pushes to consumers (RabbitMQ prefetch, Pub/Sub push endpoints). Lower latency but needs **prefetch limits/flow control** to avoid overwhelming slow consumers.

---

## 12.4 Delivery semantics and acknowledgements `[MK]`

### 12.4.1 The crash windows
A consumer’s life per message: **receive → process (side effect) → acknowledge**. A crash can occur at any point:
```text
 receive ──▶ [A] ──▶ do work (side effect happens) ──▶ [B] ──▶ ack ──▶ [C]
   crash at A: work not done, message unacked    → redelivered → done once    (fine)
   crash at B: work DONE, but not acked          → redelivered → done TWICE   (duplicate!)
   crash at C: all done and acked                → fine
 If you ack BEFORE working: crash after ack but before work → message lost (at-most-once).
```

### 12.4.2 The three guarantees
| Semantics | How | Failure behavior | Use when |
|---|---|---|---|
| **At-most-once** | Ack on receipt, then process (or fire-and-forget) | Crash → **message lost**, never duplicated | Loss is acceptable (metrics sampling, best-effort notifications) |
| **At-least-once** | Process, then ack (redeliver on timeout/failure) | Crash between work and ack → **duplicate** | Default for reliable systems: *no loss*, duplicates must be handled |
| **Exactly-once** | See below | Each message’s *effect* happens once | Money, inventory, counters |

### 12.4.3 “Exactly-once” is a property of effects, not delivery `[MK]`
It is impossible for a distributed broker and consumer to guarantee that a message is *delivered* exactly once across crashes and network failures (the Two Generals problem, Ch 14). What you can build is **effectively-once processing**:
> **at-least-once delivery + idempotent processing = exactly-once effect**

Variants: (a) **idempotent consumers** (§12.5); (b) **transactional processing within one system**: Kafka’s transactions atomically commit the consumed offsets and produced output messages (“exactly-once” for Kafka-to-Kafka pipelines) `[Public]`; (c) **single-database atomicity**: write the business effect and the “processed message ID” in the *same DB transaction*.
Treat any vendor claim of “exactly-once” with the question: *within what boundary?* External side effects (emails, payments, HTTP calls) are outside it.

### 12.4.4 Acknowledgement mechanics
- **Ack:** “done, delete it.” **Nack/reject:** “failed; redeliver (maybe to the back, or to the DLQ).”
- **Visibility timeout (SQS-style lease):** when a consumer receives a message, it becomes **invisible** to others for T seconds. If not deleted within T, it reappears and another consumer gets it. Choose T > the processing time distribution’s tail (e.g., p99.9 plus margin), or **extend the lease with heartbeats** for long jobs. Too short → duplicate concurrent processing while the first is still running; too long → slow recovery after a consumer crash.
- **Prefetch / in-flight limit:** how many unacked messages a consumer may hold. High prefetch improves throughput but concentrates work on one consumer and increases redelivery bursts on crash.
- **Batching:** receive/ack in batches for efficiency; partial failures within a batch require per-message handling.

### 12.4.5 Measured: delivery semantics under failures (Lab 1, part A)
100,000 messages; per attempt: 1% consumer dies before working, 2% dies **after the work but before the ack**, 1% is so slow that the visibility timeout expires and a second consumer repeats the work; 0.1% of messages are poison (always fail); max 5 receives, then dead-letter.
```text
at-most-once (ack first, then work)      deliveries= 100000  side-effects applied=  96875  messages with duplicate effects=    0  lost= 3125  in DLQ=  0
at-least-once, plain consumer            deliveries= 103585  side-effects applied= 103042  messages with duplicate effects= 3075  lost=    0  in DLQ=105
at-least-once, idempotent consumer       deliveries= 103484  side-effects applied=  99903  messages with duplicate effects=    0  lost=    0  in DLQ= 97
```
**Read this carefully.** (1) At-most-once **lost 3,125 messages (3.1%)**, silent data loss. (2) At-least-once lost none, but a plain consumer applied the side effect twice for **3,075 messages (3.1%)**: duplicate emails, double charges. (3) An **idempotent** consumer on the same at-least-once delivery applied each effect exactly once (99,903 effects = 100,000 messages minus the 97 poison messages that went to the DLQ). (4) Redeliveries cost only ~3.5% extra deliveries. (5) Poison messages (≈0.1%) don’t block the queue because the DLQ limit parks them after 5 attempts.

---

## 12.5 Idempotent consumers `[MK]`

An operation is **idempotent** if performing it multiple times has the same effect as once. Make the *effect* of processing a message idempotent.

### 12.5.1 Techniques
| Technique | How | Example |
|---|---|---|
| **Naturally idempotent operations** | Use absolute set operations, not relative | `SET status='SHIPPED'`, upsert by key (not `balance = balance + 10`) |
| **Dedup table (inbox pattern)** | Record processed message IDs in the **same transaction** as the effect; unique constraint on message ID | `INSERT INTO processed(msg_id) … ON CONFLICT DO NOTHING`; if 0 rows inserted → skip |
| **Conditional/versioned writes** | Apply only if the current version is older | `UPDATE … WHERE version < :v` |
| **Idempotency keys to external APIs** | Pass the message/order ID as the provider’s idempotency key | Stripe-style `Idempotency-Key`, email provider dedupe |
| **State machines** | Only legal transitions apply; repeated events are no-ops | `PLACED → PAID → SHIPPED` |
| **Deterministic outputs** | Same input → same output key, overwrite is harmless | Thumbnail path derived from image ID/hash |
| **Dedup cache with TTL** | Redis `SET msg:{id} 1 NX EX 604800` | Cheap, but not atomic with the effect → use for best-effort only |
**Atomicity is the crux:** “check processed, do the work, mark processed” as three separate steps leaves windows. Put the effect and the marker in **one transaction** (database effects) or make the effect itself idempotent (external calls with idempotency keys).

### 12.5.2 Example: idempotent order-confirmation worker
```java
void handle(Message m) {
    try (var tx = db.begin()) {
        int inserted = tx.exec("INSERT INTO processed_messages(id) VALUES (?) ON CONFLICT DO NOTHING", m.id());
        if (inserted == 0) { tx.commit(); return; }                         // duplicate: already handled, just ack
        tx.exec("UPDATE orders SET email_status='QUEUED' WHERE id=? AND email_status='NONE'", m.orderId());
        tx.insertOutbox("SendEmail", Map.of("orderId", m.orderId()));       // next step via outbox, same transaction
        tx.commit();
    }
    // ack AFTER the commit; a crash before ack just means redelivery, which the dedup row turns into a no-op
}
```
**Dedup window:** you can’t store IDs forever. Keep them as long as redelivery can plausibly happen (the max of retry horizon, DLQ redrive horizon, and broker retention), e.g., 7–14 days. Size: 10 M messages/day × 14 days × ~60 B/row ≈ 8.4 GB; fine in a table with an index and periodic purge.

### 12.5.3 Idempotency at the producer side
Producers can also create duplicates (retry after timeout). Use **producer idempotency**: client-generated unique message IDs, broker-side dedup windows (SQS FIFO 5-minute dedup; Kafka idempotent producer with sequence numbers), and idempotent API endpoints that create the message (Ch 2).

---

## 12.6 Retries, backoff, and dead-letter queues `[MK]`

### 12.6.1 Classify failures
| Class | Examples | Action |
|---|---|---|
| **Transient** | Timeout, 503, deadlock, connection reset | Retry with backoff |
| **Permanent (poison)** | Malformed payload, schema violation, 400/404 from a downstream that will never succeed, bug on a specific input | Don’t retry endlessly: dead-letter |
| **Dependency down** | Provider outage | Retry with long backoff; circuit-break; pause consumers (don’t burn retries on a known outage) |

### 12.6.2 Retry policy
- **Exponential backoff with jitter:** delay = random(0, min(cap, base × 2^attempt)) (“full jitter”). Jitter prevents synchronized retry waves.
- **Bounded attempts** (e.g., 5–10) and **bounded total time**, then DLQ.
- **Implement delays properly:** delayed queues / per-message visibility timeout increase (SQS `ChangeMessageVisibility`), retry topics with increasing delay tiers (`retry-1m`, `retry-10m`), scheduled messages (RabbitMQ delayed exchange/TTL+DLX), or a scheduler table. Avoid sleeping inside the consumer (it blocks capacity).
- **Do not retry in a tight loop in-process** (hammers a failing dependency and blocks the consumer slot).
- **Retry budgets and circuit breakers:** when a downstream is failing broadly, stop consuming that queue (or slow down) instead of marching every message through 5 failed attempts and into the DLQ.
Arithmetic: base 1 s, ×2, 5 retries ⇒ waits ≈ 1+2+4+8+16 = 31 s worst case (with full jitter ≈ half on average).

### 12.6.3 Dead-letter queues (DLQ)
A DLQ parks messages that exceeded max attempts (or are explicitly rejected) so they don’t block or loop. A DLQ is **only useful if someone looks at it**:
- **Alert** on DLQ depth > 0 (or growth rate) and on age of oldest DLQ message.
- Store **failure context** (exception, attempts, original queue, timestamps, trace ID).
- Provide **inspect, fix, and redrive** tooling (replay to the main queue after the bug/outage is fixed; redrive must be rate-limited and idempotent).
- Distinguish **poison messages** (fix data/code) from **outage victims** (just redrive).
- Retention longer than the main queue so evidence isn’t lost.
At 1 M messages/day with a 0.1% poison rate, expect ~1,000 DLQ messages/day: you need automation and triage rules, not eyeballs.

### 12.6.4 Head-of-line blocking
In strictly ordered queues (FIFO per key), a failing message blocks later messages for that key. Options: bounded retries then park the *key* (not the whole queue), move the poison message to a DLQ and continue (accepting a gap), or process out of order with version checks.

### 12.6.5 Visibility-timeout tuning (worked example)
Processing time: p50 3 s, p99 20 s, p99.9 55 s, occasional 3 min on huge inputs. Set timeout 90–120 s plus a **heartbeat** that extends by 60 s every 30 s while work continues; consumer crash → message reappears within ≤ 60 s; no duplicate concurrent processing for slow-but-alive workers. Always make the handler idempotent anyway.

---

## 12.7 Backpressure, load leveling, and capacity `[MK]`

### 12.7.1 A queue is a buffer, not capacity
If **arrival rate > service rate** for long enough, the queue grows without bound: latency (= depth ÷ service rate) rises, memory/disk fills, and messages become stale. A queue absorbs **bursts**, not **sustained overload**.

**Measured (Lab 1, part B).** Producers send 1,000 msg/s for 60 s then 500 msg/s; 8 consumers × 100 msg/s = 800 msg/s:
```text
unbounded queue, no scaling                                depth at t=60s:  12000  worst message age:  15.0 s  rejected:      0  queue empty again at t=99s
unbounded queue, autoscale to 12 consumers (30 s lag)      depth at t=60s:      0  worst message age:   7.5 s  rejected:      0  queue empty again at t=60s
bounded queue (max 2,000), excess rejected                 depth at t=60s:   1200  worst message age:   1.5 s  rejected:  10800  queue empty again at t=63s
```
**Interpretation.** (1) With no scaling, the 200 msg/s deficit builds a 12,000-message backlog and **15 s of delay** at worst; it drains 39 s after the surge ends. Fine for emails, disastrous for fraud checks. (2) Autoscaling consumers (even with a 30 s reaction lag) cut the worst delay in half and cleared the queue as soon as the surge ended. (3) A **bounded queue with rejection** keeps latency tiny (1.5 s) but **drops 10,800 messages** (the 90% of the deficit). Which is right depends on whether the work is *valuable if late* (keep and scale) or *worthless if late* (shed). **Define the SLO on the age of the oldest message**, not on depth.

### 12.7.2 Little’s Law for sizing `[MK]`
> **Concurrent in-flight = arrival rate × processing time**   (L = λ·W, Ch 5)
- 500 msg/s with 200 ms processing ⇒ 100 concurrent handlers; at 60% target utilization ⇒ ~167 handler slots.
- To drain a backlog *B* while still handling arrivals λ with capacity μ: time = B / (μ − λ).
- Peak vs average: provision for the peak’s *processing* rate or accept a delay equal to (backlog ÷ spare capacity).

### 12.7.3 Mechanisms
| Mechanism | Use |
|---|---|
| **Autoscale consumers** on queue age/depth (KEDA, ASG on SQS metrics) | Burst handling; remember boot lag (Ch 6) |
| **Bound the queue** + reject/slow producers (HTTP 429/503, block, or drop-oldest) | Protect memory and latency; signal upstream |
| **Rate-limit consumers** | Protect a fragile downstream (e.g., partner API at 50 req/s) — queue absorbs the rest |
| **Priority queues** (separate queues per priority, weighted consumption) | Keep critical work fast under load; avoid starvation |
| **Load shedding by age** (drop or divert messages older than X) | Stale work is useless (e.g., “driver nearby” notifications) |
| **Admission control at producers** | Fail fast rather than build hours of backlog |
| **Credit/ prefetch-based flow control** | Keep consumers from over-fetching |
**Anti-pattern:** unbounded queue + no age alerts. The system “works” until the backlog is hours long and everyone is surprised.

---

## 12.8 Ordering `[MK]`

### 12.8.1 What order do you really need?
- **Global total order** across all messages is expensive (a single sequencer) and rarely required.
- **Per-entity order** (all events for order #123, account #7, device #42, conversation #9) is what applications need.

### 12.8.2 Why order breaks
With multiple parallel consumers and retries, message n+1 can finish before message n (different consumers, retry delays, redelivery). Effects applied out of order corrupt state (a `CANCELLED` event processed before `CREATED`).

**Measured (Lab 1, part C).** 8 busy entities × 625 sequential events, randomly interleaved; 4 consumers with 5–50 ms random processing:
```text
shared queue, any consumer takes next message    out-of-order events:  463 of 5000   total time: 34.1 s
partitioned: entity -> fixed consumer (hash)     out-of-order events:    0 of 5000   total time: 34.3 s
```
**Reading:** 9.3% of events were applied out of order with a shared queue; with **key-based partitioning** (each entity always goes to the same consumer, processed sequentially) there were **zero** violations at essentially the same throughput (34.3 s vs 34.1 s).

### 12.8.3 Techniques
| Technique | How | Trade-off |
|---|---|---|
| **Partition by key** | `hash(entity_id)` selects the partition/queue/consumer; each partition processed sequentially | Parallelism limited to #partitions; hot keys serialize; one slow message delays that key |
| **FIFO queues with message groups** (SQS FIFO, Service Bus sessions) | Ordering within a group ID, parallelism across groups | Lower throughput limits; head-of-line blocking per group |
| **Sequence numbers/versions in the payload** | Consumer applies only if `seq == last+1`, buffers or ignores older; `UPDATE … WHERE version < :v` | Application logic; tolerates reordering and duplicates |
| **Single consumer per queue** | Simplest | No parallelism |
| **Design order-insensitive operations** | Commutative/idempotent (set union, max, CRDTs Ch 20) | Not always possible |
| **State machines** | Reject illegal transitions and retry/park | Needs handling of early arrivals |
**Choose the shard key = the ordering key** (Ch 10): messages that must be ordered share a key.

---

## 12.9 Patterns `[MK]`

### 12.9.1 Transactional outbox (producer reliability)
Problem: “update DB **and** publish a message” is a dual write; a crash between them loses the message (or publishes something that never committed).
```text
 BEGIN;
   UPDATE orders SET status='PLACED' WHERE id=…;
   INSERT INTO outbox(id, type, payload, created_at) VALUES (…);       -- same transaction
 COMMIT;
 Relay (poller or CDC/Debezium) ─reads new outbox rows in order─▶ publishes to the broker ─▶ marks sent / deletes
```
Guarantees: if the order committed, the message **will** be published (at-least-once, so duplicates possible → consumers idempotent). Relay options: polling (`SELECT … FOR UPDATE SKIP LOCKED LIMIT 100`), or log-based CDC (lower latency, no polling load). Ordering per aggregate can be preserved by ordering the outbox by sequence and partitioning by aggregate ID.

### 12.9.2 Inbox / idempotent receiver
Persist processed message IDs with the effect (§12.5).

### 12.9.3 Claim-check
Queues are for small messages (KB, with limits like 256 KB–1 MB). For large payloads, store the blob in object storage (Ch 11) and put only its key/URL (+ checksum) in the message. Benefits: smaller broker load, cheaper retries, can re-read.

### 12.9.4 Competing consumers & prefetch tuning
Many workers read the same queue; throughput scales with workers until the broker or downstream limits. Tune prefetch (small for slow/uneven jobs to avoid hoarding; large for tiny jobs for throughput).

### 12.9.5 Fan-out (publish/subscribe) with per-consumer queues
One event (`OrderPlaced`) → topic → each subscriber has its own durable queue (email, analytics, fraud, warehouse). Isolates failures and rates per consumer; adding a subscriber doesn’t affect others. Filtering via routing keys/attributes.

### 12.9.6 Delayed and scheduled messages
“Send reminder in 24 h,” retries with backoff, TTL-based expiry. Implementations: broker-native delay (SQS delay ≤15 min, per-message visibility tricks; RabbitMQ delayed exchange/TTL+DLX; Pub/Sub no native long delay), a **scheduler service** (sorted set in Redis by due time; DB table with `run_at`), or a workflow engine timer (Temporal). Long delays should live in durable storage, not in broker memory.

### 12.9.7 Request/reply and correlation IDs
Reply-to queue + correlation ID to match responses. Prefer plain async events or direct RPC; request/reply over queues adds latency and complexity.

### 12.9.8 Event notification vs event-carried state
- **Notification (thin event):** `OrderPlaced{id}`; consumers call back for details (fresh data, extra load/coupling).
- **Event-carried state transfer (fat event):** includes the data consumers need (no callback, bigger messages, consumers hold copies that may be stale).
Choose per consumer needs; version event schemas (additive changes, schema registry).

### 12.9.9 Sagas (preview)
Multi-step business processes across services as a sequence of local transactions with compensations, coordinated by events (choreography) or an orchestrator (Ch 17).

### 12.9.10 Using the database as a queue (and when it’s fine) `[MK]`
For modest scale (hundreds–low thousands of jobs/s) a Postgres table is a robust, transactional queue with no extra infrastructure:
```sql
CREATE TABLE jobs (
  id bigserial PRIMARY KEY, type text NOT NULL, payload jsonb NOT NULL,
  status text NOT NULL DEFAULT 'queued',      -- queued | running | done | failed
  run_at timestamptz NOT NULL DEFAULT now(), attempts int NOT NULL DEFAULT 0, locked_until timestamptz
);
CREATE INDEX ON jobs (run_at) WHERE status = 'queued';

-- a worker claims a batch without blocking other workers:
WITH next AS (
  SELECT id FROM jobs
  WHERE status = 'queued' AND run_at <= now()
  ORDER BY run_at
  FOR UPDATE SKIP LOCKED LIMIT 10
)
UPDATE jobs SET status='running', attempts = attempts + 1, locked_until = now() + interval '2 minutes'
WHERE id IN (SELECT id FROM next) RETURNING *;
```
Benefits: enqueue in the **same transaction** as business data (outbox for free), simple retry/delay/priority queries, easy inspection. Limits: polling load, vacuum/bloat from high churn, throughput ceiling, no built-in fan-out/backpressure semantics. Libraries: Sidekiq-like (Postgres: GoodJob, Oban, Graphile Worker, pg-boss).

---

## 12.10 Workflows and schedulers `[GK]`

### 12.10.1 Cron and scheduled jobs
Schedulers (cron, Kubernetes CronJob, Quartz) fire periodically. In a multi-instance fleet **ensure only one runs the job**: dedicated scheduler service, leader election/lock (Ch 16), or a database-claimed schedule row. Make jobs **idempotent** and tolerant of missed or double runs; record last successful run; alert on missed runs (“dead-man’s switch”).

### 12.10.2 Workflow orchestration
Multi-step processes with branching, waits (“wait for approval up to 3 days”), retries, and compensations are painful to hand-roll with queues. **Durable execution/workflow engines** persist state after each step and resume after crashes: Temporal/Cadence, AWS Step Functions, Azure Durable Functions, Camunda, Airflow (batch DAGs). You write the workflow as code or state machine; the engine provides retries, timers, history, and visibility. Costs: operational weight and a programming model (determinism rules). Use for long-running, business-critical flows; use plain queues for simple fire-and-forget tasks.

### 12.10.3 Batch vs stream vs queue-driven
| Style | Latency | Example |
|---|---|---|
| Batch (hourly/daily jobs) | Hours | Billing runs, reports |
| Queue-driven jobs | Seconds | Emails, resizes |
| Stream processing (Ch 13) | Sub-second to seconds | Fraud scoring, aggregations |

---

## 12.11 Choosing a broker `[MK]`

| System | Model | Ordering | Replay | Strengths | Watch out |
|---|---|---|---|---|---|
| **Amazon SQS (standard)** | Managed work queue | None (best-effort) | No | Zero ops, huge scale, visibility timeout, DLQ | At-least-once duplicates; no ordering; polling cost |
| **SQS FIFO** | Managed FIFO | Per message group | No | Ordering + dedup | Throughput limits per group/queue |
| **SNS / EventBridge / Pub/Sub** | Managed pub/sub | Optional | Limited | Fan-out, filtering | Per-subscriber queues needed for isolation |
| **RabbitMQ** | Broker with exchanges/queues | Per queue | No (unless streams) | Flexible routing, priorities, TTL/DLX, ack semantics | Operating clusters; unbounded queue memory/disk; throughput lower than logs |
| **Kafka / Pulsar / Kinesis** | Partitioned log | Per partition | **Yes** | Very high throughput, many consumers, replay, stream processing | Operational complexity; per-message retry/delay awkward (retry topics) |
| **Redis Lists/Streams** | In-memory queue/log | Per list/stream | Streams: yes | Very fast, simple | Durability (persistence/replication trade-offs), memory bound |
| **Postgres/MySQL table** | DB queue | By query | N/A | Transactional with data, simple | Throughput ceiling, polling, bloat |
| **NATS / JetStream** | Lightweight messaging (+ persistence) | Per subject/stream | JetStream | Low latency | Smaller ecosystem for some features |
**Selection questions:** required throughput and message size; ordering needs; replay/retention; per-message retries/delays/priorities; fan-out count; delivery guarantees (durability/replication); latency; operational capacity (managed vs self-hosted); cost; ecosystem.

---

## 12.12 Observability and operations `[MK]`

Monitor per queue/consumer group:
- **Age of oldest message** (the primary SLI: “emails are at most 2 minutes late”), **queue depth** (and its derivative: growing/shrinking), enqueue vs dequeue rate.
- **Processing latency** (p50/p99), **success/failure rate**, **retry/redelivery count** and attempt histogram, **DLQ depth/age**.
- **Consumer count and utilization**, in-flight/unacked messages, visibility-timeout expirations (indicator of slow handlers or too-short timeouts).
- **Lag** (log systems), partition skew.
- **Tracing:** propagate trace/correlation IDs in message headers so a request’s async tail appears in the same trace.
- **Runbooks:** pause/resume consumers, redrive DLQ, purge queue (dangerous), scale consumers, replay.
- **Capacity tests** with realistic message sizes and consumer failure injection.

---

## 12.13 Failure modes

| Failure | Mechanism | Mitigation |
|---|---|---|
| **Duplicate side effects** | Crash after work before ack; visibility timeout expiry; producer retries | Idempotent consumers, dedup keys, idempotent producers |
| **Lost messages** | Ack-before-work; broker without durability; non-transactional dual write | Ack after work; durable queues with replication; outbox |
| **Poison message loop** | Always-failing message redelivered forever | Max receive count, DLQ, validation, per-key parking |
| **Unbounded backlog** | Arrival > service for long | Autoscale, bound queue, shed load, age alerts |
| **Retry storm** | Synchronized retries on dependency recovery | Backoff + jitter, retry budgets, circuit breaker, slow ramp |
| **Visibility timeout too short** | Slow handler exceeds lease | Heartbeats, p99.9-based timeout, idempotency |
| **Out-of-order effects** | Parallel consumers, retries | Partition by key, sequence/version checks, state machines |
| **Hot key / hot partition** | Skewed keys | Split keys, bounded concurrency per key, dedicated consumers |
| **Head-of-line blocking** | Failing message blocks key or queue | Park the key, DLQ, bounded retries |
| **DLQ ignored** | No alerting | Alerts, dashboards, redrive tooling |
| **Large messages** | Exceed limits, slow broker | Claim-check pattern |
| **Schema break** | Producer changes event format | Versioned schemas, compatibility checks, tolerant readers |
| **Broker outage** | Single broker/cluster failure | Replicated cluster, multi-AZ, producer buffering/outbox, circuit breaker |
| **Consumer starvation by one tenant** | Noisy neighbor floods shared queue | Per-tenant queues or fair scheduling, quotas |
| **Stale work processed** | Backlog processed after relevance expired | TTL on messages, age checks |
| **Hidden coupling / saga gaps** | Missing compensation after partial failure | Explicit workflows, reconciliation jobs |
| **Clock-based scheduling errors** | Timezone/DST, drift, double fire | UTC, idempotent jobs, single scheduler |

---

## 12.14 Labs

### Lab 1: Delivery semantics, backpressure, and ordering (Java 17+)
```java
import java.util.*;

public class QueueLab {
    // ------------------------------------------------------------------ A) delivery semantics
    static void deliverySemantics() {
        int messages = 100_000; Random r = new Random(9);
        double pCrashBefore = 0.01, pCrashAfterEffect = 0.02, pSlow = 0.01, pPoison = 0.001; int maxReceive = 5;
        System.out.println("== A) Delivery semantics. 100,000 messages; per attempt: 1% consumer dies before work, 2% dies AFTER doing the work but BEFORE ack,");
        System.out.println("      1% is so slow the visibility timeout expires (message handed to a 2nd consumer), 0.1% of messages are poison (always throw). ==");
        for (String mode : new String[]{"at-most-once (ack first, then work)", "at-least-once, plain consumer", "at-least-once, idempotent consumer"}) {
            boolean ackFirst = mode.startsWith("at-most"), idempotent = mode.contains("idempotent");
            long deliveries = 0, effects = 0, lost = 0, dlq = 0, duplicates = 0;
            for (int m = 0; m < messages; m++) {
                boolean poison = r.nextDouble() < pPoison; int effectsForThis = 0; boolean done = false; int receives = 0;
                while (!done) {
                    if (receives == maxReceive) { dlq++; break; }                 // parked in the dead-letter queue after N failed receives
                    receives++; deliveries++;
                    if (ackFirst) {                                                // acknowledged on receipt: never redelivered
                        if (poison || r.nextDouble() < pCrashBefore + pCrashAfterEffect) { lost++; } else effects++;
                        done = true; continue;
                    }
                    if (poison) continue;                                          // throws -> no ack -> redelivered later
                    double x = r.nextDouble();
                    if (x < pCrashBefore) continue;                                // died before work: retry
                    boolean applyEffect = !(idempotent && effectsForThis > 0);     // idempotent consumer checks "already processed?"
                    if (applyEffect) { effects++; effectsForThis++; }
                    if (x < pCrashBefore + pCrashAfterEffect) continue;            // did the work, died before ack: redelivered
                    if (x < pCrashBefore + pCrashAfterEffect + pSlow) { if (!idempotent) { effects++; effectsForThis++; } done = true; continue; }   // 2nd consumer repeats work
                    done = true;
                }
                if (effectsForThis > 1 || (!idempotent && effectsForThis > 1)) duplicates++;
            }
            System.out.printf("%-40s deliveries=%7d  side-effects applied=%7d  messages with duplicate effects=%5d  lost=%5d  in DLQ=%3d%n", mode, deliveries, effects, duplicates, lost, dlq);
        }
    }

    // ------------------------------------------------------------------ B) backpressure
    static void backpressure() {
        System.out.println("\n== B) Backpressure. Producers send 1,000 msg/s for 60 s then 500 msg/s. 8 consumers x 100 msg/s = 800 msg/s. ==");
        for (String policy : new String[]{"unbounded queue, no scaling", "unbounded queue, autoscale to 12 consumers (30 s lag)", "bounded queue (max 2,000), excess rejected"}) {
            double depth = 0; int consumers = 8; double rejected = 0, worstAge = 0, depthAt60 = 0; int drainedAt = -1;
            for (int t = 0; t < 400; t++) {
                double arrivals = t < 60 ? 1000 : 500;
                if (policy.contains("autoscale") && t == 30) consumers = 12;                        // decision at ~t=0, effective after 30 s
                if (policy.startsWith("bounded") && depth + arrivals > 2000) { rejected += depth + arrivals - 2000; arrivals = 2000 - depth; }
                depth += arrivals; depth -= Math.min(depth, consumers * 100.0);
                worstAge = Math.max(worstAge, depth / (consumers * 100.0));
                if (t == 59) depthAt60 = depth;
                if (drainedAt < 0 && t >= 60 && depth == 0) drainedAt = t;
            }
            System.out.printf("%-58s depth at t=60s: %6.0f  worst message age: %5.1f s  rejected: %6.0f  queue empty again at t=%s%n",
                policy, depthAt60, worstAge, rejected, drainedAt < 0 ? "never(<400s)" : drainedAt + "s");
        }
    }

    // ------------------------------------------------------------------ C) ordering
    static void ordering() {
        System.out.println("\n== C) Ordering. 8 busy entities x 625 sequential events each, randomly interleaved; 4 consumers, random 5-50 ms processing. Count events applied OUT OF ORDER. ==");
        int entities = 8, perEntity = 625, consumers = 4;
        for (String mode : new String[]{"shared queue, any consumer takes next message", "partitioned: entity -> fixed consumer (hash)"}) {
            Random r = new Random(4);
            List<int[]> msgs = new ArrayList<>(); int[] next = new int[entities]; int remaining = entities * perEntity;
            while (remaining > 0) { int e = r.nextInt(entities); if (next[e] < perEntity) { msgs.add(new int[]{e, next[e]++}); remaining--; } }   // random merge keeps per-entity order
            double[] freeAt = new double[consumers]; List<double[]> completions = new ArrayList<>();   // {finishTime, entity, seq}
            for (int[] m : msgs) {
                int c;
                if (mode.startsWith("shared")) { c = 0; for (int i = 1; i < consumers; i++) if (freeAt[i] < freeAt[c]) c = i; }   // next free consumer
                else c = m[0] % consumers;                                                                                        // sticky lane per entity
                double fin = freeAt[c] + 5 + r.nextDouble() * 45; freeAt[c] = fin; completions.add(new double[]{fin, m[0], m[1]});
            }
            completions.sort(Comparator.comparingDouble(x -> x[0]));
            int[] last = new int[entities]; Arrays.fill(last, -1); int outOfOrder = 0;
            for (double[] c : completions) { int e = (int) c[1], q = (int) c[2]; if (q < last[e]) outOfOrder++; last[e] = Math.max(last[e], q); }
            double makespan = Arrays.stream(freeAt).max().getAsDouble() / 1000;
            System.out.printf("%-48s out-of-order events: %4d of %d   total time: %.1f s%n", mode, outOfOrder, msgs.size(), makespan);
        }
    }

    public static void main(String[] a) { deliverySemantics(); backpressure(); ordering(); }
}
```
**Sample output**
```text
== A) Delivery semantics. 100,000 messages; per attempt: 1% consumer dies before work, 2% dies AFTER doing the work but BEFORE ack,
      1% is so slow the visibility timeout expires (message handed to a 2nd consumer), 0.1% of messages are poison (always throw). ==
at-most-once (ack first, then work)      deliveries= 100000  side-effects applied=  96875  messages with duplicate effects=    0  lost= 3125  in DLQ=  0
at-least-once, plain consumer            deliveries= 103585  side-effects applied= 103042  messages with duplicate effects= 3075  lost=    0  in DLQ=105
at-least-once, idempotent consumer       deliveries= 103484  side-effects applied=  99903  messages with duplicate effects=    0  lost=    0  in DLQ= 97

== B) Backpressure. Producers send 1,000 msg/s for 60 s then 500 msg/s. 8 consumers x 100 msg/s = 800 msg/s. ==
unbounded queue, no scaling                                depth at t=60s:  12000  worst message age:  15.0 s  rejected:      0  queue empty again at t=99s
unbounded queue, autoscale to 12 consumers (30 s lag)      depth at t=60s:      0  worst message age:   7.5 s  rejected:      0  queue empty again at t=60s
bounded queue (max 2,000), excess rejected                 depth at t=60s:   1200  worst message age:   1.5 s  rejected:  10800  queue empty again at t=63s

== C) Ordering. 8 busy entities x 625 sequential events each, randomly interleaved; 4 consumers, random 5-50 ms processing. Count events applied OUT OF ORDER. ==
shared queue, any consumer takes next message    out-of-order events:  463 of 5000   total time: 34.1 s
partitioned: entity -> fixed consumer (hash)     out-of-order events:    0 of 5000   total time: 34.3 s
```
**Experiments**
1. In part A set `pCrashAfterEffect` to 0 and `pSlow` to 0. What happens to duplicates under at-least-once? Which failure creates duplicates, and which does not?
2. Make the poison rate 2% and `maxReceive` = 3, then 10. How do deliveries, DLQ size, and wasted work change? What is the cost of a high retry limit when messages are truly poison?
3. Add **exponential backoff with jitter** to the simulation by tracking time; show the retry-wave when 10,000 messages fail at once and retry immediately vs with full jitter (plot retries per second).
4. In part B, add **priority queues**: 10% of messages are “critical.” Compare the critical-message age with a single FIFO queue and with two queues (critical gets 40% of consumer capacity).
5. In part B, drop messages whose age exceeds 5 s (age-based shedding). How many are dropped and what happens to the latency of surviving messages?
6. In part C, change to **16 entities** and **hot entity** (one entity has 50% of messages). What happens to the partitioned throughput? How would you mitigate it (finer-grained ordering keys, per-key concurrency with sequence checking)?

### Lab 2: Postgres as a job queue with `SKIP LOCKED` (needs Postgres; not executed in the book’s sandbox)
Use the schema and claim query from §12.9.10. Run two or more worker processes (e.g., two `psql` sessions or a small Python/Java worker). **Questions:** What happens if two workers run the claim query simultaneously? What does `SKIP LOCKED` do vs a plain `FOR UPDATE`? How do you recover jobs whose worker died (`locked_until` expiry sweep)? How would you implement exponential backoff (update `run_at` on failure) and a DLQ (status `dead` after N attempts)? Measure throughput with 1, 4, 16 workers and observe table bloat/VACUUM behavior after 1 M jobs.

### Lab 3: Design the async checkout (paper)
Redesign the OrderFlow checkout: list the synchronous steps, the events emitted, the queues and consumers (with retry/DLQ policy and idempotency strategy for each), the outbox, and the user-visible states (“Order placed”, “Confirmation email pending”…). Specify what happens when (a) the email provider is down for 2 hours, (b) the warehouse API rejects an order permanently, (c) the same checkout request arrives twice, (d) a worker dies after sending an email but before acking.

---

## 12.15 Interview perspective

- State **what is synchronous and why** (the minimal critical path), then **what is async and via which queue**.
- Use the phrase: **“at-least-once delivery with idempotent consumers”**, and explain the crash window that creates duplicates.
- Mention **outbox** for reliable publishing, **DLQ with alerting and redrive**, **backoff with jitter**, and **visibility timeout/heartbeat**.
- Discuss **backpressure**: queue depth/age as metrics, autoscaling consumers, bounded queues, load shedding, and *queues don’t fix sustained overload*.
- Handle **ordering** by partitioning on the entity key; call out hot keys.
- Choose a broker with a reason (SQS for simple managed work queues; Kafka for streams/replay; RabbitMQ for flexible routing; DB queue for small scale with transactions).
- Close with **observability**: age of oldest message, DLQ depth, redelivery rate, trace propagation.

---

## 12.16 Common misconceptions

1. **“Queues make the system faster.”** They make *requests* faster by deferring work; total work is unchanged (plus overhead).
2. **“Exactly-once delivery exists.”** Effects can be exactly-once; delivery can’t be, across failures.
3. **“Acknowledge on receipt for safety.”** That’s at-most-once and loses messages on crashes.
4. **“A queue will absorb any load.”** Only bursts; sustained overload grows the backlog without bound.
5. **“A DLQ solves poison messages.”** Only if it is monitored, triaged, and redriven.
6. **“Ordering is free in a queue.”** Parallel consumers and retries break it; use keyed partitioning.
7. **“Retries are harmless.”** They multiply load and create duplicates without idempotency and backoff.
8. **“Pub/sub and queues are the same.”** Pub/sub delivers to all subscribers; work queues to one consumer.
9. **“Kafka replaces all queues.”** Different strengths: per-message delay/priorities/ack semantics fit queues.
10. **“Just publish to the broker after the DB commit.”** The crash between them loses the message; use an outbox.
11. **“Bigger visibility timeout is always safer.”** It delays recovery; use heartbeats.
12. **“Queue depth is the metric.”** Depth without rate and age is misleading; alert on message age.

---

## 12.17 Chapter summary

- **Async processing** shortens the critical path, isolates failures, levels load, and scales independently, at the price of eventual consistency, duplicates, reordering, and operational complexity. Decide by critical-path analysis.
- **Models:** work queues (one consumer), pub/sub (all subscribers), logs (retained, replayable; Ch 13). Combine fan-out with per-consumer queues.
- **Delivery semantics:** ack-first = at-most-once (**3.1% lost** in the lab); work-then-ack = at-least-once (**3.1% duplicated effects**); **idempotent consumers** make the effect exactly-once (0 duplicates, 0 lost). “Exactly-once” is about effects within a boundary.
- **Idempotency** via natural idempotent ops, dedup tables updated in the **same transaction** as the effect, conditional writes, and provider idempotency keys.
- **Retries:** classify failures; exponential backoff with jitter; bounded attempts; **DLQ with alerts and redrive**; tune visibility timeouts and use heartbeats.
- **Backpressure:** queues buffer bursts, not sustained overload; measure **age of oldest message**; autoscale consumers, bound queues, shed stale work; size with Little’s Law (L = λW).
- **Ordering:** only per-key order is practical; **partition by key** (463 of 5,000 out-of-order → 0, same throughput); use sequence numbers/state machines; beware hot keys and head-of-line blocking.
- **Patterns:** transactional **outbox**, inbox, claim-check, competing consumers, delayed/scheduled messages, DB-as-queue with `SKIP LOCKED`, workflow engines for long-running processes.
- **Choose brokers** by throughput, ordering, replay, routing, ops burden; observe age, depth, redeliveries, DLQ.

### Mental models
| Concept | Mental model |
|---|---|
| Queue | Restaurant ticket rail between waiters and cooks |
| Visibility timeout | Borrowing a ticket for 2 minutes; if you don’t return it stamped, someone else takes it |
| At-least-once + idempotent | A stamp that says “already done”; stamping twice does no harm |
| DLQ | A tray for tickets nobody can cook, that the manager must review |
| Backpressure | A full ticket rail telling the waiters to slow down |
| Partition by key | Each regular customer always served by the same waiter, so their orders stay in order |
| Outbox | Writing the delivery note in the same ledger entry as the sale |

---

## 12.18 Exercises

### A. Conceptual
1. Give three benefits and three costs of making a step asynchronous. Apply critical-path analysis to “user signs up” (create account, send verification email, create default workspace, notify sales, update analytics).
2. Compare work queues, pub/sub, and logs. When is each right?
3. List the three crash windows in “receive → work → ack” and what each causes under at-most-once and at-least-once.
4. Explain why exactly-once *delivery* is impossible but exactly-once *effects* are achievable. State the formula.
5. What is a visibility timeout? What goes wrong if it is too short or too long? How do heartbeats help?
6. Design an idempotent handler for “charge customer for order 123” using a payment provider that supports idempotency keys.
7. Why must the dedup marker and the effect be in the same transaction? Show the failure if they aren’t.
8. Explain exponential backoff with full jitter. Why is jitter necessary?
9. Classify: JSON parse error; 503 from provider; deadlock; 404 on a resource that will never exist; provider outage for 30 minutes. For each, retry/DLQ/pause?
10. What makes a DLQ useful vs a graveyard? List five required capabilities.
11. Explain head-of-line blocking in an ordered queue and three ways to handle it.
12. Why does a queue not fix sustained overload? What metric would you alert on and why age rather than depth?
13. Describe four backpressure mechanisms and when to use each.
14. What is the outbox pattern and what failure does it prevent? How do you relay safely?
15. What is the claim-check pattern and when do you use it?
16. How would you implement “send a reminder in 24 hours” reliably?
17. Compare SQS standard, SQS FIFO, RabbitMQ, and Kafka on ordering, replay, and typical use.
18. When is a database table a perfectly good queue, and what are its limits?
19. When do you need a workflow engine instead of plain queues?
20. How do you keep per-entity order while scaling to many consumers?

### B. Calculations
21. 800 msg/s arrive; each takes 150 ms. How many concurrent handlers are needed? How many at 65% target utilization? If each worker handles 10 concurrent messages, how many workers?
22. Arrival 1,500 msg/s for 120 s; capacity 1,100 msg/s; then arrival 600 msg/s. Compute the peak backlog, the maximum message age, and the time to drain after the surge.
23. Retry schedule base 2 s, factor 2, max 6 retries, cap 60 s. List the delays; total worst-case time; expected total with full jitter. How many total attempts (including the first)?
24. 5 M messages/day; poison rate 0.05%; transient failure rate 2% (each retried, resolved on first retry with 90%). Expected DLQ messages/day? Expected extra deliveries/day?
25. Visibility timeout sizing: handler durations log-normal with p50 4 s, p99 30 s, p99.9 90 s. Choose a timeout and heartbeat interval; justify. What duplicate rate would a 30 s timeout cause (≈1% of messages)?
26. Dedup storage: 20 M messages/day, window 10 days, 70 bytes per row including index. Compute the size. What eviction method do you use?
27. An outbox poller runs every 500 ms with batch 200. What is the maximum sustainable publish rate? Added latency (average, worst)? How does CDC change this?
28. Partitioning: 6 consumers, 6 partitions; one key accounts for 30% of messages and each message takes 20 ms. Maximum overall throughput if that key must be processed sequentially? What parallelism limit does the hot key impose?
29. A downstream partner allows 40 requests/s. Messages arrive at an average of 25/s with bursts to 200/s lasting 20 s every 5 minutes. Queue size needed during a burst? Time to drain? Is the average rate sustainable?
30. At-least-once with duplicates at 3% of messages and an email provider without idempotency keys: with 1 M emails/day how many duplicate emails? What dedup mechanism reduces it to ~0?

### C. Design
31. Redesign OrderFlow’s checkout (Lab 3). Include outbox, queues per consumer, idempotency approach, retry/DLQ policy, and user-visible states.
32. Design a notification system (email/SMS/push) handling 50 M notifications/day with per-user rate limits, priorities (OTP codes vs marketing), provider failover, and deduplication.
33. Design an image-processing pipeline from upload events to derived images (Ch 11), with at-least-once events, idempotent workers, DLQ, and reconciliation.
34. Design a job scheduler for “run X at time T” for 10 M scheduled jobs (reminders) with exactly-once-effect and horizontal scaling.
35. Design a webhook delivery system: customers’ endpoints are slow or down; need retries with backoff for 24 h, ordering per customer endpoint, rate limiting per endpoint, and visibility into failures.
36. Design a multi-tenant queue strategy so one tenant’s flood doesn’t starve others.

### D. Debugging
37. Customers receive two confirmation emails about 1 in 30 times. Give four possible causes and the fix for each.
38. The queue depth is flat at ~0, but customers report emails take 40 minutes. What could be happening? (Hint: age, DLQ, consumer prefetch.)
39. After a deploy, all consumers keep crashing on one message, and the queue stops draining. What mechanisms limit the damage, and what should you do now?
40. Autoscaling consumers from 10 to 100 made the database fall over. Explain and fix.
41. A worker that “sometimes” processes jobs twice concurrently corrupts totals. Diagnose (visibility timeout, lack of locking) and fix.
42. Orders sometimes appear in the warehouse system without a payment record. What dual-write bug is likely, and how do you fix it?

### E. Interview-style
43. “Design a system to send millions of emails reliably.”
44. “How do you guarantee a message is processed exactly once?”
45. “Walk me through what happens when a consumer crashes mid-processing.”

### F. Teach-back
46. Explain at-least-once delivery plus idempotency to a non-engineer using a pizza-order slip analogy.

---

## 12.19 Full answer key

**1.** Benefits: lower latency, isolation, load leveling, independent scaling, retries. Costs: eventual consistency, duplicates/reordering, harder debugging, operational burden. Sign-up: create account = sync; verification email = async (but the user needs it soon: high priority); default workspace = sync or fast async (depends if needed immediately); notify sales, analytics = async.

**2.** Work queue: one consumer per message (task distribution). Pub/sub: all subscribers get each message (events). Log: retained, replayable, multiple independent readers, very high throughput (streams, audit, event sourcing).

**3.** A (crash before work): not done; at-least-once redelivers → done once; at-most-once (acked on receipt) → lost. B (after work, before ack): at-least-once → duplicate; at-most-once n/a if already acked. C (after ack): fine in both.

**4.** Networks and crashes make it impossible for sender and receiver to agree whether the last message/ack was received (Two Generals), so the sender must retry (→ duplicates) or give up (→ loss). With at-least-once delivery and idempotent processing, repeated deliveries have no additional effect: effectively exactly-once.

**5.** The lease during which a received message is hidden from others. Too short: a slow handler’s message reappears and runs concurrently (duplicates). Too long: after a crash, recovery is delayed. Heartbeats extend the lease while work continues, so the base timeout can be short.

**6.** Use order ID as the provider’s idempotency key; store `(order_id, charge_id, status)` with a unique constraint; on redelivery, look up/insert first; call the provider with the same key; persist result; ack. A repeated call returns the original charge.

**7.** If they’re separate: crash after the effect but before marking → duplicate on redelivery; crash after marking but before the effect → effect never happens (lost). A single transaction makes both happen or neither.

**8.** Delay = random(0, min(cap, base·2^n)). Exponential growth reduces load over time; jitter spreads retries so many clients don’t retry at the same instants (thundering herd).

**9.** JSON parse error: permanent → DLQ. 503: transient → backoff retry. Deadlock: transient → immediate/short retry. 404 never-existing: permanent → DLQ or discard with log. 30-minute outage: pause/slow consumption (circuit breaker) and retry later, don’t exhaust retry counts.

**10.** Alerts on depth/age; failure context stored; inspection UI; fix-and-redrive tooling (rate-limited, idempotent); retention longer than main queue; ownership/runbook.

**11.** One failing message blocks later messages for the same key/queue. Handling: bounded retries then DLQ and continue (accept gap), park only that key, or process out of order with version checks.

**12.** If arrival exceeds service capacity persistently, the backlog grows without bound. Alert on the age of the oldest message (it expresses user-visible delay regardless of rate); depth alone depends on throughput (1,000 messages may be 1 s or 1 hour).

**13.** Autoscaling consumers (bursts); bounded queue with rejection/429 (protect memory, signal producers); consumer rate limits (protect fragile downstreams); priority queues (preserve critical work); age-based shedding (stale work is worthless); admission control at producers.

**14.** Write the state change and an outbox row in one DB transaction; a relay publishes outbox rows to the broker and marks them sent. Prevents lost or phantom messages from dual writes. Relay safely using `FOR UPDATE SKIP LOCKED` batches or CDC, publish with idempotent message IDs, and retry; consumers dedupe.

**15.** Store large payload in object storage and put a reference in the message; use when payloads exceed broker limits or are large enough that retries/copies are costly.

**16.** Persist `(job_id, run_at)` in a durable scheduler store (DB table or sorted set); pollers claim due jobs (`SKIP LOCKED`) and enqueue or run them; idempotent execution; or use a workflow engine timer or broker with long delay support.

**17.** SQS standard: no ordering, at-least-once, simple managed work queue. SQS FIFO: ordering per message group, dedup, lower throughput. RabbitMQ: flexible routing, per-queue order, acks/priorities/TTL, no replay. Kafka: ordered per partition, retention + replay, very high throughput, multiple consumer groups.

**18.** When throughput is modest, you want transactional enqueue with business data, and ops simplicity. Limits: polling load, bloat/vacuum, lower throughput ceiling, fewer broker features (fan-out, flow control).

**19.** For long-running, multi-step processes with waits, branching, compensation, human approval, or strict visibility/audit needs, where hand-written queue state machines become error-prone.

**20.** Partition by entity key so each key maps to one consumer/partition processed sequentially; parallelism across keys; handle hot keys via sub-key ordering or sequence-based reordering.

**21.** In-flight = 800 × 0.150 = **120**. At 65%: 120/0.65 ≈ **185** slots. At 10 concurrent per worker → **19 workers**.

**22.** Backlog grows (1,500 − 1,100) = 400/s × 120 s = **48,000** peak. Max age ≈ 48,000/1,100 ≈ **43.6 s**. After the surge: spare capacity 1,100 − 600 = 500/s → 48,000/500 = **96 s** to drain.

**23.** Delays: 2, 4, 8, 16, 32, 60 (cap; 64→60) s. Worst-case total = 2+4+8+16+32+60 = **122 s**; with full jitter expected ≈ **61 s**. Total attempts = 1 + 6 = **7**.

**24.** DLQ ≈ 5 M × 0.0005 = **2,500/day** (poison). Transient: 2% × 5 M = 100,000 first failures; each retried once → 100,000 extra deliveries; 10% of them fail again → another 10,000 (assuming one more retry) → ≈ **110,000** extra transient deliveries, plus poison: 2,500 × (max receives − 1) e.g., 4 → 10,000. Total ≈ **120,000** extra deliveries/day (~2.4%).

**25.** Timeout ≈ 2–3 × p99.9 = ~180 s is conservative; better: base timeout 60 s with heartbeat every 20 s extending by 60 s while running; a crash causes redelivery within ≤ 60 s. With a fixed 30 s timeout about 1% of messages (p99) exceed it → ~1% concurrent duplicates.

**26.** 20 M × 10 = 200 M rows × 70 B = **14 GB**. Evict by time-partitioned tables (drop the oldest partition daily) or TTL; avoid row-by-row deletes.

**27.** Max rate = 200 / 0.5 s = **400 msg/s** (or more if batch loops continue immediately when full). Added latency: average ≈ 250 ms, worst ≈ 500 ms (+ processing). CDC streams changes within tens of ms and scales with the log, removing polling limits.

**28.** The hot key (30% of messages) must be sequential: 20 ms each → max 50 msg/s for that key; since it’s 30% of the load, total throughput ≤ 50/0.30 ≈ **167 msg/s**, versus the 6 consumers’ nominal 6 × 50 = 300 msg/s. The hot key caps overall throughput at ~56% of capacity.

**29.** If 25/s is the long-run average including bursts, the burst traffic contributes 200 × 20 / 300 ≈ 13.3/s, so the baseline between bursts is (25 × 300 − 4,000) / 280 ≈ 12.5/s. Average 25/s < 40/s service rate, so it is sustainable. During a burst the queue grows by (200 − 40) = 160/s × 20 s = **3,200 messages**; the oldest message waits ≈ 3,200 / 40 = **80 s** at the end of the burst. After the burst the spare capacity is 40 − 12.5 = 27.5/s, so the backlog drains in ≈ 3,200 / 27.5 ≈ **116 s**, well inside the 280 s gap. Size the queue for ≥ 4,000 messages and alert when message age exceeds ~2 minutes.

**30.** 3% × 1 M = **30,000 duplicate emails/day**. Add a dedup table keyed by `(notification_id)` updated in the same transaction as the “sent” record, plus provider idempotency keys if available; reduce to ~0 (remaining window: crash after provider accepted but before recording; use provider-level dedupe to close).

**31 (sketch).** Sync: validate cart, reserve inventory, charge (or authorize) with idempotency key, persist order + outbox, return 201. Outbox → `OrderPlaced` topic → queues: email (idempotent by order ID, retry 24 h, DLQ), invoice (claim-check PDF to object store), warehouse (retry with backoff; permanent rejections → manual review queue), analytics (at-most-once ok), loyalty (idempotent upsert by order ID). States: PLACED → PAID → FULFILLMENT_PENDING; email/invoice statuses separate.

**32 (sketch).** Ingest API → priority topics (OTP, transactional, marketing); per-user rate limiter (Redis); dedup by notification ID; provider adapters with circuit breakers and failover; retry with backoff by provider error class; DLQ; delivery receipts update status; scheduling windows for marketing; per-tenant quotas.

**33 (sketch).** ObjectCreated event → queue; idempotent worker keyed by `(object key, version/etag)` writes derived objects deterministically; DLQ for corrupt files; reconciliation job lists objects without READY status for > N minutes and re-enqueues.

**34 (sketch).** Store jobs in a partitioned table/sorted set keyed by `run_at` bucket; time-bucket pollers (per shard) claim due jobs (`SKIP LOCKED`), enqueue execution messages with job ID as idempotency key; workers run idempotently; mark done; sweep stuck jobs; shard by job ID hash for horizontal scaling.

**35 (sketch).** Per-endpoint queue (partition key = endpoint ID) for order; delivery workers with per-endpoint concurrency=1 and rate limits; exponential backoff for 24 h then disable endpoint and notify; HMAC signatures; delivery logs and replay UI; circuit breaker per endpoint.

**36 (sketch).** Per-tenant (or per-tier) queues with weighted fair consumption; per-tenant concurrency and rate limits; shuffle-sharding tenants across consumer pools; quotas and 429 at ingestion; isolate large tenants to dedicated queues.

**37.** Causes: crash after sending, before ack (fix: idempotent send with provider idempotency key / dedup table); visibility timeout shorter than processing (heartbeat/longer timeout); producer retries creating duplicate messages (idempotent producer / dedupe on order ID); multiple subscriptions delivering the same event to two services that both send emails (single owner). 

**38.** Messages are delayed in a different place: consumers hold many prefetched unacked messages (invisible, so depth looks small); or the oldest messages are in a retry/delay queue or DLQ; or very few consumers process slowly while the queue is polled by a different metric; or an upstream queue/outbox relay is lagging. Check age of oldest, in-flight count, DLQ and retry queues, outbox lag.

**39.** Poison message: crashes before ack; redelivered repeatedly. Limits: max receive count → DLQ; per-message timeout; crash-loop backoff. Now: identify the message (logs), move it to DLQ manually, fix the bug/validation, redeploy, then redrive; add schema validation and catch/ack-with-error handling.

**40.** Consumers scaled but the DB connection pool/capacity was not; 100 workers overwhelmed it. Fix: cap consumer concurrency by downstream capacity (rate limit/bulkhead), connection pooler, batch writes, scale DB, and autoscale based on both queue age and downstream health.

**41.** Visibility timeout shorter than processing time → two workers process the same job concurrently; handler isn’t idempotent/locked. Fix: longer timeout + heartbeat, idempotent updates, row-level locking/version checks, unique constraints.

**42.** Dual write: the app wrote to the warehouse integration (or published the message) before the payment transaction committed (or the payment failed after). Fix: outbox—publish only after commit, drive warehouse notification from committed state, idempotent consumers.

**43 (sketch).** Accept requests via API → queue per priority; workers call providers with rate limits and per-domain throttles; idempotency keys; retries with backoff; bounce/complaint handling; DLQ; provider failover; dedupe; templates/personalization; observability (age, delivery rates); compliance (unsubscribe).

**44 (sketch).** Explain effects vs delivery; at-least-once + idempotent consumers; outbox; dedup table in same transaction; idempotency keys with external systems; within Kafka transactions for Kafka-to-Kafka.

**45 (sketch).** Message unacked → visibility timeout expires → redelivered to another consumer; partial effects may exist; idempotency handles duplicates; heartbeats; if repeated crashes → DLQ after max receives.

**46.** A restaurant writes each order on a numbered slip. If the cook is unsure whether he already made slip #57 (maybe he got interrupted), he checks the “done” spike: if #57 is there, he skips it. Slips may be handed over twice, but a pizza is only made once.

---

## 12.20 Flashcards

| Front | Back |
|---|---|
| Why async | Latency, isolation, load leveling, scaling, retries |
| Work queue vs pub/sub | One consumer per message vs all subscribers |
| Queue vs log | Delete-on-ack vs retained/replayable |
| At-most-once | Ack first; may lose (3.1% lost in lab) |
| At-least-once | Work then ack; may duplicate (3.1% duplicate effects in lab) |
| Exactly-once effect | At-least-once + idempotent processing |
| Visibility timeout | Lease hiding a message while processing; extend with heartbeat |
| Inbox / dedup table | Message ID + effect in one transaction |
| Backoff | Exponential + full jitter; bounded attempts |
| DLQ | Parked failures; needs alerts, context, redrive |
| Poison message | Always fails; limit receives |
| Backpressure | Bound queues, autoscale, shed, rate-limit consumers |
| Key SLI | Age of oldest message |
| Little’s Law | In-flight = rate × time |
| Ordering fix | Partition by entity key (463/5000 → 0 out of order) |
| Outbox | State change + message row in one transaction; relay publishes |
| Claim-check | Blob in object store, reference in message |
| Delayed jobs | Scheduler store + pollers, not sleeping workers |
| DB as queue | `FOR UPDATE SKIP LOCKED`; fine at modest scale |
| Workflow engine | Durable execution for long multi-step processes |
| Tracing | Propagate trace/correlation IDs in headers |

## 12.21 Glossary additions

| Term | Simple Meaning | Technical Meaning | Why It Exists | Example |
|---|---|---|---|---|
| Queue | Waiting line of tasks | Broker-held ordered buffer of messages | Decouple in time | SQS |
| Producer / consumer | Sender / worker | Components that enqueue / process messages | Roles | Checkout / email worker |
| Broker | Message hub | Server storing and routing messages | Reliability | RabbitMQ |
| Pub/sub | Broadcast | Deliver each message to all subscribers | Events | SNS topic |
| Competing consumers | Shared workers | Many consumers on one queue | Scale | 20 workers |
| Ack / nack | Done / failed | Consumer feedback to broker | Delivery control | `basic_ack` |
| At-least-once | Never lose, maybe repeat | Redeliver until acked | Reliability | Default |
| Idempotent | Safe to repeat | Same effect when repeated | Duplicate safety | Upsert |
| Visibility timeout | Hidden lease | Period a received message is invisible | Crash recovery | 60 s |
| Heartbeat (lease extension) | “Still working” | Extend lease during long jobs | Avoid duplicates | +60 s |
| Dead-letter queue | Failure parking | Queue for messages exceeding retries | Isolate poison | `orders-dlq` |
| Redrive | Replay parked messages | Move DLQ messages back for reprocessing | Recovery | After fix |
| Backoff + jitter | Spaced random retries | Exponential delay with randomization | Avoid herd | 1–2–4 s |
| Backpressure | Slow-down signal | Limit producers/queue growth | Stability | 429 |
| Load shedding | Drop excess | Reject low-value/stale work | Protect SLO | Age >5 s |
| Outbox | DB-held outgoing messages | Same-transaction message rows + relay | Reliable publish | `outbox` table |
| Inbox | Processed-ID record | Dedup store for consumers | Exactly-once effect | `processed_messages` |
| Claim check | Pointer to payload | Reference to blob in object store | Small messages | S3 key |
| Message group / partition key | Order scope | Key defining sequential processing | Ordering | `order_id` |
| Head-of-line blocking | Stuck at the front | One slow/failing item blocks followers | Ordered queue risk | FIFO group |
| Saga | Compensating workflow | Series of local transactions with undo | Distributed processes | Ch 17 |
| Durable execution | Resume after crash | Workflow state persisted per step | Reliability | Temporal |
| Message age | Wait time of oldest | Time since enqueue of oldest message | Primary SLI | 45 s |

---

**Next: Chapter 13: Event Streaming and Kafka in Depth.** The commit log as a data structure: partitions, offsets, segments, replication and ISR, producers (acks, idempotence, batching), consumer groups and rebalancing, retention and compaction, exactly-once transactions, stream processing, schema evolution, and a working mini-Kafka log lab.

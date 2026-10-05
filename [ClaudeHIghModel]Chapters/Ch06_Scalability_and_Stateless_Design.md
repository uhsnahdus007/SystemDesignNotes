# Chapter 6: Scalability and Stateless Design

## Chapter card

| | |
|---|---|
| **Objective** | Understand what scalability really means, the difference between scaling up and scaling out, why *stateless* services are the foundation of scale-out, how to find and eliminate hidden state, how autoscaling works (and fails), and how a system grows step by step along a "scaling ladder" driven by measured triggers |
| **Why this chapter exists** | This is the pivot of the book. Part I told you what a system is; Part II shows how it grows. Every later chapter (load balancers, caches, replicas, shards, queues) is a rung on the ladder introduced here |
| **Prerequisites** | Chapters 0–5 (especially stateless servers in Ch 2, queueing/utilization in Ch 5, database limits in Ch 3) |
| **Study time** | 8–11 hours including labs |
| **Tier** | `[MK]` throughout; cell architecture `[GK]` |
| **Mastery criteria** | (1) Define scalability, elasticity, and scale-out efficiency, and distinguish scalability from performance. (2) Compare vertical and horizontal scaling with numbers and know when each is right. (3) Audit a service for hidden state and externalize it. (4) Explain graceful shutdown, readiness, and connection draining. (5) Predict how an autoscaling policy behaves for a traffic ramp (using boot time, thresholds, and queueing). (6) Walk the scaling ladder and state the *measured trigger* for each step. (7) Spot shared-resource bottlenecks that stop scale-out (Amdahl/USL) |

---

## 6.1 The Story: TaskBoard Grows Up

In Chapter 2 you built **TaskBoard**. Here is its life over 18 months. For each stage, notice what breaks:

| Month | Users | What happens |
|---|---|---|
| 1 | 200 | One VM runs the app and Postgres. Everything is fast. |
| 4 | 5,000 | CPU averages 40%. A Monday-morning spike hits 95%; pages crawl for 20 minutes. |
| 6 | 30,000 | You resize the VM to 4× the size (a "bigger machine"). Great: 3 months of peace. |
| 9 | 120,000 | The biggest VM your cloud offers is at 70% average. A deploy takes the site down for 4 minutes. A disk failure on a Sunday takes it down for 5 hours (restore from backup). |
| 12 | 400,000 | You add a second app server. Users report being randomly logged out and "losing" uploaded files. |
| 15 | 1.2 M | You fix those, add a load balancer, add a cache. The database CPU is now the problem. |
| 18 | 3 M | A marketing push triples traffic in two minutes; autoscaling reacts too late; 15% of requests fail for ten minutes. |

### Pause & Predict 6.1
> For months 9, 12, and 18, name the *root cause category* of each problem (capacity limit, single point of failure, hidden state, or control-loop lag). Then name the fix. Write your answers; we'll check them in §6.12.

This chapter explains each of those failures and the principles that prevent them.

---

## 6.2 What is scalability?

### 6.2.1 Definitions `[MK]`
- **Scalability:** the ability of a system to handle *increased load* by *adding resources*, while maintaining acceptable performance (a latency SLO) at a cost that grows sensibly.
- **Performance** answers "how fast is it for *one* request, right now?" **Scalability** answers "what happens to performance as load or data grows?" A system can be fast but unscalable (a fast single-threaded program), or slow but scalable (adding servers keeps latency flat, though each request is slow).
- **Elasticity:** the ability to add and remove resources *automatically and quickly* in response to demand (cloud). Scalability is about *whether* you can; elasticity is about *how fast and how automatically*.
- **Availability** (staying up) is related but different: a scalable system may still go down; scale-out designs *help* availability by removing single points of failure.

### 6.2.2 Dimensions of load `[MK]`
"Scaling" can mean growth in any of these, and each calls for different tools:
| Load dimension | Example | Typical remedies |
|---|---|---|
| Request rate (QPS) | Flash sale | Add stateless servers, caching, queues |
| Data volume | 50 TB of photos | Partitioning, object storage, tiering |
| Concurrent connections | 10 M open sockets | Event-loop gateways, connection tiers |
| Write throughput | 500 k events/s | Log-structured stores, partitioned queues |
| Geographic spread | Users on 5 continents | CDN, regional deployment |
| Data complexity / team size | 200 engineers | Service boundaries, modular architecture |

### 6.2.3 Measuring scalability: scale-out efficiency `[MK]`
Add N servers; measure throughput T(N) at the same latency SLO. Define
> **Scale-out efficiency = T(N) / (N × T(1))**

- 100% = perfectly linear (rare).
- Typical well-designed stateless tier: 85–95%.
- Systems with a shared bottleneck: efficiency falls as N grows, and total throughput can plateau or even *decline* (Chapter 5's USL: contention + coherence costs).

*Example.* A service handles 1,000 req/s on one server. With 10 servers behind a load balancer it handles 8,600 (86% efficient: LB overhead, uneven load, a shared cache). With 50 servers, 31,000 (62%: now the shared database is the constraint). The next 50 servers add almost nothing until the database is addressed. **The tier that doesn't scale out determines your ceiling.**

### 6.2.4 Cost matters
"We can scale" is incomplete without "at what cost per unit of load?" Track **cost per 1,000 requests** (or per active user, per stream-hour). A design that scales but whose unit cost rises with scale (e.g., fan-out growing with followers) can be economically unscalable.

---

## 6.3 Vertical scaling (scale up) `[MK]`

### 6.3.1 The idea
Replace the machine with a bigger one: more cores, more RAM, faster disks, more network bandwidth.
**Mental model:** hire one stronger cashier.

### 6.3.2 What you get
- **Simplicity:** no distributed-systems problems; no code changes; transactions and joins stay local; one thing to operate.
- **Speed of execution:** minutes to resize (often a reboot).
- **Great for stateful systems** (databases) where distribution is hard.

### 6.3.3 Limits
1. **Hard ceiling.** Machines top out (hundreds of cores, a few TB of RAM in mainstream clouds; beyond that is exotic and costly).
2. **Superlinear cost.** Price per unit of capacity rises at the high end: a 64-core machine is often more than 2× the price of a 32-core one.
3. **Diminishing returns.** Software may not use more cores (locks, single-threaded paths; Amdahl). A memory-bound or I/O-bound workload may not benefit from more CPU.
4. **Single point of failure.** One bigger machine is still one machine. When it fails, everything fails; recovery time grows with data size (restoring 10 TB takes hours).
5. **Downtime to resize** (many platforms), and big machines take longer to boot/restore.
6. **Blast radius.** A bad deploy or a kernel bug hits 100% of capacity.

### 6.3.4 When vertical scaling is the *right* answer
- Early in a product's life (engineering time is the scarcest resource).
- For the **database primary** (until read replicas/sharding are justified).
- For workloads that don't partition cleanly (single-writer systems).
- When the ceiling is far above forecast demand.
> **A good rule:** scale up until the next doubling is more expensive in dollars-plus-risk than the engineering work to scale out. Many successful products ran for years on a handful of large machines.

---

## 6.4 Horizontal scaling (scale out) `[MK]`

### 6.4.1 The idea
Add more machines and distribute work across them.
**Mental model:** open more checkout lanes.

### 6.4.2 What you get
- **Near-unbounded capacity** (add more nodes).
- **Fault tolerance:** one node dying loses 1/N of capacity, not everything.
- **Smooth cost curve:** commodity machines.
- **Rolling operations:** deploy and patch node by node.
- **Geographic placement:** put nodes near users.

### 6.4.3 What you pay
- **Distribution complexity:** load balancing, health checks, discovery, partial failure.
- **Data consistency problems:** state spread across nodes must be partitioned and/or replicated (Chapters 9, 10, 14, 15).
- **Network overhead:** every cross-node call costs ~0.5 ms+ (Ch 1) and can fail.
- **Operational overhead:** many nodes to monitor, secure, and upgrade.
- **Coordination costs:** locks, leader election, cache invalidation (USL's coherence term).

### 6.4.4 The three ways to spread work and state `[MK]`
| Technique | Meaning | Used for |
|---|---|---|
| **Replication** | Copies of the same data/service on several nodes | Stateless service replicas; read scaling; HA |
| **Partitioning (sharding)** | Each node owns a *different slice* of the data | Scaling writes and data volume |
| **Functional decomposition** | Different nodes do different *jobs* | Separate DB/cache/search/queue tiers; services |

Scaling a system is almost always some combination of these three. Chapter 9 is replication; Chapter 10 is partitioning; Chapter 19 is functional decomposition.

### 6.4.5 Shared-nothing architecture `[MK]`
Three architectural families:
| Architecture | Nodes share… | Example | Scalability |
|---|---|---|---|
| **Shared-memory** | RAM (one machine) | A big SMP server | Limited by one machine |
| **Shared-disk** | A storage system | Oracle RAC; some cloud DBs (Aurora's shared storage) | Moderate; storage/lock manager can bottleneck |
| **Shared-nothing** | Nothing; each node has own CPU, RAM, disk | Cassandra, sharded MySQL, most web tiers | **Highest**; coordination only via network messages |

Shared-nothing scales best because there's no common resource to contend on, and it fails in small pieces. It's the model behind most large-scale systems.

---

## 6.5 Stateless design in depth

Chapter 2 introduced "stateless servers." Here we go deep, because *almost all* scale-out difficulty on the application tier comes from state you didn't know you had.

### 6.5.1 What "stateless" really means `[MK]`
A service instance is **stateless** if, **between requests, it holds nothing that another instance couldn't reconstruct from the request itself or from shared external systems**. Consequences (these are the *tests*):
1. **Any instance can serve any request.** (No stickiness required.)
2. **Any instance can be killed at any time without losing user data or breaking user sessions.** (Disposability.)
3. **Adding instances increases capacity linearly (until a shared resource limits it).**
4. **Instances can be started, stopped, replaced, and scaled by automation, with no manual steps.**

A good acid test: *"If I kill -9 this process right now, what does a user lose?"* The answer should be "at most the in-flight requests, which will be retried."

### 6.5.2 The hidden-state audit `[MK]`
State hides in unexpected places. Walk through every service with this checklist:

| Hidden state | How it bites | Fix |
|---|---|---|
| **In-memory sessions** | User logged out when LB sends them to another instance | Shared session store (Redis) or signed tokens (JWT) |
| **Local file uploads / temp files** | File on instance A, request lands on B | Object storage (pre-signed URLs); temp files only for in-request scratch |
| **In-process caches** | Each instance has a different (stale) view; cache invalidation can't reach them | Distributed cache; or accept local caching with short TTLs and pub/sub invalidation |
| **Local counters / rate limiters** | A "100/min" limit becomes 100 × N | Central atomic store (Redis) or consistent assignment; or approximate with local+sync |
| **Local scheduled jobs (cron in the app)** | Every instance runs the job → duplicates | Dedicated scheduler, leader election, or distributed lock; idempotent jobs |
| **Singletons assuming one instance** | Unique-ID generator, in-memory queue, "current leader" flag | Externalize or coordinate (DB sequences, ID services, coordination service) |
| **In-memory queues / background threads** | Work lost on kill; invisible to other instances | Durable queue + workers |
| **Sticky connections (WebSockets, long polls)** | Genuinely stateful: connection pinned to an instance | Gateway tier + connection registry + reconnect/resync (Ch 12, 25) |
| **Configuration baked per-instance / mutated at runtime** | Instances drift; config changes need restarts | Environment/config service; immutable images |
| **Local logs on disk** | Lost with the instance; hard to search | Stream to a log pipeline (stdout → collector) |
| **Clock/ID assumptions** | Locally generated sequential IDs collide across instances | UUIDs, Snowflake-style IDs, DB sequences |
| **Idempotency/dedup memory** | Duplicate detection only works on one instance | Shared idempotency store (Ch 2, 14) |
| **Warm-up state** (JIT, connection pools, loaded models) | New instance is slow at first | Warm-up routines; slow-start in the LB; readiness gates |
| **Leader roles / "primary" duties** | Only one instance should do X | Explicit leader election or a separate singleton service |

**Process:** for each item, ask: *Is this state needed after the request ends? Where should it live? What happens if this instance dies mid-request?*

### 6.5.3 Where state should live `[MK]`
Push state "down" into purpose-built stateful systems:
```text
   Clients ─▶ [ Stateless app tier (N identical instances) ]
                    │          │            │           │
                    ▼          ▼            ▼           ▼
              Relational   Distributed   Object      Queue / log
              DB (truth)   cache/session storage      (work, events)
                           store (Redis) (files)
```
You haven't eliminated state; you've *concentrated* it in a few components designed to hold it (and replicate, back up, and scale it). Those components become your scaling challenge (Chapters 8–13).

### 6.5.4 The Twelve-Factor App, the parts that matter for scale `[GK]`
A well-known set of guidelines for cloud-native services (Heroku engineers, 2011):
- **Config in the environment** (not in code or images): same image runs in dev/stage/prod.
- **Backing services as attached resources:** DB, cache, queue are swappable via URLs/credentials.
- **Processes are stateless and share-nothing** (the whole of §6.5).
- **Port binding & concurrency by scaling processes out**, not by threads inside one giant instance.
- **Disposability:** fast startup, **graceful shutdown**.
- **Dev/prod parity**.
- **Logs as event streams** (to stdout; the platform collects).
- **Admin tasks as one-off processes** (migrations, scripts).

### 6.5.5 Disposability: startup, shutdown, and draining `[MK]`
Scale-out means instances constantly start and stop (autoscaling, deploys, spot reclaim, crashes). Each must be handled gracefully.

**Graceful shutdown sequence**
```text
1. Receive SIGTERM (platform wants this instance gone).
2. Fail the readiness check immediately → the LB stops sending NEW requests.
3. Wait for in-flight requests to finish (bounded: e.g., 30 s). Close idle keep-alive connections.
4. Flush buffers, finish or hand off background work, release locks/leases.
5. Exit 0. (If SIGKILL arrives after the grace period, anything left is lost.)
```
**Fast and safe startup**
- Don't accept traffic until **ready** (dependencies connected, caches warmed if needed): use **readiness probes** distinct from **liveness probes** (liveness = "restart me if stuck"; readiness = "send me traffic").
- Use **slow start** at the LB: ramp traffic to a new instance over 30–120 s so cold JIT/caches don't cause latency spikes (Chapter 7).
- Keep startup time short (seconds, not minutes): it directly limits how quickly you can scale (see §6.7).

**Idempotent handlers.** Because instances die mid-request and clients retry, every state-changing handler must be safe to repeat (Chapter 2: idempotency keys).

### 6.5.6 Statelessness and security/consistency subtleties
- If you use **JWTs** to avoid session stores, revocation becomes your problem (Ch 2).
- **"Stateless" tokens** can carry stale authorization claims.
- **Local caching** of authz decisions can serve revoked permissions for the TTL.
- **Read-after-write** across instances: a write via instance A followed by a read via instance B must see it. This is a *data tier* guarantee (replicas! Ch 9) and not something statelessness gives you.

---

## 6.6 Scaling each tier

The stateless app tier is the *easy* part. Real scaling is a tier-by-tier exercise.

| Tier | Scale-out method | Difficulty | Main trap |
|---|---|---|---|
| **DNS / edge / CDN** | Provider-managed, global | Low | Cache invalidation, cost |
| **Load balancer** | Managed multi-AZ, or LB tier | Low–Med | LB as SPOF/bottleneck (Ch 7) |
| **Stateless app servers** | Add instances behind LB | **Low** | Hidden state; connection explosion on DB |
| **Cache (Redis/Memcached)** | Partition by key (consistent hashing); replicas | Medium | Hot keys, rebalancing, stampedes (Ch 8) |
| **Relational DB** | Vertical → read replicas → functional split → sharding | **High** | Writes and joins don't scale out (Ch 9, 10) |
| **NoSQL/wide-column** | Built-in partitioning | Medium | Hot partitions, modeling lock-in (Ch 4) |
| **Queue/stream** | Partitions + consumer groups | Medium | Ordering, partition skew (Ch 12, 13) |
| **Search** | Shards + replicas | Medium | Fan-out tail latency, reindexing |
| **Object storage** | Provider-managed | Low | Cost, hot prefixes (Ch 11) |
| **External dependencies** | You can't scale them; rate-limit, cache, queue, isolate | High | Their limits become your limits |

### 6.6.1 Connection explosion: a classic scale-out bug `[MK]`
Each app instance keeps a DB connection pool (say 20). Scale from 10 to 500 instances: 500 × 20 = **10,000 DB connections**; the database (which handles perhaps 200–500 active connections well, Ch 3) collapses even though traffic per instance fell. Fixes: a connection **pooler/proxy** (pgBouncer, RDS Proxy) multiplexing many clients onto few DB connections; smaller per-instance pools; fewer, larger instances; read replicas; caching. *When scaling out one tier, always ask what it does to the tier behind it.*

### 6.6.2 Thundering-herd on scale-out
New instances start with cold caches and cold connection pools. If 50 instances start simultaneously (a big scale-out step), they all miss the cache, and slam the database. Mitigations: stagger startup, warm caches via shared cache, slow-start ramping, limit scale-out step sizes.

### 6.6.3 Serial bottlenecks (Amdahl, applied) `[MK]`
Look for anything every request touches that can't be parallelized:
- A single DB primary (writes).
- A global lock or a hot row/counter (Ch 3).
- A single-threaded component (Redis command loop; a coordinator).
- A shared rate limit (third-party API).
- A single leader (consensus group) in the request path.
- A shared file or sequence generator.
Each caps the scale-out. Remedies: partition the contended resource (sharded counters), batch operations, move work off the request path (queues), cache, or make the component itself scale-out (replicas/partitions).

---

## 6.7 Autoscaling `[MK]`

### 6.7.1 The problem
Traffic varies (daily cycle, promotions, incidents). Provisioning for peak all the time wastes money; provisioning for average fails at peak. **Autoscaling** adjusts capacity automatically.

### 6.7.2 Types
| Type | How it decides | Good for | Weakness |
|---|---|---|---|
| **Reactive (threshold/target tracking)** | Metric (CPU, request rate per instance, queue depth) crosses a target | General use | Lags behind fast ramps |
| **Step scaling** | Different step sizes at different thresholds | Bursty load | Tuning complexity |
| **Scheduled** | Time-based (e.g., 8:55 AM weekdays) | Known patterns, launches | Needs forecasts |
| **Predictive** | ML/forecast from history | Strongly periodic load | Cold-start of new patterns |
| **Event/queue-driven** | Scale consumers by backlog/lag | Workers (KEDA, SQS depth) | Needs good metric |

### 6.7.3 Which metric? `[MK]`
- **CPU** is easy but lags and misses I/O-bound saturation.
- **Requests per instance** (or concurrency per instance) is closer to the true capacity limit for most web services.
- **Queue depth / consumer lag** is best for workers.
- **Latency/saturation signals** (p95, thread-pool queue length) are *leading* indicators; better than CPU for catching the queueing knee (Ch 5).
- Combine: scale out on the **fastest** signal, scale in on the **slowest** (be quick to add, slow to remove).

### 6.7.4 The control loop and its delays `[MK]`
```text
traffic changes ─▶ metric emitted (≈15–60 s) ─▶ autoscaler evaluates (≈15–60 s)
   ─▶ decision ─▶ instance provisioned & booted (30 s – 5+ min) ─▶ app warm-up (10–120 s) ─▶ passes health check
   ─▶ LB starts routing ─▶ capacity actually increases
```
**Total lag: often 2–8 minutes.** During that lag, the *existing* instances carry the surge. By Chapter 5, if they're already past ~80% utilization, queues build and latency/failures spike before help arrives.

### 6.7.5 Simulation: how policy and boot time interact (Lab 1)
Scenario: 17 servers (each 100 req/s) serving 1,000 req/s at ~59% utilization. Traffic ramps to 3,000 req/s over two minutes. Clients time out after 5 s in queue. Results from the simulation in §6.14:

| Policy | Requests dropped | Seconds with >1 s queue wait | Peak servers | Server-hours (cost) |
|---|---|---|---|---|
| A: target 80% util, +20%/step, 5-min boot | **18.6%** | 363 | 38 | 7.2 |
| B: target 50% util, +50%/step, 5-min boot | **14.8%** | 304 | 60 | 10.8 |
| C: target 50% util, +50%/step, **60-s boot** | **0.0%** | 0 | 60 | 10.8 |
| D: queue-aware (leading), target 60%, 60-s boot | 0.1% | 73 | 61 | 10.2 |
| E: **pre-scaled to 40 servers** + reactive 80% target | **0.0%** | 0 | 40 | 10.0 |

**Lessons (read the table carefully)**
1. **Boot time dominates.** Policies B and C are identical except boot time (5 min vs 1 min): 14.8% dropped vs 0%. Faster boot (smaller images, pre-baked AMIs, warm pools, lighter runtimes) is often the highest-leverage autoscaling improvement.
2. **Lower thresholds help but cost more** (A→B: fewer drops, more server-hours).
3. **Leading indicators** (D) approximate faster reaction, with some residual impact.
4. **For known events, pre-scaling (E) wins on both reliability *and* cost** in this scenario: 40 servers for the whole window cost 10.0 server-hours, less than the reactive 50% policies, because they overshoot to 60.
5. Autoscaling is **not** a substitute for capacity planning, load shedding, or queues (Ch 5, 12).

### 6.7.6 Scale-in is dangerous `[MK]`
Removing instances can kill in-flight requests and concentrate load. Rules:
- **Cool-down** after scale-out before scale-in (e.g., 5–15 min); scale in slowly (10% steps).
- **Connection draining**; respect graceful shutdown (§6.5.5).
- Avoid **flapping** (hysteresis: different thresholds in/out).
- **Protect instances doing work** (scale-in protection for long jobs).
- Never scale below **N+1 / zone-survivable** minimum (Ch 5, 18).

### 6.7.7 Guardrails
Set **min and max** counts (max prevents runaway bills from a bug or attack; min preserves HA). Alert when at max. Make sure *dependencies* (DB connections, rate limits) can handle max × per-instance load: autoscaling the app tier can DDoS your own database (§6.6.1).

### 6.7.8 Kubernetes note `[GK]`
The **Horizontal Pod Autoscaler (HPA)** scales pods based on CPU or custom metrics; the **Cluster Autoscaler/Karpenter** adds nodes when pods can't be scheduled (adding node boot time to the chain); **KEDA** scales on queue length. Pod startup is fast (seconds) *if nodes already exist*; headroom ("overprovisioning pods") keeps spare node capacity warm.

---

## 6.8 The Scaling Ladder: a measured path from 1 server to millions

Each rung is justified by a *measured trigger*, not by user count alone. **Don't climb before you have to; do plan the next rung.**

```text
Rung 0   One box: app + DB + files                         trigger to leave: need HA, DB contention, can't deploy w/o downtime
Rung 1   Separate managed DB; backups; object storage      trigger: app CPU > 60% at peak, single point of failure matters
Rung 2   LB + 2–3 stateless app servers (multi-AZ)         trigger: DB CPU > 60% (reads), p99 up because repeated queries
Rung 3   Cache (Redis) + CDN for static assets             trigger: DB reads still dominate; or media bandwidth/latency
Rung 4   Read replicas + read/write splitting              trigger: write load/slow endpoints; slow work blocks requests
Rung 5   Async: queues + workers; search index; derived stores   trigger: primary near write capacity or table too big
Rung 6   Partitioning/sharding (or move hot data to scale-out stores)   trigger: need > 1 region / DR / latency abroad
Rung 7   Multi-region, cells, global traffic management    trigger: org scale, compliance, blast-radius control
```

| Rung | Typical scale it supports (very rough, workload-dependent) | What you add | What new problem appears |
|---|---|---|---|
| 0 | ≤ ~1–5k users, ≤ ~50 RPS | Nothing | Everything shares resources; deploys = downtime; no HA |
| 1 | ≤ ~50k, ≤ ~300 RPS | Managed DB, object storage, backups | App tier is a SPOF and bottleneck |
| 2 | ≤ ~500k, ≤ ~3k RPS | LB, stateless instances, external sessions | DB read load; DB connection count |
| 3 | ≤ ~2–5M, ≤ ~10–20k RPS | Cache, CDN | Staleness; cache failures; stampedes |
| 4 | ≤ ~10M | Replicas | Replication lag; read-your-writes |
| 5 | ≤ ~50M+ | Queues, workers, search, analytics pipeline | Eventual consistency; duplicates; ordering |
| 6 | 50M–hundreds of M | Sharding, scale-out stores | Cross-shard queries; rebalancing; hot shards |
| 7 | Global | Multi-region, cells | Global consistency; failover; cost |

*(User counts are indicative only: a chat app with persistent connections or a video app has very different ladders from a to-do app. Use your own estimates from Chapter 5.)*

### 6.8.1 Triggers you can actually measure
- Peak **CPU > 60–70%** or **p99 over SLO** at current load.
- **DB CPU/IOPS** sustained > 60%, or replication lag, or connection saturation.
- **Cache hit ratio** or evictions signalling memory pressure.
- **Queue lag** growing during peaks.
- **Storage growth** forecasts crossing a limit within ~6 months.
- **Deploy risk:** if every deploy needs a maintenance window, move up a rung.
- **Availability target** not achievable with the current topology.

### 6.8.2 Planning ahead vs building ahead
Design so the next rung is *possible* (stateless services, IDs that survive sharding, decoupled schemas, idempotent operations) without *building* it yet. Choices that are cheap now and expensive later:
- Don't use auto-increment IDs as public identifiers (sharding, enumeration).
- Don't join across what will be separate services/shards unnecessarily.
- Don't write to local disk.
- Keep business logic out of the database where possible (stored procedures complicate sharding).
- Keep a "tenant/user ID" on every row (a future shard key).

---

## 6.9 Cell-based architecture (blast-radius control) `[GK]`

**Problem.** In a large shared fleet, one bad deploy, poison request, or noisy tenant can take down *everyone*.
**Idea.** Partition the system into independent **cells**: each a complete copy of the stack (app servers, cache, database slice) serving a fixed subset of users. A thin **router** maps users to cells.
```text
                    Global router (maps user → cell)
          ┌──────────────┬──────────────┬──────────────┐
          ▼              ▼              ▼              ▼
       Cell A         Cell B         Cell C         Cell D     ← each: full stack, capped size
     (users 0–25%)  (25–50%)       (50–75%)       (75–100%)
```
- **Benefits:** a failure affects only one cell (blast radius ≤ 1/N); capacity added by adding cells (predictable, tested size); safer deployments (roll cell by cell); simpler capacity planning (cells have fixed max load).
- **Costs:** routing layer complexity; cross-cell operations (shared data) are hard; some duplication; rebalancing users between cells.
- Used by large cloud providers and platforms `[Public: AWS Builders' Library and others describe cell-based designs]`.

---

## 6.10 Reference architecture v1 (end-to-end)

This is the baseline we'll refine through Part II. Read each arrow.

```text
                    ┌────────────────────┐
  Users ──────────▶ │ DNS (+ geo routing)│
                    └─────────┬──────────┘
                              ▼
                    ┌────────────────────┐   static files, images, video
                    │   CDN / edge cache │ ◀──────────────────────────────┐
                    └─────────┬──────────┘                                │
                              ▼  (dynamic requests)                       │
                    ┌────────────────────┐                      ┌─────────┴────────┐
                    │ Load balancer (L7) │                      │ Object storage   │
                    │  multi-AZ, TLS     │                      └──────────────────┘
                    └─────────┬──────────┘                               ▲
              ┌───────────────┼───────────────┐                          │ pre-signed URLs
              ▼               ▼               ▼                          │
         ┌─────────┐     ┌─────────┐     ┌─────────┐                     │
         │ App 1   │     │ App 2   │ ... │ App N   │ ────────────────────┘
         │stateless│     │stateless│     │stateless│
         └──┬───┬──┘     └──┬───┬──┘     └──┬───┬──┘
            │   │           │   │           │   │
            │   └───────────┴───┴───────────┘   │ enqueue slow work
            │ reads (hot)                        ▼
            ▼                              ┌────────┐      ┌─────────────┐
      ┌───────────┐  miss                  │ Queue  │ ───▶ │ Workers     │
      │ Cache     │ ─────┐                 └────────┘      │ (email,     │
      │ (Redis)   │      ▼                                 │  resize...) │
      └───────────┘  ┌────────────┐  replication stream    └──────┬──────┘
                     │ DB primary │ ─────────────────▶ [Replica(s)]
                     └────────────┘  (writes)          (reads, analytics)
                              ▲                                   │
                              └─────────── workers write results ◀┘
```
Why each arrow exists:
- **DNS → CDN:** users find the nearest edge; static content never reaches your servers.
- **CDN → LB:** only cache misses and dynamic requests cross to the origin.
- **LB → App:** spreads load and hides failures; apps are stateless and identical.
- **App → Cache → DB:** hot reads served from RAM; misses fall through; writes go to the primary.
- **App → Object storage:** files go straight to cheap, durable storage (clients upload via pre-signed URLs).
- **App → Queue → Workers:** slow or unreliable work (email, resizing) is off the request path.
- **Primary → Replicas:** read scaling, HA, analytics offload.
**Everything left of the DB scales out horizontally with ease. The DB is the hard part.**

---

## 6.11 Deployment strategies enabled by statelessness `[MK]`

Because instances are interchangeable, you can change them gradually:
| Strategy | How | Pros | Cons |
|---|---|---|---|
| **Rolling** | Replace instances in batches | Simple; no extra capacity | Mixed versions coexist; slower rollback |
| **Blue/green** | Stand up a full new environment; switch traffic | Instant cutover/rollback | 2× capacity during switch; data/schema compat |
| **Canary** | Send 1–5% of traffic to the new version; watch metrics; ramp | Limits blast radius; real-traffic validation | Needs good metrics/automation |
| **Feature flags** | Ship code dark; enable per cohort | Decouples deploy from release | Flag debt, combinatorics |

**Compatibility rule:** during rolling/canary deploys, *old and new versions run simultaneously* against the *same* database and cache. Therefore: schema changes must be backward compatible (expand → migrate → contract, Ch 3), cache entries must be readable by both versions (version your cache keys/serialization), and API changes must be additive (Ch 2).

---

## 6.12 Answers to Pause & Predict 6.1

- **Month 9: "deploy takes the site down; disk failure → 5 hours":** *single point of failure* + capacity ceiling. Fix: at least two stateless app instances behind an LB (zero-downtime rolling deploys), a managed database with automated backups and a replica/standby (failover in minutes), object storage for files.
- **Month 12: "random logouts and lost files" after adding a second server:** *hidden state* (in-memory sessions; files on local disk). Fix: shared session store or JWT; object storage for uploads.
- **Month 18: "traffic triples in two minutes; 15% failures":** *control-loop lag* (autoscaling boot + metric + decision delays) and insufficient headroom. Fix: lower utilization targets, leading metrics, faster boot, pre-scaling for known events, load shedding/queues, protecting the DB (pooler), rate limiting.

---

## 6.13 Failure modes of scaled-out systems

| Failure | Mechanism | Mitigation |
|---|---|---|
| **Scaling the wrong tier** | App servers added but the DB is the bottleneck | USE method; trace latency by hop (Ch 5, 21) |
| **Connection explosion** | N instances × pool size > DB limit | Poolers, smaller pools |
| **Retry amplification** | Each tier retries, multiplying load | Budgets, backoff + jitter (Ch 1, 18) |
| **Hidden state surfaces under scale** | Works on 1 instance, breaks on 2 | Hidden-state audit; test with ≥2 instances early |
| **Uneven load** | Hot instance (sticky sessions, bad hashing, slow node) | Least-connections/P2C (Ch 7), outlier ejection |
| **Cold-start storms** | Many new instances cold at once | Slow start, staggering, warm pools |
| **Autoscaler oscillation** | Scale-out/in thrash | Cool-downs, hysteresis |
| **Runaway scale** | Bug/attack triggers max scale and a huge bill | Max limits, budgets/alerts, WAF/rate limits |
| **Shared dependency collapse** | Cache or DB stalls, all instances block | Timeouts, bulkheads, circuit breakers (Ch 18) |
| **Version skew** | Mixed versions during deploy | Backward-compatible schemas/APIs/caches |
| **Single-region dependency** | "Multi-AZ" app depends on a single-AZ component | Dependency mapping; test AZ loss |

---

## 6.14 Labs

### Lab 1: Autoscaling simulator (Java 17+)
```java
import java.util.*;

public class AutoscaleSim {
    // Offered load (req/s): 1,000 -> ramps to 3,000 over 2 minutes (t=60..180), holds until t=600, then back to 1,000.
    static double load(int t) {
        if (t < 60) return 1000;
        if (t < 180) return 1000 + (t - 60) * (2000.0 / 120);
        if (t < 600) return 3000;
        return 1000;
    }

    /** targetUtil: autoscaler tries to keep utilization at this level. maxStep: max servers added per decision, as a fraction of current.
     *  leading: also counts the current backlog as demand (queue-aware). minServers: floor (also the starting size). */
    record Policy(String name, double targetUtil, double maxStep, int bootSeconds, int minServers, boolean leading) {}

    static final double CAP = 100;        // req/s per server at 100% utilization
    static final double MAX_WAIT_S = 5;   // requests queued longer than this time out (are dropped)

    static void run(Policy p) {
        int ready = p.minServers();
        List<int[]> pending = new ArrayList<>();           // {readyAtSecond, count}
        double backlog = 0, dropped = 0, offered = 0, worstWait = 0, serverSeconds = 0, secondsOver = 0;
        int lastScaleOut = -10_000, peakServers = ready;
        for (int t = 0; t < 900; t++) {
            for (Iterator<int[]> it = pending.iterator(); it.hasNext(); ) {
                int[] x = it.next();
                if (x[0] <= t) { ready += x[1]; it.remove(); }
            }
            double arrivals = load(t);
            double capacity = ready * CAP;
            offered += arrivals;
            backlog += arrivals;
            backlog -= Math.min(backlog, capacity);
            double maxBacklog = MAX_WAIT_S * capacity;
            if (backlog > maxBacklog) { dropped += backlog - maxBacklog; backlog = maxBacklog; }
            double wait = backlog / capacity;
            worstWait = Math.max(worstWait, wait);
            if (wait > 1.0) secondsOver++;
            serverSeconds += ready + pending.stream().mapToInt(a -> a[1]).sum();   // you pay for booting servers too
            peakServers = Math.max(peakServers, ready);

            if (t % 15 == 0) {                                                     // autoscaler decision every 15 s
                double demand = arrivals + (p.leading() ? backlog / 15.0 : 0);     // req/s we need to serve
                int desired = (int) Math.ceil(demand / (CAP * p.targetUtil()));
                int inFlight = pending.stream().mapToInt(a -> a[1]).sum();
                if (desired > ready + inFlight) {
                    int add = Math.min(desired - ready - inFlight, Math.max(1, (int) Math.ceil(ready * p.maxStep())));
                    pending.add(new int[]{t + p.bootSeconds(), add});
                    lastScaleOut = t;
                } else if (desired < ready && t - lastScaleOut > 300) {              // scale in slowly, after a cool-down
                    ready -= Math.min(ready - Math.max(desired, p.minServers()), Math.max(1, (int) Math.ceil(ready * 0.1)));
                    ready = Math.max(ready, p.minServers());
                }
            }
        }
        System.out.printf("%-52s dropped=%5.1f%%  seconds with >1s queue wait=%3.0f  peak servers=%3d  server-hours=%5.1f%n",
            p.name(), 100 * dropped / offered, secondsOver, peakServers, serverSeconds / 3600);
    }

    public static void main(String[] args) {
        System.out.println("Traffic 1,000 -> 3,000 req/s in 2 min. Each server = 100 req/s. Requests queued >5 s time out.\n");
        run(new Policy("A: target 80% util, +20%/step, 5-min boot",             0.80, 0.20, 300, 17, false));
        run(new Policy("B: target 50% util, +50%/step, 5-min boot",             0.50, 0.50, 300, 17, false));
        run(new Policy("C: target 50% util, +50%/step, 60-s boot",              0.50, 0.50,  60, 17, false));
        run(new Policy("D: queue-aware (leading), target 60%, 60-s boot",       0.60, 1.00,  60, 17, true));
        run(new Policy("E: pre-scaled to 40 servers + reactive 80% target",     0.80, 0.20, 300, 40, false));
    }
}
```
**Experiments**
1. Reproduce the table in §6.7.5.
2. Try boot times of 30 s, 120 s, 600 s for policy B; plot dropped% vs boot time.
3. Change the ramp to 6 minutes (slower). Which policies are now fine?
4. Add a **max servers = 45** cap (budget guardrail). What happens to drops?
5. Make the per-server capacity 50 req/s *while warming up* for the first 60 s after boot (cold caches). How much worse do the reactive policies get?
6. Add **load shedding**: if wait > 2 s, reject immediately (count as 'rejected' not 'timed out'). Compare user-visible outcomes: fast rejection vs slow timeouts.

### Lab 2: Hidden-state audit (paper exercise using the Chapter 2 TaskApi)
Open the Chapter 2 `TaskApi.java`. List every piece of state it holds (`tasks`, `ids`, `idem`, `idemLocks`). For each: (a) what breaks with two instances behind a load balancer; (b) where it should live; (c) what changes in the code (e.g., DB table with unique constraint for idempotency; database sequence/UUID for IDs). Then, after Chapter 7, run two instances behind the Chapter 7 `MiniLb` and *observe* each failure.

### Lab 3: Connection explosion arithmetic
A service has DB `max_connections=300`, of which 50 are reserved for admin/replication. Each app instance has a pool of 20 (each query ~4 ms; each instance serves 500 req/s making 2 queries per request).
1. How many instances saturate the DB's connection limit? 
2. What is the *actual* number of connections each instance needs (Little's Law: concurrency = rate × time)? Compute and set a sensible pool size.
3. At 200 instances with pgBouncer in transaction mode (pool of 100 server connections), what is the maximum DB query throughput (1 query = 4 ms)? Is the pooler or the DB the limit?

---

## 6.15 Interview perspective
- Open scaling discussions with **"first, what's the bottleneck at this load?"** Show you don't scale reflexively.
- State **"app servers are stateless; sessions in Redis/JWT; files in object storage"** whenever you draw an app tier.
- Walk the ladder in order and tie each rung to a metric ("DB reads exceed ~10k/s so I'd add a cache then replicas").
- Mention **N+1 / AZ-survivable capacity**, **autoscaling lag**, **pre-scaling for events**, and **connection pooling**.
- For follow-ups ("10× traffic?"), find the *next* bottleneck explicitly (often the DB or a shared hot key).
- Avoid "just add servers" without addressing state, DB limits, and caches.

---

## 6.16 Common misconceptions

1. **"Scalable means fast."** Scalable means performance holds as load grows.
2. **"Horizontal scaling is always better."** It trades simplicity for capacity; vertical is often right early and for databases.
3. **"Stateless means there's no state in the system."** It means the state lives elsewhere.
4. **"Autoscaling solves traffic spikes."** Lag, boot time, and dependencies often make it too late for sharp spikes.
5. **"Adding servers always adds proportional capacity."** Shared bottlenecks (Amdahl/USL) cap it.
6. **"Cloud means infinite capacity."** Quotas, instance availability, and dependency limits exist.
7. **"Sticky sessions are fine."** They hide statefulness and hurt failover and load balance.
8. **"We'll shard when we need to."** Preparation (IDs, keys, schemas) must come early; sharding itself can be deferred.
9. **"More instances = more resilience."** Not if they share a single point of failure (DB, AZ, config service).
10. **"Scale-in is the easy direction."** It's riskier (draining, state, concentration).

---

## 6.17 Chapter summary

- **Scalability** = sustaining performance as load grows by adding resources; **elasticity** = doing so automatically; measure **scale-out efficiency** and **unit cost**.
- **Vertical** scaling is simple, limited, and a SPOF; **horizontal** scaling is unbounded but demands **statelessness**, distribution, and coordination. Scaling uses **replication, partitioning, and functional decomposition**; **shared-nothing** designs scale best.
- **Stateless services** pass the "kill -9" test; audit for **hidden state** (sessions, files, caches, counters, cron, singletons, queues, config); design for **disposability** with graceful shutdown, readiness, draining, and idempotent handlers.
- **The app tier is easy; the data tier is hard.** Watch for **connection explosion**, cold-start storms, and **serial bottlenecks**.
- **Autoscaling** is a delayed control loop: boot time dominates; use leading metrics, lower targets, pre-scale for known events, scale in slowly, set min/max guardrails, and remember dependencies.
- **The scaling ladder** gives a measured path (single box → separate DB → LB + stateless apps → cache/CDN → replicas → queues → sharding → multi-region/cells). Climb only when metrics demand; design so the next rung is possible.
- **Cell architecture** limits blast radius. **Rolling/canary deploys** rely on statelessness and backward-compatible data/API/cache formats.

### Mental models
| Concept | Mental model |
|---|---|
| Vertical vs horizontal | One superhuman cashier vs more checkout lanes |
| Stateless instance | A hotel desk clerk reading from the shared booking system |
| Shared-nothing | Independent food stalls, not one shared kitchen |
| Autoscaling lag | Ordering extra staff by mail when the rush has already started |
| Scaling ladder | Adding rooms to a house as the family grows, not building a mansion first |
| Cell architecture | Watertight compartments on a ship |

---

## 6.18 Exercises

### A. Conceptual
1. Define scalability, elasticity, and performance. Give an example of a system that is fast but not scalable.
2. Compute scale-out efficiency: 1 server = 800 req/s; 8 servers = 5,600 req/s; 32 servers = 14,400 req/s. What is likely limiting the 32-server case?
3. List five limits of vertical scaling and two situations where it's still the right choice.
4. What do replication, partitioning, and functional decomposition each contribute to scaling? Give an example for each.
5. Why does shared-nothing scale better than shared-disk?
6. State the "kill -9 test" and two other tests of statelessness.
7. For each: in-memory session, local upload, in-process cache, local rate-limit counter, in-app cron job. Explain the failure with 3 instances and the fix.
8. What is the difference between liveness and readiness probes? What happens if you use liveness for readiness and the DB is briefly unavailable?
9. Describe a correct graceful-shutdown sequence.
10. Why must handlers be idempotent in an elastic fleet?
11. Explain connection explosion with numbers (200 instances × 25 connections). Give three fixes.
12. Why do new instances cause a thundering herd, and how do slow start and staggering help?
13. List five serial bottlenecks that cap scale-out.
14. Compare reactive, scheduled, predictive, and queue-driven autoscaling.
15. List the stages of autoscaling delay. Which can you shorten?
16. Why scale in slowly? List three scale-in risks.
17. Describe the scaling ladder and one measurable trigger per rung.
18. What is a cell-based architecture? Benefits and costs?
19. Rolling vs blue/green vs canary: choose for (a) a risky change; (b) an urgent small fix; (c) a major database migration.
20. Why must schemas, APIs, and caches be backward compatible during rolling deploys?

### B. Calculations
21. A service handles 400 req/s per instance at 60% target utilization. Peak = 18,000 req/s. How many instances for N+1 across 3 AZs, where the loss of an entire AZ must be survivable?
22. Pool sizing: 600 req/s per instance, 3 queries/request at 3 ms each, 40% of queries run concurrently with others... simplify: compute average concurrent queries per instance by Little's Law and suggest a pool size (with 2× headroom).
23. From Lab 3: 250 usable DB connections; each instance has a pool of 20. How many instances before exhaustion? With pgBouncer (100 server connections, 4 ms queries) what's the max query throughput?
24. Autoscaling: traffic ramps from 2,000 to 6,000 req/s over 90 seconds. Each instance serves 150 req/s at 100%. Boot + warm-up = 4 minutes. Metrics + decision lag = 45 s. Starting from 20 instances, how much capacity shortfall exists at the end of the ramp and for how long? What is the minimum initial instance count that avoids overload (target ≤100% utilization) if the reaction cannot arrive before the ramp ends + lag?
25. Cost comparison: Policy X: 60 servers for 10 minutes plus 17 otherwise; Policy Y: 40 servers all day; server cost $0.10/hr. Compute daily cost for each (assume the event happens once per day).
26. A cache is added so that 80% of reads are cache hits. Reads were 6,000/s against a DB that handles 4,000/s at 70% target utilization. Does the DB now fit? What if the hit ratio is only 60%?

### C. Design
27. Audit this service for hidden state and propose changes: A Java app stores uploaded avatars in `/tmp/uploads`, keeps a `HashMap<String, Session>` for logins, runs a `@Scheduled` nightly report job in every instance, and generates order numbers with an in-memory counter.
28. Plan the scaling path for a photo-sharing app from 10k to 10M users: specify the rungs, triggers, and what each step changes. Include the schema/ID decisions to make early.
29. Design the autoscaling policy for (a) a stateless API; (b) an image-resizing worker fleet consuming a queue; (c) a batch nightly job. Choose metrics, thresholds, min/max, cool-downs.
30. A flash sale will 10× traffic for 20 minutes at a known time. Produce the capacity plan (pre-scaling, caches, DB protection, queueing, load shedding, test plan).
31. Propose a cell-based design for a multi-tenant SaaS with 5,000 tenants of widely varying sizes. How do you place tenants, handle a huge tenant, and migrate a tenant between cells?

### D. Debugging
32. After adding a 3rd app server, error rates increase and the DB shows "too many connections". Diagnose and fix.
33. Throughput stops improving at 12 instances although CPU on each is 35%. List four hypotheses with tests.
34. During scale-in, users report being kicked out of file uploads midway. Why? Fix.
35. After each deploy, p99 doubles for ~3 minutes. List causes (cold caches/JIT, pool warm-up, missing slow start) and fixes.
36. Autoscaler oscillates every 10 minutes between 20 and 40 instances. Diagnose.

### E. Interview-style
37. "Walk me through scaling a web app from 1 to 100M users." (3-minute answer)
38. "How do you know when to scale vertically vs horizontally?"
39. "Your service is stateless but still can't scale beyond 10k RPS. Why?"

### F. Teach-back
40. Explain "stateless" to a new engineer using the hotel desk analogy and then via the kill -9 test.

---

## 6.19 Full answer key

**1.** Scalability: sustaining acceptable performance as load grows by adding resources. Elasticity: automatic, rapid adjustment of resources to demand. Performance: speed for a given load. A fast single-threaded program on one machine is fast but not scalable.

**2.** Efficiency(8) = 5,600/(8×800) = 87.5%. Efficiency(32) = 14,400/(32×800) = 56%. The drop signals a shared bottleneck: database, cache, a lock, LB limits, or a shared downstream rate limit.

**3.** Limits: hard ceiling; superlinear cost; diminishing returns (Amdahl/NUMA/locks); SPOF; downtime/long recovery; large blast radius. Right choice: early stage (cheap in engineering time), database primary, workloads that don't partition, or ceiling comfortably above forecast.

**4.** Replication: copies for read scaling/HA (stateless replicas, DB replicas). Partitioning: each node owns a slice for write/data scale (sharded users). Functional decomposition: separate nodes for separate jobs (DB, cache, search, queue; microservices).

**5.** Shared-nothing has no common resource (lock manager, shared storage) to contend on; nodes coordinate by messages only, so capacity adds nearly linearly and failures are isolated.

**6.** Kill -9 test: killing a process loses nothing but in-flight requests. Other tests: any instance can serve any request; instances can be added/removed freely without manual steps or user impact.

**7.** Sessions: user randomly logged out → Redis/JWT. Local uploads: file missing on other instances → object storage. In-process cache: inconsistent/stale views → distributed cache or short-TTL local + invalidation. Local rate limit: effective limit ×3 → central counters. In-app cron: runs 3× → single scheduler/leader election/distributed lock and idempotent jobs.

**8.** Liveness: should this process be restarted? Readiness: should it receive traffic? If a liveness check includes the DB and the DB blips, every instance gets restarted (cascading outage). Dependency checks belong in readiness at most, with care (do not mark all instances unready simultaneously).

**9.** On SIGTERM: fail readiness (stop new traffic) → wait/finish in-flight within a deadline, close idle connections → flush/hand off work and release leases → exit; if the deadline passes, the platform kills (so keep work short or checkpointed).

**10.** Instances die mid-request, LBs retry, clients time out and retry; the same operation may execute more than once, so writes must be safe to repeat (idempotency keys, unique constraints, conditional writes).

**11.** 200 × 25 = 5,000 connections vs a DB comfortable at a few hundred. Fixes: pgBouncer/RDS Proxy pooling, reduce per-instance pool sizes (Little's Law), fewer larger instances, caching/replicas to cut DB demand.

**12.** New instances have cold caches/pools/JIT and all miss the cache simultaneously, slamming the DB and slowing themselves. Slow start ramps traffic to new instances; staggering limits simultaneous starts; shared caches mean new instances don't start cold.

**13.** Single DB primary (writes); global locks/hot rows; single-threaded components (Redis loop); shared third-party rate limits; consensus leader in the request path; shared sequence generator/file.

**14.** Reactive: responds to metrics (lags). Scheduled: pre-scales for known times. Predictive: forecasts from history. Queue-driven: scales consumers by backlog. Use combinations: scheduled/predictive baseline + reactive for surprises.

**15.** Metric emission → autoscaler evaluation → provisioning/boot → app warm-up → health check → LB registration. Most shortenable: boot/image size, warm pools, app warm-up, evaluation intervals, and using leading metrics.

**16.** Scale in slowly because load may return, in-flight work may be killed, and load concentrates on the survivors (raising utilization toward the knee). Risks: dropped connections, lost in-flight work, thrash/flapping, falling below N+1.

**17.** See §6.8 table: e.g., rung 2 triggered by app CPU > 60% or SPOF concerns; rung 3 by DB read load; rung 4 by read-heavy limits/lag; rung 5 by slow tasks/write capacity; rung 6 by primary write ceiling/data size; rung 7 by global latency/compliance/DR.

**18.** Independent full-stack copies each serving a subset of users, with a router. Benefits: small blast radius, predictable capacity, safer rollout. Costs: routing complexity, cross-cell operations, rebalancing, duplicated overhead.

**19.** (a) Canary/feature flag; (b) rolling or blue/green; (c) expand/migrate/contract with blue/green or canary and careful validation.

**20.** Old and new code run simultaneously against the same DB/cache/API clients; incompatible changes break one of them.

**21.** Per-instance effective = 400 × 0.6 = 240 req/s. For peak 18,000 → 75 instances if all healthy. To survive loss of one of 3 AZs, the remaining 2 AZs must carry the load at the target: need ≥ 75 instances in 2 AZs → 38 per AZ → total 3 × 38 = **114 instances** (≈ 50% more than 75). (If you relax the target to 80% during an AZ loss, fewer: 18,000/320 = 57 in 2 AZs → 29 per AZ → 87 total.)

**22.** Queries/s per instance = 600 × 3 = 1,800/s; concurrency = 1,800 × 0.003 = 5.4 average; pool of ~10–12 (2× headroom).

**23.** 250/20 = 12.5 → exhaustion at ~13 instances. With pgBouncer: 100 server connections / 0.004 s = **25,000 queries/s** maximum (if the DB itself can execute 100 concurrent queries; usually the DB's CPU/IO limits earlier).

**24.** Capacity at 20 instances = 3,000 req/s. At the end of ramp (6,000 req/s) shortfall = 3,000 req/s (50% deficit). Reaction arrives ≈ 45 s lag + 4 min boot = ~285 s after the ramp begins; the ramp ends at 90 s, so overload lasts ~195 s at the full deficit. Avoid overload entirely by pre-scaling: at least 6,000/150 = 40 instances at 100%; with a 70% target, 6,000/(150×0.7) = 58 instances.

**25.** X: 60 servers for 10 min = 10 server-hours... compute: 60 × (10/60) = 10 server-hours; plus 17 × (24 − 0.1667) ≈ 405 server-hours ⇒ ≈ 415 server-hours ⇒ **$41.5/day**. Y: 40 × 24 = 960 server-hours ⇒ **$96/day**. Pre-scaling all day is costlier; scheduled pre-scaling only around the event is cheaper (e.g., 40 for 1 hour: 17×23 + 40×1 = 431 sh ⇒ $43.1).

**26.** DB budget = 4,000 × 0.7 = 2,800 reads/s. At 80% hits: misses = 20% × 6,000 = 1,200/s ✔ fits. At 60% hits: misses = 2,400/s ✔ still fits (< 2,800) but with little headroom (and bursts/cold caches could break it).

**27.** Issues: `/tmp/uploads` → object storage; `HashMap<Session>` → Redis/JWT; `@Scheduled` job on every instance → dedicated scheduler/leader election and idempotent job; in-memory order counter → DB sequence/ID service (UUID/Snowflake). Also: config, logs, graceful shutdown, readiness.

**28 (sketch).** 10k: single server + managed DB. 100k: LB + 2–3 stateless apps, sessions externalized, object storage, CDN. 1M: Redis cache, replicas, queue for resizing/notifications. 10M: partition photos/follows by user ID (decide ID format early), timeline cache, search. Early decisions: time-ordered IDs, user_id on every row, avoid cross-user joins on hot paths, idempotent writes, object-key naming for CDN.

**29 (sketch).** (a) Target requests-per-instance ~60% of load-tested capacity; scale-out fast (+50%), scale-in slow (10%/10 min); min = AZ-survivable. (b) Scale on queue lag/age of oldest message; max bounded by downstream limits; scale-in protection while processing. (c) Scheduled start/stop; size for the job window; use spot capacity with checkpointing.

**30 (sketch).** Pre-scale app tier and workers 30–60 min ahead; warm caches; enlarge DB headroom (vertical or replicas), pre-warm connection poolers; cache product/inventory reads; virtual waiting room/queue for checkout; atomic inventory updates; load shedding for non-essential features; rate limit bots; test at 1.5× with open-loop tests; runbooks, on-call, and rollback plans.

**31 (sketch).** Cells with fixed capacity; place tenants by size class (large tenants get dedicated cells); a router service/dir maps tenant → cell; migration via replicate-then-cutover; keep a "tenant_id" on all data; cross-tenant features via async aggregation; monitor per-cell utilization and rebalance.

**32.** Connection explosion: per-instance pool × instances exceeded DB limit; add a pooler and shrink pools. Also check long transactions and leaked connections.

**33.** Hypotheses: DB or cache bottleneck (check their utilization); a global lock or serialized section (thread dumps); LB/NIC limits (packets/bandwidth); rate limits/quotas; pool sizes; single-threaded dependency; uneven load. Tests: USE metrics per tier, thread dumps, vary pool sizes, bypass dependency with stubs.

**34.** Scale-in killed instances with in-flight long uploads (no draining, aggressive timeout). Fix: connection draining/longer grace period, scale-in protection while uploading, or direct-to-object-storage uploads via pre-signed URLs (so app instances aren't in the upload path), resumable uploads.

**35.** Cold JIT/caches/pools on new instances and no slow start; the LB sends full traffic immediately; rolling restarts reduce capacity. Fixes: slow start, warm-up requests, readiness gating, surge capacity, canary, smaller batch sizes.

**36.** Symptoms of poorly tuned thresholds without hysteresis/cool-down: scale-out reduces utilization below the scale-in threshold, which removes instances, which raises utilization again. Fix: cool-downs, separated thresholds, use average over longer windows, scale-in slower than scale-out.

**37 (sketch).** Start with one box; separate DB/object storage; LB + stateless app tier; cache + CDN; replicas; queues/workers; search/analytics; sharding; multi-region/cells; for each, mention trigger, new problem, and mitigation; emphasize measuring and N+1.

**38 (sketch).** Vertical when early, for stateful primaries, when the ceiling is far, or engineering time is scarce; horizontal when approaching the ceiling, cost curve steepens, need HA, or workload partitions cleanly. Evidence: utilization trends, SPOF risk, cost per unit, deployment frequency.

**39 (sketch).** A shared bottleneck: DB connections/CPU, lock contention, a downstream rate limit, a single-threaded service, a chatty fan-out, or LB/NIC limits. Use traces and USE to find it; then cache, partition, batch, or decouple.

**40.** (Self-assessed.) Include: any clerk can help you because your booking is in the shared system; if a clerk disappears, nobody loses anything but the interaction in progress; the kill -9 test.

---

## 6.20 Flashcards

| Front | Back |
|---|---|
| Scalability vs performance | Holding performance as load grows vs speed at one load |
| Elasticity | Automatic, rapid resource adjustment to demand |
| Scale-out efficiency | T(N) / (N × T(1)) |
| Vertical scaling limits | Ceiling, superlinear cost, SPOF, downtime, blast radius |
| Three scaling tools | Replication, partitioning, functional decomposition |
| Shared-nothing | No shared CPU/RAM/disk; scales best |
| Stateless test | kill -9 loses only in-flight requests; any instance serves any request |
| Hidden state examples | Sessions, local files, in-proc caches, counters, cron, singletons |
| Graceful shutdown | Fail readiness → drain → flush → exit |
| Liveness vs readiness | Restart me vs send me traffic |
| Connection explosion | instances × pool size > DB limit; use poolers |
| Autoscale delay chain | Metric → decision → boot → warm-up → health check → LB |
| Biggest autoscale lever | Faster boot; pre-scale for known events |
| Scale-in rule | Slow, cooled-down, drained, never below N+1 |
| Scaling ladder | Single box → managed DB → LB+apps → cache/CDN → replicas → queues → sharding → multi-region |
| Cell architecture | Independent full-stack partitions to limit blast radius |
| Deploy compat rule | Old+new run together: additive APIs, expand/contract schemas, versioned caches |

## 6.21 Glossary additions

| Term | Simple Meaning | Technical Meaning | Why It Exists | Example |
|---|---|---|---|---|
| Scalability | Grows without breaking | Sustaining SLOs as load grows via added resources | Growth planning | 10× users |
| Elasticity | Grows and shrinks automatically | Rapid automatic provisioning/deprovisioning | Cost and spikes | Autoscaling group |
| Vertical scaling | Bigger machine | Increase one node's resources | Simplicity | 8→32 cores |
| Horizontal scaling | More machines | Add nodes and distribute work | Capacity, HA | 3→30 servers |
| Shared-nothing | Independent nodes | No shared CPU/RAM/disk | Max scalability | Cassandra |
| Stateless service | Forgets between requests | No local client state; externalized | Interchangeable instances | API tier |
| Hidden state | State you didn't notice | Per-instance data affecting behavior | Breaks scale-out | In-memory sessions |
| Disposability | Safe to kill anytime | Fast start, graceful stop | Elastic fleets | SIGTERM handler |
| Readiness probe | "Send me traffic?" | Health signal for LB eligibility | Safe rollouts | `/ready` |
| Liveness probe | "Am I stuck?" | Signal to restart a process | Self-healing | `/live` |
| Slow start | Ramp traffic up | LB gradually increases a new node's share | Avoid cold-start spikes | 60-s ramp |
| Connection pooler | Shared DB connections | Multiplexes many clients onto few DB connections | Protect the DB | pgBouncer |
| Autoscaling | Automatic capacity changes | Policy-driven instance count adjustment | Match demand | HPA |
| Cool-down | Wait between scale actions | Delay preventing thrash | Stability | 300 s |
| Pre-scaling | Scale before the event | Scheduled capacity increase | Beat the lag | Flash sale |
| Scaling ladder | Stepwise growth path | Sequence of architecture upgrades by trigger | Avoid over-engineering | Cache → replicas |
| Cell | Independent stack slice | Self-contained deployment serving a user subset | Limit blast radius | Cell A |
| Canary | Small-scale trial release | Gradual traffic to new version | Safer deploys | 1% traffic |
| Blue/green | Two environments | Switch traffic between full environments | Instant rollback | Cutover |
| Blast radius | Impact of a failure | Fraction of users/system affected | Risk control | One cell |

---

**Next: Chapter 7: Load Balancers, Proxies, and Gateways.** Algorithms (with simulation evidence for why *power of two choices* matters), L4 vs L7, health checks and outlier detection, high availability for the balancer itself, global load balancing, reverse proxies, API gateways, service meshes, and a working Java load balancer lab.

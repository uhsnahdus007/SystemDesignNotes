# Chapter 24: The Design Method and Interview Framework

## Chapter card

| | |
|---|---|
| **Objective** | Learn a repeatable procedure for designing any system under time pressure: clarify requirements, estimate with numbers, define the API and data model, draw the simplest end-to-end architecture, deep-dive the hard parts, find bottlenecks, plan for failure, state trade-offs, and communicate clearly. The same method works in interviews, design reviews, and real projects |
| **Why this chapter exists** | Chapters 0–23 gave you the building blocks. Knowing them is necessary but not sufficient: most weak interview answers are *not* missing knowledge; they skip requirements, skip numbers, jump to technologies, or never explain trade-offs. This chapter turns the toolbox into a procedure, and Chapters 25–28 apply it to 24 problems |
| **Prerequisites** | The whole book, but especially Ch 5 (estimation), 6 (scaling ladder), 14–15 (failure and consistency), 18 (reliability) |
| **Study time** | 8–10 hours plus deliberate practice |
| **Tier** | `[MK]` |
| **Mastery criteria** | (1) Run the nine-step method on an unseen problem within 45 minutes. (2) Ask a complete set of clarifying questions and write down the resulting requirements. (3) Produce an estimate (QPS, storage, bandwidth, cache, servers) in under five minutes and sanity-check it. (4) Derive a data model and API from access patterns. (5) Evolve an architecture in justified steps, each driven by a number or a failure. (6) State every key decision as *choice, reason, cost, alternative*. (7) Self-score an answer against the rubric and name your weakest dimension |

---

## 24.1 The Story: Two Answers to "Design a Chat Service"

**Candidate A** hears the question and starts talking: "We'll have WebSocket gateways, Kafka for messages, Cassandra for storage, Redis for presence, and microservices on Kubernetes. We can shard by user ID." After 12 minutes the whiteboard has 14 boxes. The interviewer asks: "How many messages per second?" A says: "A lot, probably millions." "How do you keep messages in order in a group chat?" A pauses: "Kafka preserves order." "Across partitions?" Silence. "What happens if a gateway crashes?" "Kubernetes restarts it." The interviewer notes: strong vocabulary, no requirements, no numbers, no trade-offs, unexamined claims.

**Candidate B** asks for four minutes of clarifying questions ("1:1 and groups? Group size? Delivery guarantees? Read receipts? History retention? How many users?"), writes down requirements, and computes: *"50 M DAU × 40 messages = 2 B messages/day ≈ 23 K/s average, ~70 K/s peak, ~120 bytes each → 87 TB/year raw. Connections: if 20% of DAU are online at peak, 10 M concurrent sockets: about 100–200 K per gateway → 50–100 gateways."* Then B draws the **simplest** system that could work (one gateway, one database), shows which number breaks it (10 M connections; 70 K writes/s), and adds one component at a time with a reason. When asked about ordering, B says: "per-conversation sequence numbers assigned by the conversation's single writer; clients dedupe and reorder by sequence; no global order needed." On the crash question B answers: "the client reconnects with its last seen sequence number and the server returns everything after it; messages are acknowledged only after durable write."

Same knowledge. A different procedure. B's answer is more *checkable*: every claim is attached to a number or a mechanism.

### Pause & Predict 24.1
> List five concrete behaviours that distinguish B from A. For each, say which piece of the evaluation (requirements, estimation, design, depth, reliability, communication) it earns credit in. What did A do with the first 12 minutes, and what could B do with the same time?

---

## 24.2 What a design exercise evaluates `[MK]`

Whether in an interview or a design review, reviewers are (consciously or not) scoring:

| Dimension | What a strong answer shows |
|---|---|
| **1. Problem exploration** | Asks about users, scale, features, constraints; scopes the problem; states assumptions explicitly |
| **2. Quantitative reasoning** | Estimates traffic, storage, bandwidth; uses numbers to choose and to reject designs; sanity-checks |
| **3. Design coherence** | A complete end-to-end path for every core use case; consistent data model and API; no hand-waving boxes |
| **4. Technical depth** | Can go two or three levels down on the hardest components (partitioning, consistency, indexing, caching, queues) |
| **5. Trade-off judgment** | Names alternatives, picks one for stated reasons, and says what it costs and when they would change their mind |
| **6. Scalability** | Finds the bottleneck at each growth step; applies the scaling ladder; handles hot spots |
| **7. Reliability and operations** | Failure modes, redundancy, degradation, monitoring, deployment, data protection |
| **8. Communication** | Structured, thinks aloud, uses the whiteboard well, checks in, adapts to hints and pushback |
| **9. Pragmatism** | Starts simple, avoids unnecessary technology, considers cost and team complexity |
| **10. Ownership and learning signals** | Admits uncertainty, corrects mistakes, asks for feedback, connects to real experience |

**Level calibration (typical):** *Junior/mid:* a working basic design with correct components. *Senior:* numbers drive decisions; deep dives with real trade-offs; failure handling; clear communication. *Staff+:* identifies the true risks and the hardest problem, reasons about cost, org and migration, multi-region, evolution over years, and when *not* to build.

---

## 24.3 The method: nine steps `[MK]`

```text
  1 CLARIFY        requirements, scope, scale, constraints         (5 min)
  2 ESTIMATE       QPS, storage, bandwidth, cache, servers          (5 min)
  3 INTERFACE      API + data model from access patterns            (5 min)
  4 SKETCH         simplest end-to-end architecture                 (8-10 min)
  5 DEEP DIVE      2-3 hardest components, in detail                (12-15 min)
  6 SCALE          bottlenecks, hot spots, scaling ladder           (3-5 min)
  7 HARDEN         failures, consistency, security, monitoring      (3-5 min)
  8 TRADE-OFFS     decisions, alternatives, what would change them  (continuous)
  9 WRAP UP        summary, risks, evolution plan, questions        (2 min)
```
Time budget for a 45-minute round. Use a timer. The most common failure is spending 25 minutes on step 4.

**Principles behind the method**
- **Breadth first, then depth.** Cover every core use case end-to-end with a coarse design before diving; depth on a broken skeleton earns little.
- **Numbers decide.** A component enters the design because a number (or a failure) demands it, not because it is fashionable.
- **Access patterns decide the data model.** Start from the queries, not the entities.
- **Evolve, don't leap.** Show v1 (works for 1,000 users), then v2, v3 as scale or failures force changes: this also demonstrates the scaling ladder (Ch 6).
- **Say the trade-off every time.** *Choice, reason, cost, alternative.*

---

## 24.4 Step 1: Clarify requirements `[MK]`

### 24.4.1 Functional requirements
What must the system *do*? Identify 3–5 **core use cases** and say what is out of scope. Write them as user stories:
- "A user can post a message to a conversation and recipients see it in near real time."
- "A user can load the last 50 messages when opening a conversation."
Ask about **actors** (users, admins, third parties), **platforms** (web/mobile), **entities** and **relationships**, **read vs write dominance**, and **features that change the design** (editing, deleting, search, ranking, media, notifications, analytics).

### 24.4.2 Non-functional requirements (NFRs) (the ones that shape architecture)
| Dimension | Questions |
|---|---|
| **Scale** | Users (total, DAU, peak concurrent)? Requests per user per day? Data volume and growth? Geographic spread? |
| **Latency** | Target for key operations (p50/p99)? Real-time or eventual? |
| **Availability** | 99.9%? 99.99%? Tolerance for downtime and degraded modes? |
| **Consistency** | Can a user see stale data? Which operations need strong consistency (money, uniqueness)? Read-your-writes? |
| **Durability** | Can we ever lose data? RPO/RTO? Retention? Deletion/GDPR? |
| **Security & privacy** | AuthN/Z, multi-tenancy, encryption, compliance (PCI, HIPAA), abuse? |
| **Cost** | Budget constraints? Build vs buy? Team size and skills? |
| **Operability** | Deployment model, observability, on-call, multi-region? |
| **Compatibility** | Existing systems, protocols, clients that cannot be changed? |

### 24.4.3 How to ask well
- Ask **closed questions with defaults**: "I'll assume 100 M DAU and ~10:1 read:write; is that reasonable?" This saves time and shows judgment.
- **Prioritize questions that change the architecture** (read/write ratio, consistency needs, latency target, scale, data size). Don't ask for the colour of the buttons.
- **Restate** the requirements in one slide-sized summary and get a nod.
- Write **"Out of scope"** explicitly (e.g., no video calls, no end-to-end encryption in v1). Revisit if time remains.
- Capture requirements on the whiteboard in a corner and **refer back** to them when justifying decisions.

### 24.4.4 Worked example: "Design a global game leaderboard"
Functional: submit a score; get top 100; get *my* rank and neighbours; per game and season. Out of scope: anti-cheat details, matchmaking. NFRs: 10 M DAU, 50 M registered players, ~20 score submissions per user per day, leaderboard views ~10 per user per day; rank freshness within ~1–2 s acceptable; rank must be *correct* (no wrong ordering) for the top 100; durability: a submitted score must not be lost; availability 99.95%; no multi-region requirement in v1.

---

## 24.5 Step 2: Estimation `[MK]`

### 24.5.1 Why estimate
Estimates (a) tell you whether one machine suffices, (b) pick the architecture (sharding? cache? CDN? queue?), (c) expose impossible designs early, (d) give you a basis for every later number. The goal is the right **order of magnitude** (within ~2×), shown with **explicit assumptions**.

### 24.5.2 Reference numbers `[MK]`
| Quantity | Value | Rounding tip |
|---|---|---|
| Seconds per day | 86,400 | ≈ 10⁵ (use 100 K for rough math; remember it overstates seconds by 16%) |
| Seconds per month / year | 2.6 M / 31.5 M | ≈ 2.5 M / 30 M |
| 1 M requests/day | ≈ 12 requests/s | 10⁶ / 10⁵ = 10, ×1.16 |
| 1 B requests/day | ≈ 11.6 K requests/s | |
| Peak factor | 2–5× average | Use 3× unless the product is bursty (sales, games, sports: 5–10×) |
| KB / MB / GB / TB / PB | 10³ / 10⁶ / 10⁹ / 10¹² / 10¹⁵ bytes | Use decimal for estimates |
| Bits vs bytes | 1 byte = 8 bits | Network: Gbit/s ÷ 8 = GB/s |
| Typical record sizes | tweet/message ≈ 100–300 B; user profile ≈ 1–2 KB; photo ≈ 0.2–3 MB; 1 min of video ≈ 5–30 MB | |
| One server | ≈ 1–5 K simple req/s; DB primary ≈ 5–20 K simple queries/s; Redis ≈ 50–100 K ops/s; Kafka broker ≈ 50–100 MB/s | Order of magnitude only |
| Latency ladder | L1 1 ns · RAM 100 ns · SSD read 100 µs · same-AZ RTT 0.5 ms · cross-region RTT 50–150 ms · disk seek 10 ms | Chapter 1 |
| Availability | 99.9% = 8.8 h/yr · 99.99% = 53 min/yr | Chapter 14 |

### 24.5.3 The estimation recipe
1. **Traffic.** `avg QPS = (DAU × actions per user per day) ÷ 86,400`; `peak QPS = avg × peak factor`. Separate **reads** from **writes** (and fan-out: one write may cause N reads/writes).
2. **Storage.** `bytes per day = writes/day × bytes per record (+ index overhead 30–100%)`; `raw per year = ×365`; `retained = ×years`; `with replication = ×3` (or ×1.4 with erasure coding). Mention hot vs cold tiers.
3. **Bandwidth.** `ingress = peak write QPS × write bytes`; `egress = peak read QPS × response bytes`. Convert to Gbit/s (×8). Media dominates: compute it separately and put it behind a CDN.
4. **Memory/cache.** `hot set = fraction of daily unique objects × object size`. Cache 10–20% of the daily working set for a Zipf workload (hit ratio logarithmic in size; Chapter 8).
5. **Servers.** `servers = peak QPS ÷ (per-server capacity × 0.6 target utilization)`; then add **N+1 / AZ-survivable** capacity (×1.5 for three AZs; Chapter 18).
6. **Connections** (real-time systems): concurrent connections = DAU × online fraction at peak; ~50–200 K per gateway.
7. **Sanity checks.** Compare with known systems; check units (bits/bytes, per second/day); check that the answer is not larger than the planet's population or the internet's bandwidth; recompute one number two ways.

### 24.5.4 A tool-verified table for the problems in this book
The following table was generated by the program in the lab (§24.12). Each row is a problem in Chapters 25–26 (plus this chapter's leaderboard), with the assumptions stated in the lab code.
```text
Assumptions: peak = 3x average; storage replicated 3x; servers sized at 60% utilization of the stated per-server capacity.
problem                     writes/s avg   reads/s avg   peak reads/s   storage/yr raw   retained x3   peak egress    peak ingress   app servers (peak)   cache (hot set)
URL shortener                    1.2 K       115.7 K        347.2 K          18.3 TB      273.8 TB        0.8 Gbps      0.01 Gbps          117            30.0 GB
Pastebin                            58           579          1.7 K          18.3 TB       54.8 TB        0.1 Gbps      0.01 Gbps            2            20.0 GB
File upload service                 12            35            104           1.8 PB       16.4 PB        4.2 Gbps      1.39 Gbps            1               0 KB
Chat (1:1 + groups)             23.1 K        69.4 K        208.3 K          87.6 TB        1.3 PB        0.2 Gbps      0.07 Gbps           47            40.0 GB
News feed (timeline)               463        23.1 K         69.4 K           7.3 TB       65.7 TB        3.3 Gbps      0.01 Gbps           60           640.0 GB
Notification system              6.9 K         6.9 K         20.8 K          65.7 TB       19.7 TB        0.1 Gbps      0.05 Gbps           14            30.0 GB
Photo sharing                    1.2 K        57.9 K        173.6 K         127.8 PB        1.9 EB      416.7 Gbps     97.22 Gbps          591            20.0 GB
File sync (metadata+chunks)      1.2 K         3.5 K         10.4 K          36.5 PB      328.5 PB       83.3 Gbps     27.78 Gbps           47           100.0 GB
Web crawler (pages)                382           382          1.1 K           1.2 PB        3.6 PB        0.9 Gbps      0.92 Gbps           39               0 KB
Typeahead                          231        23.1 K         69.4 K         438.0 GB        1.3 TB        0.2 Gbps      0.00 Gbps            6            10.0 GB
Game leaderboard (Ch 24)         2.3 K         1.2 K          3.5 K           2.9 TB        8.8 TB        0.1 Gbps      0.00 Gbps            1             5.0 GB

Short-code capacity (base62) at 100 M new URLs/day:
  6 characters: 62^6 = 56.8 B codes -> lasts 2 years (5 years of URLs = 182.5 B -> 321.30% of the space)
  7 characters: 62^7 = 3.5 T codes -> lasts 96 years (5 years of URLs = 182.5 B -> 5.18% of the space)
  8 characters: 62^8 = 218.3 T codes -> lasts 6.0 K years (5 years of URLs = 182.5 B -> 0.08% of the space)
```
**How to read it.** (1) Look at the **ratio of reads to writes** first: the URL shortener is 100:1, the file-upload service is read-light but byte-heavy. (2) **Bytes dominate for media** (photo sharing: 417 Gbit/s peak egress, 128 PB/year of new data, ~1.9 EB retained at 3× replication, which is why erasure coding, tiering, and a CDN are *requirements*, not options). (3) **Request rate dominates for metadata** (chat: 23 K writes/s, 69 K reads/s, but only 88 TB/year). (4) The **"app servers" column is small** for most problems: *the application tier is rarely the hard part*; the data tier, fan-out, hot keys, and connections are. (5) The **cache column** tells you whether the hot set fits in one machine's RAM (leaderboard: 5 GB, yes) or needs a cluster (feed timelines: 640 GB).

### 24.5.5 Common estimation mistakes
- **Users ≠ QPS.** 100 M users is not 100 M QPS; convert via actions per user per day.
- **Forgetting peak.** Capacity must handle peak (3–5× average), not average.
- **Forgetting replication, indexes, and retention.** The raw number can be 5–10× too small.
- **Bits vs bytes**, **MB vs MiB**, **per-second vs per-day**: a 10× error is easy.
- **Ignoring fan-out.** One post to a user with 1 M followers is 1 M timeline writes (Chapter 26).
- **False precision.** "47,381 QPS" is worse than "~50 K QPS"; round aggressively and say "assume".
- **Using estimates once.** Refer back: "this is why I need sharding", "this is why a single Redis is enough."

---

## 24.6 Step 3: API and data model `[MK]`

### 24.6.1 API design (recap of Ch 2)
- Define the **core endpoints** that realize the use cases: resource-oriented REST (`POST /conversations/{id}/messages`), or RPC (gRPC) for internal services, WebSocket/SSE for push.
- Specify **request/response shapes** only for the key calls; mention **authentication**, **idempotency keys** for writes, **pagination** (cursor-based, not offset), **rate limits**, **versioning**, and **error semantics**.
- Show the **read and write paths** by naming which API calls are on the hot path.

### 24.6.2 Data model from access patterns
1. List the **queries** the system must answer ("messages in conversation C after sequence N", "top 100 scores for game G", "short code → URL").
2. For each, decide the **lookup key** and **sort order**. This determines the primary key/partition key (Chapter 4, 10).
3. Choose the **store** per access pattern (Chapter 3/4 decision table): relational for transactional integrity and ad-hoc queries; key-value/wide-column for massive key-based access; document for aggregates; search index for text; time-series for metrics; object storage for blobs; graph for traversals; in-memory sorted sets for ranking.
4. Define **entities, keys, indexes, and sizes**; note which fields are hot.
5. Identify **derived data** (counters, timelines, search indexes) and how it is kept up to date (events/CDC; Chapter 17).
6. Think about **IDs** (Chapter 10: Snowflake/UUIDv7; short codes), **time handling**, **soft deletes**, and **schema evolution**.

### 24.6.3 SQL or NoSQL? A decision table
| Choose relational when… | Choose key-value / wide-column / document when… |
|---|---|
| You need multi-row transactions, constraints, joins, ad-hoc queries | Access is by key or key+range at huge scale |
| Data fits on a few machines (or can be sharded by a clean key) | Write throughput or data size exceeds one node and you can partition cleanly |
| Schema is stable and relational | Schema varies, or you store aggregates/documents |
| Strong consistency is the default need | You accept tunable/eventual consistency for availability and scale |
**Default:** start with a relational database (Postgres) unless a number says otherwise; Chapter 3's ladder shows how far one primary plus replicas goes.

---

## 24.7 Step 4: The high-level sketch `[MK]`

### 24.7.1 Draw the simplest thing that could work
```text
 Client ──▶ [Load balancer] ──▶ [App servers (stateless)] ──▶ [Database]
```
Walk through each core use case on it ("a write goes here; a read goes here"). Only then ask: **which number breaks this?** Add components in response:

| The number says… | Add / change | Chapter |
|---|---|---|
| Reads far exceed what the DB serves | Cache (Redis/CDN), read replicas | 8, 9, 11 |
| Writes exceed one primary / data exceeds one node | Partitioning, LSM store, queue for write smoothing | 4, 10, 12 |
| Large binary data | Object storage + CDN, pre-signed uploads | 11 |
| Slow work on the request path | Queue + workers | 12 |
| Many consumers need the same events | Pub/sub or log (Kafka) | 12, 13 |
| Millions of persistent connections | Gateway tier, connection registry, pub/sub between gateways | 7, 12 |
| Global users, latency | CDN, regional deployment, geo-DNS | 7, 11, 9 |
| Hot keys / celebrities | Key splitting, caching, hybrid fan-out | 8, 10 |
| Text search | Inverted index (Elasticsearch/OpenSearch) | 4, 26 |
| Nearby / geo queries | Geohash/quadtree/S2 index | 27 |
| Uniqueness or coordination | Consensus-backed store, unique index, ID service | 16 |
| Counting at scale | Sketches / batched counters | 20 |
| Availability target | Multi-AZ redundancy, failover, degradation | 9, 18 |

### 24.7.2 The "evolution narrative"
Present the design as versions: **v1** single server + DB (works for ~10 K users), **v2** separate stateless app tier behind an LB + managed DB with replica, **v3** cache + CDN, **v4** queue/async + workers, **v5** partitioning, **v6** multi-region. At each step state *the trigger* (a measured number or a failure) and *the new problem the step introduces*. This shows the scaling ladder, gives the interviewer natural places to probe, and guards against over-engineering.

### 24.7.3 Whiteboard hygiene
Left-to-right data flow; label arrows with protocol and data; mark **sync vs async**; show **data stores with their keys**; use consistent shapes (boxes services, cylinders stores, queues as pipes); keep a legend of numbers in a corner; erase nothing important; narrate while drawing.

---

## 24.8 Step 5: The deep dive `[MK/GK]`

Pick the **2–3 hardest or riskiest components** (ideally in agreement with the interviewer) and go deep. Use this checklist on each:

1. **Data:** shape, size, key, access patterns, indexes, retention.
2. **Scaling:** partition key and scheme (Chapter 10), replication factor, cache layers, expected hot spots and mitigations.
3. **Consistency:** what must be strongly consistent? what is eventual? read-your-writes? ordering? idempotency? (Chapter 12, 15, 17)
4. **Failure modes:** node dies, network partition, duplicate/retried requests, poison messages, slow dependency, overload (Chapter 14, 18).
5. **Algorithms:** the specific mechanism (consistent hashing, token bucket, fan-out, trie with top-k, rolling hash chunking, politeness scheduling).
6. **Operational concerns:** monitoring signals, SLOs, deployment/rollback, capacity, cost.
7. **Alternatives considered** and why rejected.

**The five-question bank** (reusable for any feature): *What is the data and how big? How is it accessed? What happens when it grows 10×? What happens when X fails? What is the cheapest correct design?*

---

## 24.9 Steps 6–7: Bottlenecks, scaling, and hardening `[MK]`

**Scaling walk (step 6).** Take the sketch and ask of each component: *what is its limit and what is the next rung?* (stateless tier → add instances; DB reads → cache/replicas; DB writes → partition; queue → partitions/consumers; cache → shards; media → CDN). Find the **single points of failure** and the **hot spots** (popular keys, giant tenants, celebrities, time-ordered inserts). Quote the limits you computed: "At 70 K peak writes/s a single Postgres primary is at its ceiling, so I would shard the messages table by conversation ID."

**Hardening checklist (step 7).**
- **Availability:** multi-AZ; no single point of failure; stateless tiers; health checks; graceful degradation; backups and restore; RPO/RTO (Chapter 18).
- **Consistency & correctness:** idempotent writes; ordering guarantees where required; conflict handling; exactly-once *effects* (Chapter 12).
- **Overload:** rate limiting, backpressure, load shedding, timeouts, circuit breakers (Chapter 18).
- **Security & privacy:** authN/authZ, object-level checks, encryption, abuse controls, PII (Chapter 22).
- **Observability:** the SLIs/SLOs, key dashboards and alerts, tracing, audit (Chapter 21).
- **Deployment & migration:** canary, backward-compatible schema changes, data migration plan (Chapter 18, 19).
- **Cost:** biggest cost drivers (egress, storage tier, cross-AZ traffic) and levers (Chapter 23).

---

## 24.10 Step 8: Stating trade-offs `[MK]`

### 24.10.1 The sentence pattern
> **"I'd choose X because [requirement/number]. The cost is Y. The alternative is Z, which I'd choose if [condition]."**

Examples:
- "I'll use **cache-aside Redis** with a 60-second TTL because the read:write ratio is 100:1 and slightly stale redirects are fine. The cost is a stale-data window and cache-stampede risk, which I'll mitigate with single-flight and jitter. If URLs could be edited and needed immediate effect, I'd invalidate on write via an event."
- "I'll use **asynchronous replication** for the primary because commit latency matters more than zero data loss for this data; the cost is up to ~20 acknowledged writes lost on failover at 1,000 writes/s (Chapter 9). For payments I'd use synchronous or quorum replication."
- "**Fan-out on write** for most users, **fan-out on read** for accounts with over 100 K followers, because pure push would cost ~15 B timeline inserts/day (Chapter 26)."

### 24.10.2 Recurring trade-off axes
Consistency ↔ availability/latency (Ch 15); latency ↔ cost; read cost ↔ write cost (fan-out, indexes, denormalization); storage ↔ compute (precompute vs compute on demand); simplicity ↔ flexibility; freshness ↔ load (TTL); durability ↔ throughput (fsync, sync replication); build ↔ buy; strictness ↔ user experience (exact vs approximate); isolation ↔ efficiency (multi-tenancy); centralization ↔ resilience.

### 24.10.3 Phrases to avoid
- "It will scale infinitely." / "Kafka guarantees exactly-once." / "NoSQL is faster than SQL." / "We'll just add more servers." Replace with **mechanism + number + limit**.
- "Strong consistency everywhere" or "five nines in five regions" without cost: these show you haven't priced the requirement.

---

## 24.11 Communicating under pressure `[MK]`

- **Structure out loud:** "I'll start with requirements, then estimates, then API and data model, then a high-level design and two deep dives, then failure handling." The interviewer can steer.
- **Think aloud:** reasoning matters more than the final answer. Say what you're considering and rejecting.
- **Checkpoint** every 8–10 minutes: "Does this direction work for you, or would you like me to go deeper somewhere?"
- **Handle hints:** a hint is information about what the interviewer cares about. Acknowledge, adapt, and don't get defensive.
- **Handle challenges:** "What if X fails?" is a chance, not an attack. Restate, answer with a mechanism, and give the cost.
- **When you don't know:** say so, then reason from first principles or from related systems ("I haven't used Cassandra's LWT, but a Paxos-based compare-and-set would cost roughly 4 round trips, so I'd use it only for the rare uniqueness check").
- **Time management:** use a timer; if you overrun on one step, say "I'll make a simplifying assumption here and revisit it."
- **Don't memorize designs:** memorized architectures break when the interviewer changes one requirement. Learn the *method* and the *building blocks*.
- **Collaborate:** treat the interviewer as a teammate in a design review.

### 24.11.1 Frequent failure patterns
1. Starting with technology instead of requirements. 2. No numbers, or numbers never used. 3. A box-and-arrow salad with no data model. 4. Overengineering for scale the problem doesn't have. 5. Ignoring failure and consistency. 6. One-sided trade-offs ("just use Kafka"). 7. Talking only, never drawing (or drawing only, never explaining). 8. Not answering the question asked. 9. Getting stuck in one component for 30 minutes. 10. Never summarizing, so the interviewer can't see the whole.

---

## 24.12 Lab: a back-of-envelope calculator `[MK]`

Estimation is a skill that improves with a reliable reference. The program below reproduces the table in §24.5.4. Use it as a template: change the assumptions, rerun, and compare with your hand calculations (do the hand calculation *first*).
```java
import java.util.*;

/** Back-of-envelope calculator used by the design method (Ch 24) and every problem in Ch 25-26. */
public class EstimateLab {
    record P(String name, double writesPerDay, double readsPerDay, double writeBytes, double readBytes, double retentionYears,
             double uniqueReadObjectsPerDay, double cachedBytesPerObject, double hotFraction, double serverQps) {}

    static String fmt(double v) { if (v >= 1e12) return String.format("%.1f T", v / 1e12); if (v >= 1e9) return String.format("%.1f B", v / 1e9); if (v >= 1e6) return String.format("%.1f M", v / 1e6); if (v >= 1e3) return String.format("%.1f K", v / 1e3); return String.format("%.0f", v); }
    static String bytes(double v) { if (v >= 1e18) return String.format("%.1f EB", v / 1e18); if (v >= 1e15) return String.format("%.1f PB", v / 1e15); if (v >= 1e12) return String.format("%.1f TB", v / 1e12); if (v >= 1e9) return String.format("%.1f GB", v / 1e9); if (v >= 1e6) return String.format("%.1f MB", v / 1e6); return String.format("%.0f KB", v / 1e3); }

    public static void main(String[] a) {
        double peak = 3.0, replicas = 3.0, util = 0.6;
        P[] ps = {
            new P("URL shortener",               100e6, 10e9,  500,   300,   5,   300e6, 500,    0.20, 5000),
            new P("Pastebin",                    5e6,   50e6,  10_000, 10_000, 1,  20e6,  10_000, 0.10, 2000),
            new P("File upload service",         1e6,   3e6,   5e6,   5e6,   3,   0,     0,      0,    500),
            new P("Chat (1:1 + groups)",         2e9,   6e9,   120,   150,   5,   50e6,  8_000,  0.10, 10000),
            new P("News feed (timeline)",        40e6,  2e9,   500,   6000,  3,   100e6, 6_400,  1.00, 2000),
            new P("Notification system",         600e6, 600e6, 300,   300,   0.1, 600e6, 50,     1.00, 5000),
            new P("Photo sharing",               100e6, 5e9,   3.5e6, 300e3, 5,   100e6, 1_000,  0.20, 500),
            new P("File sync (metadata+chunks)", 100e6, 300e6, 1e6,   1e6,   3,   50e6,  20_000, 0.10, 500),
            new P("Web crawler (pages)",         33e6,  33e6,  100e3, 100e3, 1,   0,     0,      0,    100),
            new P("Typeahead",                   20e6,  2e9,   60,    400,   1,   100e6, 100,    1.00, 20000),
            new P("Game leaderboard (Ch 24)",    200e6, 100e6, 40,    2000,  1,   50e6,  100,    1.00, 20000),
        };
        System.out.println("Assumptions: peak = 3x average; storage replicated 3x; servers sized at 60% utilization of the stated per-server capacity.");
        System.out.println("problem                     writes/s avg   reads/s avg   peak reads/s   storage/yr raw   retained x3   peak egress    peak ingress   app servers (peak)   cache (hot set)");
        for (P p : ps) {
            double wAvg = p.writesPerDay() / 86400, rAvg = p.readsPerDay() / 86400, rPeak = rAvg * peak, wPeak = wAvg * peak;
            double storYear = p.writesPerDay() * 365 * p.writeBytes(), retained = storYear * p.retentionYears() * replicas;
            double egressGbps = rPeak * p.readBytes() * 8 / 1e9, ingressGbps = wPeak * p.writeBytes() * 8 / 1e9;
            double servers = Math.ceil((rPeak + wPeak) / (p.serverQps() * util));
            double cache = p.hotFraction() * p.uniqueReadObjectsPerDay() * p.cachedBytesPerObject();
            System.out.printf("%-27s %10s %13s %14s %16s %13s %10.1f Gbps %9.2f Gbps %12.0f %18s%n", p.name(), fmt(wAvg), fmt(rAvg), fmt(rPeak), bytes(storYear), bytes(retained), egressGbps, ingressGbps, servers, bytes(cache));
        }
        System.out.println();
        double[] sizes = {6, 7, 8}; double perDay = 100e6;
        System.out.println("Short-code capacity (base62) at 100 M new URLs/day:");
        for (double k : sizes) { double space = Math.pow(62, k); System.out.printf("  %.0f characters: 62^%.0f = %s codes -> lasts %s years (5 years of URLs = %s -> %.2f%% of the space)%n", k, k, fmt(space), fmt(space / perDay / 365), fmt(perDay * 365 * 5), 100 * perDay * 365 * 5 / space); }
    }
}
```
**Exercises with the lab**
1. Add three problems from your own experience (e.g., an IoT telemetry ingestion service, an email provider, a ride-hailing location service); first estimate by hand, then check.
2. Change the peak factor from 3 to 8 for the game leaderboard and see which columns change. Which design decisions change?
3. Add a column for **cross-AZ replication traffic** (writes/s × bytes × (replicas − 1)) and for **number of Kafka partitions** if all writes go through a log at 10 MB/s per partition.
4. Add **N+1 across 3 AZs** to the server count (×1.5).
5. Add an **erasure-coding** option (1.4×) for the storage column and recompute the photo-sharing retained size.

---

## 24.13 A complete worked answer: the game leaderboard (45 minutes) `[MK]`

This is the method end to end, with timing. The problem was introduced in §24.4.4.

**[0:00–0:05] Clarify.** (As in §24.4.4.) Summary written on the board: *submit score; top-100; my rank + neighbours; per game/season; 10 M DAU, 50 M players; rank within ~1–2 s fresh; no lost scores; 99.95%.*

**[0:05–0:10] Estimate** (verified by the lab's last row):
- Writes: 10 M × 20 = 200 M score submissions/day ≈ **2.3 K/s average**; game events cluster in evenings: peak ×5 ≈ **12 K/s**.
- Reads: 10 M × 10 = 100 M views/day ≈ 1.2 K/s average, **~3.5 K/s peak** (×3).
- Data: 50 M players × one *best score* each. A Redis sorted-set entry costs ~100 B (member + score + skip-list overhead) → **~5 GB**: fits in one machine's RAM. If we kept every submission: 200 M × 40 B × 365 = 2.9 TB/year; that is a log/analytics concern, not the ranking store.
- Conclusion: **a single in-memory sorted structure with replicas is enough**; no sharding needed in v1.

**[0:10–0:15] API and data model.**
- `POST /games/{g}/seasons/{s}/scores {player_id, score, submission_id}` (idempotent via `submission_id`; server-side validation).
- `GET /games/{g}/seasons/{s}/top?n=100`; `GET /games/{g}/seasons/{s}/players/{p}/rank?around=5`.
- Store 1 (truth, durable): `best_scores(game, season, player_id) → best_score, updated_at` in a relational or key-value store; Store 2 (serving): a **Redis sorted set** `lb:{game}:{season}` scoring players for ranking. Tie-break: encode `score × 2^20 + (MAX_TS − timestamp)` so earlier achievers rank higher.

**[0:15–0:25] Architecture and deep dive.**
```text
 Game client ──▶ API (stateless, LB) ──▶ validate + dedupe (submission_id) ──▶ (a) durable store: UPSERT best score (only if higher)
                                                              └──▶ (b) Redis ZADD GT lb:g:s score player   (O(log N))
 Reads:  top-100 ──▶ app cache 1 s ──▶ ZREVRANGE 0 99 ;  rank ──▶ ZREVRANK + ZREVRANGE around rank
```
- **Ranking operations:** `ZADD ... GT` updates only if the new score is greater (idempotent and monotonic, safe to retry); `ZREVRANK` returns the rank in O(log N); `ZREVRANGE` returns a window; both are fast at 50 M elements.
- **Durability:** write the best score to the durable store first (or append to a log); Redis is a *rebuildable index*: on loss, rebuild from the table (50 M rows at ~1 M rows/s ≈ 1 minute) while the API serves a degraded "rank unavailable" for a short time or reads from a replica.
- **Consistency:** two stores updated by the API creates a dual-write problem; use the outbox/stream (Ch 12): write to the DB and an outbox; a consumer applies `ZADD GT` (idempotent) so Redis converges even after retries or crashes; freshness ≈ 100 ms–1 s.
- **Hot reads:** the top-100 page is requested by millions: cache it in the app tier for 1 s (collapse requests with single-flight), or precompute it every second and serve from the CDN; a 1-second stale top-100 is acceptable.
- **Failure:** Redis primary dies → promote a replica (async replication may lose the last ~second of ZADDs; the outbox replays them); API instance dies → stateless, retried with `submission_id` idempotency.

**[0:25–0:32] Scale and bottlenecks.** Per-season, per-game sets are independent: **shard by leaderboard** (key includes game/season) across Redis nodes; this scales to thousands of leaderboards. For a single leaderboard beyond ~500 M players (or 100 GB), partition **by score range**: keep a coarse histogram of counts per score bucket; `rank = (players in higher buckets) + position inside the bucket`; accept approximate ranks for deep positions (nobody cares whether they are 4,203,117th or 4,203,121st). Mitigate write hot spots (everyone submits at the same instant after an event) with a queue in front of the Redis writer and batching (`ZADD` multiple).

**[0:32–0:40] Harden.** Anti-cheat: server-authoritative scoring, signed submissions, rate limits per player, anomaly detection offline; abuse of rank queries: limit `around` window size; observability: SLIs = submit→visible latency p99 < 2 s, availability 99.95%, ZADD error rate; alerts on outbox lag; multi-AZ Redis with a replica per AZ; backups of the durable store with PITR; seasonal archival: at season end, freeze the set and snapshot to object storage, then drop the key.

**[0:40–0:45] Trade-offs and wrap-up.** *Redis sorted set vs SQL `ORDER BY`:* SQL rank queries need a sort/index scan (`COUNT(*) WHERE score > x` is O(N) without an index and still O(log N) with an order-statistic tree); the sorted set gives O(log N) rank in RAM at ~100 B/entry. *Approximate vs exact rank:* exact for top-100 and neighbours; approximate far down. *Eventual freshness vs strict:* ≤ 1–2 s is fine. Evolution: multi-region would add per-region leaderboards plus a global one built by merging periodically. Risks: dual-write drift (mitigated by outbox + periodic reconciliation), hot leaderboard at season rollover.

*Why this is a strong answer:* every box exists because of a number; the data model came from three queries; the hard part (rank at scale) got a real mechanism and a scale-out plan; failure and consistency were addressed; and trade-offs were stated with costs.

---

## 24.14 Step 9: Wrapping up `[MK]`

In the last two minutes: **summarize** the architecture in three sentences; **name the top risks** and what you'd monitor; **state what you'd build next** if given more time (evolution plan); **mention what you deliberately left out**; ask whether there's an area to go deeper. This also gives you control of the final impression.

---

## 24.15 Self-scoring rubric `[MK]`

After every practice session, score yourself 1–5 on each dimension (1 = missing, 3 = adequate, 5 = excellent). Be harsh and cite evidence.

| # | Dimension | 1–2 (weak) | 3 (adequate) | 4–5 (strong) | Your score / evidence |
|---|---|---|---|---|---|
| 1 | Requirements | Jumped in; assumptions hidden | Some questions; vague scope | Scoped, summarized, drove the design | |
| 2 | Estimation | None, or never used | Done, rough, partly used | Done fast, sanity-checked, drove decisions | |
| 3 | API & data model | Absent or entity-first | Reasonable but not tied to queries | Derived from access patterns; keys and indexes explicit | |
| 4 | High-level design | Box salad or incomplete | Complete core path | Minimal, justified, evolves with numbers | |
| 5 | Deep dive | Surface only | One component with some depth | Mechanism-level depth in the hardest parts | |
| 6 | Scalability | Hand-wave | Generic ("add servers") | Specific bottlenecks, hot spots, scaling ladder | |
| 7 | Reliability/ops | Not discussed | A few failures mentioned | Failure modes, degradation, monitoring, deployment | |
| 8 | Trade-offs | None / one-sided | Some alternatives | Every key decision with cost and alternative | |
| 9 | Communication | Rambling or silent | Clear enough | Structured, checkpoints, adapts to hints | |
| 10 | Time management | Ran out of time | Finished unevenly | Balanced; summary delivered | |
**Scoring guide:** total /50. ≥ 42 strong senior-level; 34–41 solid; 26–33 gaps to fix; < 26 practice the method before more content. **Track your weakest dimension and spend the next three sessions on it.**

---

## 24.16 Building a personal pattern library and practicing

1. **Learn patterns, not answers.** Keep a one-page note per building block (cache, queue, shard, replica, CDN, search index, geo index, fan-out, rate limiter, ID generator, workflow) with: *when to use, key numbers, failure modes, chapter reference*. Chapter 25–28 problems each emphasize different patterns.
2. **Practice under time.** 45-minute timed sessions, out loud, on a whiteboard or paper; record yourself; score with the rubric.
3. **Vary the constraints.** After solving a problem, change one requirement (10× scale, strong consistency, multi-region, 99.999%, cost cap) and redesign. This builds the flexibility interviewers test.
4. **Do estimation drills daily** (five minutes): pick a product, estimate QPS/storage/bandwidth, check against the lab.
5. **Read real architectures** (engineering blogs, papers) and label what each choice optimizes.
6. **Teach it.** Explaining a design to someone else exposes gaps faster than rereading.
7. **Do mock interviews** with peers; give each other rubric-based feedback.
8. **Keep an error log:** every missed trade-off or number goes in, with the correction.

---

## 24.17 Common misconceptions

1. **“There is one right architecture.”** There are defensible architectures with different trade-offs; the *reasoning* is graded.
2. **“More components = better.”** The simplest design that meets the numbers is better.
3. **“I should name as many technologies as possible.”** Name the few that matter and justify them.
4. **“Estimation must be precise.”** Orders of magnitude with stated assumptions suffice.
5. **“Requirements are the interviewer's job.”** Eliciting them is part of the test.
6. **“Interviewers want my memorized solution.”** They want to see how you think and adapt.
7. **“Scaling means sharding.”** Often caching, replicas, async, or better indexes come first.
8. **“Failure handling is an afterthought.”** It is a graded dimension.
9. **“If I don't know a technology I fail.”** Reasoning from first principles about mechanisms is acceptable and often better.
10. **“Deep dive means more boxes.”** It means more mechanism on fewer boxes.

---

## 24.18 Chapter summary

- A **nine-step method**: Clarify → Estimate → Interface (API + data model) → Sketch → Deep dive → Scale → Harden → Trade-offs → Wrap-up, with a 45-minute time budget (5/5/5/10/12-15/5/5/continuous/2).
- **Requirements** drive the design: functional use cases, out-of-scope list, and NFRs (scale, latency, availability, consistency, durability, security, cost); ask closed questions with defaults; restate.
- **Estimation**: traffic, storage, bandwidth, cache, servers, connections; decimal units; peak factor; replication and retention; sanity checks. Tool-verified numbers show where problems differ: media is bytes-bound (photo sharing 417 Gbit/s peak, 1.9 EB retained), messaging is request-bound (chat 23 K writes/s), and the app tier is rarely the hard part.
- **Access patterns decide the data model**; start with a relational store unless a number says otherwise.
- **Draw the simplest system, then let numbers force each addition** (the evolution narrative); use the "number says → add" table.
- **Deep dive** with a checklist (data, scaling, consistency, failure, algorithm, operations, alternatives).
- **State trade-offs** as *choice, reason, cost, alternative*; avoid absolutes.
- **Communicate**: structure aloud, checkpoint, handle hints and challenges, manage time, summarize.
- **Rubric and practice loop**: score 10 dimensions, fix the weakest, vary constraints, keep an error log.
- Worked example: the **game leaderboard** (Redis sorted set, outbox, idempotent `ZADD GT`, shard by leaderboard, bucketed approximate rank at extreme scale).

### Mental models
| Concept | Mental model |
|---|---|
| The method | A pilot's checklist: the same steps every flight, adapted to conditions |
| Estimation | Measuring a room before buying furniture |
| Access patterns first | Designing the shelves after you know what people will ask for |
| Evolution narrative | Growing a house room by room as the family grows |
| Trade-off sentence | A receipt: what you chose, what it cost, what you declined |
| Deep dive | Taking one engine apart rather than listing all the parts of the car |
| Rubric | A fitness log: measure, find the weak muscle, train it |

---

## 24.19 Exercises

### A. Conceptual
1. List the nine steps with a time budget for a 45-minute interview. What goes wrong if step 4 takes 25 minutes?
2. Why is “breadth first, then depth” the right order? What risk does early depth create?
3. Give five architecture-changing questions to ask for “Design a ride-sharing service,” and say what each changes.
4. What is an access pattern and why does it come before the schema?
5. Explain the evolution narrative using a URL shortener: what are v1 and v2, and what number triggers v2?
6. Rewrite “I'll use Kafka” as a complete trade-off sentence for a notification service.
7. List five estimation mistakes and a defense against each.
8. How would you respond if the interviewer says, “What if the scale were 100× larger?”
9. How do level expectations differ between a mid-level and a staff-level answer to the same question?
10. What does “the app tier is rarely the hard part” mean, and where does the difficulty usually live?

### B. Estimation drills (do by hand, then check)
11. 300 M DAU, each making 8 API calls/day. Average and peak QPS (peak ×3).
12. 50 M photo uploads/day at 2 MB. Storage per day, per year, with 3× replication, and with 1.4× erasure coding.
13. 100 M hours of video watched per day at 3 Mbit/s average. Average and peak (×2) egress in Tbit/s.
14. Peak 50 K QPS, 2 K QPS per server at 60% target utilization. Servers needed? With AZ-survivable capacity across 3 AZs?
15. 200 M DAU, 2 KB profile objects, cache the hottest 20%. Cache size?
16. A Bloom filter for 1 B URLs at 1% false positives. Memory?
17. 100 M Redis keys averaging 100-byte values with ~50 bytes overhead each. Memory? Does it fit one 64 GB node with 50% headroom?
18. Each event costs 5 ms of CPU; 100 K events/s must be processed at 60% utilization per core. Cores?
19. Chat: 50 M DAU, 20% online at peak, 150 K connections per gateway. Gateways needed (N+1 at 3 AZs)?
20. A write-heavy service stores 400 B/record at 30 K writes/s. Storage per day and per year; Kafka partitions needed at 10 MB/s each if all writes pass through a log?

### C. Requirements drills
21. Write the clarifying-question list (grouped) for “Design an email service.” Mark the five questions that most change the architecture.
22. Write the functional/non-functional requirement summary and the out-of-scope list for “Design a collaborative document editor.”
23. For “Design a stock-trading order book,” which consistency and latency requirements would you probe first and why?

### D. Critique these answers
24. “For the chat app I'll use microservices on Kubernetes with Kafka and Cassandra, sharded by user ID.” List at least six problems.
25. “We have 100 M users so we need to handle 100 M QPS, which needs about 50,000 servers.” Identify the errors and fix the estimate assuming 20 actions per user per day.
26. “To be safe I'll make everything strongly consistent and highly available across five regions.” Explain what is wrong and propose a better statement.
27. The candidate draws 15 components in 10 minutes and the interviewer asks “why is the cache needed?” The candidate says “for performance.” Rewrite the answer with a number.

### E. Design exercises
28. Apply the nine steps (quickly) to “Design a service that returns the current weather for any location.” Produce requirements, estimates, API, architecture, and one deep dive.
29. Redesign the leaderboard for 1 B players and a single global leaderboard. What changes?
30. Take any system from Chapters 6–23 and write its evolution narrative (v1 to v5) with the number that triggers each step.

### F. Teach-back
31. Explain the method to a colleague who has never done a system design interview, in five minutes.

---

## 24.20 Full answer key

**1.** Clarify (5), Estimate (5), Interface (5), Sketch (8–10), Deep dive (12–15), Scale (3–5), Harden (3–5), Trade-offs (continuous), Wrap-up (2). If the sketch consumes 25 minutes, there is no time for the deep dive, failure handling, or trade-offs, which together carry most of the evaluation.

**2.** A complete coarse design shows you can solve the whole problem; depth on a broken or incomplete skeleton earns little and risks leaving core use cases unaddressed. Early depth risks sinking time into the wrong component.

**3.** Examples: (1) Real-time matching latency target? (matching algorithm, geo index); (2) Scale: rides/day and drivers online? (location update rate, sharding); (3) Pricing/ETA accuracy needs? (data sources, compute); (4) Consistency for matching (can two riders get the same driver)? (locks/leases, consensus); (5) Regions/countries? (multi-region, residency).

**4.** The query shapes (lookup key, filters, ordering) the system must serve. They determine primary/partition keys, indexes, and store choice; designing the schema first leads to unanswerable or expensive queries.

**5.** v1: one app server + a database table `code→url`, fine for ~10 K redirects/day. Trigger for v2 (cache/replicas/stateless tier): peak ~350 K redirects/s exceeds one DB (≈ 5–20 K qps); the cache hit ratio math (Zipf) shows how far a cache cuts reads; later triggers: 5-year storage 270 TB → sharded KV store.

**6.** “I'll use Kafka between the API and the senders because notifications arrive in bursts (a 2 M-message campaign) and providers accept only ~5 K msg/s; the log absorbs the burst and lets me add priority topics. The cost is operational complexity and eventual delivery; the alternative, a managed queue like SQS, would be simpler if I don't need replay or very high throughput.”

**7.** (1) Users ≠ QPS → convert via actions/day. (2) Forgetting peak → multiply by 3–5. (3) Missing replication/index/retention → multiply storage. (4) Unit slips (bits/bytes, day/second) → write units on every line. (5) Ignoring fan-out → model downstream amplification. (Also false precision → round.)

**8.** Restate the new numbers, find which component breaks first (likely the data tier), and apply the next rungs: partitioning, regional deployment, cache tiers, async paths, reduced consistency where allowed, cost implications; say what you'd measure first.

**9.** Mid-level: correct components and a working design with basic scaling. Staff-level: identifies the real risks and hardest problem, reasons about cost, migration, multi-region, long-term evolution, org implications, and what not to build.

**10.** The stateless application tier scales horizontally easily; difficulty lives in the data tier (write scale, hot keys), consistency and ordering, fan-out, connection counts, media bandwidth, and failure handling.

**11.** 300 M × 8 = 2.4 B/day ÷ 86,400 = **27.8 K QPS avg**; peak ×3 = **83 K QPS**.

**12.** 50 M × 2 MB = 100 TB/day; ×365 = **36.5 PB/year**; ×3 = **110 PB**; ×1.4 = **51 PB**.

**13.** 100 M hours = 3.6×10¹¹ s × 3 Mbit/s = 1.08×10¹⁸ bits/day ÷ 86,400 = 1.25×10¹³ bit/s = **12.5 Tbit/s** average; peak ×2 = **25 Tbit/s** (served by CDN/edge/ISP caches).

**14.** 50,000 ÷ (2,000 × 0.6) = 41.7 → **42 servers**. AZ-survivable across 3 AZs: two AZs must carry 42 → 21 each → **63 servers** (1.5×).

**15.** 200 M × 0.2 × 2 KB = **80 GB**.

**16.** 9.6 bits × 1 B = 9.6 Gbit = **1.2 GB**.

**17.** 100 M × (100 + 50) B = **15 GB**; with 50% headroom needs 30 GB: fits a 64 GB node (23%).

**18.** Per core: 0.6 × 200 events/s (5 ms each) = 120 events/s → 100,000 / 120 = **833 cores**.

**19.** Concurrent = 50 M × 0.2 = 10 M; ÷ 150 K = 66.7 → **67 gateways**; N+1 across 3 AZs → ×1.5 = **~100 gateways**.

**20.** 30 K × 400 B = 12 MB/s → ×86,400 = **1.04 TB/day**; ×365 = **379 TB/year** (before replication/indexes). Kafka: 12 MB/s ÷ 10 MB/s = 1.2 → 2 partitions minimum; for headroom/consumer parallelism use 6–12.

**21.** Groups: users/scale (accounts, messages/day, mailbox size, attachments), features (send/receive, search, labels, threads, calendar?), protocols (SMTP/IMAP/web), consistency/durability (never lose mail; deletion semantics), spam/abuse, retention/compliance, availability, latency (delivery time). The five most architecture-changing: scale and mailbox size; attachment size; search requirements; durability expectations; spam/abuse scope.

**22.** Functional: create/edit documents, multiple concurrent editors with live cursors, version history, sharing/permissions. Non-functional: ≤ 200 ms perceived edit latency, convergence of concurrent edits, offline support?, durability of every acknowledged edit, 99.9%, 10 M DAU, up to 50 concurrent editors per doc. Out of scope: rich media embedding, comments (v1), spreadsheets.

**23.** Probe: ordering/fairness (strict price-time priority → single-writer sequencing per instrument), latency targets (microseconds–milliseconds), consistency (linearizable order book per symbol, no lost orders), durability (replicated journal before ack), failover (RTO seconds, no divergence), regulation (audit, time sync). They dominate the architecture.

**24.** Problems: (1) no requirements/scale; (2) technology-first answer; (3) microservices/K8s unjustified; (4) sharding by user ID breaks conversation locality and ordering for group chats; (5) no delivery/ordering semantics (Kafka order is per partition); (6) no offline sync/duplicates/acks; (7) no numbers; (8) no failure handling.

**25.** Users ≠ QPS. 100 M users × 20 actions = 2 B/day ÷ 86,400 ≈ **23 K QPS avg**, ~70 K peak. At 2 K QPS per server and 60% utilization → 70,000/1,200 ≈ **58 servers** (about 90 with AZ-survivable capacity), not 50,000.

**26.** Strong consistency across five regions forces cross-region round trips (≥ ~100 ms per write) and reduces availability under partitions (CAP), at high cost; most data doesn't need it. Better: “Strong consistency only for X (e.g., payments, uniqueness) within a region using consensus; eventual consistency with read-your-writes for the rest; two regions active-passive for DR at 99.95%.”

**27.** “The cache is needed because peak redirects are ~350 K/s, a database handles ~10 K/s, and an LRU holding ~10% of URLs reaches ~64% hits (Zipf 0.9) cutting DB reads to ~125 K/s; with a replica fleet or KV store that is within reach, and cache latency of ~1 ms vs ~10 ms improves p99.”

**28 (sketch).** Requirements: lookup by lat/long or city, update every 10 min, 99.9%, 100 K QPS peak, freshness ≤ 15 min. Estimate: 2 K locations × ... cache by rounded geohash (cell ≈ 5 km), TTL 10 min, CDN-able responses; data from provider API with rate limit → batch/cache; deep dive: cache key design (geohash precision) and provider fallbacks.

**29 (sketch).** Single ZSET of 1 B entries ≈ 100 GB: shard by score range into buckets with a count histogram; maintain per-bucket sorted sets; rank = count above bucket + rank inside; top-100 from the top bucket(s); approximate rank for deep positions; batched/streamed updates; consider a log-structured store and periodic rank rebuilds.

**30 (sketch).** Example (photo sharing): v1 single server + Postgres + local disk; v2 object storage + pre-signed uploads (disk fills); v3 CDN (egress bandwidth); v4 async resizing workers (upload latency); v5 sharded metadata, erasure-coded storage tiers (retained size ~EB).

**31 (sketch).** Clarify → estimate → API/data → simplest design → deep dive → scale → failure → trade-offs → wrap-up; drive each decision from a number; state trade-offs as choice/reason/cost/alternative; practice timed and score with the rubric.

---

## 24.21 Flashcards

| Front | Back |
|---|---|
| The nine steps | Clarify, Estimate, Interface, Sketch, Deep dive, Scale, Harden, Trade-offs, Wrap-up |
| 45-minute budget | 5 / 5 / 5 / 10 / 12–15 / 5 / 5 / continuous / 2 |
| Core principle | Breadth first, numbers decide, evolve don't leap |
| Seconds per day | 86,400 (≈ 10⁵); 1 M req/day ≈ 12/s |
| Peak factor | 3× default; 5–10× bursty |
| Storage recipe | writes/day × bytes × 365 × years × 3 (or 1.4 EC) |
| Servers | peak QPS ÷ (capacity × 0.6); ×1.5 for 3-AZ survivability |
| Cache | 10–20% of daily unique hot objects |
| Access patterns | Decide keys, indexes, and store; come before schema |
| Default store | Relational until a number says otherwise |
| Evolution narrative | v1 single box → LB/replicas → cache/CDN → queue → shard → multi-region |
| Trade-off sentence | Choice, reason, cost, alternative/when it changes |
| Deep-dive checklist | Data, scaling, consistency, failure, algorithm, ops, alternatives |
| Five-question bank | Data? Access? 10×? Failure? Cheapest correct? |
| Common failure | Tech-first, no numbers, no failure handling, no trade-offs |
| Rubric | 10 dimensions × 1–5; fix the weakest |
| Leaderboard | Redis ZSET + outbox + `ZADD GT`; shard by leaderboard; bucket histogram at extreme scale |
| App tier | Rarely the hard part; data tier, fan-out, hot keys are |

## 24.22 Glossary additions

| Term | Simple Meaning | Technical Meaning | Why It Exists | Example |
|---|---|---|---|---|
| Back-of-envelope estimate | Rough sizing math | Order-of-magnitude calculation with stated assumptions | Choose architecture | 70 K peak QPS |
| Access pattern | How data is queried | Lookup key, filters, ordering of a query | Drives schema | “latest 50 by conversation” |
| Hot set | Frequently used subset | Working set that should be cached | Cache sizing | Top 20% of profiles |
| Peak factor | Peak ÷ average | Multiplier for burst capacity | Provisioning | 3× |
| N+1 / AZ-survivable | Spare capacity | Capacity to lose one unit/zone | Availability | 1.5× across 3 AZs |
| Evolution narrative | Step-by-step growth | Versions of the design triggered by numbers | Justify complexity | v1…v5 |
| Deep dive | Detailed component design | Mechanism-level analysis of the hardest parts | Show depth | Rank at scale |
| Trade-off sentence | Decision statement | Choice, reason, cost, alternative | Communicate judgment | “X because Y; cost Z” |
| Out of scope | Deliberate exclusions | Features not designed in this iteration | Focus | No video calls |
| Rubric | Scoring guide | Dimensions with level descriptors | Self-assessment | 10 × 5 |

---

**Next: Chapter 25: Problem Book I (Beginner).** URL shortener, Pastebin, rate limiter, file upload service, chat service, and an LRU cache, each solved with the nine-step method, with measured labs for ID generation and cache sizing, rate-limiting algorithms, resumable uploads, chat ordering, and cache policies.

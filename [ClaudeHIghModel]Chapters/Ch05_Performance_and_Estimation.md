# Chapter 5: Performance and Capacity Estimation

## Chapter card

| | |
|---|---|
| **Objective** | Reason about performance with numbers: define latency, throughput, utilization, and tail latency precisely; understand queueing (why systems fall off a cliff near full load); find bottlenecks systematically; test honestly; and master back-of-the-envelope estimation for traffic, storage, bandwidth, memory, and server counts |
| **Why this chapter exists** | Designs without numbers are guesses. Numbers tell you *whether you need a cache, replicas, sharding, or just a bigger machine*, and prevent both under-engineering and over-engineering. This is also the most reliably tested interview skill |
| **Prerequisites** | Chapters 0–4 (latency ladder, Little's Law, CPU vs I/O bound, database capacity) |
| **Study time** | 10–14 hours including labs and drills |
| **Tier** | `[MK]` throughout (queueing theory depth `[GK]`) |
| **Mastery criteria** | (1) Define and distinguish latency, response time, service time, throughput, bandwidth, utilization, saturation. (2) Explain why latency explodes near 100% utilization (queueing) and pick safe utilization targets. (3) Explain tail latency and fan-out amplification; use percentiles correctly. (4) Apply USE/RED to localize a bottleneck. (5) Design an honest load test and explain coordinated omission. (6) Estimate QPS, storage, bandwidth, cache memory, and server/DB counts for a new product in ≤10 minutes, stating assumptions and sanity-checking. (7) Translate numbers into architecture decisions |

---

## 5.1 The Story: "Can We Survive Launch Day?"

You're the tech lead for **StreamBox**, a video platform that is launching next month with a big marketing campaign. The CEO asks three questions:

1. "How many servers do we need on launch day?"
2. "How much will storage cost over the next two years?"
3. "Last week the site felt fine at 60% load and fell over at 90%. Why does 'a little more load' break everything?"

You say: "It depends." The CEO says: "On what? Give me a number."

Your job is to turn vague product statements ("we expect a million users") into **defensible numbers**, and to understand why systems behave non-linearly near their limits.

### Pause & Predict 5.1
> A web service handles each request in exactly 10 ms of work on one server (no parallelism). Requests arrive randomly at an average of 80 per second. What do you expect the *average response time* to be? Now at 90 per second? At 99 per second? Write your guess. (Most people guess 10–15 ms for all three. The real answer is in §5.4.)

---

## 5.2 Vocabulary: be precise `[MK]`

| Term | Definition | Unit | Notes |
|---|---|---|---|
| **Latency** | Time for one operation/request to complete (or the *waiting* component of it, depending on context) | ms | Always specify what's measured: client-observed or server-side? |
| **Service time** | Time actually spent *being processed* (no waiting) | ms | The 10 ms of work above |
| **Queueing (wait) time** | Time spent waiting for a resource | ms | Zero when idle; explodes near saturation |
| **Response time** | Service time + queueing + network = what the user experiences | ms | What SLOs should measure |
| **Throughput** | Completed operations per unit time | req/s, MB/s | Also called "goodput" if only successful ones |
| **Bandwidth** | Maximum data rate of a link | bit/s | Capacity, not achieved speed |
| **Concurrency** | Operations in flight at once | count | Little's Law: `L = λ × W` |
| **Utilization (ρ)** | Fraction of time a resource is busy (or fraction of capacity used) | 0–100% | The key to queueing behavior |
| **Saturation** | Work waiting because the resource is fully used | queue length | Utilization says "busy," saturation says "backed up" |
| **Capacity** | Max sustainable throughput meeting a latency target | req/s | "Capacity" without a latency target is meaningless |
| **Bottleneck** | The resource that limits system capacity | n/a | Improving anything else changes nothing |
| **Headroom** | Capacity minus current load | % | Needed for spikes and failures |
| **QPS/RPS/TPS** | Queries/Requests/Transactions per second | 1/s | Specify *what* is counted |

> **Capacity is a curve, not a number.** A server might do 1,000 req/s at 20 ms p99, 1,200 req/s at 100 ms p99, and 1,250 req/s at 5 s p99. Which is its "capacity"? Whichever satisfies your SLO.

---

## 5.3 Percentiles and tail latency `[MK]`

### 5.3.1 Why averages mislead
Imagine 100 requests: 99 take 20 ms and 1 takes 10 seconds. The average is ~119 ms: it describes *no actual request*. The p50 is 20 ms (typical user), and the p99 (or max) reveals the pain. Latency distributions are **long-tailed** (queues, GC pauses, retries, cold caches, noisy neighbors), so averages hide what hurts.

### 5.3.2 Percentile notation
- **p50 (median):** half of requests are faster.
- **p90, p95, p99, p99.9:** 90%/95%/99%/99.9% of requests are faster than this.
- **p99 = 800 ms** means 1 in 100 requests takes at least 800 ms.
Report p50 **and** p99 (and sometimes p99.9). Don't average percentiles across servers (it's mathematically meaningless); aggregate the raw histograms (e.g., HDR histograms, Prometheus histogram buckets).

### 5.3.3 Tail latency amplification (fan-out) `[MK]`
A request that calls N backends in parallel is as slow as the **slowest** of them.
If each backend is "fast" with probability 0.99 (p99 = its fast threshold), the probability that *all N* are fast is `0.99^N`:

| Fan-out N | P(all fast) | P(at least one slow) |
|---|---|---|
| 1 | 99% | 1% |
| 10 | 90.4% | 9.6% |
| 50 | 60.5% | 39.5% |
| 100 | 36.6% | **63.4%** |
| 500 | 0.7% | 99.3% |

**Consequences**
- At fan-out 100, **a backend's p99 becomes your median.** Search engines scatter-gather over hundreds of index shards; so tail latency of each shard matters enormously.
- A page that issues 40 sequential-or-parallel service calls will almost always hit at least one slow call.
- This is why large systems obsess over p99/p99.9 of *components*, and use mitigations (below).

**Mitigations** `[GK]`
- **Hedged requests:** send the same request to a second replica if the first hasn't answered within, say, the p95 time; take whichever returns first (costs ~5% extra load, slashes tail). `[Public: Google "The Tail at Scale"]`
- **Timeouts + retries to another replica**, **backup requests**.
- **Reduce fan-out** (fewer shards per query, caching, aggregation tiers).
- **Isolation/bulkheads** so a slow tenant doesn't affect others; **load shedding**.
- **Fix the sources:** GC tuning, avoid noisy neighbors, background-task throttling, warm caches.

### 5.3.4 Latency budgets
An end-to-end target (say 300 ms) is split across hops: edge 20 ms, gateway 10 ms, service A 50 ms, DB 30 ms, serialization/network 40 ms... plus slack. Each component gets a **budget**; calls pass remaining **deadlines** downstream (Chapter 1). If the sum of the *p99s* exceeds the target, the design (not just code) must change: fewer sequential hops, caching, or parallelization.

---

## 5.4 Queueing: why systems fall off a cliff `[MK concept, GK math]`

### 5.4.1 The problem (answer to Pause & Predict 5.1)
A single server takes exactly 10 ms per request → capacity 100 req/s. At 80 req/s utilization is 80%. Why isn't the response time just 10 ms?

Because arrivals are **random** (bursty), not evenly spaced. Sometimes two requests arrive within 3 ms of each other; the second must wait. When the server is nearly always busy, the chance that a newcomer finds it busy (and a long line) grows dramatically.

### 5.4.2 The core result
For the classic single-server queue with random (Poisson) arrivals and random (exponential) service times (M/M/1):

> **Average response time = S / (1 − ρ)**
> where S = mean service time and ρ = utilization (λ × S).

Even if service times are *fixed* (M/D/1), the wait is about half as bad, but the shape is the same: the `1/(1−ρ)` blow-up as ρ → 1.

### 5.4.3 What it looks like (simulation results, S = 10 ms, exponential service)
Sample output from the simulation in Lab 1:

| Utilization ρ | Avg response (ms) | p50 (ms) | p99 (ms) | Theory avg `S/(1−ρ)` |
|---|---|---|---|---|
| 30% | 14.3 | 9.9 | 66 | 14.3 |
| 50% | 20.1 | 13.9 | 93 | 20.0 |
| 70% | 33.3 | 23.2 | 151 | 33.3 |
| 80% | 49.7 | 34.9 | 220 | 50.0 |
| 90% | 104.5 | 71.9 | 475 | 100 |
| 95% | 191.7 | 135.4 | 832 | 200 |
| 99% | 573 *(noisy; theory 1,000)* | 462 | 2,086 | 1,000 |

**Read it carefully:**
- From 50% to 80% utilization, average response time goes **2.5×**. From 80% to 95%, **4×** more. The p99 goes from ~93 ms to ~832 ms.
- So at the Pause & Predict numbers (S = 10 ms): 80 req/s → ~50 ms; 90 req/s → ~100 ms; 99 req/s → ~1,000 ms. **"A little more load" can mean 10× worse latency.**
- Tail (p99) is ~4.6× the mean in this model (`−ln(0.01) ≈ 4.6`) and worsens with variability.

```text
 latency
   │                                         ╱
   │                                        ╱
   │                                      ╱
   │                                  ╱─╱          ← the "knee": steep rise
   │                          ___╱───╱
   │ ________________────────╱
   └───────────────────────────────────────▶ utilization
   0%                 50%       70%  80% 90% 100%
```
(That's the CEO's question #3.)

### 5.4.4 Practical consequences `[MK]`
1. **Never plan to run resources near 100%.** Typical *steady-state targets*: CPU 50–70% at peak for latency-sensitive services; databases 50–60%; leave headroom for spikes and for **failure** (if one of N servers dies, the others absorb its load: with N=3, losing one pushes the remaining from 60% → 90%: the knee!).
2. **Variability makes it worse.** Bursty arrivals and variable service times (a mix of 1 ms and 500 ms requests) push the knee earlier. Reduce variance: separate slow and fast request classes into different pools.
3. **Queues amplify failures.** When a dependency slows (S increases), ρ rises *for the same arrival rate* (ρ = λS) — a 2× slowdown at 60% utilization pushes you to 120%: queues grow without bound, timeouts trigger retries, load increases further (retry storm), and the system collapses. This is the mechanism behind many outages.
4. **Autoscaling must react before the knee**, and it has lag (minutes). Scale on leading indicators (queue depth, concurrency, request rate), not only CPU.
5. **Bounded queues + load shedding** protect latency: it's better to reject 5% of traffic quickly (HTTP 503/429) than to make 100% wait 30 seconds. Unbounded queues turn overload into universal timeouts.
6. **Multiple servers help:** M servers sharing one queue (M/M/c) handle higher utilization with less waiting than M separate queues (the "supermarket" lesson: one shared line beats separate lines). Load balancers with least-connections approximate this.

### 5.4.5 Little's Law revisited `[MK]`
`L = λ × W` holds for *any* stable system. Uses:
- **Concurrency planning:** 2,000 req/s × 150 ms = **300** requests in flight; thread pools, DB connections, and file descriptors must accommodate it (+ headroom).
- **Queue sizing:** items waiting = arrival rate × wait time.
- **Throughput from concurrency limit:** a connection pool of 50 with 25 ms queries caps throughput at 50 / 0.025 = **2,000 queries/s** *no matter how many app servers you add*.
- **Detecting trouble:** if `L` (concurrency) climbs while λ is constant, W (latency) rose: something got slower.

### 5.4.6 Amdahl's law and the Universal Scalability Law `[GK]`
**Amdahl:** if a fraction *s* of work is inherently serial, max speedup with N workers = `1 / (s + (1−s)/N)`. With 5% serial (a lock, a single-threaded component, a single DB primary), the ceiling is **20×**, no matter how many servers.
**USL (Gunther):** adds a **coherence** penalty (cost of nodes coordinating): throughput can *decrease* beyond some N. This is why "just add more servers" stops working when servers contend on shared state (a hot DB row, a global lock, a cache invalidation storm).
**Design implication:** scaling is limited by shared, serial resources, and Chapter 6 onward is largely about *removing or partitioning* those.

---

## 5.5 Finding bottlenecks `[MK]`

### 5.5.1 The principle
A system's capacity is set by its **most constrained resource**. Optimizing anything else is wasted effort. Find it by measurement.

### 5.5.2 Resource checklist (ask in order)
1. **CPU:** utilization per core, run-queue length. Hot code? Serialization (JSON), compression, TLS, regexes?
2. **Memory:** used vs available, page faults/swap, GC time/pauses, OOM kills, cache sizes.
3. **Disk I/O:** IOPS, throughput, queue depth, latency (`await`), fsync latency.
4. **Network:** bandwidth, packet loss/retransmits, connection counts, TIME_WAIT, RTT.
5. **Concurrency limits:** thread pools, connection pools, file descriptors, semaphore waits.
6. **Locks/contention:** DB lock waits, mutex contention, hot keys/rows.
7. **Dependencies:** downstream latency/error rates (DB, cache, third parties).
8. **Application logic:** N+1 queries, chatty calls, inefficient algorithms.

### 5.5.3 USE and RED methods
- **USE (for resources — Brendan Gregg):** for every resource check **U**tilization, **S**aturation, **E**rrors.
- **RED (for services — Tom Wilkie):** **R**ate, **E**rrors, **D**uration (latency distribution).
- **Four Golden Signals (Google SRE):** latency, traffic, errors, saturation.
Quick diagnosis template: *Is latency high? → Is the service saturated (queueing)? → Which resource is saturated (USE)? → Which dependency is slow (traces)? → Which code/query (profiler, EXPLAIN)?*

### 5.5.4 Tools of the trade
- **System:** `top/htop`, `vmstat`, `iostat -x`, `sar`, `ss -s`, `perf`.
- **App profiling:** CPU flame graphs (where does CPU time go?), allocation profiles, thread dumps (what are threads blocked on?), async-profiler/JFR for Java.
- **Database:** slow query log, `EXPLAIN ANALYZE`, `pg_stat_statements`, lock views.
- **Distributed tracing:** shows time per hop (Chapter 21).
- **Load generation:** k6, wrk2, Gatling, Locust, JMeter, vegeta.

### 5.5.5 Typical bottleneck → fix map
| Symptom | Likely bottleneck | Typical fixes |
|---|---|---|
| CPU 100%, latency up | Compute | Optimize hot path, cache results, scale out, cheaper serialization |
| Latency up, CPU low, threads blocked | I/O wait / dependency / lock | Timeouts, pool sizing, async I/O, fix slow dependency |
| DB CPU high, same queries repeating | Read load | Cache, replicas, indexes |
| DB lock waits | Write contention | Shorter txns, sharded counters, queues |
| Disk `await` high | Storage | Faster disk, batching, avoid random I/O, more RAM cache |
| Network at line rate | Bandwidth | Compression, CDN, bigger NICs, fewer bytes |
| Memory growth/GC pauses | Leaks, big heaps | Fix leaks, tune GC, bound caches, restart policy |
| Connection errors at spikes | Pool/FD/port exhaustion | Pool sizing, keep-alive, limits |

---

## 5.6 Load testing honestly `[MK]`

### 5.6.1 Types
| Test | Question |
|---|---|
| **Load test** | Does it meet SLOs at expected peak? |
| **Stress test** | Where does it break, and *how* (gracefully or catastrophically)? |
| **Spike test** | What happens with a sudden 10× surge? |
| **Soak/endurance test** | Leaks, slow degradation over hours/days? |
| **Capacity test** | What's the max throughput within the SLO? (find the knee) |
| **Failure/chaos test** | Does it survive losing a node/zone/dependency? |

### 5.6.2 Open-loop vs closed-loop; coordinated omission `[MK]`
- **Closed-loop generator:** N virtual users each send a request, **wait for the reply**, then send the next. If the server stalls, the generator *stops sending*, so it records one slow sample instead of the hundreds of requests that real users would have sent during the stall.
- **Open-loop generator:** requests are issued **on a schedule** (e.g., 100 per second) regardless of replies — like real, independent users. Latency is measured from the **intended send time**.
- **Coordinated omission** (coined by Gil Tene): the measurement error from closed-loop generators that coordinate with the system under test and thereby *omit* the bad data. It makes tail latency look far better than reality.

**Demonstration (Lab 2, real output).** A server with 5 ms service time freezes for 2 seconds in a 10-second test at 100 req/s:
```text
CLOSED loop: 801 samples   p50=5 ms   p99=5 ms     max=2005 ms     ← "p99 = 5 ms!" (wrong)
OPEN   loop: 1000 samples  p50=5 ms   p99=1960 ms  max=2005 ms     ← reality: ~20% of users suffered
```
Same system, same outage. The closed-loop report would pass an SLO review; the open-loop report fails it.
**Rules:** use open-loop (constant arrival rate) tools (wrk2, k6 with `constant-arrival-rate`, Gatling open model, vegeta); measure from intended start; report full histograms; test against production-like data and cache states; generate load from outside the system under test (separate machines).

### 5.6.3 Other pitfalls
- Testing with a hot cache or tiny dataset (unrealistic hit rates).
- Uniform keys when production is skewed (hot keys!).
- Ignoring the client side (generator is the bottleneck).
- Testing a single instance and extrapolating linearly (Amdahl/USL).
- No warm-up (JIT, connection pools, caches).
- Averaging percentiles across runs.
- Not testing failure modes (the knee, the retry storm).

---

## 5.7 The back-of-the-envelope toolkit `[MK]`

### 5.7.1 Why estimate?
- To **choose** architecture (does this fit one DB? one machine? do we need a CDN?).
- To **communicate** scale and cost.
- To **sanity-check** designs and spot order-of-magnitude errors early.
Precision isn't the goal. **Being within 2–3× and explaining your assumptions is.**

### 5.7.2 Numbers to know

**Powers and units**
| Quantity | Value |
|---|---|
| 1 KB / MB / GB / TB / PB | 10³ / 10⁶ / 10⁹ / 10¹² / 10¹⁵ bytes (use powers of 10 for estimating; 2¹⁰ ≈ 10³) |
| Thousand / Million / Billion | 10³ / 10⁶ / 10⁹ |
| Bits vs bytes | 1 byte = 8 bits; link speeds are in **bits**/s: 1 Gbit/s = 125 MB/s |
| Seconds per day | 86,400 ≈ **10⁵** (round; 14% error is fine) |
| Seconds per month | ~2.6×10⁶ (≈ 2.5 M) |
| Seconds per year | ~3.15×10⁷ (≈ 3×10⁷) |
| Days per year | 365 |

**Handy conversions**
- **Requests/s ≈ requests per day ÷ 10⁵.**
- 1 M requests/day ≈ **12 req/s** average.
- 100 M requests/day ≈ **1,200 req/s** average.
- 1 B requests/day ≈ **12,000 req/s** average.
- 1 GB/day ≈ 12 KB/s ≈ 0.1 Mbit/s.
- 1 TB/day ≈ 12 MB/s ≈ 100 Mbit/s.
- 1 PB/day ≈ 12 GB/s ≈ 100 Gbit/s.

**Typical object sizes (order of magnitude)**
| Object | Size |
|---|---|
| Tweet/short message (text + metadata) | 0.3–1 KB |
| Chat message with metadata | 100–200 B |
| User profile record | 1–5 KB |
| URL mapping record | ~500 B |
| GPS telemetry record | 50–200 B |
| Log line | 200–500 B |
| JSON API response (typical) | 2–50 KB |
| Web page (HTML+JS+CSS, compressed) | 0.5–2 MB (with images/media: 2–5 MB) |
| Photo (JPEG, phone) | 2–5 MB original; thumbnail 10–50 KB; web-size 200–500 KB |
| Audio (music, 3 min, 128 kbps) | ~3 MB |
| Video, 1 min | SD ~5–10 MB; HD 1080p ~50–100 MB; 4K ~200–400 MB |
| Video bitrates | 480p ~1 Mbit/s; 720p ~3; 1080p ~5; 4K ~15–25 Mbit/s |

**Typical hardware capability (order of magnitude; workload-dependent)**
| Component | Rough capability |
|---|---|
| App server (simple JSON API) | 1,000–10,000 req/s per instance (less if heavy logic) |
| App server (CPU-heavy) | 50–500 req/s |
| Nginx static/proxy | 10k–50k+ req/s per node |
| Relational DB (well-tuned, cached) | ~10–50k simple reads/s; ~1–10k writes/s (single primary) |
| Redis | 50k–200k+ ops/s per node |
| Kafka broker | 100 MB/s–1 GB/s throughput, 100k–1M msgs/s depending on size |
| Cassandra/Scylla node | 10k–100k+ ops/s |
| NIC | 1 / 10 / 25 / 100 Gbit/s |
| SSD (NVMe) | ~100k–1M IOPS, 2–7 GB/s |
| RAM per server | 64 GB–1 TB; hot working set should fit |
| Data per DB node | ~1–10 TB comfortable; tens of TB with care |

**Rules of thumb**
- **80/20 (Pareto):** 20% of items get ~80% of traffic, so caching ~20% of data can serve ~80% of reads (skew is often steeper).
- **Peak/average ratio:** 2–5× for consumer apps (daily cycle); up to 10×+ for event-driven spikes (flash sales, live sports, New Year's Eve).
- **Read:write ratios:** social/feed ~100:1–1000:1; URL shortener ~100:1; chat ~1:1; telemetry ~1:100 (write-heavy); e-commerce catalog ~1000:1.
- **DAU/MAU:** ~20–50% typical for sticky consumer apps; lower for utilities.
- **Replication factor:** ×3 for durable storage.
- **Storage overhead:** indexes/metadata +30–100%; compression can divide by 2–10×.
- **Growth:** plan for 2–3× per year for startups, 5 years of retention for capacity, and a 20–30% headroom factor.
- **Utilization target:** plan to run at ≤60–70% at peak.

### 5.7.3 The estimation method (use every time) `[MK]`
```text
1. Clarify & assume:     users (total, DAU), actions per user per day, read:write, peak factor,
                         object sizes, retention, replication, growth.
2. Traffic:              daily ops → average QPS (÷10⁵) → peak QPS (× peak factor); split read/write.
3. Storage:              new items/day × size → per day → × retention → × replication (+ index overhead).
4. Bandwidth:            peak QPS × payload size (in and out) → convert to bits/s (×8).
5. Memory (cache):       hot fraction (20%) × daily data or active-set size.
6. Servers/DB nodes:     peak QPS ÷ per-node capacity → ÷ target utilization (0.6) → +N+1 redundancy.
7. Connections/concurrency:  QPS × latency (Little's Law).
8. Conclusion:           which components are needed, what bottlenecks first, what to revisit.
```
**Write the arithmetic visibly. Say assumptions out loud. Round aggressively. Sanity-check against known systems.**

---

## 5.8 Worked estimations

### 5.8.1 URL shortener
**Assumptions:** 100 M new URLs/month; read:write = 100:1; record 500 B; keep 5 years; peak factor 5; replication 3.
- **Write QPS:** 100 M / 2.6×10⁶ s ≈ **40/s** average; peak ≈ 200/s.
- **Read QPS:** ×100 = **4,000/s** average; peak ≈ **20,000/s**.
- **Storage:** 100 M × 12 × 5 = 6 B URLs × 500 B = **3 TB**; ×3 replication = **9 TB**; with index overhead ≈ 12–15 TB.
- **Bandwidth:** reads 20,000/s × ~0.5 KB (redirect response small, headers dominate ≈ 500 B) ≈ 10 MB/s ≈ **80 Mbit/s**: trivial.
- **Cache:** daily reads 4,000 × 10⁵ = 4×10⁸; assume heavy skew, hot set 20% of the 4×10⁷ distinct daily URLs ≈ 8×10⁶ × 500 B = **4 GB** → one Redis node.
- **App servers:** 20,000 peak / (5,000 per node × 0.6 utilization) ≈ **7 nodes** (+ redundancy → 8–10).
- **Conclusion:** the entire dataset (3 TB) fits on one modern DB node; reads are served by cache and replicas; **no sharding needed initially** (consider for headroom or write availability). The design's challenge is availability and key generation, not raw scale.

### 5.8.2 Twitter-like feed
**Assumptions:** 300 M MAU, 150 M DAU; each DAU opens the feed 10×/day (each load = 1 timeline read) and 20% post 1.5 tweets/day on average; tweet = 1 KB metadata+text; 10% have a 500 KB image; retention indefinite; replication 3.
- **Timeline reads:** 150 M × 10 = 1.5 B/day → **15,000/s** avg; peak ×3 ≈ **45,000/s**.
- **Tweets written:** 150 M × 20% × 1.5 = 45 M/day → **~450/s** avg; peak ~1,500/s.
- **Read:write** ≈ 33:1 by operation count (heavily read-skewed).
- **Text storage:** 45 M × 1 KB = 45 GB/day → 16 TB/year → ×3 ≈ **50 TB/year**.
- **Media:** 4.5 M images/day × 500 KB = **2.25 TB/day** → ~0.8 PB/year (×3 = 2.5 PB): **media dominates storage (50×)** → object storage + CDN.
- **Fan-out writes (push model):** avg 200 followers → 45 M × 200 = **9 B timeline inserts/day ≈ 100,000/s** avg (peak 300k/s) — the real write load, 200× the tweet write rate; a celebrity with 50 M followers = 50 M inserts for one tweet → hybrid push/pull (Chapter 26).
- **Timeline cache memory:** keep 800 tweet IDs (8 B each) for active users: 100 M × 800 × 8 B ≈ **640 GB** → a Redis cluster of ~10–20 nodes (with replication, more).
- **Conclusion:** DB sizing is not the story; **fan-out and media delivery** are.

### 5.8.3 Chat / messaging
**Assumptions:** 500 M DAU, 40 sent messages/day each, 150 B/message with metadata; 1:1 mostly; 5% media (200 KB avg); peak ×3; concurrent connections = 30% of DAU at peak.
- **Messages/s:** 500 M × 40 = 2×10¹⁰/day → **200,000/s** avg; peak **~600,000/s**.
- **Text storage:** 2×10¹⁰ × 150 B = **3 TB/day** → 1.1 PB/year (×3 = 3.3 PB).
- **Media:** 5% × 2×10¹⁰ = 10⁹ media/day × 200 KB = **200 TB/day**.
- **Concurrent connections:** 150 M persistent connections; at 100k per gateway node ⇒ **1,500 gateway nodes** (+headroom); memory per connection ~20–50 KB → 150 M × 30 KB ≈ 4.5 TB RAM across the fleet.
- **Conclusion:** the dominant challenges are **connection count, fan-out, and storage volume**, not request rate per se.

### 5.8.4 Video streaming (YouTube/Netflix-like)
**Assumptions:** 100 M DAU watching 45 min/day on average; average stream 3 Mbit/s; uploads: 500 hours/min.
- **Egress:** 100 M × 45 × 60 s × 3 Mbit/s = 100 M × 8.1 Gbit = 8.1×10¹⁷ bits/day ≈ 1.0×10¹⁷ bytes = **~100 PB/day**; average 100 PB / 86,400 ≈ **1.17 TB/s ≈ 9.3 Tbit/s average**, peak ×2–3 ⇒ **20–30 Tbit/s**. Only a **CDN** with thousands of edge servers (often inside ISPs) can serve this.
- **Peak sanity check:** the average-based estimate (× 2–3 peak factor) gives **20–30 Tbit/s**. A different route: if 20% of DAU were watching at the same instant (20 M streams × 3 Mbit/s) you'd get **60 Tbit/s**. The 2–3× gap shows how sensitive these results are to the concurrency assumption; in an interview, state which one you chose and why (here: average × peak factor).
- **Uploads:** 500 h/min × 1,440 min = **720,000 hours/day**; at ~1 GB per hour for one stored rendition ≈ 720 TB/day; with ~5 renditions plus the original, roughly **1.5–2 PB/day** of new storage (before replication). Transcoding compute: ~10–20 CPU-hours per video-hour across renditions ⇒ ~10⁷ CPU-hours/day ⇒ ~400k cores continuously... *(order-of-magnitude illustration; real systems use hardware encoders and smarter ladders.)*
- **Conclusion:** bandwidth and storage dominate; CDN and transcoding pipelines are the design.

### 5.8.5 Telematics ingestion (your domain)
**Assumptions:** 5 M vehicles; 20% active at any moment (1 M); GPS+engine record = 150 B every 5 s; batched upload every 60 s (12 records/batch) over cellular; peak ×3 (shift starts, reconnect bursts); retention: raw 30 days hot, 1-minute rollups for 2 years, raw cold 1 year; replication 3.
- **Records/s:** 1 M / 5 s = **200,000/s**; peak 600,000/s.
- **Ingest messages/s (batched):** 1 M devices / 60 s ≈ **16,700 batches/s** (each ~1.8 KB + protocol overhead): the gateway/connection rate is ~12× lower than the record rate: *batching is a major lever*.
- **Ingest bandwidth:** 200,000 × 150 B = **30 MB/s** (240 Mbit/s); peak ~90 MB/s.
- **Raw storage/day:** 30 MB/s × 86,400 = **2.6 TB/day**; compressed 5–10× ⇒ ~0.3–0.5 TB/day. 30-day hot: ~13 TB compressed (×3 = ~40 TB).
- **Rollups:** 5 M vehicles × 1,440 min/day × 100 B ≈ 0.7 TB/day if all rolled up (only active minutes in practice) ⇒ 2 years ≈ 500 TB: consider coarser rollups (5-min/hourly) for older data.
- **Stream (Kafka-like) sizing:** 30 MB/s average ×3 replication ⇒ 90 MB/s disk write; a 6-broker cluster with room for peaks and 7 days retention: 30 MB/s × 604,800 s = 18 TB ×3 = 54 TB.
- **Live "latest state" store:** 5 M vehicles × 500 B = **2.5 GB**: trivially in RAM.
- **Cellular data cost:** 12 records × 150 B = 1.8 KB per minute per active vehicle ≈ **108 KB/hour** raw (≈ 20 MB per vehicle per month at ~200 driving hours). Across 1 M simultaneously active vehicles that is ~30 MB/s, matching the ingest figure above. Compression/delta encoding on-device can cut this several-fold: a real cost lever tied to firmware design, since cellular data is metered per device.
- **Conclusion:** ingest is steady and moderate in bytes/s but high in records/s; the hard parts are **connection management, out-of-order/duplicate data, and tiered retention**, not any single big number.

### 5.8.6 Ride-hailing location updates
**Assumptions:** 5 M active drivers at peak, location update every 4 s, 100 B each; riders requesting trips: 1 M/hour peak.
- **Location writes:** 5 M / 4 = **1.25 M updates/s** → 125 MB/s. Too much for a typical relational DB; keep **latest location in memory/geo-indexed store** (Redis/in-memory grid, partitioned geographically), stream to cold storage sampled.
- **Memory:** 5 M × 100 B = 500 MB for current locations.
- **Trip requests:** 1 M/h = **280/s**: tiny; each triggers a proximity query: easy relative to location ingest.
- **Insight:** *writes of ephemeral state dominate* → in-memory + partitioned by geography.

### 5.8.7 Rate limiter for an API gateway
**Assumptions:** 200,000 API requests/s at peak, each checked against a per-user limit in Redis (1 INCR/check or 1 Lua call).
- **Redis ops/s:** 200,000 → at ~80k ops/s per node with 50% headroom (40k usable) ⇒ **5 nodes** (sharded by user ID) + replicas for failover.
- **Memory:** 10 M active users × 100 B ≈ 1 GB.
- **Latency:** +0.5–1 ms per request (network hop to Redis): consider a local pre-check cache.

### 5.8.8 Search autocomplete
**Assumptions:** 100 M DAU; each performs 5 searches/day, ~8 keystrokes triggering suggestions per search (with debouncing, say 4 calls) ⇒ 20 calls/user/day.
- **Requests:** 100 M × 20 = 2 B/day ≈ **23,000/s** avg; peak ×4 ≈ 90,000/s.
- **Index:** top 10 M query strings × 30 B avg ≈ 300 MB raw; a trie with top-K precomputed per node ≈ 1–5 GB: fits in memory on each of many replicas; update by periodic rebuild (hourly/daily) from query logs.
- **Latency target:** < 100 ms end-to-end; edge caching of popular prefixes (short prefixes like "a", "ap" dominate) can serve a large share.

---

## 5.9 From numbers to decisions `[MK]`

| If the estimate shows… | Then consider… |
|---|---|
| Avg QPS < 100, data < 100 GB | One app server + one DB. Don't over-engineer; add backups/monitoring/HA basics |
| Peak read QPS > 5–10k on one DB | Cache + read replicas |
| Peak write QPS > ~5–10k for one primary | Batching/async writes, queue buffering, vertical scale, then sharding or LSM/wide-column store |
| Data > ~5–10 TB for relational | Archive/partition (time), replicas for reads, consider sharding or a different store |
| Large media / > few TB/day egress | Object storage + CDN |
| Persistent connections > 100k | Dedicated gateway tier (event-loop servers), connection registry |
| p99 target tight with fan-out > 20 | Reduce fan-out, hedge requests, cache, tiered aggregation |
| Needed availability ≥ 99.99% | Multi-AZ redundancy, no SPOF, failover automation, load shedding |
| Bursty writes beyond steady capacity | Queue/stream as shock absorber with consumers sized for steady state |
| Global users, tight latency | Regional deployment/CDN/edge; data locality strategy |

**Cost sanity (rough, 2020s cloud on-demand; verify current pricing):** storage in object stores ~$20/TB-month; block SSD ~$80–100/TB-month; compute ~$30–40/vCPU-month (varies hugely); **egress ~$50–90/TB** from big clouds (before volume discounts) — egress often dominates media businesses; cross-AZ traffic ~$10/TB each direction. Always compute: *annual storage*, *annual egress*, *compute fleet*.

---

## 5.10 Common estimation errors (and how to catch them) `[MK]`

1. **Per-day vs per-second confusion.** Convert early; write units.
2. **Average vs peak.** Servers must meet peak (and survive failure at peak).
3. **Bits vs bytes.** 1 Gbit/s ≈ 125 MB/s, not 1 GB/s. Network figures ×8.
4. **Forgetting replication, indexes, metadata, backups.**
5. **Forgetting fan-out / write amplification** (feeds, notifications, replication).
6. **Forgetting retention × growth** (5 years of 3× per year growth is not linear).
7. **Using decimal precision** (don't compute 86,400 exactly; use 10⁵).
8. **Confusing users with DAU with concurrent users.** Concurrent ≈ DAU × (fraction online at peak), usually 5–30%.
9. **Ignoring object-size skew** (media vs text).
10. **Assuming linear scaling** (Amdahl/USL).
11. **Planning for 100% utilization.** Use 60–70%.
12. **No sanity check.** Compare with known quantities: "YouTube serves on the order of ~100 PB/day? Plausible for Tbit/s-scale CDN" or "my numbers say one DB handles 10 M writes/s? Impossible; recheck."

**Sanity-check techniques:** compare to well-known scales (Twitter ~hundreds of millions of tweets/day; WhatsApp ~tens of billions of messages/day `[Public, approximate]`); check units by dimensional analysis; ask "is the number per-node plausible?" (a single server doing 10⁶ writes/s to a RDBMS isn't).

**In interviews:** state assumptions and invite correction ("assuming 100 M DAU... does that seem reasonable?"), show the arithmetic, round freely, finish with an architectural conclusion. Spend ~3–5 minutes; don't polish.

---

## 5.11 Capacity planning in practice `[GK]`

- **N+1 / N+2 redundancy:** provision so the loss of one (or two) nodes/zones leaves capacity ≥ peak. With 3 AZs and one AZ lost, each remaining AZ must handle 1.5× its normal share → run each at ≤ ~65% at peak.
- **Headroom for deploys:** rolling deploys remove capacity temporarily.
- **Autoscaling:** scale-out latency (VM boot, image pull, cache warm-up) is minutes: pre-scale for known events; scale on leading metrics; set min/max; guard against thrash.
- **Load shedding & priorities:** protect critical paths (checkout) over optional (recommendations).
- **Forecasting:** use historical growth + launch calendar; review quarterly; track "time to exhaustion" per resource (disk, connections, IOPS).
- **Cost modeling:** unit economics (cost per 1,000 requests/stream hour/active user) guides design trade-offs (cache more? compress more? tier storage?).
- **Performance regressions:** track p50/p99 and throughput per release; canary deploys with automated rollback.

---

## 5.12 Interview perspective
- Begin estimation after clarifying requirements; ask for or state **DAU, actions/user, read:write, object sizes, retention**.
- Show a **repeatable structure**: traffic → storage → bandwidth → memory → servers → conclusion.
- Link numbers to design choices: "40k reads/s means one DB won't do; I'll add a cache with ~90% hit ratio, leaving 4k/s for replicas."
- Mention **peak vs average**, **replication**, **fan-out**, **utilization headroom**.
- For latency, give a **budget** and identify the critical path.
- Common follow-ups: "What if traffic doubles?" (find the next bottleneck), "What happens when a node dies at peak?" (N+1), "How do you test it?" (open-loop load test, find the knee).

---

## 5.13 Labs

### Lab 1: Queueing simulation — see the knee (Java 17+)
```java
import java.util.*;

public class QueueSim {
    public static void main(String[] args) {
        Random r = new Random(42);
        double serviceMs = 10.0;                       // mean service time (exponential)
        int n = 400_000;
        System.out.println("util   avg_resp(ms)  p50    p99    theory_avg(S/(1-rho))");
        for (double rho : new double[]{0.30, 0.50, 0.70, 0.80, 0.90, 0.95, 0.99}) {
            double lambda = rho / serviceMs;           // arrivals per ms
            double t = 0, serverFreeAt = 0;
            double[] resp = new double[n];
            for (int i = 0; i < n; i++) {
                t += -Math.log(1 - r.nextDouble()) / lambda;          // Poisson arrivals
                double start = Math.max(t, serverFreeAt);              // wait if busy (FIFO)
                double service = -Math.log(1 - r.nextDouble()) * serviceMs;
                serverFreeAt = start + service;
                resp[i] = serverFreeAt - t;                            // queueing + service
            }
            double sum = 0; for (double x : resp) sum += x;
            Arrays.sort(resp);
            System.out.printf("%.2f   %8.1f   %7.1f %7.1f   %8.1f%n",
                rho, sum / n, resp[n / 2], resp[(int) (n * 0.99)], serviceMs / (1 - rho));
        }
    }
}
```
**Experiments**
1. Run it; match the table in §5.4.3 (the 99% row is noisy because convergence is slow near saturation).
2. Change service times to **constant** (`double service = serviceMs;`) and compare: waits drop (M/D/1) but the knee remains.
3. Make service times bimodal (90% at 5 ms, 10% at 55 ms; same 10 ms mean) and see how p99 worsens at the same utilization: **variance hurts**.
4. Add a second server (two parallel servers, shared queue) at the same *per-server* utilization and compare. Why is it better?
5. Add a **bounded queue** (reject if more than 50 requests are waiting) and measure the reject rate vs the latency of accepted requests at 95–120% offered load: that's **load shedding**.

### Lab 2: Coordinated omission — closed vs open loop
```java
import java.util.*;

public class CoordinatedOmission {
    static double serverFree = 0;
    static final double STALL_START = 5000, STALL_END = 7000, SERVICE = 5;

    /** Single FIFO server with 5 ms service time that is frozen between 5 s and 7 s. Returns completion time. */
    static double serve(double arrival) {
        double begin = Math.max(arrival, serverFree);
        if (begin >= STALL_START && begin < STALL_END) begin = STALL_END;      // frozen: can't start work
        else if (begin < STALL_START && begin + SERVICE > STALL_START) begin = STALL_END; // would straddle the stall
        serverFree = begin + SERVICE;
        return serverFree;
    }
    static double pct(List<Double> l, double p) { List<Double> c = new ArrayList<>(l); Collections.sort(c); return c.get((int) Math.min(c.size() - 1, c.size() * p)); }

    public static void main(String[] args) {
        // CLOSED LOOP: one virtual user sends the next request only after the previous one returns
        serverFree = 0;
        List<Double> closed = new ArrayList<>();
        for (double t = 0; t < 10000; ) {
            double end = serve(t);
            closed.add(end - t);
            t = Math.max(end, t + 10);                    // intended rate: one request per 10 ms
        }
        // OPEN LOOP: requests arrive on a schedule (every 10 ms) regardless of how the server is doing
        serverFree = 0;
        List<Double> open = new ArrayList<>();
        for (double s = 0; s < 10000; s += 10) open.add(serve(s) - s);

        System.out.printf("CLOSED loop: %d samples  p50=%.0f ms  p99=%.0f ms  max=%.0f ms%n",
            closed.size(), pct(closed, .5), pct(closed, .99), Collections.max(closed));
        System.out.printf("OPEN   loop: %d samples  p50=%.0f ms  p99=%.0f ms  max=%.0f ms%n",
            open.size(), pct(open, .5), pct(open, .99), Collections.max(open));
    }
}
```
Expected output (matches §5.6.2): closed p99 = 5 ms vs open p99 ≈ 1,960 ms. **Questions:** Which would your dashboard show if your load test tool were closed-loop? What does a real user experience? How do `wrk2`/`k6` constant-arrival-rate executors avoid this?

### Lab 3: Estimation workbench (Java; a reusable calculator)
```javapublic class Estimator {
    static final double SEC_PER_DAY = 1e5;       // the 10^5 approximation

    record Result(double avgQps, double peakQps, double storagePerDayGB, double retainedTB,
                  double peakReadMbps, double cacheGB, double appServers) {}

    /**
     * @param dailyOps            total operations per day (reads + writes)
     * @param readFraction        fraction of operations that are reads (e.g. 0.99)
     * @param bytesPerWrite       stored bytes per write
     * @param bytesPerRead        response bytes per read
     * @param retentionDays       how long data is kept
     * @param replication         copies kept (e.g. 3)
     * @param distinctItemsPerDay distinct items read per day (the "active set" before skew)
     * @param hotFraction         fraction of that active set worth caching (e.g. 0.2)
     * @param perServerQps        QPS one app server can sustain (from a load test)
     * @param targetUtil          planned peak utilization (e.g. 0.6)
     */
    static Result estimate(double dailyOps, double peakFactor, double readFraction,
                           double bytesPerWrite, double bytesPerRead, double retentionDays,
                           double replication, double distinctItemsPerDay, double hotFraction,
                           double perServerQps, double targetUtil) {
        double avg = dailyOps / SEC_PER_DAY;
        double peak = avg * peakFactor;
        double storDayGB = dailyOps * (1 - readFraction) * bytesPerWrite / 1e9;
        double retTB = storDayGB * retentionDays * replication / 1e3;
        double readMbps = peak * readFraction * bytesPerRead * 8 / 1e6;
        double cacheGB = distinctItemsPerDay * hotFraction * bytesPerRead / 1e9;
        double servers = Math.ceil(peak / (perServerQps * targetUtil)) + 1;      // +1 for N+1
        return new Result(avg, peak, storDayGB, retTB, readMbps, cacheGB, servers);
    }

    public static void main(String[] args) {
        // URL shortener: 4,000 reads/s + 40 writes/s on average  ->  ~4.04e8 operations/day
        Result r = estimate(4.04e8, 5, 0.99, 500, 500, 365 * 5, 3, 4e7, 0.2, 5000, 0.6);
        System.out.printf("avg QPS = %.0f   peak QPS = %.0f%n", r.avgQps(), r.peakQps());
        System.out.printf("storage/day = %.1f GB   retained (5y, x3) = %.1f TB%n", r.storagePerDayGB(), r.retainedTB());
        System.out.printf("peak read bandwidth = %.0f Mbit/s   cache = %.1f GB   app servers = %.0f%n",
                          r.peakReadMbps(), r.cacheGB(), r.appServers());
    }
}
```
**Expected output** (matches §5.8.1 within rounding): ~4,000 avg QPS, ~20,000 peak QPS, ~2 GB/day of new data, ~11 TB retained over 5 years with 3× replication (the hand estimate said 9 TB; the difference is 4 M writes/day ≈ 1.46 B/year vs the 1.2 B/year assumed by the monthly figure), ~80 Mbit/s peak read bandwidth, ~4 GB cache, ~8 app servers.

**Exercises:** feed it the assumptions from §5.8.2–5.8.5 (adjust the function for media vs text and fan-out) and compare with the hand calculations. Then add a *cost* estimate (storage + egress + compute) with explicit price assumptions.

### Lab 4: Measure a real service's knee (needs `wrk2` or `k6`)
1. Run your Chapter 2 task API (or any HTTP service) locally; add `Thread.sleep(5)` per request to simulate work, with a fixed thread pool of 16.
2. Use an **open-loop** tool: `wrk2 -t2 -c64 -d30s -R 1000 --latency http://localhost:8080/v1/tasks` and then step `-R` through 500, 1000, 2000, 3000, 3200, 3500.
3. Plot p50/p99 vs offered rate. Where is the knee? Compare with capacity = threads / service time = 16 / 0.005 = 3,200 req/s.
4. Repeat with a pool of 4 and verify the knee moves to 800 req/s (Little's Law).

---

## 5.14 Common misconceptions

1. **"Average latency is fine to report."** Use percentiles, aggregated from histograms.
2. **"Doubling servers doubles capacity."** Only if no shared bottleneck (Amdahl/USL).
3. **"90% CPU means we're efficient."** It means you're on the knee: latency is about to explode.
4. **"Latency grows linearly with load."** It's hockey-stick shaped.
5. **"Throughput and latency are independent."** They're linked by queueing and Little's Law.
6. **"A load test that passes proves capacity."** Only if open-loop, realistic, and including failure/skew.
7. **"Bandwidth fixes latency."** Not for small, chatty transfers; distance and round trips dominate.
8. **"p99 only affects 1% of users."** With fan-out and multiple requests per session, most users encounter p99 regularly.
9. **"Estimation must be precise."** Order of magnitude plus clear assumptions is the goal.
10. **"Plan for average load."** Plan for peak plus failure.
11. **"If it's I/O bound, add CPU."** Wrong bottleneck.
12. **"More threads/connections = more throughput."** Past the bottleneck, it just adds contention.

---

## 5.15 Chapter summary

- **Define terms precisely:** latency, service vs response time, throughput, bandwidth, utilization, saturation, capacity (always relative to a latency target).
- **Percentiles, not averages;** **fan-out amplifies tails** (`1−0.99^N`); mitigate with hedging, fewer hops, isolation.
- **Queueing:** response time ≈ `S/(1−ρ)`; the knee arrives around 70–80% utilization; plan for ≤60–70% at peak with N+1; variability and slow dependencies push you off the cliff; bounded queues and load shedding protect latency.
- **Little's Law** links concurrency, throughput, and latency; pool sizes cap throughput; **Amdahl/USL** explain why scaling stalls on shared resources.
- **Find bottlenecks** with USE/RED/golden signals; fix the constrained resource only.
- **Load test open-loop** from the intended send time; avoid coordinated omission; use realistic data/skew; test failures.
- **Estimate** with 10⁵ s/day, peak factors, object sizes, replication, retention; produce traffic → storage → bandwidth → memory → servers → conclusion; sanity-check; **let numbers choose architecture**.

### Mental models
| Concept | Mental model |
|---|---|
| Utilization/queueing | A highway: fine at 50% full, gridlock at 95% — small increases cause huge delays |
| Tail latency | A group dinner: everyone waits for the slowest eater |
| Little's Law | Customers in a shop = arrival rate × time each stays |
| Amdahl | The one-lane bridge on a 10-lane highway |
| Open vs closed loop | Real customers keep arriving vs a test that politely waits for the shop to reopen |
| Back-of-envelope | A map sketch, not a survey: to decide the route, not to pour concrete |

---

## 5.16 Exercises

### A. Conceptual
1. Distinguish latency, service time, response time, and queueing time. Why is capacity meaningless without a latency target?
2. Why do averages hide problems? What's wrong with averaging p99s from multiple servers?
3. A page makes 30 parallel backend calls, each with p99 = 200 ms and p50 = 20 ms. Estimate the probability the page is not slowed by a "p99-slow" call. What do hedged requests do about this?
4. Explain why a server at 90% utilization has much worse latency than at 60%, even though the average arrival rate is steady.
5. A service runs at 60% CPU with 3 servers. One server fails. What utilization do the survivors reach? What do you conclude about N+1 sizing?
6. Why does a 2× slowdown of a dependency risk a total outage even without increased traffic? (Use ρ = λS.)
7. State Little's Law and give two uses.
8. Explain Amdahl's law with a 10% serial fraction: what's the maximum speedup?
9. List the USE method's three questions for each resource, applied to a disk.
10. What is coordinated omission? Why do closed-loop tests hide tail latency?
11. List five load-testing pitfalls.
12. Why use 10⁵ seconds/day? What's the error?
13. 1 Gbit/s equals how many MB/s? Why is this often mis-stated?
14. Why should you plan ≤60–70% utilization at peak?
15. What is load shedding and why is it better than unbounded queues?

### B. Calculations (do these on paper, then check)
16. A service has 12 ms average CPU per request on 8 cores. (a) Max throughput if CPU-bound? (b) At 70% target utilization, how many req/s per server? (c) For 30,000 peak req/s how many servers (+1)?
17. A thread pool of 200 serves requests averaging 80 ms. Max throughput? If latency doubles under load, what happens?
18. A DB connection pool of 40; queries average 5 ms. Max query throughput? How many app instances are needed if each instance does 600 qps of DB work and the pool is shared via a pooler?
19. 50 M DAU × 12 requests/day. Average and peak (×4) RPS. Servers needed at 4,000 RPS per server and 60% utilization (+1)?
20. A photo app: 8 M uploads/day × 3 MB; thumbnails (20 KB) ×4 sizes. Storage/day, per year, with replication ×3. Upload bandwidth at peak (×3 average)?
21. Reads of those photos: 20× uploads, average 250 KB per view. Egress per day? Average and peak Gbit/s? What would that cost per month at $0.05/GB egress (ignoring CDN discounts)?
22. A chat app: 200 M DAU, 30 messages/day, 120 B/message. Writes/s avg and peak (×3)? Storage/year (×3 replication)?
23. Cache sizing: 300 M items, 2 KB each, hot set 15% → memory? How many 128 GB cache nodes with 30% overhead?
24. Kafka-like stream: 25 MB/s ingress, retention 3 days, replication 3. Disk needed?
25. Fan-out: 120 M DAU post 2 items/day on average (20% of users post), average 300 followers, push model. Timeline inserts/s average? Peak ×4?
26. A rate limiter checks 150,000 req/s; each check = 1 Redis op; Redis node does 100k ops/s; target 50% utilization. Nodes?
27. Latency budget: target 400 ms p99. The path: CDN 30 ms, gateway 15 ms, service A 60 ms, service B 80 ms (sequential after A), DB 40 ms (inside B), serialization/network overhead 50 ms. Total? How would you meet the target if it's 300 ms?
28. Queueing: S = 20 ms. Using `S/(1−ρ)`, compute avg response at ρ = 50%, 75%, 90%, 95%. At what ρ does avg response reach 10×S?

### C. Estimation exercises (full method)
29. **Pastebin:** 5 M new pastes/day, avg 10 KB, read:write 20:1, retention 1 year, peak ×4, replication ×3. Full estimate and architecture implications.
30. **Notification system:** 300 M DAU, 8 notifications/user/day (push 70%, email 20%, SMS 10%), peak ×6 around events. Rates per channel; provider throughput; queue sizing (30 min of peak backlog).
31. **Typeahead:** 200 M DAU, 6 searches/day, 5 suggestion calls per search, peak ×3. QPS, edge-cache benefit if 60% hit, backend QPS.
32. **Web crawler:** crawl 1 B pages/month, avg page 100 KB (stored compressed 30 KB), 5 fetchers per second per... derive: pages/s, bandwidth, storage/month and 2-year, number of fetch workers if each handles 20 pages/s.
33. **Video upload:** 300 hours uploaded/min, source ~1.5 GB/hour; 6 renditions totaling ×2 the source size. Daily storage, replication ×3 (originals) / ×2 (renditions)? Transcoding: 1 CPU-hour per source-hour per rendition: CPU-hours/day and cores needed for real-time-ish processing.
34. **Ride-hailing:** 3 M drivers online at peak, location every 5 s, 80 B; 400 k trip requests/hour. Write QPS, bandwidth, memory for current locations, and the proximity query rate.

### D. Debugging
35. A team's p99 is 3 s at 70% CPU and fine at 50%. They propose upgrading the CPU. Evaluate; what else could cause it? (Think queueing, variance, GC, lock contention, dependency.)
36. Autoscaling policy: scale out at CPU > 80% with 5-minute instance boot. Traffic ramps 3× in 2 minutes during a promotion. What happens? Redesign the policy.
37. A load test reports p99 = 120 ms and the team signs off; production p99 during peak is 2.4 s. List five reasons the test misled.
38. A service has 3 replicas at 55% each. After a deploy that does a rolling restart of one at a time, error rates spike. Why, and what are the fixes (surge capacity, readiness gates, connection draining)?
39. Throughput plateaus at 2,500 req/s although CPU is 30% on every server and the DB CPU is 20%. Find the likely limits (pools, locks, single-threaded component, rate limits, NIC) and the experiments you'd run.

### E. Interview-style
40. "Estimate the storage and bandwidth requirements for Instagram." (Give a 5-minute structured answer.)
41. "How would you determine how many servers you need for Black Friday?"
42. "Our service latency spikes when we're at 85% utilization. Why, and what do we do?"

### F. Teach-back
43. Explain to a non-engineer why a restaurant that is 95% full has long waits, then map that to servers.

---

## 5.17 Full answer key

**1.** Latency (generic) = time for an operation; service time = time being processed; queueing time = waiting; response time = queueing + service (+ network) as experienced. Capacity without a latency target is meaningless because throughput can be pushed higher by letting latency explode.

**2.** Averages mix typical and rare slow events and describe no real request; a few outliers can ruin the experience. Percentiles of percentiles are not defined (you can't derive an overall p99 from per-server p99s); aggregate histograms/buckets instead.

**3.** P(all 30 are faster than their p99) = 0.99³⁰ ≈ **74%**, so ~26% of page loads include at least one p99-slow call. Hedged requests send a duplicate request after a short delay (e.g., the p95 latency), so a slow replica rarely determines the result, cutting tail latency at small extra load.

**4.** Random arrivals create bursts; the chance that a request finds the server busy rises with utilization, and the queue length grows like ρ/(1−ρ). At 60%: avg response = S/0.4 = 2.5S; at 90%: S/0.1 = 10S, four times worse.

**5.** Load per server rises from 60% to 60% × 3/2 = **90%** → on the knee; latency ~4× worse. Capacity planning must ensure that after a failure, the remaining nodes stay ≤ ~70%; so run 3 servers at ≤ ~47%, or use more, smaller nodes (e.g., 6 nodes at 60%: losing one gives 72%).

**6.** ρ = λS. If S doubles at constant λ, ρ doubles: 60% → 120% > 100%, so the queue grows without bound; timeouts trigger retries which further increase λ, forming a death spiral.

**7.** L = λ × W (average number in system = arrival rate × average time in system). Uses: (1) size thread/connection pools and concurrency limits; (2) detect trouble: if L rises at constant λ, latency W rose; (3) compute throughput ceiling = pool size / latency.

**8.** Max speedup = 1/s = 1/0.10 = **10×** regardless of workers.

**9.** For a disk: **Utilization** (% time busy / IOPS used vs limit), **Saturation** (queue depth, `await`/wait time), **Errors** (I/O errors, SMART warnings, retries).

**10.** Coordinated omission: a measuring client that waits for each response before sending the next effectively pauses the load during server stalls, so the stall appears as a single slow sample instead of the many slow requests real users would have experienced; this underestimates tail latency drastically.

**11.** Examples: hot cache/small dataset; uniform keys vs real skew; generator is the bottleneck; no warm-up; closed-loop measurement; testing one instance and extrapolating; ignoring failure cases; averaging percentiles; test environment unlike production.

**12.** Because 86,400 ≈ 10⁵ makes mental arithmetic simple; the error is ~16% (high), well within estimation tolerance.

**13.** 1 Gbit/s = 125 MB/s (divide by 8). Mis-stated because people confuse bits and bytes (network vs storage conventions) and think "1 GB/s."

**14.** Because of the queueing knee, variability, spikes, failures (N+1), and deploys: at ≥80% utilization latency rises steeply and any perturbation causes timeouts.

**15.** Rejecting excess requests quickly (503/429) once queues exceed a bound. It preserves low latency for accepted work and avoids all requests timing out as unbounded queues grow; it also gives clients a signal to back off.

**16.** (a) 8 cores / 0.012 s = **667 req/s**. (b) 70% → **467 req/s**. (c) 30,000 / 467 ≈ 64.3 → 65 servers, +1 = **66**.

**17.** 200 / 0.08 = **2,500 req/s**. If latency doubles to 160 ms, max throughput halves to 1,250 req/s; with the same arrival rate the pool saturates, requests queue, and timeouts begin.

**18.** 40 / 0.005 = **8,000 qps** max for the pool. If each app instance generates 600 qps, 8,000/600 ≈ 13 instances saturate the pool; at 60% target: ~8 instances. (Beyond that, more connections or replicas/caching are required: adding app instances doesn't add pool capacity.)

**19.** Requests/day = 600 M → average 6,000 RPS; peak ×4 = 24,000 RPS. Per server effective = 4,000 × 0.6 = 2,400 → 24,000/2,400 = 10 → +1 = **11** servers.

**20.** Originals 8 M × 3 MB = 24 TB/day; thumbnails 8 M × 4 × 20 KB = 0.64 TB/day; total ≈ 24.6 TB/day. Per year ≈ 9.0 PB; ×3 = **27 PB/year**. Upload bandwidth: avg 24 TB / 86,400 s ≈ 278 MB/s ≈ 2.2 Gbit/s; peak ×3 ≈ **6.7 Gbit/s**.

**21.** Views = 20 × 8 M = 160 M/day × 250 KB = 40 TB/day. Average = 40 TB/86,400 ≈ 463 MB/s ≈ **3.7 Gbit/s**; peak ×3 ≈ 11 Gbit/s. Monthly egress = 40 TB × 30 = 1,200 TB = 1.2 PB; at $0.05/GB = 1.2×10⁶ GB × 0.05 = **$60,000/month**. A CDN with volume pricing and cache hit ratios materially reduces origin egress.

**22.** Messages/day = 6×10⁹ → **60,000/s** avg; peak 180,000/s. Storage/day = 6×10⁹ × 120 B = 720 GB; per year = 263 TB; ×3 = **~790 TB/year**.

**23.** Total = 300 M × 2 KB = 600 GB; hot 15% = 90 GB; +30% overhead = 117 GB → **one 128 GB node barely fits**; for headroom and replication use 2–3 nodes (sharded) plus replicas; more realistically 3 primaries + 3 replicas.

**24.** 25 MB/s × 86,400 × 3 days = 6.48 TB; ×3 replication = **19.4 TB** (plus 20–30% headroom ⇒ ~25 TB).

**25.** Posts/day = 120 M × 20% × 2 = 48 M; × 300 = 14.4 B inserts/day ⇒ **144,000/s** avg; peak ×4 ≈ **576,000/s**.

**26.** Usable per node = 100k × 50% = 50k ops/s → 150,000/50,000 = **3 nodes** (shard by key) + replicas for HA.

**27.** Sequential sum: 30 + 15 + 60 + 80 + 50 = 235 ms (DB 40 ms is inside B's 80 ms). Under the 400 ms target ✔ with 165 ms slack (but those are p99 components; p99s don't add exactly). For 300 ms: still passes nominally (235 ms) but slack is small; to be safe parallelize A and B if independent, cache A's data, reduce serialization overhead, and shorten the critical path.

**28.** ρ = 50%: 20/0.5 = **40 ms**; 75%: 20/0.25 = **80 ms**; 90%: 20/0.1 = **200 ms**; 95%: 20/0.05 = **400 ms**. 10×S requires 1/(1−ρ) = 10 → ρ = **90%**.

**29 (sketch).** Writes 5 M/day = 58/s avg (peak ~230/s); reads 100 M/day = 1,160/s (peak ~4,600/s); storage 50 GB/day → 18 TB/year ×3 = 55 TB; read bandwidth peak 4,600 × 10 KB = 46 MB/s ≈ 370 Mbit/s. Implications: store pastes in object storage (blob) with metadata in DB; cache hot pastes; CDN for popular; TTL/expiry; one DB node adequate for metadata.

**30 (sketch).** Notifications/day = 2.4 B ⇒ 24,000/s avg; peak ×6 = 144,000/s. Push 70% = ~100k/s peak; email 20% = ~29k/s; SMS 10% = ~14k/s. Provider throughput planning: e.g., APNs/FCM connection pools; SMS gateways are rate limited and costly. Queue backlog 30 min at peak: 144k × 1,800 = 260 M messages × ~500 B = 130 GB of queue storage ⇒ sizeable Kafka/SQS cluster; separate queues per channel and priority.

**31 (sketch).** Calls/day = 200 M × 6 × 5 = 6 B ⇒ 60,000/s avg, 180,000/s peak. With 60% edge hit: backend 72,000/s peak ⇒ ~15–25 nodes of in-memory trie servers (5k/s each at 60% utilization +N+1); the trie fits in memory.

**32 (sketch).** 1 B pages/month ÷ 2.6×10⁶ s ≈ 385 pages/s; bandwidth 385 × 100 KB = 38.5 MB/s ≈ 308 Mbit/s. Storage compressed 30 KB × 1 B = 30 TB/month ⇒ 720 TB over 2 years (×3 = 2.2 PB, or erasure coded ~1.5×). Fetch workers: 385 / 20 ≈ 20 workers (plus politeness constraints per domain, DNS, retries — in practice many more due to per-host rate limits and latency).

**33 (sketch).** Source: 300 h/min × 1,440 = 432,000 h/day × 1.5 GB = 648 TB/day; renditions ×2 = 1,296 TB/day; with replication: originals ×3 = 1.94 PB + renditions ×2 = 2.59 PB ⇒ **~4.5 PB/day**. Transcoding CPU-hours/day: 432,000 source-hours × 6 renditions × 1 CPU-hour = 2.6 M CPU-hours/day ⇒ ÷ 24 ≈ 108,000 cores continuously. (Hardware encoders/GPUs and smarter ladders reduce this materially.)

**34 (sketch).** 3 M / 5 s = **600,000 writes/s**; bandwidth 48 MB/s; memory 3 M × 80 B = 240 MB; trip requests 400k/h ≈ 111/s with proximity queries; location store is in-memory, geo-partitioned.

**35.** A CPU upgrade shifts the knee but doesn't remove it; a p99 jump between 50% and 70% suggests queueing amplified by variance or a serial component. Other causes: GC pauses scaling with heap/allocation rate, lock contention rising non-linearly, a slow dependency or connection pool saturation at that traffic, noisy neighbors, an uneven load balancer, retry amplification. Investigate with USE/traces/profiles; consider reducing variance (isolate slow requests), increasing parallelism, caching, adding capacity before the knee.

**36.** CPU > 80% already means you're past the knee; instance boot (5 min) means the ramp outruns scaling: queues, timeouts, retries → collapse before capacity arrives. Redesign: scale earlier (CPU > 50–60%), use leading indicators (request rate, concurrency, queue depth), predictive/scheduled scaling for promotions, pre-warm capacity, faster boot (smaller images, warm pools), load shedding and rate limiting during ramp, buffers via queues for async work.

**37.** Closed-loop generator (coordinated omission); test used warm caches/small dataset; no skew/hot keys; generator saturated; test omitted dependencies/failures; average of percentiles; different instance sizes/config; no background jobs/competing traffic; single instance extrapolated; test duration too short for GC/compaction effects.

**38.** Taking one replica out at a time leaves 2 replicas to absorb 3/2 of traffic: from 55% to ~83% (knee); connection draining may be missing so in-flight requests fail; new instances are cold (JIT, caches, pools) and may receive full traffic immediately. Fixes: surge capacity (max surge ≥ 1), readiness gates, slow-start/ramp-up in the LB, graceful draining, lower baseline utilization, canary deploys.

**39.** Likely limits not captured by CPU: thread/connection pool sizes (Little's Law ceiling), a global lock or single-threaded component, rate limits (gateway/third-party), network NIC/bandwidth or packet-per-second limits, file descriptor/ephemeral port exhaustion, DB connection limits, queue concurrency settings, a hot key. Experiments: thread dumps, pool metrics, per-resource saturation, vary pool sizes, test dependencies in isolation, check LB and gateway limits, packet counters.

**40 (sketch).** Assume 1 B MAU, 500 M DAU; 100 M photos/day uploaded; avg original 2 MB; stored renditions ~0.5 MB; ⇒ 100 M × 2.5 MB = 250 TB/day (×3 replication = 750 TB/day; 270 PB/year). Views: 500 M DAU × 100 views/day (feed images) = 5×10¹⁰/day × 200 KB = 10 PB/day egress ⇒ 115 GB/s ≈ 1 Tbit/s avg; peak 3 Tbit/s ⇒ CDN essential. Metadata: 100 M posts/day × 1 KB = 100 GB/day. Read QPS for feeds: 500 M × 10 loads = 5 B/day ⇒ 58k/s avg, 170k/s peak ⇒ caches + timeline stores. Structured, assumptions stated, conclusion: media dominates; CDN + object storage; feed read path optimized with caching/precomputation.

**41 (sketch).** Start from forecasts: baseline peak QPS × expected surge multiple (from last year/marketing), per-instance capacity from load tests at the SLO (p99), target utilization 60% with N+1 / AZ-loss, dependency capacities (DB, caches, third parties, rate limits), pre-scaling plan with warm-up, load shedding and degrade modes, queue buffers for async work, dry-run load test at 1.5× forecast, runbooks, and monitoring for leading indicators.

**42 (sketch).** Queueing knee: latency rises sharply as ρ → 1; variance and retries worsen it; fixes: add capacity or reduce service time before 70%, autoscale on leading indicators, reduce variance (separate slow endpoints), load shedding, cap concurrency, caches, fix GC/locks, and ensure N+1 headroom.

**43.** A restaurant at 95% full: any random cluster of arrivals finds no empty tables, so parties wait; the line grows faster than it clears. Same for servers: at 95% utilization any burst creates a queue that takes a long time to drain; add tables/servers before they fill, turn away people when the line is too long (load shedding), and keep spare capacity for a bad night (N+1).

---

## 5.18 Flashcards

| Front | Back |
|---|---|
| Response time | Queueing + service + network = what users feel |
| Capacity | Max throughput meeting a latency target |
| Why percentiles | Averages hide the tail; aggregate histograms, not averages of percentiles |
| Fan-out tail | P(all fast) = 0.99ᴺ; at N=100 only 37% fast |
| Hedged request | Duplicate request after ~p95 delay; take first reply |
| M/M/1 response time | S / (1−ρ) |
| ρ=90% | Avg response = 10×service time |
| Target utilization | ≤60–70% at peak, with N+1 |
| Little's Law | L = λ × W |
| Pool throughput cap | pool size ÷ latency |
| Amdahl | Max speedup = 1/s (serial fraction) |
| USE | Utilization, Saturation, Errors (resources) |
| RED | Rate, Errors, Duration (services) |
| Coordinated omission | Closed-loop test hides stalls; use open-loop, measure from intended time |
| Seconds/day | ≈10⁵ |
| 1 Gbit/s | 125 MB/s |
| QPS from daily | ops/day ÷ 10⁵ |
| 1 B req/day | ≈ 12k req/s |
| 1 TB/day | ≈ 12 MB/s ≈ 100 Mbit/s |
| Peak factor | 2–5× typical; 10×+ for events |
| Estimation steps | traffic → storage → bandwidth → memory → servers → conclusion |
| Load shedding | Reject excess fast to protect latency |

## 5.19 Glossary additions

| Term | Simple Meaning | Technical Meaning | Why It Exists | Example |
|---|---|---|---|---|
| Service time | Time being served | Processing time excluding wait | Basis for queueing | 10 ms of work |
| Queueing delay | Waiting in line | Time spent waiting for a resource | Explains nonlinear latency | 90 ms wait |
| Utilization | How busy | Fraction of capacity used | Predicts queueing | CPU 70% |
| Saturation | Backed up | Work waiting for a resource | Early warning | queue length |
| Percentile (p99) | "Slowest 1% start here" | Value below which 99% of samples fall | Tail visibility | p99 = 800 ms |
| Tail latency | Slow outliers | High-percentile latency | Dominates fan-out | p99.9 |
| Hedged request | Backup request | Duplicate to a second replica after delay | Cut tail | "Tail at Scale" |
| Little's Law | Count = rate × time | L = λW | Capacity planning | 300 in flight |
| Amdahl's law | Serial part limits speedup | Speedup ≤ 1/s | Scaling ceilings | 5% serial → 20× |
| Knee | Where latency shoots up | Region near high utilization | Planning headroom | ρ≈80% |
| Headroom | Spare capacity | Capacity − load | Absorb spikes/failures | 40% spare |
| N+1 | Survive one loss | Provision one extra unit | Failure tolerance | 3+1 servers |
| Open-loop load | Fixed arrival rate | Requests issued on schedule | Honest latency | wrk2 |
| Coordinated omission | Hidden stalls | Closed-loop measurement bias | Avoid fake p99 | 5 ms vs 1,960 ms |
| Back-of-envelope | Rough math | Order-of-magnitude estimation | Early design decisions | 12k req/s |
| Peak factor | Peak ÷ average | Traffic burst multiple | Size for peak | ×5 |
| Egress | Data out of cloud | Outbound transfer (billed) | Cost driver | CDN offload |
| Load shedding | Reject extra work | Drop low-priority/excess requests | Protect latency | 503 on overload |
| Soak test | Long-run test | Hours/days under load | Find leaks | 24 h test |

---

## Part I complete

You now have the foundations: what systems are (Ch 0), the machines and networks they run on (Ch 1), how they expose and secure APIs (Ch 2), how data is stored and kept correct (Ch 3–4), and how to reason about performance and capacity with numbers (Ch 5).

**Next: Part II: Scaling, starting with Chapter 6: Scalability and Stateless Design** (vertical vs horizontal scaling, the scaling ladder, stateless services in depth, shared-nothing architectures, autoscaling, the first end-to-end reference architecture), then Chapters 7–9 (load balancers and proxies, caching, replication) in the next batch.

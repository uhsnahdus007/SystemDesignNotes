# Chapter 18: Reliability and Fault Tolerance

## Chapter card

| | |
|---|---|
| **Objective** | Learn to design systems that keep working (or fail gracefully) when components fail, load surges, dependencies degrade, and humans make mistakes: reliability vocabulary and SLOs, redundancy and failure domains, timeouts/retries/backoff and why retries cause outages, circuit breakers, bulkheads, load shedding, graceful degradation, metastable failures, safe deployment, data protection and disaster recovery, resilience testing, and incident management |
| **Why this chapter exists** | Chapter 14 told you failures are inevitable; this chapter turns that into engineering practice. Most large outages are not caused by one broken box but by a small fault amplified by well-meaning mechanisms (retries, autoscaling, failover) or by a change gone wrong. Interviewers expect you to design for failure explicitly and to quantify it |
| **Prerequisites** | Chapters 5, 6, 7, 8, 9, 12, 14 |
| **Study time** | 12–15 hours including labs |
| **Tier** | `[MK]` throughout; metastable failures and adaptive concurrency `[GK]` |
| **Mastery criteria** | (1) Define SLI/SLO/SLA, error budgets, and compute burn rates. (2) Design redundancy with correct failure domains and compute N+1 / AZ-survivable capacity. (3) Configure timeouts, retries, backoff, jitter, and retry budgets, and explain retry amplification with numbers. (4) Explain circuit breakers (with half-open ramp), bulkheads, and load shedding, and what each protects against. (5) Explain metastable failure and how to prevent and recover from it. (6) Design a safe deployment pipeline. (7) Design backups and DR with RPO/RTO. (8) Plan resilience tests (load, chaos, DR drills). (9) Run and learn from an incident (blameless postmortem) |

---

## 18.1 The Story: The Outage That Started With a Recommendation Widget

**StreamBox** serves video. Its home page makes about 15 calls; one is to **Recommendations** (nice to have). On a Friday evening:

1. 18:02 A deploy of the Recommendations service introduces a slow database query. Its p99 latency climbs from 40 ms to 6 seconds. It still returns correct results, just slowly. **No alert fires**: the service is “up.”
2. 18:09 The **API gateway** has 400 worker threads shared across all endpoints. Requests to the home page now wait up to 6 s on Recommendations (no timeout: the library default is “infinite”). Threads fill up with slow calls.
3. 18:14 Thread pool exhausted. **Every endpoint**, including login and playback, starts timing out because there are no free threads. Error rate 60%.
4. 18:15 Clients and mobile apps retry (3×), and the gateway retries failed upstream calls (3×). Traffic to the already-struggling services is **up to 9× normal**.
5. 18:20 The autoscaler adds gateway instances. Each new instance opens fresh connection pools to the database, which now has 3× its normal connections and begins refusing new ones. The playback service, which was healthy, starts failing.
6. 18:31 Engineers roll back the Recommendations deploy. The query is fast again. **But the outage continues**: the retry storm and the queued work keep the system saturated even though the original cause is gone.
7. 19:40 After manually shutting off retries at the gateway, blocking traffic at the load balancer, and slowly letting it back in, the site recovers. Total impact: 98 minutes. Customer-facing availability for the month falls to 99.78% (SLO: 99.95%).
8. Postmortem: also discovered that the last database backup had never been test-restored, and that the “disaster recovery region” had been missing a configuration change for eight months.

### Pause & Predict 18.1
> Identify at least six distinct design flaws in this story. For each, name the standard mitigation (timeout, bulkhead, retry budget, load shedding, circuit breaker, graceful degradation, safe deployment, backup testing…). Which single change would have turned a 98-minute outage into a degraded home page with no recommendations?

---

## 18.2 Reliability vocabulary and the nature of failure `[MK]`

### 18.2.1 Terms
- **Fault:** a defect or abnormal condition in a component (a disk sector goes bad, a bug, a bad config).
- **Error:** the incorrect internal state a fault produces.
- **Failure:** the system deviates from its specification as observed by users.
- **Fault tolerance:** preventing faults from becoming failures. **Resilience:** the ability to absorb, adapt to, and recover from disruptions. **Robustness:** correct behavior under unexpected inputs/conditions.
- **Reliability:** continuity of correct service; **availability:** fraction of time the service is usable; **durability:** data is not lost; **safety:** no catastrophic consequences; **maintainability:** how fast it is repaired.

### 18.2.2 Why things fail (and what to design against)
| Cause | Notes |
|---|---|
| **Hardware** | Disks (AFR 1–4%), memory errors, NICs, power, cooling; independent but frequent at scale (Ch 14) |
| **Software bugs** | **Bohrbugs** (deterministic, reproducible) vs **Heisenbugs** (timing/race/state dependent); bugs correlate across replicas running the same code |
| **Operator and change errors** | The leading cause of large outages: bad deploys, config pushes, migrations, manual commands `[Public: Google SRE reports ~70% of outages tied to changes]` |
| **Dependencies** | Third-party APIs, DNS, cloud services, internal services; failures propagate along call graphs |
| **Overload/capacity** | Flash crowds, retry storms, noisy neighbors, resource leaks, unbounded queues |
| **Data corruption/loss** | Bugs, bit rot, malicious or accidental deletes |
| **Security incidents** | DDoS, compromise, ransomware |
| **Environment/natural** | AZ/region events, fire, flood, backhoes cutting fiber |
**Key point:** redundancy defends mainly against **independent hardware failure**. Most severe outages involve **correlated** causes: software, configuration, change, or overload. So resilience engineering is mostly about *limiting blast radius*, *containing overload*, and *making change safe*.

### 18.2.3 Failure domains and blast radius `[MK]`
A **failure domain** is a set of components that fail together: process < container < host < rack < power zone < **availability zone** < region < provider < global control plane/config/DNS. **Blast radius** = the share of users or capacity lost when one domain fails. Design questions: *What is the largest failure domain any single fault could take down? Can a bad deploy or config reach all domains at once?* Techniques: spread replicas across domains; deploy to one domain at a time; **cells** (Ch 6) so a failure touches ≤ 1/N of customers; **shuffle sharding** (assigning each customer a random subset of workers so a poison customer harms only a tiny fraction of others `[Public: AWS Builders’ Library]`).

---

## 18.3 SLIs, SLOs, SLAs, and error budgets `[MK]`

### 18.3.1 Definitions
- **SLI (Service Level Indicator):** a measured property, expressed as a ratio of good events to total events. Examples: *availability* = successful requests ÷ valid requests; *latency* = requests faster than 300 ms ÷ requests; *freshness* = reads returning data less than 5 s old; *correctness*; *durability*.
- **SLO (Service Level Objective):** the target for an SLI over a window, e.g., “99.9% of requests succeed over 30 days.”
- **SLA (Service Level Agreement):** a contractual promise with consequences (credits), usually looser than the internal SLO.
Measure at the **user-facing boundary** (load balancer, client, synthetic probes), not only inside a service (a service that is “up” but slow is down to users).

### 18.3.2 Error budgets
Budget = 1 − SLO. For 99.9% over 30 days: 0.1% × 43,200 min = **43.2 minutes** of allowed unavailability (or the equivalent in failed requests). The budget turns reliability into a shared resource: while budget remains, ship features and take risk; when it is exhausted, freeze risky changes and invest in reliability. This resolves the eternal dev-vs-ops conflict with data.

### 18.3.3 Burn-rate alerting `[GK]`
**Burn rate** = (observed error rate) ÷ (1 − SLO). Burn rate 1 consumes the budget exactly over the full window; burn rate 14.4 over a 30-day window would exhaust it in 50 hours. Budget consumed = burn rate × (alert window ÷ SLO window).
- *Fast burn (page):* 14.4× over 1 h (consumes 14.4 × 1/720 = **2%** of a monthly budget) confirmed by a 5-minute window to avoid stale alerts.
- *Slow burn (page/ticket):* 6× over 6 h (consumes 5%), 1× over 3 days (10%).
Multi-window, multi-burn-rate alerts catch both sharp outages and slow leaks with few false pages `[Public: Google SRE Workbook]`.

### 18.3.4 Choosing SLOs
Pick SLOs users would notice, not what is easy to hit. 100% is the wrong target (cost explodes; users’ networks aren’t 100% either). Each additional nine costs roughly 10× in effort. Dependencies bound you: if you depend serially on three services at 99.9%, your ceiling is 99.7% (Ch 14). Define SLOs *per critical user journey* (login, checkout, playback), not just per microservice.

---

## 18.4 Redundancy and failure-domain design `[MK]`

### 18.4.1 N+1, N+2, and AZ-survivable capacity
- **N+1:** enough capacity to lose one unit (instance, node). **N+2** for maintenance during failure.
- **AZ-survivable capacity:** with 3 AZs and peak load P, each AZ must be able to carry P/2 so that losing one AZ leaves two AZs carrying 100% of P. Total provisioned = **1.5 × P** (50% overhead), not 1.0. With 2 AZs you need 2× P; with 4 AZs, 1.33× P. This is the **static stability** idea: the system continues to work in a degraded zone state **without needing any control-plane action** (like launching new instances during an outage, when the control plane may itself be impaired) `[Public: AWS Builders’ Library, “Static stability”]`.
- **Utilization targets:** run at ≤ 60% average so that N+1 failure and traffic spikes do not push you onto the queueing knee (Ch 5).

### 18.4.2 Active–active vs active–passive
| | Active–active | Active–passive |
|---|---|---|
| Capacity use | All sites serve traffic | Standby idle |
| Failover | Remove failed site; others absorb load (need headroom) | Promote standby (needs detection, warm-up) |
| Complexity | Data replication, conflicts, routing | Simpler; risk that standby is **untested/stale** |
| Failure surprise | Hidden capacity shortage | Standby doesn’t work when needed |
**Rule:** a standby that is never exercised does not exist. Regularly fail over for real (game days), or run active–active so every site is continuously proven.

### 18.4.3 Disaster recovery strategies (cost vs recovery time) `[MK]`
| Strategy | Description | RTO | RPO | Cost |
|---|---|---|---|---|
| **Backup & restore** | Periodic backups restored into a new environment | Hours–days | Hours (backup interval) | Lowest |
| **Pilot light** | Minimal core (databases replicated) always on; scale up on disaster | Tens of minutes–hours | Minutes | Low |
| **Warm standby** | Scaled-down full environment running | Minutes | Seconds–minutes | Medium |
| **Multi-site active–active** | Full capacity in ≥ 2 regions | Seconds–minutes | ~0–seconds | Highest |
Define **RTO/RPO per service** based on business impact; not everything needs active–active. DR plans must be **automated and rehearsed**, including how to fail **back**.

### 18.4.4 Redundancy pitfalls
- **Shared dependencies** (one config service, one DNS provider, one CA, one build pipeline) make “redundant” systems fail together.
- **Correlated software failure:** N replicas of the same buggy code fail together; use staggered rollouts and canaries rather than N-way identical simultaneous change.
- **Failover that increases load on the survivor** (retry storms, cache cold start, connection storms).
- **Split brain** and data divergence in active–active (Ch 9, 15, 16).

---

## 18.5 Timeouts, retries, backoff, and why retries cause outages `[MK]`

### 18.5.1 Timeouts: the first line of defense
Every remote call needs an explicit **timeout** (connect and request), and ideally a propagated **deadline** (the remaining time budget travels with the request, so downstream work stops once the caller has given up). Without timeouts, one slow dependency holds threads/connections indefinitely (Story step 2). Guidelines:
- Timeout ≈ a high percentile (p99–p99.9) of normal latency × a small factor, **not** “seconds because it’s safe.”
- **Timeouts shrink with depth:** inner calls must time out before outer ones, leaving the caller time to degrade gracefully.
- Handle the ambiguity of a timeout (Ch 14): idempotency for retries.

### 18.5.2 Retries
Retrying turns transient faults into non-events, but retries add load exactly when the system is struggling.
- **Retry only idempotent operations** (or those with idempotency keys) and only for **retriable errors** (timeouts, 503, connection reset), never for 4xx client errors.
- **Exponential backoff with jitter:** delay = random(0, min(cap, base·2^attempt)). Jitter desynchronizes clients.
- **Bound attempts** (2–3) and total elapsed time.
- **Retry at one layer only.** If client, gateway, service, and library each retry 3 times, one user action becomes 3⁴ = 81 attempts at the bottom.
- **Retry budgets:** cap retries at, e.g., 10% of regular traffic per client/service (Finagle, Envoy, gRPC). When the budget is exhausted, fail fast.
- **Hedged requests** for tail latency on idempotent reads (send a second request after the p95 latency; Ch 5), only with budgets.

### 18.5.3 Measured: retry storms and metastable failure (Lab 1, part 1)
Model: backend capacity 1,000 req/s, normal load 700 req/s. For 30 seconds (t = 60–90 s) the backend is degraded to 300 req/s; after that the *trigger is gone*. Under heavy overload the backend wastes work on requests whose callers already gave up, so its useful capacity shrinks (down to 30%). The table shows what the clients experience, depending on retry policy:
```text
policy                                         success during incident   success t=90..180 s   peak load on backend   back to >=99% success
A: no retries                                               22.0%                  100.0%                 700 req/s (0.7x cap)    0 s after the incident ended
B: 3 attempts, immediate retry (client only)                 5.2%                   98.1%                2007 req/s (2.0x cap)    4 s after the incident ended
C: 9 attempts (3 layers x 3), immediate                      2.7%                    6.0%                5931 req/s (5.9x cap)    NEVER (still failing at t=180)
D: 3 attempts, backoff + jitter                              5.3%                   96.9%                2007 req/s (2.0x cap)    6 s after the incident ended
E: 3 attempts, backoff + 10% retry budget                   18.3%                  100.0%                 770 req/s (0.8x cap)    0 s after the incident ended
F: 3 attempts immediate + circuit breaker (half-open ramp)   7.2%                   92.8%                1246 req/s (1.2x cap)    9 s after the incident ended
```
**Reading it carefully:**
1. **A (no retries):** during the incident only 22% succeed (the degraded backend can’t serve 700/s, and overload wastes capacity), but the moment the backend heals, **everything is fine**: 100% success immediately.
2. **B (3 immediate attempts):** retries make the incident **worse** (5.2% vs 22%) by doubling the offered load to 2.0× capacity; recovery takes a few seconds after the trigger ends.
3. **C (retries at three layers, 9 attempts):** the retry amplification pushes the backend to **5.9× capacity** and its goodput collapses; **after the original fault is gone, the system never recovers** (6% success at the end of the observation window). This is a **metastable failure**: the *sustaining effect* (retry traffic) keeps the system in the bad state long after the *trigger* ended. It matches Story steps 4–6, where rolling back the bad deploy did not end the outage.
4. **D (backoff + jitter)** spreads retries but still adds load: slightly better recovery than B, similar incident success.
5. **E (backoff + retry budget)** is the standout: peak load stays at 0.8× capacity; incident success 18.3% (the best of any retrying policy); instant recovery. A retry budget **bounds amplification at the source**.
6. **F (circuit breaker with half-open ramp)** limits the load reaching the backend (peak 1.2×), avoids the stuck state, and recovers about nine seconds after the trigger ends (the time to ramp through half-open stages); the success rate during the incident is low because the breaker is deliberately rejecting traffic to protect the backend.
Notice the fundamental limit: with 300 req/s of capacity and 700 req/s offered, **at most 43% of requests can ever succeed during the incident**. No client-side policy can beat that; better policies simply avoid making things *worse* and avoid being stuck afterward. Only *shedding the excess intentionally* (below) or adding capacity improves the incident itself.

---

## 18.6 Circuit breakers, bulkheads, load shedding, and rate limiting `[MK/GK]`

### 18.6.1 Circuit breakers
A circuit breaker wraps calls to a dependency and tracks failures:
- **Closed:** calls flow; failures counted over a rolling window.
- **Open:** when the failure rate (or slow-call rate) exceeds a threshold, calls **fail immediately** (no network call) for a cool-down period. Fail-fast protects the dependency and frees the caller’s threads.
- **Half-open:** after the cool-down, allow a **limited trial** of requests (e.g., 10% → 25% → 50% → 100%, each stage requiring success); close on success, re-open on failure.
Important details: *fast-fail errors must not be retried* (the lab showed that retrying breaker rejections re-created the overload); decide the **fallback** (cached data, default, error); breakers are **per dependency (and often per endpoint/host)**; count *slow* calls as failures; add jitter to cool-downs so many instances do not probe in lockstep. Libraries: Resilience4j, Polly, Envoy outlier detection, Istio.

### 18.6.2 Bulkheads
Isolate resources so a failure in one part cannot consume everything: separate **thread pools / connection pools / queues** per dependency or per tenant/priority class, concurrency limits per downstream, separate processes or instances for critical vs non-critical traffic, separate cells. The ship-compartment analogy.

**Measured (Lab 1, part 2).** A service with 100 worker threads and two endpoints, `/fast` (10 ms) and `/report` (depends on a service that becomes 3 s slow from t = 20 to 50 s), each at 150 req/s; callers give up after 1 s:
```text
design                                              /fast success during incident   /fast success after the incident
shared pool of 100 threads, no dependency timeout (3 s)         18.4%                           92.9%
shared pool, 1 s dependency timeout                             49.1%                          100.0%
shared pool, 300 ms dependency timeout                         100.0%                          100.0%
bulkhead 70/30 threads, no dependency timeout                  100.0%                          100.0%
bulkhead 70/30 threads, 1 s dependency timeout                 100.0%                          100.0%
```
**Reading it.** By Little’s Law the slow endpoint holds 150 req/s × 3 s = **450 threads' worth** of work against a pool of 100: the shared pool saturates, and the *healthy* `/fast` endpoint succeeds only 18% of the time (this is Story step 3). A 1 s timeout helps but still needs 150 threads (49%). A 300 ms timeout needs only 45 threads, so even the shared pool survives. A **bulkhead keeps `/fast` at 100% regardless of how the dependency behaves**, because the damage is contained to the 30 threads reserved for `/report`. Defense in depth: use **both**: tight timeouts shrink the problem, bulkheads cap its worst case.

### 18.6.3 Load shedding and goodput `[MK/GK]`
When offered load exceeds capacity, a server that tries to serve everything in FIFO order can enter **congestion collapse**: it spends its time on requests whose callers have already timed out, so *useful work (goodput) approaches zero even though the server is 100% busy*.

**Measured (Lab 1, part 3).** One server, 1,000 req/s capacity, clients abandon after 1 s:
```text
offered load    FIFO, unbounded queue          FIFO + bounded queue (max 100)        LIFO + drop requests older than 1 s
  (req/s)       goodput (useful responses/s)   goodput / rejected                    goodput / dropped
    500            502                            502 /      0                         502 /      0
    900            899                            899 /      0                         899 /      0
   1000            999                            996 /      4                         987 /     13
   1200             87                           1002 /    199                        1002 /    198
   1500             46                           1002 /    496                        1005 /    492
   3000             24                           1002 /   1985                        1011 /   1976
```
Just 20% over capacity, FIFO with an unbounded queue delivers **87 useful responses per second out of 1,000 possible**: the queue grows, every request waits more than the 1-second deadline, and the server dutifully processes responses nobody wants. With a **bounded queue** (reject early) or **LIFO plus dropping expired requests**, goodput stays at ≈ 1,000/s however hard the load grows; the excess is rejected cheaply and quickly. **Shedding load is how you keep serving the requests you can.**

Load-shedding techniques:
- **Bounded queues** and concurrency limits; reject with 429/503 + `Retry-After` quickly.
- **Deadline-aware dropping:** skip work whose deadline has passed (propagate deadlines).
- **LIFO under overload** (newest first: they are the ones still wanted).
- **Priority / criticality tiers:** shed analytics and prefetch before checkout; reserve capacity for critical traffic.
- **Adaptive concurrency limits** (Netflix concurrency-limits, TCP-style AIMD, gradient algorithms): measure latency, shrink the limit when latency rises, probe upward slowly `[Public]`.
- **Admission control at the edge** (gateway/CDN/WAF) so overload never reaches deep services.
- **Client-side throttling** (Google SRE): clients stop sending when their recent accept ratio falls.

### 18.6.4 Rate limiting
Protects against abusive or runaway clients and enforces fairness.
- **Token bucket:** bucket of capacity *b* refilled at rate *r* tokens/s; each request consumes a token; allows bursts up to *b*, average rate *r*. Most common.
- **Leaky bucket:** requests drain at a fixed rate; smooths bursts (a queue).
- **Fixed window counter:** simple but allows 2× bursts at window edges. **Sliding window log/counter:** accurate or approximate.
- **Where/how:** per client/API key/IP/tenant; at gateway; distributed counters in Redis (atomic Lua) or local approximate limits with periodic sync; return `429 Too Many Requests` with `Retry-After` and rate-limit headers.
- Rate limiting is *policy per caller*; load shedding is *protection of the server*. Use both.

---

## 18.7 Graceful degradation and fallbacks `[MK]`

When something fails, do less, not nothing.
1. **Classify dependencies by criticality.** In the Story, Recommendations was *optional*; login and playback were *critical*. Design the home page to render without recommendations.
2. **Fallbacks** (in order of preference): serve **stale cached data** (stale-while-revalidate/stale-if-error, Ch 8), serve **defaults** (popular titles), **static content**, **read-only mode**, queue writes for later, **disable the feature** with a feature flag, show a clear error for just that widget.
3. **Fail-open vs fail-closed:** a rate limiter or authorization service failing: should requests pass (open, availability) or be rejected (closed, safety)? Decide per dependency deliberately: payments and authorization usually fail closed; recommendations and ads fail open.
4. **Static stability:** fallbacks must not depend on the failing system (a fallback that calls the same broken dependency is not a fallback). Precompute, cache locally, ship defaults with the client.
5. **Partial results:** return what you have (with an indicator) rather than waiting for the slowest of 50 shards (Ch 10).
6. **Degrade expensive features under load** (dynamic): lower image quality, disable personalization, shorter result lists (“brownout” controllers).
7. **Test the degraded paths** regularly; fallbacks that never run are broken.

---

## 18.8 Metastable failures `[GK]`

A **metastable failure** (Bronson et al., HotOS 2021 `[Public]`) occurs when a **trigger** pushes the system into a bad state that is then **sustained by a feedback loop** (the sustaining effect) even after the trigger is removed: e.g., a load spike causes timeouts → clients retry → more load → more timeouts. The system is stuck until something external breaks the loop.

**Common sustaining effects:** retry amplification; **cold caches** (cache loss → database overload → cannot refill cache); **queues** with stale work (the backlog is never cleared); connection storms/thread exhaustion; GC death spirals; autoscaling thrash; failover that overloads the survivor; **work amplification** (each failed request triggers expensive recovery); lock convoys.

**Why they are dangerous:** the original trigger may be tiny and self-resolved (a 30-second blip, as in the lab), yet the outage lasts hours; “fix the root cause” doesn’t help; and the system is often at **high utilization** where it has no slack to absorb the retry load (Ch 5).

**Prevention**
- Keep utilization headroom; load shed and bound queues; **retry budgets**, exponential backoff + jitter, one retry layer; circuit breakers with ramped recovery.
- **Make the system stateless w.r.t. recovery**: caches should be warmed by replicas, not demand; recover with traffic ramp-up (token buckets that grow).
- Prefer **fast-fail over queueing**; use deadlines to drop stale work.
- Reduce **amplification factors** (fan-out, retries, recomputation) and monitor them as metrics.
- Design **admission control** to keep goodput stable under overload (lab part 3).

**Recovery (when it happens)**
1. **Stop the feedback loop:** disable retries at gateways/clients, shed load aggressively (rate limit, block non-critical traffic), cut traffic at the edge (e.g., 10% allowed).
2. **Reduce load below the stable threshold,** let queues drain, then ramp traffic back gradually (watch for the secondary spike).
3. Restart/scale as needed; warm caches before readmitting traffic.
4. **Postmortem:** identify the sustaining effect and put a mechanism in place to cap it.

---

## 18.9 Safe deployment and change management `[MK]`

Most large outages are caused by **change**. Treat the deployment pipeline as part of the reliability system.
- **Small, frequent, reversible changes** beat big-bang releases.
- **Progressive delivery:** canary (1% → 5% → 25% → 100%) with automated analysis of error rate, latency, saturation, business metrics; promote only if healthy for a **bake time**; automatic rollback on SLO regressions.
- **Stage by failure domain:** one cell/AZ/region at a time, lowest-risk first; never push to all regions simultaneously; configuration changes follow the same rules as code (many global outages were config pushes).
- **Feature flags / dark launches:** decouple deploy from release; kill switches to turn off a feature in seconds; dark traffic/shadow reads to compare behavior without user impact.
- **Backward compatibility:** old and new versions coexist (APIs, schemas expand/contract, cache keys; Ch 6, 8).
- **Fast rollback, tested:** a rollback you have never exercised may not work (database migrations that cannot be reversed, incompatible serialization). Keep the previous version deployable; prefer roll-forward for data changes only with a tested plan.
- **Deploy hygiene:** peak-hour and Friday-afternoon freezes (judgment), change calendars, peer review, automated tests, **pre-production load tests**, infrastructure as code and drift detection.
- **Database changes:** expand → migrate → contract; online schema change tools; guard against long locks (Ch 3).
- **Blast-radius limits on operators:** least privilege, confirmation for destructive commands, rate-limited automation (an automation bug can delete the fleet), “two-person rule” for dangerous operations.

---

## 18.10 Data protection and disaster recovery `[MK]`

- **Replication is not backup** (Ch 9): it faithfully replicates deletes and corruption.
- **Backups:** full + incremental + continuous log archiving for **point-in-time recovery (PITR)**; **3-2-1 rule** (3 copies, 2 media/types, 1 off-site/off-account); **immutable/WORM** backups against ransomware and credential compromise; encrypted; separate credentials from production.
- **Test restores regularly and automatically:** measure the actual RTO and check data integrity. The Story’s untested backup is the norm, not the exception. An untested backup is a hope.
- **Corruption defenses:** end-to-end checksums, scrubbing, application-level validation, canary reads, schema constraints (Ch 11).
- **Soft delete and retention windows** for user-initiated deletes; recycle bins; protection against bulk deletes (rate-limit, approvals); database-level delayed replica (Ch 9).
- **DR plan and runbooks:** who declares a disaster, how traffic moves (DNS/anycast/GSLB, Ch 7), data promotion, dependencies (secrets, DNS, certificates, CI), communication; **rehearsal at least annually**, with failback.
- **Capacity in the DR site:** can it carry production load? Are quotas raised? Are configs identical (drift detection)?

---

## 18.11 Testing for resilience `[MK/GK]`

| Technique | What it finds |
|---|---|
| **Load testing (open-loop, Ch 5)** | Capacity limits, knee, tail latency, autoscaling behavior; avoid coordinated omission |
| **Soak/endurance tests** | Leaks, slow degradation, log/disk growth, connection leaks |
| **Stress and spike tests** | Behavior beyond capacity: do you shed or collapse? |
| **Failure injection** | Kill instances, drop packets, add latency, exhaust disk/CPU, partition networks (Toxiproxy, tc/netem, Chaos Mesh, Gremlin, AWS FIS) |
| **Chaos engineering** `[Public: Netflix, principlesofchaos.org]` | Define a *steady-state hypothesis* (e.g., orders/min), inject realistic faults in production-like environments with a controlled blast radius, and verify the hypothesis; start small, automate, build confidence |
| **Game days** | Practice responding: on-call rehearses incidents with real tooling |
| **DR drills** | Region failover and backup restores, with timing |
| **Dependency failure tests** | Make each dependency slow/down in staging; verify timeouts, fallbacks, and alerts |
| **Canary analysis & synthetic probes** | Catch regressions and external reachability issues |
| **Deterministic simulation / Jepsen** | Rare interleavings in distributed protocols (Ch 14–16) |
| **Production readiness reviews (PRR)** | Checklist: SLOs, alerts, runbooks, capacity, rollback, dependencies, backups |
**Principles:** test in production-like conditions; test the *recovery* path as much as the failure; keep an inventory of “things that have never been exercised.”

---

## 18.12 Incident response and learning `[MK]`

- **Detection:** page on **symptoms users feel** (SLO burn rate) rather than every cause (CPU high); supplement with synthetic checks and dependency health; avoid alert fatigue (every page must be actionable).
- **Response roles:** incident commander (coordinates), operations lead (hands on keyboards), communications lead (status page/customers), scribe (timeline). Escalation paths, severity definitions (SEV1–SEV4), war-room discipline.
- **Mitigate first, diagnose later:** rollback, failover, disable a feature, shed load, scale out. Restore service before finding root cause.
- **Runbooks and tooling:** one-click rollback, traffic shifting, kill switches, dashboards organized around the user journey; ensure the tools you need during an outage **do not depend on the failing system** (a classic: the status page or VPN or internal chat is down with the platform).
- **Communication:** early, honest, regular updates.
- **Blameless postmortems:** reconstruct the timeline, find contributing factors (not a single “root cause”: complex failures have several), ask “how did our system make this mistake easy?”, produce prioritized, owned, tracked **action items** (detect faster, mitigate faster, prevent), share widely. Track MTTD, MTTR, recurring themes.
- **Reliability culture:** error budgets, toil reduction, on-call health, capacity for fixing, and leadership support for slowing feature work when budget is spent.

---

## 18.13 Capacity and overload planning `[MK]`

- Headroom: plan for peak × growth × failure of the largest failure domain (AZ), at ≤ 60–70% utilization after failure.
- **Load test to find real capacity** and set autoscaling and shedding thresholds from data; recheck after major changes.
- **Quotas and limits** (cloud account limits, DB connections, file descriptors, API rate limits from providers) are capacity too; monitor them.
- **Known events** (launches, sales, sports finals): pre-scale (Ch 6), warm caches, raise limits, freeze changes, staff on-call, prepare kill switches and a waiting room/queue for admission.
- **Dependency capacity:** your retries and autoscaling can overload the dependencies; bound concurrency per dependency.
- **Cost of resilience:** quantify: AZ-survivable (+50%), multi-region (2×), cell isolation; match to business value and SLO.

---

## 18.14 Failure modes (reliability engineering catalogue)

| Failure | Mechanism | Mitigation |
|---|---|---|
| **Missing timeouts** | One slow dependency holds all threads | Explicit timeouts, deadlines, bulkheads |
| **Retry amplification / storm** | Multi-layer retries, no budgets | One retry layer, budgets, backoff + jitter, breakers |
| **Metastable failure** | Feedback loop persists after trigger | Load shedding, caps on amplification, ramped recovery |
| **Congestion collapse** | FIFO unbounded queues serve expired work | Bounded queues, LIFO/deadline dropping, admission control |
| **Thread/connection pool exhaustion** | Shared pools, leaks | Bulkheads, limits, leak detection |
| **Cascading failure** | Overload moves up/down the call chain | Backpressure, circuit breakers, shedding |
| **Thundering herd after recovery** | All clients retry/reconnect/cache-miss at once | Jitter, staged reintroduction, request coalescing |
| **Failover overloads survivor** | Insufficient headroom | N+1/AZ-survivable capacity, static stability |
| **Untested standby/backup** | Silent rot, drift | Game days, restore tests, active–active |
| **Bad deploy / config push** | Global change without staging | Canary, staged rollout, auto-rollback, config validation |
| **Shared fate dependency** | Common DNS/config/CA/auth | Dependency mapping, fallbacks, diversity |
| **Alert blindness** | Alert on causes, noisy pages | Symptom-based SLO burn alerts |
| **Slow/gray failures** | Alive but degraded | Latency-aware routing, outlier ejection, deep health, SLO monitoring |
| **Data corruption/deletion** | Bugs, operator error | PITR, immutable backups, soft delete, checksums |
| **Operator error at scale** | Automation with unbounded blast radius | Rate limits, approvals, dry-runs, least privilege |
| **Clock/certificate expiry** | Time bombs | Expiry monitoring, automation, leap-second handling |
| **Resource exhaustion** | Disk full, FD limits, inode, memory leak | Quotas, alerts, load tests, limits |
| **Observability outage** | Monitoring depends on failing system | Out-of-band monitoring/paging, independent status page |
| **Human factors** | Fatigue, unclear runbooks | Clear roles, runbooks, game days, sustainable on-call |

---

## 18.15 Labs

### Lab 1: Retry storms, bulkheads, and load shedding (Java 17+)
```java
import java.util.*;

public class ResilienceLab {
    // ====================================================================================================
    // 1) Retry storms and metastable failure (fluid model, one-second steps)
    // ====================================================================================================
    static final double NEW_LOAD = 700;                // new user requests per second
    static final double BASE_CAP = 1000;               // backend capacity (req/s) when healthy
    static double capAt(int t) { return (t >= 60 && t < 90) ? 300 : BASE_CAP; }       // 30 s degradation: the backend loses 70% of its capacity
    /** Under heavy overload a server wastes work on requests whose callers have already given up, so useful capacity FALLS as offered load rises. */
    static double goodput(double cap, double offered) { return offered <= 1.2 * cap ? cap : Math.max(0.3 * cap, Math.min(cap, 1.2 * cap * cap / offered)); }

    static void retryStorm() {
        System.out.println("== 1) Retry storm. Backend capacity 1,000 req/s, normal load 700 req/s. For t=60..90 s the backend is degraded to 300 req/s. Trigger gone after 90 s. ==");
        System.out.println("   Model: under overload the backend wastes work on abandoned requests, so useful capacity shrinks (to as little as 30%).");
        System.out.println("policy                                         success during incident   success t=90..180 s   peak load on backend   back to >=99% success");
        String[] names = {"A: no retries", "B: 3 attempts, immediate retry (client only)", "C: 9 attempts (3 layers x 3), immediate", "D: 3 attempts, backoff + jitter", "E: 3 attempts, backoff + 10% retry budget", "F: 3 attempts immediate + circuit breaker (half-open ramp)"};
        int[] maxAttempts = {1, 3, 9, 3, 3, 3};
        double[] ramp = {0.10, 0.25, 0.50, 1.00};                                   // half-open: let through 10% -> 25% -> 50% -> 100% of traffic
        for (int sc = 0; sc < names.length; sc++) {
            int R = maxAttempts[sc]; int T = 180;
            double[][] pending = new double[T + 40][R + 1];                         // pending[t][k] = requests that will make attempt k at second t
            double okIncident = 0, newIncident = 0, okAfter = 0, newAfter = 0, peak = 0; int recovered = -1;
            int state = 0; /* 0 closed, 1 open, 2 half-open */ int stateSince = 0, stage = 0; double badEwma = 0;
            for (int t = 0; t < T; t++) {
                double[] offered = new double[R + 1]; offered[1] = NEW_LOAD;
                for (int k = 1; k <= R; k++) offered[k] += pending[t][k];
                double total = 0; for (int k = 1; k <= R; k++) total += offered[k];
                if (sc == 4) { double retries = total - offered[1]; double budget = 0.10 * NEW_LOAD; if (retries > budget) { double scale = budget / retries; for (int k = 2; k <= R; k++) offered[k] *= scale; total = offered[1] + budget; } }   // retry budget
                double allow = 1.0;                                                  // fraction of requests the circuit breaker lets through to the backend
                if (sc == 5) { if (state == 1) allow = 0; else if (state == 2) allow = ramp[stage]; }
                double reaching = total * allow;
                double cap = goodput(capAt(t), reaching);
                double p = reaching <= 0 ? 0 : Math.min(1.0, cap / reaching);        // success probability of a request that reaches the backend
                double okFrac = allow * p;                                           // fraction of ALL offered requests that succeed
                peak = Math.max(peak, reaching);
                double successes1 = offered[1] * okFrac;
                for (int k = 1; k <= R; k++) {
                    double fail = offered[k] * allow * (1 - p);                      // only failures that came from the BACKEND are retried; a breaker's fast-fail is not retryable
                    if (k < R) {
                        if (sc == 3 || sc == 4) { int d = (int) Math.pow(2, k - 1); for (int j = 0; j < d; j++) pending[Math.min(T + 39, t + d + j)][k + 1] += fail / d; }   // backoff 1,2,4 s + jitter spread
                        else pending[Math.min(T + 39, t + 1)][k + 1] += fail;
                    }
                }
                if (sc == 5) {                                                       // breaker state machine, driven by the success rate of requests that REACHED the backend
                    if (reaching > 0) badEwma = 0.6 * badEwma + 0.4 * (1 - p);
                    if (state == 0 && badEwma > 0.5) { state = 1; stateSince = t; }
                    else if (state == 1 && t - stateSince >= 10) { state = 2; stage = 0; stateSince = t; badEwma = 0; }
                    else if (state == 2) { if (p < 0.8) { state = 1; stateSince = t; } else if (t - stateSince >= 3) { stateSince = t; if (++stage >= ramp.length) { state = 0; badEwma = 0; } } }
                }
                if (t >= 60 && t < 90) { okIncident += successes1; newIncident += NEW_LOAD; }
                if (t >= 90) { okAfter += successes1; newAfter += NEW_LOAD; if (recovered < 0 && successes1 / NEW_LOAD >= 0.99) recovered = t; if (recovered >= 0 && successes1 / NEW_LOAD < 0.99) recovered = -1; }
            }
            System.out.printf("%-58s %5.1f%%                  %5.1f%%              %6.0f req/s (%.1fx cap)    %s%n", names[sc], 100 * okIncident / newIncident, 100 * okAfter / newAfter, peak, peak / BASE_CAP,
                recovered < 0 ? "NEVER (still failing at t=180)" : (recovered - 90) + " s after the incident ended");
        }
    }

    // ====================================================================================================
    // 2) Bulkheads: one slow dependency must not take the whole service down (discrete-event simulation)
    // ====================================================================================================
    static void bulkheads() {
        System.out.println("\n== 2) Bulkheads and timeouts. A service has 100 worker threads and two endpoints: /fast (10 ms, 150 req/s) and /report (calls a dependency that normally takes 20 ms,");
        System.out.println("      150 req/s). From t=20 to t=50 s the dependency becomes slow (3 s per call). Callers give up after 1 s of waiting for a thread. ==");
        System.out.println("design                                              /fast success during incident   /fast success after the incident");
        record D(String name, boolean bulkhead, double timeout) {}
        for (D d : new D[]{new D("shared pool of 100 threads, no dependency timeout (3 s)", false, 3.0), new D("shared pool, 1 s dependency timeout", false, 1.0), new D("shared pool, 300 ms dependency timeout", false, 0.3),
                           new D("bulkhead 70/30 threads, no dependency timeout", true, 3.0), new D("bulkhead 70/30 threads, 1 s dependency timeout", true, 1.0)}) {
            Random r = new Random(5); int fastPool = d.bulkhead() ? 70 : 100, repPool = d.bulkhead() ? 30 : 0;
            PriorityQueue<Double> sharedFree = new PriorityQueue<>(), fastFree = new PriorityQueue<>(), repFree = new PriorityQueue<>();    // each entry = time a thread becomes free
            for (int i = 0; i < fastPool; i++) { sharedFree.add(0.0); fastFree.add(0.0); } for (int i = 0; i < repPool; i++) repFree.add(0.0);
            List<double[]> arrivals = new ArrayList<>();                          // {time, endpoint(0 fast,1 report)}
            for (int e = 0; e < 2; e++) { double t = 0; while (t < 80) { t += -Math.log(1 - r.nextDouble()) / 150.0; arrivals.add(new double[]{t, e}); } }
            arrivals.sort(Comparator.comparingDouble(x -> x[0]));
            int[] ok = new int[3], tot = new int[3];                               // /fast only: phase 0 before, 1 incident, 2 after
            for (double[] a : arrivals) {
                double t = a[0]; int e = (int) a[1]; int phase = t < 20 ? 0 : (t < 50 ? 1 : 2);
                PriorityQueue<Double> pool = d.bulkhead() ? (e == 0 ? fastFree : repFree) : sharedFree;
                double start = Math.max(t, pool.peek()); boolean gaveUp = start - t > 1.0;
                if (e == 0) tot[phase]++;
                if (gaveUp) continue;                                               // abandoned while waiting for a thread
                pool.poll();
                double service = e == 0 ? 0.010 : (phase == 1 ? d.timeout() : 0.020);
                pool.add(start + service);
                if (e == 0 && (start + service - t) <= 1.0) ok[phase]++;
            }
            System.out.printf("%-58s     %5.1f%%                          %5.1f%%%n", d.name(), 100.0 * ok[1] / tot[1], 100.0 * ok[2] / tot[2]);
        }
    }

    // ====================================================================================================
    // 3) Load shedding: goodput under overload (discrete-event, single server)
    // ====================================================================================================
    static void loadShedding() {
        System.out.println("\n== 3) Overload. One server handles 1,000 req/s (1 ms each). Clients give up after 1 s (a response later than that is useless). 60 s of Poisson traffic. ==");
        System.out.println("offered load    FIFO, unbounded queue          FIFO + bounded queue (max 100)        LIFO + drop requests older than 1 s");
        System.out.println("  (req/s)       goodput (useful responses/s)   goodput / rejected                    goodput / dropped");
        for (int lambda : new int[]{500, 900, 1000, 1200, 1500, 3000}) {
            double[] res = new double[6];
            for (int pol = 0; pol < 3; pol++) {
                Random r = new Random(9); double t = 0; List<Double> arr = new ArrayList<>();
                while (t < 60) { t += -Math.log(1 - r.nextDouble()) / lambda; arr.add(t); }
                int useful = 0, shed = 0; double serverFree = 0; int n = arr.size();
                if (pol == 0) {                                                       // FIFO, everything queued, server serves every request in order
                    for (double a : arr) { double start = Math.max(a, serverFree); serverFree = start + 0.001; if (serverFree - a <= 1.0) useful++; }
                } else if (pol == 1) {                                                // FIFO but reject when 100 are already waiting
                    ArrayDeque<Double> done = new ArrayDeque<>();                     // completion times of requests in the system
                    for (double a : arr) { while (!done.isEmpty() && done.peekFirst() <= a) done.pollFirst(); if (done.size() >= 100) { shed++; continue; }
                        double start = Math.max(a, serverFree); serverFree = start + 0.001; done.addLast(serverFree); if (serverFree - a <= 1.0) useful++; }
                } else {                                                              // LIFO: when idle pick the NEWEST waiting request; drop anything older than 1 s without doing work
                    int i = 0; ArrayDeque<Double> stack = new ArrayDeque<>(); double now = 0;
                    while (i < n || !stack.isEmpty()) {
                        if (stack.isEmpty()) { now = Math.max(now, arr.get(i)); }
                        while (i < n && arr.get(i) <= now) stack.addLast(arr.get(i++));
                        while (!stack.isEmpty() && now - stack.peekFirst() > 1.0) { stack.pollFirst(); shed++; }          // oldest entries are expired
                        if (stack.isEmpty()) continue;
                        double a = stack.pollLast(); now += 0.001; if (now - a <= 1.0) useful++; else shed++;
                    }
                }
                res[pol * 2] = useful / 60.0; res[pol * 2 + 1] = shed / 60.0;
            }
            System.out.printf("  %5d         %6.0f                         %6.0f / %6.0f                      %6.0f / %6.0f%n", lambda, res[0], res[2], res[3], res[4], res[5]);
        }
    }

    public static void main(String[] a) { retryStorm(); bulkheads(); loadShedding(); }
}
```
**Sample output**
```text
== 1) Retry storm. Backend capacity 1,000 req/s, normal load 700 req/s. For t=60..90 s the backend is degraded to 300 req/s. Trigger gone after 90 s. ==
   Model: under overload the backend wastes work on abandoned requests, so useful capacity shrinks (to as little as 30%).
policy                                         success during incident   success t=90..180 s   peak load on backend   back to >=99% success
A: no retries                                               22.0%                  100.0%                 700 req/s (0.7x cap)    0 s after the incident ended
B: 3 attempts, immediate retry (client only)                 5.2%                   98.1%                2007 req/s (2.0x cap)    4 s after the incident ended
C: 9 attempts (3 layers x 3), immediate                      2.7%                    6.0%                5931 req/s (5.9x cap)    NEVER (still failing at t=180)
D: 3 attempts, backoff + jitter                              5.3%                   96.9%                2007 req/s (2.0x cap)    6 s after the incident ended
E: 3 attempts, backoff + 10% retry budget                   18.3%                  100.0%                 770 req/s (0.8x cap)    0 s after the incident ended
F: 3 attempts immediate + circuit breaker (half-open ramp)   7.2%                   92.8%                1246 req/s (1.2x cap)    9 s after the incident ended

== 2) Bulkheads and timeouts. A service has 100 worker threads and two endpoints: /fast (10 ms, 150 req/s) and /report (calls a dependency that normally takes 20 ms,
      150 req/s). From t=20 to t=50 s the dependency becomes slow (3 s per call). Callers give up after 1 s of waiting for a thread. ==
design                                              /fast success during incident   /fast success after the incident
shared pool of 100 threads, no dependency timeout (3 s)         18.4%                           92.9%
shared pool, 1 s dependency timeout                             49.1%                          100.0%
shared pool, 300 ms dependency timeout                         100.0%                          100.0%
bulkhead 70/30 threads, no dependency timeout                  100.0%                          100.0%
bulkhead 70/30 threads, 1 s dependency timeout                 100.0%                          100.0%

== 3) Overload. One server handles 1,000 req/s (1 ms each). Clients give up after 1 s (a response later than that is useless). 60 s of Poisson traffic. ==
offered load    FIFO, unbounded queue          FIFO + bounded queue (max 100)        LIFO + drop requests older than 1 s
  (req/s)       goodput (useful responses/s)   goodput / rejected                    goodput / dropped
    500            502                            502 /      0                         502 /      0
    900            899                            899 /      0                         899 /      0
   1000            999                            996 /      4                         987 /     13
   1200             87                           1002 /    199                        1002 /    198
   1500             46                           1002 /    496                        1005 /    492
   3000             24                           1002 /   1985                        1011 /   1976
```
**Experiments**
1. In part 1, change the capacity loss to 50% (c = 500) and then to 90% (c = 100). For each retry policy, how does the metastable region change? What is the minimum amplification at which the system stays stuck? (Plot success after the incident vs retry attempts 1..9.)
2. Change the goodput function: set the minimum goodput floor to 80% instead of 30%. Which policies stay stuck now? What does this tell you about the severity of overload collapse in your real service (how would you measure it)?
3. Add **adaptive load shedding at the backend** (reject when offered > 1.1 × capacity, regardless of retries). Does the backend stay healthy under policy C? What is the new success rate during the incident?
4. Add a **client-side adaptive throttle** (Google SRE): `reject_probability = max(0, (requests − K × accepts) / (requests + 1))` with K = 2, computed over the last 30 s. Compare with the retry budget.
5. In part 2, give the pools **different sizes** and a **queue limit**; find the thread allocation that maximizes total success for both endpoints during the incident. Add a third endpoint and a priority tier.
6. In part 3, implement an **adaptive concurrency limit** (AIMD on observed latency) instead of a fixed queue bound of 100. How quickly does it find the right limit when capacity suddenly halves?
7. Add **token-bucket rate limiting** per client to part 3 with two clients (one abusive at 5× the normal rate). Show fairness before and after.

### Lab 2: Design and run a chaos experiment (paper/practical)
Pick a service from an earlier lab (the Ch 7 `MiniLb` with three backends works). Write the experiment card: **steady-state metric** (e.g., success rate ≥ 99.9%, p99 < 200 ms), **hypothesis** (“killing one backend does not reduce the success rate”), **blast radius** and abort conditions, **fault** (kill backend; add 500 ms latency to one backend; drop 20% of packets), and **observations**. Run it with `kill`, `tc netem`, or Toxiproxy. Record what you learned and the fixes.

### Lab 3: SLO and error-budget workshop (paper)
A checkout API has the SLO “99.95% of requests succeed within 400 ms, 30-day window.” (a) Compute the monthly error budget in minutes and in failed requests at 40 million requests/month. (b) A deploy causes a 20-minute 8% error rate: how much of the budget is consumed? (c) Design the multi-window burn-rate alerts (fast and slow) with thresholds and windows. (d) Decide what policy applies when 80% of the budget is consumed by day 12.

---

## 18.16 Interview perspective

- State reliability goals as **SLOs** and tie decisions to **error budgets**; quote **N+1/AZ-survivable** capacity and **RTO/RPO** numbers.
- For any remote call say: **timeout, retry (idempotent, backoff + jitter, budget), circuit breaker, bulkhead, fallback.**
- Name the **cascading failure and retry storm** mechanisms and the fix: shedding, budgets, breakers, bounded queues.
- Mention **metastable failures** and **graceful degradation** (what the page does without recommendations).
- Discuss **safe deployment** (canary, staged by region/cell, auto-rollback, feature flags) because most outages are change-induced.
- Cover **backups with tested restores**, **DR strategy** choices, and **game days/chaos testing**.
- Close with **incident response** and **blameless postmortems**.

---

## 18.17 Common misconceptions

1. **“Redundancy gives reliability.”** Only against independent failures; correlated and software failures dominate.
2. **“Retries make things more reliable.”** Unbudgeted, multi-layer retries cause outages (lab C: never recovers).
3. **“Higher timeouts are safer.”** Long timeouts hold resources and amplify overload.
4. **“A queue protects us from overload.”** An unbounded FIFO queue turns overload into congestion collapse (87 useful/s of 1,000 at 1.2× load).
5. **“Our service is up, so we’re fine.”** Slow is down; measure from the user’s perspective.
6. **“Autoscaling fixes overload.”** It lags, and new instances stress dependencies (Ch 6).
7. **“100% availability is the goal.”** Cost explodes; use SLOs and error budgets.
8. **“We have a DR region.”** Untested, drifted DR regions fail when needed.
9. **“Replication is our backup.”** It replicates mistakes instantly.
10. **“Root cause analysis finds the one cause.”** Incidents have multiple contributing factors.
11. **“Circuit breakers fix retry storms by themselves.”** They help but need ramped recovery, non-retried fast-fails, and fallbacks.
12. **“Chaos engineering means breaking production randomly.”** It is hypothesis-driven, controlled experimentation.

---

## 18.18 Chapter summary

- Failures come from hardware, bugs, **change**, dependencies, overload, data errors; **redundancy handles independent faults**, but severe outages are usually **correlated** or **self-inflicted**. Limit **blast radius** with failure domains, cells, shuffle sharding.
- **SLIs/SLOs/error budgets** make reliability measurable and tradeable; alert on **burn rate** (14.4× over 1 h = 2% of a 30-day budget).
- **Redundancy:** N+1, **AZ-survivable capacity (1.5× for 3 AZs)**, static stability; untested standbys don’t exist; choose DR (backup/restore, pilot light, warm standby, active–active) by RTO/RPO.
- **Timeouts and deadlines everywhere; retries only idempotent, one layer, backoff + jitter, with budgets.** Lab: three layers of 3 retries → 5.9× load and a **metastable failure that never recovered**; a 10% retry budget kept load at 0.8× and recovered instantly.
- **Circuit breakers** (half-open ramp, don’t retry fast-fails), **bulkheads** (lab: `/fast` success 18% → 100%), **load shedding** (lab: unbounded FIFO at 1.2× load → 87 useful/s vs 1,002 with bounded queue/LIFO), **rate limiting** (token bucket).
- **Graceful degradation:** classify dependencies, fallbacks that don’t depend on the failing system, fail-open vs fail-closed.
- **Metastable failures:** trigger + sustaining feedback loop; prevent by capping amplification and shedding; recover by breaking the loop and ramping.
- **Safe deployment** (canary, staged, flags, auto-rollback), **tested backups/PITR**, **chaos/game days/DR drills**, **blameless incident response**.

### Mental models
| Concept | Mental model |
|---|---|
| Blast radius | How much of the building burns when one room catches fire |
| Timeout | Hanging up the phone after a reasonable wait instead of holding forever |
| Retry storm | Everyone redialing a busy line, keeping it busy |
| Retry budget | A fixed number of redial attempts per hour for the whole office |
| Circuit breaker | A fuse: trips to protect the wiring, then is reset carefully |
| Bulkhead | Watertight ship compartments |
| Load shedding | A club bouncer turning people away so the people inside can still dance |
| Metastable failure | A traffic jam that persists after the accident is cleared |
| Error budget | A monthly allowance for risk |
| Static stability | A lifeboat already in the water, not one that must be launched during the storm |

---

## 18.19 Exercises

### A. Conceptual
1. Define fault, error, and failure. Give an example chain from fault to failure and two places to break it.
2. List five causes of outages and the dominant one in large systems. What does that imply for engineering effort?
3. Define SLI, SLO, SLA, error budget. Why measure at the user boundary?
4. What is burn rate? Explain multi-window multi-burn-rate alerting.
5. What is a failure domain? Name seven levels. What is blast radius and how do cells and shuffle sharding reduce it?
6. Explain AZ-survivable capacity and static stability. Why must failover not depend on the control plane?
7. Compare backup & restore, pilot light, warm standby, and active–active DR on RTO, RPO, and cost.
8. Why is a never-exercised standby effectively non-existent?
9. List the rules of safe retries. Why “retry at one layer”?
10. Explain exponential backoff with full jitter and why jitter matters.
11. What is a retry budget and how does it prevent a retry storm? Cite the lab.
12. Describe the circuit breaker states, the half-open ramp, and why fast-fail rejections must not be retried.
13. What is a bulkhead? Use Little’s Law to explain why the shared pool failed in the lab.
14. What is congestion collapse/goodput collapse? Why does an unbounded FIFO queue cause it?
15. Compare bounded-queue rejection, LIFO with expiry, and adaptive concurrency limits.
16. Token bucket vs leaky bucket vs fixed window: behavior and failure modes.
17. What is graceful degradation? Give four fallback strategies and the fail-open/closed decision for three services.
18. Define metastable failure with trigger and sustaining effect; list four common sustaining effects.
19. How do you recover from a metastable failure in production?
20. Describe a safe deployment pipeline. Why must config changes follow it too?
21. Why is “replication ≠ backup”? What does a minimal backup/DR practice include?
22. What is chaos engineering? How does it differ from random breakage?
23. List roles and phases of incident response. Why “mitigate first”?
24. What makes a postmortem blameless and useful?

### B. Calculations
25. SLO 99.95% over 30 days. Compute the budget in minutes. A 12-minute full outage and a 45-minute period with 5% errors: how much budget is consumed?
26. Burn-rate alert: SLO 99.9% monthly (30 days). Error rate is 0.5% for 2 hours. What is the burn rate, the fraction of the monthly budget consumed, and time to exhaustion if it continues? Which alert tier fires?
27. Provisioning: peak 12,000 req/s; per-instance capacity 400 req/s at 70% target utilization. Compute instance counts for (a) single AZ, (b) 3 AZs surviving loss of 1 AZ, (c) 4 AZs surviving loss of 1 AZ. What is the overhead in each?
28. Retry amplification: client retries 3, gateway retries 2, service retries 2 (attempts = retries + 1). Max attempts at the database per user request? If the database fails 30% of calls (independent), expected load multiplier at the database (assume retries until success or limit at each layer).
29. Backoff schedule: base 100 ms, factor 2, cap 5 s, 6 retries, full jitter. List the maximum delays, expected total delay, and worst-case total.
30. Token bucket: rate 50 req/s, burst 100. A client sends 300 requests in 1 second after being idle for 10 seconds, then 60 req/s steadily. How many are accepted in the first second? What is the steady acceptance rate afterwards?
31. Bulkhead sizing: `/search` 80 req/s with 50 ms normal latency; dependency may stall; timeout 400 ms. Compute threads needed normally and during a stall; choose pool sizes for `/search` and `/checkout` (120 req/s, 30 ms) with 40 total threads.
32. DR: database 4 TB; backups nightly at 02:00; WAL archiving every 5 minutes; restore throughput 200 MB/s plus 20 minutes replay and 30 minutes of DNS/app reconfiguration. Compute RPO, RTO. How to reduce RTO to 30 minutes?
33. Series availability: a request depends on 8 services at 99.95% each, of which 3 are optional with fallback. Compute availability with and without fallbacks. How many minutes per month does the fallback save?
34. Load shedding: capacity 2,000 req/s, offered 3,000 req/s; 20% critical, 80% best-effort. With priority shedding, what is the goodput by class? Without prioritization (random shedding)?

### C. Design
35. Fix StreamBox: list the changes (timeouts, bulkheads, degradation, shedding, deployment, backups) with concrete values.
36. Design the resilience policy for a service with three dependencies: payments (critical, idempotent with keys), recommendations (optional), and a feature-flag service (critical for startup, optional at runtime).
37. Design a multi-region active–active architecture with an RTO of 2 minutes and RPO of ~0 for orders; state the data layer and traffic management, failure modes, and test plan.
38. Create a chaos experiment plan for a Kafka-backed order pipeline: hypotheses, faults, abort criteria.
39. Design an alerting scheme for a user-facing API (SLIs, SLOs, burn-rate alerts, dashboards, runbooks).
40. Design the deployment strategy for a service with a schema change: steps, flags, compatibility, rollback, and monitoring.

### D. Debugging
41. Latency is fine but error rate spikes to 20% every day at the same time. List hypotheses (batch jobs, certificate expiry, cron, quota, resource exhaustion) and checks.
42. After a dependency outage ends, your service stays degraded for 40 minutes. Diagnose (retry backlog, cold cache, circuit breaker stuck open, autoscaler, connection pool) and fix.
43. An autoscaler adds instances during an incident and the database falls over. Explain and fix.
44. Your canary passes but the full rollout breaks. List five reasons and improvements.
45. A circuit breaker oscillates open/closed every few seconds. Why? Tune it.
46. Pages fire constantly for CPU > 80%, but users are happy. What do you change?

### E. Interview-style
47. “Design a resilient API gateway.”
48. “A downstream service is slow. How do you prevent it from bringing down your service?”
49. “How do you do a zero-downtime database migration?”

### F. Teach-back
50. Explain retry storms and retry budgets to a non-engineer using a restaurant that gets too many phone calls.

---

## 18.20 Full answer key

**1.** Fault: the underlying defect; error: the incorrect state it causes; failure: user-visible deviation. Example: a memory leak (fault) → heap exhaustion (error) → request failures (failure). Break the chain by preventing the fault (testing, leak detection), containing the error (restart, bulkhead), or masking the failure (fallback, redundancy).

**2.** Hardware, bugs, change/operator errors, dependencies, overload, data corruption, security, environment. Changes and operator errors dominate large outages, so invest in safe deployment, review, automation guardrails, observability, and fast rollback more than in hardware redundancy alone.

**3.** SLI: measured ratio of good events; SLO: target for it over a window; SLA: contractual promise with consequences; error budget = 1 − SLO. Measure at the user boundary because internal health can look fine while users are hurt (slow or partial failures, broken network paths).

**4.** Burn rate = observed error rate ÷ (1 − SLO). Multi-window/multi-burn alerts: page on fast burn (e.g., 14.4× over 1 h, confirmed over 5 min), page or ticket on slower burns (6× over 6 h, 1× over 3 days), catching both sharp and slow budget consumption with few false positives.

**5.** Levels: process, container, host, rack, power/zone, AZ, region (also provider/global control plane). Blast radius: the fraction of users/capacity affected by a failure. Cells partition users into independent stacks; shuffle sharding assigns each customer a random subset of workers so a poison customer affects few others.

**6.** To survive loss of one of 3 AZs, each AZ must carry 50% of peak, so provision 150% of peak; static stability means the system works in the degraded state without launching/reconfiguring capacity during the event, because the control plane may be impaired.

**7.** Backup & restore: RTO hours–days, RPO hours, lowest cost. Pilot light: RTO tens of minutes–hours, RPO minutes. Warm standby: RTO minutes, RPO seconds–minutes. Active–active: RTO seconds–minutes, RPO ≈ 0, highest cost.

**8.** Because configs drift, credentials expire, capacity isn’t present, and procedures have never been run; when the real failure comes it often fails. Only exercised paths are known to work.

**9.** Only idempotent ops/with keys; retriable errors only; bounded attempts; exponential backoff + jitter; budgets; retry at one layer (otherwise attempts multiply: 3×3×3 = 27).

**10.** Delay = random(0, min(cap, base·2^n)); exponential growth reduces pressure; jitter spreads clients to avoid synchronized waves.

**11.** A cap on retries as a fraction of normal traffic (e.g., 10%). In the lab, policy E kept peak load at 0.8× capacity and recovered instantly, while unbounded 9-attempt retries pushed the backend to 5.9× and never recovered.

**12.** Closed (normal), open (fail fast for a cool-down), half-open (trial traffic ramped, e.g., 10→25→50→100%; re-open on failure). Fast-fail rejections must not be retried; otherwise callers re-create the overload (and retry accumulation re-trips the breaker).

**13.** Bulkhead: separate resource pools per dependency/priority. Little’s Law: concurrency = rate × time = 150 × 3 s = 450 threads wanted against a 100-thread shared pool, so the pool saturates and healthy `/fast` fails (18%); with a 70/30 split `/fast` has its own 70 threads and stays at 100%.

**14.** Under overload the server spends capacity on requests that callers have abandoned; useful goodput falls toward zero. Unbounded FIFO: the queue grows, wait > deadline, every response is late (87/s useful vs 1,000/s capacity at 1.2×).

**15.** Bounded queue: reject early (cheap) keeping latency low; LIFO + expiry: serve the newest (still wanted) and discard stale; adaptive concurrency limits: automatically find the sustainable concurrency by watching latency (AIMD/gradient), adjusting to capacity changes.

**16.** Token bucket: burst up to b, average rate r; leaky bucket: constant drain rate, smooths bursts; fixed window: simple but allows 2× edge bursts; sliding windows more accurate but heavier.

**17.** Graceful degradation: provide reduced functionality instead of failing. Fallbacks: stale cache, defaults, read-only mode, feature disable, queue writes. Examples: payments fail closed; authorization usually fail closed; recommendations and ads fail open.

**18.** Metastable failure: a trigger moves the system into a bad state sustained by a feedback loop that persists after the trigger. Sustaining effects: retry amplification, cold caches, stale queue backlogs, connection/thread exhaustion, GC spirals, failover overload.

**19.** Break the loop: disable retries, shed/block traffic, rate-limit at the edge, restart components if needed, let queues drain, warm caches, then ramp traffic gradually while watching for a secondary spike.

**20.** Automated tests, canary with automated analysis, staged rollout by failure domain, bake time, flags/kill switches, automatic rollback, tested rollback, compatibility rules. Config changes cause many global outages, so they need the same staging and validation.

**21.** Replication copies deletions and corruption instantly. A minimal practice: periodic full + continuous log archiving, 3-2-1, immutable copies in a separate account, encryption, regular automated restore tests measuring RTO/RPO, a documented DR runbook, and drills.

**22.** Chaos engineering is hypothesis-driven experiments with controlled blast radius and a defined steady state to verify resilience; random breakage lacks hypothesis, control, and learning.

**23.** Detect → declare/assign roles (commander, operations, comms, scribe) → mitigate (rollback, failover, shed) → communicate → resolve → postmortem. Mitigate first because user impact ends sooner and diagnosis may take longer.

**24.** Blameless: focuses on system and process conditions that made the error possible, not individuals. Useful: timeline, contributing factors, impact, what went well/poorly, owned and tracked action items prioritized by effect, shared learning.

**25.** Budget = 0.05% × 43,200 = **21.6 min**. 12-minute outage = 12 min; 45 min at 5% errors = 2.25 min; total 14.25 min = **66%** of the budget.

**26.** Burn rate = 0.5% ÷ 0.1% = **5×**. Two hours at 5× consumes 5 × (2/720) = **1.39%** of the monthly budget; if it continued, the budget would be exhausted in 720/5 = **144 hours (6 days)**. Which alerts fire? The fast page needs 14.4× (not met). The 6× over 6 h tier needs a 0.6% error rate (5× < 6×: not met). The 1× over 3 days tier averages the 2-hour spike over 72 h: 5 × (2/72) = 0.14× (not met). So a 2-hour, 0.5% error episode fires **nothing**, and if it continued, the 3-day tier would only fire after 72/5 ≈ **14.4 hours**. That is too slow for a problem that would burn the whole budget in 6 days: add a mid tier (for example 3× over 12 h, or 2× over 24 h) so sustained moderate burns are caught within hours.

**27.** Per-instance capacity = 400 × 0.7 = 280 req/s. Needed for peak: 12,000/280 = 42.9 → **43 instances** (one AZ, no AZ redundancy). 3 AZs surviving loss of one: the remaining 2 must carry 43 → 22 per AZ ⇒ **66** instances total (+53%). 4 AZs surviving loss of one: the remaining 3 carry 43 → 15 per AZ (14.3→15) ⇒ **60** instances (+40%).

**28.** Attempts multiply: 4 (client) × 3 (gateway) × 3 (service) = **36** database calls in the worst case per user request. With 30% independent failure at the database and retries until success at the service layer (3 attempts): expected DB calls per service request = 1 + 0.3 + 0.09 = 1.39. If the gateway retries only after the service fails entirely (probability 0.3³ = 0.027), the amplification is small in the independent-failure case (~1.39 × ~1.03 × ~1.03); but when the failure is correlated (the DB is down) every layer exhausts its attempts and the multiplier approaches 36×. Retries multiply precisely during correlated failures.

**29.** Max delays: 100, 200, 400, 800, 1600, 3200 ms (all below 5 s cap). With full jitter the expected delay is half: 50, 100, 200, 400, 800, 1600 ms = 3,150 ms expected total; worst case 6,300 ms total.

**30.** Bucket starts full at 100 tokens (idle for 10 s); in the first second it accepts 100 (burst) + 50 refilled during the second ≈ **150** of 300. Afterwards, the steady-state acceptance rate is 50 req/s (the refill rate); 60 req/s offered → ~10 req/s rejected.

**31.** Normal: 80 × 0.05 = 4 threads. During a stall with a 400 ms timeout: 80 × 0.4 = 32 threads. `/checkout` normal: 120 × 0.03 = 3.6 → 4 threads (say 6 with headroom). With 40 total threads: give `/search` 24–28 (it will saturate during stalls but be contained), `/checkout` 12; reject excess `/search` quickly.

**32.** RPO ≈ 5 minutes (WAL archive interval; plus possible lag). RTO: restore 4 TB at 200 MB/s = 20,000 s = 5.6 h + 20 min + 30 min ≈ **6.4 hours**. Reduce RTO to 30 min via warm standby/replica with streaming replication and automated promotion, or snapshot-based restore with pre-staged storage, or PITR from storage snapshots plus fast attach.

**33.** All hard: 0.9995⁸ = 0.99601 → 99.60% (≈ 175 min/month downtime = 0.399% × 43,200 = 172 min). With 3 optional (fallbacks): 0.9995⁵ = 0.99750 → 99.75% (108 min/month). Savings ≈ **64 minutes per month**.

**34.** Capacity 2,000 of 3,000 offered. Critical = 600 req/s, best-effort = 2,400 req/s. With priority shedding: critical 600 fully served; best-effort 1,400 served (of 2,400): goodput 2,000 total. With random shedding: each request accepted with probability 2/3: critical 400 served (200 lost!), best-effort 1,600.

**35 (sketch).** Timeouts: 300–500 ms for optional calls, 1–2 s for critical; deadline propagation; recommendations behind a bulkhead (separate pool of 30 threads) with fallback to cached/popular list; load shedding at gateway (bounded queue, 429); one retry layer with 10% budget; circuit breakers per dependency; canary + automatic rollback + kill switch for deploys; synthetic and burn-rate alerting; restore-tested backups; DR drill.

**36 (sketch).** Payments: timeout 2 s, idempotency key, retry once with backoff, breaker, fail closed; recommendations: timeout 200 ms, no retries, bulkhead, fail open to cached; flag service: cache last-known flags locally (fail static), refresh in background, fail open to defaults at runtime; startup fetch with retry + fallback to bundled defaults.

**37 (sketch).** Two or three regions; orders in a consensus-replicated DB (RPO≈0) or sync cross-region with regional write ownership; global traffic management with health checks (anycast/DNS, low TTL); stateless services warm in all regions with capacity for loss of a region (N+1 regions); runbooks, automated failover, regular region-evacuation drills.

**38 (sketch).** Hypotheses: broker loss doesn’t lose acknowledged orders; consumer lag recovers within 5 minutes; duplicate processing is idempotent. Faults: kill a broker, partition a consumer, slow disk on a broker, delay ZooKeeper/KRaft. Abort when the order success rate falls below 99% or lag > 15 minutes. Check duplicates and gaps.

**39 (sketch).** SLIs: success ratio and latency at the load balancer per journey; SLOs 99.9%/300 ms; burn-rate alerts (14.4× 1 h, 6× 6 h, 1× 3 d); dashboards: RED + saturation per dependency; runbooks linked from alerts; no cause-based CPU pages.

**40 (sketch).** Expand (add nullable column), deploy code that writes both, backfill in batches, switch reads behind a flag, verify, contract later; each step backward compatible; canary the code and monitor errors/latency; rollback by flipping the flag; keep old column until confident.

**41.** Cron/batch jobs competing for resources; certificate/token expiry on a schedule; quota resets; traffic pattern (time zone peak) hitting a limit; cache TTL alignment (avalanche); nightly backup/compaction. Check time-correlated metrics, scheduled tasks, cert expiries, dependency quotas, cache expiry patterns.

**42.** Candidates: retry backlog/queue still draining; cold caches; circuit breaker stuck open (no half-open) or too long cool-down; autoscaler scaled down/oscillating; exhausted/leaked connection pools; thundering herd. Fixes: ramped reintroduction, bounded queues, warm caches, breaker tuning, connection pool recovery, load shedding.

**43.** New instances open many connections/warm caches, overloading the DB (connection storm). Fix: connection pooler, cap scale-out step, limit connections per instance, scale on queue/saturation signals with dependency-aware limits, stagger starts.

**44.** Canary traffic unrepresentative (wrong region/user mix); low volume hides rare errors; slow-burn leaks (memory) appear later; feature flags differ; dependency capacity only exhausted at full traffic; cache warm vs cold. Improve with representative canaries, longer bake, automated metrics, staged rollout, and load tests.

**45.** Thresholds too tight with small sample sizes, short cool-down, half-open allows too much traffic, or the breaker counts slow calls without hysteresis. Tune: minimum request volume, longer cool-down with jitter, staged half-open ramp, rolling window.

**46.** Page on user-facing SLO burn, not CPU; keep CPU as a dashboard/ticket; create runbooks; remove non-actionable alerts; tune thresholds with saturation indicators.

**47 (sketch).** Gateway with timeouts per route, bounded queues, concurrency limits per upstream (bulkheads), outlier ejection and breakers, retry budgets, rate limits/quotas, health-checked upstreams, canary routing, multi-AZ with static stability, shed by priority, observability, config staged rollout.

**48 (sketch).** Per-dependency timeouts and bulkheads (separate pools/queues), circuit breaker with fallback, bounded queue + shedding, deadline propagation, caching stale data, and alarms on dependency latency; test via fault injection.

**49 (sketch).** Expand/migrate/contract: add new structures compatibly, dual-write, backfill with throttling and verification, switch reads gradually (flag), monitor, remove old; use online schema change tools; keep rollback paths until verified.

**50.** A restaurant’s phone line gets busy; frustrated callers redial immediately, so the line stays busy even after the kitchen catches up. A retry budget says “each caller may redial only a few times and the whole office may redial at most 10% as often as normal calls,” so the kitchen recovers and the line clears.

---

## 18.21 Flashcards

| Front | Back |
|---|---|
| Fault → error → failure | Defect → bad state → user-visible deviation |
| Biggest outage cause | Change / operator error |
| SLO budget (99.9%/30 d) | 43.2 min |
| Burn rate | error rate ÷ (1 − SLO); 14.4× over 1 h = 2% of monthly budget |
| AZ-survivable capacity | 3 AZs → 1.5× peak; static stability |
| DR ladder | Backup/restore → pilot light → warm standby → active–active |
| Timeouts | Everywhere; shrink with depth; propagate deadlines |
| Retry rules | Idempotent only, 1 layer, backoff + jitter, budget (≈10%) |
| Lab retry storm | 9 attempts → 5.9× load, never recovered; budget → 0.8× load, instant recovery |
| Circuit breaker | Closed → open → half-open ramp; don’t retry fast-fails |
| Bulkhead | Separate pools; lab: /fast 18% → 100% |
| Load shedding | Bounded queue / LIFO + expiry; lab: 87 → 1,002 useful/s at 1.2× |
| Token bucket | Burst b, rate r |
| Graceful degradation | Classify deps, fallback independent of failure, fail-open vs closed |
| Metastable failure | Trigger + sustaining feedback loop that outlives the trigger |
| Recovery | Break loop (stop retries, shed), drain, ramp |
| Safe deploy | Canary, staged by domain, bake time, flags, auto-rollback |
| Replication vs backup | Replication copies mistakes; backups + PITR undo them |
| Test restores | An untested backup is a hope |
| Chaos engineering | Hypothesis + controlled blast radius |
| Postmortem | Blameless, contributing factors, owned actions |

## 18.22 Glossary additions

| Term | Simple Meaning | Technical Meaning | Why It Exists | Example |
|---|---|---|---|---|
| Fault tolerance | Keep working despite faults | Prevent faults becoming failures | Reliability | Replicas |
| SLI / SLO / SLA | Measure / target / contract | Ratio indicator, objective, agreement | Manage reliability | 99.9% |
| Error budget | Allowed unreliability | 1 − SLO over a window | Balance risk | 43.2 min |
| Burn rate | Speed of budget use | Error rate ÷ (1 − SLO) | Alerting | 14.4× |
| Failure domain | Fails together | Set of correlated components | Placement | AZ |
| Blast radius | Damage extent | Fraction of users/capacity affected | Containment | One cell |
| Static stability | Works without reacting | Pre-provisioned capacity for failure | Avoid control-plane dependence | 1.5× across 3 AZs |
| RTO / RPO | Time / data loss tolerance | Recovery targets | DR design | 5 min / 1 min |
| Timeout / deadline | Wait limit | Per-call / end-to-end budget | Bound resource use | 300 ms |
| Retry budget | Retry allowance | Cap on retries as fraction of traffic | Prevent storms | 10% |
| Backoff + jitter | Spaced random retries | Exponential delay with randomization | Avoid herds | 100–200–400 ms |
| Circuit breaker | Fail-fast fuse | Open/half-open/closed dependency guard | Protect dependencies | Resilience4j |
| Bulkhead | Resource compartment | Isolated pools per dependency | Contain failures | 70/30 threads |
| Load shedding | Reject excess | Drop work to preserve goodput | Overload control | 503 |
| Goodput | Useful throughput | Responses delivered before deadline | True capacity | 87/s vs 1,000/s |
| Congestion collapse | Busy but useless | Throughput of useful work collapses | Overload failure | Unbounded FIFO |
| Adaptive concurrency | Self-tuning limit | AIMD/gradient on latency | Find capacity | Netflix limits |
| Token bucket | Burst-friendly limiter | Tokens refilled at rate r, capacity b | Rate limiting | 50/s, burst 100 |
| Graceful degradation | Do less | Reduced functionality under failure | Availability | Hide widget |
| Metastable failure | Self-sustaining outage | Bad state persisting after trigger | Outage class | Retry storm |
| Canary | Small trial rollout | Gradual release with analysis | Safe change | 1% traffic |
| Chaos engineering | Controlled breakage | Hypothesis-driven fault experiments | Verify resilience | Kill instance |
| Game day | Rehearsal | Practiced incident/DR drill | Preparedness | Region failover |
| Blameless postmortem | Learning review | System-focused incident analysis | Improvement | Action items |

---

**Next: Chapter 19: Microservices and Architectural Patterns.** When splitting a system pays off and when it hurts (with measured latency, availability, and chattiness costs), service boundaries and data ownership, communication styles, API gateways/BFF/strangler patterns, service meshes, the monolith-first path, modular monoliths, event-driven architecture, serverless, and team topology (Conway’s law).

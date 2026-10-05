# Chapter 21: Observability

## Chapter card

| | |
|---|---|
| **Objective** | Learn to make a distributed system *understandable from the outside*: metrics, logs, and traces (and what each is good for), the cost and cardinality traps, correct latency aggregation, trace sampling, SLO-based alerting that pages humans only when it matters, dashboards and investigation workflows, and the architecture and economics of an observability pipeline |
| **Why this chapter exists** | Every earlier chapter told you to "monitor X." In a system of dozens of services and thousands of instances, you cannot debug by logging into a box. Observability is what turns the reliability practices of Chapter 18 (SLOs, burn rates, incident response) into something you can actually do. Interviewers expect you to name concrete signals, thresholds, and the cost trade-offs |
| **Prerequisites** | Chapters 5 (percentiles, USE/RED), 7, 12, 14, 18 (SLOs, alerting, incidents), 20 (sketches, mergeable quantiles) |
| **Study time** | 10–13 hours including labs |
| **Tier** | `[GK]` throughout; metric/alert design `[MK]` |
| **Mastery criteria** | (1) Distinguish monitoring from observability and the roles of metrics, logs, traces, profiles. (2) Choose RED/USE/golden signals for a system. (3) Compute and control metric cardinality. (4) Aggregate latency correctly (histograms/sketches, never averaged percentiles). (5) Design structured logging and tracing with context propagation and sampling. (6) Design SLO burn-rate alerts that avoid false pages. (7) Follow a systematic investigation workflow. (8) Size and cost an observability pipeline. (9) Observe the observability system itself |

---

## 21.1 The Story: Green Dashboards, Angry Customers

**ShopLine** pages its on-call engineer at 03:07: "checkout slow." What she finds:

1. **The dashboards are green.** Average checkout latency 180 ms (normal). CPU on every service under 40%.
2. **Customers disagree.** Support has 300 tickets of timeouts. She suspects a subset of users, but metrics are aggregated; she cannot slice by region, device, or payment provider. The one metric that might (`checkout_duration{user_id=...}`) was removed last month after it crashed the metrics server (2 million distinct label values).
3. **Logs:** 400 GB/day, plain text, no request IDs. A grep across 60 hosts takes 25 minutes and returns 8 million lines. The logs say `ERROR payment failed` with no context.
4. **No traces.** She cannot tell which of the nine services involved in checkout is slow. The architecture diagram is two years out of date.
5. **Alerts:** her phone has buzzed 31 times this week for "CPU > 80%" and "disk 85%" with no user impact, so she muted the channel. The one real alert fired at 02:40 and was lost in the noise.
6. **"What changed?"** Nobody knows whether a deploy happened. The deploy system and the dashboards share no timeline.
7. After 90 minutes someone remembers that the payment provider's API had been intermittently slow for **European cards only**. The p99 for that segment was 14 seconds. The average hid it.

### Pause & Predict 21.1
> List the specific observability capabilities ShopLine lacked (at least six). For each, name the signal (metric, log, trace, profile, event, alert) and the design change. Which single capability would have found the problem fastest?

---

## 21.2 Monitoring vs observability `[MK]`

- **Monitoring:** collecting predefined signals and alerting on known failure conditions (CPU, disk, error rate). It answers *known unknowns*: "is the thing I thought could break broken?"
- **Observability:** the ability to ask **arbitrary new questions** of a running system and get answers from the data it already emits, without shipping new code. It answers *unknown unknowns*: "why are European card payments slow only on mobile?"
It depends on emitting **rich, high-dimensional, context-carrying telemetry**, and on tools that can slice it.

### 21.2.1 The signals
| Signal | What it is | Best at | Weakness |
|---|---|---|---|
| **Metrics** | Numeric time series (counters, gauges, histograms), aggregated | "Is something wrong? How bad? Trend?", alerting, capacity | Lose individual requests; cardinality limits |
| **Logs** | Timestamped records of discrete events | Detailed *what happened* for one case; audit | Voluminous, expensive, hard to correlate unless structured |
| **Traces** | A request's path across services as a tree of timed spans | *Where* time went and *which* dependency failed | Sampling needed; instrumentation effort; async gaps |
| **Profiles** | CPU/memory/lock samples by code location | *Why* a service burns resources (hot functions) | Overhead, lower-level |
| **Events/changes** | Deploys, config changes, flag flips, incidents | "What changed?" correlation | Needs integration |
| **Real-user & synthetic monitoring** | Client-side timings; scripted probes | The user's view; external reachability | Limited depth |
No single signal suffices: **metrics tell you that, traces tell you where, logs tell you why, profiles tell you which line.** Link them with shared identifiers (trace ID, request ID, deployment version, tenant).

### 21.2.2 What to instrument
Every service should emit a standard core: **request rate, error rate, latency distribution** (per route, per dependency), **saturation** (queues, thread pools, connection pools), **dependency health from the caller's view**, **business metrics** (orders/min), **version/deployment info**, plus structured logs with correlation IDs and trace context. Standardize via libraries or a service template so every team gets it free ("paved road").

---

## 21.3 Metrics `[MK/GK]`

### 21.3.1 Types
| Type | Behavior | Example | Notes |
|---|---|---|---|
| **Counter** | Monotonically increasing (resets on restart) | `http_requests_total` | Query as a **rate** over a window; handle resets |
| **Gauge** | Goes up and down | `queue_depth`, `memory_bytes` | Sampled value; fast spikes between scrapes are invisible |
| **Histogram** | Counts of observations in configured buckets (+ sum, count) | `request_duration_seconds_bucket{le="0.25"}` | Aggregatable across instances; quantiles estimated |
| **Summary** | Client-side computed quantiles | `request_duration{quantile="0.99"}` | **Not aggregatable** across instances |
| **Distribution / sketch** | Mergeable quantile sketch (DDSketch/t-digest) | Datadog distributions, OTel exponential histograms | Accurate and mergeable |

### 21.3.2 The standard signal sets
- **RED (for request-driven services):** **R**ate (requests/s), **E**rrors (failed requests/s or ratio), **D**uration (latency distribution).
- **USE (for resources: CPU, memory, disk, network, pools):** **U**tilization (busy fraction), **S**aturation (queued/waiting work), **E**rrors.
- **Four golden signals (Google SRE):** latency (separating successful from failed requests), traffic, errors, saturation.
- Add **business/product metrics** (checkouts completed, signups) and **freshness/correctness** metrics for data pipelines.
Latency distributions: always percentiles (p50/p95/p99/p99.9) or histograms/heatmaps, never only averages (Ch 5).

### 21.3.3 Collection models
- **Pull (Prometheus):** the server scrapes `/metrics` endpoints every 15–60 s; discovers targets via service discovery; easy to see which targets are down; short-lived jobs need a push gateway.
- **Push (StatsD, OTLP, Datadog agent):** applications/agents send to a collector; good for ephemeral jobs and firewalled networks; need backpressure and sampling.
- **Scrape interval = resolution:** 15 s resolution cannot show a 5-second spike; use histograms for latency (which accumulate between scrapes) and counters rather than sampled gauges where possible.
- **Storage:** time-series databases use delta-of-delta timestamp and XOR value compression (Gorilla): about **1.3–1.4 bytes per sample**; memory per **active series** is a few KB, which is why *series count* dominates cost. Long-term: Thanos/Mimir/VictoriaMetrics/Cortex with object storage, downsampling (5-min, 1-h rollups), retention tiers.

### 21.3.4 Cardinality: the cost driver `[MK]`
**Cardinality** = number of distinct label-value combinations = number of time series. Each series costs memory (~3 KB active), storage (5,760 samples/day at 15 s scrape), and query time. Labels with unbounded values (user ID, request ID, email, URL with IDs, container ID churn) cause **cardinality explosions** that crash metric stores and bankrupt budgets.

**Measured (Lab 1, part 1).** 2,000,000 requests across 40 services × 25 endpoints × 4 methods × 4 regions × 30 pods:
```text
label design                                  active series      TSDB memory      samples/day       storage/day (compressed)
A: service, endpoint, status_class                  4,000         0.01 GB         23,040,000           0.03 GB
B: A + method + region                             61,157         0.18 GB        352,264,320           0.49 GB
C: B + pod                                        624,206         1.87 GB      3,595,426,560           5.03 GB
D: C + exact status code                          639,798         1.92 GB      3,685,236,480           5.16 GB
E: C + user_id (200k users)                     1,999,971         6.00 GB     11,519,832,960          16.13 GB
F: C + request_id (unique)                      2,000,000         6.00 GB     11,520,000,000          16.13 GB
```
**Reading it.** Each added dimension *multiplies*: adding `pod` (30 values) to design B multiplies series ×10 (61 k → 624 k; the product is capped by the traffic actually observed, not the theoretical 40×25×4×4×30×5 = 2.4 M). Exact status codes barely matter (+2.5%) because few codes occur. But **one unbounded label (`user_id` or `request_id`) turns 624 k series into 2 million, i.e., one series per request in this sample**: 6 GB of server memory and 16 GB/day of storage for a single metric, and it grows without bound with traffic. That is ShopLine's crashed metrics server. **Rules:** (1) labels must have a small, bounded set of values (tens to hundreds); (2) never put IDs, emails, raw URLs, timestamps, or error messages in labels; (3) put high-cardinality context into **logs and traces** (which are per-event) and link to it using **exemplars** (a trace ID attached to a histogram bucket sample); (4) budget cardinality per team and alert on series growth; (5) drop or aggregate noisy labels (pod/instance) at ingestion when only service-level views are needed.

### 21.3.4a Naming and label hygiene
Use consistent names and base units (`_seconds`, `_bytes`, `_total` for counters), a `service`/`job` label, `env`, `region`, `version`; avoid embedding label values in names; document semantics; keep the same label set across related metrics so joins work.

### 21.3.5 Latency aggregation: the percentile trap, measured
You cannot compute a fleet-wide p99 by averaging per-server p99s: percentiles are not additive, and averaging ignores how much traffic each server handled. **Measured (Lab 1, part 2):** 20 servers with **unequal traffic** (10,000 to 200,000 requests each); server #3 has a heavy tail but only 10,000 requests. "TRUE" is computed from all raw samples:
```text
method                                                   p50 (ms)    p95 (ms)    p99 (ms)    p99.9 (ms)
TRUE (all raw samples)                                     11.23       28.80       42.71        68.73
average of the 20 per-server p99s                              -           -       47.82            -
maximum of the 20 per-server p99s                              -           -      167.47            -
merged histogram, 8 coarse buckets (5..1000 ms)            12.42       35.20       48.30        91.21
merged histogram, 200 exponential buckets (6% steps)       11.24       28.85       42.80        68.82
mergeable log-bucket sketch (DDSketch style)               11.13       29.08       42.52        68.72   (346 buckets, guaranteed <=1% relative error)
```
**Reading it.** (1) The **average of per-server p99s (47.8 ms) overstates the true p99 (42.7 ms) by 12%** because the tiny, slow server counts as much as the huge ones; the **maximum** (167 ms) is off by 4×. Neither is the right number. (2) **Merging histograms is correct** (counts add), but accuracy depends on bucket layout: the typical 8 coarse buckets give p99.9 = 91 ms vs 68.7 true (**33% error**) because interpolation inside a wide bucket is a guess; 200 exponential buckets with 6% steps are within 0.2%; a DDSketch-style logarithmic sketch gives a guaranteed ≤1% relative error with 346 sparse buckets. **Instrument latency as histograms with bucket boundaries around your SLO thresholds (or use exponential/native histograms or sketches), and aggregate by summing buckets before computing quantiles.**

### 21.3.6 Recording rules, exemplars, and query cost
Precompute expensive aggregations as **recording rules**; keep dashboards fast; use **exemplars** to jump from a latency bucket to an example trace; watch query concurrency; limit regex on high-cardinality labels.

---

## 21.4 Logs `[MK/GK]`

### 21.4.1 Structured logging
Plain text lines (`ERROR payment failed`) are for humans reading a terminal; at scale you need **structured events** (JSON or key=value) that machines can filter and aggregate:
```json
{"ts":"2026-10-04T03:07:12.481Z","level":"ERROR","service":"checkout","version":"2026.40.3","env":"prod",
 "trace_id":"4bf92f3577b34da6a3ce929d0e0e4736","span_id":"00f067aa0ba902b7","request_id":"req_8f2a",
 "route":"POST /checkout","tenant":"acme","region":"eu-west","payment_provider":"cardnet",
 "event":"payment_failed","error.kind":"upstream_timeout","duration_ms":14020,"attempt":2,"user_hash":"u_91af"}
```
Guidelines: one event per line; consistent field names (a schema/dictionary); **always include trace/request IDs and service/version**; log **context** (who/what/where) and **outcome + duration** rather than narrative; use levels consistently (ERROR = needs attention, WARN = degraded, INFO = business events, DEBUG = off in prod or sampled); never log secrets, tokens, full card numbers, or raw PII (hash/redact; Ch 22); avoid logging in hot loops; be careful with multi-line stack traces (single JSON field).

### 21.4.2 Wide events / canonical log lines
Rather than dozens of scattered log lines per request, emit **one wide event per request per service** (a "canonical log line") with 50–200 attributes (route, user tier, cache hits, DB calls, bytes, feature flags, dependency timings, error). Wide events make *any* question answerable by filtering and grouping ("p99 by region and payment provider for mobile users") with no pre-aggregation, and they are the backbone of the "observability 2.0" approach (Honeycomb and similar). Cost: high cardinality needs a columnar event store (ClickHouse, Honeycomb, BigQuery-like).

### 21.4.3 The logging pipeline and its economics
```text
 app (stdout/JSON) → node agent (Fluent Bit/Vector/OTel Collector) → [buffer: Kafka] → processing (parse, enrich, redact, sample, route)
        → hot index (Elasticsearch/OpenSearch/Loki/ClickHouse; 7–30 days) → warm/cold object storage (S3; months/years) → archive/compliance
```
*Cost model:* volume (GB/day) × retention × (storage + indexing + query). *Example:* 20,000 req/s × 1.5 KB of log per request = 30 MB/s = **2.6 TB/day**; at 30 days hot retention that is 78 TB of indexed data (with 1.5× index overhead ≈ 117 TB). Full-text indexing every field is the main cost driver. Controls: sample DEBUG/INFO, drop noisy lines, index only chosen fields (Loki indexes labels only and stores compressed chunks), tier cold data, enforce per-team budgets, convert frequent log queries into metrics ("log-based metrics").
- **Elasticsearch/OpenSearch:** powerful full-text search; heavy index/storage cost; operationally demanding. **Loki:** cheap (label index only), grep-like queries; limited ad-hoc analytics. **ClickHouse/columnar stores:** efficient high-cardinality analytics. **Splunk/Datadog:** managed, expensive at volume.
- Reliability: logging must not take the app down (async, bounded buffers, drop policy under pressure); local disk buffering during pipeline outages; backpressure.
- Security: access control, audit, PII scrubbing, retention limits (GDPR), protection against **log injection** (newlines/control characters in user input).

---

## 21.5 Distributed tracing `[GK]`

### 21.5.1 Model
A **trace** records one request's journey as a tree of **spans**. A span has: trace ID (shared by the whole request), span ID, parent span ID, name/operation, start time and duration, status, **attributes** (route, DB statement summary, peer service), events (logs within the span), and links.
```text
 trace 4bf9…  POST /checkout                                     total 14.2 s
 ├─ gateway      auth                    ██ 12 ms
 ├─ checkout     reserve inventory       ███ 40 ms
 │   └─ inventory  SELECT stock         ██ 22 ms
 └─ checkout     charge payment          ██████████████████████████████ 14.0 s      ← the problem is obvious
     └─ payments   POST cardnet.example  ██████████████████████████████ 13.9 s (timeout, attempt 2)
```
A trace answers: *which service/dependency is slow or failing, what is the critical path, what was the call graph for this request.* Aggregated, traces give a **service dependency map**.

### 21.5.2 Context propagation
For a trace to cross processes, the trace context must be passed along every hop: the standard is **W3C Trace Context**: `traceparent: 00-<trace-id>-<parent-span-id>-<flags>` (plus `tracestate`, and `baggage` for user-defined key-values). HTTP/gRPC headers carry it; for messaging, put it in message headers (Kafka headers, SQS attributes) and create **linked spans** for asynchronous consumers (a consumer span *links* to the producer span, since the consumer may run long after). Inside a process, context travels through thread-local/context objects and **must be explicitly propagated across thread pools, async continuations, and reactive pipelines**: the most common cause of "broken traces."

### 21.5.3 OpenTelemetry (OTel)
The vendor-neutral standard (CNCF) for telemetry: **API/SDKs** (instrument code), **auto-instrumentation** agents for common libraries (HTTP, JDBC, gRPC, Kafka, Redis), the **OTLP** wire protocol, and the **Collector** (receive → process (batch, filter, redact, sample) → export to any backend: Jaeger, Tempo, Zipkin, Datadog, Honeycomb, X-Ray). Use OTel instead of vendor SDKs to avoid lock-in; the same SDK emits metrics, logs, and traces with shared resource attributes (service.name, version, environment).

### 21.5.4 Sampling: the key trade-off, measured
Tracing everything is usually unaffordable (every request × 20–50 spans). **Head-based sampling** decides at the start of the trace (e.g., keep 1%): cheap, but it is blind to the outcome, so it discards most of the interesting traces. **Tail-based sampling** decides after the trace completes (keep all errors and slow traces plus 1% of the rest): captures what you need, but the collector must buffer every in-flight trace until it finishes.

**Measured (Lab 1, part 3).** 1,000,000 requests; 0.5% fail, 1% are slow (about 14,800 "interesting"); budget ≈ 1% of normal traffic:
```text
strategy                                        traces stored   fraction stored   interesting traces captured
head-based, random 1%                                 9897          0.99%             170 of 14848  (1.1%)
tail-based: keep all errors/slow + 1% of the rest    24604          2.46%           14848 of 14848  (100.0%)
cost of tail-based sampling: buffer every in-flight trace until it completes = 20000 req/s x 0.8 s x 30 spans x 500 B = 240 MB of collector memory (before replication/headroom)
```
Head sampling at 1% kept 9,897 traces but **only 170 of the 14,848 failed or slow ones (1.1%)**: when ShopLine's on-call looked for a failing European checkout trace, there was almost surely none. Tail sampling stored 2.5× as many traces (24.6 k) and captured **100%** of the interesting ones, at the price of ~240 MB of collector buffer (tiny compared with the value) and the engineering requirement that *all spans of a trace reach the same collector* (route by trace ID to the same collector instance, e.g., with a load-balancing exporter). Hybrid practice: always-on tracing of errors and latency outliers, adaptive per-service rates, higher sampling for new deployments, and a **debug flag** (force-sample a specific request, user, or tenant).

### 21.5.5 Pitfalls
- **Broken context** at async boundaries, queues, thread pools, third-party libraries.
- **Clock skew** across hosts distorts span ordering (rely on parent-child structure and durations measured within a process).
- **Attribute explosion**: high-cardinality attributes are fine on spans (per-event) but huge payloads are not; redact PII; cap attribute sizes.
- **Instrumentation gaps** (uninstrumented hops appear as unexplained time).
- **Cost** (span volume, retention): sample, filter noisy health checks, and limit span counts per trace.
- Not a replacement for metrics: use traces for *explanation*, metrics for *detection*.

---

## 21.6 Profiling, eBPF, RUM, and synthetic checks `[GK]`

- **Continuous profiling** (pprof, async-profiler, Pyroscope, Parca, Datadog Profiler): always-on, low-overhead (1–3%) sampling of CPU, allocations, locks, with flame graphs over time. Answers "why does this service use 40% more CPU since Tuesday's deploy?" and complements traces (which tell you *which service*).
- **eBPF-based observability** (Pixie, Cilium/Hubble, Parca): kernel-level capture of network flows, syscalls, and latency without code changes; excellent for dependency discovery and unmodified binaries.
- **Real User Monitoring (RUM):** browser/mobile SDKs report page load, Core Web Vitals, JS errors, API timing as users experience them (network + device effects invisible server-side).
- **Synthetic monitoring:** scripted probes from multiple regions hitting key journeys (login, checkout) at fixed intervals: detects outages when there is no traffic, validates DNS/CDN/TLS from the outside, gives a stable baseline.
- **Black-box vs white-box:** black-box probes see what users see; white-box instrumentation explains why. Use both.
- **Change events:** deploys, config pushes, feature-flag flips, scaling events, incidents, and infrastructure changes recorded as annotations on dashboards: the cheapest big win for "what changed?"

---

## 21.7 SLO-based alerting `[MK]`

### 21.7.1 Principles
1. **Page on symptoms users feel (SLO burn), not on causes** (CPU, disk, one pod down). Causes go to dashboards and tickets.
2. **Every page must be actionable, urgent, and require a human** (otherwise it is a ticket, a dashboard, or an automatic remediation).
3. **Fewer, better alerts:** an alert that fires and gets ignored teaches people to ignore alerts. Track alert quality: pages per on-call shift (aim for ≤ 2 actionable pages per 12-hour shift), % actionable, time-to-acknowledge.
4. **Multi-window, multi-burn-rate** conditions (Ch 18): a long window for significance and a short window for recency, so the alert fires quickly and stops firing quickly.
5. Each alert links to a **runbook**, **dashboard**, and **owner**; supports **silencing/maintenance**; and is tested.
6. A **dead-man's switch** (an alert that should always fire and pages if it *stops*) detects a broken alerting pipeline.
7. Alert on **absence** (no data) carefully: missing metrics can mean a dead exporter or a dead service.

### 21.7.2 Measured: naive thresholds vs burn-rate alerts (Lab 1, part 4)
Thirty days of per-minute error rates for a service with SLO 99.9% (budget 43.2 error-minutes) containing: a 10-minute hard outage (A), a **3-day slow leak at 0.5% errors (B: a 5× burn)**, a 30-minute 2% error episode (C), and **25 harmless 1–2-minute blips** at 2–4% errors:
```text
detector                                                         alert groups   FALSE alerts   detected A / B / C (minutes after start; budget burned before alert)
N1: page if error rate > 1% for 1 minute                             27            23        A: 0 min (2% of budget) | B: 2113 min (25% of budget) | C: 0 min (0% of budget)
N2: page if 5-minute average error rate > 0.5%                       27            22        A: 0 min (2% of budget) | B: 4 min (0% of budget) | C: 1 min (0% of budget)
BR: multi-window burn-rate (14.4x 1h&5m, 6x 6h&30m page; 1x 3d&6h ticket)      2             0        A: 0 min (2% of budget) | B: 698 min (8% of budget) | C: MISSED
```
**Reading it carefully.**
- **N1 (error rate > 1% for a minute)** fired **27 times, 23 of them false** (transient blips): ~5 pages a week of noise, exactly the alert fatigue in the Story. Its "detection" of the slow leak B came only at 2,113 minutes (35 hours, **25% of the monthly budget gone**), and only because a random blip happened to land inside the incident; the leak itself (0.5%) never crossed 1%.
- **N2 (5-minute average > 0.5%)** also produced 22 false alerts, and its quick "detection" of B (4 minutes) is **luck of the threshold**: the leak runs at 0.52% (0.5% + baseline noise), barely above 0.5%. At 0.45% it would never fire. Hand-tuned thresholds are brittle.
- **Burn-rate alerting** produced **2 alerts and 0 false alerts.** It paged immediately for the real outage A; it caught the slow leak B at 698 minutes (11.6 hours, 8% of the budget) via the slow-burn tier: not fast, but with **no human woken for blips**, and it would have fired earlier at a higher leak rate. It deliberately **ignored incident C** (2% errors for 30 minutes = 20× burn but only 0.6 error-minutes, **1.4% of the monthly budget**): not worth a page; it will show on dashboards and in the weekly SLO review. If you *do* want to be paged for C-sized events, lower the thresholds or add a mid tier (e.g., 10× over 30 min), accepting more pages.
- The design lesson: **tie paging to budget consumption**, and tune tiers to the pace at which you want to hear about problems.

### 21.7.3 Other alert kinds
- **Saturation/ capacity forecasts** (disk fills in 4 hours at current rate; `predict_linear`): tickets or pages by time-to-failure.
- **Dependency alerts** from the caller's view (error rate to payments).
- **Data pipeline freshness** (last successful run older than expected).
- **Security/audit alerts** (impossible travel, privilege escalation; Ch 22).
- **Anomaly detection** (statistical/ML): good for finding unknowns, noisy for paging; use for ticketing and exploration unless validated.
- **Composite alerts** to reduce duplicates (alert on the root cause, suppress symptoms with inhibition rules).

---

## 21.8 Dashboards and the investigation workflow `[MK]`

### 21.8.1 Dashboard design
- **Top level: service/journey health** by SLO: SLI vs target, error-budget remaining, burn rate; for each service RED panels with deploy markers; latency heatmaps (distribution over time) instead of lines of averages.
- **Dependencies:** outgoing call rate/errors/latency per dependency; downstream saturation; queues (depth, **age of oldest message**); caches (hit ratio, evictions); databases (connections, slow queries, replication lag, locks).
- **Infrastructure (USE):** nodes, containers, network, disks.
- **Business:** orders, payments, signups per minute by segment.
- Avoid vanity dashboards (walls of unrelated graphs), per-host graphs for fleets of hundreds, and dashboards nobody uses. Fewer, standardized, drill-down dashboards linked from alerts.

### 21.8.2 A systematic investigation (top-down)
1. **Scope the impact:** which SLI/journey is hurt, since when, for whom (region, tenant, version, device)? Segment using metrics labels or wide events.
2. **What changed?** Compare with deploys, flags, config, scaling, traffic shifts, dependencies' incidents (change annotations).
3. **Mitigate early** if there's a safe lever (rollback, failover, disable feature, shed load) (Ch 18).
4. **Follow the request:** use a failing trace (via an exemplar or sampled error trace) to find the slow/failing span and dependency.
5. **Check saturation and errors of that component** (USE): CPU, memory, GC, pools, queues, locks, disk.
6. **Drill into logs** of that span/trace ID for the specific error; **profile** if CPU/memory is implicated.
7. **Form a hypothesis, test it,** record findings in the incident timeline; confirm the fix with the same signals that showed the problem.
Hypothesis-driven debugging beats scrolling dashboards. The ShopLine incident would have taken minutes: p99 by `payment_provider` × `region` → one segment at 14 s → trace → span to the provider → rollout of a timeout/fallback.

---

## 21.9 Architecture and economics of an observability pipeline `[GK]`

```text
 services ──(OTLP / scrape / stdout)──▶ agents/sidecars ──▶ OTel Collector tier (batch, filter, redact, sample, route, tail-sample by trace ID)
                                                                 │               │                │
                                                              metrics          traces           logs
                                                    TSDB (Prometheus/Mimir/VM)  Tempo/Jaeger   Loki/ES/ClickHouse
                                                                 └──── query + dashboards (Grafana) + alerting (Alertmanager/PagerDuty) ────┘
                                                                                  cold storage (object store), exemplars/trace-ID links
```
**Design considerations**
- **Reliability of the pipeline:** buffering and backpressure (disk queues; Kafka between collection and storage), drop policy under overload (drop low-value data first, never block the app), horizontal scaling of collectors, multi-AZ storage.
- **Observe the observer:** monitoring must not share fate with what it monitors. Run **out-of-band** alerting (a second path: external synthetic checks and a dead-man's switch; paging provider independent of your cloud), and keep a minimal, separate view for when the main stack is down. In the 2021 Facebook outage, internal tools that depended on the same infrastructure were unavailable to engineers `[Public]`.
- **Multi-tenancy and quotas:** per-team cardinality and ingestion limits so one team can't take the system down.
- **Data governance:** PII redaction at the collector, access control by tenant, retention by data class, audit.
- **Consistent resource attributes** (service.name, version, env, region, team) so signals join.
- **Cost control levers:** cardinality budgets, sampling (tail-based), log level policy, index only needed fields, downsampling metrics (raw 15 s for 14 days, 5-min for 90 days, 1-h for 2 years), tiered storage, compression, dropping unused metrics/dashboards (usage analysis), moving from per-host to per-service aggregation.
- **Build vs buy:** OSS stack (Prometheus + Grafana + Loki + Tempo/Jaeger + OTel) = low license cost, high operations cost; SaaS (Datadog, Honeycomb, New Relic, Grafana Cloud, Splunk) = fast start, can become the second-largest infrastructure bill if undisciplined. Typical observability spend is **10–30% of infrastructure spend** without controls.

### 21.9.1 Back-of-envelope sizing
- **Metrics:** 1,000 services × 10 pods × 800 series/pod = 8 M active series → ~24 GB RAM at 3 KB/series (plus replication ×2, HA pairs); samples/s = 8 M / 15 s = 533 k samples/s; storage ≈ 8 M × 5,760 × 1.4 B ≈ **64 GB/day raw**.
- **Logs:** 2.6 TB/day (example above); 30-day hot ≈ 78 TB before replicas/indexes.
- **Traces:** 20,000 req/s × 30 spans × 500 B = 300 MB/s if everything were kept (26 TB/day); tail sampling at 2.5% ≈ **650 GB/day**.
These numbers explain why sampling, aggregation, and cardinality control are architecture, not afterthoughts.

---

## 21.10 Observing specific components `[MK]`

| Component | Key signals |
|---|---|
| **Web/API services** | RED per route; saturation (thread/connection pools, event-loop lag, GC pauses); dependency latency/errors; per-version comparisons |
| **Load balancers/gateways** | Requests, 4xx/5xx by backend, **LB-generated vs backend-generated errors**, active connections, TLS handshakes, healthy backend count, retries/ejections |
| **Databases** | QPS, latency (p99), slow-query log, connections, lock waits, replication lag (time and bytes), cache hit ratio, disk IOPS/latency, checkpoint/WAL, vacuum |
| **Caches** | Hit ratio (overall and per key class), evictions, memory, hot keys, latency, connection errors |
| **Queues/streams** | Depth, **age of oldest message**, enqueue/dequeue rate, consumer lag, redeliveries, DLQ size; Kafka: under-replicated partitions, ISR churn, request latency |
| **Batch/data pipelines** | Job success, duration, **freshness** (data age at output), row-count/quality checks, dead-man's switch |
| **Kubernetes** | Pod restarts/OOMKills, pending pods, CPU throttling, node pressure, HPA decisions, image pull errors, API server latency |
| **CDN/edge** | Hit ratio, origin request rate, error rate by POP, TTFB by region |
| **Client apps** | RUM: load times, crashes, API failures by device/network |
| **Business** | Orders, conversions, payment success by provider and region |

---

## 21.11 Failure modes and anti-patterns

| Anti-pattern | Symptom | Fix |
|---|---|---|
| **Average-only latency** | Dashboards green while users suffer | Percentiles/histograms by segment |
| **High-cardinality labels** | TSDB OOM, huge bills | Bounded labels; IDs in logs/traces; exemplars |
| **Averaging percentiles** | Wrong fleet p99 (+12% in lab) | Merge histograms/sketches |
| **Unstructured logs, no correlation IDs** | grep archaeology | JSON logs with trace/request IDs |
| **Logging everything at DEBUG** | Cost explosion, noise | Level policy, sampling, budgets |
| **No traces / head-only sampling** | Cannot find failing requests (1.1% captured) | Tail-based sampling, error/latency always-on |
| **Broken trace context** | Orphan spans, partial traces | Propagate across async/thread boundaries; test it |
| **Alert on causes** | 31 buzzes a week, ignored | SLO burn-rate paging |
| **Static brittle thresholds** | Miss at 0.45%, flap at 0.5% | Budget-based tiers |
| **Missing "what changed"** | Slow root-cause | Change events on timeline |
| **Dashboards no one owns** | Stale, misleading | Ownership, review, standard templates |
| **Observability depends on the system it observes** | Blind during outage | Out-of-band monitoring, dead-man's switch |
| **Scrape interval too long** | Spikes invisible | Histograms/counters; shorter intervals for critical metrics |
| **Sensitive data in telemetry** | Compliance incident | Redaction at source/collector, access control |
| **No retention policy** | Unbounded cost | Tiered retention, downsampling |
| **Vendor lock-in** | Cannot move | OpenTelemetry, open formats |
| **Alert storm during outage** | Pager meltdown | Inhibition/grouping, page on root cause |
| **Ignoring saturation** | Surprise outages | USE metrics, capacity forecasts |

---

## 21.12 Labs

### Lab 1: Cardinality, percentile aggregation, trace sampling, and burn-rate alerting (Java 17+)
```java
import java.util.*;

@SuppressWarnings("unchecked")
public class ObsLab {
    static final Random R = new Random(17);
    static long mix(long z) { z += 0x9E3779B97F4A7C15L; z = (z ^ (z >>> 30)) * 0xBF58476D1CE4E5B9L; z = (z ^ (z >>> 27)) * 0x94D049BB133111EBL; return z ^ (z >>> 31); }

    // ====================================================================== 1) metric cardinality
    static void cardinality() {
        int requests = 2_000_000; int services = 40, endpointsPer = 25, methods = 4, regions = 4, pods = 30, codes = 12; int users = 200_000;
        String[] designs = {"A: service, endpoint, status_class", "B: A + method + region", "C: B + pod", "D: C + exact status code", "E: C + user_id (200k users)", "F: C + request_id (unique)"};
        Set<Long>[] seen = new Set[designs.length]; for (int i = 0; i < seen.length; i++) seen[i] = new HashSet<>();
        for (int i = 0; i < requests; i++) {
            int svc = R.nextInt(services), ep = svc * endpointsPer + (int) (Math.pow(R.nextDouble(), 2) * endpointsPer), m = R.nextInt(methods), rg = R.nextInt(regions), pod = R.nextInt(pods);
            int code = R.nextDouble() < 0.9 ? 0 : 1 + R.nextInt(codes - 1); int cls = code == 0 ? 2 : 3 + (code % 3);
            long user = R.nextInt(users); long rid = i;
            long a = mix(svc * 1000003L + ep * 31L + cls);
            long b = mix(a * 31 + m * 7 + rg), c = mix(b * 31 + pod), d = mix(c * 31 + code), e = mix(c * 31 + user + 99), f = mix(c * 31 + rid + 77);
            seen[0].add(a); seen[1].add(b); seen[2].add(c); seen[3].add(d); seen[4].add(e); seen[5].add(f);
        }
        System.out.println("== 1) Metric cardinality. 2,000,000 requests across 40 services x 25 endpoints x 4 methods x 4 regions x 30 pods. How many DISTINCT time series does each label design create? ==");
        System.out.println("   (Assume a time-series database keeps ~3 KB of memory per active series and each series is scraped every 15 s, ~1.4 bytes/sample compressed.)");
        System.out.println("label design                                  active series      TSDB memory      samples/day       storage/day (compressed)");
        for (int i = 0; i < designs.length; i++) {
            long s = seen[i].size(); double memGB = s * 3.0 / 1e6; double samples = s * 5760.0; double gbDay = samples * 1.4 / 1e9;
            System.out.printf("%-44s %,12d    %9.2f GB     %,14.0f      %9.2f GB%n", designs[i], s, memGB, samples, gbDay);
        }
    }

    // ====================================================================== 2) aggregating latency across servers
    static double[] gen(int n, double median, double sigma) { double[] x = new double[n]; for (int i = 0; i < n; i++) x[i] = Math.exp(Math.log(median) + sigma * R.nextGaussian()); return x; }
    static double q(double[] sorted, double p) { return sorted[(int) Math.min(sorted.length - 1, Math.floor(p * sorted.length))]; }
    static double histQuantile(double[] bounds, long[] counts, double p) {                       // Prometheus-style histogram_quantile with linear interpolation inside the bucket
        long total = 0; for (long c : counts) total += c; double rank = p * total; long cum = 0;
        for (int i = 0; i < counts.length; i++) { if (cum + counts[i] >= rank) { double lo = i == 0 ? 0 : bounds[i - 1], hi = i < bounds.length ? bounds[i] : bounds[bounds.length - 1] * 2; return lo + (hi - lo) * (rank - cum) / Math.max(1, counts[i]); } cum += counts[i]; }
        return bounds[bounds.length - 1];
    }
    static void aggregation() {
        int servers = 20; int[] per = new int[servers]; int totalN = 0; for (int s = 0; s < servers; s++) { per[s] = s == 3 ? 10_000 : 50_000 + R.nextInt(150_000); totalN += per[s]; }     // unequal traffic: the heavy-tail server 3 gets only 10,000 requests
        List<double[]> data = new ArrayList<>(); double[] all = new double[totalN]; int k = 0;
        for (int s = 0; s < servers; s++) { double med = 8 + R.nextDouble() * 6; double sigma = s == 3 ? 1.1 : 0.55; double[] d = gen(per[s], med, sigma); data.add(d); for (double v : d) all[k++] = v; }   // server 3 has a heavy tail
        Arrays.sort(all);
        double[] p99s = new double[servers]; double sumP99 = 0, maxP99 = 0; for (int s = 0; s < servers; s++) { double[] c = data.get(s).clone(); Arrays.sort(c); p99s[s] = q(c, 0.99); sumP99 += p99s[s]; maxP99 = Math.max(maxP99, p99s[s]); }
        double[] coarse = {5, 10, 25, 50, 100, 250, 500, 1000};                                  // typical default buckets (ms)
        double[] fine = new double[200]; fine[0] = 1; for (int i = 1; i < fine.length; i++) fine[i] = fine[i - 1] * 1.06;      // exponential buckets, 6% growth
        long[] cc = new long[coarse.length + 1], fc = new long[fine.length + 1];
        for (double v : all) { int i = 0; while (i < coarse.length && v > coarse[i]) i++; cc[i]++; int j = Arrays.binarySearch(fine, v); if (j < 0) j = -j - 1; fc[j]++; }
        double alpha = 0.01, gamma = (1 + alpha) / (1 - alpha); Map<Integer, Long> dd = new TreeMap<>(); for (double v : all) dd.merge((int) Math.ceil(Math.log(v) / Math.log(gamma)), 1L, Long::sum);
        System.out.println("\n== 2) Aggregating latency across 20 servers with UNEQUAL traffic (10,000 to 200,000 requests each; server #3 has a heavy tail but only 10,000 requests). TRUE values come from all raw samples. ==");
        System.out.println("method                                                   p50 (ms)    p95 (ms)    p99 (ms)    p99.9 (ms)");
        System.out.printf("%-55s %8.2f    %8.2f    %8.2f    %9.2f%n", "TRUE (all raw samples)", q(all, .5), q(all, .95), q(all, .99), q(all, .999));
        System.out.printf("%-55s %8s    %8s    %8.2f    %9s%n", "average of the 20 per-server p99s", "-", "-", sumP99 / servers, "-");
        System.out.printf("%-55s %8s    %8s    %8.2f    %9s%n", "maximum of the 20 per-server p99s", "-", "-", maxP99, "-");
        System.out.printf("%-55s %8.2f    %8.2f    %8.2f    %9.2f%n", "merged histogram, 8 coarse buckets (5..1000 ms)", histQuantile(coarse, cc, .5), histQuantile(coarse, cc, .95), histQuantile(coarse, cc, .99), histQuantile(coarse, cc, .999));
        System.out.printf("%-55s %8.2f    %8.2f    %8.2f    %9.2f%n", "merged histogram, 200 exponential buckets (6% steps)", histQuantile(fine, fc, .5), histQuantile(fine, fc, .95), histQuantile(fine, fc, .99), histQuantile(fine, fc, .999));
        double[] qs = {.5, .95, .99, .999}; double[] est = new double[4]; long total = all.length;
        for (int t = 0; t < 4; t++) { long cum = 0; for (var en : dd.entrySet()) { cum += en.getValue(); if (cum >= qs[t] * total) { est[t] = 2 * Math.pow(gamma, en.getKey()) / (gamma + 1); break; } } }
        System.out.printf("%-55s %8.2f    %8.2f    %8.2f    %9.2f   (%d buckets, guaranteed <=1%% relative error)%n", "mergeable log-bucket sketch (DDSketch style)", est[0], est[1], est[2], est[3], dd.size());
    }

    // ====================================================================== 3) trace sampling
    static void tracing() {
        int requests = 1_000_000; double pErr = 0.005, pSlow = 0.01, headRate = 0.01;
        long interesting = 0, headKeptInteresting = 0, headKept = 0, tailKept = 0, tailInteresting = 0;
        for (int i = 0; i < requests; i++) {
            boolean err = R.nextDouble() < pErr, slow = R.nextDouble() < pSlow, intr = err || slow; if (intr) interesting++;
            if (R.nextDouble() < headRate) { headKept++; if (intr) headKeptInteresting++; }                          // head-based: decide at the start, blind to the outcome
            if (intr || R.nextDouble() < headRate) { tailKept++; if (intr) tailInteresting++; }                        // tail-based: decide after the trace completes
        }
        System.out.println("\n== 3) Trace sampling. 1,000,000 requests; 0.5% fail, 1% are slow (together 'interesting'). Sampling budget: ~1% of normal traffic. ==");
        System.out.println("strategy                                        traces stored   fraction stored   interesting traces captured");
        System.out.printf("%-47s %10d         %5.2f%%          %6d of %d  (%.1f%%)%n", "head-based, random 1%", headKept, 100.0 * headKept / requests, headKeptInteresting, interesting, 100.0 * headKeptInteresting / interesting);
        System.out.printf("%-47s %10d         %5.2f%%          %6d of %d  (%.1f%%)%n", "tail-based: keep all errors/slow + 1% of the rest", tailKept, 100.0 * tailKept / requests, tailInteresting, interesting, 100.0 * tailInteresting / interesting);
        double qps = 20_000, durSec = 0.8, spans = 30, bytes = 500;
        System.out.printf("cost of tail-based sampling: buffer every in-flight trace until it completes = %.0f req/s x %.1f s x %.0f spans x %.0f B = %.0f MB of collector memory (before replication/headroom)%n", qps, durSec, spans, bytes, qps * durSec * spans * bytes / 1e6);
    }

    // ====================================================================== 4) alerting strategies on 30 days of per-minute error rates
    static void alerting() {
        int T = 43_200; double slo = 0.999, budget = 1 - slo; double[] e = new double[T];
        for (int t = 0; t < T; t++) e[t] = Math.max(0, 0.0002 + 0.0001 * R.nextGaussian());                          // baseline 0.02% errors
        for (int k = 0; k < 25; k++) { int s = R.nextInt(T - 3); int len = 1 + R.nextInt(2); double lvl = 0.02 + 0.02 * R.nextDouble(); for (int j = 0; j < len; j++) e[s + j] = lvl; }   // 25 transient blips: 1-2 minutes at 2-4% errors
        int[][] inc = {{10 * 1440, 10, 100}, {15 * 1440, 3 * 1440, 0}, {25 * 1440, 30, 0}}; double[] lvl = {1.0, 0.005, 0.02}; String[] names = {"A: hard outage, 100% errors for 10 min", "B: slow leak, 0.5% errors for 3 days (5x burn)", "C: partial outage, 2% errors for 30 min"};
        for (int i = 0; i < 3; i++) for (int j = 0; j < inc[i][1]; j++) e[inc[i][0] + j] = Math.max(e[inc[i][0] + j], lvl[i]);
        double[] cum = new double[T + 1]; for (int t = 0; t < T; t++) cum[t + 1] = cum[t] + e[t];
        java.util.function.BiFunction<Integer, Integer, Double> avg = (t, w) -> { int a = Math.max(0, t - w + 1); return (cum[t + 1] - cum[a]) / (t + 1 - a); };
        String[] det = {"N1: page if error rate > 1% for 1 minute", "N2: page if 5-minute average error rate > 0.5%", "BR: multi-window burn-rate (14.4x 1h&5m, 6x 6h&30m page; 1x 3d&6h ticket)"};
        System.out.println("\n== 4) Alerting strategies on 30 days of per-minute error rates (SLO 99.9% => budget = 43.2 error-minutes). 3 real incidents + 25 one/two-minute blips. ==");
        System.out.println("detector                                                         alert groups   FALSE alerts   detected A / B / C (minutes after start; budget burned before alert)");
        for (int d = 0; d < 3; d++) {
            boolean[] fire = new boolean[T];
            for (int t = 0; t < T; t++) {
                if (d == 0) fire[t] = e[t] > 0.01;
                else if (d == 1) fire[t] = avg.apply(t, 5) > 0.005;
                else fire[t] = (avg.apply(t, 60) > 14.4 * budget && avg.apply(t, 5) > 14.4 * budget) || (avg.apply(t, 360) > 6 * budget && avg.apply(t, 30) > 6 * budget) || (avg.apply(t, 4320) > budget && avg.apply(t, 360) > budget);
            }
            List<int[]> groups = new ArrayList<>(); int start = -1, last = -100;
            for (int t = 0; t < T; t++) if (fire[t]) { if (start < 0 || t - last > 30) { groups.add(new int[]{t, t}); } groups.get(groups.size() - 1)[1] = t; last = t; start = t; }
            int falseAlerts = 0; String[] res = new String[3]; Arrays.fill(res, "MISSED");
            for (int[] g : groups) { boolean real = false; for (int i = 0; i < 3; i++) { int s = inc[i][0], en = inc[i][0] + inc[i][1] + 120; if (g[0] >= s && g[0] <= en) { real = true; if (res[i].equals("MISSED")) { double burned = cum[g[0] + 1] - cum[s]; res[i] = String.format("%d min (%.0f%% of budget)", g[0] - s, 100 * burned / (budget * T)); } } } if (!real) falseAlerts++; }
            System.out.printf("%-64s %6d        %6d        A: %s | B: %s | C: %s%n", det[d], groups.size(), falseAlerts, res[0], res[1], res[2]);
        }
        System.out.println("incidents: " + names[0] + "; " + names[1] + "; " + names[2]);
    }

    public static void main(String[] a) { cardinality(); aggregation(); tracing(); alerting(); }
}
```
**Sample output**
```text
== 1) Metric cardinality. 2,000,000 requests across 40 services x 25 endpoints x 4 methods x 4 regions x 30 pods. How many DISTINCT time series does each label design create? ==
   (Assume a time-series database keeps ~3 KB of memory per active series and each series is scraped every 15 s, ~1.4 bytes/sample compressed.)
label design                                  active series      TSDB memory      samples/day       storage/day (compressed)
A: service, endpoint, status_class                  4,000         0.01 GB         23,040,000           0.03 GB
B: A + method + region                             61,157         0.18 GB        352,264,320           0.49 GB
C: B + pod                                        624,206         1.87 GB      3,595,426,560           5.03 GB
D: C + exact status code                          639,798         1.92 GB      3,685,236,480           5.16 GB
E: C + user_id (200k users)                     1,999,971         6.00 GB     11,519,832,960          16.13 GB
F: C + request_id (unique)                      2,000,000         6.00 GB     11,520,000,000          16.13 GB

== 2) Aggregating latency across 20 servers with UNEQUAL traffic (10,000 to 200,000 requests each; server #3 has a heavy tail but only 10,000 requests). TRUE values come from all raw samples. ==
method                                                   p50 (ms)    p95 (ms)    p99 (ms)    p99.9 (ms)
TRUE (all raw samples)                                     11.23       28.80       42.71        68.73
average of the 20 per-server p99s                              -           -       47.82            -
maximum of the 20 per-server p99s                              -           -      167.47            -
merged histogram, 8 coarse buckets (5..1000 ms)            12.42       35.20       48.30        91.21
merged histogram, 200 exponential buckets (6% steps)       11.24       28.85       42.80        68.82
mergeable log-bucket sketch (DDSketch style)               11.13       29.08       42.52        68.72   (346 buckets, guaranteed <=1% relative error)

== 3) Trace sampling. 1,000,000 requests; 0.5% fail, 1% are slow (together 'interesting'). Sampling budget: ~1% of normal traffic. ==
strategy                                        traces stored   fraction stored   interesting traces captured
head-based, random 1%                                 9897          0.99%             170 of 14848  (1.1%)
tail-based: keep all errors/slow + 1% of the rest      24604          2.46%           14848 of 14848  (100.0%)
cost of tail-based sampling: buffer every in-flight trace until it completes = 20000 req/s x 0.8 s x 30 spans x 500 B = 240 MB of collector memory (before replication/headroom)

== 4) Alerting strategies on 30 days of per-minute error rates (SLO 99.9% => budget = 43.2 error-minutes). 3 real incidents + 25 one/two-minute blips. ==
detector                                                         alert groups   FALSE alerts   detected A / B / C (minutes after start; budget burned before alert)
N1: page if error rate > 1% for 1 minute                             27            23        A: 0 min (2% of budget) | B: 2113 min (25% of budget) | C: 0 min (0% of budget)
N2: page if 5-minute average error rate > 0.5%                       27            22        A: 0 min (2% of budget) | B: 4 min (0% of budget) | C: 1 min (0% of budget)
BR: multi-window burn-rate (14.4x 1h&5m, 6x 6h&30m page; 1x 3d&6h ticket)      2             0        A: 0 min (2% of budget) | B: 698 min (8% of budget) | C: MISSED
incidents: A: hard outage, 100% errors for 10 min; B: slow leak, 0.5% errors for 3 days (5x burn); C: partial outage, 2% errors for 30 min
```
**Experiments**
1. In part 1, count the series with `status_code` replaced by a bucketed `status_class`, then add `route_template` vs raw `url` (simulate URLs with embedded IDs). Compute the cost in memory and storage per day for each choice and set a cardinality budget policy.
2. In part 2, vary histogram bucket layouts (linear 10 ms, powers of two, SLO-aligned buckets) and compute the p99 error for each; find the cheapest layout with ≤2% error on p99.
3. In part 2, simulate **three regions** with different load and show that the per-region p99 average is wrong for the global p99 but correct per-region histograms merge exactly.
4. In part 3, add **adaptive sampling**: 100% of errors, 100% of requests slower than 2× the p99, and a rate for the rest that targets 1,000 traces per minute regardless of traffic. How does coverage change when traffic doubles?
5. In part 3, simulate a **trace split across collectors** (spans of the same trace routed to different collectors) and show how tail sampling decisions become inconsistent; then implement consistent routing by trace ID hash.
6. In part 4, add a **fourth incident**: a 6-hour period at 1.5% errors (15× burn). Which detectors catch it and when? Tune the burn-rate thresholds to page for incident C (2% for 30 min) and count the extra false pages.
7. In part 4, add **seasonality** (a daily pattern in baseline errors) and noise level changes; compare the stability of static thresholds vs burn-rate alerts.

### Lab 2: Instrument a service with OpenTelemetry (needs Docker; not executed in the book's sandbox)
Run `otel-collector`, `jaeger` (or Tempo), and `prometheus` + `grafana` with docker compose; add the OpenTelemetry Java agent (`-javaagent:opentelemetry-javaagent.jar`) to the Chapter 2 `TaskApi`; send requests through the Chapter 7 `MiniLb` to two backends. **Tasks:** (1) find a trace in Jaeger showing LB → backend; (2) add a custom span around the idempotency lookup; (3) expose a request-duration histogram with SLO-aligned buckets and graph p99 in Grafana; (4) add a JSON logger that includes `trace_id` and click from a log line to the trace; (5) inject a 2-second delay into one backend (`tc netem` or a sleep endpoint) and find it using only traces and the latency heatmap; (6) configure a burn-rate alert rule for a 99.9% availability SLO.

### Lab 3: Observability plan for a design (paper)
For the Chapter 12 order-processing pipeline (API → queue → workers → payment provider → email), list: the SLIs/SLOs per user journey; the metrics (with labels and cardinality estimates); the structured log schema; the trace propagation plan through the queue; sampling rules; the alerts (with burn-rate tiers) and runbook links; the dashboards; and a monthly telemetry volume/cost estimate. State which questions you want to be able to answer at 3 a.m. and which signal answers each.

---

## 21.13 Interview perspective

- Say **metrics for detection, traces for localization, logs for explanation, profiles for code-level cause**, and that you join them with trace IDs and exemplars.
- Name **RED/USE/golden signals** and give concrete metrics per component (queue age, replication lag, cache hit ratio).
- Warn about **cardinality** and **averaged percentiles**; propose histograms/sketches.
- Describe **SLO burn-rate alerting** and why you wouldn't page on CPU.
- Describe **tail-based sampling** and its buffering/routing needs; mention OpenTelemetry.
- Add **change annotations**, **synthetic checks**, **RUM**, and **out-of-band monitoring**.
- Quantify telemetry volume and cost (series × samples; GB/day logs; traces sampled) and the levers to control it.

---

## 21.14 Common misconceptions

1. **“We have dashboards, so we have observability.”** Dashboards answer predefined questions.
2. **“Average latency is fine.”** It hides the tail and segments (Story; Ch 5).
3. **“Add labels for everything.”** Cardinality explodes (user_id → 2 M series in the lab).
4. **“Average the p99s from each server.”** Statistically invalid (+12% error in the lab, 4× for the max).
5. **“Trace 1% of requests.”** Random 1% captured 1.1% of the interesting traces.
6. **“More logs means more insight.”** More cost; structure and context matter more.
7. **“Alert on everything that could go wrong.”** Page on user-visible SLO burn.
8. **“Thresholds are simple.”** They are brittle (fire on blips, miss slow leaks).
9. **“Monitoring is an ops concern.”** Instrumentation is a development responsibility.
10. **“Observability tools are free once installed.”** Telemetry can be 10–30% of infra spend.
11. **“If the monitoring stack is down we'll know.”** Only with an independent dead-man's switch.
12. **“Traces replace metrics.”** Different jobs; sampling makes traces unsuitable for rates.

---

## 21.15 Chapter summary

- **Observability** = the ability to ask new questions of a running system from the telemetry it emits; **metrics** detect, **traces** localize, **logs** explain, **profiles** point to code, **change events** answer "what changed?", **RUM/synthetics** show the user's view.
- **Metrics:** counters/gauges/histograms; **RED, USE, golden signals**; pull vs push; ~1.4 bytes/sample, ~3 KB per active series. **Cardinality is the cost driver:** adding `pod` multiplied series 10× and one unbounded label (user_id) produced a series per request (2 M series, 6 GB RAM, 16 GB/day).
- **Latency aggregation:** averaging per-server p99s was 12% too high (max: 4× off); merged histograms are right if buckets are fine (8 coarse buckets: 33% error at p99.9; 6% exponential buckets: 0.2%); use sketches/native histograms and SLO-aligned buckets.
- **Logs:** structured JSON with trace/request IDs, wide events, tiered pipeline; cost ∝ GB/day × retention × indexing (2.6 TB/day example); never log secrets/PII.
- **Traces:** spans, W3C context propagation, OpenTelemetry; head sampling at 1% captured **1.1%** of failures; **tail sampling captured 100%** for 2.5% of traces at ~240 MB of buffer.
- **Alerting:** page on SLO burn. In 30 days naive thresholds paged **23 and 22 false times**, burn-rate alerting **0 false pages** (and chose not to page for a 1.4%-of-budget blip); slow leaks need tiers (the 5× leak was caught after ~11.6 h at 8% of budget).
- **Workflow:** scope → what changed → mitigate → follow a trace → check saturation → logs/profile → hypothesis; dashboards organized by SLO, dependency, infrastructure.
- **Pipeline:** collectors, buffering/backpressure, quotas, redaction, tiered retention, out-of-band monitoring; budgets and sampling are architectural decisions.

### Mental models
| Concept | Mental model |
|---|---|
| Metrics | The dashboard gauges of a car: tell you something is off |
| Traces | A parcel-tracking page: every hop and how long it took |
| Logs | The black-box recorder: details of what happened |
| Profiles | An X-ray of where the engine burns fuel |
| Cardinality | The number of separate pages in a ledger: each new column multiplies the pages |
| Percentile aggregation | You cannot get a city's median income by averaging each street's median |
| Tail sampling | Keeping every accident report plus a sample of ordinary trips |
| Burn-rate alert | A fuel-gauge alarm tied to how fast you will run out, not a flicker |
| Dead-man's switch | A guard who must check in every hour; silence itself raises the alarm |

---

## 21.16 Exercises

### A. Conceptual
1. Contrast monitoring and observability with an example of a known-unknown and an unknown-unknown.
2. For each of metrics, logs, traces, profiles: what question does it answer best and what is its main limitation?
3. Define RED, USE, and the four golden signals. Apply RED and USE to a database connection pool.
4. Explain counters, gauges, histograms, summaries. Why can't summaries be aggregated across instances?
5. What is cardinality? Why do high-cardinality labels break metric systems? Where should high-cardinality data go instead?
6. Why are averaged percentiles wrong? How do histograms and sketches fix it? What determines histogram accuracy?
7. What is an exemplar and how does it connect metrics to traces?
8. List the properties of good structured logs and what must never be logged.
9. What is a wide event/canonical log line and what does it enable?
10. Describe trace, span, parent/child, and W3C `traceparent`. How is context propagated through a message queue?
11. Compare head-based and tail-based sampling, including the infrastructure tail sampling requires.
12. What is OpenTelemetry and what are the roles of the SDK, auto-instrumentation, and the Collector?
13. Why page on SLO burn instead of CPU? Describe multi-window multi-burn-rate alerting.
14. Why are static thresholds brittle? Use the lab's N1/N2 results.
15. Why might a burn-rate alert deliberately not page for a 30-minute 2% error episode?
16. What is a dead-man's switch and why do you need one?
17. Describe a top-down investigation workflow and why "what changed?" comes early.
18. What are RUM and synthetic monitoring and when do you need each?
19. How do you keep the observability stack itself from failing during an outage?
20. List five levers for controlling telemetry cost.

### B. Calculations
21. A metric has labels: service (60), route (30), method (4), status_class (5), region (6), pod (25). What is the theoretical series count? If only 35% of combinations occur, how many series, memory (3 KB each), and storage per day (15 s scrape, 1.4 B/sample)?
22. Adding `customer_id` (50,000 values) to a metric with 20,000 series, assuming each customer hits 10% of the existing combinations: new series count? Is it acceptable?
23. Latency data: three regions with 1 M, 200 k, and 50 k requests and p99 of 40, 120, and 400 ms. Compute the traffic-weighted average of the three p99s and compare with the arithmetic average. Explain why neither is the true global p99 and give a bound for the true value.
24. Histogram buckets at 100, 250, 500, 1000 ms; counts: ≤100: 9,200; ≤250: 700; ≤500: 70; ≤1000: 25; >1000: 5 (total 10,000). Estimate p99 and p99.9 by linear interpolation within buckets. What is the possible error?
25. Log volume: 8,000 req/s, each request writes 6 lines of 300 bytes. GB/day? Cost at $0.30/GB ingested and $0.03/GB-month stored for 30 days (assuming 10:1 compression for storage). With a policy that drops DEBUG lines (60% of lines), what changes?
26. Trace sampling: 12,000 req/s, 25 spans/trace, 400 B/span. Data rate if 100% traced; storage/day at 1% head sampling; at tail sampling (all of the 2% interesting + 1% of the rest). Collector buffer needed for tail sampling with an average trace duration of 1.2 s.
27. SLO 99.95% over 30 days (budget in minutes?). An incident causes 3% errors for 40 minutes. Burn rate, budget consumed, and which of these alerts fire: 14.4× over 1 h&5 m; 6× over 6 h&30 m; 3× over 12 h&1 h?
28. Prometheus scrapes every 30 s. A CPU spike lasts 8 seconds at 100% on a 2-core node. What is the probability the spike is captured by at least one scrape? What would you measure instead?
29. A counter reads 1,000 at t0, then (after a restart) 120 at t0+60 s. What is the correct per-second rate using counter-reset handling (assume the true count before reset was 1,000 + x with x unknown, discuss bounds)?
30. 5 M active series; HA pair of Prometheus servers; each needs 3.5 KB/series. RAM per server? What happens with a 4× cardinality increase?

### C. Design
31. Design the observability for a checkout service: metrics (names, labels), logs (schema), traces (spans, attributes), alerts (SLO tiers), dashboards, sampling.
32. Design a cardinality governance policy: budgets, enforcement at the collector, review process, and a dashboard for top series growth.
33. Design a telemetry pipeline for 2,000 services with HA, multi-tenant quotas, and cost controls. Sketch components and failure behavior.
34. Design the alerting scheme for a Kafka consumer service (SLIs, thresholds, burn rates, runbooks).
35. Design an on-call dashboard for investigating a latency incident in a microservice call graph (what panels, what drill-downs, what links).
36. Propose an out-of-band monitoring approach that still works when your cloud region is impaired.

### D. Debugging
37. Prometheus memory doubled overnight. How do you find the offending metric/label and prevent recurrence?
38. Traces show a 3-second gap between two spans with no child spans. List hypotheses.
39. After enabling tracing, p99 latency rose 8%. Possible causes and mitigations.
40. An alert fires every night at 02:00 and resolves itself at 02:20. How do you investigate and decide whether to fix the alert or the system?
41. A dashboard shows p99 dropping to zero for ten minutes during an incident. Possible explanations?
42. Logs from a service are missing for 5 minutes around an outage. Where in the pipeline could they be lost, and how do you make the pipeline more robust?

### E. Interview-style
43. “How would you monitor a microservices system?”
44. “Your p99 doubled; walk me through finding the cause.”
45. “How do you avoid alert fatigue?”

### F. Teach-back
46. Explain why you cannot average percentiles to a non-technical manager using exam scores from schools of different sizes.

---

## 21.17 Full answer key

**1.** Monitoring checks known conditions (known-unknown: “is error rate > 1%?”); observability lets you explore new questions (unknown-unknown: “why are only European mobile users on provider X slow?”) because telemetry is rich and sliceable.

**2.** Metrics: detection/trends; limited by aggregation and cardinality. Logs: detail about discrete events; voluminous and hard to correlate if unstructured. Traces: where time went across services; sampling and instrumentation gaps. Profiles: which code consumes resources; overhead and specialization.

**3.** RED: rate, errors, duration per service/route. USE: utilization, saturation, errors per resource. Golden signals: latency, traffic, errors, saturation. Connection pool: utilization = busy/max; saturation = threads waiting for a connection; errors = timeouts acquiring/connect failures; RED for queries issued.

**4.** Counter: monotonic count (use rate). Gauge: value that goes up/down. Histogram: bucket counts (aggregatable). Summary: client-side quantiles; cannot be aggregated because quantiles of different instances can’t be combined into the quantile of the union.

**5.** Cardinality = distinct label combinations = series count. Each series costs memory, storage, and query time; unbounded labels (user_id) grow without bound and crash the TSDB. Put such data in logs/traces/wide events and link via exemplars/trace IDs.

**6.** A percentile of a union isn’t a function of percentiles of the parts, and averaging ignores traffic weights (lab: +12% error; max 4×). Histograms sum bucket counts (exact merge); sketches merge too. Accuracy depends on bucket layout (coarse buckets: 33% error at p99.9).

**7.** An exemplar attaches a sample trace ID (and value) to a metric bucket/point so you can click from a slow latency bucket to an example trace.

**8.** One JSON event per line, consistent field names, trace/request IDs, service/version/env, outcome and duration, appropriate level; no secrets, tokens, full PANs, or raw PII.

**9.** A single rich event per request per service with many attributes; allows arbitrary slicing/grouping (“p99 by region and provider”) without pre-aggregation; needs a high-cardinality event store.

**10.** A trace is a tree of spans sharing a trace ID; each span has a parent span ID, name, timings, attributes. `traceparent: 00-<trace-id>-<parent-id>-<flags>` is the W3C header. For queues, put the context in message headers and create linked spans for consumers.

**11.** Head: decide at the start, cheap, blind to outcome (captured 1.1% of interesting traces in the lab). Tail: decide after completion; keeps all errors/slow; needs buffering (≈240 MB in the example) and routing all spans of a trace to the same collector.

**12.** OpenTelemetry is the vendor-neutral standard: SDKs/API for instrumentation, auto-instrumentation agents for libraries, OTLP protocol, and the Collector (receive, process, export), supporting metrics, logs, traces.

**13.** Users feel errors/latency, not CPU; paging on cause metrics produces noise. Burn-rate alerts compare error rate to the SLO budget over a long window (significance) and a short window (recency), with tiers (14.4× 1 h, 6× 6 h, 1× 3 d).

**14.** N1/N2 fired 22–23 false alerts per month on blips, and N2 caught the 0.52% leak only because the threshold was 0.5%; at 0.45% it would miss. Static thresholds ignore budget and are sensitive to noise level and incident size.

**15.** C burns 20× but consumes only 0.6 error-minutes (1.4% of the budget); not worth waking someone. It remains visible on dashboards and in SLO reviews.

**16.** An alert that should always fire; if it stops arriving, the alerting pipeline is broken. Prevents silent monitoring failure.

**17.** Scope impact, ask what changed, mitigate, follow a failing trace to the slow span, check saturation/errors there, read logs/profile, test hypotheses. “What changed?” is the highest-yield question because most incidents follow a change.

**18.** RUM: real-user client-side metrics (page loads, JS errors) showing device/network effects. Synthetic: scripted probes from outside to detect outages even with no traffic and validate DNS/CDN/TLS.

**19.** Out-of-band paths: external synthetic checks, independent paging provider, dead-man’s switch, a minimal separate dashboard/status page that does not depend on the main infrastructure.

**20.** Cardinality budgets, tail-based sampling, log level policy and index only needed fields, downsampling/retention tiers, dropping unused metrics/dashboards (usage analysis), aggregation at the collector.

**21.** Theoretical: 60×30×4×5×6×25 = 5,400,000. 35% → 1,890,000 series. Memory ≈ 1.89 M × 3 KB = **5.7 GB**. Storage/day: 1.89 M × 5,760 samples × 1.4 B = **15.2 GB/day**.

**22.** Each customer touches 10% of 20,000 = 2,000 combos; 50,000 customers × 2,000 = 100 M series (capped by traffic). That’s ~300 GB of memory: unacceptable; keep customer_id in logs/traces or use a bounded “tier” label.

**23.** Traffic-weighted average: (1,000,000 × 40 + 200,000 × 120 + 50,000 × 400) / 1,250,000 = (40 M + 24 M + 20 M) / 1.25 M = **67.2 ms**; the plain arithmetic average is (40 + 120 + 400) / 3 = **186.7 ms**. Neither is the true global p99. The true value cannot be computed from the three p99 numbers at all: it depends on the full distributions (how far above its p99 each region's tail extends). All you can say is that it lies between the smallest and largest regional p99 (40 ms to 400 ms), and that it is certainly above 40 ms (more than 1% of total requests exceed 40 ms because regions 2 and 3 have far more than 1% above it). To get the real number you must merge the regional histograms or sketches and compute the quantile of the merged distribution.

**24.** Cumulative: ≤100: 9,200; ≤250: 9,900; ≤500: 9,970; ≤1000: 9,995; total 10,000. p99 → rank 9,900 → exactly at the end of the 100–250 bucket → **250 ms**. p99.9 → rank 9,990: in the 500–1000 bucket (cum 9,970 → 9,995): fraction (9,990−9,970)/25 = 0.8 → 500 + 0.8 × 500 = **900 ms**. Error possible: up to the whole bucket width (the true p99.9 could be anywhere from 500 to 1,000 ms); finer buckets reduce it.

**25.** Lines/day = 8,000 × 6 × 86,400 = 4.147 B lines × 300 B = **1.244 TB/day**. Ingest cost = 1,244 GB × $0.30 = **$373/day**. Storage: 30 days × 1,244 GB / 10 = 3,732 GB stored × $0.03 = $112/month. Dropping 60% of lines: ingest falls to 498 GB/day → **$149/day** (saves $224/day ≈ $6.7k/month) and storage to 1,493 GB ($45/month).

**26.** 100%: 12,000 × 25 × 400 B = **120 MB/s = 10.4 TB/day**. Head 1%: **104 GB/day**. Tail: interesting 2% + 1% of 98% = 2.98% ≈ **309 GB/day**. Buffer: 120 MB/s × 1.2 s = **144 MB** (plus overhead/headroom).

**27.** Budget = 0.05% × 43,200 = **21.6 min**. Burn rate = 3%/0.05% = **60×**. Budget consumed = 40 min × 3% = 1.2 error-min = **5.6%**. 14.4× over 1 h & 5 m: 1-h average = (40/60) × 3% = 2% → burn 40× > 14.4 ✓ fires. 6× over 6 h & 30 m: 6-h average = 40/360 × 3% = 0.33% → burn 6.7× > 6 ✓ (and 30-min window is 100% of the incident at 3%: 60× ✓) fires. 3× over 12 h & 1 h: 12-h avg = 0.167% → burn 3.3× > 3 ✓ and 1-h avg 2% ✓ fires too.

**28.** A scrape samples an instant; the spike covers 8 s of each 30 s interval: probability a scrape lands inside = 8/30 = **26.7%** per spike (per node). Instead measure with a counter of CPU seconds (`rate` of cumulative usage captures the spike's contribution in the interval average) or a histogram/max-over-interval via an agent; use finer resolution for important metrics.

**29.** A decreasing counter indicates a reset; Prometheus-style `rate` treats the post-reset value as the increase since reset: increase ≈ 120 (assuming reset to zero at the restart), so rate ≈ 120/60 = **2/s** at least; the count between the last pre-restart scrape and the restart is unknown (lost), so the true rate is ≥ 2/s with an unknown additional contribution bounded by the pre-restart rate × time-to-restart.

**30.** 5 M × 3.5 KB = **17.5 GB** per server. A 4× increase (20 M series) needs **70 GB** per server: likely OOM; mitigations: drop labels, shard/federate, use a horizontally scalable backend (Mimir/VictoriaMetrics/Thanos), add cardinality limits.

**31 (sketch).** Metrics: `checkout_requests_total{route,method,status_class,region}`, `checkout_duration_seconds` histogram with SLO-aligned buckets (0.1,0.25,0.5,1,2.5), dependency histograms `{dependency,outcome}`, pool saturation gauges, business `orders_total{provider,region}`. Logs: wide JSON event per request. Traces: spans for gateway, inventory, payment with attributes (provider, region, attempt). Alerts: availability and latency burn-rate tiers; dependency alerts. Dashboards: SLO, dependencies, saturation. Sampling: tail-based (errors/slow 100%, 1% rest).

**32 (sketch).** Per-team series budget (e.g., 500 k); collector enforces label allowlists and drops/aggregates offending labels; CI check for new metrics (cardinality estimate); dashboard of top-N series growth; weekly review; alert at 80% of budget; exception process.

**33 (sketch).** Agents → regional OTel Collector tier (HPA, load-balanced by trace ID for tail sampling) → Kafka buffers → storage clusters (Mimir/Tempo/Loki or ClickHouse) with per-tenant limits; multi-AZ; hot/warm/cold tiering; separate control plane; dead-man's switch; self-metrics for the pipeline; graceful degradation: drop DEBUG/low-priority first.

**34 (sketch).** SLIs: end-to-end processing latency (age of oldest unprocessed message), success ratio of processing; alerts: burn-rate on freshness SLO, consumer lag growth, DLQ rate, redelivery rate; runbooks: scale consumers, check poison messages, check downstream.

**35 (sketch).** Panels: SLO/burn; service RED with deploy markers; latency heatmap; per-dependency latency/error; saturation (pools/GC/CPU); top slow endpoints; exemplar links to traces; recent errors log panel filtered by service/version; call-graph with latency edges; change timeline.

**36 (sketch).** External synthetic probes from other providers/regions; a separate lightweight monitoring stack in another region/cloud; independent paging (SMS/phone provider); status page hosted elsewhere; dead-man’s switch; credentials/runbooks stored out-of-band.

**37.** Query TSDB cardinality endpoints (`topk(10, count by (__name__)({__name__=~".+"}))`, TSDB status page) to find the metric and label with growth; check recent deploys; fix the label, drop series at ingestion (relabeling), add per-job series limits and alerts on series growth.

**38.** Uninstrumented work (blocking I/O, GC pause, lock wait, thread-pool queueing), async gap, retry/backoff sleep, clock skew between hosts, missing child spans due to dropped context.

**39.** Instrumentation overhead (span creation, exporter on request path), synchronous export, large attributes, too-verbose auto-instrumentation, GC from allocations. Mitigate: async batch export, sampling, filter noisy spans, limit attributes.

**40.** Correlate with cron/batch jobs, backups, cert rotations, traffic patterns, dependency maintenance; look at metrics around 02:00–02:20; if impact is real fix the system; if no user impact, fix the alert (inhibition, threshold, maintenance window) or convert to a ticket.

**41.** No data (scrape failure, collector outage) interpreted as zero, quantile calculation on empty histograms, load balancer sending no traffic, metric rename/relabel, aggregation across failing targets. Treat “no data” explicitly.

**42.** Agent crash or buffer overflow, backpressure drop under load, collector outage, Kafka/ES unavailability, network partition, clock issues in ingestion. Add disk-backed queues, retry with backoff, redundant collectors, monitoring of dropped-log counters, and a heartbeat log.

**43 (sketch).** Standard instrumentation (RED/USE), OTel traces with tail sampling, structured logs with correlation IDs, SLO dashboards and burn-rate alerts, dependency maps, synthetic and RUM, change events, cardinality governance, runbooks.

**44 (sketch).** Confirm via SLO dashboard; segment by region/route/version/tenant; check deploy/flag timeline; look at the latency heatmap and dependency latencies; open a slow trace via exemplar; find the span; check saturation/logs/profile; mitigate; verify.

**45 (sketch).** Page only on SLO burn, tiered windows, dedupe/inhibition, ownership/routing, runbooks, tickets for non-urgent, regular alert review (delete/tune alerts with low actionability), measure pages per shift, blameless follow-up.

**46.** Imagine schools of 10 and 2,000 students, each with a “median score.” Averaging the schools’ medians treats a 10-student school as important as a 2,000-student one, and the middle student of the whole district isn’t the average of the schools’ middles. To get the district median you need the full distribution (the histogram of all scores), not the per-school medians.

---

## 21.18 Flashcards

| Front | Back |
|---|---|
| Monitoring vs observability | Known conditions vs arbitrary new questions |
| Signals | Metrics detect, traces localize, logs explain, profiles locate code |
| RED / USE | Rate, Errors, Duration / Utilization, Saturation, Errors |
| Golden signals | Latency, traffic, errors, saturation |
| Histogram vs summary | Aggregatable vs not |
| Cardinality | Distinct label combos = series; ~3 KB RAM each |
| Cardinality lab | +pod = 10×; +user_id → 2 M series, 16 GB/day |
| Percentile aggregation | Never average; merge histograms/sketches (avg of p99s +12% in lab) |
| Bucket accuracy | 8 coarse buckets: 33% error at p99.9; 6% exponential: 0.2% |
| Exemplar | Trace ID attached to a metric sample |
| Structured logs | JSON, trace/request IDs, no secrets |
| Wide event | One rich event per request per service |
| Trace context | W3C `traceparent`; headers on messages; links for async |
| Head vs tail sampling | 1.1% vs 100% of failing traces captured (lab) |
| Tail sampling cost | Buffer in-flight traces; route by trace ID |
| OpenTelemetry | Vendor-neutral SDK + OTLP + Collector |
| Burn-rate alerting | Page on SLO budget burn; multi-window |
| Naive vs burn-rate | 22–23 false pages vs 0 in 30 days (lab) |
| Dead-man’s switch | Always-firing alert; silence = broken pipeline |
| Investigation | Scope → what changed → mitigate → trace → saturation → logs/profile |
| Out-of-band | Monitoring must not share fate with the system |
| Cost levers | Cardinality budgets, sampling, retention tiers, index less |

## 21.19 Glossary additions

| Term | Simple Meaning | Technical Meaning | Why It Exists | Example |
|---|---|---|---|---|
| Observability | Ability to ask new questions | Inferring internal state from telemetry | Debug unknown unknowns | Slice by provider |
| Metric | Numeric time series | Aggregated measurement over time | Detection | `http_requests_total` |
| Time series | Values over time | Stream of timestamped samples per label set | Metrics storage | Series |
| Cardinality | Number of series | Distinct label-value combinations | Cost driver | 624 k |
| Label / tag | Dimension | Key-value attached to a metric | Slicing | `region=eu` |
| Histogram | Bucketed distribution | Counts per bucket + sum/count | Aggregatable latency | `le="0.25"` |
| Exemplar | Example link | Trace ID on a metric sample | Metric→trace | Heatmap click |
| RED / USE | Service / resource method | Rate-Errors-Duration / Utilization-Saturation-Errors | Standard signals | Dashboard |
| Structured log | Machine-readable log | JSON event with fields | Searchable | `{"event":"x"}` |
| Wide event | Rich per-request event | Single event with many attributes | Arbitrary analysis | Canonical log line |
| Trace / span | Request path / unit of work | Tree of timed operations sharing a trace ID | Localize latency | Jaeger |
| Context propagation | Passing trace IDs along | Headers/metadata carrying trace context | Connect spans | `traceparent` |
| OpenTelemetry | Telemetry standard | APIs, SDKs, OTLP, Collector | Vendor neutrality | OTel Collector |
| Head sampling | Decide at start | Probabilistic trace selection up front | Cheap | 1% |
| Tail sampling | Decide at end | Selection after trace completes | Keep errors/slow | Collector policy |
| Continuous profiling | Always-on profiler | Low-overhead sampled profiles | Code-level cost | pprof |
| RUM | Real user monitoring | Client-side telemetry | User view | Core Web Vitals |
| Synthetic monitoring | Scripted probes | External checks of journeys | Outside-in availability | Uptime check |
| SLO burn rate | Budget spend speed | Error rate ÷ (1 − SLO) | Paging policy | 14.4× |
| Alert fatigue | Ignored alerts | Desensitization from noisy pages | Reliability risk | 31 buzzes/week |
| Dead-man’s switch | Heartbeat alert | Alert on absence of an always-firing signal | Pipeline health | Watchdog |
| Recording rule | Precomputed query | Stored aggregation | Fast dashboards | `job:rate5m` |

---

**Next: Chapter 22: Security for System Designers.** Threat modeling, authentication and authorization (OAuth/OIDC, JWT, mTLS), object-level authorization, secrets and key management, encryption at rest and in transit with a measured AES-GCM lab (including the nonce-reuse failure), password hashing costs, rate limiting and credential-stuffing defenses, multi-tenant isolation, and secure design reviews.

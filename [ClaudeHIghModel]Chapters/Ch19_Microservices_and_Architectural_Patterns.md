# Chapter 19: Microservices and Architectural Patterns

## Chapter card

| | |
|---|---|
| **Objective** | Decide *when and how* to split a system into services, and how to structure the result: architecture styles (monolith, modular monolith, SOA, microservices, serverless, event-driven), the measured costs of distribution (latency, availability, chattiness), service boundaries via domain-driven design, data ownership, communication styles and API evolution, migration (strangler fig), and the organizational side (Conway’s law, team topologies) |
| **Why this chapter exists** | “Microservices” is the most over-applied pattern in the industry. A strong designer can explain *what problem* each architectural style solves, quantify its costs, and propose the simplest structure that meets the requirements. This chapter turns that judgment into numbers and checklists |
| **Prerequisites** | Chapters 2, 5, 7, 12, 14, 17, 18 |
| **Study time** | 9–12 hours including labs |
| **Tier** | `[GK]`; boundaries, communication, and costs are `[MK]` for interviews |
| **Mastery criteria** | (1) Compare monolith, modular monolith, SOA, microservices, and serverless on deployment, scaling, coupling, and operations. (2) Quantify the latency, availability, and chattiness cost of service decomposition. (3) Identify service boundaries with bounded contexts and business capabilities. (4) Apply database-per-service and the patterns for cross-service queries. (5) Choose between synchronous and asynchronous communication; evolve APIs compatibly. (6) Plan a strangler-fig migration. (7) Explain Conway’s law and team topologies. (8) Recognize anti-patterns (distributed monolith, shared database, nano-services). (9) Recommend a starting architecture and justify it |

---

## 19.1 The Story: Two Companies, Two Decisions

**Company A (BuyMore)** has a 9-year-old e-commerce monolith (1.2 M lines, 140 engineers). Releases happen every two weeks after a three-day regression cycle; any team’s bug blocks everyone; the build takes 55 minutes. The CTO announces: “We’re moving to microservices.” Eighteen months later there are **210 services**:
- Rendering the product page now makes **38 service calls**, 14 of them sequential. p50 latency went from 120 ms to 480 ms; p99 from 400 ms to 3.1 s.
- Services share one big database “for now” (schema changes require meetings of seven teams).
- A new engineer needs 4 days to run the system locally; nobody can say what a given request touches.
- An outage in the *Reviews* service takes down the product page, because it is called synchronously without a timeout.
- The infrastructure bill is up 70% (hundreds of idle containers, service meshes, duplicated caches).
- Release frequency: **still every two weeks**, because the services must be deployed in a specific order.

**Company B (Pinwheel)** has 25 engineers and a Rails monolith. It restructures into a **modular monolith**: strict module boundaries enforced in the build, one schema per module, internal interfaces. It extracts exactly two services (image processing, because it needs GPU workers and independent scaling; payments, because of PCI scope). Deploys happen 20 times a day from trunk. Onboarding takes half a day.

### Pause & Predict 19.1
> What did Company A actually change: the *structure of the code* or the *structure of the system and the teams*? List the specific costs it paid that Company B avoided. Under what circumstances would Company B eventually be right to split further? What exactly was Company A’s original problem, and could it have been solved without a network between its components?

---

## 19.2 The architecture styles `[MK]`

| Style | Description | Deployment unit | Typical fit |
|---|---|---|---|
| **Monolith** | One codebase, one deployable, in-process calls, usually one database | 1 | Early products, small teams, simple domains |
| **Modular monolith** | A monolith with strict internal module boundaries, enforced interfaces and per-module data ownership | 1 | Most products up to dozens/hundreds of engineers |
| **Layered (n-tier)** | Presentation / business / data layers | 1+ | Traditional enterprise apps |
| **SOA** | Coarse-grained shared services, often with an enterprise service bus (ESB), heavy standards (SOAP/WS-*) | Several | Large enterprises, integration-heavy |
| **Microservices** | Many small, independently deployable services around business capabilities; own data; lightweight communication | Many | Large organizations needing team autonomy; very different scaling profiles |
| **Serverless / FaaS** | Event-triggered functions managed by a platform; pay per invocation | Many tiny | Spiky, event-driven, glue logic |
| **Event-driven** | Components communicate through events/streams (can be combined with any of the above) | Varies | Decoupling, integration, real-time data |
| **Hexagonal / Clean** | *Internal* structure: domain core with ports/adapters, dependencies pointing inward | n/a | Testability and independence from frameworks within any service |
| **Space-based / cell-based** | Replicated self-contained units with in-memory data grids / isolated cells | Cells | Extreme scale, blast-radius control |
These are **not mutually exclusive**: a modular monolith can be event-driven internally and extract a few services later; each microservice should be hexagonal inside.

### 19.2.1 What “microservice” should mean
A service is “micro” not because of lines of code but because it is **independently deployable, owned by one team, and encapsulates its own data and a cohesive business capability**. If you cannot deploy it without coordinating with another team, or if it cannot function (even degraded) without five others, it is not independent; you built a distributed monolith.

---

## 19.3 Why split? And what it costs `[MK]`

### 19.3.1 Genuine benefits
1. **Independent deployability:** teams ship on their own schedule; smaller blast radius per change.
2. **Team autonomy and ownership** (Conway’s law, §19.11): a team owns a service end to end (build, run, on-call).
3. **Independent scaling:** scale the hot component (search, image processing) without scaling everything.
4. **Technology heterogeneity** where it is truly justified (GPU workloads, specialized datastores).
5. **Fault isolation** (if designed with timeouts, bulkheads, fallbacks; Ch 18).
6. **Compliance isolation:** reduce the audit scope of PCI/PHI components.
7. **Organizational scaling:** coordination cost grows superlinearly with team size in a shared codebase (Brooks’ law); service boundaries cap the number of people who must agree.

### 19.3.2 The costs (the “microservice premium”)
- **Network instead of function calls:** latency, partial failure, serialization, versioning (Ch 14).
- **Distributed data:** no joins, no cross-service ACID transactions → sagas, eventual consistency (Ch 17).
- **Operational overhead:** CI/CD per service, service discovery, secrets, observability, on-call, upgrades, security patching ×N.
- **Testing complexity:** contract tests, environments, end-to-end tests that are slow and flaky.
- **Debugging complexity:** requests span services; need tracing and correlation IDs (Ch 21).
- **Duplicated infrastructure** and baseline resource waste per service.
- **Cognitive load:** nobody understands the whole system.
- **Organizational maturity required:** platform tooling, DevOps culture, API governance.

### 19.3.3 Measured: what a network between components costs (Lab 1)
**(a) Latency compounds.** Every call: median 10 ms, p99 100 ms (lognormal) plus 0.5 ms network. Sequential chains:
```text
   k    p50 (ms)    p99 (ms)    p99.9 (ms)    vs one call: p99 multiplier
   1       10.5        99.3        212.8      1.0x
   2       25.4       147.7        276.0      1.5x
   3       41.0       187.4        338.6      1.9x
   5       73.2       252.5        412.9      2.5x
   8      122.2       336.7        500.4      3.4x
  12      188.8       439.6        634.0      4.4x
```
A request that traverses 12 services in series has a median 18× and a p99 4.4× that of a single hop. In process, the same 12 calls would cost microseconds each.

**(b) Fan-out waits for the slowest.** One request calling *f* services in parallel and waiting for all:
```text
   f    p50 (ms)    p99 (ms)    p99.9 (ms)    | wait for only 90% of replies: p50 / p99 (ms)
    1       10.5       100.1        213.5      |       10.5 /    100.1
    5       31.0       172.3        328.7      |       31.0 /    172.3
   10       44.6       213.4        390.9      |       27.0 /     85.3
   50       89.2       330.3        597.2      |       33.7 /     59.0
  100      114.9       395.8        675.7      |       34.8 /     51.9
```
Fanning out to 100 services makes the *median* request as slow as a single service’s p96; waiting for all of them gives p99 of 396 ms, but **waiting for only 90% of replies (and degrading gracefully for the rest) cuts p99 to 52 ms**: a design choice, not a tuning knob (the page renders without the slowest 10% of widgets).

**(c) Availability multiplies.** Each service 99.9% available, independent failures:
```text
design                                                          analytic     simulated     downtime per year
monolith (1 deployable)                                          99.900%      99.898%        8.8 hours
5 services, all hard dependencies                                99.501%      99.506%       43.7 hours
12 services, all hard dependencies                               98.807%      98.799%      104.5 hours
12 services: 4 hard + 8 optional with graceful fallback          99.601%      99.602%       35.0 hours
12 services: 4 hard + 8 optional, NO fallback (treated as hard)   98.807%      98.814%      104.5 hours
```
Twelve hard synchronous dependencies at “three nines” each give **98.8%: 104 hours of downtime per year, 12× the monolith**. Classifying eight of them as optional and *actually implementing fallbacks* recovers 70 hours a year (35 vs 104.5). If you classify them but don’t implement fallbacks, you get nothing. **Availability is a property of the dependency graph and of your fallback discipline, not of the individual services.**

**(d) Chatty APIs are expensive.** The page needs 50 items from another service; each call is 0.5 ms network + median 5 ms service time (p99 40 ms):
```text
approach                                                     p50 (ms)    p99 (ms)
50 sequential GET /item/{id}                                    393.0       562.1
50 calls, 10 in parallel at a time (5 waves)                    111.5       224.8
50 calls, all in parallel                                        36.3       118.1
1 batch call GET /items?ids=... (service time +0.05 ms/item)      8.0        42.7
```
The N+1 call pattern is the classic microservice performance bug: **393 ms vs 8 ms** for the batch API: a 49× difference, purely from API design. Coarse-grained, batchable, and aggregated APIs (and read models owned by the consumer) are essential across service boundaries.

**Lessons:** (1) keep synchronous call depth shallow (≤ 3–4 hops on the critical path); (2) classify dependencies as critical/optional and implement fallbacks; (3) design APIs for batching and aggregation; (4) cache and replicate read data close to consumers; (5) the cheapest remote call is the one you don’t make.

---

## 19.4 Monolith first, modular monolith, and when to split `[MK]`

### 19.4.1 “Monolith first”
Martin Fowler’s observation `[Public]`: almost all successful microservice systems started as monoliths that grew too big; almost all systems built as microservices from scratch ran into trouble. Early on, you don’t yet know the right boundaries, and getting them wrong across network and data boundaries is far more expensive than getting them wrong across packages in one codebase.

### 19.4.2 The modular monolith (the underrated default)
A single deployable with **enforced module boundaries**:
- **Modules = bounded contexts / business capabilities** (Orders, Catalog, Payments, Identity).
- **Explicit public interfaces** (in-process APIs or a facade per module); other modules cannot reach internals. Enforce with language visibility, build-tool rules (ArchUnit, Gradle/Maven modules, Go internal packages, Nx boundaries), and code review.
- **Data ownership per module:** each module owns its tables/schema; no cross-module joins or foreign keys; cross-module access through the interface or domain events. (Even in one database, separate schemas.)
- **Asynchronous internal events** (in-process bus or outbox) to decouple modules.
- **Independent test suites per module;** fast whole-system builds.
Benefits: ACID transactions across modules where needed (initially), simple deployment and debugging, refactoring is cheap, zero network cost, and a clean **extraction path**: a well-bounded module can be moved out later into its own service by replacing the in-process interface with a remote one.

### 19.4.3 Signals that it’s time to extract a service
- **Team and release contention:** many teams in one codebase blocking each other; a deployment frequency ceiling you cannot fix by modularizing and improving CI.
- **Divergent scaling profiles:** one component needs 50× the capacity or GPUs/different hardware.
- **Divergent availability/blast-radius needs:** a flaky component must not take down the core (isolate with a bulkhead, or a service).
- **Different technology/security/compliance needs** (PCI/PHI scope; a language or datastore better suited to the job).
- **Different lifecycle/rate of change:** a stable core vs a fast-moving experiment.
- **Organizational autonomy:** a team is ready to own build-run-operate of a capability.
### 19.4.4 Signals that you should *not* split (yet)
Small team (< ~30–50 engineers); domain boundaries unclear; no CI/CD, observability, or on-call maturity; the “problem” is code quality (a spaghetti monolith becomes spaghetti microservices with latency); the split would cut through transactions that must be atomic; the motivation is résumé-driven or “because Netflix.” **Fix modularity first; a monolith with good boundaries is far better than microservices with bad ones.**

### 19.4.5 Infrastructure tax: a back-of-envelope
Each service needs (at minimum) CI pipeline, container image, 2–3 replicas for HA, a sidecar/mesh proxy, logging/metrics agents, secrets, dashboards, alerts, on-call. If each replica idles at 0.25 vCPU and 300 MB, **200 services × 3 replicas** = 150 vCPU and 180 GB RAM of baseline before serving a request; the human cost (upgrades, certificate rotation, dependency updates ×200) is larger still. Platform investment (golden paths, templates, automation) is the price of microservices at scale.

---

## 19.5 Service boundaries and domain-driven design `[MK/GK]`

### 19.5.1 Where to draw boundaries
Bad boundaries are the root of most microservice pain. Principles:
1. **Align with business capabilities / bounded contexts,** not technical layers (a “database service,” “validation service,” or “UI service” is a layer, not a capability).
2. **High cohesion, low coupling:** things that change together live together; a feature change should usually touch **one** service.
3. **Own your data:** the service is the sole writer (and gatekeeper) of its data (§19.6).
4. **Minimize chatty communication** across the boundary (lab part d); if two services talk constantly, they probably belong together.
5. **Keep transactional invariants inside a boundary** (an aggregate and its invariants live in one service; Ch 17).
6. **Boundaries follow teams** (Conway), ideally one team per service group, with a team able to own it end to end.
7. **Size by ownership and replaceability,** not lines of code: a service a small team can understand fully and rewrite in weeks.

### 19.5.2 Domain-driven design vocabulary
- **Domain / subdomain:** the business problem space, split into *core* (differentiating), *supporting*, and *generic* (buy/outsource: auth, billing).
- **Bounded context:** an explicit boundary within which a domain model and its language (“ubiquitous language”) are consistent. The word “Customer” means different things to Sales, Support, and Billing; each context has its own model. **Bounded contexts are the natural service (or module) boundaries.**
- **Aggregate:** a cluster of objects with a root, treated as a unit for consistency (an Order and its lines). Transactions don’t span aggregates (Ch 17).
- **Context mapping:** relationships between contexts: **shared kernel** (shared code/model: tight coupling), **customer–supplier** (upstream/downstream with negotiated needs), **conformist** (downstream accepts upstream’s model), **anti-corruption layer (ACL)** (translate foreign models to protect your own), **open host service / published language** (a stable, documented public interface), **separate ways** (no integration).
- **Event storming:** a workshop technique that discovers events, commands, aggregates, and contexts by mapping business processes on a wall.

### 19.5.3 Decomposition heuristics
- **By business capability** (what the business does: catalog, pricing, checkout, fulfilment).
- **By subdomain** (DDD).
- **By data/aggregate ownership** and transaction boundaries.
- **By volatility** (separate fast-changing from stable).
- **By scaling or availability needs** and **by compliance**.
- Validate with **change analysis:** look at version-control history: which files change together? Which teams collide? And **call analysis:** which calls are hottest? A cut that slices the most frequent change sets or hottest call paths is wrong.

### 19.5.4 Coupling, the thing to minimize
| Coupling | Meaning | Example | Remedy |
|---|---|---|---|
| **Temporal** | A calls B and needs the answer now | Synchronous call chains | Async messaging, caching, fallbacks |
| **Deployment** | A and B must be deployed together | Shared libraries with breaking changes, lock-step API versions | Backward-compatible APIs, versioning, consumer-driven contracts |
| **Domain/semantic** | A depends on B’s internal model | Shared DB tables, leaked entities | Published language, ACL |
| **Data** | A reads B’s tables | Shared database | Own your data; events/APIs |
| **Platform** | Same runtime/language forced | Mandatory tech stack | Standard interfaces; paved roads, not mandates |
**Distributed monolith test:** if a change to one service routinely forces coordinated changes/deploys in others, or the system can’t run when any one service is down, you have the worst of both worlds.

---

## 19.6 Data management in a service architecture `[MK]`

### 19.6.1 Database per service
Each service owns its datastore (a separate database, or at least a separate schema with access control); **other services access it only through its API or events**. This enables independent schema evolution, technology choice (polyglot persistence: relational for orders, search index for catalog, key-value for sessions), and independent scaling. A shared database is the most common “microservice” anti-pattern: it couples every service to every schema change and makes independent deployment impossible.

### 19.6.2 Queries that cross services (no joins!)
| Pattern | How | Trade-offs |
|---|---|---|
| **API composition** | A composer (gateway/BFF/aggregator service) calls several services and joins in memory | Simple; latency = slowest call; no cross-service consistency; heavy for big joins/pagination |
| **CQRS read model / materialized view** | A service maintains a denormalized copy of the data it needs, updated via events | Fast reads, decoupled; eventual consistency; duplicated data (Ch 17) |
| **Event-carried state transfer** | Events contain the data consumers need so they don’t call back | Fewer calls; larger events; staleness |
| **Data replication of reference data** | Replicate slowly-changing reference data (countries, currency, product names) locally | Stale-tolerant; need change feed |
| **Search/analytics store** | Feed a search engine or warehouse from events/CDC for cross-cutting queries | Not for transactional reads |
Rule: **replicate data you read often; call for data you rarely need or must have fresh.**

### 19.6.3 Writes that cross services
No distributed ACID: use **sagas, the outbox pattern, idempotent consumers** (Ch 12, 17). Each service keeps its own invariants; cross-service invariants are eventually consistent and monitored via reconciliation.

### 19.6.4 Data ownership and duplication
Duplicating data is acceptable and expected, as long as **there is one system of record per fact** and copies are read-only derivatives with known staleness. Beware ownership ambiguity (“who owns the customer address?”); resolve via contexts (billing address vs shipping address belong to different contexts).

---

## 19.7 Communication styles `[MK]`

### 19.7.1 Synchronous: request/response
REST/JSON over HTTP, gRPC (HTTP/2, protobuf, streaming), GraphQL (client-shaped queries). ✔ Simple mental model, immediate results. ✘ Temporal coupling (the callee must be up), latency chains, cascading failures (Ch 18). Use for **queries** and for **commands that need an immediate answer**, with timeouts, retries (idempotent), breakers, and fallbacks.

### 19.7.2 Asynchronous: messaging and events
Message queues and pub/sub (Ch 12), streams (Ch 13). ✔ Temporal decoupling, buffering, fan-out, resilience to downtime. ✘ Eventual consistency, harder tracing, ordering/duplicates. Three message intents:
- **Command** (“do X”): directed at one service, expects it to be done; can be async with a reply/event.
- **Event** (“X happened”): a fact broadcast to anyone interested; the producer doesn’t know the consumers.
- **Query/reply:** usually synchronous; async request/reply is rarely worth it.

### 19.7.3 Choosing
| Situation | Prefer |
|---|---|
| User is waiting for data to render | Sync query (with timeout/fallback), or read model |
| State change that other services react to | **Events** (publish and forget; outbox) |
| Long-running business process across services | Saga (orchestrated or choreographed) |
| Heavy/bursty background work | Queue + workers |
| Needs strong immediate consistency | Keep it inside one service |
| Notifying many consumers | Pub/sub |
**Rule of thumb:** synchronous for queries on the user’s critical path; asynchronous for state changes and integration. Avoid synchronous call chains for writes.

### 19.7.4 Orchestration vs choreography (recap, Ch 17)
Orchestration gives visibility and central control (good for complex flows); choreography gives autonomy (good for simple, stable flows). Mixed approaches are common.

### 19.7.5 API design and evolution `[MK]`
- **Backward compatibility:** add optional fields, don’t remove or change meaning; **tolerant reader** (ignore unknown fields); Postel’s law (be conservative in what you send, liberal in what you accept).
- **Versioning:** URL/header versions for breaking changes; run versions in parallel; deprecation windows; measure usage before removal.
- **Consumer-driven contract tests** (Pact): consumers publish expectations; providers verify them in CI, catching breaking changes before deploy without full integration environments.
- **Schemas:** protobuf/Avro with registry compatibility checks (Ch 13); OpenAPI for REST.
- **Idempotency keys** on writes; **pagination** with cursors; **batch endpoints**; **bulk/aggregation** APIs; **rate limits and quotas** per consumer; **timeouts and deadlines** documented; **error models** consistent.
- **API-first:** design and review the contract before implementation; publish a catalog.

---

## 19.8 Edge and integration patterns `[MK/GK]`

| Pattern | Purpose |
|---|---|
| **API gateway** (Ch 7) | Single entry point: auth, rate limiting, routing, aggregation |
| **Backend for Frontend (BFF)** | A gateway per client type (web, iOS, TV) shaping and aggregating responses; owned by the frontend team; reduces chattiness |
| **Service mesh** (Ch 7) | Sidecar proxies: mTLS, retries, observability, traffic shifting (adds overhead and complexity) |
| **Sidecar / ambassador** | Co-deployed helper process (logging, proxying, config) |
| **Anti-corruption layer** | Translation layer to isolate your model from a legacy or foreign one |
| **Strangler fig** | Incrementally replace a legacy system by routing slices of functionality to new components until the old can be retired (§19.9) |
| **Branch by abstraction** | Introduce an abstraction over the part you’ll replace, implement the new version behind it, switch gradually |
| **Saga / outbox / inbox** | Cross-service consistency and reliable messaging (Ch 12, 17) |
| **Circuit breaker / bulkhead / retry budget** | Resilience (Ch 18) |
| **Service registry & discovery** | Locate instances (Ch 7) |
| **Shared libraries vs services** | Prefer libraries for stable cross-cutting logic that doesn’t need independent scaling or data (watch for lock-step upgrades) |
| **Sync-over-async facade** | Offer a synchronous API on top of an async process (return 202 + status resource/callback) |
| **Micro frontends** | Split the UI into independently deployable pieces aligned with teams (ambitious; adds integration complexity) |

---

## 19.9 Migrating from a monolith: the strangler fig `[MK]`

Big-bang rewrites fail (two systems to maintain, moving target, years without value). The strangler approach delivers value continuously:
```text
 Step 1: put a routing layer (proxy/gateway) in front of the monolith
 Step 2: pick a seam (a capability with clear boundaries and data) and build it as a new service/module
 Step 3: route a slice of traffic (by feature flag, user cohort, % canary) to the new implementation; compare outputs (shadow/parallel run)
 Step 4: migrate the data (dual-write/CDC, backfill, verify); make the new component the system of record
 Step 5: remove the old code path; repeat with the next seam
```
**Choosing seams:** start with loosely coupled, high-value, low-risk capabilities (notifications, search, reporting, image processing), not the core transactional heart. Look for natural seams in the code (packages with few inbound dependencies), data (tables accessed by one module), and teams.
**Hard part: the data.** Steps: (1) make the monolith access the data through a module interface; (2) separate schema; (3) replicate to the new store (CDC); (4) switch reads; (5) switch writes; (6) drop the old tables. Handle foreign keys that cross the seam (replace with IDs + eventual consistency/validation), shared transactions (introduce sagas), and reporting joins (move to the warehouse).
**Guard rails:** an anti-corruption layer so the new service doesn’t inherit the monolith’s model; parallel-run comparison; the ability to roll back each step; metrics for the migration (percentage of traffic, defect rate).

---

## 19.10 Event-driven architecture (EDA) `[MK/GK]`

Components communicate by publishing and subscribing to events (Ch 12, 13). Styles:
1. **Event notification:** a thin event (“OrderPlaced{id}”); consumers call back for details. Low coupling in data but creates callback traffic.
2. **Event-carried state transfer:** events contain the changed data; consumers maintain local copies (read models). Fewer calls; eventual consistency; larger payloads.
3. **Event sourcing:** state is derived from the event log (Ch 17).
4. **Choreographed sagas** (Ch 17).
**Benefits:** loose coupling (producers don’t know consumers), easy addition of consumers, buffering and resilience, natural audit trail, real-time reactions, scalability.
**Costs:** the system’s behavior is emergent and hard to trace (“who reacts to this event?”), eventual consistency, event schema governance, duplicate and out-of-order handling, debugging, testing of flows, and risk of event spaghetti (cycles).
**Guidelines:** name events as business facts in the past tense; separate **domain events** (internal to a context) from **integration events** (stable public contracts); version and register schemas; keep events small but self-sufficient for consumers’ needs; document the event catalog (AsyncAPI); build tooling for tracing (correlation IDs through events) and replay; use idempotent consumers and DLQs.

---

## 19.11 Serverless and FaaS `[GK]`

**Model:** deploy individual functions; the platform provisions instances on demand, scales to zero, charges per invocation and compute time (AWS Lambda, Google Cloud Functions/Run, Azure Functions, Cloudflare Workers).
**Benefits:** no server management, automatic scaling, pay-per-use (great for spiky or low traffic), fast to prototype, natural fit for event handlers (S3 upload → function, queue → function, cron).
**Costs and limits:**
- **Cold starts** (tens of ms to seconds, depending on runtime, package size, VPC attachment) harm p99.
- **Execution limits:** time (e.g., 15 min), memory, payload size, concurrency limits; statelessness (state must be external).
- **Cost curve inverts at high steady load:** per-request pricing exceeds the cost of always-on instances. *Illustrative:* 100 M requests/month, 200 ms each at 512 MB ≈ 10 M GB-seconds; at typical list prices (~$0.0000167 per GB-s plus ~$0.20 per million requests) ≈ $167 + $20 ≈ **$190/month**: cheap. At 5 B requests/month the same workload is ≈ **$9,400/month**, where a handful of reserved containers at a few hundred dollars would do. (Prices change; recompute with current rates.)
- **Vendor lock-in** (triggers, IAM, runtime semantics), harder local testing, observability gaps, connection management to databases (many short-lived instances → connection storms; use proxies like RDS Proxy), orchestration of many functions becomes complex (use workflow services).
**Use for:** event glue, spiky APIs, scheduled jobs, ETL steps, prototypes, edge logic. **Avoid** for latency-critical steady high-throughput services, long-running or stateful workloads, or heavy local-state needs. Containers-as-a-service (Cloud Run, Fargate) offer a middle ground (Ch 23).

---

## 19.12 The organizational side `[MK]`

### 19.12.1 Conway’s law
“Organizations which design systems are constrained to produce designs which are copies of the communication structures of these organizations” (Melvin Conway, 1968). Four teams building a compiler produce a four-pass compiler. If your service boundaries don’t match team boundaries, interfaces will be negotiated across team lines, slowly. The **inverse Conway maneuver:** deliberately structure teams to produce the architecture you want (a team per bounded context, owning it end to end).

### 19.12.2 Team Topologies `[Public: Skelton & Pais]`
- **Stream-aligned teams:** own a flow of business value (a product area) end to end.
- **Platform teams:** provide internal self-service capabilities (CI/CD, observability, runtime, data platform) so stream-aligned teams don’t each reinvent them; treat the platform as a product with “paved roads.”
- **Enabling teams:** help others adopt practices (SRE, security, architecture).
- **Complicated-subsystem teams:** own deeply specialized components (ML, video encoding).
Three interaction modes: collaboration, X-as-a-service, facilitation. **Cognitive load** is the limiting factor: a team should own only what it can understand and operate (rule of thumb: a team of ~5–9 engineers owns 1–5 services).

### 19.12.3 Ownership and governance
- **“You build it, you run it”:** the owning team is on call; incentives align with reliability.
- **Service catalog** (owner, tier, on-call, SLOs, dependencies, runbooks, repo): Backstage-style.
- **Standards without bureaucracy:** a small number of mandatory cross-cutting standards (security, logging format, trace propagation, API conventions, SLO reporting), **golden paths** for everything else.
- **Architecture decision records (ADRs)**, API review, dependency maps, deprecation policies.
- **Monorepo vs polyrepo:** monorepo eases cross-service refactors, shared tooling, and atomic changes; polyrepo gives clearer ownership/boundaries; both work with good tooling; avoid shared mutable libraries that force lock-step upgrades.

---

## 19.13 Testing, delivery, and operations of service architectures `[MK]`

- **Test pyramid adapted:** many unit tests; component tests (service with its own datastore, dependencies stubbed); **contract tests** (consumer-driven) at boundaries; few end-to-end tests (slow, flaky; focus on critical user journeys); **testing in production** (canaries, synthetic transactions, feature flags, shadow traffic); chaos/resilience tests (Ch 18).
- **Independent deployability test:** can you deploy this service alone, at any time, with zero coordination? If not, fix the coupling.
- **CI/CD per service,** trunk-based development, progressive delivery (Ch 18).
- **Observability** (Ch 21): distributed tracing with propagated context, structured logs with correlation IDs, RED/USE metrics per service, SLOs per user journey, dependency graph visualization.
- **Security** (Ch 22): zero-trust between services, mTLS and workload identity, least-privilege service accounts, secrets management, consistent authN/authZ propagation (tokens), API gateway enforcement plus defense in depth.
- **Local development:** the ability to run a service with stubs/containers (docker-compose, Tilt/Skaffold), or to develop against a shared dev environment with request routing/overrides; make onboarding a metric.

---

## 19.14 Anti-patterns and failure modes

| Anti-pattern | Symptoms | Fix |
|---|---|---|
| **Distributed monolith** | Lock-step deploys; any outage breaks everything; shared DB | Re-draw boundaries; own data; async; contract tests; or merge back |
| **Shared database** | Schema changes need cross-team meetings | Database per service; events/APIs |
| **Chatty services / N+1 calls** | High latency, 10–50 calls per request (lab: 393 vs 8 ms) | Batch APIs, aggregation, read models, merge services |
| **Long synchronous chains** | Tail latency and availability multiply (lab: 12 services → 4.4× p99, 98.8% availability) | Shorten chains, parallelize, async, fallbacks |
| **Nano-services** | Too many tiny services, huge overhead, no ownership | Consolidate around capabilities |
| **God service** | One service everybody depends on | Split by capability; publish events instead of queries |
| **Circular dependencies** | A→B→C→A; startup/ordering issues | Break cycles with events or merge |
| **Big-bang rewrite** | Years without delivery | Strangler fig |
| **Resume-driven design** | Technology diversity without need | Paved roads; justify exceptions |
| **No platform** | Every team reinvents CI/CD/observability | Platform team, golden paths |
| **Versioning hell** | Many consumers on old API versions | Compatibility, deprecation, contract tests |
| **Shared library lock-step** | Upgrading a common lib requires all services | Keep libs small, stable, backwards compatible; prefer services/sidecars for logic |
| **Orphaned services** | No owner on-call | Service catalog, ownership policy, sunset process |
| **Data inconsistency** | Services disagree on state | Sagas, reconciliation, idempotency, single system of record |
| **Mock-heavy tests that pass while prod breaks** | Integration failures | Contract tests, canaries, synthetic probes |
| **Security gaps between services** | Flat internal network trust | Zero-trust, mTLS, authz at each service |
| **Cost blindness** | Idle replicas, duplicated caches | FinOps, rightsizing, consolidation |
| **Microservices envy** | Splitting a 10-person team into 40 services | Modular monolith; split when forces appear |

---

## 19.15 Labs

### Lab 1: The measurable cost of splitting a system (Java 17+)
```java
import java.util.*;

public class MicroLab {
    static final Random R = new Random(31);
    static final double SIGMA = Math.log(100.0 / 10.0) / 2.326;                     // lognormal with median 10 ms and p99 100 ms
    static double call() { return 0.5 + Math.exp(Math.log(10) + SIGMA * R.nextGaussian()); }     // one remote call: 0.5 ms network + service time
    static double pct(double[] sorted, double p) { return sorted[(int) Math.min(sorted.length - 1, Math.floor(p * sorted.length))]; }

    static void latencyComposition() {
        int n = 200_000;
        System.out.println("== 1) Latency composition. Every service call: median 10 ms, p99 100 ms (lognormal) + 0.5 ms network. 200,000 requests per row. ==");
        System.out.println("A) k services called SEQUENTIALLY (each depends on the previous one):");
        System.out.println("   k    p50 (ms)    p99 (ms)    p99.9 (ms)    vs one call: p99 multiplier");
        double base99 = 0;
        for (int k : new int[]{1, 2, 3, 5, 8, 12}) {
            double[] x = new double[n]; for (int i = 0; i < n; i++) { double s = 0; for (int j = 0; j < k; j++) s += call(); x[i] = s; } Arrays.sort(x);
            if (k == 1) base99 = pct(x, 0.99);
            System.out.printf("  %2d   %8.1f   %9.1f   %10.1f      %.1fx%n", k, pct(x, 0.5), pct(x, 0.99), pct(x, 0.999), pct(x, 0.99) / base99);
        }
        System.out.println("B) one request fans out to f services IN PARALLEL and waits for ALL of them:");
        System.out.println("   f    p50 (ms)    p99 (ms)    p99.9 (ms)    | wait for only 90% of replies: p50 / p99 (ms)");
        for (int f : new int[]{1, 5, 10, 50, 100}) {
            double[] all = new double[n], most = new double[n];
            for (int i = 0; i < n; i++) { double[] c = new double[f]; for (int j = 0; j < f; j++) c[j] = call(); Arrays.sort(c); all[i] = c[f - 1]; most[i] = c[(int) Math.ceil(0.9 * f) - 1]; }
            Arrays.sort(all); Arrays.sort(most);
            System.out.printf("  %3d   %8.1f   %9.1f   %10.1f      |   %8.1f / %8.1f%n", f, pct(all, 0.5), pct(all, 0.99), pct(all, 0.999), pct(most, 0.5), pct(most, 0.99));
        }
    }

    static void availability() {
        int n = 2_000_000; double a = 0.999;
        System.out.println("\n== 2) Availability of a request that depends on other services (each service is 99.9% available, failures independent). 2,000,000 requests. ==");
        System.out.println("design                                                          analytic     simulated     downtime per year");
        record Design(String name, int hard, int optional) {}
        for (Design d : new Design[]{new Design("monolith (1 deployable)", 1, 0), new Design("5 services, all hard dependencies", 5, 0), new Design("12 services, all hard dependencies", 12, 0),
                                     new Design("12 services: 4 hard + 8 optional with graceful fallback", 4, 8), new Design("12 services: 4 hard + 8 optional, NO fallback (treated as hard)", 12, 0)}) {
            double analytic = Math.pow(a, d.hard()); int ok = 0;
            for (int i = 0; i < n; i++) { boolean good = true; for (int j = 0; j < d.hard() && good; j++) if (R.nextDouble() > a) good = false; if (good) ok++; }
            double sim = (double) ok / n;
            System.out.printf("%-62s %8.3f%%    %8.3f%%     %6.1f hours%n", d.name(), 100 * analytic, 100 * sim, (1 - analytic) * 8760);
        }
    }

    static void chattyVsBatched() {
        int n = 100_000, items = 50;
        System.out.println("\n== 3) Chatty vs batched APIs. The page needs 50 items from another service. Each call = 0.5 ms network + lognormal service time (median 5 ms, p99 40 ms). ==");
        double sg = Math.log(40.0 / 5.0) / 2.326;
        System.out.println("approach                                     p50 (ms)    p99 (ms)");
        String[] names = {"50 sequential GET /item/{id}", "50 calls, 10 in parallel at a time (5 waves)", "50 calls, all in parallel", "1 batch call GET /items?ids=... (service time +0.05 ms/item)"};
        for (int m = 0; m < 4; m++) {
            double[] x = new double[n];
            for (int i = 0; i < n; i++) {
                double t = 0;
                switch (m) {
                    case 0 -> { for (int j = 0; j < items; j++) t += 0.5 + Math.exp(Math.log(5) + sg * R.nextGaussian()); }
                    case 1 -> { for (int w = 0; w < 5; w++) { double mx = 0; for (int j = 0; j < 10; j++) mx = Math.max(mx, 0.5 + Math.exp(Math.log(5) + sg * R.nextGaussian())); t += mx; } }
                    case 2 -> { for (int j = 0; j < items; j++) t = Math.max(t, 0.5 + Math.exp(Math.log(5) + sg * R.nextGaussian())); }
                    default -> { t = 0.5 + Math.exp(Math.log(5) + sg * R.nextGaussian()) + 0.05 * items; }
                }
                x[i] = t;
            }
            Arrays.sort(x); System.out.printf("%-60s %8.1f    %8.1f%n", names[m], pct(x, 0.5), pct(x, 0.99));
        }
    }

    public static void main(String[] a) { latencyComposition(); availability(); chattyVsBatched(); }
}
```
**Sample output**
```text
== 1) Latency composition. Every service call: median 10 ms, p99 100 ms (lognormal) + 0.5 ms network. 200,000 requests per row. ==
A) k services called SEQUENTIALLY (each depends on the previous one):
   k    p50 (ms)    p99 (ms)    p99.9 (ms)    vs one call: p99 multiplier
   1       10.5        99.3        212.8      1.0x
   2       25.4       147.7        276.0      1.5x
   3       41.0       187.4        338.6      1.9x
   5       73.2       252.5        412.9      2.5x
   8      122.2       336.7        500.4      3.4x
  12      188.8       439.6        634.0      4.4x
B) one request fans out to f services IN PARALLEL and waits for ALL of them:
   f    p50 (ms)    p99 (ms)    p99.9 (ms)    | wait for only 90% of replies: p50 / p99 (ms)
    1       10.5       100.1        213.5      |       10.5 /    100.1
    5       31.0       172.3        328.7      |       31.0 /    172.3
   10       44.6       213.4        390.9      |       27.0 /     85.3
   50       89.2       330.3        597.2      |       33.7 /     59.0
  100      114.9       395.8        675.7      |       34.8 /     51.9

== 2) Availability of a request that depends on other services (each service is 99.9% available, failures independent). 2,000,000 requests. ==
design                                                          analytic     simulated     downtime per year
monolith (1 deployable)                                          99.900%      99.898%        8.8 hours
5 services, all hard dependencies                                99.501%      99.506%       43.7 hours
12 services, all hard dependencies                               98.807%      98.799%      104.5 hours
12 services: 4 hard + 8 optional with graceful fallback          99.601%      99.602%       35.0 hours
12 services: 4 hard + 8 optional, NO fallback (treated as hard)   98.807%      98.814%      104.5 hours

== 3) Chatty vs batched APIs. The page needs 50 items from another service. Each call = 0.5 ms network + lognormal service time (median 5 ms, p99 40 ms). ==
approach                                     p50 (ms)    p99 (ms)
50 sequential GET /item/{id}                                    393.0       562.1
50 calls, 10 in parallel at a time (5 waves)                    111.5       224.8
50 calls, all in parallel                                        36.3       118.1
1 batch call GET /items?ids=... (service time +0.05 ms/item)      8.0        42.7
```
**Experiments**
1. In part 1, change the per-call distribution to a lighter tail (p99 = 30 ms). How do the sequential and fan-out tail multipliers change? Then make it heavier (p99 = 300 ms). What does this say about the importance of controlling *tail* latency per service?
2. In part 1, add **hedged requests** (a second request after the p95 latency of the first) to the fan-out case and measure p99 and the extra load.
3. In part 2, add **retries with one retry on failure** (50% of failures transient) and **fallbacks** for the optional dependencies; compute the improved availability both analytically and by simulation. Add **correlated failures** (a shared dependency that fails with probability 0.1% taking down 6 of the 12 services) and see how much of the redundancy benefit disappears.
4. In part 2, model **partial degradation**: the 8 optional services each degrade the page by 1/8 when down. Compute the expected “fraction of page rendered.”
5. In part 3, add a **cache** with 80% hit ratio on item lookups (0.2 ms hit) to the chatty pattern: how close does it get to the batch API? What staleness does it introduce?
6. In part 3, add a **token-bucket rate limit** on the provider service (200 calls/s) and simulate 100 concurrent users each making 50 sequential calls vs 1 batch call: compute the time-to-complete and the rejection rate.

### Lab 2: Decompose a monolith (paper)
BuyMore’s monolith covers: catalog, search, pricing and promotions, cart, checkout, payments, inventory, order management, fulfilment/shipping, customer accounts, reviews, recommendations, notifications, reporting. Using DDD vocabulary: (a) classify subdomains (core/supporting/generic); (b) propose bounded contexts and the initial extraction order (justify with change frequency, scaling needs, coupling); (c) for each extracted service define its data ownership and the events it publishes; (d) identify the three queries that currently join across your boundaries and choose a pattern (API composition, read model, replication) for each; (e) specify which flows need sagas; (f) estimate the synchronous call depth of the product page and design it to stay ≤ 3 hops with fallbacks; (g) describe the team structure.

### Lab 3: Strangler migration plan (paper)
Plan the migration of “reviews” and “recommendations” out of the monolith over 6 months: routing layer, data migration steps (CDC, dual reads, cutover), shadow traffic comparison, rollback plan, SLOs, and how you detect that the new service is not yet ready. Define the exit criteria for decommissioning the old code.

---

## 19.16 Interview perspective

- Start with the **simplest architecture that satisfies the requirements**; justify any split with a concrete force (team scaling, independent scaling, compliance, fault isolation).
- Say **“modular monolith first, extract services along bounded contexts when needed.”**
- Quantify the cost of distribution: **latency chain depth, availability multiplication, chattiness**; propose batching, read models, fallbacks.
- Insist on **database per service** and **no shared tables**; explain how to do cross-service queries (composition, CQRS) and writes (saga/outbox).
- Choose **sync vs async** deliberately: sync for queries on the critical path with timeouts/fallbacks; async events for state changes.
- Mention **API evolution** (backward-compatible, consumer-driven contracts), **gateway/BFF**, **service discovery**, **observability**, **platform team**.
- For migrations, use the **strangler fig** with seams, parallel runs, and data migration steps.
- Tie to **Conway’s law** and team ownership (“you build it, you run it”).

---

## 19.17 Common misconceptions

1. **“Microservices are modern monoliths done right.”** They solve organizational and scaling problems, not code-quality problems.
2. **“Smaller services are better.”** Smaller means more network, more overhead; size by ownership and cohesion.
3. **“We need microservices to scale.”** Monoliths scale horizontally (Ch 6); you split for independent scaling of specific hotspots.
4. **“Each service can use its own technology freely.”** Technology sprawl is a tax; use paved roads.
5. **“A shared database is fine for now.”** It permanently couples every service.
6. **“Services communicate via REST, done.”** Choose sync vs async deliberately; REST chains hurt availability.
7. **“Microservices mean better fault isolation.”** Only with timeouts, bulkheads, and fallbacks; otherwise failures cascade (lab: 98.8% availability).
8. **“We can split by technical layer.”** Layers are not capabilities and create chatty coupling.
9. **“Serverless means no operations.”** Different operations: limits, cold starts, observability, cost.
10. **“Event-driven is always more decoupled.”** Implicit coupling via event schemas and hidden flows can be worse.
11. **“Rewrite to microservices in a big bang.”** Strangle incrementally.
12. **“Conway’s law is trivia.”** Ignore it and your architecture will drift to match your org chart.

---

## 19.18 Chapter summary

- Architecture styles span **monolith → modular monolith → SOA → microservices → serverless**, combinable with **event-driven** and internal **hexagonal** structure. “Microservice” means independently deployable, team-owned, data-owning, capability-aligned.
- **Benefits** are organizational and operational (independent deploy/scale, autonomy, isolation); **costs** are real and measurable: **12 sequential services → p99 4.4× and median 18×**, **12 hard dependencies at 99.9% → 98.8% availability (104 h/yr)**, **fallbacks recover 70 h/yr**, **chatty APIs 393 ms vs 8 ms** with a batch call.
- **Monolith first / modular monolith:** enforce boundaries and data ownership in one deployable; extract services when forces appear (team contention, scaling divergence, isolation, compliance); don’t split without CI/CD, observability, and clear boundaries.
- **Boundaries:** business capabilities/bounded contexts, aggregate-based transactional units, minimal chatty coupling; coupling taxonomy (temporal, deployment, domain, data); the distributed-monolith test.
- **Data:** database per service; cross-service reads via composition, read models, replication; writes via sagas/outbox; one system of record per fact.
- **Communication:** sync for critical-path queries (timeouts, breakers, fallbacks), async events for state changes; API evolution via backward compatibility, versioning, and consumer-driven contracts.
- **Patterns:** gateway, BFF, ACL, strangler fig, branch by abstraction, sidecar/mesh; **EDA** with governance; **serverless** where spiky and event-driven (cost inverts at high steady load).
- **Organization:** Conway’s law, inverse Conway maneuver, Team Topologies, cognitive load, platform teams, ownership, service catalog.
- **Anti-patterns:** distributed monolith, shared DB, chatty/nano-services, long sync chains, big-bang rewrites.

### Mental models
| Concept | Mental model |
|---|---|
| Monolith | One big workshop where everyone shares the tools |
| Modular monolith | One building with soundproof rooms and a front desk for each |
| Microservices | A town of small independent shops that must trade by courier |
| Network cost | Replacing walking across a room with mailing letters |
| Bounded context | A country with its own language and law |
| Distributed monolith | Shops that can only open if all the others are open |
| Strangler fig | Growing a new tree around the old one until the old one can be removed |
| Conway’s law | The building plan mirrors the org chart |
| Platform team | The city that provides roads and power to every shop |

---

## 19.19 Exercises

### A. Conceptual
1. Define monolith, modular monolith, SOA, microservices, and serverless. Give one scenario where each is the best fit.
2. What makes a service “micro”? Give a three-question test for independent deployability.
3. List five benefits and five costs of microservices. Which are organizational and which technical?
4. Use the lab data to explain how latency and availability degrade as sequential call depth grows. Propose four mitigations.
5. What is the N+1 service-call problem? Give three remedies and their trade-offs.
6. Describe the modular monolith and how module boundaries and data ownership are enforced. What is its extraction path?
7. List signals that justify extracting a service and signals that argue against.
8. What are bounded contexts and ubiquitous language? Why are bounded contexts natural service boundaries?
9. Explain the context-mapping relationships: shared kernel, customer–supplier, conformist, anti-corruption layer, open host service.
10. Compare temporal, deployment, domain, and data coupling with examples and remedies.
11. What is a distributed monolith and how do you detect one?
12. Why is a shared database an anti-pattern? What are the alternatives for cross-service queries?
13. Compare API composition, CQRS read models, and event-carried state transfer.
14. When do you choose synchronous vs asynchronous communication? Give examples.
15. How do you evolve an API without breaking consumers? What is a consumer-driven contract?
16. Explain BFF, API gateway, and service mesh and when each is justified.
17. Describe the strangler fig pattern in five steps and how to migrate data.
18. Compare event notification and event-carried state transfer. What are the governance needs for events?
19. When is serverless a good fit and when is it not? Explain the cost-curve inversion.
20. State Conway’s law and the inverse Conway maneuver. What are the four team types in Team Topologies?
21. What belongs in a service catalog? Why “you build it, you run it”?
22. List eight microservice anti-patterns and fixes.

### B. Calculations
23. A page calls 6 services sequentially, each with p99 80 ms and p50 8 ms (assume roughly the lab’s distribution shape). Estimate the end-to-end p50 and p99 using the lab table as a guide. How would parallelizing 4 of the 6 calls change the p50?
24. Each service has 99.95% availability. A request needs 7 services, of which 3 are optional with fallbacks. Compute availability with and without fallbacks, and downtime per month for each.
25. 150 services × 3 replicas; each replica baseline 0.2 vCPU and 250 MB RAM; mesh sidecar adds 0.1 vCPU and 60 MB per replica. Total baseline vCPU and RAM. At $25 per vCPU-month and $4 per GB-month, estimate monthly baseline cost.
26. A page needs 40 items; each item call costs 0.4 ms network + 4 ms service (median). Compare: 40 sequential calls, 8 waves of 5 parallel, and 1 batch call (service 4 ms + 0.04 ms/item). Estimate the medians.
27. Serverless vs containers: 30 M requests/month, 120 ms average, 256 MB. Compute GB-seconds; at $0.0000167/GB-s and $0.20 per million requests estimate the cost. At what monthly request count does it equal $400 (the cost of 4 small always-on containers)?
28. Team sizing: 120 engineers; teams of 8; each team can own at most 3 services. What is the maximum sensible number of services? If you currently have 210, what does it imply?
29. Deployment frequency: monolith with a weekly release train and 10% failure rate with 4-hour rollback; vs 20 independent services each deploying 3 times a week with 2% failure and 10-minute rollback. Compare expected weekly downtime-minutes from deploy failures (assume each failure causes full outage for the rollback time on the affected unit, weighting the monolith as 100% of users and each service as 5% of users).
30. A service has 8 consumers; a breaking change is needed. Each consumer team needs 2 weeks to migrate; teams start sequentially in the worst case. What is the minimum deprecation window with parallel migration? With a compatibility shim (v1 adapter) what changes?

### C. Design
31. Decompose BuyMore (Lab 2): choose the first three services to extract and justify with forces; define the APIs/events.
32. Design the product page for ≤ 3 sync hops: aggregation, read models, fallbacks, caching, timeouts.
33. Design the data migration for extracting “reviews” from a monolith database with a foreign key to products and users.
34. Design an event catalog and governance process for 40 services (naming, versioning, compatibility, ownership, discovery).
35. Propose an organization (teams, platform, enabling) for 90 engineers building a ride-hailing platform (riders, drivers, trips, pricing, payments, maps, support).
36. Design a modular monolith for a 30-engineer startup: modules, boundaries, enforcement, data ownership, and extraction triggers.

### D. Debugging
37. After splitting a service, p99 latency of the main page tripled. Describe how you would diagnose (traces) and the likely fixes.
38. Services must be deployed in a fixed order each release. Diagnose the coupling and fix.
39. Two services disagree about order status. List causes and fixes (ownership, events lost, idempotency, reconciliation).
40. A change to a shared “common-models” library requires redeploying 60 services. What went wrong and what are better patterns?
41. A new service has no owner and pages go to a team that doesn’t know it. What organizational mechanism is missing?
42. A migration to serverless raised the monthly bill by 8×. Possible causes and checks.

### E. Interview-style
43. “Would you start this new product with microservices?” (Give a nuanced answer.)
44. “How would you break up a monolith?”
45. “How do services share data without sharing a database?”

### F. Teach-back
46. Explain why splitting a system into services can make it slower and less available, using a postal-system analogy.

---

## 19.20 Full answer key

**1.** Monolith: single deployable (small team MVP). Modular monolith: single deployable with enforced modules (30–200 engineers, evolving domain). SOA: coarse shared services/ESB (enterprise integration). Microservices: many small independently deployable services (large org, divergent scaling). Serverless: event-driven functions (spiky glue, low-traffic APIs).

**2.** Micro = independently deployable, owned by one team, owns its data, cohesive capability. Test: Can I deploy it alone, any time, with no coordination? Can it run (even degraded) when other services are down? Can its data schema change without asking another team?

**3.** Benefits: independent deploy (organizational/technical), team autonomy (org), independent scaling (tech), fault isolation (tech, if designed), tech flexibility (tech/org). Costs: network latency & partial failure (tech), distributed data/consistency (tech), operational overhead (ops), testing/debugging complexity (tech), cognitive load/governance (org).

**4.** Latency: 12 sequential services → p50 189 ms (18×) and p99 440 ms (4.4×) vs one service; availability: twelve 99.9% hard dependencies → 98.8%. Mitigations: shorten chains, parallelize, cache/replicate data, async for writes, timeouts + fallbacks for optional dependencies, aggregate APIs.

**5.** N+1: one request triggers N calls to another service (393 ms for 50 sequential vs 8 ms batch). Remedies: batch endpoints (fewer calls, larger payloads); read models/replication (fast reads, staleness); aggregation layer/GraphQL (shifts complexity); caching (staleness); merge chatty services.

**6.** A single deployable with modules that expose interfaces only; boundaries enforced via visibility rules, build modules, architecture tests; each module owns its schema/tables; communication via interfaces/in-process events. Extraction: replace the in-process interface with a network API and move its data and code to a separate deployable.

**7.** For: team/release contention, divergent scaling or hardware needs, isolation/compliance, different tech/lifecycle needs, ownership readiness. Against: small team, unclear boundaries, no CI/CD/observability, mostly code-quality problems, atomic transactions across the cut.

**8.** A bounded context is an explicit boundary within which a model and language are consistent. They are natural service boundaries because each has coherent concepts, its own data, and minimal need to coordinate with others.

**9.** Shared kernel: shared model/code (tight). Customer–supplier: upstream/downstream with negotiated contract. Conformist: downstream adopts upstream model. ACL: translation layer isolating your model. Open host service: published, stable interface/language for many consumers.

**10.** Temporal: needs callee now (sync chains) → async/caches. Deployment: must deploy together → compatibility, contracts. Domain: depends on internal model → published language/ACL. Data: reads another’s tables → own data/events. (Platform coupling: forced tech → paved roads.)

**11.** Services that cannot be deployed or run independently (lock-step releases, shared DB, shared models, outage of one breaks all). Detect via deployment dependencies, incident analysis, dependency graphs, change coupling in VCS.

**12.** Shared tables couple schemas and prevent independent deploys/ownership. Alternatives: API composition, CQRS read models fed by events, event-carried state, replicated reference data, search/warehouse feeds.

**13.** API composition: call and join at request time (fresh, slower, no consistency). Read model: pre-joined local view (fast, eventual). Event-carried state: events carry data enabling local copies (fewer calls, stale risk).

**14.** Sync for queries on the user’s critical path and commands needing immediate results (with timeouts/fallbacks); async for state changes, integration, long-running work, fan-out. Example: product page query sync; “OrderPlaced” event async.

**15.** Add optional fields, don’t change meaning, tolerant readers, version breaking changes and run in parallel with deprecation windows. Consumer-driven contract: consumers publish their expectations; providers verify them in CI so breaking changes are detected before release.

**16.** API gateway: external entry (auth, limits, routing). BFF: per-client aggregation layer reducing chattiness. Service mesh: sidecars for mTLS, retries, observability service-to-service. Justify mesh with many services/polyglot/security needs; BFF with diverse clients; gateway for any public API.

**17.** (1) Add routing layer; (2) build new component for a seam; (3) route a slice with flags and shadow compare; (4) migrate data via CDC/dual-write, switch system of record; (5) remove old code; repeat. Data: access through interface, separate schema, replicate, switch reads then writes, drop old tables.

**18.** Notification: thin event + callback; carried state: full data in event. Governance: schemas in a registry with compatibility checks, naming, ownership, versioning, catalog, deprecation, PII handling.

**19.** Good for spiky, event-driven, low/variable traffic, glue. Bad for steady high throughput, low-latency tail, long-running or stateful jobs. Cost inversion: per-request pricing exceeds always-on cost at high sustained load (e.g., ~$190 at 100 M requests vs ~$9,400 at 5 B).

**20.** Conway: system structure mirrors communication structure of the organization. Inverse Conway: organize teams to produce the desired architecture. Team types: stream-aligned, platform, enabling, complicated-subsystem.

**21.** Owner, on-call, tier/SLOs, dependencies, runbooks, repos, docs. “You build it, you run it” aligns incentives: the builders feel operational pain and fix reliability.

**22.** Distributed monolith (re-draw/own data), shared DB (database per service), chatty services (batch/read models), long sync chains (async/fallbacks), nano-services (consolidate), god service (split by capability), big-bang rewrite (strangler), no platform (golden paths), versioning hell (contracts), orphan services (catalog/ownership).

**23.** Scale the lab table (per-call median 10.5 ms, p99 99 ms) by ≈ 0.8 to match 8 ms / 80 ms. For six sequential calls, interpolate between k = 5 (p50 73, p99 253) and k = 8 (p50 122, p99 337): about p50 ≈ 90 ms and p99 ≈ 280 ms before scaling, so **p50 ≈ 70 ms, p99 ≈ 225 ms** after scaling. Parallelizing four of the six: the max of four parallel calls has a median of about 28 ms (lab, f = 5 gives 31 ms), scaled ≈ 22 ms, plus two sequential calls at ≈ 8 ms each, so p50 ≈ **38 ms**, about half of the sequential 70 ms.

**24.** All 7 hard: 0.9995⁷ = 0.99650 → 99.65% → 0.35% × 43,200 = **151 min/month**. 4 hard + 3 optional with fallback: 0.9995⁴ = 0.99800 → 99.80% → **86 min/month**. Without fallbacks (treated as hard) = 151 min.

**25.** Replicas = 450. vCPU = 450 × (0.2 + 0.1) = **135 vCPU**; RAM = 450 × (250 + 60) MB = 139,500 MB ≈ **139.5 GB**. Cost ≈ 135 × $25 = $3,375 + 139.5 × $4 = $558 → **≈ $3,930/month** baseline (before any real work).

**26.** Sequential: 40 × (0.4 + 4) ≈ **176 ms**. Waves of 5 in parallel: the median of the maximum of 5 lognormal calls is about 2.8× the single-call median for this tail shape (the lab’s fan-out of 5 gave ≈ 3×), so each wave ≈ 0.4 + 4 × 2.8 ≈ 11.6 ms and 8 waves ≈ **93 ms**. One batch call: 0.4 + 4 + 0.04 × 40 = **6 ms**. Batching beats parallelism because it removes calls instead of overlapping them.

**27.** GB-seconds = 30 M × 0.12 s × 0.25 GB = **900,000 GB-s** → ×$0.0000167 = $15.03; requests: 30 × $0.20 = $6 → **≈ $21/month**. Equals $400: cost per M requests ≈ ($15.03+$6)/30 = $0.70 per million → 400/0.70 ≈ **570 M requests/month**.

**28.** 120/8 = 15 teams × 3 = **45 services** maximum sensible. 210 services means ~14 per team: unsustainable cognitive and operational load; indicates nano-services/ownership gaps → consolidate.

**29.** Monolith: 1 release/week × 10% failure × 240 min × 100% users = **24 user-weighted outage minutes/week** (full-impact minutes). Services: 60 deploys/week × 2% = 1.2 failures × 10 min × 5% = **0.6 weighted minutes/week**. About 40× less deploy-induced downtime (plus faster feature flow), assuming real independence.

**30.** Worst-case sequential: 8 × 2 = 16 weeks. In parallel: 2 weeks plus coordination (≥ 4–6 weeks including communication and support). With a compatibility shim/v1 adapter, consumers migrate at their own pace; the provider maintains the adapter for a defined sunset period, removing the need for a hard coordinated deadline.

**31 (sketch).** Extract (1) search (different scaling/tech; read-only, fed by events), (2) notifications (generic, async, simple), (3) payments (compliance scope and distinct lifecycle). Keep catalog/pricing/checkout/order together initially. Events: `ProductUpdated`, `OrderPlaced`, `PaymentCaptured`; APIs: `search?q=`, `payments/authorize`.

**32 (sketch).** Gateway/BFF composes: product (critical), price (critical), inventory (cached/read model), reviews summary (optional, cached, fallback hidden), recommendations (optional, async-loaded after first paint). Max 3 sync hops: gateway → product/price in parallel → their datastores; optional calls with 100–200 ms timeouts and stale caches.

**33 (sketch).** Make the monolith access reviews through an interface; create a `reviews` schema/service; replace FKs with IDs (`product_id`, `user_id`) validated via events/replicas; CDC from the old table to the new store; dual-read compare; switch writes; drop old table; handle reporting joins in the warehouse.

**34 (sketch).** Registry of schemas (Avro/Protobuf) with compatibility checks in CI; naming `<domain>.<entity>.<event>.v1`; ownership metadata; AsyncAPI docs and catalog; deprecation policy (6 months); PII classification; consumer registration; replay tooling; review board lightweight.

**35 (sketch).** Stream-aligned teams: Riders, Drivers, Trips & Dispatch, Pricing, Payments; platform team (infra, CI/CD, observability, data); enabling team (SRE/security); complicated-subsystem team (maps/routing ML). ~8 teams for 90 engineers, each owning ≤ 3 services.

**36 (sketch).** Modules: Identity, Catalog, Orders, Payments, Notifications; interfaces + events between modules; separate schemas; ArchUnit/Gradle boundaries; trunk-based CI under 10 min; extraction triggers: payments compliance, image processing scaling, team >50.

**37.** Use distributed traces to find the new sequential calls, N+1 patterns, and slow hops; fixes: parallelize, batch, cache/read model, merge services, move to async, tighten timeouts and fallbacks.

**38.** Deployment-order coupling due to breaking API/schema changes; fix with backward-compatible changes (expand/contract), feature flags, consumer-driven contracts, and versioned APIs.

**39.** Causes: no single owner of order status, lost/duplicated events, non-idempotent consumers, out-of-order events, missing reconciliation. Fixes: define the system of record, outbox + idempotent consumers, versioned events, reconciliation jobs.

**40.** A shared model library creates deployment and domain coupling. Better: each service owns its model; share only stable schemas via contracts (protobuf/OpenAPI) and generate clients; keep shared libraries tiny, backward compatible, and optional to upgrade.

**41.** Ownership mechanism/service catalog and on-call routing; policies that every service has an owner, tier, runbook; automated checks in CI; sunset process for unowned services.

**42.** Causes: high steady traffic making per-invocation pricing expensive; large memory sizes or long durations; chatty function-to-function calls; retries/duplicates; logging/data transfer charges; provisioned concurrency. Check cost breakdown, invocation counts, durations, memory, retries; consider containers for steady load.

**43 (sketch).** Probably not: start with a modular monolith; split along bounded contexts when team size, scaling, isolation, or compliance forces appear; ensure CI/CD, observability, and platform maturity first.

**44 (sketch).** Identify bounded contexts and seams, add a routing layer, extract low-risk capabilities first via the strangler fig, migrate data with CDC and dual reads, keep an ACL, measure, and decommission old code; align teams.

**45 (sketch).** Via APIs, events, and replicated read models: owner publishes events/CDC; consumers keep local copies; sync queries for fresh needs; sagas for writes; one system of record per fact.

**46.** Replace walking across a room with mailing letters between offices. Each letter takes time (latency) and may get lost; if a task needs twelve letters in a row, it’s slow, and if any office is closed the task can’t finish. You gain independence (each office can reorganize itself) but must design the paperwork carefully.

---

## 19.21 Flashcards

| Front | Back |
|---|---|
| Microservice | Independently deployable, team-owned, data-owning, capability-aligned |
| Modular monolith | One deployable with enforced module boundaries and data ownership |
| Monolith first | Learn boundaries before paying the distribution tax |
| Sequential chain cost | 12 services: p50 18×, p99 4.4× (lab) |
| Availability cost | 12 hard deps @99.9% → 98.8% (104 h/yr); fallbacks → 99.6% |
| Chatty vs batch | 393 ms vs 8 ms for 50 items (lab) |
| Extract when | Team contention, scaling divergence, isolation, compliance, ownership readiness |
| Bounded context | Consistent model/language boundary; natural service boundary |
| Aggregate | Consistency unit; transactions stay inside |
| Coupling types | Temporal, deployment, domain, data |
| Distributed monolith | Lock-step deploys; shared DB; any outage breaks all |
| Database per service | Sole writer; others via API/events |
| Cross-service reads | Composition, read models, event-carried state, replication |
| Cross-service writes | Saga + outbox + idempotency |
| Sync vs async | Sync for critical-path queries; async for state changes |
| API evolution | Backward compatible, tolerant reader, versioning, consumer-driven contracts |
| BFF | Per-client aggregation layer |
| Strangler fig | Route slices from legacy to new until legacy retires |
| Serverless | Pay per use; cold starts; cost inverts at high steady load |
| Conway’s law | Systems mirror org communication structure |
| Team Topologies | Stream-aligned, platform, enabling, complicated-subsystem |
| Service catalog | Owner, on-call, SLOs, dependencies, runbooks |

## 19.22 Glossary additions

| Term | Simple Meaning | Technical Meaning | Why It Exists | Example |
|---|---|---|---|---|
| Monolith | One big deployable | Single codebase/deployment unit | Simplicity | Rails app |
| Modular monolith | Boxes inside one app | Enforced module boundaries in one deployable | Cheap modularity | Gradle modules |
| Microservice | Small independent service | Independently deployable capability-owning service | Autonomy | Orders service |
| SOA | Shared enterprise services | Coarse services with ESB/standards | Integration | SOAP services |
| Distributed monolith | Coupled services | Services that must change/deploy together | Anti-pattern | Lock-step releases |
| Bounded context | Model boundary | DDD boundary with consistent language | Service boundary | Billing vs Support |
| Ubiquitous language | Shared vocabulary | Terms consistent within a context | Clarity | “Order” |
| Aggregate | Consistency cluster | Root + entities treated as unit | Transaction boundary | Order + lines |
| Anti-corruption layer | Translation shield | Adapter between models | Protect your model | Legacy adapter |
| Conformist | Accept upstream model | Downstream adopts upstream | Pragmatism | SaaS API |
| Database per service | Own your data | Service is the sole data owner | Independence | Orders DB |
| API composition | Join in a layer | Aggregator calls services | Cross-service reads | BFF |
| Event-carried state transfer | Events with data | Payload carries state for local copies | Fewer calls | `CustomerUpdated` |
| Consumer-driven contract | Consumers define tests | Provider verifies consumer expectations | Safe evolution | Pact |
| BFF | Client-specific backend | Gateway per client type | Reduce chattiness | Mobile BFF |
| Strangler fig | Incremental replacement | Route-by-route migration | Safe migration | Proxy + flags |
| Branch by abstraction | Swap behind an interface | Introduce abstraction then replace | In-code migration | Repository interface |
| Serverless / FaaS | Functions on demand | Event-triggered managed functions | No server ops | Lambda |
| Cold start | Spin-up delay | Latency creating a new function instance | FaaS trade-off | 400 ms |
| Conway’s law | Org shapes system | Design mirrors communication structure | Align teams | — |
| Inverse Conway | Org to match design | Structure teams to get target architecture | Intentional design | Team per context |
| Team Topologies | Team types | Stream-aligned, platform, enabling, subsystem | Reduce cognitive load | Platform team |
| Golden path | Easy default | Supported way to build/run services | Productivity | Service template |
| Service catalog | Inventory | Registry of services and owners | Governance | Backstage |
| ADR | Decision record | Documented architectural decision | Memory | `adr-012` |

---

**Next: Chapter 20: Probabilistic Data Structures, Clocks Revisited, CRDTs, and Gossip.** Bloom filters, Count-Min sketches, and HyperLogLog (measured against theory), CRDTs that converge without coordination (measured against naive replicated types), gossip dissemination (rounds vs fanout, loss, failures), and Merkle-tree anti-entropy.

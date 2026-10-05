# Chapter 7: Load Balancers, Proxies, and Gateways

## Chapter card

| | |
|---|---|
| **Objective** | Understand how traffic is distributed across servers and regions: why load balancers exist, how L4 and L7 balancing differ, how algorithms behave under realistic conditions (with simulation evidence), how health checks and failover work, how the balancer itself is made highly available, and what proxies, API gateways, service meshes, and service discovery add |
| **Why this chapter exists** | Horizontal scaling (Ch 6) creates a new question: *who decides which server gets each request, and what happens when one fails?* The answer shapes latency, availability, deployment safety, and security |
| **Prerequisites** | Chapters 1, 2, 5, 6 (TCP/TLS/HTTP, stateless services, queueing and tail latency) |
| **Study time** | 9–12 hours including labs |
| **Tier** | LB fundamentals `[MK]`; gateways/proxies `[MK]`; mesh/GSLB internals `[GK]` |
| **Mastery criteria** | (1) Explain what an LB does and where LBs appear in an architecture. (2) Compare L4 vs L7 balancing, TLS termination modes, and why gRPC/HTTP-2 needs L7 or client-side balancing. (3) Choose and justify a balancing algorithm; explain why *power of two choices* works. (4) Design health checks (active/passive, liveness/readiness, thresholds, slow start, draining) without causing cascades. (5) Make the LB tier highly available. (6) Explain global traffic management (DNS, anycast). (7) Distinguish reverse proxy, API gateway, service mesh, and client-side balancing, and say when each is worth it. (8) Reason about retries, timeouts, and overload behavior at the proxy |

---

## 7.1 The Story: Three Servers, No Director

TaskBoard now has three identical, stateless app servers (Chapter 6). You hand out their addresses to clients. Then:

1. A mobile client caches server #2's IP. Server #2 is replaced during a deploy; the client's requests fail until the app is updated. (Clients know too much.)
2. Clients on corporate networks all pick the first address in the list, so server #1 gets 70% of the traffic. (Uneven load.)
3. Server #3's disk fills; it begins returning errors. Clients keep trying it. (No failure detection.)
4. Someone adds TLS certificates to each server by hand; one expires on a Friday night. (Operational sprawl.)
5. A new server is added but nobody tells the clients. (No dynamic membership.)
6. During a slow day on server #2 (a noisy neighbor makes it twice as slow), its users have a terrible experience while the others idle. (No load awareness.)

### Pause & Predict 7.1
> Design, in words, the smallest thing you could put *between* clients and servers that fixes all six problems. What must it know? What must it do on every request? What new problems does it introduce? Write your answer, then compare with §7.2 and §7.9.

---

## 7.2 What a load balancer is and why it exists

### 7.2.1 Definition `[MK]`
A **load balancer (LB)** is a component that receives requests (or connections) on behalf of a **pool** of backend servers and forwards each to a chosen backend, while monitoring backend health and hiding the pool's membership from clients.

```text
              ┌─────────────────────┐
 Clients ───▶ │  Load balancer      │   one stable name/address (VIP/DNS)
              │  (decide, forward)  │
              └─────────┬───────────┘
        ┌───────────────┼───────────────┐
        ▼               ▼               ▼
    Server A        Server B        Server C        ← pool; membership changes freely
```
**Mental model:** a receptionist who knows which doctors are in, which are busy, and which are ill; patients just say "I'm here to see a doctor."

### 7.2.2 What it solves (mapping to the six problems)
| Problem | LB capability |
|---|---|
| Clients know server addresses | **Abstraction:** one stable endpoint; backends can change |
| Uneven distribution | **Distribution algorithms** |
| Dead servers still receive traffic | **Health checks** and automatic removal |
| TLS on every server | **TLS termination**, one place for certificates |
| New servers invisible | **Dynamic membership** (registration, autoscaling integration) |
| Load/speed differences between servers | **Load-aware algorithms** (least connections, latency-aware) |
| (bonus) Deployments | **Connection draining**, canary/blue-green traffic splitting |
| (bonus) Protection | Rate limiting, DDoS absorption, WAF, request buffering |

### 7.2.3 What it costs (the new problems)
- **Extra hop:** adds ~0.1–1 ms (more if poorly placed or doing heavy L7 work).
- **A new single point of failure** unless made redundant (§7.8).
- **A new bottleneck:** it must handle *all* traffic (bandwidth, connections, TLS handshakes).
- **A new failure surface:** misconfiguration (timeouts, health checks) can take down a healthy fleet.
- **Operational complexity:** configs, certificates, capacity, observability.

---

## 7.3 Where load balancers appear `[MK]`

Large systems have several balancing points, not one:

```text
 Users
   │  DNS / anycast  (global traffic management: pick a region)         ← §7.9
   ▼
 CDN / edge proxies  (TLS, caching, DDoS absorption)
   ▼
 Regional L4/L7 LB   (public entry: spread across AZs)                  ← §7.4
   ▼
 API gateway         (auth, rate limits, routing to services)           ← §7.11
   ▼
 Internal LBs / service mesh sidecars  (service → service)              ← §7.12
   ▼
 DB proxies (pgBouncer, ProxySQL), cache proxies (twemproxy, mcrouter)  ← read/write splitting, pooling
```
Each layer uses the technique suited to its job: DNS for coarse global steering, L4 for raw speed, L7 for content-aware routing, client-side or mesh for service-to-service.

### Form factors
| Form | Examples | Notes |
|---|---|---|
| **Hardware appliance** | F5 BIG-IP, Citrix ADC | High performance, expensive, on-prem |
| **Software on commodity servers** | NGINX, HAProxy, Envoy, Traefik | Flexible, scriptable; you operate it |
| **Cloud-managed** | AWS ALB/NLB, GCP Cloud Load Balancing, Azure LB/App Gateway | Elastic, multi-AZ by design; limited tuning; per-request/LCU billing |
| **Kernel/network-layer** | LVS/IPVS, Maglev-style software LBs, eBPF (Katran, Cilium) | Extremely high packet rates; L4 `[Public: Google Maglev paper, Meta Katran]` |
| **Client-side library** | gRPC client LB, Finagle, Ribbon-style | No extra hop; clients need discovery data |
| **Sidecar proxy (mesh)** | Envoy via Istio/Linkerd | Per-service-instance proxies |

---

## 7.4 Layer 4 vs Layer 7 `[MK]`

### 7.4.1 The idea
Recall Chapter 1's layers. An LB can make its decision using only **transport** information (IP addresses, ports: **L4**) or by **understanding the application protocol** (HTTP paths, headers, cookies, gRPC methods: **L7**).

### 7.4.2 How L4 works
- Sees TCP/UDP flows. When a new connection arrives, picks a backend and forwards **packets** for that connection (via NAT, or Direct Server Return, DSR, where backend replies directly to the client).
- A **flow table** (connection tracking) keeps all packets of one connection on the same backend. Consistent hashing of the 5-tuple can make this survive LB failover.
- Doesn't terminate TLS (passes encrypted bytes through) or parse HTTP.
- Extremely fast and cheap: millions of connections, tens of Gbit/s per node; low latency overhead (microseconds).

### 7.4.3 How L7 works
- **Terminates** the client's TCP (and usually TLS) connection, parses HTTP, makes a decision *per request*, and opens or reuses a **separate connection to the backend**.
- It's a **reverse proxy** with balancing.
- Can: route by host/path/header/cookie/method; rewrite headers; compress; cache; rate limit; authenticate; retry on another backend; split traffic (canary); inject tracing IDs; enforce timeouts; buffer slow clients.
- Costs more CPU (TLS handshakes, parsing), more memory per connection, slightly more latency.

### 7.4.4 Comparison
| | L4 | L7 |
|---|---|---|
| Decision basis | IP/port (5-tuple), connection | URL, headers, cookies, gRPC method, body (rare) |
| Unit balanced | **Connection** | **Request** |
| TLS | Pass-through (or terminate in some products) | Usually terminate (and optionally re-encrypt) |
| Throughput | Very high | High, but lower per node |
| Latency overhead | Tiny | Small–moderate |
| Features | Basic | Routing, retries, caching, auth, WAF, canary |
| Backend connection reuse | No (client connection ↔ backend connection 1:1) | **Yes**: pooling/multiplexing to backends |
| Visibility | Little (bytes, connections) | Rich HTTP metrics (status codes, latency per route) |
| Examples | AWS NLB, IPVS, Maglev | AWS ALB, NGINX, HAProxy (http mode), Envoy |
| Typical use | Front door for non-HTTP (TCP/UDP/DB/MQTT), extreme throughput, fronting L7 tier | Web/API traffic, microservice routing |

A common pattern: **L4 at the edge fanning out to a fleet of L7 proxies**, because L4 scales to enormous connection counts and the L7 fleet scales out for the heavier processing.

### 7.4.5 TLS handling modes `[MK]`
| Mode | Flow | Pros | Cons |
|---|---|---|---|
| **TLS termination** at LB | Client⇄LB encrypted; LB→backend plaintext | Offloads crypto; LB can inspect HTTP; central certs | Internal traffic unencrypted (not zero-trust) |
| **Re-encryption** (TLS bridging) | Client⇄LB TLS; LB⇄backend TLS (often mTLS) | Inspect + encrypted end to end internally | Double crypto cost |
| **Pass-through** (L4) | LB forwards encrypted bytes | End-to-end encryption; no certs on LB | No L7 features; SNI-based routing only |
Use termination + re-encryption for most modern zero-trust setups; pass-through when the backend must see the client certificate or for non-HTTP protocols.

### 7.4.6 HTTP/2 and gRPC: the "long-lived connection" trap `[MK]`
HTTP/2 and gRPC multiplex *many requests over one long-lived TCP connection*. An **L4** balancer picks a backend **once per connection**. Suppose 10 client instances each open one gRPC connection to a 5-backend service through an L4 LB; each client's connection (and thus all of its traffic) sticks to one backend. Result: severe imbalance; and after you scale out the backends, **existing connections never move** to the new ones.

**Fixes**
1. Use an **L7 LB** that understands HTTP/2 and balances **per request** (Envoy, ALB for gRPC, NGINX).
2. **Client-side load balancing**: the gRPC client resolves all backend addresses and balances per call.
3. Periodically **recycle connections** (max connection age / graceful GOAWAY) so clients re-balance.
This pitfall is common in real incidents: "we scaled to 20 pods but 3 of them get all the traffic."

### 7.4.7 Source IP preservation
When an L7 proxy terminates connections, the backend sees the proxy's IP. Preserve the client address with `X-Forwarded-For` / `Forwarded` headers or the **PROXY protocol**. **Only trust these headers when set by your own proxy** (they can be forged by clients, which matters for rate limiting and logging).

---

## 7.5 Balancing algorithms `[MK]`

### 7.5.1 The catalog
| Algorithm | Rule | Strengths | Weaknesses |
|---|---|---|---|
| **Round robin** | A, B, C, A, B, C… | Simple, fair for equal requests/servers | Ignores load & request-size variance |
| **Weighted round robin** | Servers get shares proportional to weights | Heterogeneous hardware | Static weights |
| **Random** | Pick uniformly | Stateless, simple, scales across many LBs | Imbalance by chance |
| **Least connections** | Fewest active connections/requests | Adapts to slow servers and long requests | Needs state; many LBs → stale counts |
| **Weighted least connections** | Connections ÷ weight | Heterogeneous + adaptive | Same |
| **Least response time / EWMA** | Lowest recent latency × outstanding | Latency-aware | Needs measurement; oscillation risk |
| **IP hash / source hash** | `hash(client IP) mod N` | Simple stickiness | Reshuffles when N changes; NAT clumping |
| **Consistent hashing** | Ring; key→ nearest node | Stable assignment when nodes change; cache locality | Can be uneven (fix: virtual nodes, bounded loads) |
| **Power of two choices (P2C)** | Pick 2 random backends; send to the less loaded | Near-optimal balance, minimal state, robust | Needs load signal |
| **Maglev hashing** | Lookup table consistent hashing | Even + minimal disruption | Table rebuild cost |

### 7.5.2 Why "just round robin" often fails: simulation evidence (Lab 1)
Realistic traffic has **high variance in request cost** (mostly 5 ms, sometimes 50+ ms) and real fleets have **slow nodes** ("gray failures"). The simulation in §7.15 uses 16 backends at 80% utilization, with 90% of requests averaging 5 ms and 10% averaging 50 ms.

**All backends healthy:**
| Algorithm | p50 | p99 | p99.9 |
|---|---|---|---|
| Random | 63 ms | 654 ms | 917 ms |
| Round robin | 50 ms | 629 ms | 1,008 ms |
| **Least connections** | **5.3 ms** | **150 ms** | **270 ms** |
| **Power of two choices** | 14 ms | 247 ms | 391 ms |

**One backend is 2× slower (gray failure):**
| Algorithm | p50 | p99 | p99.9 | Load share on slow node |
|---|---|---|---|---|
| Random | 74 ms | **145,585 ms** | 171,513 ms | 6.2% |
| Round robin | 61 ms | **142,503 ms** | 162,333 ms | 6.3% |
| **Least connections** | 5.9 ms | **166 ms** | 313 ms | **3.8%** |
| **Power of two choices** | 17.5 ms | **288 ms** | 515 ms | **3.6%** |

**Read this carefully:**
1. Even when everything is healthy, *load-agnostic* algorithms (random, round robin) give **4× worse p99** than load-aware ones, because they send requests to servers that are already busy with a slow request (head-of-line blocking behind a 50 ms job).
2. With one 2× slower node at 80% overall utilization, that node's *own* utilization is 160% under round robin/random (it receives an equal 6.25% share but serves at half speed). Its queue grows without bound (the simulation's 145-second p99 is just how long the run lasted; it keeps growing) and **6% of all requests suffer unbounded delay** (a single sick node poisoning the tail).
3. Load-aware algorithms automatically **shift traffic away** from the slow node (3.6–3.8% instead of 6.25%) with no explicit health check at all: they detect "slow" through queue length.
This is exactly the queueing knee from Chapter 5, hitting one backend.

### 7.5.3 Why power of two choices works `[GK]`
Throw *n* balls (requests) into *n* bins (servers):
- **Uniform random:** the fullest bin has about `ln n / ln ln n` balls: noticeably unbalanced.
- **Pick 2 random bins, put the ball in the emptier one:** the fullest bin has only about `ln ln n / ln 2` balls: an *exponential* improvement for a trivial amount of extra work. (Azar–Broder–Karlin–Upfal; Mitzenmacher "The Power of Two Choices in Randomized Load Balancing".)
For n = 1,000,000: random ≈ 8–10 in the fullest bin vs P2C ≈ 4. Picking 3 or more choices gives only marginal further gain.
**Why it's valuable in practice:** (a) no global view needed (only two load readings), so *many independent LBs* can use it without coordination; (b) resistant to stale information (herd behavior: "least connections" with stale counts can send a burst to the same "least loaded" server); (c) cheap. Used in Envoy, NGINX (`random two`), and many service meshes `[Public]`.

### 7.5.4 The herd effect with "least connections"
With multiple LBs each choosing the globally least-loaded backend using slightly stale information, they *all* pick the same one simultaneously → it's now overloaded. P2C avoids this by randomization.

### 7.5.5 Consistent hashing for locality and stickiness `[MK]`
Use when the *same key should go to the same backend*: caches (so each cache holds a partition, not N copies), session affinity, per-tenant isolation, stateful processors, WebSocket rooms.
- Naive `hash(key) mod N` reshuffles ~(N−1)/N of keys when N changes.
- **Consistent hashing** (Ch 10) moves only ~1/N of keys.
- **Bounded-load consistent hashing** (Google, Mirrokni et al.): cap each node's load at (1+ε)× the average, spilling the overflow to the next node; keeps locality while preventing hotspots `[Public]`.

### 7.5.6 Choosing `[MK]`
- Homogeneous, uniform small requests, stateless, low stakes → **round robin/random** is fine.
- Variable request cost, mixed hardware, risk of slow nodes → **least connections / P2C / EWMA**.
- Many LB instances → **P2C** (or random with outlier ejection).
- Need locality/stickiness → **consistent hashing (with bounded loads)**.
- Long-lived connections (WebSocket, gRPC) → balance at **connection setup with load awareness** and consider max connection age.
- Always combine with **health checks + outlier detection** (algorithms alone don't handle dead nodes).

---

## 7.6 Health checks, failure detection, and safe rollout `[MK]`

### 7.6.1 Why
An LB that keeps sending traffic to a dead or sick server converts a partial failure into user-visible errors. It needs to know who's healthy.

### 7.6.2 Active health checks
The LB probes each backend periodically (e.g., `GET /health` every 2–5 s with a 1–2 s timeout).
- **Thresholds** prevent flapping: *unhealthy after N consecutive failures (e.g., 2–3); healthy after M consecutive successes (e.g., 2–5).*
- **Probe types:** TCP connect (is something listening?), HTTP status (does the app respond?), application-specific (can it serve a real request?).
- **Cost:** with 1,000 backends × several LBs × a probe every 2 s, probe traffic is non-trivial; use jitter, and consider centralized health checkers publishing results.

### 7.6.3 Passive health checks / outlier detection
Observe *real* traffic: eject a backend after consecutive 5xx errors, connection failures, or latency anomalies; re-admit after an ejection period (exponentially increasing). Advantages: reacts instantly (no waiting for the next probe), catches "passes health check but fails real requests" cases. Envoy calls this **outlier detection**. In Lab 2, you see this in action: the first request sent to a just-killed backend fails with `ConnectException`, is retried elsewhere, and the user sees no error; the health check then removes it.

### 7.6.4 What should `/health` check? `[MK]`
| Check design | Benefit | Danger |
|---|---|---|
| **Shallow** ("process is up, can respond") | Never causes cascades; fast | Doesn't catch broken dependencies |
| **Deep** (checks DB, cache, downstreams) | Catches real brokenness | **Shared dependency blip fails every instance's check → LB removes *all* backends → total outage that the DB blip alone wouldn't have caused** |

**Guidance**
- Separate **liveness** (restart me?), **readiness** (route to me?), and **dependency health** (exposed for monitoring, not necessarily gating traffic).
- Gate traffic on **local** readiness (initialized, not shutting down, not overloaded). If a *critical* dependency is down, decide per dependency whether failing readiness helps (it usually doesn't when all instances share that dependency: removing them doesn't fix the DB).
- Add **panic mode / fail-open**: if more than X% (e.g., 50%) of backends are unhealthy, ignore health status and send to all, since a mass failure is more likely a bad check than 80% of servers dying simultaneously (Envoy "panic threshold").
- Health check endpoints should be **cheap, unauthenticated-internal, rate-limit-exempt**, and not hit the DB on every probe.

### 7.6.5 Slow start, warm-up, and connection draining
- **Slow start:** newly healthy backends receive a gradually increasing share (e.g., 30–120 s) so cold JIT/caches/pools don't cause latency spikes or failures (Ch 6).
- **Connection draining (deregistration delay):** when removing a backend (deploy/scale-in), stop sending *new* requests but let in-flight ones finish up to a timeout (30–300 s depending on request length). Pair with the app's graceful shutdown (Ch 6).
- **Deploy sequencing:** register new → wait until healthy and warmed → then drain old.

### 7.6.6 Failure scenarios
| Failure | LB behavior (well-configured) |
|---|---|
| Backend process crashes | New connections refused → passive ejection + active health check removes it; retries on idempotent requests hide most errors |
| Backend hangs (accepts connection, never answers) | Request timeout fires; retry elsewhere; outlier detection ejects; deep timeouts matter |
| Backend slow (gray failure) | Load-aware algorithm shifts traffic; latency-based outlier detection; avoid round robin |
| Partial dependency failure (some endpoints fail) | Per-route circuit breaking; health check doesn't catch it, so use passive checks and SLO alerts |
| Network partition between LB and a subset of backends | Health checks fail for that subset; ensure failover capacity |
| All backends "unhealthy" | Panic mode (fail open) rather than serving 503 to everyone |
| Bad deploy | Canary + automated rollback on error-rate increase |

---

## 7.7 Session affinity ("sticky sessions") `[MK]`

**What:** route a given client to the same backend repeatedly via a cookie inserted by the LB, a hash of the source IP, or a header value.
**Legitimate uses:** WebSocket/long-polling reconnections to a gateway holding state; cache locality; legacy stateful apps; per-user in-memory workloads; sharded stateful consumers.
**Costs (recall Ch 2/6):** uneven load (heavy users pile onto one backend), failover loses session data, scale-in/out disrupt users, deploys complicated, security (cookie tampering), and NAT/IP-hash clumping (a large corporate NAT looks like one user).
**Better:** make backends stateless; if you need locality, use **consistent hashing on a stable key** (user ID) rather than cookies or IPs, plus a fallback when the target is down.

---

## 7.8 Making the load balancer highly available `[MK]`

The LB is on the path of *every* request, so it must not be a single point of failure and must scale.

### 7.8.1 Patterns
| Pattern | How it works | Notes |
|---|---|---|
| **Active–passive with floating IP** | Two LBs share a virtual IP (VRRP/keepalived). The standby takes over the IP when the active's heartbeat fails | Simple; the standby is idle capacity; failover in seconds; connection state may be lost unless synced |
| **Active–active behind ECMP/anycast** | Routers spread flows across several LBs advertising the same IP (BGP + ECMP); consistent hashing keeps flows pinned | Scales horizontally; used for L4 tiers at scale (Maglev-style) |
| **DNS-based multiple LBs** | DNS returns several LB IPs (round robin / health-checked) | Simple; TTL limits failover speed; coarse |
| **Cloud-managed** | Provider runs many LB nodes across AZs behind one DNS name | Elastic and multi-AZ by default; subject to scaling lag and per-account limits (pre-warm for big events) |
| **LB per AZ + cross-zone balancing** | Each AZ has LB nodes; optionally balance across AZs | Cross-AZ traffic costs money and adds latency; balance carefully |

### 7.8.2 Capacity
Plan LB capacity for: **peak connections**, **new connections/s** (TLS handshakes are the CPU hog), **requests/s**, **bandwidth**, and **headroom for AZ loss**. Cloud LBs scale automatically but not instantly: sudden 10× spikes can hit limits; ask the provider to pre-scale for known events. Monitor LB metrics (active connections, 5xx from LB vs from targets, rejected connections, surge queue).

### 7.8.3 Failure modes of the LB tier
- **Config push errors** (a bad config deployed to all LBs at once). Mitigate: canary config, validation, automatic rollback.
- **Certificate expiry.** Automate renewal and monitor expiry days.
- **Connection table exhaustion** (SYN floods, TIME_WAIT, ephemeral ports to backends; Ch 1).
- **Idle timeout mismatches** (LB 60 s vs backend keep-alive 75 s → intermittent 502s). *Rule: backend keep-alive timeout must exceed the LB's idle timeout.*
- **Asymmetric capacity** during failover (the survivor takes 100% load).
- **LB as DDoS target:** put behind a CDN/DDoS service; rate limiting; SYN cookies.

---

## 7.9 Global traffic management `[MK/GK]`

When you run in multiple regions (or have users worldwide), you also need to decide *which region* serves each user.

### 7.9.1 DNS-based routing
The authoritative DNS server returns different answers depending on policy:
- **Geo routing** (by resolver location), **latency-based** (lowest measured latency to a region), **weighted** (for gradual migrations/canaries), **failover** (primary/secondary, driven by health checks), **multi-value** (several healthy IPs).
- **Limits:** resolver caching (TTL, typically 30–300 s for failover use; some resolvers ignore low TTLs), location is that of the *resolver*, not the user, coarse control, slow failover (TTL + detection time).

### 7.9.2 Anycast
Many locations advertise the **same IP** via BGP; the internet's routing sends each client to the "nearest" (in routing terms) location. Used by CDNs, DNS providers, and DDoS-mitigation services. Pros: fast automatic failover (withdraw the route), natural DDoS dilution, low latency. Cons: BGP-level control is coarse; flows can shift mid-connection when routes change (mitigated by flow pinning and QUIC connection IDs); stateful TCP across anycast needs care.

### 7.9.3 Global server load balancing (GSLB) and traffic steering
Products/services combining health checks across regions, capacity awareness, and policy: steer traffic away from a failing region, spill over when a region is at capacity, keep data-residency rules (EU users stay in EU).

### 7.9.4 Edge/CDN as the first LB
CDNs terminate user TLS close to the user, cache, and forward misses to the origin over optimized, warm connections: improving latency even for *dynamic* content (Ch 1's handshake math).

### 7.9.5 Regional failover design questions
- How fast must failover happen (RTO)? DNS TTL + detection time vs anycast withdrawal.
- Is the target region provisioned to absorb the failed region's load (N+1 *regions*)? Often only partially; plan degraded modes and load shedding.
- Is **data** available there (replication lag, RPO; Ch 9)?
- How do we **fail back** without thundering herds?

---

## 7.10 Reverse proxies `[MK]`

A **reverse proxy** accepts client requests and forwards them to backends on the client's behalf, so clients never talk to backends directly. (An L7 LB *is* a reverse proxy; a reverse proxy may front a single backend.) A **forward proxy** is the opposite direction: it sits in front of *clients* (corporate egress, privacy proxies).

### 7.10.1 Capabilities
| Capability | Why it matters |
|---|---|
| **TLS termination** | Central certs, offloaded crypto, TLS 1.3/HTTP-2/3 at the edge while backends speak simple HTTP |
| **Connection pooling to backends** | Few warm backend connections instead of one per client (saves handshakes, protects backends from thousands of idle connections) |
| **Request buffering** | The proxy absorbs slow clients (slowloris, mobile) so backend threads aren't tied up (huge for thread-per-request servers) |
| **Response buffering/compression** | gzip/brotli once at the edge |
| **Caching** | Serve repeated responses without hitting the backend (Ch 8) |
| **Static file serving** | Offload from app servers |
| **Rate limiting & connection limits** | Protect backends (Ch 22) |
| **Routing/rewrites/redirects** | Path-based routing, versioned APIs, A/B tests |
| **Retries/timeouts/circuit breaking** | Resilience policies in one place |
| **Header normalization/security headers** | HSTS, CSP, strip hop-by-hop, set X-Request-ID |
| **WAF / bot filtering** | Block known-bad traffic |
| **Access logging/metrics/tracing** | Uniform observability |

### 7.10.2 Typical config concepts (NGINX/HAProxy/Envoy)
- **Upstream/cluster:** the pool of backends. **Listener:** a port/protocol the proxy accepts. **Route:** match → action.
- **Timeouts:** connect (~1 s), request/response read (per route), idle, total (deadline). **Always set explicit timeouts**; defaults are often generous or infinite.
- **Max connections/pending requests** per backend (circuit-breaker thresholds).
- **Buffers:** header size limits, body size limits (reject oversized requests early).

### 7.10.3 Pitfalls
- **Header size and body limits** causing mysterious 413/431 errors.
- **Timeout stacking:** client 30 s, proxy 60 s, app 120 s → the app continues working long after the client gave up (wasted work). Rule: **timeouts should shrink as you go deeper** (upstream timeout < downstream timeout), and propagate deadlines.
- **Trusting `X-Forwarded-For`** blindly.
- **Buffering streaming/SSE/WebSocket** incorrectly (disable buffering for streams).
- **Proxy as bottleneck** (single-threaded worker misconfig, file descriptor limits).

---

## 7.11 API gateways `[MK]`

### 7.11.1 What
An **API gateway** is a reverse proxy specialized as the **single entry point for APIs**, adding API-management features on top of routing.
```text
 Mobile / Web / Partners ─▶ [ API Gateway ] ─┬─▶ Users service
                                             ├─▶ Orders service
                                             └─▶ Search service
```

### 7.11.2 Responsibilities
- **Authentication & authorization** (validate JWT/API key; coarse scopes).
- **Rate limiting & quotas** per client/plan.
- **Routing** and API **versioning**; request/response **transformation**.
- **Protocol translation** (REST ⇄ gRPC, WebSocket upgrade).
- **Aggregation** (compose several backend calls into one response: a BFF-style role).
- **Caching**, **compression**, **CORS**, **TLS**.
- **Observability:** access logs, metrics, tracing correlation IDs.
- **Developer portal/keys/monetization** (for public APIs).
- **Resilience:** timeouts, retries, circuit breakers.

### 7.11.3 Gateway vs load balancer vs service mesh
| | LB | API gateway | Service mesh |
|---|---|---|---|
| Primary concern | Distribute connections/requests | Manage and secure *external* API traffic | Manage *service-to-service* traffic |
| Location | Edge or internal | Edge (north-south) | Inside the cluster (east-west) |
| Awareness | L4/L7 | L7 + API semantics (keys, plans, versions) | L7 + service identity |
| Examples | NLB, HAProxy | Kong, Apigee, AWS API Gateway, Envoy-based gateways | Istio, Linkerd, Consul Connect |

### 7.11.4 Risks
- **Single point of failure / bottleneck** (so it must be redundant and scalable).
- **Logic creep:** business logic accumulating in the gateway makes it a "distributed monolith" choke point.
- **Added latency** (an extra hop plus auth/transform work).
- **Coupling and release coordination** if one team owns the gateway config for all services.
- **Backend authorization still required:** gateway checks are coarse; services must enforce object-level authorization (Ch 2).
Mitigations: keep gateways thin and mostly declarative; let services own business logic; use per-team route ownership; canary config changes.

### 7.11.5 BFF (Backend for Frontend)
A gateway variant per client type (web, iOS, TV) aggregating backend calls to shape responses for that client, cutting round trips (Ch 1's latency math). Owned by the frontend team.

---

## 7.12 Service mesh and sidecars `[GK]`

**Problem.** In a microservice system with 200 services in 5 languages, every service needs: TLS between services, retries, timeouts, circuit breaking, load balancing, metrics, tracing. Implementing this in every language/library is inconsistent and painful.
**Idea.** Run a **sidecar proxy** (typically Envoy) next to each service instance. All inbound/outbound traffic flows through it. A **control plane** (Istio, Linkerd) pushes configuration (routes, policies, certificates) to the sidecars (the **data plane**).
```text
 [Service A] ⇄ [sidecar] ═══mTLS═══ [sidecar] ⇄ [Service B]
                    ▲                    ▲
                    └──── control plane ─┘ (config, certs, service discovery, policy)
```
**Provides:** automatic mTLS and identity, per-request load balancing (P2C/least request), retries/timeouts/circuit breakers, traffic splitting (canary), fault injection, uniform metrics/traces.
**Costs:** extra CPU/memory per pod; extra latency hop (~0.5–2 ms) twice per call; operational complexity (control plane upgrades, debugging a proxy you don't own); config sprawl. Newer designs (ambient/sidecarless meshes, eBPF) reduce the overhead.
**When worth it:** many services, polyglot, strong security (mTLS) requirements, need for uniform traffic policy. **Not** worth it for a handful of services; a shared library or gateway may suffice.

---

## 7.13 Service discovery and client-side balancing `[MK/GK]`

**Problem.** Instances come and go (autoscaling, deploys). How do callers find the current set of healthy instances?

| Approach | How | Notes |
|---|---|---|
| **Static config** | Hard-coded addresses | Only for tiny/stable setups |
| **DNS** | Name → A/SRV records of instances (e.g., Kubernetes headless services, Consul DNS) | Simple; TTL/caching delays; coarse |
| **Server-side discovery** | Caller talks to a stable LB/VIP; the LB knows the pool (Kubernetes Service/kube-proxy, cloud LB) | Simple clients; extra hop |
| **Client-side discovery** | Client queries a **registry** (Consul, Eureka, etcd, ZooKeeper) and balances itself | No extra hop; clients need logic + fresh data |
| **Mesh/sidecar-based** | Sidecar subscribes to endpoint updates (xDS API) | Uniform; mesh cost |

**Registry mechanics:** instances **register** (with metadata, health), send **heartbeats/TTL renewals**, and are removed on missed heartbeats or deregistration; clients **watch** for changes and cache locally. Design for the registry being unavailable: clients should keep using their last-known good list (**fail static**).
**Client-side LB tips:** use **P2C/least-request**, maintain connection pools per endpoint, apply outlier ejection, jitter refresh, and subset very large backend pools (each client connects to a subset) to limit connection counts.

---

## 7.14 Retries, timeouts, and overload at the proxy `[MK]`

The proxy is where resilience policies are often enforced. Be deliberate:

### 7.14.1 Timeouts
- **Connect timeout** short (100 ms–1 s intra-DC).
- **Per-try timeout** < **overall deadline**; deadlines **shrink with depth**.
- **Idle timeouts** > client keep-alive intervals; **backend keep-alive timeout > LB idle timeout** (else sporadic 502s).

### 7.14.2 Retries
- Retry **only idempotent** requests (GET, PUT, DELETE, or POST with idempotency keys) and only on **safe failure types** (connect failure, reset before response, 503 with `Retry-After`), not on timeouts of non-idempotent calls (Ch 1/2).
- **Retry budgets:** cap retries at, say, 10% of normal traffic so a degraded backend isn't hammered (Envoy/Finagle retry budgets).
- **Backoff + jitter**, and **retry to a different backend** (not the same one).
- **One layer retries**, not all (27× amplification, Ch 1).
- **Hedging** for tail latency on idempotent reads (Ch 5).

### 7.14.3 Overload behavior
When backends are saturated, the proxy should **shed load** instead of queueing indefinitely:
- Bound **pending requests** per cluster (circuit-breaker thresholds); return **503 fast** when exceeded.
- Bound **connections** per backend.
- **Priority/criticality**: shed low-priority traffic first (e.g., recommendations before checkout).
- **Adaptive concurrency limits** (Netflix-style gradient/AIMD algorithms) that find each backend's capacity automatically `[Public: Netflix concurrency-limits library]`.
- Rate limit abusive clients at the edge (Ch 22).
These implement Chapter 5's lesson: *reject some work quickly to keep the rest fast.*

### 7.14.4 Observability at the proxy
Track per-route and per-backend: request rate, error rate by class (4xx/5xx/LB-generated 502/503/504), latency histograms (p50/p99), active connections, retries, ejections, pending queue depth, TLS handshake rate. Distinguish **LB-generated errors** (e.g., 502 because no healthy upstream) from **backend-generated** ones.

---

## 7.15 Labs

### Lab 1: Compare balancing algorithms under realistic conditions (Java 17+)
```java
import java.util.*;

public class LbSim {
    interface Picker { int pick(Backend[] bs, double now, Random r, int[] rrState); }

    static class Backend {
        double speed = 1.0;                        // 1.0 = normal, 2.0 = twice as slow
        double freeAt = 0;                         // time when its FIFO queue drains
        final ArrayDeque<Double> finishTimes = new ArrayDeque<>();
        int outstanding(double now) { while (!finishTimes.isEmpty() && finishTimes.peekFirst() <= now) finishTimes.pollFirst(); return finishTimes.size(); }
    }

    static double serviceMs(Random r) {            // high-variance mix: 90% fast (mean 5 ms), 10% slow (mean 50 ms) -> overall mean 9.5 ms
        double mean = r.nextDouble() < 0.9 ? 5 : 50;
        return -Math.log(1 - r.nextDouble()) * mean;
    }

    static void run(String name, int n, double util, boolean oneSlow, Picker picker) {
        Random r = new Random(7);
        Backend[] bs = new Backend[n];
        for (int i = 0; i < n; i++) bs[i] = new Backend();
        if (oneSlow) bs[0].speed = 2.0;
        double lambda = util * n / 9.5;            // arrivals per ms for the whole fleet
        int requests = 400_000; double t = 0;
        double[] lat = new double[requests]; int[] counts = new int[n]; int[] rr = {0};
        for (int i = 0; i < requests; i++) {
            t += -Math.log(1 - r.nextDouble()) / lambda;
            int b = picker.pick(bs, t, r, rr);
            Backend be = bs[b];
            double start = Math.max(t, be.freeAt);
            double finish = start + serviceMs(r) * be.speed;
            be.freeAt = finish; be.finishTimes.addLast(finish);
            lat[i] = finish - t; counts[b]++;
        }
        Arrays.sort(lat);
        System.out.printf("%-28s p50=%6.1f  p99=%7.1f  p99.9=%8.1f ms   share of load on backend0=%4.1f%%%n",
            name, lat[requests / 2], lat[(int) (requests * 0.99)], lat[(int) (requests * 0.999)], 100.0 * counts[0] / requests);
    }

    public static void main(String[] args) {
        Picker random = (bs, now, r, rr) -> r.nextInt(bs.length);
        Picker roundRobin = (bs, now, r, rr) -> rr[0]++ % bs.length;
        Picker leastConn = (bs, now, r, rr) -> { int best = 0, bo = Integer.MAX_VALUE; for (int i = 0; i < bs.length; i++) { int o = bs[i].outstanding(now); if (o < bo) { bo = o; best = i; } } return best; };
        Picker p2c = (bs, now, r, rr) -> { int a = r.nextInt(bs.length), b = r.nextInt(bs.length); return bs[a].outstanding(now) <= bs[b].outstanding(now) ? a : b; };

        for (boolean slow : new boolean[]{false, true}) {
            System.out.println(slow ? "\n--- 16 backends, 80% utilization, backend0 is 2x SLOWER (a 'gray failure') ---"
                                    : "--- 16 backends, 80% utilization, all healthy, high-variance request sizes ---");
            run("random", 16, 0.80, slow, random);
            run("round robin", 16, 0.80, slow, roundRobin);
            run("least connections", 16, 0.80, slow, leastConn);
            run("power of two choices (P2C)", 16, 0.80, slow, p2c);
        }
    }
}
```
**Experiments**
1. Reproduce the tables in §7.5.2.
2. Vary utilization (0.5, 0.7, 0.9) and see when the algorithms diverge: at low load they're equivalent; at high load the choice matters enormously.
3. Make request sizes uniform (all 9.5 ms). Does round robin improve? (It should look much better: variance, not just load, drives the gap.)
4. Add a **stale-information** model: least-connections sees queue lengths that are 20 ms old (keep a snapshot). Observe herd behavior; compare with P2C using the same stale data.
5. Add **panic ejection**: eject any backend whose recent p99 exceeds 5× the fleet median (with a minimum of 12 backends kept). Show how it handles the gray failure for random/round robin.
6. Implement **consistent hashing with bounded loads** and compare load spread and cache-hit locality for 10 M keys.

### Lab 2: A working load balancer with health checks, retries, and failover (Java 17+)
Run backends and an LB, then break things on purpose.

```java
import com.sun.net.httpserver.*;
import java.io.*;
import java.net.*;
import java.net.http.*;
import java.time.Duration;
import java.util.*;
import java.util.concurrent.*;
import java.util.concurrent.atomic.*;

/** Usage:  java MiniLb.java backend 9001
 *          java MiniLb.java lb 8000 http://localhost:9001 http://localhost:9002 http://localhost:9003 */
public class MiniLb {
    // ------------------------------------------------------------------ backend
    static void runBackend(int port) throws Exception {
        HttpServer s = HttpServer.create(new InetSocketAddress(port), 0);
        s.setExecutor(Executors.newFixedThreadPool(32));
        s.createContext("/health", ex -> reply(ex, 200, "ok"));
        s.createContext("/slow", ex -> { try { Thread.sleep(3000); } catch (InterruptedException ignored) {} reply(ex, 200, "slow from " + port); });
        s.createContext("/", ex -> reply(ex, 200, "hello from backend " + port));
        s.start();
        System.out.println("backend listening on " + port);
    }
    static void reply(HttpExchange ex, int code, String body) throws IOException {
        byte[] b = body.getBytes(); ex.sendResponseHeaders(code, b.length);
        try (OutputStream os = ex.getResponseBody()) { os.write(b); }
    }

    // ------------------------------------------------------------------ load balancer
    static class Be {
        final String url; volatile boolean healthy = true;
        final AtomicInteger active = new AtomicInteger(); int okStreak = 0, failStreak = 0;
        Be(String url) { this.url = url; }
    }
    static final HttpClient client = HttpClient.newBuilder().connectTimeout(Duration.ofMillis(300)).build();

    static void runLb(int port, List<String> urls) throws Exception {
        List<Be> all = new ArrayList<>(); for (String u : urls) all.add(new Be(u));

        // active health checker: every 500 ms, 2 consecutive failures -> out, 2 successes -> back in
        ScheduledExecutorService hc = Executors.newScheduledThreadPool(1);
        hc.scheduleAtFixedRate(() -> { for (Be b : all) {
            boolean ok;
            try { ok = client.send(HttpRequest.newBuilder(URI.create(b.url + "/health")).timeout(Duration.ofMillis(300)).build(),
                                   HttpResponse.BodyHandlers.discarding()).statusCode() == 200; }
            catch (Exception e) { ok = false; }
            if (ok) { b.failStreak = 0; if (++b.okStreak >= 2 && !b.healthy) { b.healthy = true; System.out.println("[health] " + b.url + " is back IN"); } }
            else    { b.okStreak = 0;   if (++b.failStreak >= 2 && b.healthy) { b.healthy = false; System.out.println("[health] " + b.url + " marked OUT"); } }
        } }, 0, 500, TimeUnit.MILLISECONDS);

        HttpServer s = HttpServer.create(new InetSocketAddress(port), 0);
        s.setExecutor(Executors.newFixedThreadPool(64));
        s.createContext("/", ex -> {
            boolean idempotent = ex.getRequestMethod().equals("GET");
            byte[] reqBody = ex.getRequestBody().readAllBytes();
            Set<Be> tried = new HashSet<>();
            for (int attempt = 0; attempt < (idempotent ? 2 : 1); attempt++) {          // retry once, only for idempotent requests
                Be b = pick(all, tried);
                if (b == null) { reply(ex, 503, "no healthy backends"); return; }
                tried.add(b); b.active.incrementAndGet();
                try {
                    HttpRequest.Builder rb = HttpRequest.newBuilder(URI.create(b.url + ex.getRequestURI()))
                        .timeout(Duration.ofSeconds(2)).method(ex.getRequestMethod(), HttpRequest.BodyPublishers.ofByteArray(reqBody));
                    HttpResponse<byte[]> resp = client.send(rb.build(), HttpResponse.BodyHandlers.ofByteArray());
                    ex.getResponseHeaders().add("X-Backend", b.url);
                    ex.sendResponseHeaders(resp.statusCode(), resp.body().length);
                    try (OutputStream os = ex.getResponseBody()) { os.write(resp.body()); }
                    return;
                } catch (Exception e) {
                    System.out.println("[proxy] " + b.url + " failed: " + e.getClass().getSimpleName() + (idempotent && attempt == 0 ? " -> retrying elsewhere" : ""));
                } finally { b.active.decrementAndGet(); }
            }
            reply(ex, 502, "bad gateway");
        });
        s.start();
        System.out.println("LB listening on " + port + " -> " + urls);
    }

    /** least-connections among healthy backends not yet tried; ties broken randomly */
    static Be pick(List<Be> all, Set<Be> exclude) {
        List<Be> c = new ArrayList<>(); for (Be b : all) if (b.healthy && !exclude.contains(b)) c.add(b);
        if (c.isEmpty()) return null;
        Collections.shuffle(c); Be best = c.get(0);
        for (Be b : c) if (b.active.get() < best.active.get()) best = b;
        return best;
    }

    public static void main(String[] a) throws Exception {
        if (a[0].equals("backend")) runBackend(Integer.parseInt(a[1]));
        else runLb(Integer.parseInt(a[1]), Arrays.asList(a).subList(2, a.length));
    }
}
```
**Run it** (each in its own terminal; Java source launcher, no build step):
```bash
java MiniLb.java backend 9001      # terminals 1–3 with ports 9001, 9002, 9003
java MiniLb.java lb 8000 http://localhost:9001 http://localhost:9002 http://localhost:9003
for i in $(seq 1 12); do curl -si localhost:8000/ | grep -i x-backend; done | sort | uniq -c
```
**What I observed when running this scenario** (single script; kill backend 9002, then restart it):
```text
1) healthy: 12 GETs          →  3× :9001, 5× :9002, 4× :9003
2) kill :9002, immediately send 12 GETs (health check hasn't noticed yet)
                             →  all 12 returned HTTP 200 (4× :9001, 8× :9003)
3) ~3 s later (ejected)      →  all 200, only :9001 and :9003
4) restart :9002, wait       →  traffic returns to all three
LB log:  [proxy] http://localhost:9002 failed: ConnectException -> retrying elsewhere
         [health] http://localhost:9002 marked OUT
         [health] http://localhost:9002 is back IN
```
**Interpretation:** in step 2, the *one* request that was routed to the dead backend failed at connection time, was **transparently retried** on another backend (GET = idempotent), and the user never saw an error. The health check then ejected the node (**passive + active detection working together**), and after restart the node was re-admitted after 2 successful checks.

**Experiments**
1. Hit `/slow` (3 s) with 20 parallel curls and watch **least-connections** shift new requests away from busy backends. Which backend gets the `/health`-style quick requests meanwhile?
2. Make a backend return 500 for `/` while `/health` still returns 200: the health check passes but users see errors. Add **passive outlier detection** (eject after 3 consecutive 5xx) to fix it.
3. Send `POST /` while a backend is down. Why does the LB not retry it? What would you need to add (idempotency keys) to retry safely?
4. Add **connection draining**: `POST /drain` on the LB for a backend: stop new requests, wait until `active == 0`, then mark it removed.
5. Run two instances of the Chapter 2 `TaskApi` behind this LB (ports 8081, 8082) and *observe* the hidden-state bugs from Chapter 6's audit (tasks created on one instance missing on the other; idempotency keys honored only per-instance).
6. Add **slow start**: weight a newly admitted backend at 10% and ramp to 100% over 30 s.

### Lab 3: Health-check design exercise (paper)
For a service with dependencies {Postgres, Redis, a payment provider API}: define liveness, readiness, and dependency-health endpoints; thresholds; what each returns during (a) a 10-s Redis blip, (b) Postgres failover (30 s), (c) payment provider outage; and show that your design avoids removing all instances in each case.

---

## 7.16 Common misconceptions

1. **"Round robin is fair."** Fair in *count*, not in *work*: variance in request cost and server speed makes it unfair in practice.
2. **"A load balancer makes the system highly available."** Only if the LB tier itself is redundant.
3. **"Deep health checks are safer."** They can create cascading total outages when a shared dependency blips.
4. **"L7 is just a smarter L4."** It terminates connections, so it changes TLS, connection reuse, and failure semantics.
5. **"gRPC will balance automatically behind my L4 LB."** It balances *connections*, not requests; use L7 or client-side balancing.
6. **"Sticky sessions are a free fix."** They trade away balance, failover, and deploy ease.
7. **"DNS round robin is a load balancer."** No health awareness, caching distorts distribution, slow failover.
8. **"More retries = more reliability."** They amplify overload and duplicate writes unless budgeted and idempotent.
9. **"The gateway can hold our business logic."** Logic belongs in services; gateways stay thin.
10. **"Service mesh is required for microservices."** It's an option with real costs.
11. **"The LB adds no failure modes."** Timeout mismatches, config pushes, and cert expiry are classic outage causes.
12. **"Anycast/DNS failover is instant."** Caches, BGP convergence, and health detection all take time.

---

## 7.17 Chapter summary

- An **LB** gives clients one stable endpoint, spreads load, detects failures, centralizes TLS, and enables safe deploys; it also adds a hop and must itself be redundant and scalable.
- **L4** balances connections quickly; **L7** terminates and understands HTTP, enabling routing/retries/caching. **TLS** can terminate, bridge, or pass through. **HTTP/2 & gRPC** need L7 or client-side balancing because connections are long-lived.
- **Algorithms:** load-agnostic ones (round robin/random) break down under variable request cost and slow nodes (simulated: p99 4× worse healthy, collapse with one 2× slower node); **least connections** and **power of two choices** adapt; **consistent hashing (bounded loads)** for locality.
- **Health checks:** active + passive, thresholds, liveness vs readiness, shallow vs deep (deep checks can cascade), panic mode, slow start, connection draining.
- **HA of the LB tier:** active–passive (VRRP), active–active (ECMP/anycast), managed multi-AZ; watch config pushes, cert expiry, idle-timeout mismatches.
- **Global traffic management:** DNS (geo/latency/weighted/failover), anycast, GSLB, CDN edge; failover takes time and needs capacity and data in the target region.
- **Reverse proxies, API gateways, service meshes, client-side discovery** each solve specific problems; keep gateways thin; adopt meshes when scale/security justifies them.
- At the proxy: set **explicit timeouts (shrinking with depth)**, **retry budgets with idempotency**, **bounded queues and load shedding**, and monitor LB-vs-backend errors.

### Mental models
| Concept | Mental model |
|---|---|
| Load balancer | Receptionist who knows who's in, who's busy, who's ill |
| L4 vs L7 | Routing parcels by address label vs opening the parcel to read the contents |
| Least connections / P2C | Choosing the shorter checkout line (P2C: glance at two lines and pick the shorter) |
| Gray failure | A cashier who is awake but half-speed: invisible to attendance checks |
| Deep health check cascade | Closing every shop because the street power flickered |
| Sticky sessions | Always the same waiter regardless of how busy he is |
| Retry storm | Everyone redialing the same busy line |

---

## 7.18 Exercises

### A. Conceptual
1. List six problems a load balancer solves and three it introduces.
2. Compare L4 and L7 LBs on decision basis, TLS handling, backend connection reuse, features, and cost. When would you put L4 in front of L7?
3. Explain TLS termination, re-encryption, and pass-through, with one reason to choose each.
4. Why do gRPC clients behind an L4 load balancer often overload a few backends? Give three fixes.
5. Explain why round robin performs poorly with variable request sizes (use "head-of-line blocking").
6. Explain power of two choices and why it beats random. Why is it better than least-connections when many independent LBs exist?
7. When do you want consistent hashing at the LB rather than least-connections?
8. Define active and passive health checks. Give one failure each catches that the other misses.
9. Explain how a deep health check can cause a total outage during a database blip. Propose a better design.
10. What is panic mode (fail-open)? Why is it sensible?
11. Slow start and connection draining: what problem does each solve?
12. Compare active–passive, active–active, and DNS-based HA for the LB tier.
13. Why must the backend keep-alive timeout exceed the LB idle timeout?
14. Compare DNS geo routing and anycast for global traffic management, including failover speed.
15. List five things a reverse proxy does besides balancing.
16. API gateway vs LB vs service mesh: purpose and location of each.
17. Why should API gateways stay thin? Name three risks of gateways.
18. What are retry budgets and why are they necessary?
19. Why should timeouts shrink as you go deeper into the call chain?
20. What is client-side load balancing and what extra machinery does it need (discovery, health, pooling)?

### B. Calculations
21. An LB tier must terminate TLS for 40,000 new connections/s at peak; each node handles 8,000 new TLS handshakes/s at 60% target. How many nodes (N+1)? Why are new-connections/s more constraining than requests/s for many workloads?
22. A fleet of 50 backends serves 20,000 req/s; each backend handles 500 req/s at 80% max. Is it enough if 8 backends fail? If an AZ holding 1/3 of backends fails?
23. Health checks: 400 backends, 10 LBs, each LB probes each backend every 2 s. Probes per second? Propose a design that reduces this.
24. Retry amplification: Client → Gateway → Service A → Service B. Each hop retries 2 extra times on failure. If B is down, how many requests reach B per client request? What retry budget limit would you set?
25. Idle timeout mismatch: LB idle timeout 60 s, backend keep-alive 30 s, and clients send requests on a connection every ~45 s. What happens and what's the fix?
26. Power of two choices: for n = 100,000 requests into 100,000 servers, the expected max load under random is roughly ln n / ln ln n. Compute it; compare with ln ln n / ln 2.
27. DNS failover timing: health check interval 10 s, 3 failures required, record TTL 60 s, resolvers cache up to TTL. Worst-case time until all clients stop using a dead region? Compare with anycast withdrawal (~30 s BGP convergence).

### C. Design
28. Design the traffic entry for a service: 100k RPS, global users, WebSockets and REST, strict 99.99% availability. Specify DNS, CDN, L4/L7 tiers, algorithms, health checks, TLS mode, and failure handling.
29. Design the health-check and rollout (deploy) process for a stateless API with a 90-second warm-up, a Redis cache, and a Postgres DB (liveness/readiness, slow start, draining, canary).
30. A multi-tenant API has a few "whale" customers causing noisy-neighbor issues. Design per-tenant isolation using the proxy/gateway (rate limits, concurrency limits, routing to dedicated pools).
31. Choose between gateway, mesh, and client library for resilience features in (a) 6 Java services; (b) 150 polyglot services; (c) a public partner API.
32. Plan a regional failover: Region A serves 60% of traffic, Region B 40%. B has capacity for 80% of total. Describe steps, traffic steering, load shedding, and data considerations.

### D. Debugging
33. Intermittent 502s every few minutes, mostly on idle-then-reused connections. Diagnose and fix.
34. After scaling gRPC backends from 5 to 20, three pods still get 80% of the traffic. Explain and fix.
35. A health check hits the database; during a 20-second DB failover, all instances are marked unhealthy and the site returns 503 for 3 minutes, long after the DB recovered. Explain the extended outage (hints: thresholds, re-admission, thundering herd, caches) and the fix.
36. One backend shows normal CPU and passes health checks but has a p99 of 3 s while others are at 100 ms. The LB uses round robin. Diagnose; list mitigations at three layers.
37. All requests suddenly appear to come from one IP in logs and the rate limiter blocks everyone. What happened and how do you fix it?
38. Sticky sessions are enabled; after a deploy, a quarter of users lose their carts. Explain and propose the right fix.

### E. Interview-style
39. "Where would you put load balancers in this architecture and why?" (Draw and justify.)
40. "How do you avoid the load balancer being a single point of failure?"
41. "Explain L4 vs L7 and when to use each."

### F. Lab follow-ups
42. In Lab 1, find the utilization at which least-connections and round robin p99 first differ by 2×.
43. In Lab 2, add outlier detection (3 consecutive 5xx → eject for 10 s, doubling each time) and demonstrate recovery.
44. In Lab 2, implement slow start and show the traffic share of a restarted backend over time.

### G. Teach-back
45. Explain power of two choices to a friend with a supermarket checkout analogy, then give the technical result.

---

## 7.19 Full answer key

**1.** Solves: stable endpoint/abstraction, uneven load, dead-server routing, TLS sprawl, dynamic membership, load/speed awareness (+ drains for deploys, protection). Introduces: an extra hop (latency), a new SPOF/bottleneck, new failure modes (config/health-check/timeouts) and operational burden.

**2.** L4: decisions on IP/port, balances connections, passes TLS through, no backend connection reuse, simple features, very fast. L7: terminates TLS/HTTP, balances requests, pools backend connections, rich features (routing, retries, caching, auth), more CPU. L4 in front of L7 gives massive connection scale and simple HA for the L7 fleet which does the content-aware work.

**3.** Termination: central certs/inspection, plaintext internally (fine in trusted networks). Re-encryption: inspection plus encryption inside (zero-trust, compliance). Pass-through: end-to-end encryption/client certs visible to backends, non-HTTP protocols.

**4.** Long-lived multiplexed connections are balanced once at connect time, so each client's traffic sticks to one backend, and new backends don't get existing connections. Fixes: L7 per-request balancing; client-side balancing; recycle connections (max age/GOAWAY).

**5.** With variable request cost, a request can land on a server that's already working on a slow job; others idle. Round robin ignores queue lengths, so short requests wait behind long ones (head-of-line blocking), inflating the tail. Simulation: p99 629 ms vs 150 ms for least connections.

**6.** Choose two random backends and send to the less loaded. It makes the maximum load grow like ln ln n instead of ln n/ln ln n (exponential improvement). With many independent LBs, "global least connections" suffers herd effects from stale information; P2C needs only two load reads and the randomization prevents all LBs from picking the same server.

**7.** When the same key must map to the same backend for locality or state (cache partitioning, per-user state, WebSocket rooms, sharded consumers). Least-connections ignores keys and would scatter them.

**8.** Active: probes on a schedule; catches idle-but-broken backends and confirms recovery, but misses failures that appear only under real traffic and is delayed by the interval. Passive: watches real traffic and reacts instantly to errors/latency but needs traffic to detect, and can't test an ejected node for recovery without probes.

**9.** The check includes the DB; when the DB blips, every instance fails the check simultaneously and the LB removes them all, so the site is down for as long as the checks take to recover (plus warm-up and thundering herd), even for requests that don't need the DB. Better: shallow readiness for local health, dependency status exposed for monitoring/alerts, panic mode when most backends are unhealthy, degrade gracefully with cached data.

**10.** If more than a threshold (e.g., 50%) of backends are unhealthy, ignore health status and route to all. A mass failure is more likely a bad check, shared dependency blip, or network issue than half your fleet dying; failing open keeps serving whatever can be served.

**11.** Slow start prevents cold instances from being overwhelmed by a full traffic share (cold caches/JIT/pools). Draining prevents dropping in-flight requests when removing a backend (deploys, scale-in).

**12.** Active–passive: simple, failover seconds, idle standby, state may be lost. Active–active: scales horizontally, no idle capacity, needs flow-consistent hashing/ECMP, more complex. DNS-based: simple multi-LB, but TTL-bound slow failover and no health awareness unless health-checked DNS.

**13.** If the backend closes an idle connection before the LB's idle timeout, the LB may reuse a connection the backend has already closed, producing connection resets/502s. The backend must keep connections open longer than the LB would.

**14.** DNS geo: location by resolver, TTL-bound failover (tens of seconds to minutes), flexible policies. Anycast: BGP routes to the nearest location; failover by withdrawing routes within tens of seconds; coarse control, flows may shift mid-connection.

**15.** TLS termination, connection pooling, request/response buffering, compression, caching, static serving, rate limiting, routing/rewrites, retries/timeouts/circuit breaking, security headers/WAF, access logs and metrics.

**16.** LB: distribute traffic (edge or internal). API gateway: edge entry for external API traffic with auth, quotas, routing, transformation. Service mesh: service-to-service (east–west) traffic via sidecars with mTLS, retries, and observability.

**17.** Thin gateways keep business logic in services. Risks: SPOF/bottleneck, logic creep/distributed monolith, extra latency, release coordination across teams, false sense of security (services still need object-level authorization).

**18.** A retry budget limits retries to a fraction of normal request volume (e.g., 10%) so a degraded service isn't flooded by retries, preventing amplification and metastable failures.

**19.** The caller gives up after its timeout; if the callee continues working longer, it wastes capacity on results nobody wants and increases load during incidents. Deeper timeouts should be shorter so callers can retry or fail gracefully within their own deadlines.

**20.** The client holds the list of backends and balances itself, requiring service discovery (registry/DNS/xDS) with watch/refresh, health/outlier detection, per-endpoint connection pools, P2C/least-request logic, and fail-static behavior when discovery is down.

**21.** 40,000 / (8,000 × 0.6 = 4,800) = 8.33 → 9 nodes, +1 = **10**. New connections/s are constraining because TLS handshakes consume far more CPU than steady-state requests on established connections (especially with many short-lived mobile/IoT clients).

**22.** Capacity at 80%: 50 × 500 × 0.8 = 20,000 req/s: exactly at the limit with all healthy. 8 failures: 42 × 400 = 16,800 < 20,000 → **not enough** (shortfall 16%). AZ failure with 1/3 of backends (≈17): 33 × 400 = 13,200 → not enough. You need more headroom (target ≤ ~55–60% utilization at peak).

**23.** 400 × 10 / 2 s = **2,000 probes/s**. Reduce via centralized health checkers publishing to a control plane/discovery (e.g., 3 checkers → 600 probes/s), longer intervals with jitter, passive checks, subsetting.

**24.** Each hop retries twice → each tier makes up to 3 attempts: Gateway→A: 3 attempts per client request; each attempt A→B: 3 attempts (and the client itself may also retry 3×). Total at B = 3 (client) × 3 (gateway→A) × 3 (A→B) = **27** if all three layers retry. Use a budget of ~10% retries per tier and retry at one layer.

**25.** Backend closes at 30 s; LB thinks the connection is still valid until 60 s, so a request at ~45 s hits a closed connection → resets/502s intermittently. Fix: set backend keep-alive > LB idle timeout (e.g., 75 s vs 60 s), or reduce the LB idle timeout below the backend's.

**26.** n = 10⁵: ln n = 11.51; ln ln n = 2.44; random ≈ 11.51/2.44 ≈ **4.7** (plus lower-order terms, practically ~7–8); P2C ≈ 2.44/0.693 ≈ **3.5** (+ constant, practically ~4). The gap widens slowly with n, but the key effect is in the tail/variance with real workloads.

**27.** Detection: 3 failures × 10 s = 30 s; then the record is withdrawn; resolvers may cache up to 60 s more → worst case ≈ **90 s** (plus resolvers ignoring TTLs, so longer for some clients). Anycast: withdraw route + BGP convergence ≈ 30 s (varies).

**28 (sketch).** Anycast/GeoDNS → CDN/edge (TLS 1.3/HTTP-3, DDoS absorb) → regional L4 (NLB-like, multi-AZ, ECMP) → L7 proxy fleet (Envoy/ALB) with P2C/least-request and consistent hashing for WebSocket gateway pool; shallow readiness + passive outlier detection + panic mode; TLS re-encryption (mTLS) to services; health-checked failover across regions; N+1 AZ capacity; pre-scaling; draining on deploy; rate limits at the edge.

**29 (sketch).** Liveness `/live` (process OK); readiness `/ready` (initialized, warm-up complete, not shutting down; avoids hard dependency on Redis/DB unless truly required); dependency metrics for alerts; LB slow start (90–120 s); drain 60 s; canary 1% → 10% → 50% with automatic rollback on error/latency regressions; rolling surge ≥ 1 instance; backward-compatible schema/cache keys.

**30 (sketch).** Identify tenant at the gateway (API key/JWT claim); per-tenant rate limits and concurrency limits; weighted fair queuing; route whales to dedicated pools (header- or claim-based routing); circuit breaker per tenant; priority classes; quotas with 429 + Retry-After; metrics by tenant.

**31 (sketch).** (a) Shared library (e.g., Resilience4j) or simple gateway; mesh overkill. (b) Service mesh (consistent mTLS/retries/observability across languages). (c) Dedicated API gateway with keys, quotas, versioning, WAF.

**32 (sketch).** Detect via health checks/GSLB; shift Region A's 60% to B: B must serve 100% but has capacity for 80% → shed 20% (low-priority features, rate limits, queue-based degrade); steer via DNS/anycast with low TTLs; ensure B's data is current (replication lag/RPO) and writes are accepted (promote B's DB if needed); pre-warm caches; monitor saturation; fail back gradually with canary percentages.

**33.** Idle connections closed by one side (timeout mismatch or NAT/firewall idle expiry); the next reused connection resets → 502. Fix: align timeouts (backend keep-alive > LB idle), enable TCP keepalive/HTTP/2 pings, retry idempotent requests on connection reset.

**34.** gRPC connections established earlier remain pinned; L4 LB balanced connections once, and new pods don't receive existing connections. Fix: L7 per-request balancing or client-side LB with discovery; set max connection age; ensure DNS refresh.

**35.** The DB failover failed health checks for all instances, so the LB removed them all (hard fail instead of panic mode); re-admission required N consecutive successes and a slow start; reconnecting pools and cold caches caused a thundering herd that re-tripped checks; possibly deep checks timing out under load. Fix: shallow readiness, panic mode, jittered health checks, slow start, connection pool retry/backoff, and caches to absorb load.

**36.** A gray failure: the node is slow but alive. Mitigations: LB layer: least-connections/P2C and latency-based outlier ejection; app layer: per-request timeouts and hedged requests; infra layer: detect and replace the host (noisy neighbor, failing disk, GC), alert on per-instance latency skew.

**37.** The rate limiter uses the proxy's IP instead of the client IP (missing/untrusted `X-Forwarded-For`/PROXY protocol). Fix: configure trusted proxies to pass and parse the real client IP; ensure only your edge sets the header.

**38.** Carts held in per-instance memory; sticky sessions pinned users to old instances that were replaced in the deploy. Right fix: externalize cart state (DB/Redis) and make instances stateless; keep stickiness out of the critical path.

**39 (sketch).** CDN/edge (cache static, TLS, WAF) → L4 in front of an L7 fleet → API gateway → services (internal L7 or mesh) → DB proxy. Justify each: each layer handles scale and responsibility appropriate to it; show health checks/HA.

**40 (sketch).** Use multiple LB nodes across AZs (managed LB, or VRRP pair, or ECMP/anycast), health-checked DNS for multi-region, capacity headroom, config canaries, automated cert renewal, monitoring of the LB itself.

**41 (sketch).** L4: transport-level, connection-based, fast, no HTTP awareness; use for non-HTTP, extreme throughput, or fronting L7. L7: HTTP-aware, request-based, routing/retries/caching/auth; use for web/API traffic and microservice routing.

**42–44.** Lab results: expect divergence once utilization exceeds ~60–70% with high-variance requests; outlier detection ejects the failing backend after three errors and restores via probe; slow start shows the share rising linearly from ~10% to its fair share.

**45.** Supermarket: random queue choice leaves some lines long and others empty; glancing at two random lines and joining the shorter nearly eliminates the long lines, without needing to inspect all lines. Technically, max load drops from ~ln n/ln ln n to ~ln ln n/ln 2.

---

## 7.20 Flashcards

| Front | Back |
|---|---|
| LB solves | Abstraction, distribution, failure detection, TLS centralization, dynamic membership |
| L4 vs L7 | Connection/IP-port-based vs request/HTTP-aware |
| gRPC + L4 trap | Balances connections, not requests → use L7 or client-side LB |
| Round robin weakness | Ignores load and request size; head-of-line blocking |
| P2C | Pick two random, choose the less loaded; exponential improvement over random |
| Gray failure | Slow-but-alive node; defeats health checks; needs load-aware LB/outlier ejection |
| Consistent hashing use | Locality/stickiness with minimal reshuffle on membership change |
| Active vs passive health checks | Probing vs observing real traffic; use both |
| Deep health check danger | Shared dependency blip removes all instances |
| Panic mode | If most backends unhealthy, route to all |
| Slow start | Gradually ramp traffic to new instance |
| Connection draining | Finish in-flight requests before removal |
| Keep-alive rule | Backend keep-alive timeout > LB idle timeout |
| LB HA patterns | VRRP active–passive, ECMP/anycast active–active, managed multi-AZ, DNS |
| DNS vs anycast failover | TTL-bound minutes vs BGP-bound tens of seconds |
| Gateway vs mesh | North–south API entry vs east–west service traffic |
| Retry rules | Idempotent only, budgets, backoff+jitter, one layer, different backend |
| Timeout rule | Shrink with depth; propagate deadlines |
| Load shedding | Reject fast when saturated to keep latency for the rest |
| X-Forwarded-For | Client IP via proxy; trust only your own proxies |

## 7.21 Glossary additions

| Term | Simple Meaning | Technical Meaning | Why It Exists | Example |
|---|---|---|---|---|
| Load balancer | Traffic director | Distributes requests/connections across a pool | Scale, HA | NGINX, ALB |
| VIP | Shared front address | Virtual IP fronting a pool | Stable endpoint | 203.0.113.10 |
| L4 / L7 | Transport / app-level | Balances on IP:port vs HTTP content | Speed vs features | NLB / ALB |
| TLS termination | LB decrypts | TLS ends at the proxy | Central certs, inspection | HTTPS→HTTP |
| Pass-through | Don't decrypt | L4 forwarding of encrypted bytes | End-to-end encryption | SNI routing |
| Reverse proxy | Front for servers | Proxy acting on behalf of backends | Security, TLS, caching | NGINX |
| Forward proxy | Front for clients | Proxy acting on behalf of clients | Egress control | Corporate proxy |
| Least connections | Pick least busy | Route to the backend with fewest active requests | Adapt to load | HAProxy |
| P2C | Pick the better of two | Power of two random choices | Cheap near-optimal balance | Envoy |
| Consistent hashing | Stable key mapping | Ring-based placement minimizing remaps | Locality | Ketama |
| Health check | "Are you OK?" | Active probe of backend status | Remove bad nodes | `/health` |
| Outlier detection | Eject misbehavers | Passive ejection by error/latency | Catch gray failures | Envoy |
| Panic mode | Fail open | Ignore health when too many are unhealthy | Avoid total outage | 50% threshold |
| Slow start | Ramp-up | Gradual traffic share for new nodes | Protect cold instances | 60 s ramp |
| Draining | Graceful removal | Stop new, finish in-flight | Safe deploys | 60 s |
| Sticky session | Pin client to node | Affinity by cookie/hash | Stateful backends | LB cookie |
| ECMP / anycast | Spread flows / same IP everywhere | Equal-cost routing / BGP multi-origin | LB-tier scale, global entry | CDN IPs |
| GSLB | Global LB | Region-level steering with health/policy | Multi-region | Route 53 policies |
| API gateway | Front door for APIs | Auth, quotas, routing at the edge | API management | Kong |
| Service mesh | Network layer for services | Sidecar proxies + control plane | mTLS, retries, telemetry | Istio |
| Sidecar | Proxy beside the app | Per-instance proxy process | Uniform policy | Envoy |
| Service discovery | Finding instances | Registry/DNS/xDS for endpoints | Dynamic fleets | Consul |
| Retry budget | Cap on retries | Max retry fraction of traffic | Prevent storms | 10% |
| Hedging | Backup request | Duplicate after a delay | Cut tails | after p95 |
| Load shedding | Reject excess | Drop requests to protect latency | Overload control | 503 fast |

---

**Next: Chapter 8: Caching.** The most powerful performance tool and the most subtle source of bugs: why caching works (locality and skew, with measured hit ratios), where caches live, the read/write patterns (cache-aside, read/write-through, write-back), eviction (LRU/LFU/TinyLFU), invalidation and consistency races, stampedes/avalanches/penetration/hot keys (with simulations), distributed cache architecture, and multi-level caching.

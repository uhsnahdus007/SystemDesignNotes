# Chapter 2: Web Applications and APIs: How Software Talks

## Chapter card

| | |
|---|---|
| **Objective** | Understand how a typical web/mobile application is structured; design good HTTP APIs; understand authentication, sessions, tokens, and why *stateless* servers matter; compare real-time communication options |
| **Why this chapter exists** | Every system you'll design exposes APIs and authenticates users. The decisions here (statelessness, idempotency, pagination, auth) decide whether the system can later scale horizontally and survive retries |
| **Prerequisites** | Chapters 0 and 1 (HTTP, TCP, TLS, timeouts/retries) |
| **Study time** | 8–10 hours including the lab |
| **Tier** | `[MK]` except where marked |
| **Mastery criteria** | (1) Draw browser → API → backend → database and explain each arrow. (2) Design a REST API (resources, methods, status codes, pagination, errors, versioning). (3) Make a non-idempotent operation safe under retries with idempotency keys. (4) Compare sessions, JWTs, and OAuth 2.0/OIDC and choose per case. (5) Explain *stateless vs stateful* servers and what each does to scaling and failover. (6) Choose between polling, long polling, SSE, WebSockets. (7) Compare REST, gRPC, GraphQL |

---

## 2.1 The Story: Building "TaskBoard"

You build **TaskBoard**, a to-do app. Version 0: a single HTML page and a server script that stores tasks in memory.

```text
Browser ──▶ [ one Node/Java/Python process: HTML + logic + a list in RAM ]
```
It works for you. Then:
1. You restart the server for a bug fix: **all tasks vanish** (RAM is volatile; Ch 1).
2. A friend uses the app on his phone and wants the *same tasks* as on his laptop.
3. You build a mobile app. It needs the same logic *without* the HTML.
4. Two servers are needed for traffic; a user's request alternates between them and **sees different data** (state was in each server's memory).
5. A script starts hammering your server.
6. Users want to be logged in without re-entering passwords for each request.
7. A user taps "Add task" while on a train; the request times out. They tap again. **Now there are two identical tasks.**

Each problem maps to a concept in this chapter. By the end you'll have a clean answer to all seven.

### Pause & Predict 2.1
> For problems 4 and 7 above, what's *the root cause* in each case and what's the general fix? Write your hypotheses. (Answers in §2.5 and §2.7.)

---

## 2.2 The anatomy of a web application

### 2.2.1 From one program to tiers

**Naive:** one program does everything: UI, logic, storage.
**Why it breaks:** can't have multiple UIs (web, iOS, Android), can't scale parts independently, restarts lose data, security is tangled.
**Better:** separate by responsibility.

```text
┌──────────────┐   HTTPS (JSON)   ┌────────────────────┐   SQL/Protocol   ┌────────────┐
│ Client(s)    │ ───────────────▶ │ Backend / API      │ ───────────────▶ │ Database   │
│ Browser, iOS │ ◀─────────────── │ Server(s)          │ ◀─────────────── │            │
│ Android, IoT │                  │ (business logic)   │                  └────────────┘
└──────────────┘                  └────────────────────┘
```

| Tier | Responsibility | Examples |
|---|---|---|
| **Presentation / client** | Render UI, gather input, local state, call APIs | React app, iOS app, device firmware |
| **Application / backend** | Authentication, validation, business rules, orchestration | Spring Boot, Express, Django, Go services |
| **Data** | Durable storage and retrieval | PostgreSQL, Redis, object storage |

Why each arrow exists: client→backend because clients are *untrusted and constrained* (they can't hold the database or secrets); backend→DB because the backend *enforces rules* before touching data. **The database should never be directly reachable by clients.**

### 2.2.2 Frontend vs backend: what runs where?
- **Frontend**: runs in the user's environment (browser, phone). Anything there can be **inspected and modified by the user**. *Never trust it for security or correctness.* Use it for UX and optimistic updates.
- **Backend**: runs in your environment; the **only place** to enforce authorization, validation, rate limits, and invariants ("balance can't go negative").
- **Rule:** *validate everywhere for UX; enforce only on the server.*

### 2.2.3 Client types and their constraints
| Client | Constraints/considerations |
|---|---|
| **Browser (SPA/SSR)** | Same-origin policy, CORS, cookies, CSRF/XSS exposure |
| **Mobile app** | Flaky networks, battery, app-store release cycles (old versions live for *years* → **backward-compatible APIs**), push notifications |
| **Device/IoT** | Tiny resources, intermittent connectivity, firmware updates are risky and slow → stable binary contracts, versioning |
| **Server-to-server** | High volume, strict SLAs, mTLS, structured contracts (gRPC) |
| **Third-party developers** | Strong versioning, documentation, rate limits, keys |

**Insight:** *mobile apps and devices make API backward compatibility a hard requirement.* You can't force users to update, so the server must serve v1 clients for years while evolving.

---

## 2.3 HTTP APIs and REST

### 2.3.1 Problem → naive → better
**Problem:** a client and server (written by different teams, in different languages) need a shared contract.
**Naive:** custom endpoints named after functions (`/doCreateTaskNow`, `/getAllTheTasksForUser?id=...`) with ad-hoc formats.
**Why it breaks:** inconsistent, hard to cache, hard to evolve, can't use generic tooling (proxies, caches, SDK generators).
**Better:** a uniform, resource-oriented convention built on HTTP semantics: **REST** (Representational State Transfer, Fielding's dissertation).

### 2.3.2 REST principles (practical form) `[MK]`
1. **Resources** identified by URLs (nouns): `/users/42`, `/users/42/tasks`, `/tasks/9`.
2. **Uniform interface:** use HTTP methods consistently.
3. **Representations:** the same resource may be JSON, XML, etc. (content negotiation).
4. **Stateless interactions:** each request carries everything needed; the server doesn't remember the client between requests (§2.6).
5. **Cacheability:** responses declare whether they can be cached.
6. **Layered system:** clients can't tell if they talk to the origin, a proxy, or a CDN.

> Most "REST APIs" in industry are "resource-oriented JSON over HTTP." Strict HATEOAS (hypermedia links driving the client) is rarely used. Don't be dogmatic; be consistent.

### 2.3.3 Methods, safety, and idempotency `[MK]`

| Method | Meaning | Safe (no side effects) | Idempotent | Typical success |
|---|---|---|---|---|
| **GET** | Read | ✔ | ✔ | 200 |
| **HEAD** | Headers only | ✔ | ✔ | 200 |
| **POST** | Create / run an action | ✘ | **✘** | 201 / 202 |
| **PUT** | Replace the *entire* resource at this URL (or create at a known URL) | ✘ | ✔ | 200 / 204 |
| **PATCH** | Partial update | ✘ | Not guaranteed (`set x=5` is; `increment x` is not) | 200 |
| **DELETE** | Remove | ✘ | ✔ (second call → 404 or 204, but state same) | 204 |

**Why idempotency matters:** retries are inevitable (timeouts ambiguity, Ch 1). *Idempotent methods are safe to retry; POST is not.* We'll fix POST in §2.7.

### 2.3.4 Designing resource URLs `[MK]`

```text
GET    /v1/tasks?status=open&limit=20&cursor=abc      list/filter/paginate
POST   /v1/tasks                                       create
GET    /v1/tasks/{id}                                  read one
PUT    /v1/tasks/{id}                                  replace
PATCH  /v1/tasks/{id}                                  partial update
DELETE /v1/tasks/{id}                                  delete
POST   /v1/tasks/{id}/complete                         action that isn't plain CRUD (acceptable)
GET    /v1/users/{uid}/tasks                           sub-collection
```
**Guidelines**
- Nouns, plural collections; lowercase, hyphens.
- Don't encode actions in URLs unless the operation isn't naturally CRUD (then `POST /resource/{id}/action`).
- Use query params for filtering/sorting/pagination; path for identity.
- IDs: opaque strings/UUIDs, **not** sequential integers if enumeration is a risk.
- Keep nesting shallow (≤2 levels).

### 2.3.5 Status codes and error design `[MK]`

Use status codes *meaningfully* so clients, proxies, and monitors can act on them.
| Situation | Code |
|---|---|
| Created a resource | 201 + `Location` header |
| Accepted for async processing | 202 (+ a status URL) |
| Validation failure | 400 / 422 with field-level errors |
| Not authenticated | 401 |
| Authenticated but not allowed | 403 |
| Doesn't exist | 404 |
| Conflict (version mismatch, duplicate) | 409 |
| Precondition failed (`If-Match`) | 412 |
| Rate limited | 429 + `Retry-After` |
| Server bug | 500 |
| Dependency down / overloaded | 503 + `Retry-After` |

**Error body (consistent shape):**
```json
{
  "error": {
    "code": "VALIDATION_FAILED",
    "message": "title must be 1–200 characters",
    "details": [{"field": "title", "issue": "too_long"}],
    "request_id": "req_7f3a9c"
  }
}
```
Include a **request ID** that ties client reports to server logs/traces (Ch 21). *Never leak stack traces or internals.*

### 2.3.6 Pagination `[MK]`

**Problem:** `GET /tasks` returning 10 million rows kills the server, network, and client.

| Style | Request | Pros | Cons |
|---|---|---|---|
| **Offset/limit** | `?offset=1000&limit=20` | Simple, random page jumps | **Slow** at large offsets (DB scans/skips 1000 rows), **inconsistent** if rows are inserted/deleted between pages (duplicates/skips) |
| **Cursor/keyset** | `?limit=20&cursor=<opaque>` where cursor encodes the last seen sort key, e.g. `(created_at, id)` | Fast (index seek), stable under inserts | No random page access; cursor must be opaque and tamper-safe |
| **Time-based** | `?since=2025-01-01T...` | Great for sync/feeds | Needs monotonic timestamps/ties handled |

Query for keyset: `WHERE (created_at, id) < (:c_ts, :c_id) ORDER BY created_at DESC, id DESC LIMIT 21` (fetch one extra to know if there's a next page).
**Response shape:** `{"items": [...], "next_cursor": "..."}`. For feeds and large tables, use **cursor pagination**.

### 2.3.7 Versioning and evolution `[MK]`
APIs outlive their authors. Rules:
- **Additive changes are safe** (new optional fields, new endpoints); clients must **ignore unknown fields** (tolerant reader).
- **Breaking changes** (removing/renaming fields, changing semantics) need a **new version**: `/v2/...` or a header (`Accept-Version`).
- Support N and N-1 versions concurrently; publish deprecation timelines; measure usage per version before shutting down.
- For binary/IoT contracts, use schema evolution rules (protobuf: never reuse field numbers; add optional fields).

### 2.3.8 Conditional requests, caching, and concurrency control `[MK/GK]`
- **`ETag`** (version fingerprint) + `If-None-Match` → server replies **304 Not Modified** (no body), saving bandwidth.
- **`Cache-Control: max-age=60, private/public`** lets browsers/CDNs cache.
- **Optimistic concurrency with `If-Match`:** client sends the ETag it last saw; if the resource changed, server returns **412**, preventing *lost updates* (two editors overwrite each other). The API-level analogue of the race condition in Ch 1.

### 2.3.9 Rate limiting headers (preview; Ch 22)
`429 Too Many Requests` with `Retry-After: 30`, plus `RateLimit-Limit/Remaining/Reset` (draft standard) or `X-RateLimit-*`.

### 2.3.10 Interview perspective
When asked "design the API," give: resource model, 5–8 key endpoints with methods/status codes, request/response sketches, pagination approach, auth mechanism, idempotency handling for creates, and versioning/compat notes. Show you think of *retries, partial failure, and evolution*.

---

## 2.4 Authentication and authorization

### 2.4.1 The problem
HTTP is stateless: the server doesn't know that request #2 is from the same person who logged in during request #1. And even if it knows *who*, it must decide *what they may do*.

- **Authentication (AuthN)**: *who are you?*
- **Authorization (AuthZ)**: *what are you allowed to do?*
They're separate. A valid user (authenticated) may still be forbidden (403) from another user's data.

### 2.4.2 Passwords done right `[MK]`
- **Never store plaintext or reversibly encrypted passwords.**
- Store a **salted, slow, one-way hash** using **bcrypt, scrypt, or Argon2id**. *Salt* (unique random per user) defeats rainbow tables; *slow* hashing makes brute force expensive.
- Add **rate limiting/lockouts**, **MFA**, breached-password checks.
- Use TLS everywhere (credentials in transit).
- **Don't invent your own** crypto or auth; use a vetted library or an identity provider (Auth0, Cognito, Keycloak, Firebase Auth, etc.) when appropriate.

### 2.4.3 Approach A: Server-side sessions with cookies `[MK]`

```text
1. POST /login {user, pass}
2. Server verifies → creates session record: session_id = random 128-bit → {user_id, expiry, ...}
3. Server responds: Set-Cookie: sid=9f3c...; HttpOnly; Secure; SameSite=Lax; Path=/; Max-Age=...
4. Browser automatically sends: Cookie: sid=9f3c... with every request to that site
5. Server looks up sid in the session store → knows the user
6. POST /logout → server deletes the session record → immediately invalid
```
**Cookie flags (security-critical):**
- **`HttpOnly`**: JavaScript can't read it (limits XSS theft).
- **`Secure`**: only sent over HTTPS.
- **`SameSite=Lax|Strict`**: limits cross-site sending (CSRF defense).
- Short lifetime + **session rotation** after login (prevent fixation).

**Where's the session store?**
- *In the server's memory*: **stateful server**, which breaks horizontal scaling (§2.6).
- *In a shared store* (Redis/DB): any server can handle any request (stateless app tier; session store becomes a dependency to make highly available).

**Pros:** instant revocation, small cookie, server controls everything. **Cons:** a lookup per request; the store must scale and be reliable; CSRF exposure (cookies auto-sent).

### 2.4.4 Approach B: Token-based (JWT) `[MK]`

**JWT (JSON Web Token)** = three base64url parts: `header.payload.signature`
```text
header:  {"alg":"RS256","typ":"JWT","kid":"key-2025-1"}
payload: {"sub":"user_42","iss":"auth.example.com","aud":"api.example.com",
          "exp":1767225600,"iat":1767222000,"scope":"tasks:read tasks:write"}
signature: sign(header.payload, private_key)
```
- The signature lets *any server with the public key (or shared secret)* **verify authenticity and integrity without a database lookup**: truly stateless verification.
- Payload is **encoded, not encrypted**: anyone can read it. Don't put secrets in it.
- **Always validate:** signature, `exp`, `iss`, `aud`, algorithm allow-list (reject `alg:none`, avoid algorithm-confusion), clock skew tolerance.

**The revocation problem:** a JWT is valid until `exp`. You can't "delete" it like a session.
Mitigations: **short-lived access tokens (5–15 min)** + **refresh tokens** (long-lived, stored securely, **rotated** on use, revocable server-side); a small **denylist** for emergency revocation; key rotation via `kid`.

**Where to store tokens in a browser?** In `HttpOnly` cookies (safer from XSS, needs CSRF protection) vs `localStorage` (readable by any XSS). For native apps: OS secure storage (Keychain/Keystore).

| | Sessions (opaque ID) | JWT access tokens |
|---|---|---|
| Server state | Required (session store) | None for verification (refresh/denylist optional) |
| Revocation | Instant | Hard until expiry |
| Scaling | Needs shared store | Verify anywhere |
| Cross-service use | Needs lookup service | Pass token; services verify locally |
| Size | Small cookie | Larger (bytes of claims) |
| Risk | Store outage/DDoS | Leaked token valid until exp; stale claims (role changes) |
| Good for | Classic web apps | APIs, microservices, mobile, third parties |

Neither is "better." Many systems combine: **opaque tokens at the edge, validated/introspected by an auth service, exchanged for internal JWTs.**

### 2.4.5 OAuth 2.0 and OpenID Connect `[MK/GK]`

**Problem:** "Sign in with Google" / letting a third-party app access your calendar *without giving it your password*.
- **OAuth 2.0** = *delegated authorization*: the user grants an app limited access (scopes) to their resources, via tokens.
- **OpenID Connect (OIDC)** = identity layer on top: adds an **ID token** (JWT) that says *who* the user is.

**Authorization Code flow with PKCE (the modern standard for web/mobile):**
```text
1. App redirects user to Auth Server: /authorize?client_id=...&scope=...&redirect_uri=...
      &code_challenge=HASH(code_verifier)&state=random
2. User authenticates + consents at the Auth Server (the app never sees the password)
3. Auth Server redirects back: redirect_uri?code=ONE_TIME_CODE&state=...
4. App's back end (or app) POSTs code + code_verifier to /token
5. Auth Server returns: access_token (short), refresh_token, id_token
6. App calls API with Authorization: Bearer <access_token>
```
- **PKCE** (code verifier/challenge) prevents code interception, mandatory for public clients (mobile/SPA).
- **State** prevents CSRF on the redirect.
- **Client Credentials flow**: service-to-service (no user).
- **Roles** in the dance: *Resource Owner* (user), *Client* (app), *Authorization Server*, *Resource Server* (API).
- Avoid the old *implicit* flow and *password grant*.

### 2.4.6 Authorization models `[MK]`
- **RBAC** (roles: admin/editor/viewer), **ABAC** (attributes: department, time, resource owner), **ACLs** (per-object lists), **ReBAC** (relationships; e.g., Google Zanzibar-style "can edit doc if member of group that owns it").
- **Enforcement point:** *every request, server-side, per resource*. Check "does this user have access to **this order**?" (not just "is logged in"). Missing object-level checks cause **IDOR / BOLA**, one of the most common real-world API vulnerabilities (change `/orders/5` to `/orders/6`).
- Centralize policy decisions where possible; keep checks close to data access.

### 2.4.7 Service-to-service auth `[GK]`
**mTLS** (both sides present certificates), signed service tokens (JWT with `aud`), short-lived credentials from a workload identity system (SPIFFE/SPIRE, cloud IAM roles). "Internal network = trusted" is an outdated assumption (zero-trust).

### 2.4.8 Failure modes
Session store outage (everyone logged out or can't log in); JWT signing key leak (rotate keys, `kid`); clock skew (valid tokens rejected); refresh token theft (rotation + reuse detection); auth service as a bottleneck/SPOF (cache JWKS keys, local verification); credential stuffing (rate limit, MFA); overly broad scopes.

---

## 2.5 Stateless vs stateful servers: *the* scaling concept

### 2.5.1 The problem (answer to Pause & Predict 2.1, problem 4)
```text
Request 1: Add task → LB → Server A (stores in A's memory)
Request 2: List tasks → LB → Server B (has nothing)  ← "my tasks vanished!"
```
The root cause: **state held inside a particular server instance**.

### 2.5.2 Definitions `[MK]`
- **Stateful server**: remembers client-specific data *between requests* in its own memory/disk. A given client must keep returning to the *same* server.
- **Stateless server**: holds no client-specific data between requests. Everything needed arrives in the request (token, parameters) or lives in *shared external storage*. Any server can serve any request.

**Mental model:** a hotel front desk. A *stateful* receptionist memorizes each guest's preferences: if she's off shift, the guest is stuck. A *stateless* desk looks everything up in the shared booking system: any receptionist works.

### 2.5.3 Why stateless wins for the application tier
| Capability | Stateless | Stateful |
|---|---|---|
| Add servers | Just add; LB spreads load | Rebalancing sessions is hard |
| Remove/replace servers | Trivial (autoscaling, deployments) | Users on that server lose state |
| Failover | Any survivor continues | State lost or must be replicated |
| Deploys | Rolling restarts are easy | Sessions disrupted |
| Load balancing | Any algorithm | Needs **sticky sessions** (uneven load) |

> **Rule:** *Keep app servers stateless; push state into specialized stateful systems built to hold it (databases, caches, object storage).* Then your hardest problems are concentrated in a few well-understood components.

### 2.5.4 Where does the state go?
| State | Move it to |
|---|---|
| User sessions | Shared store (Redis) or signed tokens (JWT) |
| Uploaded files | Object storage (S3-like) |
| Shopping cart | DB/Redis (or client-side with server sync) |
| In-memory counters/rate limits | Redis or per-key atomic stores |
| Background job state | Queue/DB |
| WebSocket connection state | *Genuinely stateful*: handled specially (§2.8) |

### 2.5.5 Truly stateful components still exist
Databases, caches, message brokers, WebSocket gateways, game servers, session-affinity-based systems: these are *stateful by nature*. Their scaling requires replication, partitioning, and care (Chapters 8–13). The strategy: **isolate statefulness**.

### 2.5.6 Sticky sessions: a crutch
If you can't be stateless yet, the LB can route a user consistently to the same server (cookie/IP hash). Costs: uneven load, state lost when that server dies, awkward deploys, scaling-in disrupts users. Treat as temporary.

### 2.5.7 Stateless doesn't mean "no state in the system"
It means *this tier* doesn't own it. The system as a whole still has state; you've just placed it where it's managed properly.

---

## 2.6 The tiny web app, evolved (TaskBoard architecture)

```text
v0   Browser ─▶ One process (UI + logic + RAM)
v1   Browser ─▶ API server ─▶ SQL DB                       ← durability, multiple clients
v2   + Object storage for attachments                      ← files out of the app/DB
v3   + Session store (Redis) / JWT                         ← statelessness
v4   Many API servers ◀── Load balancer                    ← scaling + failover
v5   + Cache (read-heavy lists)                            ← latency/DB load
v6   + Background worker + queue (email reminders)         ← slow work off the request path
v7   + CDN for static assets                               ← global latency
v8   + API gateway (auth, rate limit, routing, TLS)        ← cross-cutting concerns
```
Each step is a *response to a problem* (the spine again). We're still at the "baseline" stage; chapters 6–13 deepen each step.

---

## 2.7 Idempotency in APIs (answer to Pause & Predict 2.1, problem 7)

### 2.7.1 The problem
The train scenario:
```text
Client ── POST /tasks {"title":"Buy milk"} ─▶ Server  (creates task #101)
Client ◀── response lost (tunnel) ──────────┘
Client: timeout → retries
Client ── POST /tasks {"title":"Buy milk"} ─▶ Server  (creates task #102)    ← duplicate!
```
For tasks it's an annoyance. For payments or orders it's money. The general root cause: **a non-idempotent operation + ambiguous failure + retry**.

### 2.7.2 Naive fixes and why they fail
- **"Don't retry."** Then lost responses leave the client in limbo (did it work?).
- **"Disable the button after tap."** Only protects against double-taps in one UI, not network retries, app restarts, or multiple clients.
- **Dedup by content (same title within 10 s).** Legitimate repeated requests get dropped; fragile.

### 2.7.3 Better: Idempotency keys `[MK]`

The client generates a unique key (UUID) *per logical operation* and sends it with the request. The server remembers the key and the result.

```text
POST /v1/payments
Idempotency-Key: 6f1c2a54-91de-4b8e-9c2d-0a7f3d1e5b11
{"amount": 500, "currency": "INR", "to": "acct_9"}
```
Server logic:
```text
begin transaction
  row = SELECT * FROM idempotency WHERE key = :k AND client = :c FOR UPDATE   (or INSERT with unique constraint)
  if row exists:
      if row.request_hash != hash(this request): return 422 "key reused with different payload"
      if row.status == COMPLETED: return row.stored_response (same status code & body)
      if row.status == IN_PROGRESS: return 409 (or wait / 202)
  else:
      insert (key, client, request_hash, status=IN_PROGRESS)
      perform the operation (within the same transaction when possible)
      store response; status = COMPLETED
commit
```
**Details that matter**
- **Atomicity:** the "perform operation" and "record result" should commit together (same DB transaction) so a crash can't leave "done but unrecorded."
- **Scope** keys per client/account; set a **TTL** (e.g., 24 h) to bound storage.
- **Request fingerprinting** detects key misuse.
- **Concurrent duplicates** (two retries racing): a unique constraint/lock ensures one executes, the other waits or gets 409.
- When the downstream is a third party (payment processor), pass *their* idempotency key through, otherwise a retry between your service and theirs can still double-charge.
- This is how Stripe-style APIs work `[Public]`.

### 2.7.4 Other idempotency techniques
- **Natural idempotency**: `PUT /tasks/{client_generated_id}`: client picks the ID; repeating creates/overwrites the *same* task.
- **Unique constraints** (e.g., `UNIQUE(order_id, event_type)`).
- **Conditional writes / compare-and-set** (`UPDATE ... WHERE version = :v`).
- **State machines** (transition `PENDING→PAID` only once).
- **Deduplication at consumers** by message ID (Ch 12).

### 2.7.5 What the client does
Generate the key once, **persist it with the pending operation**, reuse it on every retry (including after app restart). Retry with backoff + jitter until success or a definitive failure.

---

## 2.8 Real-time communication `[MK]`

### 2.8.1 The problem
HTTP is client-initiated. How does a server tell a client "new message!" or "price changed!"?

### 2.8.2 Options

**1) Short polling:** client asks every N seconds.
```text
GET /updates?since=123   (every 5 s)
```
Simple; works everywhere. **Wasteful** (most polls return nothing), latency up to N seconds, load scales with *clients × frequency* even when nothing happens.

**2) Long polling:** client asks; server **holds the request open** until there's data or a timeout (~30 s); client immediately re-asks.
Lower latency, fewer empty responses; each client still holds a connection; HTTP overhead per message; proxies/timeouts complicate.

**3) Server-Sent Events (SSE):** one long-lived HTTP response; server streams `text/event-stream` events. **One-way** (server→client), auto-reconnect with `Last-Event-ID`, works over HTTP/2, simple. Great for notifications, live feeds, progress.

**4) WebSocket:** upgrade an HTTP connection (`101 Switching Protocols`) to a **persistent, full-duplex** channel. Low per-message overhead, bidirectional. Ideal for chat, collaborative editing, games, live dashboards.

**5) Mobile push (APNs/FCM):** OS-level channel when the app isn't running; payloads small; delivery not guaranteed. Pair with WebSockets: *push wakes the app; the socket carries real-time flow.*

| | Short poll | Long poll | SSE | WebSocket | Push |
|---|---|---|---|---|---|
| Direction | C→S | C→S (server holds) | S→C | Both | S→device |
| Latency | Poll interval | Low | Low | Lowest | Variable |
| Connection cost | Many short | 1 held per client | 1 held | 1 held | OS-managed |
| Proxy/LB friendliness | ✔✔ | ✔ | ✔ | Needs LB support, idle timeouts | n/a |
| Complexity | Lowest | Low | Low | Higher | Platform-specific |
| Best for | Infrequent updates | Legacy/real-time-lite | Notifications/streams | Chat/collab/games | Offline/background |

### 2.8.3 System-design implications of persistent connections
Persistent connections are **stateful**: a user is "attached" to one specific gateway server.
- **Connection scaling:** each server may hold 10⁵+ connections (event loops, OS tuning: file descriptors, memory ≈ tens of KB per connection). 10 M concurrent users ⇒ tens to hundreds of gateway nodes.
- **Routing a message to a user:** you need a **registry** (user → gateway node), e.g., in Redis; or broadcast via pub/sub where each gateway subscribes to channels for its users.
- **Deploys and failures** drop connections → clients **reconnect with backoff + jitter** (avoid reconnection storms), then **resync from last-seen ID/offset**.
- **Idle timeouts** at LBs/NATs → heartbeats (ping/pong) every 20–30 s.
- **Backpressure:** slow clients must not exhaust server memory: bounded send buffers, drop/close.
- **Fan-out:** one event to many sockets.
- **Authentication** at connect time and periodic revalidation.
We'll apply all of this in the chat design (Chapter 25/27).

---

## 2.9 Communication styles: REST vs gRPC vs GraphQL `[MK/GK]`

| | REST/JSON | gRPC | GraphQL |
|---|---|---|---|
| Transport | HTTP/1.1 or 2 | HTTP/2 | HTTP (usually POST) |
| Payload | JSON (text) | Protobuf (binary, compact) | JSON |
| Contract | OpenAPI (optional) | `.proto` (strict, codegen) | Typed schema |
| Streaming | Limited (SSE/WS separate) | **Built-in** (unary, server/client/bidi streaming) | Subscriptions (separate) |
| Browser support | Native | Needs gRPC-Web proxy | Native |
| Over/under-fetching | Common issue | Defined by messages | **Client chooses fields** |
| Caching | Easy (HTTP caches, CDN) | Harder | Harder (single endpoint; persisted queries help) |
| Best for | Public APIs, simple CRUD, broad compatibility | **Internal service-to-service**, low latency, streaming, polyglot | Aggregating many backends for varied frontends |
| Risks | Chattiness, version sprawl | Tooling/debugging, browser limits | Complexity, N+1 queries, expensive-query abuse (need depth/cost limits) |

**Guidance:** REST for external/public; gRPC inside the backend; GraphQL (or a BFF) when many clients need tailored payloads. Don't adopt for fashion; choose by requirements (latency, compatibility, team skills).

**Other protocols to recognize:** **Webhooks** (server-to-server callbacks; require signature verification and retries), **MQTT/AMQP** (messaging), **SOAP** (legacy).

### Backend for Frontend (BFF) `[GK]`
A thin per-client-type API layer (mobile BFF, web BFF) that aggregates and shapes backend calls, so a mobile screen needs *one* round trip instead of six (recall Ch 1's latency math).

---

## 2.10 Cross-cutting concerns in the API layer (preview)

| Concern | Where it lives | Chapter |
|---|---|---|
| TLS termination | LB/gateway/CDN | 7 |
| AuthN | Gateway + services | 22 |
| Rate limiting | Gateway + per-service | 22, 25 |
| Request validation | Service | — |
| Logging/tracing (request IDs) | Everywhere | 21 |
| Caching (HTTP) | CDN/gateway/browser | 8, 11 |
| CORS, security headers | Gateway/app | 22 |
| Compression (gzip/brotli) | Edge | 11 |
| Idempotency | App/DB | 14 |
| Circuit breaking, timeouts | Clients/mesh | 18 |

---

## 2.11 Interview and design lens

- When you draw "API servers," immediately say: **"stateless, behind a load balancer; sessions/JWT; state in DB/cache/object storage."**
- For any **create** operation that costs money or is user-visible-duplicate-sensitive, **mention idempotency keys**.
- For **list** endpoints, **cursor pagination**.
- For **mobile/IoT**, mention **backward-compatible versioning** and **retry with backoff**.
- For **real-time**, compare the options and pick based on direction, scale, and infrastructure.
- Consider **who calls the API** (humans, devices, partners) because it changes auth and limits.

---

## 2.12 Code lab (Java 17+, no external libraries)

### Lab: a tiny task API with idempotency keys, pagination, and ETags

Run it, then use `curl` to experiment (including simulating retries).

```java
import com.sun.net.httpserver.*;
import java.io.*;
import java.net.*;
import java.nio.charset.StandardCharsets;
import java.util.*;
import java.util.concurrent.*;
import java.util.concurrent.atomic.AtomicLong;

public class TaskApi {
    // --- "database" (in-memory for the lab; in real life this is external, shared storage) ---
    record Task(long id, String title, boolean done, long version) {}
    static final ConcurrentSkipListMap<Long, Task> tasks = new ConcurrentSkipListMap<>();
    static final AtomicLong ids = new AtomicLong(100);

    // idempotency store: key -> stored response (status + body)
    record Stored(int status, String body, String requestFingerprint) {}
    static final ConcurrentHashMap<String, Stored> idem = new ConcurrentHashMap<>();
    static final ConcurrentHashMap<String, Object> idemLocks = new ConcurrentHashMap<>();

    public static void main(String[] args) throws Exception {
        HttpServer s = HttpServer.create(new InetSocketAddress(8080), 0);
        s.setExecutor(Executors.newFixedThreadPool(16));
        s.createContext("/v1/tasks", TaskApi::handle);
        s.start();
        System.out.println("Listening on http://localhost:8080/v1/tasks");
    }

    static void handle(HttpExchange ex) throws IOException {
        try {
            String method = ex.getRequestMethod();
            String path = ex.getRequestURI().getPath();          // /v1/tasks or /v1/tasks/{id}
            String[] parts = path.split("/");
            boolean isCollection = parts.length == 3;
            if (isCollection && method.equals("GET"))  { list(ex); return; }
            if (isCollection && method.equals("POST")) { create(ex); return; }
            if (!isCollection && method.equals("GET")) { getOne(ex, Long.parseLong(parts[3])); return; }
            if (!isCollection && method.equals("PUT")) { replace(ex, Long.parseLong(parts[3])); return; }
            send(ex, 405, "{\"error\":\"method_not_allowed\"}", null);
        } catch (NumberFormatException e) {
            send(ex, 400, "{\"error\":\"bad_id\"}", null);
        } catch (Exception e) {
            send(ex, 500, "{\"error\":\"internal\"}", null);
        }
    }

    // --- POST /v1/tasks with Idempotency-Key ---
    static void create(HttpExchange ex) throws IOException {
        String key = ex.getRequestHeaders().getFirst("Idempotency-Key");
        String body = new String(ex.getRequestBody().readAllBytes(), StandardCharsets.UTF_8);
        String title = extractTitle(body);
        if (title == null || title.isBlank() || title.length() > 200) {
            send(ex, 422, "{\"error\":\"title must be 1-200 chars\"}", null); return;
        }
        if (key == null) {                                        // no key: not retry-safe
            Task t = insert(title);
            send(ex, 201, toJson(t), "/v1/tasks/" + t.id()); return;
        }
        String fingerprint = Integer.toHexString(body.hashCode());
        Object lock = idemLocks.computeIfAbsent(key, k -> new Object());
        synchronized (lock) {                                     // serialize concurrent duplicates per key
            Stored prior = idem.get(key);
            if (prior != null) {
                if (!prior.requestFingerprint().equals(fingerprint)) {
                    send(ex, 422, "{\"error\":\"idempotency key reused with different payload\"}", null);
                } else {                                          // replay the original response
                    ex.getResponseHeaders().add("Idempotent-Replay", "true");
                    send(ex, prior.status(), prior.body(), null);
                }
                return;
            }
            Task t = insert(title);                               // do the work exactly once
            String resp = toJson(t);
            idem.put(key, new Stored(201, resp, fingerprint));    // record result (same critical section)
            send(ex, 201, resp, "/v1/tasks/" + t.id());
        }
    }

    // --- GET /v1/tasks?limit=2&cursor=<lastId> : keyset pagination ---
    static void list(HttpExchange ex) throws IOException {
        Map<String, String> q = query(ex.getRequestURI().getRawQuery());
        int limit = Math.min(Integer.parseInt(q.getOrDefault("limit", "20")), 100);
        long after = Long.parseLong(q.getOrDefault("cursor", "0"));
        List<Task> page = new ArrayList<>();
        for (Task t : tasks.tailMap(after, false).values()) {
            if (page.size() == limit + 1) break;                  // fetch one extra to detect "has next"
            page.add(t);
        }
        boolean hasNext = page.size() > limit;
        if (hasNext) page.remove(page.size() - 1);
        StringBuilder sb = new StringBuilder("{\"items\":[");
        for (int i = 0; i < page.size(); i++) { if (i > 0) sb.append(','); sb.append(toJson(page.get(i))); }
        sb.append("],\"next_cursor\":");
        sb.append(hasNext ? "\"" + page.get(page.size() - 1).id() + "\"" : "null").append('}');
        send(ex, 200, sb.toString(), null);
    }

    // --- GET one with ETag / If-None-Match ---
    static void getOne(HttpExchange ex, long id) throws IOException {
        Task t = tasks.get(id);
        if (t == null) { send(ex, 404, "{\"error\":\"not_found\"}", null); return; }
        String etag = "\"" + t.version() + "\"";
        if (etag.equals(ex.getRequestHeaders().getFirst("If-None-Match"))) {
            ex.getResponseHeaders().add("ETag", etag);
            ex.sendResponseHeaders(304, -1); ex.close(); return;
        }
        ex.getResponseHeaders().add("ETag", etag);
        ex.getResponseHeaders().add("Cache-Control", "private, max-age=5");
        send(ex, 200, toJson(t), null);
    }

    // --- PUT with If-Match: optimistic concurrency (prevents lost updates) ---
    static void replace(HttpExchange ex, long id) throws IOException {
        String ifMatch = ex.getRequestHeaders().getFirst("If-Match");
        String body = new String(ex.getRequestBody().readAllBytes(), StandardCharsets.UTF_8);
        String title = extractTitle(body);
        synchronized (tasks) {                                    // stand-in for a DB row lock / conditional update
            Task cur = tasks.get(id);
            if (cur == null) { send(ex, 404, "{\"error\":\"not_found\"}", null); return; }
            if (ifMatch != null && !ifMatch.equals("\"" + cur.version() + "\"")) {
                send(ex, 412, "{\"error\":\"precondition_failed: resource changed\"}", null); return;
            }
            Task upd = new Task(id, title, cur.done(), cur.version() + 1);
            tasks.put(id, upd);
            ex.getResponseHeaders().add("ETag", "\"" + upd.version() + "\"");
            send(ex, 200, toJson(upd), null);
        }
    }

    // --- helpers ---
    static Task insert(String title) { long id = ids.incrementAndGet(); Task t = new Task(id, title, false, 1); tasks.put(id, t); return t; }
    static String toJson(Task t) { return "{\"id\":" + t.id() + ",\"title\":\"" + t.title().replace("\"","\\\"") + "\",\"done\":" + t.done() + ",\"version\":" + t.version() + "}"; }
    static String extractTitle(String body) { int i = body.indexOf("\"title\""); if (i < 0) return null; int a = body.indexOf('"', body.indexOf(':', i) + 1); int b = body.indexOf('"', a + 1); return (a < 0 || b < 0) ? null : body.substring(a + 1, b); }
    static Map<String,String> query(String raw) { Map<String,String> m = new HashMap<>(); if (raw == null) return m; for (String kv : raw.split("&")) { String[] p = kv.split("=", 2); m.put(URLDecoder.decode(p[0], StandardCharsets.UTF_8), p.length > 1 ? URLDecoder.decode(p[1], StandardCharsets.UTF_8) : ""); } return m; }
    static void send(HttpExchange ex, int code, String body, String location) throws IOException {
        byte[] b = body.getBytes(StandardCharsets.UTF_8);
        ex.getResponseHeaders().add("Content-Type", "application/json");
        if (location != null) ex.getResponseHeaders().add("Location", location);
        ex.sendResponseHeaders(code, b.length);
        try (OutputStream os = ex.getResponseBody()) { os.write(b); }
    }
}
```

**Experiments (use `curl`)**
```bash
# 1) Without idempotency key: retries create duplicates
curl -s -XPOST localhost:8080/v1/tasks -d '{"title":"Buy milk"}'
curl -s -XPOST localhost:8080/v1/tasks -d '{"title":"Buy milk"}'          # → 2 tasks

# 2) With key: retries are safe (second call returns the *same* task, header Idempotent-Replay)
curl -si -XPOST localhost:8080/v1/tasks -H 'Idempotency-Key: abc-1' -d '{"title":"Pay rent"}'
curl -si -XPOST localhost:8080/v1/tasks -H 'Idempotency-Key: abc-1' -d '{"title":"Pay rent"}'
curl -si -XPOST localhost:8080/v1/tasks -H 'Idempotency-Key: abc-1' -d '{"title":"Different"}'   # → 422

# 3) Cursor pagination
curl -s 'localhost:8080/v1/tasks?limit=2'
curl -s 'localhost:8080/v1/tasks?limit=2&cursor=102'

# 4) ETag + conditional GET
curl -si localhost:8080/v1/tasks/101                                  # note ETag
curl -si localhost:8080/v1/tasks/101 -H 'If-None-Match: "1"'          # 304

# 5) Optimistic concurrency
curl -si -XPUT localhost:8080/v1/tasks/101 -H 'If-Match: "1"' -d '{"title":"A"}'    # 200, version 2
curl -si -XPUT localhost:8080/v1/tasks/101 -H 'If-Match: "1"' -d '{"title":"B"}'    # 412 (stale)
```
**Questions for reflection**
1. What *breaks* in this lab if you run **two instances** behind a load balancer? (The idempotency map and tasks are in each instance's memory: state in the app tier!) Where must they live instead?
2. The idempotency lock uses `synchronized` per key. What replaces it in a multi-server deployment? (A DB unique constraint/row lock or a Redis `SET NX` plus a durable store.)
3. What if the server crashes after `insert` but before `idem.put`? (The in-memory lab has a tiny window; in a DB, put both in one transaction.)
4. Add a TTL sweeper for old idempotency keys. What's a good TTL, and why?

---

## 2.13 Common misconceptions

1. **"REST means JSON over HTTP."** It's a style; many "REST" APIs are loosely resource-oriented.
2. **"POST is for create, PUT is for update"** (roughly) but the real distinction: PUT is **idempotent replace at a known URL**; POST is non-idempotent.
3. **"JWT is encrypted."** It's signed, not encrypted (unless JWE). Don't put secrets in it.
4. **"JWTs are always better than sessions."** They trade revocation ability for statelessness; pick per need.
5. **"Authentication = authorization."** Logged in ≠ allowed.
6. **"The frontend validates, so the backend needn't."** The client is attacker-controlled.
7. **"Stateless means no database."** It means the *server tier* keeps no per-client state locally.
8. **"Offset pagination is fine."** Not for large/changing datasets.
9. **"Retries are safe."** Only for idempotent operations or with idempotency keys.
10. **"WebSockets replace HTTP."** They complement it; most traffic is still request/response.
11. **"Internal services don't need auth."** Zero-trust: authenticate everything.
12. **"We can break the API; clients will update."** Mobile and device clients linger for years.

---

## 2.14 Chapter summary

- A web app separates **clients, backend, and data**; the backend is the trust boundary, since clients are untrusted.
- **REST-style APIs**: resources + HTTP methods + status codes; design for **pagination (cursor)**, **consistent errors**, **versioning/compatibility**, **conditional requests**.
- **AuthN vs AuthZ**: sessions (stateful, revocable) vs JWTs (stateless verify, hard revoke) vs OAuth2/OIDC (delegated, standard flows); enforce **object-level authorization** server-side.
- **Stateless app servers** are the foundation of horizontal scaling and failover; push state to DB, cache, object storage, session store.
- **Idempotency keys** make non-idempotent operations safe under retries; the root problem is *ambiguous failure + retry*.
- **Real-time**: polling < long polling < SSE/WebSocket; persistent connections are stateful and need registries, heartbeats, reconnection with jitter, backpressure.
- **REST vs gRPC vs GraphQL**: choose by audience and needs.

### Mental models
| Concept | Mental model |
|---|---|
| Backend as trust boundary | Bank teller behind glass: customers can ask, only the teller touches the vault |
| Stateless server | Hotel front desk using the shared booking system |
| Idempotency key | A receipt number: showing it twice doesn't buy twice |
| JWT | A signed concert wristband: staff can verify it without calling HQ; but you can't cancel one already issued until it expires |
| OAuth | A valet key: limited access without giving the master key |
| Cursor pagination | A bookmark rather than "page 4,000" |

---

## 2.15 Exercises

### A. Conceptual
1. Draw browser → API → backend → database. Explain why the DB isn't exposed to clients.
2. Why must validation and authorization be enforced on the server even if the client does them?
3. For each of GET, POST, PUT, PATCH, DELETE: safe? idempotent? Why does it matter for retries?
4. What status code for: (a) created; (b) invalid input; (c) not logged in; (d) logged in but not allowed; (e) resource missing; (f) conflicting edit; (g) rate-limited; (h) dependency down?
5. Offset vs cursor pagination: three differences. When is each acceptable?
6. What changes are backward-compatible for an API? Which are breaking? Why do mobile apps make this critical?
7. Explain `HttpOnly`, `Secure`, `SameSite`. What attack does each reduce?
8. Compare sessions and JWTs on revocation, scaling, size, and risk.
9. Why short-lived access tokens + refresh tokens? What's refresh-token rotation?
10. In OAuth 2.0 authorization code + PKCE, who sees the user's password? What does PKCE prevent?
11. What is IDOR/BOLA? Give an example and the fix.
12. Define stateless and stateful servers. Give two scaling consequences of each.
13. List four kinds of state and where each should live in a scalable design.
14. Why are sticky sessions a "crutch"?
15. Explain how idempotency keys work end-to-end, including concurrent duplicates and payload mismatch.
16. Short polling vs long polling vs SSE vs WebSocket: choose for (a) live sports scores to 5 M viewers; (b) chat; (c) report generation progress; (d) stock-trading UI.
17. Why are WebSocket gateways stateful, and what design consequences follow (routing, deploys, reconnects)?
18. REST vs gRPC vs GraphQL: pick one for (a) public developer API; (b) internal microservices; (c) mobile app that needs tailored aggregated data.
19. What is a BFF and how does it relate to latency (Ch 1)?
20. What is ETag/If-None-Match and If-Match? Which problem does each solve?

### B. Design
21. **Design the API** for a "Notes" app: users, notes, tags, sharing. Provide resources, 8 endpoints, status codes, pagination, error format, and auth approach.
22. **Idempotent payments:** write the server-side algorithm for `POST /payments` with an idempotency key, including failure cases (crash mid-way, concurrent duplicates, third-party timeout).
23. **Migration:** a stateful monolith stores sessions in memory and files on disk. Plan the steps to make it horizontally scalable with zero downtime.
24. **Auth design:** a company has a web app, iOS/Android apps, and 20 internal microservices. Propose the authentication/authorization architecture (tokens, flows, where verification happens).
25. **Real-time design:** design a live "order status" tracker for 2 M concurrent users. Choose the transport, connection routing, reconnect strategy, and fallback.

### C. Debugging
26. A user reports: "I added a task, refreshed, and it's gone, but only sometimes." Architecture: 3 API servers behind a round-robin LB; tasks cached in each server's memory and written to the DB asynchronously. Explain the bug and fix.
27. After a deploy, all users were logged out. Cause hypotheses (sessions vs JWT designs)?
28. An API's p99 latency spikes when clients request page 5,000 of a list endpoint. Why, and how do you fix it?
29. A support engineer notes that changing `/api/invoices/123` to `/api/invoices/124` shows another customer's invoice. Classify and fix.
30. A WebSocket service crashed; when it came back, 400k clients reconnected within 2 seconds, and it crashed again. Explain and prevent.

### D. Interview-style
31. "Design the API for a ride-hailing app's trip request." Present resources, endpoints, idempotency, real-time updates, and versioning.
32. "How would you handle duplicate requests?" Give a 2-minute answer.
33. "Sessions or JWT?" Give a balanced answer with a recommendation for a mobile-first product with microservices.

### E. Lab
34. Run the lab. Add `DELETE /v1/tasks/{id}` (idempotent), a `PATCH` for `done`, and TTL expiry of idempotency keys.
35. Simulate two concurrent identical POSTs with the same key (e.g., two terminals/`xargs -P`). Verify exactly one task is created.
36. Replace the in-memory idempotency map with a file or SQLite table using a unique constraint. What new failure modes appear?

### F. Teach-back
37. Explain statelessness to a friend using the hotel analogy. Then explain why it enables autoscaling.

---

## 2.16 Full answer key

**1.** Browser (client) calls the API over HTTPS; the backend validates, authorizes, applies rules, and talks to the DB. The DB isn't exposed because clients are untrusted: exposing it would bypass authorization/validation, expand the attack surface, couple clients to schema, and make rate limiting and auditing impossible.

**2.** Clients can be modified, bypassed (direct HTTP calls), or forged. Client-side checks are UX; only the server can reliably enforce rules and access control.

**3.** GET: safe, idempotent. POST: not safe, not idempotent. PUT: not safe, idempotent. PATCH: not safe, not necessarily idempotent. DELETE: not safe, idempotent. Retries (after ambiguous timeouts) are safe automatically for idempotent methods; POST needs idempotency keys.

**4.** (a) 201; (b) 400/422; (c) 401; (d) 403; (e) 404; (f) 409 (or 412 with If-Match); (g) 429; (h) 503 (or 502/504 if via a gateway).

**5.** Offset: simple/random access, but slow at large offsets, unstable under inserts/deletes. Cursor: fast (index seek), stable, no random page jumps, opaque. Use offset for small admin tables/UI with numbered pages; cursor for feeds, large or changing data, infinite scroll, APIs.

**6.** Compatible: adding optional fields/endpoints/enum values (if clients tolerate unknown), relaxing validation. Breaking: removing/renaming fields, changing types/semantics, making optional fields required, changing auth. Mobile/IoT clients can't be force-updated, so old versions must continue to work for years.

**7.** `HttpOnly`: JS can't read the cookie (reduces XSS token theft). `Secure`: only HTTPS (prevents sniffing). `SameSite`: restricts cross-site sending (reduces CSRF).

**8.** Revocation: sessions instant; JWT hard until expiry. Scaling: sessions need a shared store; JWT verifies anywhere. Size: session cookie small; JWT larger. Risk: session store outage/DoS; JWT theft valid until exp, stale claims, key compromise.

**9.** Short access tokens limit the damage window of theft; refresh tokens allow renewal without re-login and can be revoked server-side. Rotation issues a new refresh token on each use and invalidates the old one; reuse of an old token signals theft → revoke the family.

**10.** Only the authorization server sees the password. PKCE binds the authorization code to the client that started the flow (verifier/challenge) so an intercepted code can't be redeemed by an attacker.

**11.** Insecure direct object reference / broken object-level authorization: accessing `/orders/6` that belongs to another user. Fix: authorize per object (check owner/permissions on every access), use non-guessable IDs as defense in depth.

**12.** Stateless: no per-client local state; scale out/in freely; any failover target works; easy rolling deploys. Stateful: needs stickiness or state replication; state lost on failure; harder deploys; uneven load.

**13.** Sessions → Redis/DB/JWT; files → object storage; carts → DB/Redis; counters/rate limits → Redis/atomic stores; job state → queue/DB; connection state → gateway registry.

**14.** They hide statefulness: uneven load, session loss on server failure, deploy disruption, scale-in disruption. They postpone, but don't solve, the underlying coupling.

**15.** Client sends a unique key with each logical operation, reusing it on retries. Server atomically records (key, fingerprint, status) and the result with the operation. Duplicate with same payload → return stored result; different payload → 422; concurrent duplicate → one wins (unique constraint/lock), the other waits or 409. TTL for cleanup; forward keys to third parties.

**16.** (a) SSE or push via CDN-friendly streaming/short polling with caching (one-way, huge fan-out; consider edge-cached snapshots). (b) WebSocket (bidirectional, low latency) + push for offline. (c) SSE or polling a status URL (202 + Location). (d) WebSocket (low-latency bidirectional), perhaps with a fallback.

**17.** Each connection is pinned to a specific server (state). Consequences: need a registry (user→gateway) or pub/sub fan-out; deploys/failures disconnect clients → reconnect with backoff+jitter and resync by last-seen ID; heartbeats for NAT/LB timeouts; backpressure for slow clients.

**18.** (a) REST (ubiquitous, cacheable); (b) gRPC (efficient, typed, streaming); (c) GraphQL or a BFF (aggregate, tailored payloads).

**19.** A BFF is an API layer tailored to one client type that aggregates backend calls into fewer round trips, cutting latency (one RTT instead of many, per Ch 1).

**20.** ETag/If-None-Match: conditional GET → 304 saves bandwidth and CPU. If-Match: conditional write → 412 prevents lost updates (optimistic concurrency).

**21 (sketch).**
Resources: `/v1/users/{uid}`, `/v1/notes`, `/v1/notes/{id}`, `/v1/tags`, `/v1/notes/{id}/shares`.
Endpoints: `POST /v1/notes` (201, Idempotency-Key), `GET /v1/notes?tag=&q=&limit=&cursor=` (200), `GET /v1/notes/{id}` (200/404, ETag), `PUT/PATCH /v1/notes/{id}` (If-Match → 412), `DELETE /v1/notes/{id}` (204), `PUT /v1/notes/{id}/tags/{tag}` (idempotent), `POST /v1/notes/{id}/shares` (201; body: user, permission), `DELETE /v1/notes/{id}/shares/{uid}` (204).
Pagination: cursor on `(updated_at, id)`. Errors: JSON error object with request_id. Auth: OAuth2 access tokens (JWT, 10 min) + refresh; object-level authorization on notes (owner or share). Versioning: `/v1`, additive evolution.

**22 (sketch).**
```text
POST /payments (Idempotency-Key K, client C):
 1. In DB txn: INSERT idempotency(C,K,hash,status=IN_PROGRESS) -- unique(C,K).
    If conflict → load row: hash mismatch → 422; COMPLETED → return stored; IN_PROGRESS → 409/poll.
 2. Create payment row state=CREATED in same txn; commit.
 3. Call processor with idempotency key = (C,K) (their key), timeout T.
 4a. Success → txn: state=CAPTURED, store response, status=COMPLETED.
 4b. Definitive failure (declined) → state=FAILED, store response (replay returns same failure).
 4c. Timeout/ambiguous → do NOT blindly mark failed; leave PENDING_UNKNOWN; reconcile by querying the processor with the same key (or webhook); client retries return current state.
 Crash after step 3 before 4: recovery job finds IN_PROGRESS/PENDING rows older than X and reconciles with the processor.
```
**23 (sketch).** (1) Introduce a shared session store (Redis) and dual-read (check Redis, fall back to local) while writing to both; (2) move uploads to object storage via pre-signed URLs, migrate old files in the background and add a fallback path; (3) externalize config/secrets, make instances disposable; (4) put a load balancer in front, start with 2 instances, remove sticky sessions once the session store is authoritative; (5) health checks, graceful shutdown/connection draining; (6) blue/green or rolling deploys; (7) monitor error rates at each step; rollback plan.

**24 (sketch).** Central IdP (OIDC) for user login; Authorization Code + PKCE for web/mobile; short-lived JWT access tokens + rotating refresh tokens; API gateway validates tokens (cached JWKS), applies rate limits and coarse scopes; services verify the JWT locally (audience-checked) and perform object-level authorization; internal service-to-service via mTLS and service tokens (client credentials); central policy/permission service for ReBAC if needed; secrets in a manager; revocation via short TTL + denylist for emergencies.

**25 (sketch).** 2 M concurrent × persistent connections: SSE (one-way status updates) or WebSocket; gateway tier with event-loop servers (~100k connections each → ~20–40 nodes with headroom); orders publish status events to a pub/sub/stream; each gateway subscribes to topics for its connected users (or uses a user→gateway registry); on reconnect the client sends `last_event_id` and receives missed events from a short-retention store; heartbeats at 20–30 s; reconnect with backoff+jitter; fallback to polling a cached status endpoint; autoscale on connection count; CDN-cached "latest status" snapshot for read-heavy cases.

**26.** The write is acknowledged from one server's memory cache and persisted asynchronously; a refresh routed to a different server reads its own (stale/empty) cache or DB not yet updated → "sometimes gone." Fix: stateless servers; write-through to the DB (or at least read-your-writes via the DB/shared cache), shared cache (Redis) instead of per-server memory, async writes only via a durable queue with acknowledgement semantics.

**27.** Sessions stored in server memory (or the local cache) wiped on restart; session store flushed/misconfigured; cookie signing key changed on deploy; JWT signing key rotated without keeping the old public key in JWKS; clock skew; token audience/issuer config changed.

**28.** Offset pagination makes the DB scan and discard 100,000 rows. Fix: keyset/cursor pagination with a proper index (and limits on page size), or precomputed/cached pages.

**29.** IDOR/BOLA (broken object-level authorization). Fix: check that the authenticated user is authorized for invoice 124 on every request; scope queries by tenant/user; use unguessable IDs as defense in depth; add tests/automation.

**30.** A reconnection storm (thundering herd): synchronized reconnects overwhelmed auth/handshake capacity. Prevent: client exponential backoff with jitter, server-side admission control/rate limiting of new connections, connection draining on deploys, spread restarts, keep a warm spare capacity, return `Retry-After`-like hints.

**31 (sketch).** Resources: `/v1/trips` (POST with Idempotency-Key → 201 `{trip_id, status:"SEARCHING"}`), `GET /v1/trips/{id}`, `POST /v1/trips/{id}/cancel`, `GET /v1/drivers/nearby` (internal), real-time via WebSocket/SSE `GET /v1/trips/{id}/events`; driver location updates via a separate device channel; additive versioning; auth: user token; rate-limit trip creation; 409 if the user has an active trip; state machine for trip status.

**32 (sketch).** Explain: duplicates arise from retries/ambiguous timeouts/at-least-once delivery; solution layers: idempotency keys for APIs, natural keys/unique constraints, conditional writes, deduplication tables for consumers, state machines; retention windows; fingerprinting; third-party pass-through.

**33 (sketch).** Balanced: sessions give revocation/simplicity but need a shared store; JWTs allow stateless verification across microservices and mobile. Recommend: short-lived JWT access tokens + rotating refresh tokens (with server-side refresh state), validated at the gateway and services; use opaque tokens externally if instant revocation is a strong requirement.

**34–36.** Self-check. In 35, per-key locking guarantees exactly one insert; with a DB, a unique constraint does it. In 36, new failure modes: constraint-violation handling, lock timeouts, crash between insert and result save (use a transaction), TTL cleanup jobs, clock/time handling.

**37.** Hotel desk analogy: any receptionist can serve you because your reservation is in the shared system; so the hotel can add/remove receptionists freely (autoscaling) and a sick receptionist doesn't strand guests (failover).

---

## 2.17 Flashcards

| Front | Back |
|---|---|
| Why not expose the DB to clients? | Clients are untrusted; backend enforces auth, validation, limits |
| Safe vs idempotent | Safe: no side effects; idempotent: repeat = same effect |
| Which HTTP methods are idempotent? | GET, HEAD, PUT, DELETE (POST not; PATCH depends) |
| 401 vs 403 | Not authenticated vs authenticated but forbidden |
| 409 vs 412 | Conflict vs failed precondition (If-Match) |
| Offset pagination weakness | Slow at depth; unstable under changes |
| Cursor pagination | Keyset-based, fast, stable, opaque cursor |
| Cookie flags | HttpOnly (XSS), Secure (HTTPS), SameSite (CSRF) |
| JWT parts | header.payload.signature (signed, not encrypted) |
| JWT downside | Hard to revoke before `exp` |
| Refresh token rotation | New refresh token on each use; old invalidated; reuse = theft signal |
| OAuth2 vs OIDC | Delegated authorization vs identity layer (ID token) |
| PKCE | Binds auth code to the initiating client; stops code interception |
| IDOR/BOLA | Accessing others' objects by changing IDs; fix with object-level authz |
| Stateless server | No per-client local state; state in shared stores |
| Sticky sessions | LB pins user to a server; a crutch with downsides |
| Idempotency key | Client-generated token per operation; server dedupes/replays result |
| SSE vs WebSocket | One-way server push vs full-duplex |
| Why WS gateways are stateful | Connections pinned to servers; need registry, reconnect/resync |
| gRPC vs REST | Binary/typed/streaming internal vs ubiquitous/cacheable public |
| BFF | Per-client aggregation layer to reduce round trips |
| ETag/If-None-Match | Conditional GET → 304 |
| If-Match | Conditional write → 412; prevents lost updates |

## 2.18 Glossary additions

| Term | Simple Meaning | Technical Meaning | Why It Exists | Example |
|---|---|---|---|---|
| Frontend / Backend | What users see / the server brain | Client-side UI / server-side logic | Separate trust and responsibilities | React / Spring |
| API | The menu | Contract of requests/responses | Decouples teams/clients | `/v1/tasks` |
| REST | Resource-style web API | Resources + HTTP semantics | Uniform, cacheable interface | `GET /users/42` |
| Pagination | Page-by-page lists | Offset/cursor-based slicing | Bound response size | `?cursor=...` |
| Versioning | API editions | Managing breaking changes | Long-lived clients | `/v2/` |
| ETag | Version stamp | Resource validator header | Conditional requests | `ETag: "7"` |
| Session | Server-side login memory | Server-stored identity keyed by ID | Recognize users | `sid` cookie |
| Cookie | Browser-stored note | Small data auto-sent to origin | Maintain state | `Set-Cookie` |
| JWT | Signed token | Self-contained signed claims | Stateless auth | access token |
| OAuth 2.0 / OIDC | Delegated access / identity | Auth framework / identity layer | Third-party access, SSO | "Sign in with Google" |
| PKCE | Anti-interception | Code challenge/verifier extension | Secure public clients | Mobile login |
| RBAC / ABAC | Roles / attributes | Authorization models | Access control | admin/editor |
| IDOR/BOLA | Guess-the-ID flaw | Missing object-level authz | Common vulnerability | `/orders/6` |
| Stateless / Stateful | Forgets / remembers | Per-client local state absent / present | Scaling | API vs WS gateway |
| Sticky session | Pin user to server | Affinity routing | Support stateful servers | Cookie-based LB |
| Idempotency key | Operation receipt | Client token for dedupe | Safe retries | `Idempotency-Key` |
| Long polling | Held request | Server delays response until data | Low-latency w/o sockets | Legacy chat |
| SSE | Server stream | One-way HTTP event stream | Push notifications | Live feed |
| WebSocket | Two-way pipe | Full-duplex persistent connection | Real-time apps | Chat |
| gRPC | Typed fast RPC | Protobuf over HTTP/2 | Efficient internal calls | Microservices |
| GraphQL | Ask for fields | Query language/schema | Flexible client queries | Mobile aggregation |
| BFF | Per-client API | Aggregation layer per client type | Fewer round trips | Mobile BFF |
| Webhook | Callback | Server-to-server HTTP notification | Event delivery | Payment status |

---

**Next: Chapter 3, Relational Databases: Storing Truth.** Why databases exist, tables/keys/SQL, indexes and B-trees, query plans, transactions, ACID and isolation anomalies, locking and MVCC, deadlocks, connection pooling, and how to model data from access patterns, with SQL labs.

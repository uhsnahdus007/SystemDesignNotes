# Chapter 25: Problem Book I (Beginner)

## Chapter card

| | |
|---|---|
| **Objective** | Apply the nine-step method of Chapter 24 to six classic beginner problems: **URL shortener**, **Pastebin**, **rate limiter**, **file upload service**, **chat service**, and **LRU cache**. Each solution is built from requirements and numbers, evolves in justified steps, and is backed by measured experiments from the labs |
| **Why this chapter exists** | These six problems appear in almost every design interview, and between them they exercise the core building blocks: key generation, caching, object storage, atomic counters, resumable transfer, real-time messaging, ordering, and in-memory data structures. Mastering them gives you the vocabulary that later problems (Ch 26–28) combine |
| **Prerequisites** | Chapter 24 (method), and Ch 3–13 (storage, caching, queues, sharding), Ch 18 (reliability) |
| **Study time** | 14–20 hours (attempt each problem *before* reading the solution) |
| **Tier** | `[MK]` |
| **Mastery criteria** | For each problem you can (1) state requirements and numbers in five minutes, (2) draw a justified v1→v4 architecture, (3) explain two non-trivial mechanisms in depth, (4) name the failure modes and the fix, (5) answer the follow-ups, and (6) state the trade-offs in the *choice / reason / cost / alternative* form |

**How to use this chapter.** Read only the *problem statement* of a problem, set a 45-minute timer, and design on paper using the nine steps. Then read the model solution and score yourself with the Chapter 24 rubric. Every solution follows the same layout: **Requirements → Estimates → API and data model → Architecture (evolution) → Deep dives → Failure modes and hardening → Trade-offs → Follow-ups**, followed by a short "what graders look for" note.

---

# Problem 1: URL Shortener (TinyURL / bit.ly)

> **Problem statement.** Design a service that turns long URLs into short ones (`https://sho.rt/aZ3kQ9x`) and redirects visitors from the short URL to the original.

## 1.1 Requirements

**Functional**
- Create a short URL for a long URL (optionally: custom alias, expiry date).
- Redirect: opening the short URL sends the user to the long URL.
- Delete/disable a link. Basic analytics (click counts) is a stretch goal.
- *Out of scope for v1:* user-facing dashboards, QR codes, link previews.

**Non-functional (assumptions I'd confirm)**
- **Scale:** 100 M new URLs/day; reads:writes = 100:1 → ~10 B redirects/day.
- **Latency:** redirect p99 < 50 ms (it is on the critical path of every click).
- **Availability:** redirects 99.99% (a dead short link breaks other people's pages); creation 99.9%.
- **Durability:** a short link must never silently map to the wrong URL or vanish before expiry. Retention: 5 years.
- **Security:** reject malicious/abusive targets; codes should not be trivially enumerable (nice to have).

## 1.2 Estimates (Chapter 24 calculator)

```text
URL shortener       1.2 K writes/s   115.7 K reads/s avg   347.2 K peak reads/s   18.3 TB/yr raw   273.8 TB retained x3 (5 yrs)   0.8 Gbps peak egress   117 app servers   30 GB cache hot set
```
- Writes are tiny (1.2 K/s); the problem is the **read path** at ~350 K redirects/s and **5 years of data** (~180 B rows, ~270 TB replicated).
- **Code length:** 62^7 = 3.5 trillion codes; five years of URLs is 182.5 B = **5.2% of the space** → 7 characters suffice; 6 characters (56.8 B) would run out in **~1.6 years**.
- The app tier is small (≈117 servers at peak): *the hard parts are ID generation, the read path, and storage scale.*

## 1.3 API and data model

```text
POST   /api/v1/urls            {long_url, custom_alias?, expires_at?}   → 201 {short_url, code}
GET    /{code}                                                          → 301/302 Location: long_url   (404 if absent/expired/disabled)
DELETE /api/v1/urls/{code}                                              → 204
GET    /api/v1/urls/{code}/stats                                        → {clicks, ...}   (async-aggregated)
```
- Creation requires auth (API key) and is rate limited (Problem 3). Use an **idempotency key** so a retried create does not mint two codes.
- **Access pattern:** the only hot query is *code → long_url* (a pure key lookup). So the primary store is a **key-value / wide-column store** keyed by `code`:

```text
urls:  code (PK) | long_url | created_at | expires_at | owner_id | status(active/disabled) | flags
```
~500 B per row including overhead. 182.5 B rows over five years: shard by `hash(code)` across thousands of logical shards (Chapter 10) on DynamoDB/Cassandra/Bigtable-class storage, or sharded MySQL. A uniqueness constraint on `code` (conditional put) is the only transactional need.

## 1.4 Architecture: eight versions, each triggered by a number

| Ver. | Design | Trigger to move on |
|---|---|---|
| **v1** | One server, in-memory hash map | Restart loses everything |
| **v2** | One server + relational DB (`code` PK); counter → base62 | One DB/box = single point of failure; deploys cause downtime |
| **v3** | Stateless app servers behind an LB; DB primary + replica; managed backups | Peak 347 K redirects/s ≫ one DB (≈10 K reads/s) |
| **v4** | **Cache** (in-process LRU + Redis) in front of the store; CDN for hot links | Cache misses (36-60% in lab) still need ~125 K+ DB reads/s; write path single counter |
| **v5** | **ID generation service** with range allocation; scrambled codes; conditional put for custom aliases | Central counter becomes a bottleneck/SPOF |
| **v6** | **Async analytics**: click events → log → aggregators (no DB write on the redirect path) | 115 K clicks/s updates would swamp the store |
| **v7** | **Sharded KV store** with 3 replicas across AZs; expiry via TTL | 270 TB of data and 350 K reads/s exceed a replicated relational setup |
| **v8** | **Multi-region**: replicated read path, per-region ID ranges, abuse pipeline, edge caching | Latency for global users; regional failure tolerance |

Final picture:
```text
 Browser ──▶ CDN/edge (hot codes, 60 s TTL) ──▶ LB ──▶ Redirect service (stateless; local LRU) ──▶ Redis (hot set) ──▶ KV store (RF=3, 3 AZs)
 Creator ──▶ API gateway (auth, rate limit) ──▶ Create service ──▶ ID service (ranges) + abuse check ──▶ KV store (conditional put)
 Redirect service ──(async click event)──▶ log (Kafka) ──▶ stream aggregation ──▶ analytics store
```

## 1.4a Deep dive 1: generating short codes

**Options**
| Method | How | Problems |
|---|---|---|
| **Random code + DB check** | Pick a random 7-char code; insert if absent, else retry | Collision rate = n/N: grows with occupancy; extra DB round trip |
| **Hash the URL, truncate** | `base62(md5(url))[0:7]` | Truncation collisions (birthday); same URL → same code (nice) but different URLs may collide: need check + salt/retry |
| **Counter + base62** | `base62(next_id)` | No collisions; **sequential/guessable** codes; central counter must scale |
| **Range-allocated counter + keyed permutation** | Each server gets a block of IDs; scramble the ID with a bijection before encoding | No collisions; unguessable if the permutation is keyed; counter calls drop 1000× |
| **Pre-generated key service (KGS)** | A service fills a pool of unused random codes | Extra component; pool refill and "used" bookkeeping |

**Measured (Lab 1, A–B).**
```text
== A) Random short codes: how often does an insert collide with an existing code? ==
code length   space (62^k)        codes already stored   measured collision rate   theory n/N
     4            14,776,336             100,000            0.7035%                 0.6768%
     4            14,776,336           1,000,000            6.7805%                 6.7676%
     4            14,776,336           5,000,000           33.8650%                33.8379%
     4            14,776,336          10,000,000           67.6610%                67.6758%
     7       3,521,614,606,208       1,000,000,000        (not simulated)               0.0284%   <- expected extra DB round trips per insert = n/N
     7       3,521,614,606,208      18,250,000,000        (not simulated)               0.5182%   <- expected extra DB round trips per insert = n/N
     7       3,521,614,606,208     182,500,000,000        (not simulated)               5.1823%   <- expected extra DB round trips per insert = n/N
hash(url) truncated to 7 chars, birthday bound: P(any collision) after n URLs ~ 1 - exp(-n^2/(2N)):
   n = 100,000 -> P(at least one collision) = 0.1419%  (and EVERY new URL has an n/N chance of colliding, so a DB uniqueness check + retry is mandatory)
   n = 1,000,000 -> P(at least one collision) = 13.2362%  (and EVERY new URL has an n/N chance of colliding, so a DB uniqueness check + retry is mandatory)
   n = 10,000,000 -> P(at least one collision) = 99.9999%  (and EVERY new URL has an n/N chance of colliding, so a DB uniqueness check + retry is mandatory)

== B) Counter-based IDs scrambled by a bijection (no collisions, no sequential guessing) ==
sequential id -> plain base62      scrambled code (a*id+b mod 62^7, a = golden-ratio multiplier)
   1            0000001                 cJr9t8S
   2            0000002                 EdZxlWN
   3            0000003                 qxIlduI
   4            0000004                 TH1ZWID
   5            0000005                 5akNOg8
2,000,000 consecutive ids: duplicate codes = 0 (bijection => 0); adjacent ids sharing a first character = 0.0% (a random sequence would be 1.6%)
(An affine scramble is not secret: two (id, code) pairs reveal a and b. If codes must resist enumeration, use a keyed permutation, e.g. a Feistel network / format-preserving encryption.)
range allocation: app servers fetch a block of IDs from the central counter and hand them out locally
   block      1 -> central counter calls per 1,000,000 IDs: 1,000,000 ; IDs lost (gap) if a server crashes: up to 0
   block    100 -> central counter calls per 1,000,000 IDs:    10,000 ; IDs lost (gap) if a server crashes: up to 99
   block  1,000 -> central counter calls per 1,000,000 IDs:     1,000 ; IDs lost (gap) if a server crashes: up to 999
   block 10,000 -> central counter calls per 1,000,000 IDs:       100 ; IDs lost (gap) if a server crashes: up to 9,999
```
- With a 4-character space, the measured collision rate tracks n/N almost exactly (6.78% measured vs 6.77% at 1 M stored). For the real 7-character space, the expected number of extra DB round trips per insert is only **0.03% at 1 B codes and 5.2% at 182.5 B (our 5-year total)**: *random codes are fine for a long time but their cost rises steadily with occupancy*.
- Hash truncation hits the **birthday bound** early: 7-char prefixes collide somewhere among 1 M URLs with probability 13%, so a uniqueness check plus retry is mandatory.
- The affine bijection `code = base62(a·id + b mod 62^7)` produced **zero duplicates in 2 M consecutive IDs** and codes that look scattered. Caveat: with a golden-ratio multiplier consecutive IDs are spread *too evenly* (a low-discrepancy sequence, 0.0% adjacent first-character matches vs 1.6% for random), and an **affine map is not secret** (two samples reveal `a` and `b`). If enumeration resistance matters, use a **keyed Feistel/format-preserving permutation**.
- **Range allocation**: with blocks of 10,000 IDs the central counter handles **100 calls per million IDs** instead of a million; the cost is up to 9,999 skipped IDs when a server crashes (irrelevant in a 3.5 T space).

**Decision.** Range-allocated counters (prefetching the next block at 50% consumption) + keyed permutation + base62, 7 characters. **Custom aliases** use a **conditional put** (`attribute_not_exists(code)`) on the same table, namespaced so aliases cannot collide with generated codes (e.g., generated codes are always exactly 7 characters from the permutation range; aliases are validated to differ in format or live in a distinct keyspace).

## 1.4b Deep dive 2: the redirect path

- **301 vs 302.** A 301 (permanent) is cached by browsers and proxies: fewer hits on our servers, but we lose click counts, cannot change/disable the link quickly, and wrong redirects persist. A 302 (temporary) is requested every time: full control and accurate analytics, at higher load. **Choice:** 302 by default (or 301 with a short `Cache-Control: max-age` for links that need neither tracking nor mutation).
- **Cache sizing (Lab 1, C).** Zipf(0.9) popularity over 2 M URLs, LRU:
```text
== C) Redirect cache sizing: LRU hit ratio, 2,000,000 short URLs, Zipf(0.9) popularity, 6,000,000 redirects ==
cache size (% of URLs)   entries     memory @ 500 B     hit ratio    database reads/s at 347K peak redirects/s
         0.1%            2,000          1.0 MB         23.9%             264,163
         1.0%           20,000         10.0 MB         40.7%             206,029
         5.0%          100,000         50.0 MB         56.0%             152,656
        10.0%          200,000        100.0 MB         63.7%             125,892
        20.0%          400,000        200.0 MB         71.8%              97,799
```
  Hit ratio grows only logarithmically with cache size (24% at 0.1% of URLs, 64% at 10%). At 347 K peak redirects/s, even a 20% in-process cache leaves **~98 K reads/s** for the lower tiers, so the store must handle ~100 K reads/s itself (easy for a sharded replicated KV store) and the Redis tier (30 GB hot set in the estimate) adds a second level; real link traffic is far more skewed than Zipf 0.9 (a tiny set of viral links dominates), so measured hit ratios are higher; **measure your own distribution**.
- **Viral link / hot key.** A link receiving 500 K req/s hammers one shard. Answer: the mapping is **immutable** (mostly), so cache it at the CDN/edge with a 30–60 s TTL, and coalesce concurrent misses with **single-flight** (Chapter 8). Disabling a link then takes up to the TTL to propagate (purge API for urgent takedowns).
- **Latency budget:** edge hit ~5–20 ms; local LRU hit ~µs; Redis hit ~1 ms; KV read ~5–10 ms; total p99 < 50 ms is achievable.

## 1.4c Deep dive 3: counting clicks without destroying the database

Updating a counter row on every click means **115.7 K writes/s** of read-modify-write on hot rows.
```text
== D) Click counting: one database update per click vs in-memory aggregation, flushed every W seconds (115,700 clicks/s, 2M URLs, Zipf 0.9) ==
approach                                   DB writes per second     distinct URLs per window / clicks per window
update a counter row on every click             115,700
aggregate in memory, flush every 5 s             49,020     245,099 / 578,500  (2.4x fewer writes)
aggregate in memory, flush every 60 s            20,707     1,242,401 / 6,942,000  (5.6x fewer writes)
trade-off: counts lag by up to W seconds and an app-server crash loses the unflushed window unless clicks are also appended to a log/stream (Kafka) and counted downstream.
```
In-memory aggregation flushed every 5 s cuts DB writes by **2.4×**, every 60 s by **5.6×** (clicks concentrate on popular links; the benefit depends on skew and window). The real fix is architectural: the redirect service emits a **click event to a log (Kafka)** *after* responding (fire-and-forget, may drop under overload), and stream jobs aggregate by `(code, minute)` and write **rollups** (and approximate unique visitors with HyperLogLog, Chapter 20). Analytics are eventually consistent and may lose a tiny fraction of clicks; the redirect must never wait on them.

## 1.4d Other design points
- **Expiry and deletion.** Store `expires_at`; reject expired rows on read (lazy check) and let the store's TTL feature or a background sweeper physically delete them. **Do not reuse expired codes for a long time** (months) because CDN/browser caches and printed links may still point at the old destination.
- **Abuse.** Phishing/malware: check targets against safe-browsing lists at creation and periodically after; block redirect loops and links to our own domain; per-account/IP rate limits (Problem 3); CAPTCHA for anonymous creation; a takedown API that disables a code and purges caches.
- **Same long URL, same code?** Optional deduplication via an index on `hash(long_url, owner)`: saves space but adds a read-before-write and privacy concerns across users; usually dedupe per user only.
- **Multi-region.** Replicate the KV store to each region (async); partition the **ID space by region** (each region's allocator owns a sub-range) so creation needs no cross-region coordination; redirects read locally; a newly created link may take a moment to reach other regions (fall back to the home region on a miss).

## 1.5 Failure modes and hardening
| Failure | Handling |
|---|---|
| KV node/AZ failure | RF=3 across AZs; reads/writes at quorum or one-of-three with repair (Chapter 9) |
| ID service down | Local block lasts minutes (block of 10,000 at ~60 IDs/s/server ≈ 170 s); prefetch; fall back to random codes with conditional put |
| Cache stampede after restart | Single-flight, TTL jitter, warm hot keys (Chapter 8) |
| Hot shard | CDN/edge cache + replicas for hot partitions |
| Abuse flood on creation | Rate limits, CAPTCHA, account reputation |
| Wrong redirect bug | Immutable mapping, audit log of changes, canary deploys, integrity checks |
| Analytics pipeline lag | Never on the redirect path; backlog is harmless |

## 1.6 Trade-offs summary
| Decision | Choice | Cost | Alternative / when |
|---|---|---|---|
| Store | Sharded KV | No ad-hoc SQL | Relational + replicas if volume were 100× lower |
| Code generation | Range counters + keyed permutation | Central allocator to run | Random + conditional put (simplest, fine at low occupancy) |
| Redirect type | 302 | More traffic | 301 + short max-age when analytics are unnecessary |
| Analytics | Async log + rollups | Eventual, slightly lossy counts | Exact transactional counters (not at this scale) |
| Cache | Edge + local + Redis | Staleness window | No cache (impossible at 350 K/s) |

## 1.7 Follow-ups and answers
1. **How do you prevent users from guessing other users' links?** Keyed permutation of IDs (not affine), 7+ characters; for sensitive links use longer random codes; rate-limit 404s per IP.
2. **What if you need 1 B new URLs/day?** Writes 11.6 K/s (still modest); 62^7 lasts 9.6 years but 5 years of URLs fill **52%** of the space → collisions become frequent, so go to **8 characters** (218 T) or keep counter-based (collision-free) codes.
3. **How would you support custom domains?** Key by `(domain, code)`; route on `Host`; per-domain TLS certificates and abuse policy.
4. **How do you update a destination?** Allowed only for the owner; write a new version, purge caches; consider forbidding edits (immutable links) for safety.
5. **How do you provide per-country click analytics?** Resolve geo from the client IP at the edge, include it in the click event, aggregate by dimension.
6. **GDPR?** Do not log raw IPs long-term; hash/truncate; deletion by owner; retention policies on click logs.

*What graders look for:* clear numbers (read-heavy, 7 chars), a justified ID scheme with collision math, cache/CDN reasoning, async analytics, and abuse handling.

---

# Problem 2: Pastebin

> **Problem statement.** Design a service where users paste text (code, logs) and get a link to share. Pastes can expire.

## 2.1 Requirements
- **Functional:** create a paste (text up to 10 MB, typical ~10 KB), optional expiry (10 minutes to never), visibility (public / unlisted / private), optional language tag; fetch by link; delete (owner); "burn after reading" (stretch).
- **Non-functional:** 5 M new pastes/day; reads:writes ≈ 10:1; p99 read < 200 ms; durability: pastes survive until expiry; availability 99.9%; abuse control (spam, malware, leaked secrets).
- **Out of scope:** collaborative editing, syntax highlighting server-side (do it in the browser).

## 2.2 Estimates
```text
Pastebin            58 writes/s   579 reads/s avg   1.7 K peak reads/s   18.3 TB/yr raw   54.8 TB retained x3 (1 yr)   0.1 Gbps peak egress   2 app servers   20.0 GB cache hot set
```
- The **request rates are tiny** (≈58 writes/s): one small cluster handles them. What matters is **18 TB/year of text**, expiry-driven deletion, and abuse.
- The difference from the URL shortener: **object size** (10 KB vs 500 B) pushes content out of the metadata database.

## 2.3 API and data model
```text
POST /v1/pastes {content, language?, expires_in?, visibility}  → 201 {id, url, delete_token}
GET  /v1/pastes/{id}                                             → 200 {content, language, created_at, expires_at}   (or 404/410)
DELETE /v1/pastes/{id}  (with delete_token or owner auth)       → 204
GET  /raw/{id}                                                   → text/plain (cacheable)
```
Data split:
- **Metadata (relational or KV):** `pastes(id PK, owner_id, created_at, expires_at, visibility, size, content_sha256, storage_key, language, status)`, with an index on `expires_at` for sweeping.
- **Content (object storage):** key `p/<id>` (or `p/<sha256>` for deduplication). Content is immutable, which makes it CDN-friendly.
- Small pastes (< ~4 KB) may be **inlined** in the metadata row to save one read; this is an optimization once measured.

## 2.4 Architecture
```text
 Client ─▶ CDN ─▶ LB ─▶ App (stateless) ─┬─▶ Metadata DB (Postgres primary + replica, or KV)
                                         └─▶ Object storage (content)  ◀── lifecycle rules: delete after expiry
 Creation: app validates size/type, scans (async), writes content to object storage, then metadata (state=ACTIVE once stored)
 Expiry:   (1) reads check expires_at (lazy); (2) sweeper deletes expired objects/rows; (3) lifecycle rules by prefix/date as a backstop
```
**Evolution:** v1 one app + Postgres `TEXT` column (works up to a few GB); v2 move content to object storage (DB bloat, backup size, and replication cost); v3 CDN for public/unlisted reads (egress and latency); v4 async scanner/moderation pipeline; v5 shard metadata by `id` hash only when the table exceeds a single primary (~billions of rows, Chapter 10); v6 multi-region reads.

## 2.5 Deep dives
**Key generation.** 1.8 B pastes/year; a 62^8 space (218 T) makes random 8-character IDs collision-negligible (n/N ≈ 0.0008% after a year) with a unique-constraint retry. For **unlisted** pastes the URL *is* the capability, so make it unguessable: 12+ random base62 characters (71+ bits) from a CSPRNG; for **private** pastes require authentication and authorize on every fetch (object-level check, Chapter 22).

**Expiry correctly.**
- Store absolute `expires_at`. Reads return **404/410** past expiry even if the sweeper hasn't run (correctness never depends on cleanup timing).
- Cleanup: (a) a sweeper selects `expires_at < now()` in batches (indexed), deletes the content object then the row, idempotently; (b) object-store **lifecycle rules** with date-prefixed keys (`p/2026/10/04/...`) delete cohorts cheaply as a safety net. With 5 M pastes/day, deleting ~58 per second is trivial.
- "Never expires" pastes accumulate forever; apply storage quotas, inactivity expiry (e.g., delete after 1 year without views), and cold-tier transitions after 30 days (Chapter 11).

**Burn after reading (race condition).** Two simultaneous requests must not both receive the content. Use an atomic conditional update: `UPDATE pastes SET status='BURNED' WHERE id=? AND status='ACTIVE' RETURNING storage_key;` only the request that gets a row back may fetch the object; then delete it. This is a compare-and-set (Chapter 3/9), not a check-then-act.

**Deduplication (optional).** Store content by `sha256`; many pastes (logs, templates) are identical. Saves space but needs reference counting and garbage collection, adds cross-user existence side channels ("is this exact text already pasted?"), and complicates deletion. Usually not worth it below petabyte scale.

**Abuse.** Public paste sites are used to leak credentials and host malware/spam: rate limits per IP and per account (Problem 3), size limits, async content scanning (secrets patterns, malware signatures, spam classifiers), a report/takedown flow, `noindex` for unlisted pastes, user reputation, and for private/zero-knowledge variants client-side encryption with the key in the URL fragment (the server never sees plaintext).

## 2.6 Failure modes and hardening
| Failure | Handling |
|---|---|
| Object stored but metadata write fails | Orphan object: lifecycle rule on an "incomplete" prefix, or write metadata first as PENDING then flip to ACTIVE |
| Metadata but no object | PENDING state is never served; sweeper repairs/removes |
| DB primary failure | Replica promotion (Chapter 9); creation briefly unavailable; reads from replicas/CDN |
| Sweeper falls behind | Reads still enforce expiry; lifecycle rules backstop; alert on backlog |
| Hot paste (front page of a forum) | CDN caches `/raw/{id}`; immutable content, long TTL |
| Large paste abuse (10 MB × millions) | Per-user quotas, rate limits, size limits in the signed upload policy |

## 2.7 Trade-offs
Content in DB vs object store (simplicity vs scale/backup cost: switch above ~ a few GB); lazy vs eager expiry (correctness vs storage reclamation: do both); dedupe vs privacy/complexity; public CDN caching vs access-control freshness (private pastes must bypass shared caches, `Cache-Control: private`).

## 2.8 Follow-ups
1. **Support editing/versions?** Store versions as immutable objects with a pointer to the latest; diff on read.
2. **100 MB pastes?** Direct-to-object-store multipart upload (Problem 4), streaming reads, size tiers and stricter quotas.
3. **Search across public pastes?** Async indexer to an inverted index; moderation before indexing.
4. **Global latency?** CDN for reads; writes to the nearest region with replication; IDs include a region prefix.
5. **Cost at 5 years?** 18 TB/yr × 5 × 3 ≈ 274 TB replicated (≈ 128 TB with erasure coding at 1.4×); tier cold data.

*What graders look for:* metadata/content split, correct expiry semantics, burn-after-read atomicity, abuse handling; and recognizing that **scale is small but the details matter**.

---

# Problem 3: Rate Limiter

> **Problem statement.** Design a rate limiter for an API platform that restricts how many requests a client may make in a time period.

## 3.1 Requirements
- **Functional:** limit per client identity (API key, user, IP) by rule (e.g., 100 requests/min per key; different limits per endpoint and per pricing tier); respond `429 Too Many Requests` with `Retry-After` and `X-RateLimit-*` headers; rules configurable without redeploy.
- **Non-functional:** 1 M requests/s at peak across many gateway nodes; < 2 ms added latency; accurate enough (within ~10%) in a distributed setting; highly available (the limiter must not take the API down: define fail-open/closed); 50 M active keys.
- **Out of scope:** DDoS mitigation at network layers (CDN/WAF), billing-grade metering.

## 3.2 Estimates
- 1 M checks/s; 50 M active keys × (16 B state + map overhead ≈ 64 B) ≈ **3.2 GB** (token bucket); sliding-log state for a 100/min limit is **840 B per key → 44 GB** (Lab 2, part 3).
- A Redis shard handles ~50–100 K ops/s → **10–20 shards** if every request makes a central call; or ~1–2 shards if local buckets sync in batches.

## 3.3 Algorithms, measured

**Boundary burst (limit 100 per 60 s; 100 requests at t=59.9 s and 100 at t=60.0 s):**
```text
== 1) Boundary burst. Limit = 100 requests per 60 s. A client sends 100 requests at t=59.90-59.99 s and 100 more at t=60.01-60.10 s. ==
algorithm                                 accepted    most accepted in any 60-second window    verdict
token bucket (100/min, burst 100)            100          100                                    respects the limit
fixed window counter (100 / 60 s)            200          200                                    VIOLATES the limit (200 in one minute)
sliding window log (exact)                   100          100                                    respects the limit
sliding window counter (weighted)            101          101                                    VIOLATES the limit (101 in one minute)
```
- **Fixed window** allows **200 requests within 0.2 seconds** (twice the limit) at a window boundary. **Token bucket** and **sliding log** hold the limit at 100; the **sliding window counter** (weighted approximation) let one extra through (101).

**Well-behaved clients with random arrivals (Poisson), rejected percentage:**
```text
== 2) Well-behaved clients with random (Poisson) arrivals averaging below the limit. What fraction of their requests is rejected? (1,000 simulated minutes per row) ==
offered rate     token bucket | fixed window | sliding log | sliding counter
  60 req/min        0.00%           0.00%           0.00%           0.00%      
  80 req/min        0.00%           0.04%           0.35%           0.11%      
  90 req/min        0.00%           0.77%           2.80%           1.66%      
 100 req/min        0.43%           4.07%           7.59%           5.98%      
 120 req/min       16.61%          16.79%          19.68%          18.32%      
(an average-rate ideal would reject 0% below 100/min and (rate-100)/rate above it, e.g. 16.7% at 120/min; random clustering of arrivals pushes exact-window limiters above that)
```
- A client averaging **90 req/min** (below the limit) is rejected **2.8%** of the time by an *exact sliding log* and 0.77% by fixed windows, but **0%** by a token bucket with burst 100: exact windows penalize natural burstiness. If you want to forgive bursts, use a bucket with burst capacity; if you want a hard cap in any 60 s, use sliding log/counter and set the limit with headroom.

**Memory and speed:**
```text
== 3) Memory per key and single-thread speed (1,000,000 distinct keys, limit 100/min) ==
algorithm                                 state per key      memory for 50M active keys     checks/s (1 thread)
token bucket (100/min, burst 100)             16 B (+ map overhead)   3.2 GB                          2,347,859
fixed window counter (100 / 60 s)             16 B (+ map overhead)   3.2 GB                          2,831,771
sliding window log (exact)                   840 B (+ map overhead)   44.4 GB                         1,449,533
sliding window counter (weighted)             24 B (+ map overhead)   3.6 GB                          2,800,907
```
- Token bucket, fixed window, and sliding counter need ~16-24 B per key and run ~2.3-2.8 M checks/s per thread; the **exact sliding log needs 840 B per key (35-50× more)**. At 50 M keys that is 3.2 GB vs 44 GB.

**Algorithm comparison**
| Algorithm | Accuracy | Memory/key | Bursts | Use |
|---|---|---|---|---|
| Token bucket | Average rate exact; bounded burst | O(1) | Allowed up to capacity | Default for APIs (Stripe, AWS-style) |
| Leaky bucket | Smooth constant outflow | O(1) | Queued/smoothed | Traffic shaping |
| Fixed window | Poor at boundaries (2× burst) | O(1) | Boundary spikes | Cheap coarse limits |
| Sliding window log | Exact | O(limit) | Strict | Small limits, high accuracy |
| Sliding window counter | Approximate (~few %) | O(1) | Smooth | Good accuracy/cost compromise |

## 3.4 Architecture
```text
 Client ─▶ API gateway / edge ─▶ [Rate-limit middleware] ─▶ upstream service
                                   │  1. identify client (API key / user / IP; trust X-Forwarded-For only from our proxies)
                                   │  2. load rule (cached from the rules service: tier, endpoint, window, limit, burst)
                                   │  3. check budget: local token bucket (fast path) + central counter (Redis Cluster, Lua script)
                                   ▼  4. allow → forward;  deny → 429 + Retry-After + X-RateLimit-Remaining/Reset
 Rules service (config DB + pub/sub push) ·  Metrics (allowed/blocked per rule, p99 limiter latency) ·  Fail-open on limiter errors (configurable)
```
**Placement:** at the edge/CDN for coarse per-IP limits; at the API gateway for per-key/tier limits; inside services (library or sidecar) for expensive operations (search, export) and per-tenant fairness. Defense in depth uses several.

## 3.5 Distributed correctness, measured
Gateways are many; a user's requests land on different ones. **Global limit 100/min, 10 gateways, requests routed randomly:**
```text
== 4) Distributed enforcement. Global limit 100 requests/min per user; 10 gateways; each request lands on a random gateway. ==
strategy                                                              honest user @ 80/min: rejected     abusive user @ 1,000/min: allowed per minute
each gateway enforces the FULL limit (100/min) locally                    0.0%                               962
each gateway enforces limit/10 = 10/min locally                           5.0%                               100
central counter (e.g. Redis INCR), exact                                  0.0%                               100
local-only limits are either unsafe (10x over the limit) or unfair (honest users blocked by imbalance); production systems use a shared counter (Redis/Lua), or local buckets topped up in batches from a central one.
```
- **Local-only, full limit on each gateway** is *unsafe*: an abusive client got **962/min (9.6× the limit)**.
- **Local-only, limit/10 each** is *unfair*: an honest client at 80/min (below the limit!) was **rejected 5% of the time** because random routing makes some gateways see more than 8 requests in a minute.
- **A central exact counter** gets both right (0% false rejections, 100/min cap) but makes every request pay a network hop to the shared store.
- **Practical design:** local token buckets that **lease batches of tokens** from the central counter (e.g., 10% of the key's budget at a time, refilled asynchronously). Accuracy error is bounded by the batch size; central load drops 10–50×; if the central store is unreachable, gateways keep enforcing from their local lease (graceful degradation).

**Atomic central update.** A read-modify-write over the network races. Use a **Lua script** in Redis (atomic per key) that implements the token bucket: refill based on server time (`redis.call('TIME')`, not client clocks), try to take a token, return remaining and reset. Shard by key (consistent hashing); hot keys (one abusive client at 100 K req/s) saturate one shard → local pre-aggregation and early local rejection.

## 3.6 Failure modes and hardening
| Failure | Handling |
|---|---|
| Redis shard down | Fail open (allow, log) for general APIs; fail closed for abuse-sensitive endpoints (login, SMS send); local leases bridge short outages |
| Clock skew | Use the store's clock or monotonic local time; never compare client timestamps |
| Rule misconfiguration | Shadow/dry-run mode (count but don't block), staged rollout, kill switch |
| Client spoofing (IP rotation, forged `X-Forwarded-For`) | Identify by authenticated key first; trust forwarded headers only from known proxies; combine signals |
| Thundering retries after 429 | `Retry-After` plus client exponential backoff with jitter; cap retries (Chapter 18) |
| Limiter latency spikes | Strict timeout (≤ 5 ms), circuit breaker, then fail-open |
| Multi-region | Split the global limit by regional share and rebalance periodically; do not synchronously share counters across oceans |

## 3.7 Trade-offs
Accuracy vs cost (sliding log 35-50× memory), strictness vs user experience (burst tolerance), central vs local (consistency vs latency/availability), fail-open vs fail-closed (availability vs protection), per-request check vs async approximate (precision vs overhead).

## 3.8 Follow-ups
1. **Different limits per endpoint and tier?** Rule key = `(tier, endpoint)`; weighted costs (an export costs 50 tokens, a read 1).
2. **Limit by concurrency instead of rate?** Count in-flight requests (increment on start, decrement on finish, with lease timeouts for crashed clients).
3. **Distributed across regions?** Regional budgets with periodic rebalancing; accept bounded overshoot.
4. **How do you test it?** Property tests against the ideal model, load tests with burst traces, chaos tests of the store.
5. **How is it different from load shedding?** Rate limiting is per-caller policy; load shedding protects the server globally (Chapter 18).

*What graders look for:* named algorithms with trade-offs, awareness of the boundary problem, a distributed design that isn't "just Redis" (atomicity, hot keys, fail-open), and clear placement.

---

# Problem 4: File Upload Service

> **Problem statement.** Design a service that lets users upload large files (up to 5 GB) from browsers and mobile apps, then share/download them.

## 4.1 Requirements
- **Functional:** upload files up to 5 GB; show progress; **resume** interrupted uploads; virus scan; store metadata (owner, size, type, checksum); download via access-controlled links; quotas.
- **Non-functional:** 1 M uploads/day (avg 5 MB; a few at 5 GB); uploads from flaky mobile networks; integrity (checksum verified); durability 11 nines; download latency good globally; 99.9% availability; retention 3 years.

## 4.2 Estimates
```text
File upload service  12 writes/s   35 reads/s avg   104 peak reads/s   1.8 PB/yr raw   16.4 PB retained x3 (3 yrs)   4.2 Gbps peak egress   1.39 Gbps peak ingress
```
- **Request rates are trivial** (12 uploads/s); **bytes and connection time are the problem**: 1.8 PB/year (≈ 7.6 PB for three years with 1.4× erasure coding instead of 16.4 PB with 3× replication), a few Gbit/s of ingress, and uploads that last minutes to hours. Do **not** proxy file bytes through the application tier.

## 4.3 API and flow (pre-signed multipart upload, Chapter 11)
```text
 1. POST /uploads {filename, size, content_type, sha256?}  → app: authn, quota check, validate type/size, create upload record (state=INITIATED),
                                                             start multipart upload in object storage (quarantine bucket), return {upload_id, part_size, [signed part URLs]}
 2. PUT <signed URL for part k>  (client → object storage directly, up to 4-8 parts in parallel; each part retried independently; part checksum header)
 3. GET /uploads/{id}/parts → list uploaded parts (for resume after crash/app restart)
 4. POST /uploads/{id}/complete {parts:[{n, etag}]}  → app asks storage to assemble, verifies size/checksum, state=UPLOADED, emits event
 5. Scan worker consumes event: virus scan, content-type sniffing → state=CLEAN (move/copy to the clean bucket) or INFECTED (quarantine, notify)
 6. Download: GET /files/{id} → authz check → 302 to a short-lived signed CDN URL (Range requests supported)
```
Metadata table: `files(id, owner, filename, size, sha256, state, storage_key, created_at, ...)` and `uploads(upload_id, file_id, part_size, state, expires_at)`.

## 4.4 Why chunking is mandatory: measured (Lab 3, A)
A 10 Mbit/s connection that drops about every 5 minutes (typical for mobile/Wi-Fi), 5 s to reconnect, 2-hour deadline:
```text
== A) Uploading over a 10 Mbit/s connection that drops every ~5 minutes (exponential), 5 s to reconnect, 2-hour deadline. 1500 runs per cell. ==
file size   strategy                       finished within 2 h   mean time (min)   bytes sent / file size
100 MB      single request (restart on failure)         100.0%                 1.6        1.15x
100 MB      64 MB chunks, resume                        100.0%                 1.5        1.07x
100 MB      8 MB chunks, resume                         100.0%                 1.4        1.01x
100 MB      1 MB chunks, resume (0.1 s overhead each)   100.0%                 1.5        1.00x
1 GB        single request (restart on failure)          83.1%                49.8        3.68x
1 GB        64 MB chunks, resume                        100.0%                14.8        1.09x
1 GB        8 MB chunks, resume                         100.0%                13.7        1.01x
1 GB        1 MB chunks, resume (0.1 s overhead each)   100.0%                15.3        1.00x
5 GB        single request (restart on failure)           0.0%                   -        -
5 GB        64 MB chunks, resume                        100.0%                73.9        1.09x
5 GB        8 MB chunks, resume                         100.0%                68.5        1.01x
5 GB        1 MB chunks, resume (0.1 s overhead each)   100.0%                76.4        1.00x
minimum time with no failures: 100 MB = 1.3 min, 1 GB = 13.3 min, 5 GB = 66.7 min
```
- A **single-request upload of 1 GB succeeded in only 83% of runs**, took **49.8 minutes** on average (ideal: 13.3) and **sent 3.68× the file size** (everything resent after each drop); **a 5 GB single-request upload never finished (0%)**: surviving 67 minutes without a drop has probability e^(−67/5) ≈ 0.0002%.
- With **8 MB chunks and resume**, 5 GB finishes **100%** of the time in **68.5 minutes** (ideal 66.7) with **1.01×** bytes sent. 1 MB chunks add per-request overhead (76.4 min); 64 MB chunks waste more on each drop (1.09×).
- **Rule of thumb:** parts of 5–16 MB; the part size must keep you under the store's part limit (S3: 10,000 parts → ≥ 0.5 MB for 5 GB; 100 KB parts would need 51,200 parts and fail) and limit the retransmitted bytes per failure.

## 4.5 Deep dives
**Resumability.** The client persists `upload_id` and which parts are done; after a crash/offline period it calls `GET /uploads/{id}/parts` and continues. The server must treat part uploads as **idempotent** (same part number overwrites). Uploads expire after N days; an object-store **lifecycle rule aborts incomplete multipart uploads** (they otherwise cost storage forever).

**Integrity.** Compute a per-part checksum (CRC32C/SHA-256) client-side, verify on the server/storage side, and a whole-file hash at completion. Store the final checksum in metadata and verify on download for critical flows.

**Parallelism and bandwidth.** 4–8 parallel part uploads usually saturate a link; more adds congestion. Adaptive: increase concurrency while throughput improves. QUIC/HTTP-3 helps with network switches on mobile.

**Virus scanning and state machine.** `INITIATED → UPLOADED → SCANNING → CLEAN | INFECTED | FAILED`. Only `CLEAN` files are downloadable. Scanning is asynchronous (queue + workers with timeouts, DLQ for poison files, idempotent per `(file_id, version)`; Chapter 12). Large archives need decompression limits (zip bombs).

**Quotas and abuse.** Reserve quota at initiation, reconcile at completion, release on abort/expiry. Rate-limit initiations (Problem 3), cap concurrent uploads per user, validate content type by sniffing (not extension), restrict executable types if needed.

**Deduplication ("instant upload").** If the client first sends the file's hash and the service already has that content, skip the upload. Benefits: huge bandwidth savings; risks: a **side channel** that reveals whether anyone has uploaded a given file, so require proof of possession (hash of random byte ranges chosen by the server) or restrict dedup to within one account/tenant.

**Download path.** Authorize in the app, then redirect to a **short-lived signed URL** served via CDN; support `Range` requests (resume, seek); for private data use `Cache-Control: private` and signed cookies. Large public files benefit from CDN caching.

**Security.** Encryption in transit and at rest (envelope encryption, Chapter 22); pre-signed policies bound to key, size limit, content type, and expiry; separate buckets for quarantine and clean content; never serve user uploads from the application's origin domain (use a separate domain to prevent XSS via uploaded HTML).

## 4.6 Failure modes and hardening
| Failure | Handling |
|---|---|
| Client crash mid-upload | Resume via ListParts |
| Part corrupted | Per-part checksum fails → re-upload that part |
| `complete` called twice | Idempotent: second call returns the same result |
| Orphaned multipart uploads | Lifecycle abort rule + expiry sweeper |
| Scanner outage | Files remain `UPLOADED` (not downloadable); backlog alerts; scale workers |
| Storage region failure | Cross-region replication of CLEAN bucket; uploads retry to the alternate region |
| Very large burst of uploads | Direct-to-storage design scales independently of the app tier |

## 4.7 Trade-offs
Direct-to-storage (scalable, cheap, less control per byte) vs proxying through the app (simple, easy inspection, expensive and fragile); part size (resume granularity vs overhead and part limits); synchronous scanning (safe but slow) vs asynchronous with a state machine (fast, requires gating); dedup (savings vs privacy); replication (3×) vs erasure coding (1.4× cost saving, CPU/repair complexity).

## 4.8 Follow-ups
1. **Upload from a mobile app in the background?** OS-level background upload APIs, persistent upload state, part-sized retries, battery/network policies.
2. **Resumable protocol standard?** The tus protocol or provider-specific multipart APIs.
3. **How to show progress?** Client-side byte counters per part; server state is part-granular.
4. **Multi-GB downloads?** CDN with range requests and parallel range downloads.
5. **Time-to-available after upload?** Scan latency; could stream-scan during upload for small files.
6. **Cost?** 1.8 PB/yr dominates; lifecycle tiers (cool/archive), erasure coding, and deleting abandoned uploads.

*What graders look for:* realizing bytes (not QPS) dominate, pre-signed multipart direct upload, resume and checksums, a scan state machine, and security of the download path.

---

# Problem 5: Chat Service

> **Problem statement.** Design a chat service supporting 1:1 and group conversations (up to 100 members) with near-real-time delivery, history, and offline delivery.

## 5.1 Requirements
- **Functional:** send/receive text messages; groups up to 100; **per-conversation ordering**; delivery states (sent / delivered / read); presence (online / last seen); message history with pagination; offline delivery (push notification + sync on reconnect); multi-device.
- **Non-functional:** 50 M DAU × 40 messages/day = 2 B messages/day; **online delivery p99 < 500 ms**; messages must not be lost once acknowledged; at-least-once delivery with dedup (effectively once, Chapter 12); 99.95% availability; retention 5 years.
- **Out of scope:** end-to-end encryption protocol details (mention), voice/video, message search.

## 5.2 Estimates
```text
Chat (1:1 + groups)  23.1 K writes/s   69.4 K reads/s avg   208.3 K peak reads/s   87.6 TB/yr raw   1.3 PB retained x3 (5 yrs)   0.2 Gbps peak egress   47 app servers   40.0 GB cache hot set
```
- Treat "reads" as **deliveries** (each message is delivered to ~3 recipients on average): ~70 K deliveries/s average, ~208 K peak.
- **Concurrent connections:** 20% of 50 M DAU online at peak = **10 M** WebSockets; at ~150 K connections per gateway (~20 KB each ≈ 3 GB RAM) → **~67 gateways, ~100 with N+1 across AZs**. The connection tier, not the message store, is the unusual scaling dimension.
- Storage is modest: 120 B/message → 87.6 TB/year raw.

## 5.3 API, protocol, data model
**Client ↔ gateway:** a persistent **WebSocket** (fallback: long-polling) with frames such as `send {conv_id, client_msg_id, body}`, `ack {conv_id, seq}`, `deliver {conv_id, seq, sender, body, ts}`, `sync {conv_id, after_seq}`.
**HTTP APIs:** create conversation, add members, fetch history `GET /conversations/{id}/messages?before_seq=&limit=50`.

**Data model (wide-column such as Cassandra/Scylla, or sharded SQL):**
```text
messages:      PRIMARY KEY ((conversation_id, bucket), seq)   -- bucket = e.g. month; clustering by seq DESC for "latest N"
               columns: sender_id, client_msg_id, body, ts, type, deleted
conversations: conversation_id, members[], last_seq, created_at
inbox/cursors: (user_id, device_id, conversation_id) → last_delivered_seq, last_read_seq   -- small, hot
```
**Why partition by conversation:** the dominant read is "latest messages in conversation C", a single-partition range scan; ordering by `seq` within the partition gives total order per conversation. Bucketing (by month) bounds partition size for busy groups.

## 5.4 Architecture
```text
 Device ⇄ WebSocket ⇄ [Gateway tier] ──────────────┐
                           │ register user→gateway   │ deliver(user, msg)  (via per-gateway pub/sub topics)
                           ▼                         │
                  [Connection registry (Redis, TTL)] ◀┘
 send ─▶ Gateway ─▶ [Chat service: shard by conversation_id] ─┬─ 1. assign seq (single writer per conversation)
                                                              ├─ 2. persist to message store (ack to sender AFTER durable write)
                                                              ├─ 3. fan-out: lookup members' gateways → publish deliver events
                                                              └─ 4. offline members → push-notification service (APNs/FCM) + unread counters
 Reconnect: device sends sync {conv, after_seq} for each active conversation → server returns messages > after_seq (idempotent)
```
**Evolution:** v1 single server, in-memory + WebSocket (works for ~50 K users); v2 database + stateless app, polling; v3 WebSocket gateways + connection registry (polling cost and latency); v4 pub/sub between gateways (a message for a user on another gateway); v5 sharded chat service with per-conversation sequencing + wide-column store; v6 push notifications and multi-device cursors; v7 multi-region (route users to the nearest region; conversations homed in one region).

## 5.5 Deep dives
**Ordering, duplicates, and loss: measured (Lab 3, B).** Network jitter (0-500 ms), 3% duplicate deliveries, 1% first-delivery loss:
```text
== B) Chat delivery: 500 conversations x 100 messages; network adds 0-500 ms jitter, delivers 3% of messages twice, and drops 1% of first deliveries. ==
sent messages: 50,000 ; lost on first delivery: 521 ; duplicate deliveries: 1,456
client                                            duplicates shown   out-of-order pairs shown   messages never shown   mean extra wait for ordering
naive (append on arrival)                            1437              20550                        502               -
sequence-aware (dedupe, reorder, gap fetch)             0                  0                          0               95 ms (p99 end-to-end 1266 ms)
offline sync: client last saw seq 9,940 of 10,000; on reconnect it asks 'messages after 9,940' and the server returns exactly 60 messages in order (no timestamps needed, no gaps, idempotent if repeated)
```
- A **naive client** that appends on arrival displayed **1,437 duplicate messages, 20,550 out-of-order pairs, and never showed 502 messages** (lost deliveries) across 50,000 sent.
- A **sequence-aware client** (dedupe by `(conversation, seq)`, buffer out-of-order messages, request missing seq numbers after a 1 s gap timeout) showed **zero duplicates, zero ordering errors, zero missing messages**, at the cost of **~95 ms mean extra wait** for reordering (p99 end-to-end 1.27 s, dominated by the 1 s gap-fill timeout for lost messages).
- **Offline sync with a cursor:** the client remembers the last seq per conversation; on reconnect it asks for "after N" and receives exactly the missed messages in order: no timestamps, no gaps, safe to repeat.

**Where does the sequence number come from?** A **single writer per conversation**: route all sends for a conversation to the same chat-service shard (consistent hash on `conversation_id`), which increments `last_seq` and appends. Failover of the shard owner must not produce two writers: use a lease/epoch fencing token (Chapter 14/16) or a store with conditional writes/LWT on `last_seq`. Alternatives: Kafka partition per conversation (a log gives the order), or timestamps+tie-break (wrong under skew, Chapter 14).

**Delivery semantics.** Client sends with a unique `client_msg_id`; the server dedupes (so retries after timeout are safe) and acks with the assigned `seq` **after** the durable write. Delivery to recipients is at-least-once; devices ack by `seq`; unacked messages are redelivered or fetched at sync. Receipts (`delivered`, `read`) are small events that update cursors; batch them.

**Fan-out for groups.** For groups ≤ 100, store the message **once per conversation** (not once per member) and deliver to each member's connected devices; each user's unread count and cursor are per-user state. Writing a copy into every member's inbox ("fan-out on write", Chapter 26) is unnecessary at this group size; for channels with 100 K+ members switch to pull/fan-out-on-read plus push notification summaries.

**Presence.** Gateways receive heartbeats; a Redis key `presence:{user}` with a 30-60 s TTL is refreshed. Broadcasting every presence change to all contacts is a fan-out explosion; instead have clients subscribe only to the presence of visible contacts, coalesce updates, and allow stale "last seen".

**Connection management.** 10 M connections need: L4 load balancing with long idle timeouts, TCP keepalives/app-level pings (30 s), per-gateway connection limits, graceful drain on deploys, and **reconnect storms** control. If a gateway with 150 K connections dies, 150 K clients reconnect within seconds: use exponential backoff with jitter on clients and an admission-rate limit on gateways (Chapter 18).

**Push notifications.** If the registry shows no live connection (or the device is backgrounded), send a push via APNs/FCM with a minimal payload ("new message") and let the app sync on wake; coalesce bursts; respect rate limits and user mute settings.

**Multi-device.** Each device has its own cursor; "read" state is per user (sync across devices); new devices backfill history from the server (or from the old device in E2E designs).

## 5.6 Failure modes and hardening
| Failure | Handling |
|---|---|
| Gateway crash | Clients reconnect (jittered backoff), sync by cursor; registry entries expire by TTL |
| Chat-service shard failover | Fencing/lease prevents two sequencers; clients retry with `client_msg_id` |
| Store partition unavailable | Quorum writes tolerate one replica loss; queue + retry; return error (never ack before durable) |
| Duplicate/out-of-order delivery | Seq numbers, client dedupe/reorder (lab: 0 errors vs 22 K anomalies) |
| Hot group (viral) | Per-conversation sequencer is a throughput cap (~thousands msgs/s): shard large channels differently |
| Push provider outage | Queue and retry with backoff; messages are safe in the store |
| Abuse (spam, floods) | Rate limits per user/conversation (Problem 3), block lists |

## 5.7 Trade-offs
WebSocket vs long-poll (latency/efficiency vs simplicity/proxy-compatibility); single sequencer per conversation (clean ordering, per-conversation throughput cap) vs timestamp ordering (no coordination, wrong under skew); store-once per conversation vs per-recipient inbox (cheap writes vs simple per-user reads); at-least-once + dedupe vs exactly-once; push vs pull for large channels.

## 5.8 Follow-ups
1. **End-to-end encryption?** Server stores ciphertext; clients manage keys (Signal protocol, per-device keys, group sender keys); server-side features (search, spam detection) are limited.
2. **Groups of 1 M members?** Treat as a broadcast channel: fan-out on read, hierarchical fan-out trees, sampled presence, push only for mentions.
3. **Message edit/delete?** Events that reference the original `seq` (tombstones); clients apply in order.
4. **Media messages?** Upload to object storage (Problem 4), send a message with the media reference.
5. **Search?** Client-side local index, or server-side index per user (privacy trade-off).
6. **Multi-region?** Home each conversation in one region; cross-region members connect to their nearest gateway and relay.

*What graders look for:* the connection tier numbers (10 M sockets), message ordering via per-conversation sequence, delivery semantics and ack timing, offline sync with cursors, and reconnect storm handling.

---

# Problem 6: LRU Cache

> **Problem statement.** Design and implement a cache with a fixed capacity that evicts the least recently used item. `get(key)` and `put(key, value)` must be O(1). Then discuss making it thread-safe, adding TTL, and scaling it to a distributed cache service.

## 6.1 Requirements
- **Functional:** `get`, `put`, optional `delete`; bounded capacity (entries or bytes); evict the least-recently-used entry; optional per-entry TTL; hit/miss/eviction metrics.
- **Non-functional:** O(1) operations; thread-safe with good concurrency; predictable memory; as a service: p99 < 1-2 ms, horizontally scalable, survives node loss (it is a cache: loss is acceptable but must not cascade, Chapter 8).

## 6.2 Data structure: hash map + doubly linked list
- **HashMap** `key → node` for O(1) lookup.
- **Doubly linked list** ordered by recency (head = most recent, tail = least recent): moving a node to the head and removing the tail are O(1) because you hold pointers.
- `get`: look up node, unlink, push to head, return. `put`: if present update and move to head; else if full evict tail (and remove from map), then insert at head.
The implementation and a **differential test** against a naive O(n) reference are in the lab.

**Measured (Lab 2).**
```text
== 1) Differential test: O(1) LRU vs naive O(n) reference: 200,000 random operations over 2,000 random caches (capacity 1-8) -> mismatches = 0
LFU sanity: capacity 2; put 1, put 2, get 1, put 3 -> key 2 (least frequently used) evicted: get(2) = null, get(1) = 1, get(3) = 3
```
- 200,000 random operations over 2,000 random caches (capacity 1-8, so evictions happen constantly): **0 mismatches** with the obviously-correct reference. *Differential testing against a simple model is the cheapest way to trust a tricky data structure.*

## 6.3 Concurrency: the part interviews probe
| Approach | How | Trade-off |
|---|---|---|
| **One global lock** (`synchronized`) | Simple and correct; reads mutate the list, so they also lock | Serializes all threads; throughput = one core |
| **Lock striping / segments** | N independent LRU segments by `hash(key) % N`, each with its own lock | Scales with cores; LRU order is per-segment (approximate global LRU) |
| **CLOCK / second-chance** | Circular buffer with a "referenced" bit set on access (no list manipulation on reads) | Reads are nearly lock-free; approximate LRU |
| **Buffered reads + async policy update** (Caffeine) | Record accesses in a lock-free ring buffer; a single thread applies recency updates in batches | Excellent throughput; policy slightly delayed |
| **Sampling-based eviction** (Redis) | Sample K random keys, evict the oldest | No global structure at all; approximate |
Single-thread throughput of the simple structure is **~4.1 M ops/s** (Lab 2, part 3) — a global lock around it caps the whole machine at that; striping across 8-16 segments is the standard fix.

**Memory accounting.** Real caches bound **bytes**, not entry count: each entry has a weight (key + value size + overhead); evict until total weight ≤ capacity. A single huge value can evict many small ones: consider rejecting oversize entries or using a size-aware policy.

**TTL.** Store `expires_at`; check lazily on `get` (treat expired as a miss and remove), plus a background sweeper or timer wheel for active expiry so dead entries don't occupy memory. Add jitter to TTLs to avoid synchronized expiry (Chapter 8).

**Loading cache and stampedes.** `getOrLoad(key, loader)` with **single-flight** (one in-flight load per key; other callers wait), negative caching with short TTL, and `refreshAfterWrite` (serve stale while refreshing).

## 6.4 LRU vs LFU, measured
Cache of 2,000 items over 100,000 items (Zipf 1.0). Halfway through, popularity shifts completely (a different set becomes hot):
```text
== 2) LRU vs LFU hit ratio (100,000 items, Zipf 1.0, cache holds 2,000). Popularity shifts completely at the midpoint. ==
policy        hit ratio 1st half (stable)   hit ratio 2nd half (after the hot set changes)   2nd half, first 100k requests only
LRU                  57.3%                         57.3%                                         56.8%
LFU                  64.8%                         25.5%                                         23.5%
```
- With **stable** popularity, **LFU wins** (64.8% vs 57.3%): it protects consistently popular items.
- After the **hot set changes**, LRU adapts immediately (57.3%) while **LFU collapses to 25.5%** because the cache is full of formerly popular items with large counts that never get evicted ("cache pollution"). Real systems use aging/decay (TinyLFU with periodic halving) or windowed admission (W-TinyLFU) to get both behaviors. LRU's weakness is **scan pollution**: one large sequential scan flushes the whole cache (Chapter 8: 2Q/ARC/admission filters).

## 6.5 From a library to a distributed cache service
```text
 Clients ─(client library: consistent hash ring with vnodes, Chapter 10)─▶ cache node 1..N  (each: sharded in-memory LRU + TTL)
            └─ replication of hot keys, or a small client-side L1 cache; timeouts of a few ms; fall back to the origin on failure
```
- **Partitioning:** consistent hashing with virtual nodes (adding a node moves ~1/(N+1) of keys, Lab Ch 10); fixed hash slots (Redis Cluster) are the alternative.
- **Replication:** for a pure cache, none (rebuild from origin) or one replica for hot shards to avoid cold-start stampedes after node loss.
- **Hot keys:** replicate the key across nodes or add a local L1 with a 1-second TTL (Chapter 8).
- **Consistency:** the origin is the source of truth; invalidate on write or use short TTLs; accept staleness.
- **Failure:** a dead node loses 1/N of cached keys → the origin sees a miss spike; protect it (single-flight, rate limits, warm-up).
- **Observability:** hit ratio per key class, evictions, memory, latency, hot keys (Chapter 21).

## 6.6 Failure modes and pitfalls
| Pitfall | Effect |
|---|---|
| `LinkedHashMap` used without synchronization | Corruption under concurrent access (even `get` mutates order in access-order mode) |
| Eviction by count with variable-size values | Memory blowups |
| Caching nulls/errors without TTL | Poisoned cache |
| No single-flight | Stampede on popular-key miss |
| Mutable cached objects | Callers corrupt shared state; cache immutable values or copies |
| Unbounded key space from user input | Memory pressure and attacker-controlled churn |

## 6.7 Trade-offs
Exact LRU vs approximate (CLOCK/sampling): accuracy vs concurrency; LRU vs LFU/TinyLFU: adaptivity vs frequency protection; entry-count vs byte weights; lazy vs active expiry (CPU vs memory); local vs distributed (latency vs shared capacity/consistency).

## 6.8 Follow-ups
1. **Implement LFU in O(1).** Hash map key→(value, freq) + `freq → ordered set` buckets + `minFreq` (the lab has it).
2. **How do you handle a scan?** Admission filter (don't admit one-hit wonders), segmented LRU (probationary/protected segments).
3. **Write-through vs write-back cache?** Chapter 8: consistency window, loss risk.
4. **How would you test it?** Differential tests, property tests (size ≤ capacity, most recent key always present), concurrency stress with linearizability checks (Chapter 15).
5. **Per-tenant fairness?** Partition capacity by tenant, or weighted eviction.

*What graders look for:* correct O(1) structure, awareness that `get` mutates state (locking), a concrete concurrency strategy, eviction by bytes, TTL handling, and the distributed extension via consistent hashing.

---

# Cross-problem patterns

| Pattern | Where it appeared | Lesson |
|---|---|---|
| **Estimate first, then choose** | All | The URL shortener is read-bound, file upload is byte-bound, chat is connection-bound, pastebin is small-scale |
| **Immutable content + CDN** | URL mapping, Pastebin raw, file download | Immutability makes caching trivial |
| **Key generation** | URL shortener, Pastebin | Collision math, range allocation, keyed permutation |
| **Async off the hot path** | Click analytics, scanning, expiry | Never block the user path on side effects |
| **Atomic conditional update** | Custom alias, burn-after-read, token bucket Lua | Compare-and-set beats check-then-act |
| **State machines for pipelines** | Upload (INITIATED→CLEAN), paste (PENDING→ACTIVE) | Makes crash recovery and gating explicit |
| **Idempotency keys** | URL creation, chat `client_msg_id`, upload `complete` | Retries are inevitable |
| **Sequence numbers + cursors** | Chat | Ordering, dedupe, and sync without clocks |
| **Fail-open vs fail-closed** | Rate limiter | Decide per endpoint |
| **Lease/batch to cut coordination** | ID ranges, rate-limit token leases | Trade bounded error for 10-1000× fewer central calls |
| **Approximate structures** | Rate limiting counters, HLL uniques | Pay accuracy for memory/throughput deliberately |

---

# Labs

## Lab 1: URL shortener experiments
```java
import java.math.BigInteger;
import java.util.*;

public class UrlLab {
    static final String ALPHA = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz";
    static String base62(long v, int width) { char[] c = new char[width]; for (int i = width - 1; i >= 0; i--) { c[i] = ALPHA.charAt((int) (v % 62)); v /= 62; } return new String(c); }
    static long pow62(int k) { long r = 1; for (int i = 0; i < k; i++) r *= 62; return r; }

    // zipf sampler
    static int[] zipf(int n, double s, int count, long seed) {
        double[] cdf = new double[n]; double sum = 0; for (int k = 1; k <= n; k++) { sum += 1.0 / Math.pow(k, s); cdf[k - 1] = sum; }
        Random r = new Random(seed); int[] out = new int[count];
        for (int i = 0; i < count; i++) { double u = r.nextDouble() * sum; int lo = 0, hi = n - 1; while (lo < hi) { int mid = (lo + hi) >>> 1; if (cdf[mid] < u) lo = mid + 1; else hi = mid; } out[i] = lo; }
        return out;
    }

    static void collisions() {
        System.out.println("== A) Random short codes: how often does an insert collide with an existing code? ==");
        System.out.println("code length   space (62^k)        codes already stored   measured collision rate   theory n/N");
        Random r = new Random(1);
        for (int k : new int[]{4}) { long N = pow62(k); for (int n : new int[]{100_000, 1_000_000, 5_000_000, 10_000_000}) {
            Set<Long> seen = new HashSet<>(n * 2); long coll = 0, trials = 200_000;
            while (seen.size() < n) seen.add((long) (r.nextDouble() * N));
            for (long t = 0; t < trials; t++) if (seen.contains((long) (r.nextDouble() * N))) coll++;
            System.out.printf("%6d       %,15d     %,15d        %10.4f%%             %10.4f%%%n", k, N, n, 100.0 * coll / trials, 100.0 * n / N); } }
        long N7 = pow62(7); for (double n : new double[]{1e9, 18.25e9, 182.5e9}) System.out.printf("%6d       %,15d     %,15.0f        (not simulated)           %10.4f%%   <- expected extra DB round trips per insert = n/N%n", 7, N7, n, 100 * n / N7);
        // truncated hash: birthday collisions between *different* URLs hashing to the same prefix
        System.out.println("hash(url) truncated to 7 chars, birthday bound: P(any collision) after n URLs ~ 1 - exp(-n^2/(2N)):");
        for (double n : new double[]{1e5, 1e6, 1e7}) System.out.printf("   n = %,.0f -> P(at least one collision) = %.4f%%  (and EVERY new URL has an n/N chance of colliding, so a DB uniqueness check + retry is mandatory)%n", n, 100 * (1 - Math.exp(-n * n / (2.0 * N7))));
    }

    static void counters() {
        System.out.println("\n== B) Counter-based IDs scrambled by a bijection (no collisions, no sequential guessing) ==");
        BigInteger N = BigInteger.valueOf(pow62(7)), B = BigInteger.valueOf(123_456_789L);
        BigInteger A = new java.math.BigDecimal(N).multiply(new java.math.BigDecimal("0.6180339887498949")).toBigInteger().or(BigInteger.ONE);   // golden-ratio multiplier spreads consecutive ids across the whole space
        while (!A.gcd(N).equals(BigInteger.ONE)) A = A.add(BigInteger.TWO);                                                                   // must be coprime to 62^7 = 2^7 * 31^7 so the map is a bijection
        System.out.println("sequential id -> plain base62      scrambled code (a*id+b mod 62^7, a = golden-ratio multiplier)");
        for (long id = 1; id <= 5; id++) System.out.printf("   %d            %s                 %s%n", id, base62(id, 7), base62(A.multiply(BigInteger.valueOf(id)).add(B).mod(N).longValue(), 7));
        Set<Long> seen = new HashSet<>(); int dup = 0; int sameFirstCharAsNext = 0; String prev = null; int M = 2_000_000;
        for (long id = 1; id <= M; id++) { long v = A.multiply(BigInteger.valueOf(id)).add(B).mod(N).longValue(); if (!seen.add(v)) dup++; String s = base62(v, 7); if (prev != null && prev.charAt(0) == s.charAt(0)) sameFirstCharAsNext++; prev = s; }
        System.out.printf("2,000,000 consecutive ids: duplicate codes = %d (bijection => 0); adjacent ids sharing a first character = %.1f%% (a random sequence would be %.1f%%)%n", dup, 100.0 * sameFirstCharAsNext / M, 100.0 / 62);
        System.out.println("(An affine scramble is not secret: two (id, code) pairs reveal a and b. If codes must resist enumeration, use a keyed permutation, e.g. a Feistel network / format-preserving encryption.)");
        System.out.println("range allocation: app servers fetch a block of IDs from the central counter and hand them out locally");
        for (int block : new int[]{1, 100, 1_000, 10_000}) System.out.printf("   block %,6d -> central counter calls per 1,000,000 IDs: %,9d ; IDs lost (gap) if a server crashes: up to %,d%n", block, 1_000_000 / block, block - 1);
    }

    static void cacheSizing() {
        int urls = 2_000_000, requests = 6_000_000; int[] w = zipf(urls, 0.9, requests, 3);
        System.out.println("\n== C) Redirect cache sizing: LRU hit ratio, 2,000,000 short URLs, Zipf(0.9) popularity, 6,000,000 redirects ==");
        System.out.println("cache size (% of URLs)   entries     memory @ 500 B     hit ratio    database reads/s at 347K peak redirects/s");
        for (double pct : new double[]{0.1, 1, 5, 10, 20}) {
            int cap = (int) (urls * pct / 100); LinkedHashMap<Integer, Boolean> lru = new LinkedHashMap<>(16, 0.75f, true) { protected boolean removeEldestEntry(Map.Entry<Integer, Boolean> e) { return size() > cap; } };
            long hits = 0; for (int k : w) { if (lru.get(k) != null) hits++; else lru.put(k, true); }
            double h = (double) hits / requests; System.out.printf("%12.1f%%        %,9d      %7.1f MB        %5.1f%%        %,12.0f%n", pct, cap, cap * 500.0 / 1e6, 100 * h, 347_200 * (1 - h));
        }
    }

    static void clickCounting() {
        int urls = 2_000_000; double clicksPerSec = 115_700;
        System.out.println("\n== D) Click counting: one database update per click vs in-memory aggregation, flushed every W seconds (115,700 clicks/s, 2M URLs, Zipf 0.9) ==");
        System.out.println("approach                                   DB writes per second     distinct URLs per window / clicks per window");
        System.out.printf("%-42s %,12.0f%n", "update a counter row on every click", clicksPerSec);
        for (int window : new int[]{5, 60}) {
            int perWindow = (int) (clicksPerSec * window); int[] stream = zipf(urls, 0.9, perWindow, 7 + window);
            Set<Integer> d = new HashSet<>(); for (int x : stream) d.add(x);
            System.out.printf("%-42s %,12.0f     %,d / %,d  (%.1fx fewer writes)%n", "aggregate in memory, flush every " + window + " s", (double) d.size() / window, d.size(), perWindow, (double) perWindow / d.size());
        }
        System.out.println("trade-off: counts lag by up to W seconds and an app-server crash loses the unflushed window unless clicks are also appended to a log/stream (Kafka) and counted downstream.");
    }

    public static void main(String[] a) { collisions(); counters(); cacheSizing(); clickCounting(); }
}
```
**Full output**
```text
== A) Random short codes: how often does an insert collide with an existing code? ==
code length   space (62^k)        codes already stored   measured collision rate   theory n/N
     4            14,776,336             100,000            0.7035%                 0.6768%
     4            14,776,336           1,000,000            6.7805%                 6.7676%
     4            14,776,336           5,000,000           33.8650%                33.8379%
     4            14,776,336          10,000,000           67.6610%                67.6758%
     7       3,521,614,606,208       1,000,000,000        (not simulated)               0.0284%   <- expected extra DB round trips per insert = n/N
     7       3,521,614,606,208      18,250,000,000        (not simulated)               0.5182%   <- expected extra DB round trips per insert = n/N
     7       3,521,614,606,208     182,500,000,000        (not simulated)               5.1823%   <- expected extra DB round trips per insert = n/N
hash(url) truncated to 7 chars, birthday bound: P(any collision) after n URLs ~ 1 - exp(-n^2/(2N)):
   n = 100,000 -> P(at least one collision) = 0.1419%  (and EVERY new URL has an n/N chance of colliding, so a DB uniqueness check + retry is mandatory)
   n = 1,000,000 -> P(at least one collision) = 13.2362%  (and EVERY new URL has an n/N chance of colliding, so a DB uniqueness check + retry is mandatory)
   n = 10,000,000 -> P(at least one collision) = 99.9999%  (and EVERY new URL has an n/N chance of colliding, so a DB uniqueness check + retry is mandatory)

== B) Counter-based IDs scrambled by a bijection (no collisions, no sequential guessing) ==
sequential id -> plain base62      scrambled code (a*id+b mod 62^7, a = golden-ratio multiplier)
   1            0000001                 cJr9t8S
   2            0000002                 EdZxlWN
   3            0000003                 qxIlduI
   4            0000004                 TH1ZWID
   5            0000005                 5akNOg8
2,000,000 consecutive ids: duplicate codes = 0 (bijection => 0); adjacent ids sharing a first character = 0.0% (a random sequence would be 1.6%)
(An affine scramble is not secret: two (id, code) pairs reveal a and b. If codes must resist enumeration, use a keyed permutation, e.g. a Feistel network / format-preserving encryption.)
range allocation: app servers fetch a block of IDs from the central counter and hand them out locally
   block      1 -> central counter calls per 1,000,000 IDs: 1,000,000 ; IDs lost (gap) if a server crashes: up to 0
   block    100 -> central counter calls per 1,000,000 IDs:    10,000 ; IDs lost (gap) if a server crashes: up to 99
   block  1,000 -> central counter calls per 1,000,000 IDs:     1,000 ; IDs lost (gap) if a server crashes: up to 999
   block 10,000 -> central counter calls per 1,000,000 IDs:       100 ; IDs lost (gap) if a server crashes: up to 9,999

== C) Redirect cache sizing: LRU hit ratio, 2,000,000 short URLs, Zipf(0.9) popularity, 6,000,000 redirects ==
cache size (% of URLs)   entries     memory @ 500 B     hit ratio    database reads/s at 347K peak redirects/s
         0.1%            2,000          1.0 MB         23.9%             264,163
         1.0%           20,000         10.0 MB         40.7%             206,029
         5.0%          100,000         50.0 MB         56.0%             152,656
        10.0%          200,000        100.0 MB         63.7%             125,892
        20.0%          400,000        200.0 MB         71.8%              97,799

== D) Click counting: one database update per click vs in-memory aggregation, flushed every W seconds (115,700 clicks/s, 2M URLs, Zipf 0.9) ==
approach                                   DB writes per second     distinct URLs per window / clicks per window
update a counter row on every click             115,700
aggregate in memory, flush every 5 s             49,020     245,099 / 578,500  (2.4x fewer writes)
aggregate in memory, flush every 60 s            20,707     1,242,401 / 6,942,000  (5.6x fewer writes)
trade-off: counts lag by up to W seconds and an app-server crash loses the unflushed window unless clicks are also appended to a log/stream (Kafka) and counted downstream.
```
**Experiments:** (1) Replace the affine permutation with a 4-round **Feistel network** keyed by a secret and verify bijectivity over a 24-bit domain by exhaustion. (2) Simulate a **KGS** that pre-generates 10 M random codes and hands out blocks; measure the rate of duplicates across two generators (sharing no state). (3) Simulate a **viral link**: 500 K req/s on one key with and without single-flight and a 30 s CDN TTL; count origin requests. (4) Change the Zipf exponent to 1.1 and 0.7 and recompute the cache table. (5) Implement **expiry** with a lazy check plus sweeper and measure resident memory over a 90-day trace.

## Lab 2: Rate limiters and caches
```java
import java.util.*;

public class RateLimiterLab {
    interface Limiter { boolean allow(long key, long nowMs); }

    static class TokenBucket implements Limiter {
        final double ratePerMs; final double burst; final HashMap<Long, double[]> st = new HashMap<>();      // key -> {tokens, lastMs}
        TokenBucket(double perMinute, double burst) { this.ratePerMs = perMinute / 60_000.0; this.burst = burst; }
        public boolean allow(long key, long now) { double[] s = st.computeIfAbsent(key, k -> new double[]{burst, now}); s[0] = Math.min(burst, s[0] + (now - s[1]) * ratePerMs); s[1] = now; if (s[0] >= 1) { s[0] -= 1; return true; } return false; }
    }
    static class FixedWindow implements Limiter {
        final long limit, windowMs; final HashMap<Long, long[]> st = new HashMap<>();                          // key -> {windowId, count}
        FixedWindow(long limit, long windowMs) { this.limit = limit; this.windowMs = windowMs; }
        public boolean allow(long key, long now) { long w = now / windowMs; long[] s = st.computeIfAbsent(key, k -> new long[]{w, 0}); if (s[0] != w) { s[0] = w; s[1] = 0; } if (s[1] < limit) { s[1]++; return true; } return false; }
    }
    static class SlidingLog implements Limiter {
        final long limit, windowMs; final HashMap<Long, ArrayDeque<Long>> st = new HashMap<>();
        SlidingLog(long limit, long windowMs) { this.limit = limit; this.windowMs = windowMs; }
        public boolean allow(long key, long now) { ArrayDeque<Long> q = st.computeIfAbsent(key, k -> new ArrayDeque<>()); while (!q.isEmpty() && q.peekFirst() <= now - windowMs) q.pollFirst(); if (q.size() < limit) { q.addLast(now); return true; } return false; }
    }
    static class SlidingCounter implements Limiter {                                                           // weighted previous window + current window
        final long limit, windowMs; final HashMap<Long, long[]> st = new HashMap<>();                          // key -> {windowId, current, previous}
        SlidingCounter(long limit, long windowMs) { this.limit = limit; this.windowMs = windowMs; }
        public boolean allow(long key, long now) {
            long w = now / windowMs; long[] s = st.computeIfAbsent(key, k -> new long[]{w, 0, 0});
            if (s[0] != w) { s[2] = (s[0] == w - 1) ? s[1] : 0; s[1] = 0; s[0] = w; }
            double weight = 1.0 - (double) (now % windowMs) / windowMs; double est = s[1] + s[2] * weight;
            if (est < limit) { s[1]++; return true; } return false;
        }
    }
    static Limiter make(int which) { return switch (which) { case 0 -> new TokenBucket(100, 100); case 1 -> new FixedWindow(100, 60_000); case 2 -> new SlidingLog(100, 60_000); default -> new SlidingCounter(100, 60_000); }; }
    static final String[] NAMES = {"token bucket (100/min, burst 100)", "fixed window counter (100 / 60 s)", "sliding window log (exact)", "sliding window counter (weighted)"};

    static long maxInAnyWindow(List<Long> accepted, long windowMs) { long best = 0; int j = 0; for (int i = 0; i < accepted.size(); i++) { while (accepted.get(i) - accepted.get(j) >= windowMs) j++; best = Math.max(best, i - j + 1); } return best; }

    static void boundaryBurst() {
        System.out.println("== 1) Boundary burst. Limit = 100 requests per 60 s. A client sends 100 requests at t=59.90-59.99 s and 100 more at t=60.01-60.10 s. ==");
        System.out.println("algorithm                                 accepted    most accepted in any 60-second window    verdict");
        for (int w = 0; w < 4; w++) {
            Limiter l = make(w); List<Long> acc = new ArrayList<>();
            for (int i = 0; i < 100; i++) { long t = 59_900 + i; if (l.allow(1, t)) acc.add(t); }
            for (int i = 0; i < 100; i++) { long t = 60_010 + i; if (l.allow(1, t)) acc.add(t); }
            long m = maxInAnyWindow(acc, 60_000);
            System.out.printf("%-42s %5d        %5d                                    %s%n", NAMES[w], acc.size(), m, m > 100 ? "VIOLATES the limit (" + m + " in one minute)" : "respects the limit");
        }
    }

    static void honestClients() {
        System.out.println("\n== 2) Well-behaved clients with random (Poisson) arrivals averaging below the limit. What fraction of their requests is rejected? (1,000 simulated minutes per row) ==");
        System.out.println("offered rate     " + String.join(" | ", new String[]{"token bucket", "fixed window", "sliding log", "sliding counter"}));
        for (int rate : new int[]{60, 80, 90, 100, 120}) {
            StringBuilder sb = new StringBuilder(String.format("%4d req/min   ", rate));
            for (int w = 0; w < 4; w++) {
                Limiter l = make(w); Random r = new Random(5); long t = 0, total = 0, rej = 0; double lambdaMs = rate / 60_000.0;
                while (t < 1000L * 60_000) { t += (long) Math.max(1, -Math.log(1 - r.nextDouble()) / lambdaMs); total++; if (!l.allow(1, t)) rej++; }
                sb.append(String.format("   %6.2f%%      ", 100.0 * rej / total));
            }
            System.out.println(sb);
        }
        System.out.println("(an average-rate ideal would reject 0% below 100/min and (rate-100)/rate above it, e.g. 16.7% at 120/min; random clustering of arrivals pushes exact-window limiters above that)");
    }

    static void cost() {
        System.out.println("\n== 3) Memory per key and single-thread speed (1,000,000 distinct keys, limit 100/min) ==");
        System.out.println("algorithm                                 state per key      memory for 50M active keys     checks/s (1 thread)");
        long[] bytes = {16, 16, 8 * 100 + 40, 24}; String[] mem = new String[4];
        for (int w = 0; w < 4; w++) mem[w] = String.format("%.1f GB", 50e6 * (bytes[w] + 48) / 1e9);            // +48 B JVM/hash-map overhead per entry as a rough figure
        for (int w = 0; w < 4; w++) {
            Limiter l = make(w); Random r = new Random(1); long now = 0; int n = 2_000_000; long[] keys = new long[n]; for (int i = 0; i < n; i++) keys[i] = r.nextInt(1_000_000);
            for (int i = 0; i < 300_000; i++) l.allow(keys[i], now += 1);                                            // warm-up
            long t0 = System.nanoTime(); for (int i = 0; i < n; i++) l.allow(keys[i], now += 1); double sec = (System.nanoTime() - t0) / 1e9;
            System.out.printf("%-42s %5d B (+ map overhead)   %-28s %,12.0f%n", NAMES[w], bytes[w], mem[w], n / sec);
        }
    }

    static void distributed() {
        System.out.println("\n== 4) Distributed enforcement. Global limit 100 requests/min per user; 10 gateways; each request lands on a random gateway. ==");
        System.out.println("strategy                                                              honest user @ 80/min: rejected     abusive user @ 1,000/min: allowed per minute");
        String[] strat = {"each gateway enforces the FULL limit (100/min) locally", "each gateway enforces limit/10 = 10/min locally", "central counter (e.g. Redis INCR), exact"};
        for (int s = 0; s < 3; s++) {
            double[] rejected = new double[2]; double[] allowed = new double[2]; int[] rates = {80, 1000};
            for (int ri = 0; ri < 2; ri++) {
                Random r = new Random(9); long total = 0, rej = 0, acc = 0; int minutes = 200;
                for (int m = 0; m < minutes; m++) {
                    int[] count = new int[10]; int central = 0;
                    for (int i = 0; i < rates[ri]; i++) {
                        total++; int g = r.nextInt(10); boolean ok;
                        if (s == 0) ok = count[g]++ < 100; else if (s == 1) ok = count[g]++ < 10; else ok = central++ < 100;
                        if (ok) acc++; else rej++;
                    }
                }
                rejected[ri] = 100.0 * rej / total; allowed[ri] = (double) acc / minutes;
            }
            System.out.printf("%-70s %6.1f%%                          %8.0f%n", strat[s], rejected[0], allowed[1]);
        }
        System.out.println("local-only limits are either unsafe (10x over the limit) or unfair (honest users blocked by imbalance); production systems use a shared counter (Redis/Lua), or local buckets topped up in batches from a central one.");
    }

    public static void main(String[] a) { boundaryBurst(); honestClients(); cost(); distributed(); }
}
```
```java
import java.util.*;

public class CacheDesignLab {
    // ---------------------------------------------------------------- O(1) LRU: hash map + doubly linked list
    static class Lru<K, V> {
        class Node { K k; V v; Node prev, next; Node(K k, V v) { this.k = k; this.v = v; } }
        final int cap; final HashMap<K, Node> map = new HashMap<>(); final Node head = new Node(null, null), tail = new Node(null, null);
        Lru(int cap) { this.cap = cap; head.next = tail; tail.prev = head; }
        V get(K k) { Node n = map.get(k); if (n == null) return null; unlink(n); addFirst(n); return n.v; }
        void put(K k, V v) { Node n = map.get(k); if (n != null) { n.v = v; unlink(n); addFirst(n); return; } if (map.size() == cap) { Node lru = tail.prev; unlink(lru); map.remove(lru.k); } n = new Node(k, v); map.put(k, n); addFirst(n); }
        void unlink(Node n) { n.prev.next = n.next; n.next.prev = n.prev; }
        void addFirst(Node n) { n.next = head.next; n.prev = head; head.next.prev = n; head.next = n; }
        int size() { return map.size(); }
    }
    // ---------------------------------------------------------------- O(1) LFU: key -> node, frequency -> doubly-linked-order set (LinkedHashSet) + minFreq
    static class Lfu<K, V> {
        final int cap; int minFreq = 0; final HashMap<K, V> vals = new HashMap<>(); final HashMap<K, Integer> freq = new HashMap<>(); final HashMap<Integer, LinkedHashSet<K>> buckets = new HashMap<>();
        Lfu(int cap) { this.cap = cap; }
        V get(K k) { if (!vals.containsKey(k)) return null; touch(k); return vals.get(k); }
        void put(K k, V v) {
            if (cap == 0) return;
            if (vals.containsKey(k)) { vals.put(k, v); touch(k); return; }
            if (vals.size() == cap) { LinkedHashSet<K> s = buckets.get(minFreq); K ev = s.iterator().next(); s.remove(ev); if (s.isEmpty()) buckets.remove(minFreq); vals.remove(ev); freq.remove(ev); }
            vals.put(k, v); freq.put(k, 1); buckets.computeIfAbsent(1, f -> new LinkedHashSet<>()).add(k); minFreq = 1;
        }
        void touch(K k) { int f = freq.get(k); LinkedHashSet<K> s = buckets.get(f); s.remove(k); if (s.isEmpty()) { buckets.remove(f); if (minFreq == f) minFreq++; } freq.put(k, f + 1); buckets.computeIfAbsent(f + 1, x -> new LinkedHashSet<>()).add(k); }
    }
    // ---------------------------------------------------------------- reference implementation (obviously correct, O(n)) used for differential testing
    static class NaiveLru { final int cap; final ArrayList<Integer> order = new ArrayList<>(); final HashMap<Integer, Integer> m = new HashMap<>();
        NaiveLru(int cap) { this.cap = cap; }
        Integer get(int k) { if (!m.containsKey(k)) return null; order.remove((Integer) k); order.add(k); return m.get(k); }
        void put(int k, int v) { if (m.containsKey(k)) order.remove((Integer) k); else if (m.size() == cap) { int ev = order.remove(0); m.remove(ev); } m.put(k, v); order.add(k); } }

    static void correctness() {
        Random r = new Random(1); int trials = 2000, mismatches = 0; long ops = 0;
        for (int t = 0; t < trials; t++) { int cap = 1 + r.nextInt(8); Lru<Integer, Integer> a = new Lru<>(cap); NaiveLru b = new NaiveLru(cap);
            for (int i = 0; i < 100; i++) { int k = r.nextInt(12); ops++; if (r.nextBoolean()) { Integer x = a.get(k), y = b.get(k); if (!Objects.equals(x, y)) mismatches++; } else { int v = r.nextInt(1000); a.put(k, v); b.put(k, v); } } }
        System.out.printf("== 1) Differential test: O(1) LRU vs naive O(n) reference: %,d random operations over %,d random caches (capacity 1-8) -> mismatches = %d%n", ops, trials, mismatches);
        Lfu<Integer, Integer> lf = new Lfu<>(2); lf.put(1, 1); lf.put(2, 2); lf.get(1); lf.put(3, 3);
        System.out.printf("LFU sanity: capacity 2; put 1, put 2, get 1, put 3 -> key 2 (least frequently used) evicted: get(2) = %s, get(1) = %s, get(3) = %s%n", lf.get(2), lf.get(1), lf.get(3));
    }

    static int[] zipfDraw(int n, double s, int count, long seed, int shift) {
        double[] cdf = new double[n]; double sum = 0; for (int k = 1; k <= n; k++) { sum += 1.0 / Math.pow(k, s); cdf[k - 1] = sum; } Random r = new Random(seed); int[] out = new int[count];
        for (int i = 0; i < count; i++) { double u = r.nextDouble() * sum; int lo = 0, hi = n - 1; while (lo < hi) { int mid = (lo + hi) >>> 1; if (cdf[mid] < u) lo = mid + 1; else hi = mid; } out[i] = (lo + shift) % n; }
        return out;
    }

    static void hitRatios() {
        int n = 100_000, cap = 2_000, half = 1_000_000;
        int[] first = zipfDraw(n, 1.0, half, 3, 0), second = zipfDraw(n, 1.0, half, 4, 50_000);              // second half: popularity shifts (a different set of items becomes hot)
        System.out.println("\n== 2) LRU vs LFU hit ratio (100,000 items, Zipf 1.0, cache holds 2,000). Popularity shifts completely at the midpoint. ==");
        System.out.println("policy        hit ratio 1st half (stable)   hit ratio 2nd half (after the hot set changes)   2nd half, first 100k requests only");
        for (int p = 0; p < 2; p++) {
            Lru<Integer, Boolean> lru = new Lru<>(cap); Lfu<Integer, Boolean> lfu = new Lfu<>(cap); long h1 = 0, h2 = 0, h2early = 0;
            for (int pass = 0; pass < 2; pass++) { int[] w = pass == 0 ? first : second; for (int i = 0; i < w.length; i++) { int k = w[i]; boolean hit; if (p == 0) { hit = lru.get(k) != null; if (!hit) lru.put(k, true); } else { hit = lfu.get(k) != null; if (!hit) lfu.put(k, true); }
                if (hit) { if (pass == 0) h1++; else { h2++; if (i < 100_000) h2early++; } } } }
            System.out.printf("%-10s          %5.1f%%                        %5.1f%%                                        %5.1f%%%n", p == 0 ? "LRU" : "LFU", 100.0 * h1 / half, 100.0 * h2 / half, 100.0 * h2early / 100_000);
        }
    }

    static void speed() {
        int n = 1_000_000, cap = 100_000; Random r = new Random(2); int[] keys = zipfDraw(1_000_000, 1.0, 3_000_000, 5, 0);
        Lru<Integer, Integer> lru = new Lru<>(cap); for (int i = 0; i < 500_000; i++) { if (lru.get(keys[i]) == null) lru.put(keys[i], i); }
        long t0 = System.nanoTime(); for (int i = 500_000; i < keys.length; i++) { if (lru.get(keys[i]) == null) lru.put(keys[i], i); } double sec = (System.nanoTime() - t0) / 1e9;
        System.out.printf("%n== 3) Speed: single-thread LRU, capacity %,d, %,d operations on a Zipf stream: %.2f million ops/s%n", cap, keys.length - 500_000, (keys.length - 500_000) / sec / 1e6);
        System.out.println("(a global lock around this structure serializes all threads; production caches shard the keyspace into independent segments or use lock-free/approximate policies such as Caffeine's W-TinyLFU.)");
    }

    public static void main(String[] a) { correctness(); hitRatios(); speed(); }
}
```
**Full output (rate limiter)**
```text
== 1) Boundary burst. Limit = 100 requests per 60 s. A client sends 100 requests at t=59.90-59.99 s and 100 more at t=60.01-60.10 s. ==
algorithm                                 accepted    most accepted in any 60-second window    verdict
token bucket (100/min, burst 100)            100          100                                    respects the limit
fixed window counter (100 / 60 s)            200          200                                    VIOLATES the limit (200 in one minute)
sliding window log (exact)                   100          100                                    respects the limit
sliding window counter (weighted)            101          101                                    VIOLATES the limit (101 in one minute)

== 2) Well-behaved clients with random (Poisson) arrivals averaging below the limit. What fraction of their requests is rejected? (1,000 simulated minutes per row) ==
offered rate     token bucket | fixed window | sliding log | sliding counter
  60 req/min        0.00%           0.00%           0.00%           0.00%      
  80 req/min        0.00%           0.04%           0.35%           0.11%      
  90 req/min        0.00%           0.77%           2.80%           1.66%      
 100 req/min        0.43%           4.07%           7.59%           5.98%      
 120 req/min       16.61%          16.79%          19.68%          18.32%      
(an average-rate ideal would reject 0% below 100/min and (rate-100)/rate above it, e.g. 16.7% at 120/min; random clustering of arrivals pushes exact-window limiters above that)

== 3) Memory per key and single-thread speed (1,000,000 distinct keys, limit 100/min) ==
algorithm                                 state per key      memory for 50M active keys     checks/s (1 thread)
token bucket (100/min, burst 100)             16 B (+ map overhead)   3.2 GB                          2,347,859
fixed window counter (100 / 60 s)             16 B (+ map overhead)   3.2 GB                          2,831,771
sliding window log (exact)                   840 B (+ map overhead)   44.4 GB                         1,449,533
sliding window counter (weighted)             24 B (+ map overhead)   3.6 GB                          2,800,907

== 4) Distributed enforcement. Global limit 100 requests/min per user; 10 gateways; each request lands on a random gateway. ==
strategy                                                              honest user @ 80/min: rejected     abusive user @ 1,000/min: allowed per minute
each gateway enforces the FULL limit (100/min) locally                    0.0%                               962
each gateway enforces limit/10 = 10/min locally                           5.0%                               100
central counter (e.g. Redis INCR), exact                                  0.0%                               100
local-only limits are either unsafe (10x over the limit) or unfair (honest users blocked by imbalance); production systems use a shared counter (Redis/Lua), or local buckets topped up in batches from a central one.
```
**Full output (cache design)**
```text
== 1) Differential test: O(1) LRU vs naive O(n) reference: 200,000 random operations over 2,000 random caches (capacity 1-8) -> mismatches = 0
LFU sanity: capacity 2; put 1, put 2, get 1, put 3 -> key 2 (least frequently used) evicted: get(2) = null, get(1) = 1, get(3) = 3

== 2) LRU vs LFU hit ratio (100,000 items, Zipf 1.0, cache holds 2,000). Popularity shifts completely at the midpoint. ==
policy        hit ratio 1st half (stable)   hit ratio 2nd half (after the hot set changes)   2nd half, first 100k requests only
LRU                  57.3%                         57.3%                                         56.8%
LFU                  64.8%                         25.5%                                         23.5%

== 3) Speed: single-thread LRU, capacity 100,000, 2,500,000 operations on a Zipf stream: 4.10 million ops/s
(a global lock around this structure serializes all threads; production caches shard the keyspace into independent segments or use lock-free/approximate policies such as Caffeine's W-TinyLFU.)
```
**Experiments:** (1) Add a **leaky bucket** and a **token bucket with lease-based sync** between 10 simulated gateways and a central store; measure overshoot vs central calls. (2) Add a Lua-like **atomic** implementation using `synchronized` and show a race when you split read and write. (3) Implement **CLOCK** and compare hit ratio and speed with the linked-list LRU, then a **striped** version with 8 locks across 4 threads. (4) Add **W-TinyLFU-style** admission (count-min sketch of frequencies with periodic halving) to LFU and re-run the popularity shift. (5) Add byte-weighted capacity and measure memory with variable-size values.

## Lab 3: Resumable upload and chat ordering
```java
import java.util.*;

public class UploadChatLab {
    static double exp(Random r, double mean) { return -Math.log(1 - r.nextDouble()) * mean; }

    // ======================================================================= resumable chunked upload on an unreliable connection
    static void upload() {
        double bw = 1.25e6;                 // 10 Mbit/s
        double mtbf = 300;                  // the connection drops on average every 5 minutes (mobile / flaky Wi-Fi)
        double reconnect = 5, deadline = 2 * 3600; int runs = 1500; Random r = new Random(1);
        System.out.println("== A) Uploading over a 10 Mbit/s connection that drops every ~5 minutes (exponential), 5 s to reconnect, 2-hour deadline. " + runs + " runs per cell. ==");
        System.out.println("file size   strategy                       finished within 2 h   mean time (min)   bytes sent / file size");
        long[] sizes = {100_000_000L, 1_000_000_000L, 5_000_000_000L}; String[] sn = {"100 MB", "1 GB", "5 GB"};
        long[] chunks = {Long.MAX_VALUE, 64_000_000L, 8_000_000L, 1_000_000L}; String[] cn = {"single request (restart on failure)", "64 MB chunks, resume", "8 MB chunks, resume", "1 MB chunks, resume (0.1 s overhead each)"};
        for (int si = 0; si < sizes.length; si++) for (int ci = 0; ci < chunks.length; ci++) {
            if (si == 2 && ci == 0) { /* still run: shows near-zero success */ }
            int ok = 0; double timeSum = 0, sentSum = 0; int finished = 0;
            for (int run = 0; run < runs; run++) {
                long size = sizes[si], chunk = Math.min(chunks[ci], size); double t = 0; long sent = 0; long done = 0; double untilDrop = exp(r, mtbf);
                while (done < size && t < deadline) {
                    long thisChunk = Math.min(chunk, size - done); double need = thisChunk / bw + (ci == 3 ? 0.1 : 0);
                    if (untilDrop >= need) { t += need; untilDrop -= need; sent += thisChunk; done += thisChunk; }                // chunk completes
                    else { double partial = untilDrop; sent += (long) (partial * bw); t += partial + reconnect; untilDrop = exp(r, mtbf); }   // dropped mid-chunk: the partial chunk is wasted, retry that chunk
                }
                if (done >= size && t <= deadline) { ok++; timeSum += t; sentSum += sent; finished++; }
            }
            System.out.printf("%-8s    %-42s %6.1f%%            %8s        %s%n", sn[si], cn[ci], 100.0 * ok / runs, finished == 0 ? "-" : String.format("%.1f", timeSum / finished / 60), finished == 0 ? "-" : String.format("%.2fx", sentSum / finished / sizes[si]));
        }
        System.out.println("minimum time with no failures: 100 MB = 1.3 min, 1 GB = 13.3 min, 5 GB = 66.7 min");
    }

    // ======================================================================= chat ordering, duplicates, loss, offline sync
    static void chat() {
        int conversations = 500, perConv = 100; Random r = new Random(7);
        record Ev(double t, int conv, int seq, boolean fetched) {}
        List<Ev> arrivals = new ArrayList<>(); int lost = 0, dups = 0;
        for (int c = 0; c < conversations; c++) for (int s = 1; s <= perConv; s++) {
            double sent = s * 100.0;                                                   // 10 messages per second per conversation
            if (r.nextDouble() < 0.01) { lost++; } else arrivals.add(new Ev(sent + r.nextDouble() * 500, c, s, false));   // 1% of first deliveries never arrive
            if (r.nextDouble() < 0.03) { dups++; arrivals.add(new Ev(sent + r.nextDouble() * 500, c, s, false)); }       // 3% are delivered twice (retransmission after a lost ack)
        }
        arrivals.sort(Comparator.comparingDouble(Ev::t));
        // naive client: shows every message in arrival order
        Map<Integer, List<Integer>> naive = new HashMap<>(); for (Ev e : arrivals) naive.computeIfAbsent(e.conv(), k -> new ArrayList<>()).add(e.seq());
        long dupShown = 0, inversions = 0, missingShown = 0;
        for (var en : naive.entrySet()) { List<Integer> l = en.getValue(); Set<Integer> seen = new HashSet<>(); for (int x : l) if (!seen.add(x)) dupShown++; int maxSoFar = 0; for (int x : l) { if (x < maxSoFar) inversions++; maxSoFar = Math.max(maxSoFar, x); } missingShown += perConv - seen.size(); }
        // sequence-aware client: dedupe by seq, buffer out-of-order, fetch missing seqs after 1 s
        long dupShown2 = 0, inversions2 = 0, missing2 = 0; double sumDelay = 0, sumNaiveDelay = 0; long cnt = 0; double maxDelay = 0; List<Double> delays = new ArrayList<>();
        Map<Integer, List<Ev>> byConv = new HashMap<>(); for (Ev e : arrivals) byConv.computeIfAbsent(e.conv(), k -> new ArrayList<>()).add(e);
        for (int c = 0; c < conversations; c++) {
            List<Ev> evs = new ArrayList<>(byConv.getOrDefault(c, List.of())); Map<Integer, Double> have = new HashMap<>(); for (Ev e : evs) have.merge(e.seq(), e.t(), Math::min);
            double lastShown = 0; int next = 1; 
            for (int s = 1; s <= perConv; s++) {
                double arrive;
                if (have.containsKey(s)) arrive = have.get(s); else arrive = Math.max(lastShown, (perConv > 0 ? s * 100.0 : 0)) + 1000 + 50;      // gap detected -> fetch after a 1 s timeout (+50 ms round trip)
                double shown = Math.max(arrive, lastShown);                                                      // can only be shown after all earlier seqs
                double delay = shown - Math.min(arrive, shown); delays.add(shown - (s * 100.0)); lastShown = shown; sumDelay += shown - arrive; cnt++; maxDelay = Math.max(maxDelay, shown - arrive);
            }
        }
        Collections.sort(delays);
        System.out.println("\n== B) Chat delivery: 500 conversations x 100 messages; network adds 0-500 ms jitter, delivers 3% of messages twice, and drops 1% of first deliveries. ==");
        System.out.printf("sent messages: %,d ; lost on first delivery: %,d ; duplicate deliveries: %,d%n", conversations * perConv, lost, dups);
        System.out.println("client                                            duplicates shown   out-of-order pairs shown   messages never shown   mean extra wait for ordering");
        System.out.printf("%-48s %8d           %8d                   %8d               -%n", "naive (append on arrival)", dupShown, inversions, missingShown);
        System.out.printf("%-48s %8d           %8d                   %8d               %.0f ms (p99 end-to-end %.0f ms)%n", "sequence-aware (dedupe, reorder, gap fetch)", dupShown2, inversions2, missing2, sumDelay / cnt, delays.get((int) (delays.size() * 0.99)));
        // offline sync with cursors
        int convSeq = 10_000, lastSeen = 9_940; int missed = convSeq - lastSeen;
        System.out.printf("offline sync: client last saw seq %,d of %,d; on reconnect it asks 'messages after %,d' and the server returns exactly %d messages in order (no timestamps needed, no gaps, idempotent if repeated)%n", lastSeen, convSeq, lastSeen, missed);
    }

    public static void main(String[] a) { upload(); chat(); }
}
```
**Full output**
```text
== A) Uploading over a 10 Mbit/s connection that drops every ~5 minutes (exponential), 5 s to reconnect, 2-hour deadline. 1500 runs per cell. ==
file size   strategy                       finished within 2 h   mean time (min)   bytes sent / file size
100 MB      single request (restart on failure)         100.0%                 1.6        1.15x
100 MB      64 MB chunks, resume                        100.0%                 1.5        1.07x
100 MB      8 MB chunks, resume                         100.0%                 1.4        1.01x
100 MB      1 MB chunks, resume (0.1 s overhead each)   100.0%                 1.5        1.00x
1 GB        single request (restart on failure)          83.1%                49.8        3.68x
1 GB        64 MB chunks, resume                        100.0%                14.8        1.09x
1 GB        8 MB chunks, resume                         100.0%                13.7        1.01x
1 GB        1 MB chunks, resume (0.1 s overhead each)   100.0%                15.3        1.00x
5 GB        single request (restart on failure)           0.0%                   -        -
5 GB        64 MB chunks, resume                        100.0%                73.9        1.09x
5 GB        8 MB chunks, resume                         100.0%                68.5        1.01x
5 GB        1 MB chunks, resume (0.1 s overhead each)   100.0%                76.4        1.00x
minimum time with no failures: 100 MB = 1.3 min, 1 GB = 13.3 min, 5 GB = 66.7 min

== B) Chat delivery: 500 conversations x 100 messages; network adds 0-500 ms jitter, delivers 3% of messages twice, and drops 1% of first deliveries. ==
sent messages: 50,000 ; lost on first delivery: 521 ; duplicate deliveries: 1,456
client                                            duplicates shown   out-of-order pairs shown   messages never shown   mean extra wait for ordering
naive (append on arrival)                            1437              20550                        502               -
sequence-aware (dedupe, reorder, gap fetch)             0                  0                          0               95 ms (p99 end-to-end 1266 ms)
offline sync: client last saw seq 9,940 of 10,000; on reconnect it asks 'messages after 9,940' and the server returns exactly 60 messages in order (no timestamps needed, no gaps, idempotent if repeated)
```
**Experiments:** (1) Add **parallel chunk uploads** (4 workers) and measure completion time on a link with random drops. (2) Add **adaptive chunk size** (halve after a failure, double after 10 successes). (3) In the chat lab, add **gap-fill via cursor sync** instead of a fixed 1 s timeout and measure p99. (4) Simulate **reconnect storms**: 150 K clients reconnect after a gateway crash, with and without jittered backoff; measure peak connection attempts per second.

---

# Exercises

### A. Variations (change one requirement and redesign)
1. **URL shortener at 10× writes (1 B new URLs/day).** What changes? Compute the occupancy of a 7-character space after 5 years and decide the code length.
2. **URL shortener with strict privacy**: links must not be enumerable and must expire in 24 hours. Redesign code generation and storage.
3. **Rate limiter for 200 M active keys** with sliding-log accuracy for 1,000-request limits: memory? Propose an alternative.
4. **File upload for files up to 5 TB** (object limit 5 TB, 10,000 parts): minimum part size?
5. **Chat with 30 M concurrent connections** and 200 K connections per gateway: gateway count and memory at 20 KB per connection.
6. **Pastebin with 100 MB pastes allowed**: what changes in the upload path and abuse controls?
7. **LRU cache bounded to 1 GB** with values from 100 B to 10 MB: design the accounting and what policy issues arise.
8. **Chat: group of 500,000 members.** Why does the design break and what do you change?

### B. Calculations
9. A 4-character base62 space has 14,776,336 codes. After inserting 5 M random codes, what fraction of new inserts collide? Expected retries per insert?
10. A token bucket allows 100 req/min with burst 20. A client idle for a minute sends 50 requests instantly. How many pass? How many pass in the next 6 seconds if it keeps sending?
11. Fixed-window counter, limit 60/min: show a pattern letting 120 requests through within 2 seconds.
12. Upload: 2 GB over a 5 Mbit/s link; part size 8 MB. Number of parts; ideal time; expected resend overhead if drops occur every 3 minutes on average (rough estimate: chance a part fails ≈ part time / MTBF).
13. Chat: 50 M DAU; messages are 120 B; store in a Cassandra-like store with replication factor 3 and 40% storage overhead. Raw bytes per year; replicated bytes per year including overhead.
14. LRU cache hit ratio improves from 40.7% to 56.0% when the cache grows 1% → 5% of items. If the miss path costs 10 ms and the hit path 1 ms, compute average latency before/after and the DB read rate at 100 K requests/s.

### C. Debugging
15. After a deploy, redirect latency doubles and the database CPU hits 90%. The cache hit ratio dropped from 85% to 30%. List four hypotheses and checks.
16. Rate limiter users report being blocked at 70 requests/min when the limit is 100. What could cause this?
17. Chat users see messages out of order only in large groups. Where might the sequencing break?
18. Large uploads fail at 99% every time. What do you check?
19. The cache's memory grows without bound although capacity is 100,000 entries. Possible causes?

### D. Design critique
20. “I'd store pastes as BLOBs in Postgres and put Redis in front.” Evaluate.
21. “I'll use `System.currentTimeMillis()` as the ordering key for chat messages.” Evaluate.
22. “The rate limiter calls the DB on every request.” Evaluate and fix.

### E. Interview-style
23. Give a two-minute answer to “How do you generate unique short URLs at scale?”
24. “Walk me through what happens when a user sends a chat message, end to end.”
25. “How do you make an LRU cache thread-safe without a global lock?”

### F. Teach-back
26. Explain why fixed-window rate limiting can allow twice the limit, using a ticket-booth analogy.

---

# Full answer key

**1.** 1 B/day → 11.6 K writes/s (still modest). Five years = 1.825 T URLs; 62^7 = 3.52 T → **52%** occupancy: random codes collide on ~half of inserts, so move to **8 characters** (62^8 = 218 T → 0.84%) or counter-based collision-free codes with a keyed permutation. Storage: 1 B × 500 B × 365 = 182 TB/year raw (about 2.7 PB replicated for five years): shard the KV store over thousands of logical shards; ID allocation stays easy (11.6 K IDs/s ÷ blocks of 10,000 ≈ 1.2 allocation calls/s).

**2.** Use 128-bit random codes (22 base62 chars) or a keyed Feistel permutation of a large counter; store with TTL = 24 h (store-level TTL) and also check `expires_at` on read; no reuse; rate-limit 404s; avoid logging full codes; consider capability URLs delivered once. Because TTL is 24 h, the dataset is tiny (100 M × 500 B × 1 day = 50 GB), which makes a single replicated in-memory/KV cluster adequate.

**3.** 200 M × 840 B = **168 GB** for exact sliding logs (plus overhead). Use a sliding-window counter (24 B → 4.8 GB) or a token bucket (16 B → 3.2 GB) and accept ~few-% error, or hybrid: exact only for the premium/abuse-sensitive keys.

**4.** 5 TB / 10,000 parts = 500 MB minimum part size (≥ 500 MB per part, e.g., 512 MiB), which makes resume granularity coarse (a failure wastes up to 500 MB); use higher-level resumable protocols with sub-part retries or reconnect-friendly transports.

**5.** 30 M / 200 K = **150 gateways**; memory 200 K × 20 KB = 4 GB per gateway for connection state (plus buffers); with N+1 across 3 AZs ×1.5 → 225 gateways; reconnect storm: losing one gateway forces 200 K reconnects.

**6.** 100 MB pastes go through direct multipart upload (Problem 4), not through the metadata API; streaming reads with Range support; stricter quotas and rate limits; async scanning; likely a separate size-tiered storage class and expiry default shorter; CDN bandwidth cost rises.

**7.** Weighted LRU: weight = key + value + overhead; evict until the sum ≤ 1 GB; reject or special-case values over some threshold (e.g., > 5% of capacity); a 10 MB insert may evict ~100,000 small entries (policy issue) → size-aware policy (GDSF) or segregate large objects in a separate pool; track hit ratio by size class.

**8.** The sequencer can't serialize thousands of msgs/s from a huge channel economically, per-recipient delivery is 500 K writes per message, presence/push fan-out explode. Change: channel/broadcast model with fan-out on read (clients pull the latest N), hierarchical fan-out, push notification only for mentions, rate-limited posting, sampled presence, and a different storage layout (time-bucketed partition per channel).

**9.** Fraction colliding ≈ n/N = 5,000,000 / 14,776,336 = **33.8%** (lab: 33.865%). Expected retries per insert = (n/N)/(1 − n/N) = 0.338/0.662 = **0.51** (geometric).

**10.** Bucket starts full (capacity 20 since burst = 20): the instant burst of 50 passes 20 (refill rate 100/min = 1.67/s adds <1 in the burst). In the next 6 seconds it refills 6 × 1.67 = 10 tokens → about **10 more** pass. Total ≈ 30 of 56.

**11.** 60 requests at t = 59.5 s and 60 requests at t = 60.5 s: each window sees 60, but 120 pass within 1 second.

**12.** 2 GB / 8 MB = 250 parts. Ideal time = 2e9 / 0.625e6 B/s = 3,200 s ≈ **53.3 min**. A part takes 8e6 / 0.625e6 = 12.8 s; P(part fails) ≈ 12.8 / 180 = 7.1% → expected resends 7.1% × 250 ≈ 18 parts ≈ 142 MB wasted (**~7%**, roughly 1.07× bytes) plus reconnect delays.

**13.** Raw: 2 B × 120 B × 365 = **87.6 TB/year**. Replicated: ×3 = 262.8 TB; +40% overhead = **368 TB/year**.

**14.** Average latency: before = 0.407×1 + 0.593×10 = **6.34 ms**; after = 0.560×1 + 0.440×10 = **4.96 ms** (−22%). DB reads at 100 K req/s: before 59.3 K/s; after 44.0 K/s (−26%).

**15.** (a) Cache invalidated/cleared by deploy (cold start): check eviction/restart metrics; (b) key format/version changed (all keys miss); (c) TTL reduced by config; (d) memory limit reduced causing evictions; (e) new traffic pattern/scan. Verify with hit/miss/eviction counters, key samples, config diff; mitigate with warm-up, rollback, single-flight.

**16.** Limit per node not shared (limit/N with imbalance), window misalignment, counting retries/preflight/duplicate requests, clock skew, wrong key (shared NAT IP), burst consumption by background calls, sliding log counting rejected requests.

**17.** Sequencing happens at multiple writers (hot group spread across shards), or fallback to timestamps, or fan-out through different gateways with no client-side reordering; a lease lost causing two sequencers; seq buckets and pagination boundaries.

**18.** Final part size/limits (part count), missing `complete` call retries, checksum mismatch on the last part, request timeout on assembly of large objects, quota check failing at completion, lifecycle rule aborting long uploads (expiry shorter than upload time), scanner timeouts.

**19.** Entries not removed from the map on eviction (leak), separate maps (TTL map) never cleaned, weighting not enforced, listener references retaining values, keys with large retained strings, unbounded negative-cache entries, thread-local caches.

**20.** Inlining 10 KB-10 MB pastes in Postgres bloats the DB (backups, replication, buffer pool), makes expiry deletes expensive (vacuum), and Redis in front doubles memory for large values. Better: metadata in DB, content in object storage + CDN, optional inline for tiny pastes.

**21.** Wall-clock timestamps from different servers/devices are skewed and collide; they can't provide a reliable order (Chapter 14: LWW picks the wrong message up to 48.8% at ±200 ms skew). Use per-conversation sequence numbers from a single writer.

**22.** A DB call per check adds latency and makes the database a bottleneck/SPOF at 1 M checks/s. Use in-memory counters (Redis/local) with batched/leased sync, fail-open policy, and cached rules.

**23 (sketch).** Range-allocated counters (blocks of 10,000 per app server) → keyed permutation → base62, 7 chars (3.5 T codes; 5.2% occupied after 5 years of 100 M/day); custom aliases via conditional put; collision math shows random codes work but cost rises with occupancy; no central bottleneck.

**24 (sketch).** Client sends over WebSocket with `client_msg_id`; gateway forwards to the chat-service shard owning the conversation; shard dedupes, assigns seq, writes durably, acks the sender; looks up members' gateways in the registry, publishes deliver events; gateways push over sockets; offline members get push notifications; devices ack; on reconnect they sync after their last seq.

**25 (sketch).** Stripe the cache into N segments with independent locks (or use CLOCK/sampling), record accesses in a buffer and apply recency updates asynchronously (Caffeine), use lock-free hash maps for lookups; accept approximate global LRU; keep eviction decisions per segment.

**26.** A ticket booth that allows 60 tickets per hour resets at the top of every hour. A group buys 60 tickets at 11:59 and another 60 at 12:00: 120 tickets in a minute, although each hour stayed within its limit. A sliding window counts the *last* hour at every moment.

---

# Chapter summary

- **URL shortener:** read-bound (115 K redirects/s average, 347 K peak), 7-character base62 codes (5.2% occupancy after 5 years), range-allocated IDs with a keyed permutation (random codes work; collision rate = n/N), 302 for control vs 301 for caching, layered caches (LRU hit ratio logarithmic in size: 24% at 0.1%, 64% at 10%), async click analytics (2.4-5.6× fewer writes by batching; event log is the real answer), expiry with lazy checks and delayed reuse, abuse pipeline.
- **Pastebin:** small request rates, large objects: metadata in a DB, content in object storage + CDN, correct expiry semantics, atomic burn-after-reading, unguessable IDs for unlisted pastes, abuse control.
- **Rate limiter:** token bucket for burst-friendly average-rate control; fixed windows allow 2× at boundaries (200 in 0.2 s); exact sliding logs reject 2.8% of well-behaved 90/min clients and cost 35-50× memory; local-only distributed limits are unsafe (9.6×) or unfair (5% false rejections); central atomic counters or leased local buckets; fail-open/closed policy.
- **File upload:** bytes dominate, not requests; pre-signed multipart direct uploads; 8 MB chunks with resume made 5 GB uploads succeed 100% at 1.01× bytes while single-request uploads never finished; checksums, scan state machine, quotas, safe downloads.
- **Chat:** connection-bound (10 M sockets, ~100 gateways); per-conversation sequence numbers from a single writer, ack after durable write, client dedupe/reorder (lab: 22 K anomalies → 0), cursor-based offline sync, reconnect-storm control, pub/sub between gateways, push notifications.
- **LRU cache:** hash map + doubly linked list; differential testing (0 mismatches in 200 K ops); concurrency via striping/CLOCK/buffered updates; byte-weighted eviction and TTL; LFU beats LRU when popularity is stable (64.8% vs 57.3%) but collapses after a shift (25.5%); distributed version via consistent hashing.
- **Cross-cutting patterns:** estimate first; immutable content + CDN; async off the hot path; conditional updates; state machines; idempotency; sequence numbers and cursors; lease/batch to reduce coordination.

---

# Flashcards

| Front | Back |
|---|---|
| Code length for 5 years at 100 M/day | 7 base62 chars (5.2% of 62^7); 6 chars run out |
| Collision rate for random codes | ≈ n/N (lab: 6.78% vs 6.77% at 1 M / 14.8 M) |
| Hash truncation | Birthday bound: 13% collision among 1 M 7-char prefixes |
| Range allocation | 10,000-ID blocks → 100 central calls per million IDs |
| Affine scramble | Bijective but not secret; use keyed Feistel for secrecy |
| 301 vs 302 | Cacheable/no control vs full control/analytics |
| Cache hit vs size (Zipf 0.9) | 24% @0.1%, 41% @1%, 64% @10%, 72% @20% |
| Click counting | Async log + rollups; batching gives 2.4×–5.6× |
| Pastebin split | Metadata in DB; content in object store + CDN |
| Burn after read | Atomic compare-and-set on status |
| Fixed window flaw | Boundary burst: 200 in 0.2 s |
| Token bucket | O(1), burst-tolerant, default for APIs |
| Sliding log | Exact; 840 B/key (35-50× memory) |
| Distributed limits | Local-only: 9.6× over or 5% false rejects; use central/leases |
| Chunked upload | 8 MB parts: 5 GB succeeds 100% at 1.01×; single request 0% |
| Part limits | 10,000 parts → min part size = size/10,000 |
| Chat ordering | Per-conversation seq from a single writer; client dedupe/reorder |
| Chat sockets | 10 M concurrent → ~100 gateways |
| Reconnect storm | Jittered backoff + admission limits |
| LRU structure | HashMap + doubly linked list; O(1) |
| LFU vs LRU | LFU wins when stable; collapses after popularity shift |
| Concurrent cache | Striping, CLOCK, buffered recency updates |

# Glossary additions

| Term | Simple Meaning | Technical Meaning | Why It Exists | Example |
|---|---|---|---|---|
| Base62 | Compact ID alphabet | Encoding using 0-9, A-Z, a-z | Short URLs | `aZ3kQ9x` |
| Range allocation | Hand out ID blocks | Servers lease blocks from a central counter | Cut coordination | Block of 10,000 |
| Keyed permutation | Secret scramble | Bijective keyed mapping (Feistel/FPE) | Unguessable IDs | Feistel network |
| Single-flight | One load per key | Coalescing concurrent cache misses | Stampede control | `getOrLoad` |
| Token bucket | Burst-friendly limiter | Tokens refill at rate r up to capacity b | Rate limiting | 100/min, burst 100 |
| Lease (tokens) | Borrowed quota | Local node takes a batch of tokens from a central store | Reduce central calls | 10% of budget |
| Pre-signed multipart upload | Direct chunked upload | Signed per-part URLs to object storage | Large uploads | S3 multipart |
| Quarantine bucket | Untrusted staging area | Storage for unscanned uploads | Safe processing | `uploads-quarantine` |
| Sequence number | Per-stream counter | Monotonic number assigned by a single writer | Order/dedupe | `seq=1042` |
| Cursor sync | Resume point | Client asks for items after last seen seq | Offline sync | `after_seq=9940` |
| Connection registry | Who is connected where | Map user→gateway with TTL | Message routing | Redis key |
| CLOCK | Cheap LRU approximation | Circular buffer with reference bits | Low-contention reads | Page replacement |
| Admission filter | Gatekeeper | Policy deciding whether to cache a new key | Scan resistance | TinyLFU |

---

**Next: Chapter 26: Problem Book II (Intermediate).** News feed, notification system, Instagram-style photo sharing, Dropbox-style file sync, web crawler, and typeahead, with measured labs on fan-out strategies, notification prioritization, content-defined chunking, crawler politeness scheduling, and prefix-search data structures.

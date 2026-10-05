# Chapter 11: CDNs and Object Storage

## Chapter card

| | |
|---|---|
| **Objective** | Understand how large, immutable, widely-read content (images, video, downloads, backups, logs) is stored cheaply and durably, and delivered quickly worldwide: CDN architecture and tiering, cache-key design and invalidation, large-file and streaming delivery, object-store architecture, durability math (replication vs erasure coding), consistency, direct uploads with pre-signed URLs, lifecycle tiers, and cost |
| **Why this chapter exists** | Binary content dominates bytes on the internet. Putting it through application servers and databases is the classic beginner mistake; nearly every real design offloads it to object storage and a CDN. Interviewers expect you to do this by reflex, and to justify it with numbers |
| **Prerequisites** | Chapters 1 (latency, TLS), 2 (APIs, auth), 6, 7, 8 (caching, HTTP caching headers) |
| **Study time** | 9–12 hours including labs |
| **Tier** | `[MK]` CDN and object-store usage; durability and internals `[GK]` |
| **Mastery criteria** | (1) Explain why CDNs reduce latency and origin load, with numbers. (2) Describe tiered caching/origin shield and compute origin offload. (3) Design cache keys that avoid fragmentation. (4) Choose an invalidation strategy (versioned URLs vs purge). (5) Explain object storage vs file vs block storage and when to use each. (6) Compute durability for replication and erasure coding and explain what “eleven nines” does and doesn’t mean. (7) Design direct-to-storage uploads with pre-signed URLs and multipart. (8) Design lifecycle/cost policies. (9) Build a complete image/video pipeline end to end |

---

## 11.1 The Story: ClipShare and the 2 GB Upload

**ClipShare** lets people upload photos and short videos. Version 1 does what Chapter 2 taught: the web app receives uploads and stores them in a `/var/uploads` directory (later a database `BYTEA` column), and serves them back through the same app servers.

What happens as it grows:

1. **Bandwidth melts the app tier.** Each page shows 40 thumbnails (~200 KB each). At 20,000 page views/s that is 20,000 × 40 × 200 KB = **160 GB/s**. The app servers (built to run business logic) spend their capacity pushing bytes.
2. **Uploads hang threads.** A user on a bad connection uploads a 2 GB video over 25 minutes; the request ties up a server thread (and a connection slot at the load balancer) the whole time. Ten such users and the pool is exhausted.
3. **Latency by geography.** Users in Sydney fetch from a Virginia data center: RTT 220 ms; each image needs a TLS handshake plus request; the page takes 6 seconds.
4. **Disk and backup pain.** The images live on one server's disk; a second server doesn't have them (Ch 6 hidden state). The database with blobs reaches 6 TB; restores take a day.
5. **A deleted photo is still visible** for hours because a cache somewhere still holds it. Later, an engineer purges "everything" to fix it and the origin collapses under the refill storm (Ch 8 metastable failure).
6. **The cloud bill** shows data-transfer-out as the single largest line.

### Pause & Predict 11.1
> Sketch the minimal architecture that fixes all six problems. Which component should receive the 2 GB upload? Which component should serve the 160 GB/s? Where should the bytes actually live? Write it, then compare with §11.17.

---

## 11.2 The physics and economics behind it

### 11.2.1 Distance is latency `[MK]`
Light in fiber travels at roughly 200,000 km/s (about two-thirds of *c*). Sydney to Virginia is ~16,000 km, so one-way ≥ 80 ms and **round trip ≥ 160 ms** even with a perfect straight cable; real paths add routing and queueing (typically 200–250 ms). A page that needs TCP + TLS 1.3 + request = ~3 round trips pays ~0.5–0.75 s *before* the first byte, per fresh connection (Ch 1). The only fix for physics is to **move the bytes closer to the user**.

### 11.2.2 Bandwidth is money `[MK]`
Serving content from your origin means paying for outbound bandwidth (cloud “egress” fees are often among the largest costs) and provisioning servers/links to carry peak. Offloading cacheable bytes to a CDN means (a) the origin sends each popular object to the CDN *once per region* instead of once per user, and (b) CDN bandwidth is bulk-priced and usually cheaper than cloud egress.
*Example.* 1 M daily active users × 50 images × 200 KB = 10 TB/day = ~930 Mbit/s average (10×10¹² B × 8 / 86,400 s) and perhaps 3× that at peak (≈2.8 Gbit/s). If the CDN absorbs 95%, origin egress falls to ~0.5 TB/day.

### 11.2.3 Separate the concerns `[MK]`
| Concern | Wrong place | Right place |
|---|---|---|
| Storing big binary data durably and cheaply | App server disk, relational DB | **Object storage** |
| Serving big bytes to many users | App servers | **CDN** (backed by object storage as origin) |
| Business logic, authorization, metadata | — | App servers + database (store only the object *key/URL* and attributes) |
| Receiving large uploads | App servers | **Direct-to-object-store** via signed URL |

---

## 11.3 CDN architecture `[MK]`

### 11.3.1 What a CDN is
A **Content Delivery Network** is a globally distributed fleet of caching reverse proxies (**edge servers**, grouped into **points of presence, POPs**) close to users, plus the routing system that steers each user to a nearby POP.

```text
 User (Sydney) ──▶ edge POP Sydney ──hit──▶ response (5–30 ms)
                         │ miss
                         ▼
                 regional cache / ORIGIN SHIELD (e.g., Singapore)
                         │ miss
                         ▼
                      ORIGIN (object store or app) in us-east
```

### 11.3.2 How users reach a nearby POP
- **DNS-based steering:** the CDN’s authoritative DNS answers with the IPs of a nearby, healthy POP (using resolver location and measured latency/load).
- **Anycast:** all POPs advertise the same IPs via BGP; the internet routes each user to the “closest” one (Ch 7). Many CDNs combine both.

### 11.3.3 What the CDN does for you
| Function | Notes |
|---|---|
| **Caches static content** | Images, JS/CSS, video segments, downloads, fonts |
| **Terminates TLS near the user** | Handshake RTTs shrink from 200 ms to ~10 ms; modern protocols (TLS 1.3, HTTP/2, HTTP/3/QUIC) at the edge even if the origin is old |
| **Accelerates dynamic content** | Even uncacheable API calls benefit: short user→edge hop, then a persistent, warmed, optimized connection edge→origin (no repeated handshakes) |
| **Absorbs load and attacks** | Huge aggregate capacity: DDoS mitigation, rate limiting, bot management, WAF |
| **Edge compute** | Run code at the edge (Cloudflare Workers, Lambda@Edge, Fastly Compute): redirects, A/B tests, auth checks, personalization |
| **Media services** | On-the-fly image resizing/format conversion (WebP/AVIF), video packaging (HLS/DASH) |
| **Observability** | Hit ratios, status codes, geographies |

### 11.3.4 Tiered caching and the origin shield `[MK]`
Without tiering, each of ~100+ POPs independently fetches a missing object from the origin: a newly popular object causes ~one origin request *per POP*. With a **shield** (a designated mid-tier cache in front of the origin), POP misses go to the shield first; only the shield talks to the origin. Benefits: far fewer origin requests, request collapsing for the same object, protection for small origins. Costs: an extra hop on a shield miss; the shield’s region choice matters (near the origin).

**Measured (Lab 1, part 1).** 20 edge POPs (each caching 1% of 200,000 objects), an optional shield caching 10%, Zipf(0.9) traffic, 3 M requests:
```text
edge only                                      edge hit 35.9%   shield n/a                      ORIGIN gets 64.1% of requests
edge + shield                                  edge hit 35.9%   shield hit 39.7% of edge misses  ORIGIN gets 38.6% of requests
edge + shield, 30% of URLs carry unique ?utm=  edge hit 22.3%   shield hit 19.8% of edge misses  ORIGIN gets 62.3% of requests
```
Lessons: (1) small per-POP caches on a flat-ish popularity distribution give modest edge hit ratios (36% here; real CDNs hold far more per POP and see more skewed traffic); (2) **the shield cut origin load by 40%** (64.1% → 38.6%) by pooling the long tail across POPs; (3) **cache-key fragmentation** (below) collapses everything: 30% of requests with unique query strings sent origin load back to 62%.

### 11.3.5 Push vs pull CDNs
| Model | How content arrives | Good for |
|---|---|---|
| **Pull (origin-pull)** | Edge fetches from origin on first miss | Default; dynamic catalogs; simple |
| **Push** | You upload content to the CDN storage ahead of time | Large, known releases (game patches, video libraries); predictable, no first-user penalty |
Hybrid: pre-warm popular objects with a pull of the URL list before a launch.

---

## 11.4 Cache keys, TTLs, and hit ratio `[MK]`

### 11.4.1 The cache key
By default the key is the **scheme + host + path + query string**. The CDN may also include selected **request headers** (via `Vary`), cookies, device type, or country. Every distinct key is a separate cached object.

### 11.4.2 Fragmentation: the silent hit-ratio killer
Anything that makes the key vary without changing the response splits the cache:
- Tracking parameters (`?utm_source=…`, `?fbclid=…`, session IDs in URLs).
- Query-parameter ordering (`?a=1&b=2` vs `?b=2&a=1`).
- `Vary: User-Agent` (thousands of values), `Vary: Cookie`, or cookies forwarded to the cache key.
- Mixed-case hosts/paths, trailing slashes.
**Fixes:** configure the CDN to **ignore or whitelist** query parameters; sort them; strip cookies on static paths; normalize `Accept-Encoding` to {gzip, br, identity}; do not `Vary` on headers you don’t need; serve different representations on different URLs when you can (`/img/w400/photo.jpg`).

### 11.4.3 TTL strategy
| Content | Cache-Control |
|---|---|
| Fingerprinted static assets (`app.3f9a1c.js`) | `public, max-age=31536000, immutable` |
| User-uploaded media (immutable once stored; object key includes content hash or ID) | `public, max-age=31536000` (or a month) |
| Semi-dynamic pages/API (catalog, home) | `s-maxage=30..300, stale-while-revalidate=…` |
| Personalized or sensitive | `private, no-store`; never cache at shared layers |
| HTML shells | Short TTL or `no-cache` + ETag revalidation |
| Errors (404, 5xx) | Very short (5–30 s) negative TTL to protect the origin without trapping failures |

### 11.4.4 Request collapsing (coalescing)
When many users request the same uncached object simultaneously, the edge should send **one** request to the origin and make the others wait (single-flight, Ch 8). Without it, a flash crowd on a fresh object stampedes the origin. Verify it is enabled, and test with a cold object.

### 11.4.5 Origin offload arithmetic `[MK]`
> Origin request rate = total requests × (1 − edge hit ratio) × (1 − shield hit ratio)

Example: 200,000 req/s; edge hit 92%, shield hit 70% of the remaining → 200,000 × 0.08 × 0.30 = **4,800 req/s** at the origin (2.4% of traffic). If a bad deploy drops the edge hit ratio to 80% (new unique query strings), origin load becomes 200,000 × 0.20 × 0.30 = 12,000 req/s: **2.5× overnight**. Monitor CDN hit ratio like a database metric.

---

## 11.5 Invalidation and purging `[MK]`

### 11.5.1 Prefer immutable, versioned URLs
The most robust strategy: **never change the bytes at a URL.** Put a version or content hash in the URL (`/img/9c1f…e3.jpg`, `/static/app.3f9a1c.js`, `/v/17/catalog.json`). To “change” content you publish a *new URL* and update the reference (in HTML/DB). Old URLs expire naturally; no purge, no staleness, maximum cache lifetime.

### 11.5.2 Purge APIs
When you must invalidate (a mistaken upload, takedown, mutable URLs):
- **Purge by URL, prefix, or tag (“surrogate key”).** Tag responses with `Surrogate-Key: product-42 category-7`; purge by tag to invalidate every page that mentions product 42.
- **Soft purge:** mark as stale, serve stale while revalidating (avoids refill stampede).
- Propagation takes **seconds to a minute** across POPs; it is *eventually consistent*. Design assuming some users see old content briefly.
- **Rate limits and cost:** purging “everything” empties the cache; the refill storm hits the origin (Ch 8 metastable failure; Story problem 5). Prefer targeted purges, soft purges, and capacity to survive a cold CDN.

### 11.5.3 Legal/safety takedowns
For content that must disappear *now* (abuse, legal): (1) delete from origin/object store; (2) purge across the CDN; (3) block at the edge (WAF/edge function denylist keyed by object ID) so even stale copies are not served; (4) short TTLs on sensitive paths. Keep an audit trail.

---

## 11.6 Large files, media, and private content `[MK]`

### 11.6.1 Range requests
HTTP `Range: bytes=1000000-1999999` lets a client fetch part of an object (resume downloads, seek in video, read a Parquet footer). CDNs and object stores support ranges; ensure the CDN caches ranges efficiently (some fetch/cache the whole object, some cache by range/slice: slice caching avoids pulling a 4 GB file to serve 1 MB).

### 11.6.2 Segmented video delivery
Don’t serve one giant MP4. **Transcode** each video into several bitrates/resolutions (a “ladder”: e.g., 240p–1080p) and cut it into short **segments** (2–10 s) described by a manifest (HLS `.m3u8`, DASH `.mpd`). The player requests segments over plain HTTP, switching bitrate as bandwidth changes (**adaptive bitrate streaming**). Every segment is a small immutable file, perfect for CDN caching; popular videos live on the edge; seek = fetch a different segment.

### 11.6.3 Private and paid content: signed URLs and cookies
Public CDN URLs are guessable and shareable. For private content use **time-limited, tamper-proof signed URLs** (or signed cookies for many files): the URL embeds an expiry and an HMAC/RSA signature over (path, expiry, optional IP/user). The edge/object store verifies the signature without calling your app (Lab 1, part 3). Use short lifetimes (minutes), include the method and path in the signature, and avoid caching signed URLs under a cache key that includes the signature (otherwise every user has a separate cache entry; configure the CDN to ignore signature query params for the cache key *after* validating them).
Other protections: **hotlink protection** (Referer checks, signed tokens), **geo-restrictions**, **DRM** for premium video, **origin access control** (only the CDN may read the bucket).

### 11.6.4 Image optimization
Serve the right size and format: responsive variants (`w=320/640/1280`), modern formats (WebP/AVIF), quality tuning. Options: precompute variants at upload time (fast serving, more storage), or **resize on demand at the edge** and cache the result (flexible, first-request cost, must bound allowed sizes to prevent abuse and cache explosion).

---

## 11.7 Operating a CDN-fronted system `[MK]`

- **Protect the origin:** accept traffic only from the CDN (IP allowlists, secret header, mTLS, private link/origin access identity); otherwise attackers bypass the CDN and hit the origin directly.
- **Origin sizing:** design the origin for *miss* traffic plus a cold-cache event (shield/hit-ratio drops), not for total traffic.
- **Multi-CDN:** use two vendors for resilience, performance, and negotiating leverage; steer by DNS/real-user measurements; complexity in config parity, purge across vendors, and cache-key consistency.
- **Failover:** if the CDN is down, DNS-level switch to origin or an alternate CDN (rarely instant; capacity at the origin to absorb the full load is usually not there, so plan for degraded mode).
- **Observability:** hit ratio by content class, origin request rate, 4xx/5xx by POP, TTFB percentiles by region (use RUM, real-user monitoring), cache status headers (`X-Cache: HIT/MISS`).
- **Security pitfalls:** *web cache deception/poisoning* (an attacker causes a private page to be cached or a malicious variant to be stored by exploiting unkeyed inputs). Defences: never cache responses with `Set-Cookie` or `Authorization` unless intentionally, set `Cache-Control: private` on personalized pages, include every input that changes the response in the key, and restrict caching to known static paths/extensions.

---

## 11.8 Object storage fundamentals `[MK]`

### 11.8.1 What it is
Storage that holds **objects** (a blob of bytes + metadata) in **buckets**, each identified by a **key** in a **flat namespace** (`s3://clipshare-media/photos/2026/10/abc123.jpg`; the “/” is just part of the name, though tools show a folder illusion). Accessed through an **HTTP API** (PUT, GET, DELETE, HEAD, LIST, multipart), not a mounted file system. Objects are **immutable**: you replace a whole object, you don’t edit bytes in place. Examples: Amazon S3, Google Cloud Storage, Azure Blob Storage, MinIO, Ceph RGW, Cloudflare R2.

### 11.8.2 Object vs file vs block storage
| | Block (EBS, SAN) | File (NFS, EFS) | Object (S3, GCS) |
|---|---|---|---|
| Interface | Raw disk blocks | POSIX files/directories | HTTP PUT/GET by key |
| Mutability | In-place writes | In-place writes, append | Whole-object replace (immutable) |
| Latency | Lowest (sub-ms to ms) | Low (ms) | Higher (tens of ms first byte) |
| Scale | Per volume (TBs) | Large but bounded | **Virtually unlimited** (exabytes) |
| Cost/GB | Highest | Medium | **Lowest** |
| Sharing | Usually one host | Many hosts | Global via HTTP |
| Typical use | Databases, boot disks | Shared app files, home dirs | Media, backups, logs, data lakes, static sites, artifacts |
**Rule:** databases need block (or local NVMe); shared POSIX workloads need file; everything large, immutable, and widely shared goes to object.

### 11.8.3 Properties and limits (check your provider’s current docs) `[Public]`
- Very high durability (“11 nines”, §11.10) and availability (~99.9–99.99%).
- Object size up to terabytes (S3: 5 TB max; a single PUT up to 5 GB; larger via **multipart** up to 10,000 parts).
- Per-request overhead and cost: many small objects are inefficient; batch/aggregate.
- Rich features: **versioning**, **lifecycle rules**, **server-side encryption**, **object lock (WORM)**, **replication** (cross-region/account), **event notifications**, **access policies**, **storage classes**.
- **Consistency:** modern S3 provides strong read-after-write for new objects, overwrites, deletes, and LIST `[Public: since Dec 2020]`; other stores differ, and cross-region replication is asynchronous. Never assume; read the docs for your provider.

### 11.8.4 What belongs in the database vs the object store
The database stores **metadata** you query on (owner, caption, timestamps, object key, size, content type, checksum, status). The object store stores the **bytes**. The database row points to the object key. Don’t use `LIST` on a bucket to answer application queries (slow, expensive, not queryable); keep your own index.

---

## 11.9 How an object store works inside `[GK]`

Understanding the architecture explains its guarantees and limits.
```text
 Client ──HTTPS──▶ Front-end / API gateway fleet (auth, routing, throttling)
                       │                 │
                       ▼                 ▼
               Metadata service      Storage nodes (data plane)
        (key → {object id, version,     disks/servers across racks & AZs
         size, checksum, placement})    hold chunks (replicas or erasure-coded fragments)
                       ▲                 ▲
                       └── background services: repair, scrubbing, GC, rebalancing, lifecycle, replication
```
1. **Front end** authenticates, authorizes, throttles, and routes requests.
2. **Metadata service** maps `(bucket, key)` → object ID/version and placement. It is a huge, **partitioned** (Ch 10), strongly consistent, replicated key-value store; listing is a sorted range scan over it. (This partitioned, ordered key space is why key naming and prefix distribution historically affected throughput limits; modern services scale partitions automatically, but extremely hot prefixes can still throttle.)
3. **Data plane** stores the bytes as immutable chunks in append-friendly formats on many disks/hosts across racks/AZs.
4. **Writes:** the object is split, redundancy (replicas or erasure-coded fragments) is written to multiple failure domains; the PUT is acknowledged only after durability is achieved and metadata is committed.
5. **Reads:** metadata lookup → fetch enough fragments/replicas (any k of n for erasure coding) → reassemble → verify checksum → stream.
6. **Background:** *scrubbing* (periodically reads data and verifies checksums to catch bit rot), *repair* (re-creating lost fragments after disk/node failures), *rebalancing*, *garbage collection* of deleted/overwritten data, *lifecycle transitions*.
7. **Immutability** simplifies everything: no in-place update coordination, easy caching, easy replication, simple versioning.

---

## 11.10 Durability: replication vs erasure coding `[GK]`

### 11.10.1 Durability vs availability
- **Durability:** probability the data is *not lost* (e.g., 99.999999999% per object per year, “11 nines”).
- **Availability:** probability you can *access* it right now (e.g., 99.99%).
You can have data that is safe but temporarily unreachable (a region outage). Don’t conflate them.
**What “11 nines” means:** annual loss probability of 10⁻¹¹ per object. Store 10 million objects → expected loss 10⁻⁴ objects/year → about one object lost every 10,000 years. It does *not* protect against **your own** mistakes (deleting data, bad lifecycle rules, credentials abuse), which is why versioning, object lock, and backups exist.

### 11.10.2 Replication
Keep *r* full copies on different failure domains (disks, racks, AZs). Storage overhead = *r*× (3× is common). Survives *r*−1 failures. Simple, fast reads and fast repair (copy one replica).

### 11.10.3 Erasure coding `[GK]`
Split an object into *k* data fragments and compute *m* parity fragments (Reed–Solomon). Store all *n = k + m* on different failure domains. **Any *k* of the *n* fragments reconstruct the object.** Overhead = *n/k*; tolerates *m* simultaneous losses.
```text
 object ──split──▶ D1 D2 D3 … D10  ──encode──▶ + P1 P2 P3 P4     = 14 fragments on 14 different nodes
                  lose ANY 4 of 14 → still recoverable from the other 10
```
| Scheme | Overhead | Failures tolerated | Notes |
|---|---|---|---|
| 3× replication | 3.0× | 2 | Fast, simple; expensive at exabyte scale |
| RS(10,4) | 1.4× | 4 | Cheap, highly durable; reads touch k nodes; **repair reads k fragments to rebuild 1** |
| RS(6,3) | 1.5× | 3 | Common compromise |
| LRC (local reconstruction codes) | ~1.3–1.5× | multiple | Add local parities so common single-fragment repair reads fewer nodes (Azure) `[Public]` |
Costs of erasure coding: CPU for encode/decode; small objects are inefficient (fragments too tiny; often stored replicated or packed); **repair traffic** (rebuilding one lost fragment reads *k* others: 10× amplification for RS(10,4)); higher read latency when fragments are fetched from several nodes (mitigated by reading k+δ and taking the first k). Many systems keep hot/new/small data replicated and erasure-code colder data.

### 11.10.4 Measured durability math (Lab 1, part 2)
Assume disks fail independently at 2%/year; an object is lost only if more fragments fail than the scheme tolerates *within one repair window*. Probability one disk fails during a window of *w* hours: p = 0.02 × w/8760.
```text
scheme                      overhead  repair window   P(lose a given object per year)   objects lost per year if you store 10 billion
2x replication               2.0x      24 hours     1.10e-06                          1.10e+04
2x replication               2.0x     168 hours     7.67e-06                          7.67e+04
3x replication               3.0x      24 hours     6.00e-11                          6.00e-01
3x replication               3.0x     168 hours     2.94e-09                          2.94e+01
erasure code RS(10,4)        1.4x      24 hours     3.61e-16                          3.61e-06
erasure code RS(10,4)        1.4x     168 hours     8.64e-13                          8.64e-03
erasure code RS(6,3)         1.5x      24 hours     4.14e-13                          4.14e-03
erasure code RS(6,3)         1.5x     168 hours     1.42e-10                          1.42e+00
```
**Read this carefully.** (1) Two copies lose ~11,000 of 10 billion objects per year even with quick repair: unacceptable. (2) 3× replication reaches “10 nines” territory with fast repair (6×10⁻¹¹) but degrades **50×** if repair takes a week instead of a day (2.9×10⁻⁹). **Repair time is the hidden variable in durability.** (3) RS(10,4) at **1.4× overhead beats 3× replication by 5 orders of magnitude** with less than half the storage. (4) Caveat: this model assumes **independent** failures. Real losses come from *correlated* failures (a rack, a firmware bug on one disk model, a bad batch, an operator error, a software bug that corrupts), so providers spread fragments across racks and AZs, mix disk vintages, checksum everything, and scrub.

### 11.10.5 Other durability practices
- **End-to-end checksums** on write and on every read; background **scrubbing**.
- **Placement across failure domains** (disk < host < rack < AZ < region).
- **Fast automated repair** (parallel rebuild from many sources to shorten the window).
- **Cross-region replication** (async) for region-level disasters.
- **Versioning + object lock + MFA delete** against human error and ransomware.
- Defense in depth: your own backups for critical data (different account/region).

---

## 11.11 Uploads `[MK]`

### 11.11.1 Never proxy large uploads through the application tier
App servers should not hold a connection for 25 minutes to relay bytes. Instead: **direct-to-storage upload with a pre-signed URL**.
```text
 1. Client ──"I want to upload photo.jpg (3.2 MB, image/jpeg)"──▶ App API (authenticated)
 2. App: validates (size/type/quota), creates DB row (status=PENDING), generates a pre-signed PUT URL (expires in ~5 min,
    bound to key, method, content-type, max size) ─────────────────────────────▶ returns URL
 3. Client ──PUT bytes directly──▶ Object store (verifies signature itself; app not in the data path)
 4. Object store ──"ObjectCreated" event──▶ queue/function ──▶ App marks row READY, triggers processing
 5. (Optional) Client calls "complete" API; app verifies (HEAD object, checksum) before marking READY
```
**Why this wins:** the app tier handles only tiny JSON; bandwidth and connection hold-time move to the storage service, which is built for them; retries and resumption are handled by the storage protocol; you scale uploads independently.

**Measured (Lab 1, part 3):** the signature binds method, path, and expiry; tampering or replay with a different method fails:
```text
signed URL      : /uploads/user42/photo.jpg?expires=1000300&sig=9df4cd3f…c617587e
PUT, in time    : 200 OK
PUT, after 5min : 403 expired
GET with PUT sig: 403 signature mismatch
tampered path   : 403 signature mismatch
tampered expiry : 403 signature mismatch
```

### 11.11.2 Multipart and resumable uploads `[MK]`
For large files (hundreds of MB to TB): split into parts (e.g., 8–100 MB; S3 requires ≥5 MB except the last part, max 10,000 parts `[Public]`), upload parts **in parallel** and **retry only failed parts**, then call “complete” to assemble. Benefits: throughput (parallel connections), resilience (resume after a dropped connection), memory efficiency. Abandoned multipart uploads **still consume storage**: set a lifecycle rule to abort incomplete uploads after N days. Alternatives: the **tus** resumable-upload protocol; Google’s resumable sessions.

### 11.11.3 Validation, security, and processing
- **Validate** size, type (by magic bytes, not extension), and count; enforce quotas; reject executables.
- **Scan** for malware; **strip EXIF** (location privacy); **moderate** content asynchronously.
- Store uploads in a **quarantine** bucket/prefix until processed; serve only from a separate “public/processed” location.
- **Checksums** (Content-MD5 / SHA-256) to detect corruption; **content-addressed keys** (`sha256/ab/cd/abcd…`) give free deduplication and immutability.
- **Idempotency:** a retried “create upload” request should return the same upload/key (idempotency key, Ch 2).
- **Never trust client-provided keys/paths** (path traversal, overwriting others’ objects); the server picks the key.

### 11.11.4 Post-upload processing pipeline
Object-created event → queue (Ch 12) → workers (resize, transcode, extract metadata, virus scan, OCR) → write derived objects (thumbnails, renditions) back to storage → update DB status → invalidate/notify. Make workers idempotent (at-least-once events), bounded (timeouts), and observable (DLQ for poison files).

---

## 11.12 Lifecycle, storage classes, and cost `[MK]`

Object stores charge for (1) **storage** (per GB-month, varies by class), (2) **requests** (per 1,000 PUT/GET/LIST), (3) **data transfer out** (egress), and (4) extras (replication, retrieval from cold tiers, minimum durations/sizes in colder tiers).

| Class (generic names) | Access pattern | Cost shape |
|---|---|---|
| **Hot / standard** | Frequent access | Higher storage, no retrieval fee |
| **Infrequent access / cool** | Rare access (monthly) | Lower storage, retrieval fee, minimum duration |
| **Archive / cold** | Almost never (compliance, backups) | Lowest storage; retrieval takes minutes–hours and costs money |
| **Intelligent tiering** | Unknown/changing | Automatic movement for a monitoring fee |

**Lifecycle rules:** automatically transition objects between classes by age, expire (delete) after N days, delete old versions, abort incomplete multipart uploads.

**Cost levers:** (a) put a CDN in front (cheaper egress, fewer origin GETs); (b) tier cold data; (c) compress; (d) aggregate small files (tar/Parquet; per-request and per-object overheads); (e) delete derived variants you can regenerate; (f) same-region compute to avoid cross-region transfer fees; (g) watch versioning (every overwrite keeps a billed old version); (h) set budgets and alerts.

**Worked example.** ClipShare receives 1 M photos/day averaging 3 MB originals plus two derived sizes (≈0.5 MB total) → 3.5 TB/day ≈ **1.28 PB/year**. Storing everything hot forever grows the bill linearly; tiering originals older than 90 days to a cool class and archiving after a year, while keeping small derived images hot (served via CDN), cuts storage cost materially. The CDN hit ratio determines origin GET and egress cost for the hot variants.

---

## 11.13 Consistency, listing, and concurrency `[MK/GK]`

- **Strong read-after-write** (S3 today) simplifies upload→read flows; older systems and many S3-compatible stores had eventual consistency (read old versions, missing keys after write). Check your store.
- **Overwrites:** the last successful PUT wins; concurrent uploaders to the same key conflict silently. Use unique keys per upload (UUID/ULID/content hash) or **conditional writes** (`If-None-Match: *`, `If-Match: etag`) where supported, or versioning.
- **LIST** is paginated (≤1,000 keys per call), slower than GET, and costs money; don’t use it for hot-path logic.
- **Delete** is a metadata operation plus background reclamation; with versioning, delete adds a *delete marker*.
- **Cross-region replication** is asynchronous (seconds to minutes; replication-time SLAs exist): reads in the second region can lag.
- **Event notifications** are at-least-once and can arrive out of order or be delayed: consumers must be idempotent and verify state with HEAD/GET.
- **Rename/move** is copy+delete (not atomic, cost proportional to size) — design keys so you never need to rename.

---

## 11.14 Worked designs

### 11.14.1 Photo-sharing pipeline (the answer to the Story)
```text
 Client ─1▶ API (auth, quota) ─2▶ pre-signed PUT URL
 Client ─3▶ Object store: s3://uploads-quarantine/u42/abc123 (direct)
 Object store ─4▶ event ─▶ queue ─▶ workers: scan, EXIF-strip, thumbnails (320/640/1280), WebP/AVIF
 Workers ─5▶ s3://media-public/photos/abc123/{orig,320,640,1280}.webp  ──▶ DB row READY
 Viewers ─▶ CDN (cache key = path; ignore tracking params; immutable, 1-year TTL) ─▶ [origin shield] ─▶ media-public bucket
 Delete/takedown ─▶ delete objects + purge by tag + edge denylist
```
Sizing: 1 M uploads/day ≈ 12/s average (peak ~50/s): trivial for the API since bytes bypass it; workers: 3 renditions × ~0.4 s CPU = 1.2 CPU-s per photo → ~15 cores at peak with headroom; storage 3.5 TB/day; CDN serves >99% of bytes.

### 11.14.2 Video platform
Upload (multipart direct) → transcoding farm (queue + autoscaled workers; ladder of ~6 renditions; per-title encoding optional) → segments + manifests in object storage → CDN with long TTL for segments and short TTL for manifests (live) → player ABR. Hot titles cached at the edge; the long tail served from regional caches/origin. Storage ≈ original + ~2–3× original across renditions; plan cold-tiering for old/rare videos.

### 11.14.3 Static website / SPA
Build artifacts to a bucket (fingerprinted assets, `immutable`), `index.html` with short TTL, CDN in front, custom domain + TLS, SPA fallback rule, deployment = upload new files then flip `index.html` (atomic from the user’s view); rollback = re-point to the previous HTML.

### 11.14.4 Backups and data lake
Append-only objects (WAL archives, snapshots) with object lock/versioning, lifecycle to archive, cross-account replication; data lake: partitioned prefixes (`dt=2026-10-01/`), columnar formats (Parquet/ORC), compaction jobs to avoid small-file explosions, catalog/metastore for discovery, query engines (Spark/Trino/Athena) reading in parallel with range requests.

---

## 11.15 Failure modes

| Failure | Mechanism | Mitigation |
|---|---|---|
| **Cache fragmentation** | Query params/headers/cookies in cache key | Normalize/whitelist; measure hit ratio per content class |
| **Origin overload on cold cache** | Purge-all, new POP, flash crowd, hit-ratio drop | Request collapsing, shield, soft purge, origin capacity for miss path, pre-warm |
| **Stale/deleted content served** | TTL too long, purge failure, mutable URLs | Versioned URLs, tag purges, edge denylist, short TTL for sensitive paths |
| **Private content leaked** | Missing `private`, cacheable personalized responses, unsigned URLs, public bucket | Signed URLs, `private`/`no-store`, block-public-access, origin access control, audits |
| **CDN outage / POP failure** | Vendor incident | Multi-CDN or fallback to origin with capacity plan |
| **Origin bypass** | Attackers hit origin directly | Restrict origin to CDN (allowlist/mTLS/secret header) |
| **Hot object / hot prefix** | One object or prefix gets extreme request rates | CDN absorbs reads; spread keys; caching; request-rate-aware design |
| **Data loss by human/software error** | Accidental delete, bad lifecycle rule, ransomware | Versioning, object lock, MFA delete, cross-account backups, change reviews |
| **Silent corruption** | Bit rot, bad firmware | End-to-end checksums, scrubbing, verify on read |
| **Upload abuse** | Huge/malicious files, quota bypass | Size/type limits in signed policy, quotas, scanning, rate limits |
| **Orphaned objects** | DB row never created or removed | Reconciliation jobs, lifecycle expiry on quarantine prefix, event-driven cleanup |
| **Cost explosions** | Egress, small-object request volume, forgotten versions/multiparts | Budgets/alerts, lifecycle rules, CDN, aggregation |
| **Replication lag** | Async cross-region | Design reads to tolerate; track replication metrics |
| **Event loss/duplication** | At-least-once notifications | Idempotent consumers, reconciliation by listing/inventory reports |
| **Thundering herd on signed-URL expiry** | Many clients refresh simultaneously | Jittered expiry, longer-lived cookies for sessions |

---

## 11.16 Labs

### Lab 1: CDN tiers, cache-key fragmentation, durability math, and signed URLs (Java 17+)
```java
import java.nio.charset.StandardCharsets;
import java.util.*;
import javax.crypto.Mac;
import javax.crypto.spec.SecretKeySpec;

public class ObjectStorageLab {
    // ---------------------------------------------------------------- 1) CDN: edge + shield + origin
    static class Lru extends LinkedHashMap<Long, Boolean> {
        final int cap; Lru(int cap) { super(16, 0.75f, true); this.cap = cap; }
        @Override protected boolean removeEldestEntry(Map.Entry<Long, Boolean> e) { return size() > cap; }
    }
    static void cdn() {
        int objects = 200_000, pops = 20, requests = 3_000_000;
        double[] cdf = new double[objects]; double sum = 0;
        for (int k = 1; k <= objects; k++) { sum += 1.0 / Math.pow(k, 0.9); cdf[k - 1] = sum; }
        System.out.println("== 1) CDN: 20 edge POPs (each caches 1% of objects) + optional regional shield (10% of objects). Zipf(0.9), 3M requests ==");
        for (String scenario : new String[]{"edge only", "edge + shield", "edge + shield, 30% of URLs carry a unique ?utm= token"}) {
            boolean shield = !scenario.equals("edge only"); boolean fragment = scenario.contains("utm");
            Lru[] edge = new Lru[pops]; for (int i = 0; i < pops; i++) edge[i] = new Lru(objects / 100);
            Lru sh = new Lru(objects / 10);
            Random r = new Random(3); long edgeHits = 0, shieldHits = 0, origin = 0; long uniq = 1_000_000;
            for (int i = 0; i < requests; i++) {
                double u = r.nextDouble() * sum; int lo = 0, hi = objects - 1;
                while (lo < hi) { int mid = (lo + hi) >>> 1; if (cdf[mid] < u) lo = mid + 1; else hi = mid; }
                long key = lo; if (fragment && r.nextDouble() < 0.30) key = uniq++;           // cache key includes a never-repeating query param
                Lru e = edge[r.nextInt(pops)];
                if (e.get(key) != null) { edgeHits++; continue; }
                e.put(key, true);
                if (shield) { if (sh.get(key) != null) { shieldHits++; continue; } sh.put(key, true); }
                origin++;
            }
            System.out.printf("%-58s edge hit %5.1f%%   shield hit %5.1f%% of edge misses   ORIGIN gets %5.1f%% of requests%n", scenario,
                100.0 * edgeHits / requests, shield ? 100.0 * shieldHits / Math.max(1, requests - edgeHits) : 0.0, 100.0 * origin / requests);
        }
    }

    // ---------------------------------------------------------------- 2) durability: replication vs erasure coding
    static double logChoose(int n, int k) { double s = 0; for (int i = 1; i <= k; i++) s += Math.log(n - k + i) - Math.log(i); return s; }
    /** P(at least `need` of n independent disks fail inside one repair window), each failing with probability p */
    static double atLeast(int n, int need, double p) {
        double total = 0;
        for (int j = need; j <= n; j++) total += Math.exp(logChoose(n, j) + j * Math.log(p) + (n - j) * Math.log1p(-p));
        return total;
    }
    static void durability() {
        System.out.println("\n== 2) Durability math: annual disk failure rate 2%. Data is lost only if too many disks holding one object fail before repair finishes ==");
        System.out.println("scheme                      overhead  repair window   P(lose a given object per year)   objects lost per year if you store 10 billion");
        double afr = 0.02;
        record Scheme(String name, int n, int tolerate, double overhead) {}
        for (Scheme s : new Scheme[]{new Scheme("2x replication", 2, 1, 2.0), new Scheme("3x replication", 3, 2, 3.0), new Scheme("erasure code RS(10,4)", 14, 4, 1.4), new Scheme("erasure code RS(6,3)", 9, 3, 1.5)}) {
            for (double hours : new double[]{24, 7 * 24}) {
                double p = afr * hours / 8760.0;                       // probability one given disk fails inside a repair window
                double perWindow = atLeast(s.n(), s.tolerate() + 1, p);
                double perYear = perWindow * (8760.0 / hours);
                System.out.printf("%-26s %5.1fx   %5.0f hours     %.2e                          %.2e%n", s.name(), s.overhead(), hours, perYear, perYear * 1e10);
            }
        }
    }

    // ---------------------------------------------------------------- 3) pre-signed URLs
    static final byte[] SECRET = "server-side-secret-never-sent-to-clients".getBytes(StandardCharsets.UTF_8);
    static String hmac(String data) throws Exception {
        Mac mac = Mac.getInstance("HmacSHA256"); mac.init(new SecretKeySpec(SECRET, "HmacSHA256"));
        return HexFormat.of().formatHex(mac.doFinal(data.getBytes(StandardCharsets.UTF_8)));
    }
    static String sign(String method, String path, long expiresAt) throws Exception {
        return path + "?expires=" + expiresAt + "&sig=" + hmac(method + "\n" + path + "\n" + expiresAt);
    }
    static String verify(String method, String url, long now) throws Exception {
        String path = url.substring(0, url.indexOf('?')); Map<String, String> q = new HashMap<>();
        for (String kv : url.substring(url.indexOf('?') + 1).split("&")) { String[] p = kv.split("=", 2); q.put(p[0], p[1]); }
        long exp = Long.parseLong(q.get("expires"));
        String expected = hmac(method + "\n" + path + "\n" + exp);
        if (!java.security.MessageDigest.isEqual(expected.getBytes(), q.get("sig").getBytes())) return "403 signature mismatch";
        if (now > exp) return "403 expired";
        return "200 OK";
    }
    static void presigned() throws Exception {
        System.out.println("\n== 3) Pre-signed URL: the app signs, the client uploads straight to storage, storage verifies without calling the app ==");
        long now = 1_000_000; String url = sign("PUT", "/uploads/user42/photo.jpg", now + 300);
        System.out.println("signed URL      : " + url);
        System.out.println("PUT, in time    : " + verify("PUT", url, now + 10));
        System.out.println("PUT, after 5min : " + verify("PUT", url, now + 301));
        System.out.println("GET with PUT sig: " + verify("GET", url, now + 10));
        System.out.println("tampered path   : " + verify("PUT", url.replace("user42", "user43"), now + 10));
        System.out.println("tampered expiry : " + verify("PUT", url.replace("expires=" + (now + 300), "expires=" + (now + 999999)), now + 10));
    }

    public static void main(String[] a) throws Exception { cdn(); durability(); presigned(); }
}
```
**Sample output**
```text
== 1) CDN: 20 edge POPs (each caches 1% of objects) + optional regional shield (10% of objects). Zipf(0.9), 3M requests ==
edge only                                                  edge hit  35.9%   shield hit   0.0% of edge misses   ORIGIN gets  64.1% of requests
edge + shield                                              edge hit  35.9%   shield hit  39.7% of edge misses   ORIGIN gets  38.6% of requests
edge + shield, 30% of URLs carry a unique ?utm= token      edge hit  22.3%   shield hit  19.8% of edge misses   ORIGIN gets  62.3% of requests

== 2) Durability math: annual disk failure rate 2%. ... (table shown in §11.10.4)

== 3) Pre-signed URL: ... (output shown in §11.11.1)
```
**Experiments**
1. Increase the edge cache from 1% to 5% of objects. How do edge hit ratio and origin load change? Plot origin load vs edge size for each scenario.
2. Make traffic **geographically skewed**: each POP has its own popular set (shift the Zipf rank by POP). How does the shield change the picture compared with uniform traffic?
3. Add **request collapsing** to the simulation: model 50 concurrent requests for a missing object arriving at one edge in the same 100 ms window. How many origin fetches with and without collapsing?
4. In part 2, add **correlated failure**: with probability 1×10⁻⁴ per year a whole rack (holding up to 2 fragments of any object) fails and repair takes 24 h. Does RS(10,4) still dominate? What if fragments are placed 3 per rack instead of 1?
5. In part 2, model **repair bandwidth**: when one disk dies, how many bytes are read to rebuild its data under 3× replication versus RS(10,4)? (Answer: 1× vs 10× the lost data.)
6. In part 3, add **HMAC over user ID and IP** and show that a link shared with another user/IP is rejected. Then explain why binding to IP can break mobile users on changing networks.

### Lab 2: A real object store with MinIO and pre-signed URLs (needs Docker; not executed in the book’s sandbox)
```bash
docker run -d --name minio -p 9000:9000 -p 9001:9001 -e MINIO_ROOT_USER=admin -e MINIO_ROOT_PASSWORD=adminpass123 \
  quay.io/minio/minio server /data --console-address ":9001"
pip install awscli
export AWS_ACCESS_KEY_ID=admin AWS_SECRET_ACCESS_KEY=adminpass123 AWS_DEFAULT_REGION=us-east-1
aws --endpoint-url http://localhost:9000 s3 mb s3://clipshare
dd if=/dev/urandom of=big.bin bs=1M count=200
aws --endpoint-url http://localhost:9000 s3 cp big.bin s3://clipshare/big.bin        # CLI uses multipart automatically
aws --endpoint-url http://localhost:9000 s3 presign s3://clipshare/big.bin --expires-in 60
curl -sI "<presigned-url>"                                                              # 200 within 60 s, 403 after
curl -s -r 0-99 "<presigned-url>" | wc -c                                               # range request: 100 bytes
aws --endpoint-url http://localhost:9000 s3api put-bucket-versioning --bucket clipshare --versioning-configuration Status=Enabled
```
**Questions:** What happens to storage if you start a multipart upload and never complete it? How would you set a lifecycle rule to clean up? Why does a range request return `206 Partial Content`? What changes in the signed URL when you request a different byte range?

### Lab 3: Cache-key audit (paper/practical)
Take any website and inspect response headers (`curl -sI`): `Cache-Control`, `Age`, `Vary`, `Set-Cookie`, CDN cache-status headers. Which responses are cacheable? What would you change to raise the hit ratio? Identify a response that **should not** be cacheable but might be.

---

## 11.17 Interview perspective and the answer to the Story

**Answer to Pause & Predict 11.1:** uploads go **directly to object storage** via pre-signed URLs (solves problems 2, 4); the bytes live in **object storage** with versioning and lifecycle (4, 6); **the CDN serves reads** with long-lived immutable URLs, origin shield, and request collapsing (1, 3, 6); the database holds only metadata and keys; deletes use targeted purges plus an edge denylist, and an origin sized for the miss path avoids the purge-all collapse (5).

**In interviews**
- Whenever media or large files appear, say: **“bytes go to object storage; clients upload with pre-signed URLs; the CDN serves downloads; the DB stores metadata and keys.”**
- Quantify: file sizes × daily volume = TB/day; CDN offload = (1−hit ratio); egress savings; origin sizing for miss traffic.
- Discuss **cache keys, TTLs, immutable/versioned URLs, invalidation** and what happens on a cold cache.
- For video, mention **transcoding ladder + HLS/DASH + ABR**.
- For durability, know **replication vs erasure coding** and the difference between **durability and availability**.
- Mention **lifecycle tiers**, **multipart uploads**, **signed URLs**, **malware scanning**, **quarantine**, and **idempotent processing** of at-least-once events.

---

## 11.18 Common misconceptions

1. **“Store images in the database; it’s simpler.”** It bloats backups, steals buffer-pool memory, and ties bandwidth to the DB.
2. **“A CDN is only for static files.”** It also accelerates dynamic traffic and offloads TLS/DDoS.
3. **“Purging fixes stale content.”** Purges are eventual and risky; versioned URLs avoid the problem.
4. **“Higher TTL always better.”** Not for mutable or sensitive content; combine with versioning.
5. **“11 nines means I can’t lose data.”** It excludes human error, policy mistakes, and correlated disasters.
6. **“Durability = availability.”** Different properties.
7. **“Object storage is just a slow file system.”** Different semantics: flat namespace, immutable objects, HTTP API, huge scale.
8. **“LIST is how I query my objects.”** Keep a metadata index.
9. **“Erasure coding is always better than replication.”** It costs CPU, latency, and repair bandwidth; small/hot data often stay replicated.
10. **“Signed URLs are secure forever.”** Anyone with the URL can use it until expiry; keep them short and scoped.
11. **“Our origin only needs to handle what the CDN misses, so no capacity planning.”** A cold CDN sends everything to the origin.
12. **“Public bucket + CDN is fine.”** Block public access; allow only the CDN.

---

## 11.19 Chapter summary

- **Physics and cost** (distance = latency; bytes = money) justify **CDNs** and **object storage**; keep app servers and databases out of the byte path.
- **CDN:** POPs reached via DNS/anycast; caches, TLS termination, dynamic acceleration, DDoS/WAF, edge compute; **tiered caching with an origin shield** (lab: origin load 64.1% → 38.6%); origin load = (1−edge hit)(1−shield hit).
- **Cache keys** decide hit ratio: normalize/whitelist query strings, avoid `Vary`/cookies on static paths (lab: unique `?utm` tokens pushed origin load back to 62%). **Request collapsing** prevents stampedes.
- **Invalidation:** prefer **immutable versioned URLs**; use targeted/soft purges and tags; never purge-all casually; takedowns need origin delete + purge + edge block.
- **Large/private content:** range requests, segmented streaming (HLS/DASH, ABR), signed URLs/cookies, hotlink and origin protection, image variants.
- **Object storage:** flat keys, immutable objects, HTTP API, near-unlimited scale, cheapest per GB; metadata in the DB; architecture = front end + partitioned metadata + data plane + background repair/scrub.
- **Durability:** replication vs **erasure coding** (RS(10,4): 1.4× overhead beats 3× replication by ~10⁵ in the lab); **repair time** is the hidden variable (24 h → 168 h worsened 3× replication 50×); correlated failures and human error dominate real risk; durability ≠ availability.
- **Uploads:** direct with pre-signed URLs, multipart/resumable, quarantine and async processing with idempotent workers.
- **Cost:** storage classes, lifecycle rules, request and egress charges, CDN in front.

### Mental models
| Concept | Mental model |
|---|---|
| CDN | Neighbourhood warehouses stocked from the factory |
| Origin shield | A regional depot that restocks all neighbourhood warehouses |
| Cache-key fragmentation | Shelving the same book under a different title for each customer |
| Versioned URL | A new ISBN for every new edition instead of recalling old copies |
| Object storage | A vast warehouse with a catalogue number per sealed box; you swap boxes, never edit them |
| Erasure coding | A jigsaw where any 10 of 14 pieces rebuild the picture |
| Pre-signed URL | A time-limited ticket from the box office to enter a specific door |
| Lifecycle tiers | Moving seldom-used boxes to cheaper, slower storage rooms |

---

## 11.20 Exercises

### A. Conceptual
1. Explain why a CDN improves latency even for uncacheable (dynamic) requests.
2. Describe the request path through edge POP → shield → origin and what each hop caches. What problem does the shield solve?
3. List five causes of cache-key fragmentation and how to fix each.
4. Why are versioned/fingerprinted URLs better than purging? When is purging unavoidable?
5. What is request collapsing and what failure does it prevent?
6. How do range requests work and why do they matter for video and large downloads?
7. Explain adaptive bitrate streaming (HLS/DASH). Why are small immutable segments CDN-friendly?
8. Compare signed URLs and signed cookies. When use each?
9. Compare object, block, and file storage. Which for a database data directory, a shared config directory, a photo archive?
10. Describe the internals of an object store: front end, metadata, data plane, background services.
11. Why are objects immutable, and what does that simplify?
12. Define durability and availability. Give an example of high durability with low availability.
13. Explain erasure coding RS(k, m). What are the overhead, tolerance, and repair cost?
14. Why does repair time dominate durability in practice? Give a numerical illustration.
15. What risks fall outside “11 nines”? Name four controls that address them.
16. Why should uploads not pass through your application servers? Describe the pre-signed URL flow.
17. Explain multipart upload and what happens to incomplete uploads.
18. Why is the object key chosen by the server and not the client?
19. List storage classes and lifecycle rules, and give an example policy for user photos.
20. Why shouldn’t you use bucket LIST to answer application queries? What do you use instead?
21. How can concurrent uploads to the same key conflict, and how do you avoid it?
22. How do you take down illegal content quickly and verifiably given a CDN?
23. What is origin access control and why does it matter?
24. What is web cache deception/poisoning, and what are two defences?

### B. Calculations
25. 5 M daily users × 30 images × 150 KB. Compute daily bytes, average and 3× peak bandwidth, and origin bandwidth if the CDN hit ratio is 96%.
26. Edge hit 90%, shield hit 75% of edge misses, 120,000 req/s. Origin request rate? If edge hit falls to 70%, what is the new origin rate and increase?
27. A 4 GB video is downloaded via 100 MB parts: how many parts? Time to upload with 8 parallel connections each at 20 MB/s versus a single connection at 20 MB/s (ignore overhead)? If one part fails 5% of the time, expected re-upload volume?
28. RS(12,4): overhead, tolerated failures, bytes read to repair a single fragment of a 1 GB object (each fragment = object/12)?
29. Using p = AFR × w/8760 with AFR = 4% and w = 48 h, compute the approximate per-year loss probability for 3× replication (use p³ × 8760/w) and compare with RS(10,4) approximated as C(14,5) p⁵ × 8760/w.
30. Storage cost: 1.28 PB/year growth; hot class $23/TB-month, cool $12.5/TB-month, archive $4/TB-month. If you retain 3 years of data and keep the most recent 90 days hot, 1 year cool, the rest archive, what is the steady-state monthly cost versus all-hot? (Assume uniform ingestion.)
31. A signed URL is valid for 300 s. If 20,000 users each fetch 10 signed segments per minute and refresh signatures at 80% of validity, how many signature generations per second does the app perform? How would long-lived signed cookies change that?
32. Thumbnail pipeline: 50 uploads/s peak; each requires 3 renditions at 0.25 CPU-s each plus a 0.1 CPU-s scan. Cores needed at 60% utilization? What if the 99th percentile arrival burst is 4× the peak for 30 seconds? How big is the queue and how long does it take to drain with the provisioned cores?

### C. Design
33. Design video upload, transcoding, and playback for 100 k uploads/day (avg 200 MB), 50 M views/day. Include storage layout, CDN strategy, cost levers.
34. Design the secure delivery of paid course videos: authentication, signed URLs/cookies, DRM options, hotlinking protection, and revocation.
35. Design the image pipeline with on-demand resizing: allowed sizes, cache-key design, abuse protection, and failure handling.
36. Design a backup system on object storage with ransomware resistance and 7-year retention.
37. Design the cache invalidation strategy for a product catalog with images, descriptions, and prices updated frequently, served via CDN.
38. Plan a launch day for a major release (10 GB game patch to 5 M users in 6 hours): pre-warming, shields, capacity, monitoring, and fallback.

### D. Debugging
39. CDN hit ratio fell from 94% to 61% after a marketing campaign; origin CPU doubled. List four hypotheses and verification steps.
40. Users report seeing another user’s account page occasionally. What caching misconfigurations could cause this? How do you fix and prevent?
41. A purge-all during an incident took the origin down for 20 minutes. Why? What should the process be instead?
42. Uploaded videos sometimes show as “processing” forever. Where could events be lost or duplicated? Describe the reconciliation design.
43. Storage bill doubled; no traffic change. List five likely causes.
44. Signed URLs work in Chrome but fail in a mobile app after a clock change. Diagnose.

### E. Interview-style
45. “Design YouTube’s storage and delivery.” (Cover upload, transcoding, storage, CDN, cost.)
46. “Design Dropbox’s file storage.” (Chunking, dedup, sync, object store.)
47. “How would you store and serve profile pictures for 500 M users?”

### F. Teach-back
48. Explain to a friend why 11 nines of durability doesn’t mean you can never lose a file.

---

## 11.21 Full answer key

**1.** The user-to-edge leg is short (a few ms), TLS and TCP handshakes happen near the user, and the edge reuses warm, persistent, optimized connections to the origin, avoiding repeated cross-continent handshakes; plus DDoS filtering and route optimization.

**2.** User → nearest edge POP (caches popular objects). Edge miss → shield (caches the union of many POPs’ misses). Shield miss → origin. The shield reduces origin requests and collapses duplicate misses from many POPs for the same object.

**3.** Tracking query params (ignore/whitelist), parameter order (sort), cookies in key (strip on static), `Vary: User-Agent/Cookie` (avoid or normalize), mixed-case/trailing slash/host variants (normalize), session IDs in URLs (move to cookies/headers).

**4.** Versioned URLs make content immutable: no invalidation latency or refill storms, maximal TTL, instant rollouts and rollbacks. Purge is unavoidable for mutable URLs, takedowns, or mistaken content already published.

**5.** The edge sends a single origin fetch for simultaneous misses on the same object and serves everyone from that response, preventing stampedes on origin during flash crowds or after purges.

**6.** The client requests a byte interval (`Range`), the server responds `206 Partial Content` with `Content-Range`. Enables resume, seeking, parallel chunk download, and reading parts of large files (e.g., Parquet footer) without transferring the whole object.

**7.** Video is encoded at multiple bitrates and cut into short segments listed in a manifest; the player downloads segments over HTTP and switches bitrate based on measured throughput. Segments are small, immutable, and uniformly addressable, so CDNs cache them efficiently and seeks only fetch needed segments.

**8.** Signed URLs: one object, easy to share per resource. Signed cookies: grant access to many objects (e.g., all video segments under a path) without re-signing each URL. Use cookies for streaming/many files, URLs for single downloads/uploads.

**9.** Block: database data directories (low-latency in-place writes). File: shared config/application files needing POSIX semantics. Object: photo archive (cheap, huge, immutable).

**10.** Front-end: auth, routing, throttling. Metadata service: partitioned strongly consistent map from key to object/version/placement and checksum. Data plane: nodes storing immutable chunks/fragments across failure domains. Background: repair, scrubbing, rebalancing, GC, lifecycle, replication.

**11.** Immutability avoids in-place update coordination and partial-write corruption, enables simple replication and caching, makes versioning natural, and lets metadata commit atomically with new data.

**12.** Durability: data won’t be lost; availability: can be accessed now. Example: an object safely stored across AZs in a region experiencing a temporary regional outage: data durable, not available.

**13.** Split into k data fragments, add m parity fragments; any k of n=k+m reconstruct. Overhead n/k; tolerates m losses; repair reads k fragments to rebuild one (k× read amplification).

**14.** Loss requires more failures than tolerated *within the repair window*; longer windows raise the chance of overlapping failures. In the lab 3× replication went from 6×10⁻¹¹ to 2.9×10⁻⁹ per object-year (50×) when repair went from 24 h to 7 days.

**15.** Accidental or malicious deletion, bad lifecycle policy, ransomware/credential compromise, correlated disasters/bugs. Controls: versioning, object lock/WORM, MFA delete, cross-account/region backups, least-privilege IAM, change review, audit logs.

**16.** Holding app threads/connections for large transfers wastes capacity and cost and creates a single bottleneck; direct upload offloads the data path. Flow: authenticated API call → server validates and creates DB row → returns pre-signed URL (bound to key/method/limits/expiry) → client uploads directly → event notifies server → server verifies and processes.

**17.** Split the object into parts, upload in parallel, retry failed parts, complete to assemble. Incomplete uploads consume storage until aborted; add a lifecycle rule to abort incomplete uploads after N days.

**18.** To prevent overwriting others’ objects, path traversal, collisions, and unauthorized namespace use; the server can also encode tenant/user IDs and random components into the key.

**19.** Classes: standard, infrequent access, archive. Example: originals standard for 30–90 days, move to infrequent access, then archive after a year; derived thumbnails remain hot or are regenerated; delete incomplete multipart uploads after 7 days; expire old versions after 30 days.

**20.** LIST is slow, paginated, costly, and unqueryable by attributes; keep metadata in a database (key, owner, size, checksum, status) and query that.

**21.** Last writer wins silently, losing an upload. Avoid by unique per-upload keys (UUID/hash), conditional writes (`If-None-Match`), or versioning.

**22.** Delete from storage, purge from CDN (tags/URLs), add an edge denylist keyed by object ID so stale copies aren’t served, and verify using cache status checks/probes from multiple regions; keep audit logs.

**23.** Only the CDN (via a credential/identity/IP allowlist/private link) may read the bucket/origin; blocks bypass attacks, enforces signed access, hides the origin’s existence.

**24.** An attacker tricks the cache into storing a private or malicious response for a shared key (e.g., `/account/foo.css`) or exploits unkeyed headers. Defences: don’t cache responses with `Set-Cookie`/auth or mark `private/no-store`; include all response-affecting inputs in the key; restrict caching to known static paths; normalize paths.

**25.** Daily bytes = 5×10⁶ × 30 × 150 KB = 22.5 TB/day. Average bandwidth = 22.5×10¹² × 8 / 86,400 ≈ **2.08 Gbit/s**; 3× peak ≈ **6.25 Gbit/s**. Origin (4% miss) = 0.9 TB/day ≈ **83 Mbit/s avg**, 250 Mbit/s at peak (before shield).

**26.** 120,000 × 0.10 × 0.25 = **3,000 req/s**. At edge hit 70%: 120,000 × 0.30 × 0.25 = **9,000 req/s** (3× increase).

**27.** 4 GB / 100 MB = **40 parts**. Parallel: aggregate 160 MB/s → 4,000 MB / 160 = **25 s**; single: 4,000 / 20 = **200 s**. Retries: expected extra = 5% of 4 GB ≈ **200 MB** (plus second-order retries ≈ 0.25%).

**28.** Overhead 16/12 = **1.33×**; tolerates 4 failures; each fragment = 1 GB/12 ≈ 83.3 MB; repairing one fragment reads any 12 fragments = 12 × 83.3 MB = **1 GB** (reads a full object’s worth to rebuild 83 MB: 12× amplification).

**29.** p = 0.04 × 48/8760 = 2.19×10⁻⁴. 3×: p³ = 1.05×10⁻¹¹ × (8760/48 = 182.5) ≈ **1.9×10⁻⁹**. RS(10,4): C(14,5)=2002; p⁵ = 5.0×10⁻¹⁹ → ×2002 = 1.0×10⁻¹⁵ × 182.5 ≈ **1.8×10⁻¹³**: about four orders of magnitude better with less than half the storage overhead.

**30.** Ingestion is 3.5 TB/day, and three years of retention is ≈ 3,830 TB. Hot = last 90 days ≈ 315 TB; cool = days 91–365 ≈ 960 TB; archive = years 2–3 ≈ 2,555 TB. Monthly cost: 315 × $23 = $7.2k; 960 × $12.5 = $12.0k; 2,555 × $4 = $10.2k → **≈ $29.5k/month**. All-hot: 3,830 × $23 ≈ **$88k/month**, so tiering saves about 67%. (Ignores retrieval, request, and minimum-duration fees; prices illustrative.)

**31.** Each user needs a URL every 60/10 = 6 s per segment, but signatures last 300 s and are refreshed at 240 s: if signatures are per-segment: 20,000 × 10/60 ≈ **3,333 signatures/s**. If one signature covers a path prefix/manifest for the session, refresh every 240 s: 20,000/240 ≈ **83/s**. Signed cookies reduce it to per-session refreshes.

**32.** CPU-seconds per upload = 3 × 0.25 + 0.1 = 0.85. At 50/s that is 42.5 cores of work; at 60% target utilization → **≈71 cores**. A 4× burst is 200 uploads/s for 30 s. If the 71 cores run flat out (100%) they can complete 71 / 0.85 ≈ 83.5 uploads/s, so the backlog grows by (200 − 83.5) × 30 ≈ **3,500 jobs**; the oldest job waits ≈ 3,500 / 83.5 ≈ 42 s at the end of the burst. After the burst, arrivals return to 50/s, leaving 33.5/s of spare capacity, so the queue drains in ≈ 3,500 / 33.5 ≈ **105 s**. Autoscaling workers or accepting the delay (it is asynchronous work) are both reasonable; the queue is the shock absorber.

**33 (sketch).** Direct multipart upload (200 MB avg) to a raw bucket; event → transcoding queue; ladder (240p–1080p) with HLS segments + manifests in processed bucket; CDN with long TTL for segments, short for manifests; tier old/rare videos; 50 M views × (say 100 MB per view) = 5 PB/day delivered mostly by CDN (cost lever: CDN contract, edge caching of popular segments); metadata in DB; thumbnails/preview sprites; moderation and ContentID pipeline; DRM if required.

**34 (sketch).** Authenticate the user; check entitlement; issue short-lived signed cookies scoped to the course’s path; HLS with encrypted segments (AES-128/DRM: Widevine/FairPlay) and license server; token refresh; disable hotlinking via referrer/signature; revocation via short TTL + license revocation + edge denylist for compromised tokens; watermarking for leaks.

**35 (sketch).** Whitelist allowed sizes/formats (e.g., widths 320/640/1280); URL path encodes transform (`/img/w640/q80/<id>.webp`); edge function validates and fetches the original, resizes, caches result with immutable TTL; limit per-IP/ per-origin transform rate; cap source image size; fallback to original on failure; cache-key excludes unknown params.

**36 (sketch).** Write backups to a dedicated account/bucket with object lock (compliance mode) and versioning, MFA delete; separate credentials write-only from the source; lifecycle to archive; cross-region replication; periodic restore drills; integrity checks (checksums, manifests); 7-year retention via lock periods and legal hold; monitoring and alerting for deletion attempts.

**37 (sketch).** Images: immutable content-hash URLs (no invalidation). Descriptions: product JSON/HTML cached with short `s-maxage` + `stale-while-revalidate` and tag-based purge on edits. Prices: very short TTL or fetched via API with versioned cache; check price at checkout from source of truth. Event-driven soft purges via CDC.

**38 (sketch).** Pre-position the patch (push to CDN storage or pre-warm shield and top POPs), staged rollout by region/cohort, rate limiting, mirrors/multi-CDN, torrent-style P2P optional, origin provisioned for miss path, monitoring (hit ratio, throughput, errors), fallback download page, ability to throttle new downloads, resume support via ranges.

**39.** Hypotheses: tracking parameters in URLs fragmenting cache keys; new `Vary`/cookies; shorter TTL set by new origin headers; large objects not cached; geographic traffic shift to POPs with cold caches. Verify with cache-status headers, top miss URLs by key, query-string cardinality, TTL/Age values, per-POP hit ratios.

**40.** Personalized responses cached at a shared layer: missing `private/no-store`, cache key lacking the user identifier/cookie, caching `Set-Cookie` responses, path-based cache deception. Fix: mark personalized responses `private`/`no-store`, bypass cache on auth cookies for those paths, restrict cacheable paths, test with two users, audit CDN rules.

**41.** Purge-all emptied every POP; the refill traffic hit the origin sized for 5% miss traffic, causing overload and failed fills (metastable). Instead: targeted/soft purges, staged purges by region, enable shield and request collapsing, keep stale-if-error, and ensure origin capacity for a cold start.

**42.** Event lost (notification failure, consumer crash before ack), duplicated or reordered events, worker crash mid-job, DB update failure. Reconciliation: periodic job lists PENDING rows older than X, HEADs the object, re-enqueues processing idempotently; processing keyed by object version/ETag; DLQ for poison files.

**43.** Versioning accumulating old versions; incomplete multipart uploads; lifecycle rule misconfigured/removed; cross-region replication started; new high-cost class or minimum duration penalties; derived renditions multiplied; logging buckets growing; large number of small objects; data egress/requests from a misbehaving client or bypassed CDN.

**44.** Signature validation depends on timestamps; device clock skew makes the URL appear expired or not yet valid (some systems reject future-dated requests). Fix: use server time for expiries with leeway, avoid depending on device clocks, sync time, or retry with a fresh URL.

**45 (sketch).** Cover resumable multipart uploads, object store for originals, transcoding pipeline, HLS/DASH, CDN tiers, metadata DB, recommendations separate, cost and storage tiering, content moderation, global distribution, replication.

**46 (sketch).** Client-side chunking (4 MB), content-addressed chunks with dedup in object storage; metadata service (file → ordered chunk list) in a sharded DB; sync via change feed (cursor); delta sync; per-user namespaces; conflict copies; encryption; garbage collection of unreferenced chunks.

**47 (sketch).** Store originals in object storage; derive few sizes at upload; serve via CDN with immutable hashed URLs; default avatar; metadata in DB; cache negative lookups; update = new URL; 500 M × ~50 KB ≈ 25 TB hot; cost small relative to video.

**48.** Because “11 nines” refers to hardware-failure odds per object per year; if you (or an attacker) delete it, overwrite it, or misconfigure a lifecycle rule, the system durably and faithfully destroys it. Protect with versioning, locks, and backups.

---

## 11.22 Flashcards

| Front | Back |
|---|---|
| Why CDN | Distance = latency; bytes = money; absorb load |
| Origin shield | Mid-tier cache; lab: origin 64.1% → 38.6% |
| Origin load | total × (1−edge hit) × (1−shield hit) |
| Cache-key fragmentation | Unique params/cookies/Vary split the cache (lab: back to 62% origin) |
| Best invalidation | Immutable versioned/hashed URLs |
| Purge risks | Eventual; purge-all → refill stampede |
| Request collapsing | One origin fetch for many concurrent misses |
| Range request | `Range: bytes=a-b` → 206 Partial Content |
| ABR streaming | Multi-bitrate segments + manifest (HLS/DASH) |
| Signed URL | Expiry + HMAC; verified without calling the app |
| Object storage | Flat keys, immutable objects, HTTP API, huge scale, cheap |
| Where bytes vs metadata | Object store vs database |
| Durability vs availability | Not lost vs reachable now |
| 11 nines | 10⁻¹¹ loss/object-year; excludes human error |
| Erasure coding | k data + m parity; any k rebuild; overhead (k+m)/k |
| RS(10,4) vs 3× | 1.4× vs 3×; ~10⁵ better durability in the lab |
| Repair time | Hidden durability variable (24 h→168 h: 50× worse for 3×) |
| Direct upload | Pre-signed PUT, app not in the data path |
| Multipart | Parallel parts, resume; abort incomplete via lifecycle |
| Lifecycle | Tier by age; expire; clean versions/multiparts |
| LIST | Slow/expensive; keep your own metadata index |
| Origin access control | Only CDN may read the origin/bucket |

## 11.23 Glossary additions

| Term | Simple Meaning | Technical Meaning | Why It Exists | Example |
|---|---|---|---|---|
| CDN | Nearby copies of content | Global network of caching proxies | Latency, offload | Cloudflare, Akamai |
| POP / edge | Local server site | CDN point of presence | Be near users | Sydney POP |
| Origin | Source of truth | Backend serving cache misses | Authoritative content | S3 bucket |
| Origin shield | Middle cache | Tier that collapses POP misses | Protect origin | Shield region |
| Cache key | Identity of a cached copy | URL + selected headers/params | Hit ratio | `/img/a.jpg` |
| TTL | Freshness time | How long an object is reusable | Staleness control | `max-age` |
| Purge | Remove from cache | Invalidate objects across POPs | Takedowns, fixes | Purge by tag |
| Surrogate key | Cache tag | Header tagging responses for group purge | Targeted invalidation | `product-42` |
| Range request | Partial fetch | HTTP byte range | Resume/seek | `Range: bytes=…` |
| HLS / DASH | Segmented video | HTTP adaptive streaming formats | Streaming | `.m3u8` |
| ABR | Auto quality switching | Player selects bitrate by bandwidth | Smooth playback | 720p→480p |
| Signed URL | Ticket link | URL with expiry and signature | Private access | Pre-signed PUT |
| Object storage | Key-addressed blobs | Immutable objects in buckets via HTTP | Cheap, huge storage | S3 |
| Bucket | Container of objects | Top-level namespace | Policy scope | `clipshare-media` |
| Multipart upload | Chunked upload | Parallel parts then assemble | Large/resumable uploads | 8×100 MB |
| Versioning | Keep old versions | Retain prior object versions | Undo | Delete marker |
| Object lock (WORM) | Write once | Retention that prevents deletion | Ransomware/compliance | 7-year lock |
| Erasure coding | Parity-based redundancy | k+m fragments, any k rebuild | Efficient durability | RS(10,4) |
| Scrubbing | Background verify | Periodic checksum verification | Catch bit rot | Weekly scan |
| Storage class | Price/perf tier | Hot/cool/archive | Cost | Archive |
| Lifecycle rule | Auto tier/expire | Policy-driven transitions | Cost control | 90 days → cool |
| Egress | Data out | Outbound transfer charges | Cost driver | $/GB |
| Content addressing | Name by hash | Key derived from content digest | Dedup, integrity | `sha256/ab…` |

---

**Next: Chapter 12: Asynchronous Processing and Queues.** Work queues vs pub/sub, delivery semantics (at-most/at-least/exactly-once and why “exactly-once” is really idempotency), acknowledgements and visibility timeouts, retries and dead-letter queues, backpressure, ordering and partitioned consumers, the outbox pattern, workflows and schedulers, and a lab with measured duplicate-delivery, backpressure, and ordering behavior.

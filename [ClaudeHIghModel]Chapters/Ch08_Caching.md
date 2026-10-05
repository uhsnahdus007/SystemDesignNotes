# Chapter 8: Caching

## Chapter card

| | |
|---|---|
| **Objective** | Master caching end to end: why it works (locality and skew, quantified), where caches live, the read/write patterns, eviction policies, invalidation and the consistency races it creates, the failure modes (stampede, avalanche, penetration, hot keys, cold start, metastable collapse), distributed cache architecture, HTTP caching, and multi-level caches |
| **Why this chapter exists** | Caching is the highest-leverage performance tool in system design and the most common source of subtle correctness bugs. Nearly every large design uses it at many layers; interviewers probe it relentlessly |
| **Prerequisites** | Chapters 1, 3, 5, 6, 7 (latency ladder, DB limits, hit-ratio/queueing impact, stateless tiers, consistent hashing intro) |
| **Study time** | 12–15 hours including labs |
| **Tier** | `[MK]` throughout (TinyLFU/leases/XFetch details `[GK]`) |
| **Mastery criteria** | (1) Compute the effect of hit ratio on latency and backend load; explain why 90%→99% is a 10× load reduction. (2) Choose among cache-aside, read-through, write-through, write-back, write-around, refresh-ahead and justify. (3) Explain LRU/LFU/FIFO/TinyLFU and implement an O(1) LRU. (4) Explain and reproduce the cache-aside stale-populate race and at least two fixes. (5) Diagnose and mitigate stampede, avalanche, penetration, hot keys, cold start. (6) Design a distributed cache (sharding, replication, sizing, failure behavior). (7) Use HTTP caching headers correctly. (8) Say what *not* to cache |

---

## 8.1 The Story: The Product Page That Melted the Database

ShopFront's product page is the busiest page on the site. Each view runs 4 queries (product, price, inventory summary, review aggregates) against the primary database. Traffic: 40,000 page views/s at peak ⇒ **160,000 queries/s**. The database handles ~20,000 queries/s comfortably.

Management says: "Buy a database 8× bigger." Engineering notices something:
- There are only **2 million products** in total.
- The top 5,000 products receive about **60% of all views**.
- A product's details change maybe **a few times a day**, but are read **thousands of times a second** for popular ones.

### Pause & Predict 8.1
> If you could keep the answers for the most popular products *somewhere much faster than the database*, what fraction of the 160,000 queries/s would remain? What new problems appear (think: what if the price changes? what if the fast store dies?). Write your answers, then read on.

---

## 8.2 Why caching works

### 8.2.1 The core idea `[MK]`
A **cache** is a faster, smaller store holding **copies** of data that is expensive to fetch or compute, so repeated requests avoid the expensive path. The original (the **source of truth**) remains elsewhere.

**Mental model:** you keep the books you read most on your desk, not in the library across town.

### 8.2.2 Three reasons it works
1. **Latency gap** (Ch 1): RAM over the network ~0.5–1 ms vs a DB query 5–50 ms vs a cross-region call 100+ ms.
2. **Locality of reference:** *temporal* (recently used items are likely used again soon) and *spatial* (neighbors of used items are likely used).
3. **Skew:** access frequency is wildly uneven. Real popularity distributions are roughly **Zipf/power-law**: a few items get most requests. Caching the head of the distribution captures a large share of traffic.

### 8.2.3 The arithmetic of hit ratio `[MK]`
Let *h* = hit ratio, *c* = cache access time, *d* = backend (miss) access time.
> **Average latency = h·c + (1−h)·(c + d)**   (a miss pays the cache lookup *and* the backend)
> **Backend load = (1−h) × request rate**

Worked example: c = 1 ms, d = 20 ms.
| Hit ratio | Avg latency | Backend load (at 160,000 req/s) |
|---|---|---|
| 0% | 21 ms *(a miss pays the lookup and the backend)* | 160,000/s |
| 50% | 11 ms | 80,000/s |
| 80% | 5 ms | 32,000/s |
| 90% | 3 ms | 16,000/s |
| 95% | 2 ms | 8,000/s |
| 99% | 1.2 ms | 1,600/s |

**Crucial insight:** *backend load falls as 1/(1−h)*. Going from 90% to 99% makes the backend **10× less loaded**, though latency only improves from 3 ms to 1.2 ms. Caches protect backends more dramatically than they speed up users. And it works in reverse: **a cache dropping from 99% to 90% (because of a restart or key change) increases backend load 10×**, a classic outage trigger.

### 8.2.4 How big must the cache be? Measured hit ratios (Lab 1)
Simulation: 100,000 distinct items, popularity Zipf with exponent 1.0, 2 M requests, LRU cache:

| Cache size (% of items) | Hit ratio | Backend load | Avg latency (1 ms / 20 ms) |
|---|---|---|---|
| 0.1% | 29.0% | 71.0% | 15.2 ms |
| 1% | 50.7% | 49.3% | 10.9 ms |
| 5% | 66.5% | 33.5% | 7.7 ms |
| 10% | 73.7% | 26.3% | 6.3 ms |
| 20% | 81.0% | 19.0% | 4.8 ms |
| 50% | 90.6% | 9.4% | 2.9 ms |

**Lessons**
- Hit ratio grows roughly **logarithmically** with cache size: each doubling of memory buys a similar *additive* gain (~7–8 points here). The first 1% of items captures about half the traffic; getting from 90% to 99% could need most of the dataset (or a more skewed workload).
- The popular "80/20 rule" isn't guaranteed: here 20% of items cached gives 81% hits, close to the rule; but real workloads have different skew exponents. **Measure your own key-frequency distribution** (sample access logs) and plot a *miss-ratio curve* (hit ratio vs cache size) before sizing.
- Skewier workloads (s > 1: celebrity profiles, trending items) make small caches very effective; flatter workloads (s < 1: long-tail catalogs, search queries) make caching less effective.
- **Working set:** the amount of distinct data actively used in a time window. If the cache can hold the working set, hit ratios approach the compulsory-miss floor.

### 8.2.5 Miss categories (useful vocabulary)
- **Compulsory (cold) miss:** first access ever.
- **Capacity miss:** evicted because the cache is too small.
- **Conflict miss:** evicted due to placement constraints (CPU caches, set-associativity).
- **Invalidation miss:** removed because data changed.
- **Expiry miss:** TTL elapsed.
Knowing which dominates tells you the fix (bigger cache, longer TTL, better invalidation, pre-warming).

---

## 8.3 Where caches live: a map of layers `[MK]`

```text
Browser cache ─▶ CDN edge ─▶ Gateway/reverse-proxy cache ─▶ App in-process cache (L1)
        ─▶ Distributed cache: Redis/Memcached (L2) ─▶ DB buffer pool ─▶ OS page cache ─▶ Disk
```
| Layer | What it caches | Latency | Typical TTL/invalidation | Notes |
|---|---|---|---|---|
| **Browser / mobile client** | Static assets, API responses (`Cache-Control`) | ~0 ms (no network) | Headers; versioned URLs | Free capacity; can't be purged → use versioned URLs |
| **CDN / edge** | Static + cacheable dynamic responses | 5–50 ms (nearby) | Headers; purge APIs; stale-while-revalidate | Shields origin; ch 11 |
| **Reverse proxy / gateway** | Responses by URL/headers | sub-ms to ms | Headers; short TTL | NGINX/Varnish |
| **In-process (L1)** | Objects/results in app memory | ~100 ns–µs | Short TTL; size-bounded | Fastest; per-instance copies; invalidation is hard |
| **Distributed cache (L2)** | Objects, query results, sessions, counters | ~0.3–1 ms | TTL + explicit invalidation | Shared by all instances; Redis/Memcached |
| **Database buffer pool / OS page cache** | Pages/blocks | µs | LRU-ish | Automatic; size the DB's RAM for the working set |
| **CPU caches** | Cache lines | ns | Hardware | Why data layout matters |
| **Precomputed / materialized views** | Query results stored as tables | DB-speed | Refresh jobs/triggers | A cache in disguise (consistency = refresh policy) |

**Principle:** cache **as close to the consumer as correctness allows**, and **layer** caches so each absorbs what the next would otherwise see. Each layer you add multiplies consistency complexity.

---

## 8.4 Caching patterns: reads and writes `[MK]`

### 8.4.1 Cache-aside (lazy loading), the default
The **application** manages the cache: check cache → on miss load from DB → populate cache.
```java
Product getProduct(long id) {
    String key = "product:v3:" + id;                     // key design: namespace + schema version + id
    Product p = cache.get(key);
    if (p != null) return p;                             // HIT
    p = db.loadProduct(id);                              // MISS: go to source of truth
    if (p != null) cache.set(key, p, ttlWithJitter(300));
    else cache.set(key, NOT_FOUND, 30);                  // negative caching (short TTL)
    return p;
}
void updateProduct(Product p) {
    db.save(p);                                          // 1. write source of truth
    cache.delete("product:v3:" + p.id);                  // 2. invalidate (don't update) the cache
}
```
✔ Only caches what's requested; a cache outage degrades performance but not correctness (app falls back to DB); simple; works with any cache. ✘ First request is slow; the DB can be hit by many misses at once (stampede); **stale-data window** and races (§8.6); the application holds the caching logic.
**Why delete rather than update on write?** Updating the cache with the new value races with other writers (two concurrent updates can leave the older value in the cache); deleting is simpler: the next read repopulates from the source of truth.

### 8.4.2 Read-through
The **cache library/service** loads from the backend on a miss; the application only talks to the cache.
✔ Cleaner app code; centralizes loading logic (and makes single-flight natural). ✘ Cache must know how to load; couples cache and data source. (Examples: Caffeine `LoadingCache`, some CDN/proxy caches, NCache, AWS DAX for DynamoDB.)

### 8.4.3 Write-through
Writes go to the **cache and the database synchronously** (the cache layer writes to the DB, or the app writes both).
✔ Cache is always fresh after writes; read-your-writes easy. ✘ Write latency = DB + cache; caches data that may never be read; dual-write failure modes if the app writes both (crash between them); doesn't solve cold start.

### 8.4.4 Write-back (write-behind)
Writes go to the **cache only**; the cache flushes to the DB **asynchronously/batched**.
✔ Very fast writes; coalesces repeated writes to the same key (great for counters/hot rows); smooths DB load. ✘ **Data loss risk** if the cache node dies before flush; complex ordering/recovery; DB is stale. Use for: view counters, last-seen timestamps, telemetry aggregates, where losing a few seconds is acceptable, *or* when the cache itself is durable and replicated.

### 8.4.5 Write-around
Writes go **directly to the DB**, bypassing the cache; the cache fills on subsequent reads.
✔ Avoids polluting the cache with write-once/rarely-read data (logs, uploads). ✘ A read right after a write misses (and may read stale cache if not invalidated).

### 8.4.6 Refresh-ahead (proactive refresh)
For hot keys, refresh before expiry (asynchronously, on the first access after, say, 80% of TTL elapsed) so users never experience the miss. ✔ Smooth latency; prevents stampedes for known-hot keys. ✘ Wasted refreshes for items that become cold; needs tracking.

### 8.4.7 Choosing a pattern
| Situation | Pattern |
|---|---|
| General read-heavy data (profiles, products) | **Cache-aside** + TTL + invalidate-on-write |
| Cache library with loader support, many concurrent misses | **Read-through** (with single-flight) |
| Strong "read after write" required, write rate modest | **Write-through** |
| Very high write rate to same keys; loss tolerable | **Write-back** (or counters in Redis flushed periodically) |
| Large write-once data | **Write-around** |
| Known hot set, latency-sensitive | **Refresh-ahead** |
| Data that must always be correct (balances for debits) | **Don't cache** the decision read (or use strongly consistent paths) |

---

## 8.5 Eviction policies `[MK]`

A cache has finite memory. When full, which entry do we remove?

### 8.5.1 Policies
| Policy | Rule | Pros | Cons |
|---|---|---|---|
| **LRU** (Least Recently Used) | Evict the entry unused for the longest time | Matches temporal locality; simple; the default | **Scan pollution:** one large scan of cold keys flushes the hot set; ignores frequency |
| **LFU** (Least Frequently Used) | Evict the least often used | Protects consistently popular items | Old popularity lingers; needs aging; counters cost memory |
| **FIFO** | Evict oldest inserted | Trivial | Ignores usage |
| **Random** | Evict a random entry | O(1), no metadata; surprisingly decent | Less optimal |
| **TTL-based** | Evict expired first | Bounds staleness | Not a capacity policy by itself |
| **2Q / ARC / SLRU** | Separate "seen once" from "seen repeatedly" segments | Scan resistance, adaptive | More complex |
| **W-TinyLFU** (Caffeine) | Small LRU "window" + frequency-sketch admission filter + segmented main space | Near-optimal hit rates in practice; compact frequency sketch | Complexity (use the library) |
| **Size-aware (GDSF)** | Considers entry size and fetch cost | Best when sizes vary widely | Complexity |
| **Cost-aware** | Evict items cheap to recompute first | Matches backend economics | Needs cost estimates |

**Redis** implements *approximate* LRU/LFU by sampling (default 5 random keys, evict the best candidate among them) to avoid maintaining exact global ordering; policies: `allkeys-lru`, `volatile-lru`, `allkeys-lfu`, `volatile-ttl`, `allkeys-random`, `noeviction` (errors on full: a *good* setting for data you can't lose, a bad one for caches).

### 8.5.2 Implementing LRU in O(1) `[MK]`
Data structures: a **hash map** (key → node) for O(1) lookup, and a **doubly linked list** ordered by recency (head = most recent, tail = least recent). On access: move node to head. On insert when full: remove the tail node (and its map entry).
```java
import java.util.*;

class LruCache<K, V> {
    private class Node { K key; V val; Node prev, next; Node(K k, V v) { key = k; val = v; } }
    private final int capacity;
    private final Map<K, Node> map = new HashMap<>();
    private final Node head = new Node(null, null), tail = new Node(null, null);   // sentinels

    LruCache(int capacity) { this.capacity = capacity; head.next = tail; tail.prev = head; }

    synchronized V get(K key) {                       // coarse lock; production code uses striping/Caffeine
        Node n = map.get(key);
        if (n == null) return null;
        unlink(n); addFirst(n);                       // mark as most recently used
        return n.val;
    }
    synchronized void put(K key, V val) {
        Node n = map.get(key);
        if (n != null) { n.val = val; unlink(n); addFirst(n); return; }
        if (map.size() == capacity) { Node lru = tail.prev; unlink(lru); map.remove(lru.key); }   // evict least recent
        n = new Node(key, val); map.put(key, n); addFirst(n);
    }
    private void unlink(Node n) { n.prev.next = n.next; n.next.prev = n.prev; }
    private void addFirst(Node n) { n.next = head.next; n.prev = head; head.next.prev = n; head.next = n; }
}
```
(Java's `LinkedHashMap(accessOrder=true)` is exactly this, as used in Lab 1.)

### 8.5.3 Memory accounting
Count the **real** footprint: key + value + per-entry overhead. Redis: ~50–100 bytes of overhead per key plus allocator fragmentation; Java objects: headers, pointers, boxing, serialization buffers. A "1 KB value" cache of 10 M keys needs ~11–13 GB, not 10 GB. Set `maxmemory` below physical RAM (leave room for fork/copy-on-write during persistence and for fragmentation).

### 8.5.4 Scan resistance & admission control
A batch job or crawler reading every product once will evict your hot set under pure LRU. Defenses: TinyLFU/2Q-style **admission** (don't admit one-hit wonders until seen twice), separate caches for batch vs interactive traffic, or having batch jobs bypass the cache.

---

## 8.6 Invalidation and consistency `[MK]`

> "There are only two hard things in Computer Science: cache invalidation and naming things." (Phil Karlton)

### 8.6.1 The problem
The cache holds a copy. When the source changes, copies must be updated or removed; otherwise users see **stale** data. The question is not *whether* staleness exists, but **how long, for whom, and whether it's acceptable**.

### 8.6.2 Strategies
| Strategy | How | Staleness | Cost/risk |
|---|---|---|---|
| **TTL only** | Entries expire after T | Up to T | Simple; the safety net for every other strategy |
| **Explicit invalidation on write** | Writer deletes/updates the key | Small window (races!) | Writers must know all affected keys (derived/aggregate caches!) |
| **Write-through** | Cache updated with the write | Minimal | Write latency; dual-write issues |
| **Versioned keys** | Key includes a version: `product:42:v17`; writers bump the version | Zero for readers who see the new version | Needs a place to store current version (small, hot, maybe cached itself); old versions expire via TTL/eviction |
| **Event-driven invalidation (CDC)** | DB change log → stream → invalidator deletes keys | Seconds; ordered per key | Extra infrastructure; very reliable; no app-level dual write |
| **Stale-while-revalidate** | Serve stale briefly while refreshing in background | Bounded by revalidation time | Great for latency & stampede protection |
| **Pub/sub invalidation (for L1 caches)** | Broadcast "key X changed" to all instances | Short | Lost messages ⇒ rely on TTL |

**Rule:** *always* put a TTL on cache entries, even with explicit invalidation. TTL bounds the damage of every missed invalidation, bug, or lost message.

### 8.6.3 The cache-aside stale-populate race `[MK]`
```text
Time ─────────────────────────────────────────────────────────────────▶
Reader R:  miss ─▶ read DB (v1) ───────── (GC pause / slow net) ───────────▶ cache.set(k, v1)   ✗ stale!
Writer W:                    write DB (v2) ─▶ cache.delete(k) ─────────────▶
```
The reader fetched v1 *before* the write, but populated the cache *after* the writer's invalidation. The cache now holds v1 **indefinitely (until TTL)** while the DB has v2. This race is real and has been the root of countless "why is the old price showing?" incidents. Probability rises with load and slow reads.

**Reproduced deterministically in Lab 2:**
```text
NAIVE_DELETE                 -> cache has STALE version 1 while DB has 2  <-- BUG, persists until TTL
WRITE_THROUGH_NEWER_WINS     -> cache has version 2 (correct)
INVALIDATION_FLOOR           -> cache empty -> next read reloads version 2 (correct)
```
**Fixes (pick based on need):**
1. **Short TTLs:** bound the damage (always do this).
2. **Versioned values with "newer wins" writes:** store `(version, value)`; populate only if your version ≥ the cached one (compare-and-set). Writers write the new version into the cache (write-through) so a late reader can't overwrite it.
3. **Invalidation markers / leases** (Facebook's memcache design `[Public: "Scaling Memcache at Facebook"]`): on a miss the cache hands the reader a **lease token**; a subsequent delete/invalidate *invalidates the outstanding lease*; the reader's `set` with a stale token is rejected. Leases also **rate-limit refills** (only one reader gets the lease → stampede protection).
4. **Delayed double delete:** delete after the write, then delete again after a short delay (> typical read latency). Probabilistic, not guaranteed.
5. **CDC-driven invalidation:** since the invalidation event is emitted *after* the DB commit and ordered, a late reader's stale populate can still occur, so combine with versioning or short TTL.
6. **Serialize per key** (distributed lock around read-populate and write): correct but costly; rarely worth it.

### 8.6.4 Another race: two writers updating the cache directly
Writer A (value 1) and writer B (value 2) update the DB in order A, B but update the cache in order B, A: cache ends with 1 while DB has 2. Same fixes: delete instead of update, or versioned CAS updates.

### 8.6.5 Consistency you can actually offer
- **Bounded staleness:** "values are at most T seconds old" (TTL) is a clear contract; communicate it.
- **Read-your-writes:** after a user's own write, bypass or update the cache for that user (read from the DB, or write-through, or session-affinity to a freshly invalidated path).
- **Strong consistency:** caching is generally incompatible unless the cache is part of a consensus/replicated system or reads validate a version against the source. For money/inventory *decisions*, read the source of truth; use caches for display.
- **Derived caches** (a cached leaderboard or aggregated page) depend on many source rows; invalidating them precisely is hard → use short TTLs or event-driven recomputation.

### 8.6.6 Negative caching
Cache "not found" results briefly (10–60 s) so repeated lookups for missing keys don't hit the DB (§8.7.3). Invalidate on creation or keep TTL short.

---

## 8.7 Failure modes of caching `[MK]`

### 8.7.1 Cache stampede (thundering herd, dogpile)
**Mechanism:** a hot key expires (or is evicted/invalidated); thousands of concurrent requests miss simultaneously and all hit the backend with the same expensive query; the backend slows, which prolongs the miss window, which admits more requests.
**Measured (Lab 1, demo 2):** 300 concurrent requests arrive just after a hot key expired (DB call = 100 ms):
```text
naive cache-aside        -> DB calls: 300
single-flight (coalesce) -> DB calls: 1
```
**Mitigations**
1. **Single-flight / request coalescing:** only one caller loads; others wait on its result (in-process map of in-flight futures; or a distributed lock/lease with timeout for cross-instance coordination).
2. **Probabilistic early expiration (XFetch)** `[GK]`: each reader recomputes early with probability rising as expiry nears: refresh if `now − δ·β·ln(rand()) ≥ expiry` (δ = recompute time, β≈1). Spreads refresh across readers; no locks.
3. **Stale-while-revalidate / serve stale on error:** keep serving the old value (soft TTL < hard TTL) while one request refreshes.
4. **TTL jitter:** randomize expirations (±10–20%) so keys don't expire together.
5. **Refresh-ahead** for known hot keys; **pre-warming** after deploys.
6. **Backend protection:** concurrency limits/circuit breakers on the DB path so a miss storm sheds load instead of collapsing the DB.

### 8.7.2 Cache avalanche (mass expiry or mass loss)
**Mechanism A (synchronized TTL):** many keys cached at the same time with the same TTL expire together, producing a wave of misses.
**Measured (Lab 1, demo 3):** 10,000 keys each read once/second, TTL 60 s, all cached at t=0:
```text
jitter +/- 0%:  peak DB loads in one second = 10000 (at t=60s);  average DB loads/s = 134
jitter +/-20%:  peak DB loads in one second =   443 (at t=48s);  average DB loads/s = 152
```
Same average load; **a 23× difference in peak.** Jitter costs almost nothing and removes the wave.
**Mechanism B (cache node/cluster loss):** a failed cache node's keys all miss at once. Mitigations: consistent hashing (lose 1/N of keys, not all), replicas, multi-level caches, DB admission control, gradual traffic restoration.

### 8.7.3 Cache penetration (queries for keys that don't exist)
Attackers or bugs request nonexistent IDs; every request misses and hits the DB (nothing to cache).
**Mitigations:** negative caching (short TTL); **Bloom filter** of valid keys in front (Ch 20: "definitely not present" ⇒ reject without touching the DB); input validation (ID formats/ranges); rate limiting; per-IP quotas.

### 8.7.4 Hot keys
One key (celebrity profile, viral item, global config) receives enormous traffic, saturating **one** cache node (or one Redis shard's single thread), regardless of cluster size.
**Mitigations:** 
- **Local (L1) cache** with tiny TTL (1–5 s) in each app instance: converts N×rate into N/ttl.
- **Replicate the hot key** under multiple names (`key#1..#k`, random choice on read), invalidate all on write.
- **Detect hot keys** (sampling, `redis-cli --hotkeys`, client-side counters) and promote automatically.
- Request coalescing; CDN for public data.

### 8.7.5 Big keys / large values
A 50 MB value blocks a single-threaded server during serialization/transfer, spikes memory and network, and causes tail latency for neighbors. Split values, compress, store large blobs in object storage and cache metadata, set size limits and alerts.

### 8.7.6 Cold start
New cache (deploy, failover, scale-out) is empty ⇒ miss storm. Mitigate: warm by replaying recent keys / hot-key list, gradual traffic shift, persistent cache snapshot (RDB) or replicas, keep the DB capacity to survive a cold cache (or load-shed until warm).

### 8.7.7 Metastable failure: the cache that can't recover
Sequence: (1) cache loses data or hit rate falls; (2) DB load jumps 10×; (3) DB slows/times out; (4) requests fail *before* populating the cache (and retries add load); (5) the cache stays empty, the DB stays overloaded: the system is stuck in a **bad stable state even after the trigger is gone**. `[Public: "Metastable Failures in Distributed Systems" (Bronson et al.)]`
**Defenses:** DB-side admission control and load shedding; limit retries; serve stale data; prioritize cache refill (even partially-failed requests should populate); cap concurrency per key; ability to *manually* shed traffic and ramp back gradually.

### 8.7.8 Poisoned or inconsistent cache
Caching an error response, a partial/corrupt object, or one tenant's data under another's key (key design bug) can persist for the TTL. Mitigate: never cache 5xx (or cache them very briefly), validate before caching, include tenant/user/locale/permissions in keys where responses vary, version cache schemas.

### 8.7.9 The cache as an accidental hard dependency
If the DB can't survive without the cache, the cache is a tier-0 dependency: its outage is your outage. Make it explicit: size the DB for degraded operation or implement fallbacks (stale local copies, load shedding, graceful degradation).

### 8.7.10 Serialization and versioning issues
Rolling deploys mean old and new code read each other's cached objects. Include a schema version in keys (`product:v3:...`) or in the payload; make deserializers tolerant; avoid Java native serialization.

---

## 8.8 Distributed cache architecture `[MK]`

### 8.8.1 Why a shared cache?
In-process caches (L1) are fastest but duplicate data per instance, can't be invalidated centrally, and shrink as the fleet scales. A **shared distributed cache** (L2) gives one logical cache for all instances, bounded memory independent of fleet size, and central control.

### 8.8.2 Topologies
| Topology | How keys find nodes | Examples | Notes |
|---|---|---|---|
| **Client-side sharding** | The client library hashes the key (consistent hashing) to choose a node | Memcached clients (Ketama) | No proxy hop; every client needs the topology; resharding = client rollout |
| **Proxy-based** | Clients talk to a proxy that shards | twemproxy, mcrouter `[Public: Facebook]`, Envoy Redis proxy | Centralized routing/pooling; extra hop |
| **Server-side cluster** | Nodes own **hash slots**; clients get redirects (MOVED/ASK) | **Redis Cluster** (16,384 slots) | Built-in sharding + replication + failover; multi-key ops limited to one slot (use hash tags `{user42}`) |
| **Managed** | Cloud service | ElastiCache, Memorystore, Azure Cache | Ops handled; limits and failover behavior differ |

### 8.8.3 Replication and failover
Each shard has a **primary and ≥1 replica** (asynchronous). On primary failure a replica is promoted (Redis Sentinel/Cluster). Be aware: **async replication can lose the latest writes** on failover (acceptable for a cache; unacceptable if the cache is your only copy). Memcached has no replication (rely on consistent hashing + warm-up, or client-level replication).

### 8.8.4 Memcached vs Redis
| | Memcached | Redis |
|---|---|---|
| Model | Pure key→blob cache | Rich data structures |
| Threading | Multi-threaded | Mostly single-threaded command execution (I/O threads optional) |
| Persistence | None | RDB/AOF optional |
| Replication/HA | Client-level | Built-in (Sentinel/Cluster) |
| Eviction | LRU (slab allocator) | Configurable policies |
| Best for | Simple, huge, multi-core caches of blobs | Caching + counters, sessions, leaderboards, queues, locks |
| Ops complexity | Lower | Moderate |

### 8.8.5 Sizing a cache cluster (worked example)
ShopFront: 2 M products, average cached object 2 KB (JSON/compressed), keep all products plus 3 variants (locale/currency) ⇒ 2 M × 3 × 2 KB = **12 GB** data; +60% overhead (keys, fragmentation, headroom) ≈ **19 GB**. Throughput: peak 40,000 page views/s × ~3 cache reads each (product, price, reviews summary) = **120,000 ops/s**; with ~70k ops/s comfortable per Redis shard ⇒ need ≥2 shards for throughput; memory fits easily in one 32 GB node.
Plan: **3 shards × (primary + replica)** (=6 nodes, 16 GB each): ~48 GB total usable per copy, throughput 3×70k = 210k ops/s (75% headroom for hot keys and failover), survives loss of any single node.
Also consider network: 120,000 ops/s × 2 KB = 240 MB/s ≈ 2 Gbit/s total: fine for 10 Gbit NICs (watch per-node).

### 8.8.6 Efficient access patterns
- **Batch** with `MGET`/pipelining: 100 sequential `GET`s at 0.5 ms = 50 ms; one `MGET` or a pipeline = ~1 ms. (Ch 1: round trips dominate.)
- **Connection pooling**; keep connections warm.
- **Client-side timeouts** (≤ 10–50 ms for caches): a slow cache must fail fast and fall back, not stall requests.
- **Compression** for large values (LZ4/Zstd); **compact serialization** (protobuf/MessagePack) over verbose JSON.
- **Key design:** `namespace:version:entity:id[:variant]`; keep keys short but readable; include everything that changes the response (tenant, locale, auth scope).

### 8.8.7 Multi-region caching `[GK]`
Caches are usually **regional** (cross-region latency defeats the purpose). Issues: invalidating copies in other regions after a write (Facebook propagates invalidations from the database commit log to remote regions, e.g., via `mcsqueal`, with regional pools and cold-cluster warm-up `[Public]`); stale reads after cross-region writes; per-region hit ratios; cost of replicating entire caches. Keep TTLs short for data written elsewhere.

---

## 8.9 Application-level caching patterns `[MK]`

| Pattern | What's cached | Example |
|---|---|---|
| **Object/entity cache** | One row/aggregate by ID | `user:42` |
| **Query-result cache** | Output of an expensive query keyed by normalized params | `search:{q=headphones,page=1,sort=rel}` |
| **Computed/aggregate cache** | Counts, leaderboards, recommendations | `trending:today` |
| **Fragment cache** | Rendered HTML/JSON pieces | Product card, header |
| **Full-page/response cache** | Entire responses (for anonymous traffic) | Home page, product page |
| **Session/token cache** | Sessions, auth decisions | `sess:abc` |
| **Rate-limit counters** | Windowed counts | `rl:user42:min` (Ch 22) |
| **Negative cache** | "Not found" markers | `product:v3:999999 = NOT_FOUND` |
| **Memoization** | Pure function results in-process | Config parsing, permission checks |
| **Precomputation / materialized views** | Expensive joins or feeds computed ahead | Timelines (Ch 26), daily reports |

### 8.9.1 HTTP caching `[MK]`
The web's built-in caching, honored by browsers, proxies, and CDNs:
| Header | Meaning |
|---|---|
| `Cache-Control: max-age=N` | Fresh for N seconds in any cache (browser or shared) |
| `s-maxage=N` | Same, but for shared caches (CDN/proxy) only |
| `public` / `private` | May / may not be stored by shared caches (use `private` for per-user data!) |
| `no-cache` | Must **revalidate** with the origin before reuse (still can store) |
| `no-store` | Do not store at all (sensitive data) |
| `immutable` | Never changes: skip revalidation (use with fingerprinted URLs) |
| `stale-while-revalidate=N` | Serve stale for N seconds while revalidating in the background |
| `stale-if-error=N` | Serve stale if the origin errors |
| `ETag` / `If-None-Match` | Validator: respond 304 Not Modified if unchanged |
| `Last-Modified` / `If-Modified-Since` | Time-based validator |
| `Vary: Accept-Encoding, Accept-Language` | Cache separately per these request headers (don't `Vary: *` or on cookies carelessly: it destroys hit rates) |
**Best practices:** fingerprint static assets (`app.3f9a1c.js`, `max-age=31536000, immutable`); short `s-maxage` + `stale-while-revalidate` for semi-dynamic pages; **never** let a shared cache store personalized responses (missing `private`/`Vary` is a classic data-leak incident); use `no-store` for sensitive data.

### 8.9.2 In-process caches (L1) with Caffeine/Guava `[GK]`
Features worth knowing: size or weight bounds, `expireAfterWrite`/`expireAfterAccess`, **`refreshAfterWrite`** (async refresh while serving the old value: stale-while-revalidate built in), **loading cache with single-flight**, W-TinyLFU eviction, stats. Use for tiny, hot, read-mostly data (config, feature flags, permission lookups, reference data) with **short TTL (1–60 s)** and accept per-instance staleness.

---

## 8.10 Multi-level caching `[MK]`

```text
Request ─▶ L1 in-process (µs, ~1–5 s TTL) ─miss▶ L2 Redis (~0.5 ms, minutes) ─miss▶ DB (10+ ms)
```
- L1 **absorbs hot keys** and cuts L2 load; L2 shares across instances and survives instance restarts.
- **Invalidation across L1s:** publish "invalidate key" on a pub/sub channel that every instance subscribes to (Redis pub/sub, Kafka); *messages can be lost*, so L1 TTLs stay short (seconds) as the safety net.
- **Consistency now varies by layer;** define the SLA: "value at most L1_TTL + L2_TTL stale after a missed invalidation."
- Hit-ratio math composes: backend load = (1−h₁)(1−h₂)·rate. L1 at 60% and L2 at 90% gives 4% reaching the DB.
- Costs: memory duplication in L1 (N copies), debugging complexity ("which layer served this?"), more invalidation paths. Use only when profiling shows L2 load/latency or hot keys justify it.

---

## 8.11 Worked designs

### 8.11.1 Product page (read-heavy, rarely changing)
- **Keys:** `product:v3:{id}:{locale}` (details), `price:v1:{id}` (short TTL), `reviews_summary:v1:{id}`.
- **Pattern:** cache-aside; TTL 5 min + ±20% jitter for details; 30–60 s for price/inventory display; **read the DB (or a strongly consistent path) at checkout**, since display ≠ decision.
- **Invalidation:** on product update → delete keys; plus CDC-driven invalidation for bulk catalog imports; TTL as safety net.
- **Hot products:** L1 cache (2 s TTL) + CDN `s-maxage=60, stale-while-revalidate=300` for anonymous pages.
- **Stampede protection:** single-flight per key; jitter; refresh-ahead for the top 5,000 products.
- **Result (estimates):** 160k queries/s → with ~97% combined hit ratio ⇒ ~4,800 queries/s to the DB: well within 20k/s capacity, with 4× headroom for cold-cache events.

### 8.11.2 User profile / session
- Cache-aside with explicit invalidation on update (delete), TTL 10 min; read-your-writes: after a user edits their profile, route their next reads to the DB or write-through for that key. Sessions in Redis with sliding TTL; replicate for HA (losing sessions = forced logins, tolerable if rare).

### 8.11.3 News feed timelines
- Precomputed timelines stored as Redis lists/sorted sets of post IDs per user (a *write-time cache*, Ch 26); hydrate post objects via MGET from the object cache; cap list length; rebuild from source on miss; TTL for inactive users; celebrity posts merged at read time.

### 8.11.4 Leaderboard
- Redis sorted set (`ZADD`, `ZREVRANGE`, `ZREVRANK`); periodic flush of scores to the DB; sharded by game/season; top-N cached in L1 for 1–5 s.

### 8.11.5 Rate limiter
- Counters in Redis with `INCR` + `EXPIRE` or token bucket in Lua; per-region keys; local pre-checks; fail-open/closed policy (Ch 22).

### 8.11.6 Search results
- Key = normalized query + filters + page; short TTL (30–120 s); popular queries dominate (heavy head), so hit ratios can be high; invalidate by TTL (precise invalidation impractical); consider caching only the ID lists and hydrating items from the object cache.

---

## 8.12 When not to cache `[MK]`

- **Low locality/flat access:** hit ratio won't justify memory and complexity (e.g., unique per-request computations).
- **High write rate relative to reads:** you'll spend more invalidating than saving.
- **Strict correctness for the read itself:** balance checks before debits, inventory at the moment of purchase, authorization revocation checks (use short TTLs or authoritative reads).
- **Highly personalized responses** (per-user pages) at shared layers (CDN): low reuse, privacy risk.
- **Cheap queries:** a primary-key lookup on a hot buffer pool is already ~1 ms; a cache adds a hop and a consistency problem for little gain.
- **When the real problem is a bad query/index:** fix the query (Ch 3) before masking it with a cache; caches hide slowness until they miss.
- **When the cache hit ratio is untracked:** if you can't measure it, you can't know it helps.
**Cost of a cache:** infrastructure, memory, engineering time, consistency bugs, new failure modes. Add one when the numbers say you must (Ch 5).

---

## 8.13 Observability for caches `[MK]`

Track and alert on:
- **Hit ratio** (overall *and* per key class/endpoint) and its **trend**; sudden drops (deploy changed keys, TTL bug, eviction storm) are leading indicators of DB trouble.
- **Eviction rate** (capacity pressure) and **expired vs evicted** counts.
- **Memory used vs max**, fragmentation, big-key list.
- **Latency** (p50/p99) of cache ops; **timeouts/errors** to the cache (fallback activations).
- **Miss penalty:** backend latency on misses; **DB load** correlated with cache metrics.
- **Hot keys** and per-shard imbalance (ops/s, CPU, network per node).
- **Staleness probes:** sample N keys per minute, compare cached vs source versions, and alert on divergence rate.
- **Connection counts** and client-side pool saturation.
Dashboards should answer within a minute: "Is the cache helping? Is it healthy? What happens to the DB if it disappears?"

---

## 8.14 Interview perspective

A strong caching answer states, in order: **(1) what** is cached and why (read/write ratio, skew, cost); **(2) where** (client/CDN/L1/L2); **(3) pattern** (cache-aside default; why not write-back); **(4) key design** and **TTL** (with jitter); **(5) invalidation** (explicit delete + TTL; CDC for derived data) and the **race** you're aware of; **(6) sizing** (data size × overhead; ops/s per shard; hit-ratio target and resulting DB load); **(7) failure behavior** (cache down ⇒ fallback + DB protection; stampede/avalanche/penetration/hot-key mitigations); **(8) consistency contract** (staleness bound; read-your-writes handling); **(9) metrics**. Mention **what you would not cache** (balances, authorization decisions).

---

## 8.15 Labs

### Lab 1: Hit ratios, stampede, and avalanche (Java 17+)
```java
import java.util.*;
import java.util.concurrent.*;
import java.util.concurrent.atomic.*;

public class CacheLab {
    // ---------- 1) LRU cache hit ratio under a Zipf (skewed) workload ----------
    static class Lru<K, V> extends LinkedHashMap<K, V> {
        final int cap;
        Lru(int cap) { super(16, 0.75f, true); this.cap = cap; }          // accessOrder=true -> LRU
        @Override protected boolean removeEldestEntry(Map.Entry<K, V> e) { return size() > cap; }
    }
    static int[] zipfSample(int n, double s, int count, long seed) {
        double[] cdf = new double[n]; double sum = 0;
        for (int k = 1; k <= n; k++) { sum += 1.0 / Math.pow(k, s); cdf[k - 1] = sum; }
        Random r = new Random(seed); int[] out = new int[count];
        for (int i = 0; i < count; i++) {
            double u = r.nextDouble() * sum; int lo = 0, hi = n - 1;
            while (lo < hi) { int mid = (lo + hi) >>> 1; if (cdf[mid] < u) lo = mid + 1; else hi = mid; }
            out[i] = lo;
        }
        return out;
    }
    static void hitRatioDemo() {
        int items = 100_000, requests = 2_000_000;
        int[] workload = zipfSample(items, 1.0, requests, 1);
        System.out.println("== 1) LRU hit ratio, 100,000 distinct items, Zipf(s=1.0) popularity, 2M requests ==");
        System.out.println("cache size (% of items)   hit ratio   DB load vs no cache   avg latency (cache 1 ms, DB 20 ms)");
        for (double pct : new double[]{0.1, 1, 5, 10, 20, 50}) {
            Lru<Integer, Boolean> cache = new Lru<>((int) (items * pct / 100));
            long hits = 0;
            for (int k : workload) { if (cache.get(k) != null) hits++; else cache.put(k, Boolean.TRUE); }
            double h = (double) hits / requests;
            System.out.printf("%8.1f%%                  %6.1f%%     %5.1f%% of requests    %5.2f ms%n", pct, 100 * h, 100 * (1 - h), h * 1 + (1 - h) * 21);
        }
    }

    // ---------- 2) Cache stampede: naive vs single-flight ----------
    static final AtomicInteger dbCalls = new AtomicInteger();
    static String slowDb(String key) { dbCalls.incrementAndGet(); try { Thread.sleep(100); } catch (InterruptedException ignored) {} return "value-of-" + key; }

    static int stampede(boolean singleFlight, int threads) throws Exception {
        ConcurrentHashMap<String, String> cache = new ConcurrentHashMap<>();          // the entry has just expired -> empty
        ConcurrentHashMap<String, CompletableFuture<String>> inflight = new ConcurrentHashMap<>();
        dbCalls.set(0);
        CountDownLatch gate = new CountDownLatch(1), done = new CountDownLatch(threads);
        for (int i = 0; i < threads; i++) new Thread(() -> {
            try {
                gate.await();
                String v = cache.get("hot");
                if (v == null) {
                    if (!singleFlight) { cache.put("hot", slowDb("hot")); }
                    else {
                        CompletableFuture<String> f = new CompletableFuture<>();
                        CompletableFuture<String> existing = inflight.putIfAbsent("hot", f);
                        if (existing == null) {                                        // I am the leader: load once
                            try { String x = slowDb("hot"); cache.put("hot", x); f.complete(x); }
                            finally { inflight.remove("hot"); }
                        } else existing.join();                                        // followers wait for the leader
                    }
                }
            } catch (Exception ignored) {} finally { done.countDown(); }
        }).start();
        gate.countDown(); done.await();
        return dbCalls.get();
    }

    // ---------- 3) Mass expiry ("avalanche"): TTL with and without jitter ----------
    static void avalancheDemo() {
        int keys = 10_000, ttl = 60; Random r = new Random(3);
        System.out.println("\n== 3) Avalanche: 10,000 keys, each read once per second, TTL 60 s, all cached at t=0 ==");
        for (double jitter : new double[]{0.0, 0.2}) {
            long[] expiresAt = new long[keys]; Arrays.fill(expiresAt, ttl);                 // cached at t=0 -> expire together
            if (jitter > 0) for (int i = 0; i < keys; i++) expiresAt[i] = (long) (ttl * (1 - jitter + 2 * jitter * r.nextDouble()));
            int[] dbPerSecond = new int[300];
            for (int t = 1; t < 300; t++)
                for (int k = 0; k < keys; k++)
                    if (t >= expiresAt[k]) { dbPerSecond[t]++; expiresAt[k] = t + (long) (ttl * (1 - jitter + 2 * jitter * r.nextDouble())); }   // miss -> DB load -> recache
            int max = 0, argmax = 0; long total = 0; for (int t = 1; t < 300; t++) { total += dbPerSecond[t]; if (dbPerSecond[t] > max) { max = dbPerSecond[t]; argmax = t; } }
            System.out.printf("jitter +/-%2.0f%%: peak DB loads in one second = %5d (at t=%3ds);  average DB loads/s = %.0f%n", jitter * 100, max, argmax, total / 299.0);
        }
    }

    public static void main(String[] args) throws Exception {
        hitRatioDemo();
        System.out.println("\n== 2) Stampede: 300 concurrent requests arrive just after a hot key expired (DB call takes 100 ms) ==");
        System.out.println("naive cache-aside      -> DB calls: " + stampede(false, 300));
        System.out.println("single-flight (coalesce)-> DB calls: " + stampede(true, 300));
        avalancheDemo();
    }
}
```
**Sample output**
```text
== 1) LRU hit ratio, 100,000 distinct items, Zipf(s=1.0) popularity, 2M requests ==
cache size (% of items)   hit ratio   DB load vs no cache   avg latency (cache 1 ms, DB 20 ms)
     0.1%                    29.0%      71.0% of requests    15.20 ms
     1.0%                    50.7%      49.3% of requests    10.87 ms
     5.0%                    66.5%      33.5% of requests     7.69 ms
    10.0%                    73.7%      26.3% of requests     6.26 ms
    20.0%                    81.0%      19.0% of requests     4.81 ms
    50.0%                    90.6%       9.4% of requests     2.87 ms

== 2) Stampede: 300 concurrent requests arrive just after a hot key expired (DB call takes 100 ms) ==
naive cache-aside      -> DB calls: 300
single-flight (coalesce)-> DB calls: 1

== 3) Avalanche: 10,000 keys, each read once per second, TTL 60 s, all cached at t=0 ==
jitter +/- 0%: peak DB loads in one second = 10000 (at t= 60s);  average DB loads/s = 134
jitter +/-20%: peak DB loads in one second =   443 (at t= 48s);  average DB loads/s = 152
```
**Experiments**
1. Change the Zipf exponent to 0.7 (flatter) and 1.3 (skewier). How do hit ratios change at 1% and 10% cache size? What does this tell you about measuring your own workload?
2. Add a **scan** (sequentially read 50,000 never-repeated keys in the middle of the workload). How much does LRU's hit ratio drop for the following requests? Implement a simple **admission filter** (admit a key only on its 2nd request, tracking recent first-time keys in a small LRU) and compare.
3. Implement **LFU** (with counters) and compare with LRU on the Zipf workload. Then change popularity halfway through the run (shift the hot set) and see which adapts faster.
4. In the stampede demo, add **probabilistic early expiration** and show the DB calls spread over time rather than 1 big spike.
5. In the avalanche demo, simulate **loss of a cache node** (10% of keys vanish at t=100): compare consistent hashing vs `hash mod N` remapping when a node is *added* (percentage of keys that move).

### Lab 2: Reproduce the cache-aside stale-populate race, and fix it (Java 17+)
```java
import java.util.concurrent.*;

/** Deterministic reproduction of the cache-aside race: a slow reader re-populates the cache with a STALE value
 *  after a writer has already updated the database and invalidated the cache. */
public class CacheRaceLab {
    static volatile int dbValue;                                         // the database (value doubles as its version)
    static final ConcurrentHashMap<String, Integer> cache = new ConcurrentHashMap<>();
    static final ConcurrentHashMap<String, Integer> floorVersion = new ConcurrentHashMap<>();   // "lease"/invalidation marker

    enum Mode { NAIVE_DELETE, WRITE_THROUGH_NEWER_WINS, INVALIDATION_FLOOR }

    static String run(Mode mode) throws Exception {
        dbValue = 1; cache.clear(); floorVersion.clear();
        CountDownLatch readerReadDb = new CountDownLatch(1), writerDone = new CountDownLatch(1);

        Thread reader = new Thread(() -> {                               // a normal cache-aside READ that misses
            try {
                int v = dbValue;                                         // (1) miss -> load from DB: sees version 1
                readerReadDb.countDown();
                writerDone.await();                                      // (3) reader stalls (GC pause, slow network) and resumes later
                switch (mode) {
                    case NAIVE_DELETE -> cache.put("k", v);                                              // blindly populate
                    case WRITE_THROUGH_NEWER_WINS -> cache.merge("k", v, Math::max);                     // never overwrite a newer version
                    case INVALIDATION_FLOOR -> { if (v >= floorVersion.getOrDefault("k", 0)) cache.put("k", v); }   // refuse stale populate
                }
            } catch (InterruptedException ignored) {}
        });
        Thread writer = new Thread(() -> {                               // a WRITE: update DB, then fix the cache
            try {
                readerReadDb.await();
                dbValue = 2;                                             // (2a) update DB to version 2
                switch (mode) {
                    case NAIVE_DELETE -> cache.remove("k");                                              // (2b) just delete the key
                    case WRITE_THROUGH_NEWER_WINS -> cache.merge("k", 2, Math::max);                     // (2b) write new value into cache
                    case INVALIDATION_FLOOR -> { floorVersion.merge("k", 2, Math::max); cache.remove("k"); }  // (2b) delete + record min acceptable version
                }
                writerDone.countDown();
            } catch (InterruptedException ignored) {}
        });
        reader.start(); writer.start(); reader.join(); writer.join();
        Integer c = cache.get("k");
        String verdict = c == null ? "cache empty -> next read reloads version 2 (correct)"
                       : c == dbValue ? "cache has version " + c + " (correct)"
                       : "cache has STALE version " + c + " while DB has " + dbValue + "  <-- BUG, persists until TTL";
        return verdict;
    }

    public static void main(String[] args) throws Exception {
        for (Mode m : Mode.values()) System.out.printf("%-28s -> %s%n", m, run(m));
    }
}
```
**Output**
```text
NAIVE_DELETE                 -> cache has STALE version 1 while DB has 2  <-- BUG, persists until TTL
WRITE_THROUGH_NEWER_WINS     -> cache has version 2 (correct)
INVALIDATION_FLOOR           -> cache empty -> next read reloads version 2 (correct)
```
**Questions**
1. Why does the "floor"/lease approach leave the cache empty, and is that acceptable?
2. What breaks if the *invalidation floor* map itself is lost (cache restart)? (Think: TTL as the safety net.)
3. Real databases have monotonically increasing row versions or commit timestamps. How would you use them as the "version" in a Redis Lua script that does *compare-and-set if newer*?
4. How does a CDC-driven invalidator interact with this race? (The invalidation is delivered after the commit; can a stale reader still populate after it?)

### Lab 3: Implement and test the LRU cache (§8.5.2)
1. Implement the `LruCache` and write tests: capacity 3; put A,B,C; get A; put D ⇒ B evicted.
2. Add a TTL per entry (lazy expiry on `get`), plus statistics (hits, misses, evictions).
3. Make it thread-safe with lock striping (e.g., 16 segments by `hash(key) % 16`) and benchmark 8 threads vs the coarse lock.
4. Wrap it as a **read-through loading cache with single-flight** (`getOrLoad(key, loader)`), and show with a 100 ms loader that 300 concurrent callers trigger one load.

### Lab 4: Redis caching in practice (needs Docker; not executed in the book's sandbox)
```bash
docker run --name r -p 6379:6379 -d redis:7
docker exec -it r redis-cli
```
```text
CONFIG SET maxmemory 20mb
CONFIG SET maxmemory-policy allkeys-lru
# fill beyond 20 MB with: redis-cli --pipe  (or a small loop) and watch evicted_keys in INFO stats
INFO stats                       # keyspace_hits, keyspace_misses, evicted_keys
SET product:v3:42 "{...}" EX 300
MGET product:v3:42 product:v3:43 product:v3:44       # batched lookups
OBJECT FREQ product:v3:42        # with allkeys-lfu policy
redis-cli --hotkeys              # (needs lfu policy) find hot keys
redis-cli --bigkeys              # find big keys
```
**Questions:** compute your hit ratio from `keyspace_hits/(hits+misses)`; what happens when you switch to `noeviction` and fill memory? Why is `KEYS *` dangerous?

---

## 8.16 Common misconceptions

1. **"A cache makes things faster."** It makes the *hit path* faster and protects the backend; a poorly-hit cache adds latency and complexity.
2. **"90% hit ratio is great."** It still sends 10% of traffic to the DB; at scale that may be the bulk of the load.
3. **"Just delete the key on write; problem solved."** The stale-populate race remains.
4. **"TTL is a substitute for invalidation."** It bounds staleness; it doesn't prevent it. Use both.
5. **"Write-back is just faster write-through."** It risks data loss and complicates recovery.
6. **"Cache the whole database."** Cache the *working set*; eviction policy and sizing matter.
7. **"Redis is always durable enough to be the source of truth."** Async replication and persistence settings can lose data.
8. **"Caching is only for the database."** Use it at every layer: browser, CDN, gateway, app, DB.
9. **"The cache never goes down."** It does; design for it (and for a cold restart).
10. **"More cache nodes always help."** Hot keys, big keys, and skew can leave one node saturated.
11. **"We can cache personalized pages at the CDN."** Not without careful keys/`private`; leaking user data is a classic incident.
12. **"Hit ratio is a vanity metric."** It's an early-warning indicator for DB overload.

---

## 8.17 Chapter summary

- Caches work because of the **latency gap, locality, and skew**. **Backend load ∝ (1−hit ratio)**: caches protect backends dramatically (90%→99% = 10× less load); hit ratio grows ~logarithmically with cache size, so **measure your workload**.
- Caches live at **many layers** (browser → CDN → proxy → L1 → L2 → DB buffer pool); place them close to the consumer as correctness allows.
- **Patterns:** cache-aside (default), read-through, write-through, write-back (loss risk), write-around, refresh-ahead.
- **Eviction:** LRU (O(1) with map + doubly linked list), LFU, TinyLFU/2Q for scan resistance; account for real memory overhead.
- **Invalidation is the hard part:** always use TTL; delete-on-write, versioning, CDC, stale-while-revalidate; the **stale-populate race** is real, with fixes via versioned CAS or leases.
- **Failure modes:** stampede (single-flight, early expiry, SWR, jitter), avalanche (jitter, consistent hashing), penetration (negative caching, Bloom filter), hot keys (L1, replication), cold start, **metastable failure**, poisoned caches, hidden hard dependency.
- **Distributed caches:** client/proxy/server-side sharding (Redis Cluster slots), replication (async ⇒ possible loss), sizing by memory and ops/s, batching, short client timeouts.
- **HTTP caching** headers; **multi-level caches** with pub/sub invalidation; know what **not** to cache; **observe** hit ratio, evictions, hot keys, staleness.

### Mental models
| Concept | Mental model |
|---|---|
| Cache | Books on your desk instead of the library across town |
| Hit ratio and backend load | A bouncer: each extra percent of people he stops means fewer in the club |
| Stale-populate race | Someone photocopies a page, the original is updated, then the old copy is filed after the notice to discard old copies |
| Stampede | Everyone rushing the one open counter when the line resets |
| Avalanche | All milk cartons in the fridge expiring on the same day |
| Single-flight | One person goes to the store; the rest wait for the groceries |
| Metastable failure | A traffic jam that persists after the accident is cleared |

---

## 8.18 Exercises

### A. Conceptual
1. Explain why caching works using latency gap, locality, and skew. Give an example workload where caching works poorly.
2. Derive the average latency formula and the backend-load formula. Why does going from 90% to 99% hit ratio reduce backend load by 10× but latency by less than 3×?
3. A cache at 95% hit ratio serves 100,000 req/s to a DB that handles 8,000 qps. Is the DB OK? What if a deploy drops the hit ratio to 80%?
4. Compare cache-aside, read-through, write-through, write-back, write-around, refresh-ahead: for each give one workload where it's the best choice.
5. Why does cache-aside delete the key on write rather than update it?
6. Describe the stale-populate race with a timeline. List three fixes and their trade-offs.
7. What's the difference between LRU and LFU? What is scan pollution, and how do TinyLFU/2Q address it?
8. Explain how Redis's approximate LRU works and why it does it that way.
9. Define stampede, avalanche, penetration, and hot key. For each, give one mitigation.
10. What is probabilistic early expiration? Write its refresh condition and explain the intuition.
11. What is a metastable failure in the cache/DB context? List three defenses.
12. Compare Memcached and Redis for a pure cache. When prefer each?
13. Explain Redis Cluster's hash slots. Why are multi-key operations restricted, and how do hash tags help?
14. What does a lease token prevent in a cache design?
15. Interpret: `Cache-Control: public, s-maxage=60, stale-while-revalidate=300`. What does the CDN do at 30 s, 90 s, 400 s?
16. Why is `Vary: Cookie` dangerous for hit ratio? What about a missing `private` on personalized responses?
17. Why use both an L1 and an L2 cache? What are the invalidation challenges?
18. List five things you should *not* cache and why.
19. What metrics tell you a cache is healthy? Which is the best early-warning signal for DB trouble?
20. Why must every cache entry have a TTL even if you have explicit invalidation?

### B. Calculations
21. A cache lookup costs 0.6 ms, a miss costs a further 12 ms at the DB. Compute the average latency at hit ratios of 70%, 90%, 99%. 
22. Traffic: 50,000 req/s; objects 4 KB; 25 M distinct items in the working set; Zipf makes a cache of 10% of items yield 85% hits. Compute cache memory (with 50% overhead), DB load, and network bandwidth to/from the cache at peak.
23. A Redis shard handles 80,000 ops/s and 25 GB. You need 300,000 ops/s and 120 GB of data. How many shards (primaries) with 30% headroom on both? With one replica each, how many nodes in total?
24. A hot key receives 60,000 req/s; one Redis node handles 80,000 ops/s total and already serves 35,000 ops/s for other keys. What happens? Design a fix and compute the load per node after replicating the hot key into 4 copies.
25. TTL jitter: 1 M keys are cached at time 0 with TTL 600 s; each key is read about once per 10 s. With no jitter, how many DB loads happen in the second after t=600 s (approximately)? With ±10% jitter, estimate the peak per-second loads over [540 s, 660 s].
26. Multi-level: L1 hit 50%, L2 hit 90% of L1 misses. What fraction reaches the DB? If total traffic is 200,000 req/s, DB load?
27. `hash(key) mod N` with N=10 → 11: what fraction of keys remap? With consistent hashing (ideal), what fraction?
28. XFetch parameters: δ (recompute time) = 2 s, β = 1, TTL remaining = 3 s. Compute the probability that a request triggers an early refresh in the next request if `rand()` is uniform (the condition is `−δ·β·ln(rand) ≥ remaining`).

### C. Design
29. Design the caching strategy for an e-commerce site: product pages, search results, cart, inventory display, price, user session. Specify layers, patterns, keys, TTLs, invalidation, failure behavior.
30. Design a cache for a social profile service where a few celebrity profiles get 100× the traffic: avoid hot-key saturation; handle updates and invalidation.
31. Design a leaderboard service for 10 M players with top-100 and "my rank" queries, 20k updates/s. Where does caching fit? How do you handle durability?
32. Design the invalidation pipeline for a catalog with 5 M products updated by bulk imports (millions of changes per hour) and individual edits; avoid stale pages and stampedes after imports.
33. You have a global app with regional caches and a single-region primary DB. How do you invalidate remote caches after a write? What staleness do you promise?
34. Create a runbook for "cache cluster lost entirely at peak": immediate actions, protections for the DB, and a gradual recovery plan.

### D. Debugging
35. Users see old prices for up to an hour on 0.1% of products although you delete cache keys on update and TTL is 1 hour. Find the likely cause and fix it.
36. Hit ratio fell from 95% to 40% right after a deploy; no traffic change. List five hypotheses and how to confirm each.
37. A Redis cluster has 6 shards; one is at 100% CPU while others sit at 20%. Diagnose (hot key? big key? hash tags? scans?) and fix.
38. After a Redis failover, some users see items they deleted. Explain (async replication, lost deletes, TTL) and propose mitigations.
39. A nightly batch job reads every product once; each morning the site is slow until noon. Explain and fix.
40. CPU on app servers jumped after enabling caching of JSON objects in Redis. Why might this happen and what are the fixes?

### E. Interview-style
41. "Design a cache for a service that gets 500k reads/s on 50 M items." Give a 5-minute structured answer.
42. "How do you keep the cache and DB consistent?" Give a nuanced answer including races.
43. "What happens if Redis goes down?" Describe expected behavior and protections.

### F. Teach-back
44. Explain the cache stampede and single-flight to a friend using a restaurant kitchen analogy.

---

## 8.19 Full answer key

**1.** Latency gap: memory/network cache hit (~1 ms) vs slower backend (10–100+ ms). Locality: recently/neighboring items reused. Skew: a small head of items gets most requests. Poor fit: flat/unique access patterns (e.g., per-request computed results with no repetition, or enormous long-tail catalogs with little reuse).

**2.** Latency = h·c + (1−h)(c+d). Backend load = (1−h)·λ. 90%→99%: backend load 0.1λ → 0.01λ (10× less). Latency with c=1,d=20: 3.0 → 1.2 ms (2.5×). The miss penalty is the dominant cost, so reducing misses reduces *backend load* proportionally to 1/(1−h) but latency only linearly in (1−h)·d.

**3.** 95%: misses = 5,000 qps < 8,000 ✔ (62.5% of DB capacity). 80%: misses = 20,000 qps > 8,000 ✘ → DB overloaded (2.5× capacity) ⇒ cascading failure risk.

**4.** Cache-aside: product catalog reads. Read-through: library-managed loading with single-flight. Write-through: user settings needing read-your-writes. Write-back: high-rate counters/telemetry aggregates where small loss is OK. Write-around: large write-once uploads/logs. Refresh-ahead: top-N hot items needing consistently low latency.

**5.** Updating can race with other writers (two updates reaching the cache out of order leave the older value), and requires computing the cache representation at write time. Deleting is idempotent and lets the next reader rebuild from the source of truth.

**6.** Timeline: reader misses and reads DB (v1); writer updates DB (v2) and deletes the key; reader (slow) sets v1 into the cache ⇒ stale until TTL. Fixes: (1) TTL (bounds damage); (2) versioned values with compare-and-set/"newer wins" (+ write-through of new version); (3) leases/invalidation markers (reject stale populate; also throttle refills); (4) delayed double delete (probabilistic); (5) per-key locking (costly). Trade-offs: complexity vs guarantee strength.

**7.** LRU evicts the least recently used; LFU evicts the least frequently used. Scan pollution: a one-time sequential scan floods an LRU with cold keys and evicts the hot set. TinyLFU/2Q use admission control/segmenting (don't promote or admit one-hit items) so scans don't displace frequently used entries.

**8.** It samples a few random keys (default 5) and evicts the one with the oldest access time among them, avoiding maintenance of a global linked list (memory/CPU overhead and lock contention), with accuracy close to true LRU for modest samples.

**9.** Stampede: many concurrent misses to a hot key (single-flight). Avalanche: mass expiry/loss (TTL jitter, consistent hashing, replicas). Penetration: queries for nonexistent keys bypass the cache (negative caching, Bloom filter). Hot key: one key overloads a node (L1 cache, replicate hot key).

**10.** Each reader decides to refresh early with probability that grows as expiry approaches: refresh if `now − δ·β·ln(rand()) ≥ expiry_time`. Intuition: `−ln(rand)` is exponentially distributed; expensive items (large δ) refresh earlier; only a few readers refresh before expiry, avoiding a synchronized stampede without locks.

**11.** After a trigger (cache loss), the DB overloads, requests fail before refilling the cache, and the system remains in the bad state even after the trigger ends. Defenses: DB admission control/load shedding; limit retries and cap concurrency per key; serve stale data; allow partial results to populate the cache; gradual traffic ramp-up; keep warm replicas.

**12.** Memcached: simple multi-threaded blob cache, no persistence, easy at huge scale; Redis: richer structures, replication/persistence, atomic ops (counters, ZSETs), but mostly single-threaded per node. Prefer Memcached for plain large-object caching at high concurrency; Redis when you need structures, replication, or caching + other use cases.

**13.** The key space is split into 16,384 slots mapped to nodes; clients route by `CRC16(key) mod 16384`. Multi-key operations require all keys in one slot (otherwise CROSSSLOT error). Hash tags `{user42}` hash only the tagged part, forcing related keys into the same slot.

**14.** A lease token given to a reader on a miss is invalidated if the key is deleted/updated before the reader sets it, preventing a stale value from being stored; leases also limit concurrent refills (stampede control).

**15.** Browsers/shared caches may store it; CDN treats it fresh for 60 s. At 30 s: served from cache fresh. At 90 s: stale but within the SWR window ⇒ serve stale immediately and revalidate in the background. At 400 s: beyond 60+300 = 360 s ⇒ must revalidate synchronously (miss path).

**16.** `Vary: Cookie` creates a separate cache entry per cookie value (practically per user), destroying hit rates. Missing `private` on personalized responses lets shared caches serve one user's data to others (privacy incident).

**17.** L1: µs access, absorbs hot keys, no network; L2: shared, larger, survives instance restarts. Invalidation: L1 copies live in every instance; use pub/sub broadcast plus short L1 TTLs because messages can be lost; staleness is the sum of layers' bounds.

**18.** Examples: balances used for debits; authorization decisions with long TTLs; highly personalized pages at shared layers; very cheap PK lookups; write-heavy hot rows; sensitive data (no-store); error responses; items with strict real-time requirements.

**19.** Hit ratio (and per-class), eviction rate, memory usage, latency/timeouts, hot keys, per-shard imbalance, staleness probes. The hit-ratio trend is the best early warning: drops precede DB overload.

**20.** TTL bounds the damage of any missed invalidation, bug, race, lost pub/sub message, or code path that forgot to invalidate; also reclaims memory for abandoned keys.

**21.** Average = c + (1−h)·d: 70%: 0.6 + 0.3×12 = **4.2 ms**; 90%: 0.6 + 0.1×12 = **1.8 ms**; 99%: 0.6 + 0.01×12 = **0.72 ms**.

**22.** Cached items: 10% × 25 M = 2.5 M × 4 KB = 10 GB; +50% overhead = **15 GB**. DB load: 15% × 50,000 = **7,500 qps**. Network: hits 85% × 50,000 × 4 KB = 170 MB/s (1.36 Gbit/s) from cache; plus populates 7,500 × 4 KB = 30 MB/s to cache.

**23.** Throughput: with 30% headroom, usable 56,000 ops/s per shard → 300,000/56,000 = 5.4 → 6 shards. Memory: usable 17.5 GB per shard → 120/17.5 = 6.9 → 7 shards. Need **7 primaries**; with one replica each ⇒ **14 nodes**.

**24.** The hot key's node needs 60,000 + 35,000 = 95,000 ops/s > 80,000: saturated; latency spikes for all keys on that shard. Fix: replicate the key into 4 copies on different shards (random choice on read): each copy gets 15,000 ops/s; the hot node load becomes 35,000 + 15,000 = 50,000 (OK). Alternatively add an L1 cache with 1 s TTL per app instance (N instances × 1/s ≈ trivial load).

**25.** No jitter: all 1 M keys expire at 600 s; each is read about once per 10 s, so over the next 10 s all re-miss ≈ 100,000 loads/s on average, with a peak possibly near 1 M within the first read-interval if reads are spread (≈100k/s). With ±10% jitter (expiries spread over 540–660 s = 120 s): ≈ 1 M/120 ≈ **8,300 loads/s** peak-ish. An order-of-magnitude reduction.

**26.** Reach DB = 0.5 × 0.1 = 5%. DB load = 0.05 × 200,000 = **10,000 req/s**.

**27.** mod N: a key stays only if hash mod 10 == hash mod 11 ⇒ ~1/11 stay in the best case (≈9%); so ≈ **91%** remap. Consistent hashing: ~1/11 ≈ **9.1%** remap.

**28.** Condition: `−2·ln(r) ≥ 3` ⇒ `ln(r) ≤ −1.5` ⇒ `r ≤ e^(−1.5) = 0.223`. Probability ≈ **22.3%** that a given request triggers an early refresh at 3 s remaining.

**29 (sketch).** Product pages: CDN (anon, `s-maxage=60`, SWR) + L2 cache-aside (5 min + jitter) + invalidate on update; search results: L2, 60 s TTL, normalized keys; cart: Redis/DB per user (not shared cache only; durable store as truth); inventory display: 30 s TTL with checkout reading authoritative; price: short TTL + event-driven invalidation; sessions: Redis with replication. Failure: DB protection, stale serving, single-flight.

**30 (sketch).** L1 cache (1–5 s) for hot profiles; replicate hot keys across shards; CDN for public profile endpoints; update via write-through/delete + pub/sub to L1s; detect hot keys automatically; protect with per-key rate limits; use versioned keys for atomic switching.

**31 (sketch).** Redis ZSET per leaderboard shard (by game/season); `ZADD` with LT/GT as needed; top-100 cached in L1 for 1–2 s; "my rank" via `ZREVRANK`; persist scores durably (write-behind batch to DB, or event log) and rebuild ZSET from source on loss; handle 20k updates/s across shards.

**32 (sketch).** CDC from the catalog DB into a stream; invalidation consumers delete/refresh keys in batches with rate limits; bulk imports flagged to use versioned keys (`catalog_version` bump) so old keys age out; pre-warm top products; stampede protection; throttle imports.

**33 (sketch).** Publish invalidations from the primary's commit log to all regions (CDC/replication stream); regional caches apply deletes; promise bounded staleness (e.g., invalidation latency + TTL); read-your-writes for the writer's region via write-through or bypass; TTLs short for cross-region data.

**34 (sketch).** Immediately enable load shedding/rate limits and protect the DB (circuit breakers, concurrency caps); serve stale/CDN content; scale DB read replicas if possible; restart cache with replicas/snapshots; pre-warm the hot set (top keys); ramp traffic gradually (percentage-based); monitor DB load and hit ratio; post-incident: add cache replicas, capacity for degraded mode.

**35.** The stale-populate race (slow reader repopulates old value after the invalidation) or an invalidation path missed (e.g., updates via another service/bulk job that doesn't delete keys), or replication lag (cache populated from a lagging replica). Fix: versioned CAS/leases, CDC-based invalidation for all writers, read from primary after invalidation, shorter TTL.

**36.** Hypotheses: key format/version changed (all keys miss); serialization incompatibility between versions; TTL set too low/bug; eviction pressure (cache memory reduced or bigger objects); wrong cache cluster/namespace; connection errors falling back to the DB; new traffic mix (more unique keys); jitter bug. Confirm via keyspace stats, key samples, error metrics, memory/evictions, diffs in config.

**37.** Likely a hot key or big key on that shard; or hash tag concentrating many keys in one slot; or expensive commands (`KEYS`, big `SMEMBERS`); or a stuck rebalance. Use `--hotkeys`, `--bigkeys`, `SLOWLOG`, per-slot stats; fix by key splitting/replication, L1, removing hash tag skew, avoiding O(N) commands.

**38.** Async replication: the delete (or TTL expiry) on the old primary wasn't replicated before failover; the promoted replica still has the old entry. Mitigate: use short TTLs, delete again after failover (replay deletes from CDC), version checks on read, avoid relying on the cache for deletion semantics (authoritative check for sensitive data), enable WAIT for critical deletes.

**39.** Scan pollution: the batch job's one-time reads evict hot entries under LRU; morning traffic then misses until the hot set is rebuilt. Fix: run batch off-peak with cache bypass, separate cache namespace/cluster, admission control (TinyLFU), or read from a replica without populating the cache.

**40.** Serialization/deserialization CPU (JSON parse/Java serialization) on every cache hit; large objects; compression overhead. Fixes: compact binary formats, smaller cached projections, L1 cache of deserialized objects, avoid caching what's cheap to compute, cache at a coarser granularity (fragments) to reduce per-request deserialization.

**41 (sketch).** Assume 50 M items × 2 KB = 100 GB total; Zipf head: cache 20% = 20 GB (+50% overhead = 30 GB) → ~85% hits; 500k reads/s ⇒ DB 75k/s (too high) so add L1 (2 s TTL, 50% hit) ⇒ 37.5k/s... then replicas/more cache memory to reach 95–97%; Redis Cluster 6–8 shards (500k/70k ≈ 8) with replicas; cache-aside, TTL 5 min + jitter, single-flight, negative caching, Bloom filter for penetration, hot-key replication; invalidation via delete + CDC; metrics; fallback + DB protection.

**42 (sketch).** TTL + delete-on-write + CDC; races (stale-populate) and fixes (versioned CAS/leases); consistency contract (bounded staleness, read-your-writes for writers); authoritative reads for critical decisions.

**43 (sketch).** App falls back to DB with short cache timeouts and circuit breaker; protect DB with concurrency limits and load shedding; serve stale local copies/CDN; cluster HA (replicas/failover); cold start plan (pre-warm, gradual ramp); alert on hit ratio and DB load; size DB for degraded mode.

**44.** A kitchen at lunchtime: when the menu item runs out, 300 waiters all go to the chef for the same dish at once; with single-flight, one waiter orders it and the rest wait for that plate to be delivered.

---

## 8.20 Flashcards

| Front | Back |
|---|---|
| Avg latency with cache | h·c + (1−h)(c+d) |
| Backend load | (1−h)·λ; 90%→99% = 10× less |
| Hit ratio vs size | ~logarithmic; measure your workload (miss-ratio curve) |
| Cache-aside | App checks cache, loads DB on miss, deletes on write |
| Why delete not update | Avoids out-of-order writer races |
| Write-back risk | Data loss if cache dies before flush |
| LRU structure | Hash map + doubly linked list, O(1) |
| Scan pollution | One-time scan evicts the hot set; fix with TinyLFU/2Q |
| Redis eviction | Approximate LRU/LFU via sampling; noeviction errors when full |
| Stale-populate race | Slow reader repopulates old value after invalidation; fix with versions/leases |
| Always set | A TTL (with jitter) |
| Stampede fix | Single-flight, early expiry, SWR, jitter |
| Avalanche fix | TTL jitter (peak 10,000 → 443 in lab), consistent hashing, replicas |
| Penetration fix | Negative caching, Bloom filter |
| Hot key fix | L1 cache, replicate key |
| Metastable failure | Cache loss → DB overload → can't refill → stuck |
| Redis Cluster | 16,384 slots; hash tags for same-slot keys |
| Batch | MGET/pipeline: 1 round trip, not N |
| Cache-Control | max-age, s-maxage, private/public, no-store, SWR, immutable |
| Don't cache | Debit-time balances, long-TTL authz, personalized shared-layer responses |
| Key design | namespace:version:entity:id[:variant] |

## 8.21 Glossary additions

| Term | Simple Meaning | Technical Meaning | Why It Exists | Example |
|---|---|---|---|---|
| Cache | Fast copy store | Smaller, faster store of copies | Latency, backend protection | Redis |
| Hit ratio | Fraction served from cache | hits ÷ lookups | Measures effectiveness | 95% |
| TTL | Expiry timer | Time-to-live of an entry | Bound staleness | 300 s |
| Cache-aside | App-managed cache | Check, load on miss, populate | Default pattern | `getProduct` |
| Read-through | Cache loads for you | Cache layer fetches from source | Simpler apps | LoadingCache |
| Write-through | Write both now | Synchronous cache+DB write | Fresh cache | Profile update |
| Write-back | Write cache, DB later | Asynchronous flush | Fast writes | Counters |
| Write-around | Skip cache on write | Write to DB only | Avoid pollution | Uploads |
| Eviction | Make room | Removal policy when full | Finite memory | LRU |
| LRU / LFU | Least recently / frequently used | Eviction policies | Locality / popularity | Caffeine |
| TinyLFU | Admission filter | Frequency-sketch-based admission | Scan resistance | W-TinyLFU |
| Invalidation | Remove outdated | Delete/update on source change | Correctness | `DEL key` |
| Stale data | Outdated copy | Cached value differs from source | The cost of caching | Old price |
| Stampede | Herd of misses | Concurrent misses overload the source | Failure mode | Hot key expiry |
| Single-flight | One loads, others wait | Request coalescing | Stampede control | Future map |
| Avalanche | Mass expiry | Many keys expire/lose at once | Failure mode | Same-TTL keys |
| Penetration | Misses that never fill | Requests for nonexistent keys | Failure mode | Random IDs |
| Negative caching | Cache "not found" | Short-TTL absence markers | Stop penetration | `NOT_FOUND` |
| Hot key | One overloaded key | Skewed access to a single key | Shard saturation | Celebrity |
| Lease | Permission to fill | Token authorizing a cache set | Race/stampede control | Memcache leases |
| Stale-while-revalidate | Serve old, refresh | Serve stale during background refresh | Latency, stampede | `stale-while-revalidate` |
| Metastable failure | Stuck bad state | Self-sustaining overload after trigger gone | Outage class | Empty cache + hot DB |
| L1 / L2 cache | Local / shared cache | In-process vs distributed layers | Speed vs sharing | Caffeine / Redis |
| Hash slot | Shard bucket | Redis Cluster key-space partition | Sharding | 16,384 slots |
| Working set | Actively used data | Distinct data touched in a window | Cache sizing | 20 GB |

---

**Next: Chapter 9: Replication.** Single-leader, multi-leader, and leaderless replication; sync/async/semi-sync; replication lag and the consistency anomalies it creates (with simulations of read-your-writes, quorums, and failover data loss); failover and split-brain; conflict resolution; and how real systems (Postgres, MySQL, MongoDB, Cassandra, Kafka, Aurora) replicate.

# Chapter 26: Problem Book II (Intermediate)

## Chapter card

| | |
|---|---|
| **Objective** | Solve six intermediate problems with the nine-step method: **news feed**, **notification system**, **Instagram-style photo sharing**, **Dropbox-style file sync**, **web crawler**, and **typeahead / autocomplete**. These problems are dominated by *fan-out*, *priorities and isolation*, *large media pipelines*, *incremental sync*, *scheduling under constraints*, and *precomputation for low latency* |
| **Why this chapter exists** | Chapter 25 problems have one dominant difficulty each. Here several difficulties interact (hot keys with fan-out; bytes with pipelines; politeness with scale), and the "right" design depends on measuring the distribution of the data, not on a pattern name. Every solution is backed by an experiment that changes your mind about something |
| **Prerequisites** | Chapter 24 (method), Chapter 25 (beginner problems), Ch 8 (caching), 10 (sharding), 11 (CDN/object storage), 12–13 (queues, logs), 20 (Bloom filters, sketches) |
| **Study time** | 16–24 hours (attempt each problem first!) |
| **Tier** | `[MK]` |
| **Mastery criteria** | For each problem: (1) state requirements and compute the numbers, (2) justify the central design choice with a measurement or a derivation, (3) explain at least two failure modes and mitigations, (4) answer the follow-ups, (5) give the trade-offs in *choice / reason / cost / alternative* form |

Every problem uses the layout **Requirements → Estimates → API and data model → Architecture (evolution) → Deep dives (with measured results) → Failure modes → Trade-offs → Follow-ups**.

---

# Problem 1: News Feed (Twitter / Facebook timeline)

> **Problem statement.** Design a social network's home feed: users follow other users and see a reverse-chronological (optionally ranked) stream of their posts.

## 1.1 Requirements
- **Functional:** create a post (text + media references); follow/unfollow; load the home feed (paginated, infinite scroll); a user's own profile timeline. Likes/comments, ads, and ranking models are out of scope (we leave a hook for ranking).
- **Non-functional:** 200 M DAU; ~40 M posts/day; each DAU opens the feed ~10 times/day = **2 B feed reads/day**; feed load p99 < 300 ms; new posts visible to followers within seconds (a few seconds of staleness is fine); availability 99.95%; retention: keep feed entries for the latest 800 posts per user.
- **Social graph shape:** very skewed (a few accounts have millions of followers).

## 1.2 Estimates
```text
News feed (timeline)  463 writes/s   23.1 K reads/s avg   69.4 K peak reads/s   7.3 TB/yr raw   65.7 TB retained x3   3.3 Gbps peak egress   60 app servers   640.0 GB cache hot set
```
Posts and reads are modest: **463 posts/s and 23 K feed reads/s**. The number that decides the architecture is the one this table cannot show: **how many timeline entries one post creates** (the fan-out), which depends on the *follower distribution*. The lab measures it.

## 1.3 API and data model
```text
POST /v1/posts {text, media_ids[]}                    → 201 {post_id}
POST /v1/follow/{user_id}   DELETE /v1/follow/{user_id}
GET  /v1/feed?cursor=<opaque>&limit=20                → {posts[], next_cursor}
GET  /v1/users/{id}/posts?cursor=&limit=20
```
- **Posts:** `posts(post_id, author_id, text, media, created_at)`, `post_id` a time-sortable ID (Snowflake/UUIDv7, Chapter 10) so ordering by ID ≈ ordering by time. Sharded by `post_id` (or author) in a KV/wide-column store; content immutable except deletes.
- **Graph:** two adjacency lists: `followers(user → follower_ids)` (needed for fan-out) and `following(user → followee_ids)` (needed for pull), sharded by user.
- **Timelines:** `timeline(user_id) → list of post_ids` (just IDs, ~8 B each, newest first, capped at 800) in an in-memory store (Redis lists/sorted sets). Posts are **hydrated** by multi-get from a post cache.
- **Pagination:** cursor = `(created_at or post_id)` of the last item seen, never page numbers (feeds change while you scroll).

## 1.4 The central question: when to do the work?

| Strategy | Write path | Read path | Cost profile |
|---|---|---|---|
| **Fan-out on read (pull)** | Store the post once | For each followee fetch recent posts and merge | Reads are expensive (~200 lookups per feed load) |
| **Fan-out on write (push)** | Insert the post id into every follower's timeline | Read one precomputed list | Writes are expensive, especially for popular authors |
| **Hybrid** | Push for normal authors; do **not** push for "celebrities" | Read the user's timeline **plus** merge recent posts of followed celebrities | Balanced |

**Measured (Lab 1).** A synthetic graph of 1 M users (scaled ×200 to 200 M DAU) with a heavy-tailed follower distribution, ordinary accounts posting 0.2 posts/day and huge accounts (>100 K followers) 3.2/day:
```text
== Social graph: 1,000,000 users; followers per user: mean 200, median 76, p99 1750, p99.99 63200, max 500000; the top 1% of accounts own 34% of all follow edges ==
posts/day: 200,165 (x200 scale = 40,033,000/day = 463 posts/s); feed reads/day at 10 per user: 2,000,000,000 (23,148/s)

strategy                        timeline inserts/day   inserts/s (avg)   avg lookups per feed read   feed reads/s -> backend lookups/s   largest single fan-out   time to fan it out @ 100K inserts/s
pure pull (fan-out on read)                      0                0                  200.0                 23,148 ->        4,629,630                    0              0.0 s
hybrid: push if followers <= 1K      4,747,334,440           54,946                   82.3                 23,148 ->        1,905,478                1,000              0.0 s
hybrid: push if followers <= 10K      6,491,457,520           75,133                   38.7                 23,148 ->          896,147                9,978              0.1 s
hybrid: push if followers <= 100K      7,516,624,200           86,998                   13.1                 23,148 ->          302,880               99,078              1.0 s
pure push (fan-out on write)        15,250,637,640          176,512                    1.0                 23,148 ->           23,148              500,000              5.0 s

timeline storage (push side): 200M users x 800 entries x 8 B (post id) = 1280 GB in an in-memory store; a push-only design also pays one insert per follower per post even for users who never log in (inactive users should be skipped or lazily rebuilt).
```
**What the numbers say**
1. **The distribution is brutal.** Median followers 76, mean 200, p99 1,750, maximum 500 K; the **top 1% of accounts own 34% of all follow edges**. Averages hide the problem.
2. **Pure push costs 15.25 B timeline inserts/day (176 K/s)**, and the largest single post fans out to **500 K inserts (5 s at 100 K inserts/s)**. Note that a naive average-based estimate (40 M posts × 200 followers = 8 B inserts) is **about 2× too low**, because popular accounts have more followers *and* post more.
3. **Pure pull costs 200 lookups per feed load** = **4.6 M backend lookups/s** at 23 K feed loads/s, with tail latency set by the slowest of 200 fetches.
4. **The hybrid wins by moving the threshold.** Pushing only authors with ≤ 100 K followers **cuts push volume to 7.5 B inserts/day (87 K/s, a 51% reduction)**, bounds the worst fan-out to 99 K inserts (1 s), and leaves each feed load with only **13 lookups** (the user's timeline plus ~12 celebrity accounts they follow): ~0.3 M lookups/s. A 10 K threshold gives 75 K inserts/s but 38.7 lookups per read; a 1 K threshold gives 55 K inserts/s but 82 lookups per read. **The threshold is a dial between write cost and read cost**, set by measurement and revisited as the graph changes.
5. **Storage for timelines:** 200 M users × 800 entries × 8 B = **1.28 TB** in memory if everyone is stored; only recently-active users need a materialized timeline (≈ 640 GB in the estimate).

## 1.5 Architecture (evolution)
| Ver. | Design | Trigger |
|---|---|---|
| **v1** | Single DB; feed query = `SELECT … WHERE author IN (followees) ORDER BY id DESC LIMIT 20` | Slow with hundreds of followees; DB overloaded by 23 K feed reads/s |
| **v2** | Cache hot posts and followee lists; read replicas | Joins and 200 lookups per read still dominate |
| **v3** | **Precomputed timelines** (push) in an in-memory store; fan-out workers fed by a queue | Celebrity posts create hundreds of thousands of inserts; push cost 15 B/day |
| **v4** | **Hybrid**: skip push for authors above the threshold; merge their recent posts at read time | Inactive followers waste writes |
| **v5** | **Push only to active followers** (seen in the last 7-30 days); rebuild cold timelines lazily (pull) on return | Memory cost of 200 M timelines |
| **v6** | Ranking layer (candidate generation from timelines + celebrity posts, then a scoring model), feed cache, multi-region | Engagement goals, global latency |

```text
 Post ─▶ Post service ─▶ post store (KV) ─▶ event "post_created" ─▶ queue (partitioned by author)
                                                                 │
                       fan-out workers: read followers(author) (paged) ─▶ batch-insert post_id into each ACTIVE follower's timeline (Redis)  [skip if author is a "celebrity"]
 Feed read ─▶ Feed service ─▶ timeline(user) ─┐
                              celebrity merge: for each followed celebrity, fetch their recent posts from a per-author cache ─┤─▶ merge by post_id ─▶ rank/filter ─▶ hydrate ─▶ response
```

## 1.6 Deep dives
**Fan-out workers.** A post event is partitioned by author; the worker pages through followers (they can be millions) and writes **batches** of timeline inserts grouped by timeline shard (one network round trip per shard, not per follower). Inserts are **idempotent** (a set keyed by `post_id`; re-delivery is harmless). Prioritize active followers first so most people see the post immediately while the long tail of the fan-out completes. Track **fan-out lag** (time from post to last insert) as an SLI (Chapter 21); autoscale workers on queue age.

**Reading the hybrid.** `feed(user) = merge(timeline[user], recent_posts[c] for each celebrity c followed)`. The celebrity posts come from a small, hot **per-author recent-posts cache** (the same entries serve millions of readers, so cache hit ratios are very high). The merge is a k-way merge on `post_id`; with ~12 celebrity sources it costs microseconds.

**Edge cases that need decisions.**
- **New follow:** backfill the last N posts of the followee into your timeline (or let the hybrid read path fetch them).
- **Unfollow / block / delete / privacy changes:** timelines may contain stale post IDs; **filter at read time** (check post existence and visibility during hydration) and clean up asynchronously.
- **Read-your-writes:** insert your own post into your own timeline synchronously so you see it immediately.
- **Ordering:** time-sortable IDs make "newest first" a sort on the ID. A ranked feed reorders a candidate window (e.g., top 200 by time) with a model; keep the model off the critical path with a timeout and a chronological fallback.
- **Cold timeline / cache loss:** a missing timeline is rebuilt by the pull path (fetch recent posts from followees), so the push store is a *cache*, not the source of truth. This also makes the system resilient to Redis node loss.

**Hot keys.** A celebrity's follower list and recent-posts cache are very hot: replicate them across cache nodes and add a short-TTL local cache in the feed servers (Chapter 8).

## 1.7 Failure modes and hardening
| Failure | Handling |
|---|---|
| Fan-out worker crash mid-batch | Event redelivered; idempotent inserts |
| Fan-out backlog after a viral post | Backpressure; prioritize active followers; shed inactive; alert on lag |
| Timeline shard lost | Rebuild on demand via pull; replicate hot shards |
| Celebrity post storm | Skipped from push by design; per-author cache absorbs reads |
| Duplicate posts in feed | Dedupe by `post_id` during merge |
| Deleted/blocked content still visible | Read-time filtering plus async cleanup |
| Ranking service slow | Timeout + chronological fallback |

## 1.8 Trade-offs
| Decision | Choice | Cost | Alternative |
|---|---|---|---|
| When to compute | Hybrid push/pull with threshold ≈ 100 K | Two code paths, threshold tuning | Pure push (simplest reads, 2× writes and 5 s worst fan-out); pure pull (simplest writes, 4.6 M lookups/s) |
| Timeline content | Post IDs only | Hydration lookups | Full posts (fast reads, huge storage) |
| Who gets pushed | Active followers | Cold users pay a slow first load | Everyone (memory + wasted writes) |
| Consistency | Eventual (seconds) | A follower may see a post late | Synchronous fan-out (unacceptable for celebrities) |

## 1.9 Follow-ups
1. **How do you rank the feed?** Candidate generation from timeline + celebrity posts + recommendations → feature fetch → model scoring → diversity/ads blending; precompute where possible; budget ~100 ms.
2. **Ads?** An ad server injects at ranked slots; frequency capping per user (Chapter 25's counters).
3. **"Mute" and "close friends" lists?** Filter at read time; close-friends audiences make fan-out per audience.
4. **Real-time updates ("new posts" banner)?** WebSocket/long-poll notification carrying only a counter.
5. **Multi-region?** Home each user's timeline in their region; replicate posts globally; celebrity posts via global cache.

*What graders look for:* measuring or at least reasoning about the follower distribution, the hybrid with a rationale for the threshold, idempotent fan-out, cursor pagination, handling of deletes and cold timelines.

---

# Problem 2: Notification System

> **Problem statement.** Design a system that sends notifications (push, SMS, email, in-app) to users on behalf of many internal services: OTP codes, transactional messages, and marketing campaigns.

## 2.1 Requirements
- **Functional:** accept notification requests (API or event-triggered), render from templates with localization, respect **user preferences, opt-outs, and quiet hours**, send over the right channels through external providers (APNs/FCM, SMS gateways, email providers), retry, track delivery status, support scheduling and bulk campaigns.
- **Non-functional:** ~600 M notifications/day (avg 6.9 K/s, peak ~21 K/s, with **campaign bursts of millions**); **OTP latency p99 < 5 s**; no duplicate sends (a duplicate OTP is confusing, a duplicate charge alert is alarming); at-least-once with dedupe; 99.95% availability; providers fail and rate-limit; auditability.

## 2.2 Estimates
```text
Notification system  6.9 K writes/s   6.9 K reads/s avg   20.8 K peak reads/s   65.7 TB/yr raw   19.7 TB retained x3 (36 days)   14 app servers   30.0 GB cache hot set (one day of dedupe keys)
```
Steady-state volume is easy. The difficulty is **burstiness and priority**: a marketing campaign of 2 M messages must not delay a login OTP.

## 2.3 API and data model
```text
POST /v1/notifications {idempotency_key, user_id | segment_id, type, priority, channel_hints[], template_id, params, send_at?}
→ 202 {notification_id}
GET  /v1/notifications/{id}            → status per channel (QUEUED, SENT, DELIVERED, FAILED, SUPPRESSED)
PUT  /v1/users/{id}/preferences        → channel opt-ins, categories, quiet hours, locale
```
Tables: `preferences(user, category, channel, enabled, quiet_hours, tz)`, `device_tokens(user, platform, token, last_seen)`, `notifications(id, idempotency_key, user, type, priority, status, created)`, `deliveries(notification_id, channel, provider, status, attempts, provider_msg_id, updated)`, `templates(id, locale, version, body)`.

## 2.4 Architecture
```text
 Services/events ─▶ Notification API (authn, validate, idempotency check) ─▶ Policy stage: preferences, opt-out, quiet hours, frequency caps, dedupe
        ─▶ Render stage: template + locale + params ─▶ Priority queues (per channel):  [OTP] [transactional] [bulk/marketing]
        ─▶ Channel workers (push | SMS | email), each with per-provider rate limiter + circuit breaker + provider failover
        ─▶ Providers (APNs/FCM, SMS gateways, email) ─▶ delivery receipts/webhooks ─▶ status store ─▶ analytics
 Scheduler (delayed sends, timezone-based) ─▶ re-enqueues at send time  ·  Campaign service (segment → throttled batch enqueue)  ·  DLQ for poison/failed
```
**Evolution:** v1 a function that calls the provider inline (blocks the caller, loses messages on failure); v2 queue + workers (decouple); v3 **priority queues** (OTP vs bulk); v4 per-provider limiters, retries, failover; v5 preference/policy service and dedupe; v6 scheduler and campaign service; v7 multi-region, analytics.

## 2.5 Deep dive: priorities and isolation, measured
A provider accepts **5,000 messages/s**. Steady load: 200 OTP/s + 300 transactional/s. At t=10 s marketing enqueues a 2 M-message campaign.
```text
== Notification priorities. Provider capacity 5,000 msg/s. Steady load: 200 OTP codes/s + 300 transactional msgs/s. At t=10 s marketing enqueues a 2,000,000-message campaign. ==
policy                                                     OTP wait during campaign: median / worst (s)   transactional worst (s)   campaign finishes after (s)   spare capacity during campaign
single FIFO queue for everything                               199.30 /   399.10                                 399.10                       444                     none (queue saturated)
strict priority: OTP > transactional > marketing                 0.00 /     0.00                                   0.00                       444                     none until the campaign ends
strict priority + marketing rate-capped at 2,000/s               0.00 /     0.00                                   0.00                       999                     2,500 msg/s
weighted shares 70/20/10, work-conserving                        0.00 /     0.00                                   0.00                       444                     none until the campaign ends
takeaway: a login OTP that waits 6.5 minutes is a failed login. Priority classes protect latency-critical traffic; a rate cap on bulk traffic also keeps headroom for retries and bursts of high-priority messages, at the price of a campaign that takes 2.2x longer.
```
- With **one FIFO queue**, an OTP sent during the campaign waits a **median of 199 s and up to 399 s (6.7 minutes)**: for a login code that is a failure. The campaign itself completes after 444 s.
- **Strict priority queues** (OTP > transactional > marketing) keep OTP and transactional waits at **0** and the campaign still completes in 444 s (it only uses leftover capacity). Weighted shares (70/20/10) behave the same here because high-priority load is far below its share; they matter when high-priority traffic itself can saturate.
- **Capping the bulk rate at 2,000/s** also leaves **2,500 msg/s of headroom** for retries and surges of high-priority traffic, at the price of a campaign that takes **999 s (2.2× longer)**. That is usually the right trade for marketing.
**Lesson:** separate queues *and separate worker pools / provider credentials* per priority class (a **bulkhead**, Chapter 18), so a stuck bulk sender cannot starve OTPs.

## 2.6 Other deep dives
**Exactly-once is impossible, so design for at-least-once + dedupe.** The provider might accept a message but the response is lost; a retry would send twice. Mitigations: (1) a durable **idempotency key** per `(user, event, channel)` checked before sending (Redis/DB with TTL ≈ 24 h; 600 M keys × 50 B ≈ 30 GB, the estimate's cache row); (2) pass the provider's own idempotency token when supported; (3) record `provider_msg_id` and reconcile with delivery receipts; (4) accept the rare duplicate for low-stakes messages and design OTP messages to be idempotent (same code resent).

**Policy before send.** Check opt-outs (legally required for email/SMS), category preferences, **quiet hours in the user's time zone**, **frequency caps** (e.g., ≤ 3 marketing messages/day via counters, Chapter 25), and channel fallback (push → SMS for OTP if push fails within 10 s). Evaluate policy again at send time for delayed messages (preferences change).

**Provider failures.** Per-provider rate limiters (respect their quotas), **retries with exponential backoff and jitter**, circuit breakers, automatic **failover to a secondary provider** (SMS gateways differ by country), and a dead-letter queue for permanent failures. Handle **invalid tokens** (APNs returns "unregistered"): delete the device token. Track provider success rates per country/carrier and route adaptively.

**Scheduling.** Delayed/timezone sends ("9:00 local") use a time-bucketed schedule (a sorted set or partitioned table keyed by minute) scanned by a scheduler that enqueues due items; at 1 M scheduled messages per minute, shard the scanners by bucket and rate-limit the enqueue so the "9:00" wave doesn't become an unplanned spike.

**Campaigns.** A campaign resolves a segment (a query over users), expands it in **chunks** (e.g., 10 K users per chunk, each chunk a queue item), and enqueues them at the allowed rate; keep campaign state (progress, pause/cancel) so operators can stop a bad send. Per-recipient policy still applies.

**Observability.** SLIs: end-to-end latency by priority class, provider error rates, **age of the oldest message per queue** (the best backlog signal), dedupe/suppression counts, delivery rate; alert on OTP p99 > 5 s (Chapter 21).

## 2.7 Failure modes and hardening
| Failure | Handling |
|---|---|
| Provider outage | Circuit breaker → failover provider; queue holds; OTP fallback channel |
| Duplicate sends | Idempotency keys, receipts, provider tokens |
| Poison message (bad template/params) | Validate at ingest; DLQ after N attempts; alert |
| Queue backlog after campaign | Separate pools; bulk rate cap; autoscale workers on queue age |
| Thundering "9:00" wave | Jittered schedule, rate-limited enqueue |
| PII in logs | Redaction; store minimal payloads (Chapter 22) |
| User spam | Frequency caps, per-service quotas, abuse review |

## 2.8 Trade-offs
Separate queues per priority (isolation, more operational pieces) vs one queue with priority fields (simple, head-of-line effects); render at enqueue (simple, stale content) vs at send (fresh, more work at send time); push vs SMS for OTP (cost vs deliverability; use fallback); in-house gateway vs buying (control vs undifferentiated work).

## 2.9 Follow-ups
1. **Millions of recipients in one campaign?** Chunked fan-out with the 2 M/999 s numbers above; pause/resume.
2. **How do you guarantee an OTP is delivered?** You can't; you provide fast delivery, fallback channels, and retry/resend UX.
3. **In-app notification center?** A per-user inbox store (Chapter 26 feed-like) with unread counts.
4. **A/B tests of templates?** Variant assignment at the policy stage; log exposure.
5. **GDPR/CAN-SPAM?** Consent records, unsubscribe links, retention limits, audit logs.

*What graders look for:* priority isolation with numbers, idempotency/dedupe reasoning, provider failure handling, and honest treatment of "exactly once."

---

# Problem 3: Photo Sharing (Instagram)

> **Problem statement.** Design a photo-sharing service: users upload photos, follow others, and view photos in a feed and on profiles.

## 3.1 Requirements
- **Functional:** upload photos (up to ~20 MB); view photos at appropriate sizes; profiles; follow (feed from Problem 1); delete; basic moderation. Out of scope: video, stories, filters, search, likes/comments (mention only).
- **Non-functional:** 100 M new photos/day; 5 B photo views/day; **photo visible within seconds of upload** (at least a thumbnail); views p99 < 200 ms globally; durability (never lose a photo); 99.95% availability; retention indefinite (estimate five years).

## 3.2 Estimates
```text
Photo sharing  1.2 K writes/s   57.9 K reads/s avg   173.6 K peak reads/s   127.8 PB/yr raw   1.9 EB retained x3 (5 yrs)   416.7 Gbps peak egress   97.22 Gbps peak ingress   591 app servers   20.0 GB metadata cache
```
(3 MB original + 0.5 MB of derived renditions per photo; 300 KB average served per view.)
- This is a **bandwidth and storage problem**, not a QPS problem: **~417 Gbit/s of peak egress**, ~97 Gbit/s of peak ingress, **128 PB of new data per year**.
- **Retention at 3× replication would be 1.9 EB**; with **erasure coding at ~1.4× it is ~0.9 EB**. With an illustrative object-storage price of $0.02/GB-month, 3× replication costs ≈ **$38 M/month**, erasure coding ≈ **$18 M/month**, and moving ~80% of old photos to an infrequent-access tier (~$0.0125/GB-month) ≈ **$12.5 M/month**. *Storage layout is a cost decision worth hundreds of millions of dollars over the life of the product.* (Prices are illustrative; check current rates.)
- Egress cannot be served from origin: at a 97% CDN hit ratio the origin sees ~**12.5 Gbit/s** instead of 417.

## 3.3 API and data model
```text
POST /v1/photos/uploads {size, content_type, sha256?}  → {photo_id, upload_url(s)}      (direct-to-storage, Problem 4 of Ch 25)
POST /v1/photos/{id}/complete                          → 202   (processing starts; photo is PENDING)
GET  /v1/photos/{id}                                   → {urls: {thumb, small, medium, large}, owner, caption, created}
DELETE /v1/photos/{id}
```
- **Metadata** (`photos(photo_id, owner_id, caption, created_at, status, renditions{size→key}, width, height, hash)`), sharded by `photo_id` (or by owner for profile queries with a secondary index). Small: ~1 KB per photo → ~36 TB/year. The owner's profile grid is `(owner_id, created_at DESC)`.
- **Blobs** in object storage under content-addressed or ID-based keys: `orig/<id>`, `r/<id>/<size>.<fmt>`. Immutable, therefore CDN-friendly: a changed photo gets a new key.

## 3.4 Architecture
```text
 Mobile ─(1) get signed URL─▶ API ─▶ metadata (PENDING)                                       ┌──▶ thumbnail (150px) ─┐
 Mobile ─(2) PUT original────▶ Object store (originals, quarantine prefix) ─event─▶ queue ───▶ │ resize workers        │──▶ renditions store ─▶ metadata READY (thumb first)
 Mobile ─(3) complete────────▶ API                                                           └──▶ 320/640/1080 × (WebP/AVIF/JPEG)  moderation scan, EXIF strip, perceptual hash
 Viewer ─▶ CDN (multi-tier: edge → regional → origin shield) ─▶ renditions store          ·   Feed (Problem 1) ◀── "photo_ready" event
```
**Evolution:** v1 uploads through the app server into local disk; v2 object storage + pre-signed uploads (disks fill; app tier saturated by bytes); v3 CDN for reads (egress); v4 **async processing pipeline** producing renditions (upload latency and device diversity); v5 tiered storage + erasure coding (cost); v6 multi-region replication and origin shielding; v7 on-demand transforms at the edge for rare sizes.

## 3.5 Deep dives
**The processing pipeline.** `PENDING → THUMB_READY → READY → (FLAGGED | DELETED)`. Workers are idempotent per `(photo_id, rendition)`; the **thumbnail is produced first** so the uploader sees their photo within a second or two while larger renditions follow. Steps: validate (decode safely: limit dimensions, defend against decompression bombs), **strip EXIF** (GPS location is a privacy leak) after extracting orientation, generate renditions in modern formats (WebP/AVIF) with JPEG fallback, compute a **perceptual hash** (dedupe/abuse detection), run moderation classifiers (CSAM hash matching, nudity/violence), then publish a `photo_ready` event consumed by the feed. Failures retry with backoff; poison images go to a DLQ.

**Precompute renditions vs transform on demand.** Precomputing 4 sizes × 2 formats costs ~0.5 MB per 3 MB original (**17% extra storage**) and zero latency at read time. Transforming on the fly at the CDN edge saves storage but adds CPU spikes and cold-start latency for each new size/format. Typical hybrid: precompute the 4-5 common sizes; generate rare sizes on demand and cache the result.

**CDN design.** Use **immutable URLs** (photo id + size + content hash) with year-long TTLs, so no invalidation is needed except on delete (purge by key or version). A tiered CDN (edge → regional → origin shield) collapses misses: millions of edges asking for a new viral photo become one origin fetch. **Private accounts** need authorization: short-lived **signed URLs** or signed cookies; cached objects must be keyed so private content never leaks across users (Chapter 22). Choose CDN TTL/size based on measured hit ratio: each point of hit ratio at 417 Gbit/s peak is ~4 Gbit/s of origin traffic.

**Storage tiering and erasure coding.** Photos follow a steep access curve (most views are within days of upload). Policy: first 30 days on standard storage (fast, replicated across AZs), then erasure-coded infrequent-access, with lifecycle automation. Erasure coding (e.g., 10+4 → 1.4×) tolerates the loss of any 4 fragments at less than half the 3× replication overhead, at the price of more CPU and slower repair/reads for cold data (Chapter 11).

**Metadata scale.** 100 M photos/day = 36.5 B rows/year at ~1 KB: shard by `photo_id` (random/hashed) for even distribution; the profile page needs `(owner, time)` → a secondary index or a second table partitioned by owner; use Snowflake IDs so recency order is free.

**Uploads from mobile.** Resumable chunked uploads (Chapter 25 Problem 4), client-side compression/resizing to a max dimension before upload (saves ingress and storage), background upload with retry.

**Deletion and compliance.** Delete = mark metadata DELETED → async delete originals and all renditions → purge CDN → remove from feeds lazily; keep an audit record; legal holds and takedowns override.

**Duplicate detection / abuse.** Exact hash dedupe for re-uploads, perceptual hashes for near-duplicates and known-bad content matching.

## 3.6 Failure modes and hardening
| Failure | Handling |
|---|---|
| Resize worker backlog | Queue-age alerting, autoscale, prioritize thumbnails; user already sees the thumbnail |
| Corrupt/malicious image | Safe decoders, sandboxed workers, size/dimension limits, DLQ |
| CDN outage / regional failure | Multi-CDN or fallback to origin with rate limits; origin shield sized for fallback |
| Hot viral photo | Immutable + CDN + origin shield absorbs; single-flight at the shield |
| Storage node loss | Erasure-coded fragments across AZs; repair traffic throttled |
| Orphaned originals (upload without complete) | Lifecycle rule on the quarantine prefix |
| Wrong renditions after a bug | Versioned rendition keys; reprocess from originals (never delete originals before renditions are verified) |

## 3.7 Trade-offs
| Decision | Choice | Cost | Alternative |
|---|---|---|---|
| Durability | Erasure coding + tiering | CPU, slower cold reads, repair complexity | 3× replication (simple, ~2× the cost) |
| Renditions | Precompute common sizes | +17% storage, processing pipeline | On-demand edge transforms (less storage, latency spikes) |
| Upload path | Direct to storage | Less inspection at the edge | Via app tier (simple but bandwidth-bound) |
| Formats | WebP/AVIF + JPEG fallback | More renditions, client negotiation | JPEG only (simple, larger) |

## 3.8 Follow-ups
1. **Stories (24-hour photos)?** TTL-based keys and lifecycle deletion; higher write churn, aggressive cache TTLs.
2. **Video?** Transcoding to adaptive bitrate ladders (HLS/DASH), segment storage, much larger egress (Chapter 27).
3. **Image search / hashtags?** Async indexing of captions and ML-derived labels.
4. **Global users?** Regional upload endpoints, replicate originals across regions, CDN everywhere.
5. **Cost reduction levers?** Tiering, EC, smaller renditions/AVIF, deleting abandoned uploads, CDN hit-ratio tuning.

*What graders look for:* recognizing the bandwidth/storage scale, async pipeline with thumbnail-first, CDN and immutable keys, EC/tiering with numbers, privacy (EXIF) and moderation.

---

# Problem 4: File Sync (Dropbox / Google Drive)

> **Problem statement.** Design a service that keeps files synchronized across a user's devices and supports sharing, with efficient transfer of changes.

## 4.1 Requirements
- **Functional:** a client watches a local folder and syncs changes up; other devices receive and apply them; file versions (30 days); share files/folders; offline edits that sync later; conflict handling; selective sync.
- **Non-functional:** 50 M users; ~100 M file changes/day; files up to tens of GB; **sync latency of seconds** after a change; bandwidth-efficient (upload only what changed); integrity; no data loss; 99.95% availability; strong consistency *per user namespace* (a folder's view must not flip-flop).

## 4.2 Estimates
```text
File sync (metadata+chunks)  1.2 K writes/s   3.5 K reads/s avg   10.4 K peak reads/s   36.5 PB/yr raw   328.5 PB retained x3 (3 yrs)   83.3 Gbps peak egress   27.78 Gbps peak ingress   100.0 GB metadata cache
```
(Assuming ~1 MB of changed data per change after dedup/delta.) Metadata operations are modest; **bytes** dominate. The question is how few bytes you can move per edit.

## 4.3 Core idea: split files into content-addressed chunks
```text
 file ─▶ chunker ─▶ [c1][c2][c3]…  each chunk identified by hash(content)=id
 file version = manifest: ordered list of chunk ids + metadata (size, mtime, mode)
 upload: send manifest hashes → server replies "I lack c2, c5" → client uploads only those → commit new version (manifest)
```
This gives **delta sync**, **deduplication** (same chunk stored once, even across files and users), cheap **versioning** (a version is a manifest; unchanged chunks are shared), **resumability** (chunks are idempotent), and **integrity** (hash = checksum).

## 4.4 Chunking strategy, measured
**Fixed-size chunks** (e.g., 64 KB) are simple, but inserting even one byte near the start shifts every later boundary, so *every following chunk hashes differently*. **Content-defined chunking (CDC)** places boundaries where a rolling hash of the last few bytes matches a pattern, so boundaries move with the content and an insertion changes only the chunk around it.

20 MB file; fixed 64 KB (306 chunks) vs CDC with a Gear rolling hash (min 16 KB, max 128 KB, 422 chunks, average 46 KB):
```text
== File sync: a 20 MB file. Fixed 64 KB chunks: 306 chunks. Content-defined chunks (Gear hash, min 16 KB, max 128 KB): 422 chunks, average 46 KB. ==
edit made to the file                          fixed-size chunking: data to upload                       content-defined chunking: data to upload
insert 100 bytes at the start                  20,000,100 B (100.00%)  306 of 306 chunks                    50,559 B ( 0.25%)    1 of 422 chunks
insert 100 bytes in the middle                 10,038,628 B (50.19%)  154 of 306 chunks                     86,827 B ( 0.43%)    1 of 422 chunks
overwrite 100 bytes in the middle (same length)     65,536 B ( 0.33%)    1 of 306 chunks                     86,727 B ( 0.43%)    1 of 422 chunks
append 1 KB at the end                             12,544 B ( 0.06%)    1 of 306 chunks                     16,358 B ( 0.08%)    1 of 422 chunks
delete 10 KB near the 5 MB mark                15,009,024 B (75.08%)  230 of 306 chunks                     33,007 B ( 0.17%)    1 of 421 chunks

metadata cost for a 1 GB file: fixed 64 KB -> 15,258 chunks; CDC avg ~46 KB -> ~21,100 chunks; at ~80 B per chunk entry that is 1.2 MB vs 1.7 MB per file version
```
- **Inserting 100 bytes at the start** makes fixed chunking re-upload **100% of the file (20 MB)** but CDC only **one chunk (0.25%, 50 KB)**: 400× less.
- **Inserting in the middle:** fixed 50.2% vs CDC 0.43%. **Deleting 10 KB:** fixed 75.1% (everything after shifts) vs CDC 0.17%.
- In-place overwrites and appends are fine for both (a single chunk), so **CDC costs nothing when fixed chunking would have worked and saves orders of magnitude when it would not**.
- The price: **~38% more chunks** (422 vs 306) for the same file in this configuration, i.e., more metadata (≈1.7 MB vs 1.2 MB of manifest per GB at ~80 B per chunk entry) and more per-chunk requests. Chunk-size choice trades dedup/delta granularity against metadata and request overhead (typical average 64 KB-4 MB; Dropbox's original blocks were 4 MB `[Public]`).

## 4.5 Architecture
```text
 Client sync engine: file watcher ─▶ chunker (CDC) ─▶ hasher ─▶ ┐
                                                                ▼
  (1) POST /commit-prepare {path, base_version, manifest}  ─▶ Metadata service ─▶ replies missing chunk ids
  (2) PUT chunks ─▶ Block service ─▶ Block store (content-addressed object store, EC, cross-region)
  (3) POST /commit {path, base_version, manifest}  ─▶ Metadata service: atomic compare-and-set on base_version → new version; append to the namespace JOURNAL
  (4) Notification service (long-poll/WebSocket) wakes other devices: "namespace N changed (cursor 9137)"
  (5) Other device: GET /journal?after=cursor ─▶ list of changes ─▶ fetch missing chunks ─▶ assemble file ─▶ atomically replace local file
```
- **Metadata store** (namespaces, directory tree, file versions, manifests, sharing ACLs): relational, **sharded by namespace/user** so that one user's operations are single-shard and strongly consistent (Dropbox famously used sharded MySQL; Spanner-style globally consistent stores are an alternative) `[Public]`. Moves across namespaces are rare and implemented as copy + delete or two-phase coordination.
- **Journal:** an append-only, per-namespace log of changes with a monotonically increasing cursor. Devices sync by remembering their cursor (the same cursor idea as the chat service). This avoids scanning the tree and makes sync **incremental, ordered, and idempotent**.
- **Block store:** key = chunk hash, value = (compressed, encrypted) bytes; erasure-coded; hot chunks cached.
- **Notifications:** devices hold a long-poll/WebSocket to a notification service that only says *"something changed"* (the device then pulls the journal). Small messages, millions of connections, same engineering as chat gateways.

**Evolution:** v1 whole-file upload on each save; v2 fixed-size chunks + dedupe; v3 **CDC** (insert/shift workloads); v4 journal + cursors + notification channel (polling at scale kills the metadata DB); v5 sharded metadata by namespace; v6 cross-region block replication, LAN sync, selective sync; v7 sharing/ACL service and team namespaces.

## 4.6 Deep dives
**Conflicts.** Two devices edit the same file from the same base version. Commits are **compare-and-set on `base_version`**: the first wins; the second gets "conflict" and the client saves its copy as `file (conflicted copy of Laptop 2026-10-04).ext` (or merges for formats it understands). Never rely on mtimes or wall-clock ordering (Chapter 14). For truly collaborative documents, use real-time CRDT/OT editing instead of file-level sync (Chapter 20).

**Commit atomicity and crash safety.** A commit is only valid when all chunks exist. Chunk upload and manifest commit are separate steps, so a crash between them leaves **orphan chunks** (garbage-collected after a grace period via mark-and-sweep over manifests, or reference counts). The commit call is **idempotent** (keyed by `(path, base_version, manifest_hash)`).

**Dedup and privacy.** Global dedup (the server says "I already have this chunk, skip the upload") saves enormous bandwidth but creates a **side channel**: an attacker who guesses a file's hash learns whether anyone stored it. Mitigations: require proof of possession (hash of server-chosen random ranges), per-account/per-team dedup scopes, or convergent encryption trade-offs. Client-side encryption defeats cross-user dedup (a deliberate security choice).

**Garbage collection of chunks.** Versions expire (30 days) and files are deleted; chunks shared across versions/files/users need **reference counting or periodic mark-and-sweep** with a safety delay (never delete a chunk that an in-flight commit may reference).

**Large files.** Resumable chunk uploads (the lab of Chapter 25), parallel chunk transfers, streaming hash computation, memory-bounded chunking; selective sync for huge folders.

**Metadata hot spots.** A shared team folder with thousands of active members is a hot namespace: its journal and ACLs are a single-shard write stream; mitigate with batching commits, read replicas for listing, and caching ACLs; split very large shared namespaces by subtree if necessary.

**Local file system quirks.** Case-insensitive names, renames vs delete+create detection (use inode/content hash), partial writes (wait for quiescence/debounce events 500 ms), symlinks, permissions, Unicode normalization.

**Bandwidth etiquette.** Throttle, background priority, LAN peer sync between devices on the same network, compression (skip already compressed media).

## 4.7 Failure modes and hardening
| Failure | Handling |
|---|---|
| Client crash mid-upload | Resume: chunks are content-addressed and idempotent; re-send manifest |
| Orphan chunks | Grace-period GC with mark-and-sweep |
| Lost notification | Devices also poll with their cursor periodically (safety net) |
| Two devices edit offline | Conflicted copy; base_version CAS |
| Chunk corruption | Hash verification on read/download; repair from EC fragments |
| Namespace shard failure | Replication/failover (Chapter 9); sync resumes by cursor |
| Mass deletion by bug/malware | Versioning, trash retention, rollback of namespace to a timestamp |

## 4.8 Trade-offs
| Decision | Choice | Cost | Alternative |
|---|---|---|---|
| Chunking | CDC (avg ~64 KB-1 MB) | CPU at the client, ~40% more chunk metadata | Fixed 4 MB (simple; terrible for insertions) |
| Consistency | Per-namespace strong, cross-namespace eventual | Cross-namespace moves are awkward | Global transactions (costly) |
| Dedup scope | Per-account or proof-of-possession global | Less savings | Naive global dedup (side channel) |
| Notifications | "Changed" ping + pull by cursor | Extra round trip | Push full diffs (fan-out cost, lost-update risks) |

## 4.9 Follow-ups
1. **Real-time co-editing?** Separate OT/CRDT service (Chapter 20) for supported document types.
2. **Selective sync / "online-only" files?** Placeholder files hydrated on access via the OS file-provider APIs.
3. **File locking (Office)?** Advisory locks with leases; conflicts otherwise produce copies.
4. **How do you share a folder with 1,000 people?** ACL graph (ReBAC), per-member namespace mounts referencing the shared namespace.
5. **Quota and billing?** Count logical bytes per account vs physical deduped bytes; decide policy.

*What graders look for:* chunk/manifest model, CDC vs fixed with the shift problem, journal+cursor sync, CAS commits and conflicted copies, GC of chunks, dedup privacy.

---

# Problem 5: Web Crawler

> **Problem statement.** Design a web crawler that downloads billions of pages for a search engine, politely and at scale.

## 5.1 Requirements
- **Functional:** start from seed URLs; fetch pages; extract links; avoid re-crawling the same URL; respect `robots.txt` and crawl delays; store page content for indexing; re-crawl pages periodically (freshness); prioritize important pages.
- **Non-functional:** ~1 B pages/month (**382 pages/s**); average page 100 KB; **politeness** (limit load per host); robust to bad servers, spider traps, huge pages; extensible (new content types); fault tolerant (restart without losing progress); distributed.

## 5.2 Estimates
```text
Web crawler (pages)  382 pages/s   1.2 PB/yr raw (100 KB x 33 M/day)   3.6 PB retained x3   0.9 Gbps peak egress/ingress   39 fetcher servers (at 100 pages/s each, 60% util)
```
At 382 pages/s the raw numbers are modest (≈1 Gbit/s, ~1 PB/year). The difficulty is not throughput on average but **politeness and scheduling**: you cannot fetch faster than a site tolerates. The real-web Zipf shape of hosts decides what is achievable.

## 5.3 Architecture
```text
 Seeds ─▶ URL FRONTIER ─▶ Fetchers (async I/O) ─▶ Content store (object store / HDFS) ─▶ Parser ─▶ Link extractor ─▶ Normalize + filter
              ▲   front queues = priority          │ DNS cache · robots.txt cache         │ near-duplicate detector (SimHash)     │
              │   back queues  = one per host      ▼                                      ▼                                       │
              └──────────── URL-seen filter (Bloom + persistent set) ◀─────────────────────────────── new URLs ──────────────────┘
 Re-crawl scheduler (change-rate estimates) re-injects URLs · Metrics/monitoring · Checkpointing of frontier state
```

## 5.4 The frontier and politeness, measured
**Setup.** 2,000 hosts and ~3 M URLs distributed Zipf-style across hosts (the largest host has 367 K URLs; the median host 366). Politeness: **at most one fetch per second per host**; a fetch takes 0.2 s; 600 s simulated.
```text
== Crawler frontier. 2,000 hosts, 2,999,019 URLs (Zipf 1.0 per host: biggest host has 366,821 URLs, median host 366). Politeness: at most 1 fetch/s per host. Fetch takes 0.2 s. 600 s simulated. ==
theoretical maximum (every host fetched at exactly 1/s until its URLs run out): 801,293 pages in 600 s = 1335 pages/s; only 611 hosts have enough URLs to stay busy the whole time

frontier design                                            fetcher threads   pages crawled in 600 s   pages/s   % of theoretical max
one global FIFO queue; fetcher blocks on a busy host            100                   5,631               9          0.7%
one global FIFO queue; fetcher blocks on a busy host            500                   8,561              14          1.1%
one global FIFO queue; fetcher blocks on a busy host           5000                  23,628              39          2.9%
per-host queues + ready-time scheduler                          500                 800,909            1335        100.0%

URL-seen set for 100 billion URLs: exact hash set (8-byte fingerprints) = 800 GB; Bloom filter at 1% false positives (9.6 bits/URL) = 120 GB; at 0.1% (14.4 bits/URL) = 180 GB
```
**What this teaches**
1. **The politeness limit sets the ceiling, not threads.** Theoretical maximum = 801 K pages in 600 s (1,335 pages/s), and **only 611 hosts have enough URLs to stay busy the whole time**; the long tail of small hosts runs dry quickly.
2. **A single global FIFO queue with blocking fetchers is catastrophic: 9 pages/s with 100 threads, 14/s with 500, 39/s with 5,000 (0.7%-2.9% of the maximum).** A fetcher that dequeues a URL for a host that was just fetched must wait, holding its thread and its place; popular hosts (12% of all URLs) fill the head of the queue, so most fetchers sit idle waiting for a handful of hosts. Adding 50× more threads recovers only 4×.
3. **Per-host queues plus a ready-time scheduler reach 100% of the maximum (1,335 pages/s) with only 500 fetchers**: a fetcher never waits; the scheduler hands it a URL from *any host that is allowed to be fetched right now*.
4. **Memory for the "have I seen this URL" set:** for 100 B URLs an exact set of 8-byte fingerprints is **800 GB**; a Bloom filter at 1% false positives is **120 GB** (0.1% → 180 GB). A false positive means a genuinely new URL is skipped (acceptable at ~1% for a crawler; not acceptable for correctness-critical dedupe); a false negative never occurs.

### The Mercator-style frontier
- **Front queues (priority):** F queues by priority (PageRank-ish importance, change rate, depth); a biased selector picks a front queue.
- **Back queues (politeness):** one queue per host (maintained as a mapping host → queue), each with a **next-allowed-time**; a min-heap of `(next_allowed_time, host)` yields the next fetchable host. After fetching from host *h*, set `next_allowed = now + max(crawl_delay(h), k × last_response_time)` (adaptive: slow servers get gentler treatment).
- **Backed by disk:** the frontier holds billions of URLs; keep per-host queue heads in memory and spill the rest to disk/LSM store (Chapter 4).

## 5.5 Deep dives
**Distribution.** Partition **hosts** (not URLs) across crawler nodes by `hash(host) mod N` (consistent hashing), so each host's politeness state lives on one node and needs no cross-node coordination. Newly discovered URLs are forwarded to the owning node in batches. Node failure → its hosts are reassigned; the frontier is checkpointed (or rebuilt from the durable URL log).

**robots.txt and DNS.** Fetch and cache `robots.txt` per host (TTL ~24 h; honor `Disallow`, `Crawl-delay`); cache DNS aggressively (DNS lookup is often the bottleneck at high fetch rates; run a local resolver with large caches and its own concurrency). Respect `User-agent` identification and provide an opt-out contact.

**URL normalization and filtering.** Lowercase scheme/host, remove default ports and fragments, resolve relative links, sort/strip tracking query parameters, canonicalize `index.html`, honor `rel=canonical`. Without it, duplicates explode the frontier.

**Spider traps and quality.** Infinite calendars, session IDs in URLs, generated link graphs. Defenses: max URL length and depth, per-host page budget, detect repeating path patterns, prefer high-priority sources, content-quality signals.

**Duplicate content.** Exact duplicates via content hash; **near-duplicates** via SimHash (64-bit fingerprint where similar documents differ in few bits; compare with Hamming distance ≤ 3) or MinHash/LSH (Chapter 20) so mirrors and boilerplate pages don't flood the index.

**Fetching at scale.** Use asynchronous I/O (thousands of in-flight connections per node), connection reuse (HTTP keep-alive per host), timeouts (connect 5 s, total 30 s), max size caps (e.g., 10 MB), compressed transfer, redirect limits, and robust parsing of broken HTML. **JavaScript-rendered pages** require headless browsers: 10-100× more expensive; run them in a separate, smaller tier for selected sites.

**Freshness.** Estimate each page's change rate from past fetches (a Poisson model) and schedule re-crawls proportional to importance × change rate, bounded by politeness. Use `If-Modified-Since`/ETag to avoid downloading unchanged pages (304).

**Security.** Treat all fetched content as hostile: sandbox parsers; **block fetches to private/link-local/metadata IP ranges after DNS resolution** (the crawler is an SSRF machine; Chapter 22); limit decompression; ignore dangerous schemes.

**Storage.** Raw pages compressed (3-5×) into large append-only files in object storage; a page index (URL → offset) for lookup; the indexer reads from these snapshots.

## 5.6 Failure modes and hardening
| Failure | Handling |
|---|---|
| Fetcher node dies | Host partitions reassigned; in-flight URLs re-queued (idempotent) |
| Slow/hanging server | Timeouts; per-host adaptive backoff; temporary blacklist after repeated failures |
| Frontier corruption/loss | Periodic checkpoints; durable URL log; rebuild |
| DNS outage | Cache stale answers; multiple resolvers |
| Politeness bug hammers a site | Per-host global rate limit enforced centrally (belt and braces); alert on host fetch rate; respond to abuse complaints |
| Bloom filter false positives accumulate | Size for capacity; periodic rebuild; double-check important domains |
| Spider trap consumes budget | Per-host budgets, depth limits, trap detectors |

## 5.7 Trade-offs
| Decision | Choice | Cost | Alternative |
|---|---|---|---|
| Frontier | Per-host queues + ready-time scheduler | More complex than a FIFO | Global FIFO (catastrophic: 1.1% of max at 500 threads) |
| Partitioning | By host | Skewed hosts (giant sites) → straggler nodes | By URL hash (breaks politeness locality) |
| URL-seen set | Bloom filter (1%) | Skips ~1% of new URLs | Exact set (800 GB per 100 B URLs) |
| Freshness | Adaptive re-crawl | Estimation machinery | Fixed schedule (stale or wasteful) |

## 5.8 Follow-ups
1. **How do you crawl 10× faster?** Politeness bounds per-host rate, so you need more *hosts in flight*; 10 B pages/month = 3,858 pages/s ≈ 193 nodes at 20 pages/s each; bottlenecks shift to DNS, storage, and the frontier.
2. **How do you crawl one giant site with 1 M pages politely?** At 1 page/s it takes 11.6 days; negotiate higher limits, use sitemaps, prioritize changed pages.
3. **Detecting content changes cheaply?** HTTP validators, sitemaps, RSS, change-detection hashes.
4. **Focused crawling?** Priority by topic classifiers.
5. **Legal/ethical issues?** robots.txt, ToS, copyright, privacy, user-agent identification.

*What graders look for:* politeness as the central constraint, per-host queues with a scheduler (not a FIFO with blocking threads), URL-seen Bloom filter sizing, distributed partitioning by host, traps and duplicates.

---

# Problem 6: Typeahead / Autocomplete

> **Problem statement.** As a user types into a search box, suggest the 10 most likely completions of the current prefix, fast.

## 6.1 Requirements
- **Functional:** given a prefix, return top-10 suggested queries ranked by popularity (optionally personalized/localized); update suggestions as search behavior changes; filter inappropriate suggestions.
- **Non-functional:** 500 M searches/day; each search triggers ~4 prefix requests after client-side debouncing → **~2 B suggestion requests/day**; **server latency p99 < 20 ms** (end-to-end < 100 ms so it feels instant); suggestions fresh within minutes to hours (trending events within minutes); 99.99% availability (it degrades gracefully: no suggestions is acceptable).

## 6.2 Estimates
```text
Typeahead  231 writes/s   23.1 K reads/s avg   69.4 K peak reads/s   438.0 GB/yr raw logs   0.2 Gbps peak egress   6 app servers   10.0 GB cache (in-memory prefix → top-10 index)
```
Throughput is small; the interesting numbers are **latency** (tens of ms end to end), the size of the **prefix index**, and **freshness**.

## 6.3 API and data model
```text
GET /v1/suggest?q=<prefix>&limit=10&lang=en&geo=IN   → {suggestions:[{text, score}]}
```
- **Query log pipeline:** search events → log (Kafka) → aggregation of counts per normalized query (with time decay) → index builder.
- **Index:** a map `prefix → top-10 [query ids/text]` (or a trie with a top-k list at each node), loaded in memory; versioned snapshots.

## 6.4 Data structure choice, measured
**Setup.** 1.49 M distinct queries from 3 M searches (1-3 words drawn from a Zipf-distributed vocabulary). Two approaches over 100 K prefix probes of length 1-6:
```text
== Typeahead: 1,491,398 distinct queries from 3,000,000 searches (avg 17.1 chars). Return the top 10 most popular completions of a prefix. ==
precomputed top-10 per prefix (length <= 8): 965,556 prefix entries, build 2990 ms, ~153 MB in this JVM representation (a compact trie with 4-byte ids would be ~50 MB)

approach                                                       mean (us)    p50 (us)    p99 (us)    p99.9 (us)
sorted array: range scan + top-10 heap                            142.2       19.8     1328.3       2208.5
precomputed top-10 per prefix (hash map / trie node)                0.2        0.1        1.0          1.4

range size scanned by approach 1, by prefix length (this is why short prefixes are the expensive ones):
   prefix length 1: average 70,831 candidate queries scanned
   prefix length 2: average 11,288 candidate queries scanned
   prefix length 3: average 9,361 candidate queries scanned
   prefix length 4: average 9,562 candidate queries scanned

freshness: rebuild the top-k index offline every few minutes from the aggregated query log; blend a small real-time layer for trending terms; cache the top results of the 1-3 character prefixes at the edge (they are the hottest and the same for everyone).
```
- **Computing on demand** (binary search for the prefix range in a sorted array, then scan the range with a top-10 heap) has a median of 19.5 µs but a **mean of 142 µs and p99 of 1,328 µs**: the cost is proportional to the **size of the candidate range**, and short prefixes match huge ranges (a 1-letter prefix scans ~71 K candidate queries on average).
- **Precomputing the top-10 for every prefix** (what a trie with top-k stored at each node does) answers in **~0.2 µs mean (1 µs p99)**: a **~700× lower mean and >1,000× lower p99 latency**, with a flat latency profile independent of prefix length.
- **Cost of precomputing:** 965,556 prefix entries (prefix length ≤ 8) for 1.49 M queries; ≈ **50 MB** in a compact representation (≈ 153 MB with ordinary JVM objects); build time ≈ 2.5-3 s here. Scaled to 100 M queries, a compact index of the 50-100 M most useful prefixes is a few GB: it fits in each server's memory, so it can be **replicated, not sharded**.
**Conclusion:** trade memory and build time for read latency; the read path becomes a single hash/trie lookup. Memory-saving options: store only prefixes up to length 8-10, only queries above a frequency threshold, **FST/DAWG compression**, and ids instead of strings.

## 6.5 Architecture
```text
 Browser (debounce 100-150 ms, min 1 char, cache results per prefix) ─▶ CDN/edge cache (short prefixes, TTL 1-5 min) ─▶ LB ─▶ Typeahead servers (stateless; in-memory index replica)
      ▲                                                                                                                       │ optional: merge with user's recent searches (personalization)
      │                                                                                                                       ▼
 Index snapshots (object store) ◀── Index builder (every 5-15 min) ◀── Aggregation (stream/batch, time-decayed counts) ◀── Search log (Kafka) ◀── Search service
 Real-time trending layer (small, streaming) merged at query time · Blocklist/filters applied at build and at serve time
```
**Evolution:** v1 `SELECT … WHERE query LIKE 'pre%' ORDER BY count DESC LIMIT 10` on a DB (slow, load-heavy); v2 sorted array/trie in memory per server (range scan cost for short prefixes); v3 **top-k precomputed per prefix**; v4 periodic rebuild and atomic snapshot swap; v5 edge caching of short prefixes + client caching/debouncing; v6 real-time trending layer; v7 personalization and locale-specific indexes, sharding by language/region if the index outgrows memory.

## 6.6 Deep dives
**Why edge-caching works so well.** Short prefixes ("a", "ap", "app") are asked by millions and the answers are the same for everyone, so a CDN or in-process cache absorbs most traffic; long prefixes are rare and cheap. Cache hit ratios of 70-90% are typical. Personalization breaks sharing, so personalize by *merging* a few user-specific suggestions into a shared cached result at the server.

**Ranking.** Raw frequency favors old queries; use **time-decayed counts** (exponential decay with a half-life of days) plus recency/trending boosts, quality signals (queries with results and clicks), locale and language, and demotion of unsafe queries. Combine as a score; keep it explainable and cheap enough to precompute.

**Freshness and the two-layer index.** The batch index (rebuilt every ~10 minutes) captures the stable head. A small **real-time layer** (a streaming job maintaining counts over the last few minutes for rapidly rising queries) is merged at query time to surface breaking news. Both are bounded in size; the merge is cheap.

**Atomic index updates.** Build the new snapshot offline, ship it to servers, load it beside the old one, and **swap an atomic pointer**; keep the previous version for instant rollback; verify with canary servers and sanity checks (size within ±X%, spot-check popular prefixes) before promoting. A bad snapshot should never reach all servers at once.

**Sharding when the index is too big.** Partition by prefix range or by the first 1-2 characters; route in the client library or an aggregator; more commonly **partition by language/market** (each index fits in RAM) and replicate within a market. A prefix server answers only for its partition, so no scatter-gather is needed.

**Typos, multi-language, and misspellings.** Spell correction ("did you mean") is a separate service (edit distance/n-gram indexes); tokenization differs by language (CJK needs segmenters); normalize case/accents consistently at build and serve time.

**Abuse and safety.** Bots can poison suggestions by hammering queries; count **distinct users** (HyperLogLog, Chapter 20) rather than raw events, rate-limit by client, and run blocklists and classifiers on candidates; legal takedowns need a fast "suppress this suggestion" path (serve-time blocklist).

**Capacity.** 69 K peak QPS across ~6 servers at ~20 K QPS each is trivial for an in-memory lookup; provision 3× for redundancy and AZ loss. The memory (10 GB) is the larger constraint.

## 6.7 Failure modes and hardening
| Failure | Handling |
|---|---|
| Bad index build | Validation, canary, atomic swap, instant rollback |
| Server crash | Stateless replicas; the client simply shows no suggestions |
| Hot prefix (breaking news) | Edge cache + real-time layer; replicas |
| Pipeline lag | Stale suggestions are tolerable; alert on snapshot age |
| Offensive/PII suggestions | Build-time filters + serve-time blocklist + fast suppression API |
| Latency spikes (GC, cold snapshot) | Warm-up before taking traffic; preload memory; avoid allocation in the hot path |

## 6.8 Trade-offs
| Decision | Choice | Cost | Alternative |
|---|---|---|---|
| Compute vs precompute | Precompute top-10 per prefix | Memory (GBs) and rebuild lag | Range-scan at query time (142 µs mean, 1.3 ms p99, no extra memory) |
| Freshness | Batch (minutes) + small real-time layer | Two layers to operate | Pure batch (stale trends) / pure streaming (complex) |
| Personalization | Merge a few personal results at serve time | Per-user lookup | Fully personalized index (no cache sharing) |
| Distribution | Replicate full index per market | Memory per server | Shard by prefix (routing + hot shards) |

## 6.9 Follow-ups
1. **Support typos?** Fuzzy matching via edit-distance automata or n-gram indexes; ranking penalty for corrected matches.
2. **Local-language/regional?** Per-locale indexes selected by header/geo.
3. **How big can the index be?** Count prefixes × bytes; use frequency thresholds and FST compression to fit RAM.
4. **How do you evaluate quality?** Offline: MRR/keystrokes saved on logs; online: A/B on click-through and search completion rate.
5. **Search-as-you-type for documents (not queries)?** Inverted index with edge n-grams (Elasticsearch), different cost model.

*What graders look for:* precompute vs compute with numbers, client-side debouncing/caching, edge caching reasoning, freshness design (batch + real-time), atomic index swap, safety/abuse.

---

# Cross-problem patterns

| Pattern | Where | Lesson |
|---|---|---|
| **Fan-out dial** | Feed, notifications, campaigns | Move work between write time and read time using the *measured* distribution |
| **Isolation by priority / bulkheads** | Notifications | Separate queues and workers so bulk can't starve critical traffic |
| **Content addressing** | File sync, photos | Hash-named immutable chunks give dedup, resumability, integrity, cheap versions |
| **Rolling-hash boundaries** | File sync | Make boundaries depend on content, not position |
| **Scheduling under constraints** | Crawler, notifications | Ready-time schedulers beat blocking FIFOs |
| **Precompute vs compute** | Typeahead, feed timelines | Spend memory/build time to remove read-time work |
| **Journal + cursor** | File sync, chat | Incremental ordered sync without clocks |
| **Hybrid thresholds** | Feed (celebrities), photo renditions | A simple threshold on a heavy-tailed distribution captures most of the benefit |
| **Immutable objects + CDN** | Photos, typeahead short prefixes | Cacheability is a design choice you make by keeping data immutable |
| **Bloom filters for "seen"** | Crawler | Trade ~1% false positives for 6-7× less memory |
| **Idempotent consumers** | Fan-out, notifications, processing pipelines | Redelivery is a fact of life |
| **Lifecycle/tiering** | Photos, files | Cost is dominated by cold data; automate its movement |

---

# Labs

All code below produced the outputs quoted in the problems above.

## Lab 1: Feed fan-out strategies (Problem 1)
```java
import java.util.*;

public class FeedLab {
    public static void main(String[] args) {
        int U = 1_000_000; Random r = new Random(12); double alpha = 1.25, maxF = 500_000;
        // follower counts: Pareto tail, then rescaled so the mean is ~200 (typical social graph: average out-degree equals average in-degree)
        double xm = 20; double[] f = new double[U];
        for (int round = 0; round < 25; round++) { r = new Random(12); double sum = 0; for (int i = 0; i < U; i++) { f[i] = Math.min(maxF, Math.floor(xm * Math.pow(1 - r.nextDouble(), -1.0 / alpha))); sum += f[i]; } double mean = sum / U; xm *= 200.0 / mean; }
        double[] sorted = f.clone(); Arrays.sort(sorted); double totalEdges = Arrays.stream(f).sum();
        System.out.printf("== Social graph: %,d users; followers per user: mean %.0f, median %.0f, p99 %.0f, p99.99 %.0f, max %.0f; the top 1%% of accounts own %.0f%% of all follow edges ==%n",
            U, totalEdges / U, sorted[U / 2], sorted[(int) (U * 0.99)], sorted[(int) (U * 0.9999)], sorted[U - 1], 100 * Arrays.stream(sorted, (int) (U * 0.99), U).sum() / totalEdges);
        double scale = 200.0;                                            // 1M simulated users stand for 200M daily active users
        double readsPerUserPerDay = 10;
        double[] posts = new double[U]; for (int i = 0; i < U; i++) posts[i] = 0.2 + (f[i] > 100_000 ? 3 : 0);                    // ordinary accounts post 0.2/day, huge accounts 3.2/day
        double totalPosts = Arrays.stream(posts).sum();
        System.out.printf("posts/day: %,.0f (x%.0f scale = %,.0f/day = %,.0f posts/s); feed reads/day at 10 per user: %,.0f (%,.0f/s)%n", totalPosts, scale, totalPosts * scale, totalPosts * scale / 86400, U * readsPerUserPerDay * scale, U * readsPerUserPerDay * scale / 86400);
        System.out.println();
        System.out.println("strategy                        timeline inserts/day   inserts/s (avg)   avg lookups per feed read   feed reads/s -> backend lookups/s   largest single fan-out   time to fan it out @ 100K inserts/s");
        double[] thresholds = {0, 1_000, 10_000, 100_000, Double.POSITIVE_INFINITY}; String[] names = {"pure pull (fan-out on read)", "hybrid: push if followers <= 1K", "hybrid: push if followers <= 10K", "hybrid: push if followers <= 100K", "pure push (fan-out on write)"};
        for (int t = 0; t < thresholds.length; t++) {
            double T = thresholds[t]; double inserts = 0, pulledEdges = 0, maxPush = 0;
            for (int i = 0; i < U; i++) { if (f[i] <= T) { inserts += posts[i] * f[i]; maxPush = Math.max(maxPush, f[i]); } else pulledEdges += f[i]; }
            double avgPullLookups = pulledEdges / U;                      // each user follows (on average) this many pulled accounts -> one recent-posts lookup each at read time
            double lookups = 1 + avgPullLookups;                          // +1 for the user's own precomputed timeline
            double readsPerSec = U * readsPerUserPerDay * scale / 86400;
            System.out.printf("%-31s %,18.0f %,16.0f %,22.1f %,22.0f -> %,16.0f   %,18.0f   %14.1f s%n", names[t], inserts * scale, inserts * scale / 86400, t == 0 ? totalEdges / U : lookups, readsPerSec, readsPerSec * (t == 0 ? totalEdges / U : lookups), maxPush, maxPush / 100_000);
        }
        double timelineBytes = 800 * 8.0 * U * scale / 1e9;
        System.out.printf("%ntimeline storage (push side): 200M users x 800 entries x 8 B (post id) = %.0f GB in an in-memory store; a push-only design also pays one insert per follower per post even for users who never log in (inactive users should be skipped or lazily rebuilt).%n", timelineBytes);
    }
}
```
**Experiments:** (1) Vary the Pareto tail exponent (1.1 to 1.6) and the cap; find how the best push/pull threshold moves. (2) Skip inactive followers (assume 40% are active): recompute inserts/day. (3) Add a cost model (insert = 1 unit, lookup = 3 units) and choose the threshold that minimizes total cost. (4) Simulate fan-out lag for a 500 K-follower post at 100 K inserts/s with and without "active followers first."

## Lab 2: Notification priorities (Problem 2)
```java
import java.util.*;

public class NotifyLab {
    public static void main(String[] a) {
        double capacity = 5000, otp = 200, txn = 300, campaign = 2_000_000; int campaignAt = 10;
        System.out.println("== Notification priorities. Provider capacity 5,000 msg/s. Steady load: 200 OTP codes/s + 300 transactional msgs/s. At t=10 s marketing enqueues a 2,000,000-message campaign. ==");
        System.out.println("policy                                                     OTP wait during campaign: median / worst (s)   transactional worst (s)   campaign finishes after (s)   spare capacity during campaign");
        String[] names = {"single FIFO queue for everything", "strict priority: OTP > transactional > marketing", "strict priority + marketing rate-capped at 2,000/s", "weighted shares 70/20/10, work-conserving"};
        for (int pol = 0; pol < 4; pol++) {
            double qO = 0, qT = 0, qM = 0, fifo = 0; List<Double> waitsDuring = new ArrayList<>(); double worstT = 0; int done = -1;
            for (int t = 0; t < 3000; t++) {
                double aO = otp, aT = txn, aM = t == campaignAt ? campaign : 0;
                if (pol == 0) {
                    // everything shares one FIFO queue: a message arriving now waits behind the whole backlog
                    double backlog = fifo + aO + aT + aM; double served = Math.min(capacity, backlog); fifo = backlog - served;
                    double wait = fifo / capacity; if (fifo > 1 || t >= campaignAt) { if (t >= campaignAt && done < 0) waitsDuring.add(wait); } worstT = Math.max(worstT, wait);
                    if (t > campaignAt && fifo < 1 && done < 0) done = t;
                } else {
                    qO += aO; qT += aT; qM += aM; double cap = capacity, sO = 0, sT = 0, sM = 0;
                    if (pol == 1 || pol == 2) { sO = Math.min(qO, cap); cap -= sO; sT = Math.min(qT, cap); cap -= sT; sM = Math.min(qM, pol == 2 ? Math.min(cap, 2000) : cap); }
                    else { sO = Math.min(qO, 0.7 * cap); sT = Math.min(qT, 0.2 * cap); sM = Math.min(qM, 0.1 * cap); double spare = cap - sO - sT - sM;
                           double xO = Math.min(qO - sO, spare); sO += xO; spare -= xO; double xT = Math.min(qT - sT, spare); sT += xT; spare -= xT; sM += Math.min(qM - sM, spare); }
                    qO -= sO; qT -= sT; qM -= sM;
                    if (t >= campaignAt && done < 0) waitsDuring.add(qO / capacity);
                    worstT = Math.max(worstT, qT / capacity);
                    if (t > campaignAt && qM < 1 && done < 0) done = t;
                }
            }
            Collections.sort(waitsDuring);
            System.out.printf("%-58s %10.2f / %8.2f                               %8.2f                  %8d                     %s%n", names[pol], waitsDuring.get(waitsDuring.size() / 2), waitsDuring.get(waitsDuring.size() - 1), worstT, done - campaignAt,
                pol == 2 ? String.format("%,.0f msg/s", capacity - otp - txn - 2000) : (pol == 0 ? "none (queue saturated)" : "none until the campaign ends"));
        }
        System.out.println("takeaway: a login OTP that waits 6.5 minutes is a failed login. Priority classes protect latency-critical traffic; a rate cap on bulk traffic also keeps headroom for retries and bursts of high-priority messages, at the price of a campaign that takes 2.2x longer.");
    }
}
```
**Experiments:** (1) Add retries (5% of messages fail and are re-enqueued) and observe the effect of the rate cap. (2) Add a second provider with half the capacity and a failover rule. (3) Make high-priority load spike to 6,000/s for 30 s: compare strict priority with weighted shares (70/20/10): who starves? (4) Model "9:00 local time" waves across time zones.

## Lab 3: Fixed vs content-defined chunking (Problem 4)
```java
import java.security.MessageDigest;
import java.util.*;

public class SyncLab {
    static final long[] GEAR = new long[256];
    static { Random r = new Random(99); for (int i = 0; i < 256; i++) GEAR[i] = r.nextLong(); }

    record Chunk(String id, int len) {}
    static String sha(byte[] d, int off, int len) throws Exception { MessageDigest md = MessageDigest.getInstance("SHA-256"); md.update(d, off, len); byte[] h = md.digest(); StringBuilder sb = new StringBuilder(); for (int i = 0; i < 8; i++) sb.append(String.format("%02x", h[i])); return sb.toString(); }

    static List<Chunk> fixed(byte[] d, int size) throws Exception { List<Chunk> out = new ArrayList<>(); for (int off = 0; off < d.length; off += size) { int len = Math.min(size, d.length - off); out.add(new Chunk(sha(d, off, len), len)); } return out; }

    /** Content-defined chunking with a Gear rolling hash: a boundary is declared where the hash of the last few bytes matches a pattern. */
    static List<Chunk> cdc(byte[] d, int min, int max, long mask) throws Exception {
        List<Chunk> out = new ArrayList<>(); int start = 0;
        while (start < d.length) {
            long h = 0; int end = Math.min(d.length, start + max); int cut = end;
            for (int i = start; i < end; i++) { h = (h << 1) + GEAR[d[i] & 0xff]; if (i - start + 1 >= min && (h & mask) == 0) { cut = i + 1; break; } }
            out.add(new Chunk(sha(d, start, cut - start), cut - start)); start = cut;
        }
        return out;
    }

    static byte[] insert(byte[] d, int at, int n, long seed) { byte[] x = new byte[n]; new Random(seed).nextBytes(x); byte[] o = new byte[d.length + n]; System.arraycopy(d, 0, o, 0, at); System.arraycopy(x, 0, o, at, n); System.arraycopy(d, at, o, at + n, d.length - at); return o; }
    static byte[] overwrite(byte[] d, int at, int n, long seed) { byte[] o = d.clone(); byte[] x = new byte[n]; new Random(seed).nextBytes(x); System.arraycopy(x, 0, o, at, n); return o; }
    static byte[] delete(byte[] d, int at, int n) { byte[] o = new byte[d.length - n]; System.arraycopy(d, 0, o, 0, at); System.arraycopy(d, at + n, o, at, d.length - at - n); return o; }

    static String upload(List<Chunk> oldC, List<Chunk> newC, long fileLen) {
        Set<String> known = new HashSet<>(); for (Chunk c : oldC) known.add(c.id()); long bytes = 0; int changed = 0; for (Chunk c : newC) if (!known.contains(c.id())) { bytes += c.len(); changed++; }
        return String.format("%,10d B (%5.2f%%)  %3d of %d chunks", bytes, 100.0 * bytes / fileLen, changed, newC.size());
    }

    public static void main(String[] args) throws Exception {
        byte[] base = new byte[20_000_000]; new Random(5).nextBytes(base);
        int min = 16 * 1024, max = 128 * 1024; long mask = (1L << 15) - 1;
        List<Chunk> f0 = fixed(base, 64 * 1024), c0 = cdc(base, min, max, mask);
        double avg = c0.stream().mapToInt(Chunk::len).average().orElse(0);
        System.out.printf("== File sync: a 20 MB file. Fixed 64 KB chunks: %d chunks. Content-defined chunks (Gear hash, min 16 KB, max 128 KB): %d chunks, average %.0f KB. ==%n", f0.size(), c0.size(), avg / 1024);
        System.out.println("edit made to the file                          fixed-size chunking: data to upload                       content-defined chunking: data to upload");
        String[] names = {"insert 100 bytes at the start", "insert 100 bytes in the middle", "overwrite 100 bytes in the middle (same length)", "append 1 KB at the end", "delete 10 KB near the 5 MB mark"};
        byte[][] edited = {insert(base, 0, 100, 1), insert(base, 10_000_000, 100, 2), overwrite(base, 10_000_000, 100, 3), insert(base, base.length, 1024, 4), delete(base, 5_000_000, 10_240)};
        for (int i = 0; i < names.length; i++) System.out.printf("%-46s %-56s %s%n", names[i], upload(f0, fixed(edited[i], 64 * 1024), edited[i].length), upload(c0, cdc(edited[i], min, max, mask), edited[i].length));
        // cross-user dedup: two users hold the same 20 MB file
        System.out.printf("%nmetadata cost for a 1 GB file: fixed 64 KB -> %,d chunks; CDC avg ~%.0f KB -> ~%,d chunks; at ~80 B per chunk entry that is %.1f MB vs %.1f MB per file version%n", 1_000_000_000 / 65536, avg / 1024, (int) (1e9 / avg), 1e9 / 65536 * 80 / 1e6, 1e9 / avg * 80 / 1e6);
    }
}
```
**Experiments:** (1) Change the mask to 13, 15, 17 bits and tabulate average chunk size, chunk count, and bytes uploaded after an edit. (2) Add **compression** (java.util.zip) per chunk; measure savings on random vs text-like data. (3) Test **cross-file dedup**: two files that share a 5 MB region at different offsets. (4) Implement manifest-level **version diff** and a mark-and-sweep GC over manifests.

## Lab 4: Crawler frontier (Problem 5)
```java
import java.util.*;

public class CrawlerLab {
    public static void main(String[] args) {
        int hosts = 2000, totalUrls = 3_000_000; double horizon = 600, fetchTime = 0.2, politeness = 1.0;
        // pages per host: Zipf(1.0): a few giant sites, a long tail of tiny ones
        double[] w = new double[hosts]; double sum = 0; for (int h = 0; h < hosts; h++) { w[h] = 1.0 / (h + 1); sum += w[h]; }
        int[] pages = new int[hosts]; int assigned = 0; for (int h = 0; h < hosts; h++) { pages[h] = Math.max(1, (int) (totalUrls * w[h] / sum)); assigned += pages[h]; }
        double bound = 0; for (int h = 0; h < hosts; h++) bound += Math.min(pages[h], Math.floor(horizon / politeness) + 1);
        int sustaining = 0; for (int h = 0; h < hosts; h++) if (pages[h] >= horizon / politeness) sustaining++;
        System.out.printf("== Crawler frontier. %,d hosts, %,d URLs (Zipf 1.0 per host: biggest host has %,d URLs, median host %,d). Politeness: at most 1 fetch/s per host. Fetch takes 0.2 s. %.0f s simulated. ==%n", hosts, assigned, pages[0], pages[hosts / 2], horizon);
        System.out.printf("theoretical maximum (every host fetched at exactly 1/s until its URLs run out): %,.0f pages in %.0f s = %.0f pages/s; only %d hosts have enough URLs to stay busy the whole time%n%n", bound, horizon, bound / horizon, sustaining);
        System.out.println("frontier design                                            fetcher threads   pages crawled in 600 s   pages/s   % of theoretical max");
        for (int design = 0; design < 3; design++) for (int fetchers : design == 0 ? new int[]{100, 500, 5000} : new int[]{500}) {
            int[] remaining = pages.clone(); double[] nextAllowed = new double[hosts]; long crawled = 0;
            PriorityQueue<Double> free = new PriorityQueue<>(); for (int i = 0; i < fetchers; i++) free.add(0.0);
            if (design == 0) {
                // one global FIFO of URLs (interleaved by site popularity). A fetcher takes the next URL and, if that host is not yet allowed, WAITS while holding its slot.
                Random r = new Random(1); double[] cum = new double[hosts]; double s = 0; for (int h = 0; h < hosts; h++) { s += pages[h]; cum[h] = s; }
                for (int i = 0; i < assigned; i++) {
                    double u = r.nextDouble() * s; int lo = 0, hi = hosts - 1; while (lo < hi) { int mid = (lo + hi) >>> 1; if (cum[mid] < u) lo = mid + 1; else hi = mid; } int h = lo;
                    if (remaining[h] == 0) continue; remaining[h]--;
                    if (free.isEmpty()) break;
                    double t = free.poll(); double start = Math.max(t, nextAllowed[h]);
                    if (start > horizon) { nextAllowed[h] = start + politeness; continue; }                     // this fetcher is stuck waiting on a busy host beyond the horizon: it is lost for the rest of the run
                    nextAllowed[h] = start + politeness; free.add(start + fetchTime); crawled++;
                }
            } else {
                // per-host queues + scheduler: fetch from whichever host is allowed NOW; fetchers never wait on a host
                PriorityQueue<double[]> ready = new PriorityQueue<>(Comparator.comparingDouble(x -> x[0])); for (int h = 0; h < hosts; h++) ready.add(new double[]{0, h});
                while (!ready.isEmpty()) {
                    double t = free.poll(); double[] hq = ready.poll(); int h = (int) hq[1]; double start = Math.max(t, hq[0]); if (start > horizon) break;
                    remaining[h]--; crawled++; free.add(start + fetchTime); if (remaining[h] > 0) ready.add(new double[]{start + politeness, h});
                }
            }
            String name = design == 0 ? "one global FIFO queue; fetcher blocks on a busy host" : (design == 1 ? "per-host queues + ready-time scheduler" : "");
            if (design == 2) continue;
            System.out.printf("%-58s %8d          %,14d          %6.0f        %5.1f%%%n", name, fetchers, crawled, crawled / horizon, 100.0 * crawled / bound);
        }
        // Bloom filter sizing for the URL-seen set
        double urls = 100e9;
        System.out.printf("%nURL-seen set for 100 billion URLs: exact hash set (8-byte fingerprints) = %.0f GB; Bloom filter at 1%% false positives (9.6 bits/URL) = %.0f GB; at 0.1%% (14.4 bits/URL) = %.0f GB%n", urls * 8 / 1e9, urls * 9.6 / 8 / 1e9, urls * 14.4 / 8 / 1e9);
    }
}
```
**Experiments:** (1) Make crawl delay vary by host (1-10 s) and compare. (2) Implement adaptive delay `k × response_time`. (3) Partition hosts across 4 simulated nodes by hash and add a straggler (the node owning the biggest host); measure total throughput. (4) Add a **Bloom filter** and measure false-positive-induced skips for 100 K, 1 M URLs at 1% design FPR.

## Lab 5: Typeahead data structures (Problem 6)
```java
import java.util.*;

public class TypeaheadLab {
    static String[] vocab; static double[] cdf;
    static String word(Random r) { double u = r.nextDouble() * cdf[cdf.length - 1]; int lo = 0, hi = cdf.length - 1; while (lo < hi) { int mid = (lo + hi) >>> 1; if (cdf[mid] < u) lo = mid + 1; else hi = mid; } return vocab[lo]; }

    public static void main(String[] args) {
        Random r = new Random(11); int V = 20_000; vocab = new String[V]; cdf = new double[V]; double sum = 0;
        for (int i = 0; i < V; i++) { int len = 3 + r.nextInt(7); char[] c = new char[len]; for (int j = 0; j < len; j++) c[j] = (char) ('a' + r.nextInt(26)); vocab[i] = new String(c); sum += 1.0 / Math.pow(i + 1, 1.0); cdf[i] = sum; }
        // query log: 1-3 word queries, popularity follows word popularity
        int samples = 3_000_000; HashMap<String, Integer> counts = new HashMap<>();
        for (int i = 0; i < samples; i++) { int nw = 1 + r.nextInt(3); StringBuilder sb = new StringBuilder(); for (int j = 0; j < nw; j++) { if (j > 0) sb.append(' '); sb.append(word(r)); } counts.merge(sb.toString(), 1, Integer::sum); }
        String[] queries = counts.keySet().toArray(new String[0]); Arrays.sort(queries); int Q = queries.length; int[] cnt = new int[Q]; for (int i = 0; i < Q; i++) cnt[i] = counts.get(queries[i]);
        long chars = 0; for (String q : queries) chars += q.length();
        System.out.printf("== Typeahead: %,d distinct queries from %,d searches (avg %.1f chars). Return the top 10 most popular completions of a prefix. ==%n", Q, samples, (double) chars / Q);

        // ---- approach 1: sorted array + binary search for the prefix range + scan the range with a size-10 heap
        // ---- approach 2: precomputed top-10 for every prefix up to length 8 (what a trie with top-k per node stores), binary-search fallback for longer prefixes
        long t0 = System.nanoTime(); HashMap<String, int[]> topk = new HashMap<>(); int maxLen = 8;
        for (int i = 0; i < Q; i++) { String q = queries[i]; for (int L = 1; L <= Math.min(maxLen, q.length()); L++) { String p = q.substring(0, L); int[] cur = topk.get(p); if (cur == null) { cur = new int[10]; Arrays.fill(cur, -1); topk.put(p, cur); }
                int pos = -1; for (int k = 0; k < 10; k++) { if (cur[k] < 0 || cnt[cur[k]] < cnt[i]) { pos = k; break; } } if (pos >= 0) { for (int k = 9; k > pos; k--) cur[k] = cur[k - 1]; cur[pos] = i; } } }
        double buildMs = (System.nanoTime() - t0) / 1e6;
        long entries = topk.size(); long keyChars = 0; for (String k : topk.keySet()) keyChars += k.length();
        double memMB = (entries * (40 + 48 + 56) + keyChars * 2.0) / 1e6;               // rough JVM sizing: map entry + String header + int[10] array (+ chars)
        System.out.printf("precomputed top-10 per prefix (length <= %d): %,d prefix entries, build %.0f ms, ~%.0f MB in this JVM representation (a compact trie with 4-byte ids would be ~%.0f MB)%n", maxLen, entries, buildMs, memMB, entries * (10 * 4 + 12) / 1e6);

        int nQueries = 100_000; String[] probes = new String[nQueries];
        for (int i = 0; i < nQueries; i++) { String q = queries[Math.min(Q - 1, (int) (r.nextDouble() * r.nextDouble() * Q))]; int L = 1 + r.nextInt(Math.min(6, q.length())); probes[i] = q.substring(0, L); }
        // approach 1
        int sink = 0; double[] lat1 = new double[nQueries];
        for (int rep = 0; rep < 2; rep++) for (int i = 0; i < nQueries; i++) {
            long s = System.nanoTime(); String p = probes[i]; int lo = lowerBound(queries, p), hi = lowerBound(queries, p + Character.MAX_VALUE);
            int[] best = new int[10]; Arrays.fill(best, -1); for (int k = lo; k < hi; k++) { int pos = -1; for (int j = 0; j < 10; j++) { if (best[j] < 0 || cnt[best[j]] < cnt[k]) { pos = j; break; } } if (pos >= 0) { for (int j = 9; j > pos; j--) best[j] = best[j - 1]; best[pos] = k; } }
            sink += best[0]; if (rep == 1) lat1[i] = (System.nanoTime() - s) / 1e3; }
        // approach 2
        double[] lat2 = new double[nQueries];
        for (int rep = 0; rep < 2; rep++) for (int i = 0; i < nQueries; i++) { long s = System.nanoTime(); int[] res = topk.get(probes[i]); sink += res == null ? 0 : res[0]; if (rep == 1) lat2[i] = (System.nanoTime() - s) / 1e3; }
        Arrays.sort(lat1); Arrays.sort(lat2);
        System.out.println("\napproach                                                       mean (us)    p50 (us)    p99 (us)    p99.9 (us)");
        System.out.printf("%-62s %8.1f   %8.1f   %8.1f    %9.1f%n", "sorted array: range scan + top-10 heap", Arrays.stream(lat1).average().orElse(0), lat1[nQueries / 2], lat1[(int) (nQueries * 0.99)], lat1[(int) (nQueries * 0.999)]);
        System.out.printf("%-62s %8.1f   %8.1f   %8.1f    %9.1f%n", "precomputed top-10 per prefix (hash map / trie node)", Arrays.stream(lat2).average().orElse(0), lat2[nQueries / 2], lat2[(int) (nQueries * 0.99)], lat2[(int) (nQueries * 0.999)]);
        // how big are the scanned ranges by prefix length?
        System.out.println("\nrange size scanned by approach 1, by prefix length (this is why short prefixes are the expensive ones):");
        for (int L = 1; L <= 4; L++) { long tot = 0; int n = 0; for (int i = 0; i < 2000; i++) { String q = queries[r.nextInt(Q)]; if (q.length() < L) continue; String p = q.substring(0, L); tot += lowerBound(queries, p + Character.MAX_VALUE) - lowerBound(queries, p); n++; } System.out.printf("   prefix length %d: average %,d candidate queries scanned%n", L, tot / n); }
        if (sink == 42) System.out.println();
        System.out.println("\nfreshness: rebuild the top-k index offline every few minutes from the aggregated query log; blend a small real-time layer for trending terms; cache the top results of the 1-3 character prefixes at the edge (they are the hottest and the same for everyone).");
    }
    static int lowerBound(String[] a, String key) { int lo = 0, hi = a.length; while (lo < hi) { int mid = (lo + hi) >>> 1; if (a[mid].compareTo(key) < 0) lo = mid + 1; else hi = mid; } return lo; }
}
```
**Experiments:** (1) Raise the maximum precomputed prefix length (4, 6, 8, 12) and tabulate memory vs the fraction of probes served from the index. (2) Add a frequency threshold (drop queries with count < 2, 5) and measure index size and top-1 accuracy. (3) Implement a **trie** with top-k per node and compare memory/latency to the hash map. (4) Add time decay (half-life) and show how rankings change after a trending burst.

---

# Exercises

### A. Variations
1. **Feed:** using the lab table, what fraction of an average user's followed accounts are pulled (not pushed) at thresholds of 1 K, 10 K, and 100 K followers?
2. **Feed:** a user with 2 M followers posts. At 100 K inserts/s how long does the fan-out take? Give three mitigations.
3. **Notifications:** a 10 M-message campaign with 5,000 msg/s provider capacity and 500 msg/s of high-priority traffic: completion time with strict priority, and with a 2,000/s cap.
4. **Photos:** at a 97% CDN hit ratio, what is origin egress for 416.7 Gbit/s of peak demand? At 99%?
5. **Photos:** one 3 MB photo with 0.5 MB of renditions: bytes stored with 3× replication vs 1.4× erasure coding.
6. **Sync:** how many chunks and how much manifest metadata for a 1 GB file with ~46 KB average chunks and 80 B per entry?
7. **Crawler:** 10 B pages/month at 100 KB: pages/s, nodes at 20 pages/s each, raw storage per month.
8. **Typeahead:** 50 M prefix entries at ~52 B each: memory? Does a replicated per-server index make sense?
9. **Crawler:** a Bloom filter for 30 B URLs at 0.1% false positives: memory?

### B. Calculations
10. **Feed:** 150 M DAU, 8 feed reads per user per day, peak ×3: peak feed QPS. With the hybrid reading 13 lookups per read, backend lookups/s at peak.
11. **Notifications:** 5% of 600 M daily notifications are OTPs with a 5 s SLO, arriving evenly. OTP rate/s? If an OTP needs 1 provider call at ~200 ms, how many concurrent provider connections (Little's law)?
12. **Sync:** 100 M changes/day; peak ×4: commits/s. If each commit does 3 metadata writes, write QPS? Is one MySQL primary enough? How many shards at 5 K writes/s each?
13. **Photos:** 100 M uploads/day; thumbnail generation at 25 ms CPU each; workers needed (60% utilization, 1 core each) for the average and a 4× peak.
14. **Crawler:** politeness 1 req/s per host; 2,000 active hosts; fetch 0.2 s; how many in-flight fetches are needed at steady state to reach the maximum?
15. **Typeahead:** 500 M searches/day × 4 requests, peak ×3, 85% edge cache hit: requests/s reaching servers.

### C. Debugging
16. Feed: some users see their own post only after 30 seconds. Hypotheses?
17. Notifications: users report receiving the same OTP twice. Where could duplicates arise and how do you confirm?
18. Photos: thumbnails appear instantly but large images stay "processing" for hours. Check list?
19. Sync: two devices keep overwriting each other's changes in a loop. What is likely wrong?
20. Crawler: throughput is 5% of expectation though CPU and bandwidth are idle. What do you check?
21. Typeahead: after a deploy, suggestions for popular prefixes are empty for 3 minutes. Why, and how do you prevent it?

### D. Critique
22. “For the feed I'll compute the timeline on every read with a SQL join across followees.” Evaluate.
23. “I'll put all notifications in one Kafka topic and consumers will process them in order.” Evaluate.
24. “I'll chunk files into fixed 4 MB blocks; it's simple and dedup works fine.” Evaluate.
25. “The crawler uses a thread pool of 1,000 workers pulling from one queue.” Evaluate.

### E. Interview-style
26. Give a two-minute answer to “How would you handle a celebrity with 50 M followers in a news feed?”
27. “How would you make sure a marketing blast never delays a password-reset email?”
28. “How does Dropbox upload only the changed part of a 1 GB file?”

### F. Teach-back
29. Explain why a rolling hash fixes the “insert shifts everything” problem using a book-pages analogy.

---

# Full answer key

**1.** Average followees pulled = (lookups − 1) per user of ~200 followees: ≤1 K: 81.3/200 = **40.7%** of edges pulled; ≤10 K: 37.7/200 = **18.9%**; ≤100 K: 12.1/200 = **6.1%**.

**2.** 2,000,000 / 100,000 = **20 s**. Mitigations: treat the account as a pull-based celebrity (don't fan out); fan out to active followers first and the rest lazily; batch inserts by shard and parallelize workers; cap per-author fan-out rate to protect the cluster.

**3.** With strict priority the campaign gets 5,000 − 500 = 4,500 msg/s: 10,000,000 / 4,500 = **2,222 s ≈ 37 min**. With a 2,000/s cap: 10,000,000 / 2,000 = **5,000 s ≈ 83 min** (leaving 2,500 msg/s headroom).

**4.** 97%: 416.7 × 0.03 = **12.5 Gbit/s**; 99%: 416.7 × 0.01 = **4.2 Gbit/s**. Each point of hit ratio is ~4.2 Gbit/s at peak.

**5.** Stored data 3.5 MB: 3× replication = **10.5 MB**; 1.4× EC = **4.9 MB** (−53%).

**6.** 1,000,000,000 / 46,000 ≈ **21,700 chunks** (the lab's more precise average gives ~21,100); × 80 B ≈ **1.7 MB** of manifest.

**7.** 10 B / (30 × 86,400 = 2.592 M s) = **3,858 pages/s**; ÷ 20 = **193 nodes**; storage 10 B × 100 KB = **1 PB/month** raw (about 250 TB compressed at 4×).

**8.** 50 M × 52 B = **2.6 GB**: fits easily in memory; replicate per server (no sharding needed) and rebuild on a schedule.

**9.** 14.4 bits × 30 B = 432 Gbit = **54 GB**.

**10.** 150 M × 8 = 1.2 B reads/day → 13.9 K/s average → peak ×3 = **41.7 K feed reads/s**; × 13 lookups = **542 K lookups/s** at peak.

**11.** OTPs: 5% × 600 M = 30 M/day = **347/s** average. Little's law: concurrency = rate × time = 347 × 0.2 = **~70 concurrent connections** (peak ×3 ≈ 210); provision more for provider latency spikes (if latency rises to 2 s, concurrency is 700).

**12.** Average 1.16 K/s; peak ×4 = **4.6 K commits/s**; × 3 writes = **13.9 K metadata writes/s**. One primary (≈5 K writes/s) is not enough → **≥ 3 shards** (with headroom, e.g., 8 logical shards across 3-4 primaries).

**13.** Average 1.16 K photos/s × 25 ms = 29 cores busy; at 60% utilization = **48 cores**; at 4× peak = 4.6 K/s × 25 ms = 116 cores → **193 cores**. (Plus larger renditions, which cost more: this is the thumbnail only.)

**14.** Maximum is 2,000 hosts × 1 fetch/s = 2,000 fetches/s; each takes 0.2 s → Little's law: in-flight = 2,000 × 0.2 = **400 fetches** (plus headroom, e.g., 500 fetchers, as in the lab).

**15.** 500 M × 4 = 2 B requests/day = 23.1 K/s average; peak ×3 = 69.4 K/s; 85% edge hits → **10.4 K/s** reach servers.

**16.** Own post not inserted into own timeline synchronously (relying on fan-out lag); fan-out queue backlog; read replica lag; cache of the timeline not updated; ranking layer reordering; clock-based filter hiding "future" posts due to skew.

**17.** Duplicates arise from: retry after a lost provider response, redelivery from the queue after a worker crash before acking, the same event published twice upstream, fallback to SMS firing while push also delivered, user pressing resend. Confirm by correlating `idempotency_key`, `notification_id`, provider message ids, and attempt counts. Fix with durable idempotency keys, provider tokens, and ack-after-send plus dedupe.

**18.** Rendition queue backlog or autoscaling limits; failing jobs stuck in retry/DLQ; worker OOM on large images; format-specific encoder failures (AVIF); missing storage permissions; wrong rendition dependency (large depends on thumbnail state); poison image repeatedly retried; priority inversion (large behind a bulk reprocessing job).

**19.** Last-writer-wins by mtime without version checks, a client re-uploading its own downloaded changes as new edits (echo suppression missing), non-atomic replace triggering the file watcher, clock skew, or no base_version CAS so each device believes its version is newer.

**20.** Per-host politeness blocking (global FIFO/blocked threads), DNS resolution latency, too few hosts in the frontier (a few giant hosts), robots.txt fetch serialization, connection pool limits per host, TLS handshakes, or throttling by the target.

**21.** The new server instances start with an empty/unloaded index (or the new snapshot is still loading) while receiving traffic: need readiness gating (don't serve until the snapshot loads), warm-up, canary + gradual rollout, and keep the old version until the new one is verified.

**22.** A join over hundreds of followees per read at 23 K reads/s means ~4.6 M row lookups/s with p99 set by the slowest shard: infeasible at this scale; precompute timelines (hybrid) and cache.

**23.** One topic with in-order processing creates head-of-line blocking (a campaign delays OTPs), and strict ordering isn't needed across users. Use separate topics/queues per priority (and per channel), partition by user for per-user ordering only if required, and isolate worker pools.

**24.** Fixed blocks fail on insertions and deletions near the front: inserting 100 B shifts all following boundaries and re-uploads 100% of the file (lab: 20 MB) versus 0.25% with CDC. Use content-defined chunking (with larger average sizes if metadata overhead matters).

**25.** One queue with blocking workers causes head-of-line blocking behind popular hosts (lab: 14 pages/s at 500 threads vs 1,335/s with per-host queues). Use per-host queues and a ready-time scheduler with asynchronous I/O.

**26 (sketch).** Don't fan out celebrity posts; keep the post in a hot per-author cache; at read time merge the user's precomputed timeline with recent posts of the few celebrities they follow (k-way merge by ID); push to active followers only for ordinary accounts; measure fan-out lag; the threshold (e.g., 100 K followers) bounds worst-case fan-out to ~1 s at 100 K inserts/s.

**27 (sketch).** Separate priority classes with their own queues and worker pools (bulkheads); strict priority for OTP/transactional; rate-cap bulk (e.g., 2,000/s) leaving headroom; per-provider rate limits; monitor age of oldest message per class; alert on OTP p99.

**28 (sketch).** The client splits files into content-defined chunks, hashes them, asks the server which hashes are missing, uploads only those, and commits a new manifest; because chunk boundaries depend on content, an edit changes only the neighboring chunk(s), so a 100-byte insertion in a 20 MB file moved 50 KB instead of 20 MB in the lab.

**29.** In a book, fixed chunking cuts every 10 pages: insert a page at the start and every later 10-page group changes. Rolling-hash chunking cuts at chapter boundaries (decided by the content near the cut): inserting a page affects only its own chapter, so the other chapters still match the copies already stored.

---

# Chapter summary

- **News feed:** the follower distribution decides the design (median 76, mean 200, max 500 K; top 1% own 34% of edges). Pure push = 15.25 B inserts/day (2× the average-based estimate) with 5 s worst fan-out; pure pull = 200 lookups per read (4.6 M/s); **hybrid with a ~100 K threshold halves writes (87 K/s), caps fan-out at ~1 s, and needs only ~13 lookups per read**. Timelines are caches of post IDs; reads are cursor-paginated; deletes are filtered at read.
- **Notifications:** priorities and isolation: with a single FIFO an OTP waits a median 199 s (up to 399 s) behind a 2 M-message campaign; priority queues cut it to 0; a bulk rate cap preserves 2,500 msg/s of headroom at 2.2× campaign time. At-least-once + idempotency keys; policy before send; provider failover; scheduling and chunked campaigns.
- **Photo sharing:** a bandwidth/storage problem (417 Gbit/s peak egress, 128 PB/year, 1.9 EB retained at 3×); async pipeline (thumbnail first, EXIF strip, moderation), immutable keys + tiered CDN, erasure coding + tiering (~$38 M → ~$12 M/month at illustrative prices).
- **File sync:** content-addressed chunks + manifests + journal/cursor + CAS commits. **CDC re-uploads 0.25% where fixed chunks re-upload 100%** after a 100-byte insertion (and 0.17% vs 75% after a deletion), at the cost of ~38% more chunks. Conflicted copies, GC of orphan chunks, dedup privacy.
- **Crawler:** politeness is the constraint. A global FIFO with blocking fetchers reached **0.7-2.9%** of the achievable 1,335 pages/s even with 5,000 threads, while per-host queues + a ready-time scheduler hit **100% with 500**. Partition by host; Bloom filter for URL-seen (120 GB vs 800 GB per 100 B URLs); normalization, traps, SimHash.
- **Typeahead:** precomputing top-10 per prefix gave ~0.2 µs mean vs 142 µs mean (1.3 ms p99) for range-scan, for ~50 MB of index at 1.5 M queries; edge caching of short prefixes; batch + real-time layers; atomic snapshot swaps; abuse and safety.
- **Patterns:** fan-out dial, bulkheads, content addressing, rolling hashes, ready-time schedulers, precompute vs compute, journal+cursor, hybrid thresholds, immutability + CDN, Bloom filters, idempotent consumers, lifecycle tiering.

---

# Flashcards

| Front | Back |
|---|---|
| Feed fan-out options | Pull (200 lookups/read), push (15 B inserts/day), hybrid |
| Why averages mislead feeds | Popular accounts have more followers *and* post more: 2× the naive estimate |
| Celebrity threshold | ~100 K followers: halves push volume, 13 lookups/read, ≤1 s worst fan-out |
| Timeline content | Post IDs, capped (800), active users only; cache not truth |
| Notification priorities | Separate queues + worker pools; OTP wait 199-399 s in a shared FIFO vs 0 |
| Bulk rate cap | Leaves 2,500 msg/s headroom; campaign 2.2× slower |
| Exactly-once sends | Impossible; at-least-once + idempotency key + provider token |
| Photo scale | 417 Gbit/s egress, 128 PB/yr, 1.9 EB at 3× / 0.9 EB at 1.4× EC |
| Pipeline | PENDING → THUMB_READY → READY; strip EXIF; moderate; immutable CDN keys |
| CDN origin load | Each hit-ratio point ≈ 4.2 Gbit/s of origin at 417 Gbit/s |
| Chunk manifest | Ordered list of chunk hashes = a file version |
| Fixed vs CDC | 100% vs 0.25% upload after inserting 100 B at the start |
| Sync protocol | Missing-chunk query → upload chunks → CAS commit → journal → cursor pull |
| Conflicts | CAS on base_version; save "conflicted copy" |
| Dedup risk | Hash-existence side channel; require proof of possession |
| Crawler politeness | 1 fetch/s/host; ceiling = Σ min(host URLs, time) |
| Frontier | Front queues (priority) + per-host back queues + ready-time heap |
| Global FIFO crawler | 0.7-2.9% of max (blocking); per-host scheduler: 100% |
| URL-seen Bloom | 120 GB (1% FPR) vs 800 GB exact per 100 B URLs |
| Partition crawler | By host hash so politeness stays local |
| Typeahead index | Precomputed top-10 per prefix: 0.2 µs vs 142 µs mean scan |
| Typeahead freshness | Batch snapshot every ~10 min + real-time trending layer; atomic swap |

# Glossary additions

| Term | Simple Meaning | Technical Meaning | Why It Exists | Example |
|---|---|---|---|---|
| Fan-out on write / read | Do work at post time / read time | Push entries to follower timelines vs merge at read | Balance write and read cost | Hybrid feed |
| Celebrity problem | Huge accounts break push | Heavy-tailed follower distribution | Motivates hybrid | 500 K followers |
| Hydration | Fetch full objects from IDs | Multi-get of post bodies for timeline IDs | Keep timelines small | Post cache |
| Bulkhead | Isolated resource pools | Separate queues/workers per class | Prevent starvation | OTP vs marketing |
| Frequency cap | Limit per user per period | Counter-based send limit | Avoid spam | 3 marketing/day |
| Perceptual hash | Image fingerprint | Hash robust to resizing/compression | Near-duplicate and abuse detection | pHash |
| Erasure coding | Parity-based durability | k data + m parity fragments | ~1.4× overhead vs 3× | 10+4 |
| Content-defined chunking | Content-dependent split points | Rolling hash boundary rule | Stable chunks after shifts | Gear/Rabin |
| Manifest | List of chunk IDs | File version description | Delta sync, versioning | Chunk hashes |
| Journal / cursor | Change log + position | Append-only per-namespace log, monotonic cursor | Incremental sync | `after=9137` |
| Conflicted copy | Preserved divergent version | Result of a failed CAS commit | No silent data loss | "file (conflicted copy)" |
| URL frontier | Crawl queue | Priority + per-host politeness queues | Politeness at scale | Mercator |
| Crawl delay | Min gap between fetches to a host | `robots.txt` / adaptive rate limit | Be polite | 1 s |
| SimHash | Similarity fingerprint | Locality-sensitive 64-bit hash | Near-duplicate detection | Hamming ≤ 3 |
| Top-k per prefix | Precomputed suggestions | Index mapping prefix to best completions | O(1) typeahead | Trie node list |
| Debounce | Wait for typing pause | Delay requests until input settles | Fewer requests | 150 ms |
| Time decay | Down-weight old counts | Exponential half-life on frequencies | Freshness | 7-day half-life |

---

**Batch 7 complete after this chapter** (Ch 24, 25, 26). **Next (when you resume): Chapter 27, Problem Book III (upper-intermediate):** YouTube-style video, WhatsApp-style messaging at scale, Uber-style matching, Ticketmaster-style ticketing, Yelp-style proximity search, an ad-click aggregator, and a telematics ingestion platform tied to your embedded background.

# Chapter 0: What Is System Design? Thinking in Systems

## Chapter card

| | |
|---|---|
| **Objective** | Build the core mental model: apps are the visible tip of systems; scale, failure, and change force architecture; system design is requirement-driven decision-making with trade-offs |
| **Why this chapter exists** | Every later chapter is an answer to "what breaks when this grows or fails?" You must feel that question first, or the tools (caches, queues, shards) feel like arbitrary jargon |
| **Prerequisites** | None. Assumes you can program |
| **Study time** | 4–6 hours including exercises |
| **Tier** | `[MK]` throughout |
| **Mastery criteria** | (1) For any app, say what runs on the device vs. servers. (2) Walk through the *evolution* of a small app to a large one, naming each bottleneck and fix. (3) Distinguish functional vs. non-functional requirements and use them to justify a decision. (4) Name three things that break at 1000× scale. (5) Explain why "just write better code" isn't enough |

---

## 0.1 The Story: Arjun's Photo App

Arjun is a skilled programmer. One weekend he builds **SnapShare**, a photo-sharing app. Here is everything he builds:

- A mobile app where you take a photo and add a caption.
- A program on his **laptop** that receives photos, saves them in a folder, and lets friends see them.

He gives it to 20 friends. It works beautifully. Over the next few chapters of this book we'll follow SnapShare as it grows. At each stage you'll predict what breaks.

### Stage 0: 20 users
```text
 Friend's phone ──Wi-Fi/Internet──▶ Arjun's laptop (program + photos in a folder)
```
Everything works. There's no system design here, just a program.

### Pause & Predict 0.1
> SnapShare goes viral. Over one weekend: **5 million** people sign up. List, in order, what you think will break first, second, and third. Be concrete: *what resource runs out? what does the user see?* Write it down. (Answers at the end of §0.5, but don't peek.)

---

## 0.2 What actually happens when you use an app

Before breaking things, let's be sure we understand how a normal app works.

### The airplane-mode experiment
Turn on airplane mode, then try:
1. **Calculator**: 7 × 8 → works.
2. **WhatsApp**: send "hi" → clock icon; old chats are visible.
3. **YouTube**: new videos won't load; maybe a downloaded one plays.
4. **Google Maps**: the map you've already viewed may show; new search fails.

Why? Because each app splits its work between **your device** and **somewhere else**:

| App | On your device | Somewhere else |
|---|---|---|
| Calculator | Everything | Nothing |
| WhatsApp | UI, local copy of past chats, encryption keys | Message relay, storing messages until delivered, tracking delivery |
| YouTube | Player, buffer of the next seconds of video | Millions of videos, recommendations, accounts |
| Google Maps | Rendering, cached tiles | Map data, routing, live traffic, search |

> **Insight 1.** Most apps you use are *windows* into systems that live elsewhere. The app on your phone is a small fraction of the whole.

### The vocabulary of "elsewhere" (intuition first)

- **Client**: the program that *asks*. WhatsApp on your phone; a browser; a telematics box in a truck. Clients can be tiny devices with no screen.
- **Server**: a program, running on some computer, that *waits for requests and answers them*. "Server" is a **role**, not special hardware. (Your laptop is a server the moment it runs a program that listens for requests.)
- **Network**: whatever carries messages between them (Wi-Fi, mobile data, fibre, undersea cables).
- **Request**: client → server: "please do X."
- **Response**: server → client: "here is the result / it failed."
- **Data**: the information the system exists to manage. Often more valuable than the code.

**Can a program be both client and server?** Yes. A web server answering your browser is a *server* to the browser and a *client* to its database.

### What a message looks like in the simplest case

```text
 Your phone                    WhatsApp's computers                 Friend's phone
┌───────────┐  ① "send hi     ┌─────────────────┐  ③ "new message   ┌───────────┐
│  client   │ ───to Priya" ─▶ │     server      │ ──for you" ─────▶ │  client   │
│           │ ◀─ ② "got it" ─ │                 │ ◀─ ④ "received" ─ │           │
└───────────┘     ✓ (1 tick)  └─────────────────┘                   └───────────┘
                                                      (you see ✓✓)
```
*(Educational approximation, not WhatsApp's real internals; tick semantics are public app behavior.)*

Why does each arrow exist?
- **①** Phones move between networks, change addresses, and are often off. A fixed, always-on meeting point (the server) solves this.
- **②** You need confirmation that the message is safe somewhere other than your own phone. One tick = "the server has it."
- **③** The server delivers when it can reach your friend (possibly hours later).
- **④** Delivery confirmation relayed back (two ticks).

---

## 0.3 Software, systems, and architecture

**Software** = instructions a computer follows. **Application** = software packaged for a human task.

A **system** is a set of parts that work together to achieve a goal, where **the useful behavior emerges from the interactions**, not from any one part. "WhatsApp" isn't the app, or the server, or the database. It's all of them plus the network plus the operations practices keeping them alive.

**Architecture** = the decisions about *which parts exist, what each is responsible for, how they connect, and why*, made *before* (and while) building.

**Analogy: a house.** An architect decides the rooms, where the plumbing and electricity run, and which walls bear weight, before bricks are laid. Move a load-bearing wall later and everything cracks. In software, **code is bricks; architecture is the floor plan.** Architecture decisions are expensive to reverse.

**System design** = the process of deriving an architecture from *requirements* and *constraints*.

> A system designer repeatedly asks: *What parts do we need? What is each responsible for? How do they communicate? Where does the data live? What happens at 10× or 100× load? What happens when a part breaks? What does it cost? How will we know it's healthy?*

---

## 0.4 Why can't we just write better code?

This is the most common beginner objection, so let's take it seriously.

### Hard limits of one computer

A single server, no matter how clever the code, has hard ceilings:

1. **Finite compute.** A CPU does a finite number of operations per second. Better algorithms help, but with diminishing returns. If one request costs 5 ms of CPU, one core serves at most 200 requests/second, ever.
2. **Finite memory.** RAM holds only so much.
3. **Finite storage.** Disks fill.
4. **Finite network capacity.** A network card moves a bounded number of bits per second.
5. **One location.** Light in fibre travels ~200,000 km/s. Mumbai ↔ US East Coast is ~13,000 km, so a round trip takes ≥ ~130 ms physically, typically 200+ ms. **No code can beat physics.**
6. **It will fail.** Disks die, power flickers, someone trips a cable, a kernel panics, a data center loses cooling. A dead machine runs no code.

### Why more code doesn't fix #5 and #6
No algorithm makes a Brazil→Japan round trip faster, or keeps a powered-off machine running. These are *architecture* problems: **how many machines, where, doing what, with what redundancy.**

> **The core sentence of this chapter.** *Code decides what a computer does. Architecture decides how many computers there are, where they are, and what each one is responsible for. Problems of scale and reliability are mostly the second kind.*

### Code vs. architecture: a quick comparison
| Problem | Code fix | Architecture fix |
|---|---|---|
| A page takes 800 ms because of an inefficient loop | ✔ Fix the loop | |
| A page takes 800 ms because users are 12,000 km from the server | ✘ | ✔ Put servers/caches near users (CDN) |
| The database CPU is at 100% from 50,000 identical reads/s | Partly (optimize queries) | ✔ Cache results; add read replicas |
| The single server crashes at 3 AM | ✘ | ✔ Redundant servers + automatic failover |
| Uploading videos blocks web requests for minutes | Partly | ✔ Move work to background queues |

Good system designers still write good code, but they know which class of problem they're facing.

---

## 0.5 The Evolution of SnapShare (the heart of this chapter)

Let's grow SnapShare and watch problems appear. **This evolution is the skeleton of the entire book.** Later chapters teach each fix in depth. For now, just see the *pattern*: *solution → new problem → new solution*.

### Answers to Pause & Predict 0.1 (5 M users hit the laptop)
Order may vary, but typically:
1. **Network/CPU saturation.** The home internet connection (maybe 100 Mbit/s up) can't serve even a fraction of the requests. The laptop's CPU can't handle thousands of concurrent connections. Users see timeouts and spinners.
2. **Disk fills / disk I/O saturates.** 5 M users × photos at ~2 MB: even 1 photo each is 10 TB. A laptop disk holds ~1 TB. Meanwhile one disk can only perform a limited number of reads/writes per second.
3. **Single point of failure.** The laptop sleeps, the Wi-Fi drops, the power goes out → 100% of users get *nothing*. There is no second copy of any photo; one disk failure destroys everything permanently.
Also: data loss risk, security (anyone on the internet now hits a home machine), and no way to update software without downtime.

### Stage 1: Move to a real server in a data center
```text
Phones ─▶ [ One cloud server: program + database + photo files ]
```
Fixes: reliable power, fast network, more disk. **Works for ~thousands of users.**
New problems appear as load grows: the single machine's CPU, RAM, and disk are shared by *everything*.

### Stage 2: Separate responsibilities
```text
Phones ─▶ [App server] ─▶ [Database server]      photos → [Object storage]
```
Why: the database needs lots of RAM and fast disks; the app needs CPU; photos need cheap, huge storage. **Separating by job lets each be sized and scaled independently**. This is the first real "architecture" move.
New problem: the **app server** is now a bottleneck and a single point of failure.

### Stage 3: Many app servers + a load balancer
```text
Phones ─▶ [Load Balancer] ─┬▶ [App 1]
                           ├▶ [App 2] ─▶ [Database]
                           └▶ [App 3]
```
Why: spread requests; survive one app server dying.
New problem: **every request still hits the database**, and the same popular photos' metadata are fetched over and over.

### Stage 4: Add a cache
```text
App servers ─▶ [Cache (RAM)] ─miss─▶ [Database]
```
Why: repeated reads served from fast memory.
New problems: **stale data** (cache disagrees with DB), and the *database itself* still has a single machine's limits on writes and total data.

### Stage 5: Replicate and shard the database
```text
              ┌─▶ Primary (writes) ─▶ Replica ─▶ Replica (reads)
App servers ──┤
              └─ many shards for huge data: Shard A | Shard B | Shard C
```
Why: replicas multiply read capacity and give redundancy; shards split data and writes across machines.
New problems: **replication lag** (a replica may be behind), **cross-shard queries**, **rebalancing**, more complexity.

### Stage 6: Content Delivery Network (CDN) for photos
```text
Phones ◀── nearest CDN edge ◀── Object storage (origin)
```
Why: photos are big and static; serving them from servers near users cuts latency and your bandwidth bill.
New problem: **cache invalidation at the edge**, cost, and private content leaking if misconfigured.

### Stage 7: Background work with queues
```text
Upload ─▶ App ─▶ [Queue] ─▶ Workers: resize, scan for abuse, notify followers
```
Why: don't make users wait for slow work; absorb spikes.
New problems: **duplicates, ordering, retries, eventual consistency** ("my photo uploaded but the thumbnail isn't there yet").

### Stage 8: Many regions, many teams, many services
```text
Users in India/EU/US ─▶ nearest region; services owned by different teams
```
Why: latency, resilience to a whole data center failing, organizational scale.
New problems: **distributed systems theory begins**: partitions, consistency vs. availability, distributed transactions, clocks.

### Stage 9: You can't see what's wrong
Observability, security, cost control, deployments, on-call. Production concerns become the main work.

### The evolution on one page

```text
Laptop
  ↓ out of capacity / reliability
Single cloud server
  ↓ one box does everything
Separate app / DB / file storage
  ↓ app tier is a bottleneck + SPOF
Many app servers + load balancer
  ↓ database overloaded by repeated reads
Caching
  ↓ DB reads still too many; writes/data too large for one machine
Replication + sharding
  ↓ big static files slow, far away, expensive
CDN + object storage
  ↓ slow tasks block users; spikes overwhelm
Queues + async workers
  ↓ users and failures are global
Multi-region + distributed-systems problems
  ↓ can't see, can't secure, can't afford
Observability + security + cost management
```

> **Insight 2.** System design is *evolution under pressure*. Each component exists because something broke without it, and each introduces new problems. If you can explain the chain, you understand the field; if you can only list components, you've memorized it.

### Important caveat: don't over-evolve
Every stage adds complexity, cost, and failure modes. A 5,000-user app does *not* need Stage 8. Many successful products ran for years on a big database, a few app servers, and a cache. **Good design means applying the minimum architecture that meets the requirements, with a clear path to evolve.** Premature scale-engineering is a real and expensive failure mode.

---

## 0.6 What does "large" actually mean? The dimensions of scale

A system is "large" when *any* of these exceed what one machine/location/team can handle:

| Dimension | Meaning | Examples |
|---|---|---|
| **Users** | How many people | 100 → 100 M |
| **Traffic** | Requests per second | Average vs. peak (New Year's Eve; a goal in the World Cup final) |
| **Data volume** | How much is stored | MB of chats vs. PB of video |
| **Data velocity** | How fast data arrives | 200,000 sensor records per second |
| **Geography** | Where users are | One city vs. all continents |
| **Reliability demand** | Cost of downtime | Blog vs. payment network |
| **Complexity** | Features/teams | One developer vs. 500 engineers |
| **Change rate** | How often it's modified | Quarterly release vs. 100 deploys/day |

Real systems stress different dimensions: a bank statement system is *correctness*-heavy but not huge; a video platform is *data-volume*- and *bandwidth*-heavy; a telematics platform is *velocity*- and *ingest*-heavy; a chat app is *connection*- and *fan-out*-heavy. **Identifying which dimension dominates tells you which tools matter.**

---

## 0.7 Requirements: the real starting point of every design

Beginners start with boxes ("we need Kafka!"). Designers start with **requirements**. Why? Because the *same product* can have wildly different correct designs depending on requirements.

### Functional requirements (FRs): *what* the system does
Features and behaviors: "Users can upload photos," "Users can follow others," "Show a feed."

### Non-functional requirements (NFRs): *how well* it does it
| NFR | Question | Example target |
|---|---|---|
| **Scale** | How many users/requests/data? | 10 M DAU, 5 k RPS peak |
| **Latency** | How fast? | Feed loads p99 < 300 ms |
| **Availability** | How much uptime? | 99.95% |
| **Consistency** | How fresh/correct must data be? | A "like" may be seconds late; a payment may not |
| **Durability** | Can data ever be lost? | Photos: never. Typing indicators: who cares |
| **Cost** | Budget? | $X per million requests |
| **Security/Privacy** | Who may access what? | GDPR, encryption |
| **Maintainability** | Can the team evolve it? | Deploy daily |
| **Compliance/Regulatory** | Legal constraints | Data residency in EU |

### Why NFRs drive architecture: an example
"Store a user's *like* on a post":
- If it's fine to lose 0.1% of likes and show counts a few seconds late → queue + eventual consistency + approximate counters (cheap, scalable).
- If it's a *bank transfer* → ACID transaction, synchronous replication, idempotency, audit log (expensive, careful).
Same verb ("store a number"), totally different designs. **NFRs are the design's steering wheel.**

### Hidden requirements
Ask about: read:write ratio, peak vs. average, data retention, growth rate, geographic spread, failure tolerance, who the users are (humans? devices?), abuse/adversaries, regulatory constraints, and *what the business will do if this is down for an hour*.

### Practice: classify
For "ride-hailing trip request": FR = rider requests a trip; NFRs = match within ~2 s, driver location freshness ~ seconds, availability very high, payment correctness absolute, locations may be slightly stale, geographic partitioning natural.

---

## 0.8 The three big forces: Scale, Reliability, Change

All of system design is the management of three forces:

1. **Scale**: do more *work* (users/data/traffic).
2. **Reliability**: keep working *despite failures*.
3. **Change**: keep evolving *without breaking* (features, deployments, team growth).

Often they pull against each other: more scale and reliability mean more components, which means more complexity, which makes change harder. **Complexity is the tax you pay.** A great design spends complexity only where requirements demand.

Additional forces that show up constantly: **cost**, **latency**, **security**.

---

## 0.9 Components: the toolbox preview (a map, not yet the lessons)

You'll learn each in depth later. Here's what problem each family solves, so the words don't feel alien:

| Family | One-line purpose | Chapter |
|---|---|---|
| **Load balancer** | Spread traffic over many servers; hide failures | 7 |
| **Cache** | Remember answers to avoid repeated work | 8 |
| **Database** | Durable, queryable data | 3, 4 |
| **Replica** | Copy of data for reads and redundancy | 9 |
| **Shard** | Slice of data on its own machine to scale beyond one | 10 |
| **CDN** | Serve static content from nearby | 11 |
| **Object storage** | Cheap, huge storage for files | 11 |
| **Queue / stream** | Buffer and decouple work over time | 12, 13 |
| **API gateway** | Single front door: auth, routing, limits | 7, 19 |
| **Service** | A program owning a capability | 19 |
| **Coordination service** | Agreement among machines (leaders, locks, config) | 16 |
| **Monitoring** | Know what's happening | 21 |

---

## 0.10 A first look at real systems (high level)

For each, we separate *what users do* from *what the back end must do*. **[Approximation]** throughout; real internals are proprietary or change.

### WhatsApp
Users: send/receive messages, groups, media. Back end: connection servers keep persistent connections to phones; relay and store-until-delivered; media via object storage + CDN; notifications for offline phones; presence; end-to-end encryption. Dominant dimensions: **connections, fan-out, latency, durability** of undelivered messages.

### YouTube
Users: upload, watch, search, comment. Back end: upload pipeline → transcoding into many qualities → chunked storage → CDN delivery with adaptive bitrate; metadata DB; search; recommendations; comments; ads; analytics. Dominant dimensions: **data volume, bandwidth, global distribution**.

### Google Search
Users: type a query, get ranked results in < 0.5 s. Back end: crawlers fetch the web; indexers build an *inverted index* (word → documents); ranking; serving across thousands of index shards (scatter-gather); heavy caching. Dominant dimensions: **data volume, read latency, relevance**.

### Amazon
Users: browse, search, cart, pay, track. Back end: catalog (read-heavy, cached), search, cart (must never lose it), inventory (contention!), orders and payments (correctness), fulfillment workflows, recommendations. Dominant dimensions: **correctness under contention, availability**.

### Uber
Users: request ride; drivers share location. Back end: constant location ingestion; geospatial index; matching service; pricing; trip state machine; payments; ETA. Dominant dimensions: **real-time ingestion, geo-queries, latency**.

### Instagram
Users: post photos, follow, see feed, stories. Back end: media storage + CDN; follow graph; feed generation (fan-out); notifications; ranking. Dominant dimensions: **read-heavy feed, media bandwidth, fan-out to followers**.

### A URL shortener
Users: paste a long URL, get a short one; visiting it redirects. Back end: mapping store (short → long), ID generation, cache, analytics. Dominant dimension: **read-heavy lookups; availability of redirects**.

### A fleet telematics platform (relevant to your background)
Devices: in-vehicle units stream GPS and engine data over cellular. Back end: ingest gateways, durable stream, processing for alerts, live state, time-series storage, reporting. Dominant dimensions: **velocity (ingest rate), intermittent connectivity, late/duplicate/out-of-order data, multi-tenancy, regulatory accuracy**.

**Exercise for later:** pick two of these and write three paragraphs: users/actions, back-end responsibilities, dominant scale dimension.

---

## 0.11 The System Designer's Thinking Loop (first version)

You'll refine this over the book. For now, memorize the shape:

```text
1. Understand users and goals            (who, doing what, why)
2. Pin down requirements                 (functional + non-functional)
3. Estimate scale                        (numbers, not adjectives)
4. Sketch the simplest thing that works  (a baseline)
5. Find the bottleneck                   (what breaks first at this scale?)
6. Fix it with a component               (and name the trade-off)
7. Repeat 5–6 until requirements met
8. Ask "what if X fails?" for each part
9. Make it observable, secure, affordable
10. Explain decisions and alternatives
```

Notice this mirrors the SnapShare evolution. Step 4 prevents over-engineering; step 5 prevents guesswork; step 8 is failure-first thinking.

---

## 0.12 Mental model recap

| Concept | Mental model |
|---|---|
| App vs. system | **A window vs. the building behind it** |
| Server | **A role, not a machine**: "the one waiting to be asked" |
| Architecture | **The floor plan**, hard to change after construction |
| Scale | **A road: widen it (vertical), add more roads (horizontal), but the destination may still be far (latency)** |
| Failure | **Weather**: not "if" but "when"; build for it |
| Requirements | **The steering wheel** of the design |
| Complexity | **A tax** you pay only when requirements demand |

---

## 0.13 Common misconceptions (fix these now)

1. **"System design is advanced coding."** No: it's deciding *which machines do what*. Code is inside each box.
2. **"Large scale means lots of users."** Scale has many axes: data, velocity, geography, reliability.
3. **"More components = better design."** Each component is a liability. Fewer is better until requirements force more.
4. **"A server is special hardware."** It's a role.
5. **"The cloud is infinite and failure-free."** It's rented computers that fail, with real limits and a bill.
6. **"There's one correct architecture."** There are good designs for given requirements, with trade-offs.
7. **"Memorizing famous diagrams = skill."** The skill is deriving diagrams from requirements.
8. **"If it works for 100 users it'll work for 100,000."** Almost never unchanged.
9. **"Premature optimization isn't a concern at the architecture level."** It is. Premature distribution is *expensive* to build and to unwind.

---

## 0.14 Chapter summary

- Most apps are the visible tip of a system: clients talk over a network to servers that hold data and logic.
- A **system** is parts whose interactions produce behavior; **architecture** is the plan of parts, responsibilities, connections, and reasons; **system design** derives it from requirements.
- One machine has hard limits (compute, memory, storage, network), a fixed location (physics), and will fail. Architecture, not code, addresses these.
- Systems **evolve**: each solution (load balancer, cache, replica, shard, CDN, queue, multi-region) fixes one bottleneck and creates a new problem.
- **Requirements**, especially non-functional ones, drive every decision.
- Scale has many dimensions; identify which one dominates.
- Apply the minimum complexity needed, with a path to evolve.

---

## 0.15 Code lab: feel the limits of one machine (Java)

This tiny experiment makes "one machine has finite capacity" tangible. You'll measure how requests-per-second changes when each request does a bit of work and when many run concurrently.

```java
import java.util.*;
import java.util.concurrent.*;

public class OneMachineLimits {
    // Simulate "work" for one request: burn CPU for a given number of iterations.
    static volatile long sink;                       // prevents the JIT from deleting the loop
    static long burn(long iterations) {
        long x = 0;
        for (long i = 0; i < iterations; i++) {
            x += i ^ (i >>> 3);
        }
        return x;
    }

    public static void main(String[] args) throws Exception {
        int cores = Runtime.getRuntime().availableProcessors();
        System.out.println("CPU cores available: " + cores);

        long workPerRequest = 5_000_000;             // tune so one request takes a few ms

        // 0) Warm up: the JVM compiles hot loops after a few runs; measuring cold code is misleading.
        for (int i = 0; i < 30; i++) sink = burn(workPerRequest);

        // 1) Measure single-request cost (average of several runs)
        int runs = 20;
        long t0 = System.nanoTime();
        for (int i = 0; i < runs; i++) sink = burn(workPerRequest);
        double ms = (System.nanoTime() - t0) / 1e6 / runs;
        System.out.printf("One request CPU cost ~ %.2f ms -> max ~%.0f req/s per core%n", ms, 1000.0 / ms);
        System.out.printf("Predicted max throughput on %d core(s): ~%.0f req/s%n", cores, cores * 1000.0 / ms);

        // 2) Throughput with varying thread counts, fixed total requests
        int totalRequests = 400;
        TreeSet<Integer> threadCounts = new TreeSet<>(List.of(1, 2, cores, cores * 4));
        for (int threads : threadCounts) {
            ExecutorService pool = Executors.newFixedThreadPool(threads);
            CountDownLatch done = new CountDownLatch(totalRequests);
            long start = System.nanoTime();
            for (int i = 0; i < totalRequests; i++) {
                pool.submit(() -> { sink = burn(workPerRequest); done.countDown(); });
            }
            done.await();
            double secs = (System.nanoTime() - start) / 1e9;
            System.out.printf("threads=%-3d  throughput=%.1f req/s%n", threads, totalRequests / secs);
            pool.shutdown();
        }
    }
}
```
**What to observe and think about**
1. Throughput rises with threads *until you reach the number of cores*, then flattens. That's the **CPU ceiling**. More threads can't create more CPU. (On a single-core machine, such as a small cloud VM or container, you'll see it flat from the start. That is the lesson, too.)
2. To go beyond, you need **more machines** (horizontal scaling) or **cheaper requests** (caching, less work).
3. Predict first: with 8 cores and 5 ms per request, what is the max throughput? (≈ 1,600 req/s.) Compare with your measurement.

**Variation:** replace `burn` with `Thread.sleep(50)` (simulating waiting on a database/network, i.e., I/O). Now many more threads than cores *do* increase throughput, because threads are waiting, not computing. This distinction (CPU-bound vs. I/O-bound) returns in Chapters 1 and 5.

---

## 0.16 Exercises

### A. Conceptual (answer in your own words)
1. Why do Calculator and WhatsApp behave differently in airplane mode?
2. Is a "server" special hardware? Explain.
3. Give an example of a program that is simultaneously a client and a server.
4. Define *system*, *architecture*, *system design* in one sentence each, with a house analogy.
5. List four hard limits of a single computer. Which can code fix? Which can't?
6. Mumbai↔New York is ~12,500 km. Compute the *theoretical minimum* round-trip time if light in fibre travels at 200,000 km/s. Why is the real number higher?
7. What is the difference between a functional and a non-functional requirement? Give two examples of each for a chat app.
8. Name three dimensions of scale other than the number of users, with an example system for each.
9. Why is "more components" not automatically better?
10. Explain "each solution creates a new problem" using three consecutive stages of SnapShare's evolution.
11. Explain why the same feature ("store a number") can need totally different designs.
12. What is a single point of failure? Give two examples from SnapShare Stage 1–3.

### B. Estimation warm-ups (no jargon, just arithmetic)
13. SnapShare: 5 M users each upload one 2 MB photo. How much storage (in TB)? How many 1 TB disks?
14. 100 M page views per day. Requests per second on average? (Use 86,400 ≈ 10⁵.) If peak is 5× average, what's the peak?
15. A request costs 4 ms of CPU. How many requests/second can one core serve? A 16-core machine?

### C. Design thinking
16. **Offline friend:** You send a message to a friend whose phone is off for 8 hours. Describe where the message lives, what must be true about that place, and what happens when she turns the phone on. Draw it.
17. **Prediction:** For a to-do app used by 10 people, what's a sensible architecture? For one used by 10 million? Describe the minimal change in each case and justify.
18. **Classification:** For each, name the *dominant dimension of scale* and why: (a) a stock-trading app, (b) a weather-radar viewer, (c) a smart-thermostat fleet, (d) an e-book store.

### D. Debugging an architecture
19. A startup's diagram: *Mobile app → one VM running Node.js + MySQL + uploaded images on local disk.* They plan a TV ad that will bring 100× users. List ten things that could go wrong.

### E. Interview-style
20. "Explain what system design is to a non-technical friend in under a minute." Write your answer, then record yourself saying it.
21. "Walk me through how a simple app evolves as users grow from 100 to 100 million." Give a 3-minute answer using the evolution spine.

### F. Teach-back
22. Teach a friend "app = window, system = building" using an example *they* use daily.

---

## 0.17 Full answer key (with reasoning)

**1.** Calculator's instructions and data are entirely on the device; WhatsApp needs another computer (the server) to hold and forward messages to a friend whose phone isn't directly reachable. Past chats appear because the phone keeps a local copy.

**2.** No. A server is a role: a program that listens for requests and responds. Any computer can play it. In practice servers often run on hardware optimized for reliability, but nothing intrinsic differs.

**3.** A web application server is a *server* to browsers and a *client* of its database. A phone sharing its hotspot is a *server* (DHCP/NAT) for a laptop while being a *client* of the cell network.

**4.** System: components whose interactions produce behavior. Architecture: the plan for which components exist, their responsibilities and connections, and reasons. System design: deriving that plan from requirements. House: a system is the whole house; architecture is the blueprint; design is the architect's process of choosing the blueprint to fit the family's needs.

**5.** CPU throughput, RAM, disk capacity/I/O, network bandwidth, location (latency), fault likelihood. Code can improve CPU efficiency (algorithms), memory use, I/O patterns. Code *cannot* change physical distance, create more than the hardware offers, or prevent hardware failure; architecture handles those.

**6.** 12,500 km one way. Round trip distance 25,000 km ÷ 200,000 km/s = 0.125 s = **125 ms** minimum. Real RTT is higher: cables are not straight lines, routers add queuing/processing delays, there are multiple networks and protocol round trips (DNS/TCP/TLS before the first byte), congestion, etc.

**7.** FR: what it does ("send message", "create group"). NFR: how well ("deliver within 1 s", "99.99% available", "messages never lost", "end-to-end encrypted"). Chat app FRs: send 1:1 message; create group. NFRs: p99 delivery < 500 ms; durable until delivered.

**8.** Velocity (telematics: 200 k records/s), data volume (YouTube: petabytes), geography (a global SaaS requiring local latency), reliability (payments), connection count (chat: millions of persistent connections), fan-out (a celebrity's post to 50 M followers).

**9.** Each component adds cost, latency (network hops), failure modes, operational burden, and cognitive load. Components should be added only when a requirement can't be met without them.

**10.** Example: (Stage 3) many app servers + LB fixed the app-tier bottleneck → every request still hits the DB (new problem) → (Stage 4) cache fixes repeated reads → cache introduces staleness and doesn't fix write/data limits → (Stage 5) replicas/shards, which introduce lag and cross-shard complexity.

**11.** Because NFRs differ: a "like" can be eventually consistent, lossy, cheap; a bank balance needs ACID transactions, synchronous durability, audit. Design follows requirements.

**12.** A component whose failure takes down the whole system. Stage 1: the single server (app + DB + files); Stage 2: the single DB, the single app server; Stage 3: the single LB (unless redundant) and the single DB.

**13.** 5 M × 2 MB = 10 M MB = 10 TB (10⁷ MB = 10 TB). That's ~10 disks of 1 TB, ignoring replication/overhead. With 3× replication, 30 TB.

**14.** 100 M / 10⁵ = 1,000 req/s average; peak ≈ 5,000 req/s.

**15.** 1000 ms / 4 ms = 250 req/s per core (if purely CPU-bound); 16 cores ≈ 4,000 req/s (idealized; real is lower due to overhead, contention, I/O).

**16.** Your phone → server (message accepted, persisted, you see ✓). Server stores it in a durable "inbox" for her (survives restarts) keyed by her user ID. When her phone reconnects, the server pushes (or she pulls) pending messages; her phone acks; server marks delivered and notifies you (✓✓), then may delete or archive the message. Requirements of the storage: always-on, durable across crashes (and ideally replicated), can look up by recipient, preserves ordering, secure.

**17.** 10 people: one small server with a database; maybe a managed platform; backups. 10 million: LB + multiple stateless app servers, a replicated database (maybe sharded), cache, CDN for static assets, queue for background jobs, monitoring. Minimal change principle: only add components when numbers justify.

**18.** (a) Trading: latency + correctness (low-latency, consistency, ordering). (b) Weather radar: data volume/bandwidth and CDN/tile caching. (c) Thermostats: velocity and connectivity (many small devices, intermittent), security/OTA updates. (d) E-book store: read-heavy catalog, content delivery, relatively modest scale; licensing/DRM correctness.

**19.** Examples: single point of failure; disk fills; local disk uploads lost when the VM dies and can't be shared across servers; MySQL bottleneck; no replication/backups; CPU saturation; connection limits; slow images served from the app server; no monitoring; no autoscaling; deploys cause downtime; secrets in code; no rate limiting/DDoS protection; cache absent; sessions in memory; no failover; database writes blocked by long queries; thundering herd at ad time.

**20.** Sample: "System design is deciding how many computers an app needs, what each one does, and how they talk, so the app keeps working as more people use it and when parts break. It's like designing the floor plan of a building before constructing it."

**21.** Sample outline: one server → separate DB/files → multiple servers + LB → cache → replicas/shards → CDN → queues → multi-region → observability/security. For each: the problem, the fix, the new problem.

**22.** (Self-assessed.) A good teach-back names the visible app, the hidden servers, and shows what breaks without the hidden part.

---

## 0.18 Flashcards (add to your spaced-repetition deck)

| Front | Back |
|---|---|
| Client | The program that sends requests (browser, mobile app, in-vehicle device). |
| Server | A role: a program that listens for and answers requests; not special hardware. |
| System | Parts whose *interactions* create the useful behavior. |
| Architecture | Which parts exist, their responsibilities, connections, and the reasons. |
| System design | Deriving an architecture from requirements and constraints. |
| Why can't code alone solve scale? | Hardware limits, physical distance (latency), and inevitable failures are architecture-level problems. |
| Functional vs. non-functional requirement | What it does vs. how well it does it (scale, latency, availability, consistency, cost...). |
| Single point of failure | A component whose failure stops the whole system. |
| Dimensions of scale | Users, traffic, data volume, velocity, geography, reliability needs, complexity, change rate. |
| The SnapShare spine | Laptop → server → separate tiers → LB → cache → replicas/shards → CDN → queues → multi-region → observability. |
| Minimum-complexity principle | Add components only when requirements demand them. |
| Min RTT Mumbai–New York | ≈125 ms physics (real: ~200 ms+). |

## 0.19 Glossary additions

| Term | Simple Meaning | Technical Meaning | Why It Exists | Example |
|---|---|---|---|---|
| Software | Instructions | Programs directing hardware | Hardware needs instructions | Calculator |
| Application | Software for a human task | User-facing packaged software | Tools for people | WhatsApp |
| Client | The asker | Program issuing requests | Users interact through it | Browser |
| Server | The answerer | Program listening and responding (role) | Shared, always-on logic/data | API server |
| Network | Roads | Infrastructure moving data | Machines must communicate | Wi-Fi, fibre |
| Request/Response | Ask/answer | Paired messages | Interaction model | `GET /feed` → 200 |
| System | Parts working together | Interacting components | Behavior emerges from interaction | WhatsApp overall |
| Architecture | Floor plan | Components, responsibilities, connections, rationale | Decisions before building | LB → servers → DB |
| System design | Drawing the plan | Deriving architecture from requirements | Scale/failure aren't code problems | Design a feed |
| Scale | How big it gets | Growth in users/traffic/data/etc. | Limits appear | 1 M → 100 M users |
| Functional requirement | What it does | Feature behavior | Defines scope | "Upload photo" |
| Non-functional requirement | How well it does it | Quality attributes | Steers architecture | p99 < 300 ms |
| SPOF | Single point of failure | Component whose failure halts the system | Identify risks | One DB server |
| Bottleneck | The narrowest point | Resource limiting total capacity | Where to improve | DB CPU |

---

**Next: Chapter 1, Computers and Networks: The Physical Reality.** You'll learn the machines and wires every system runs on, with enough depth to reason about performance and failures.

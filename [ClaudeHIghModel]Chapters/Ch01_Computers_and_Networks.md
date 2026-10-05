# Chapter 1: Computers and Networks: The Physical Reality

## Chapter card

| | |
|---|---|
| **Objective** | Understand the machines and wires every system runs on well enough to *predict* performance and failure: CPU, memory, storage, operating system concurrency, I/O models, IP, DNS, TCP/UDP, HTTP/1.1/2/3, TLS, timeouts and retries |
| **Why this chapter exists** | Every performance and reliability story later ends with "because of CPU / memory / disk / network." Without this you will treat latency and failure as magic |
| **Prerequisites** | Chapter 0 |
| **Study time** | 8–12 hours including labs |
| **Tier** | Mostly `[MK]`; items marked `[GK]` / `[ADV]` as noted |
| **Mastery criteria** | (1) Quote the latency ladder from memory (ns → ms) and explain why each step is slower. (2) Explain CPU-bound vs I/O-bound and what changes in scaling. (3) Explain threads, race conditions, and the event-loop alternative. (4) Trace `https://example.com` from keypress to first byte with timings, including DNS/TCP/TLS. (5) Explain what timeouts, retries, and backoff do and why retries can be dangerous. (6) Reason about connection-level failure modes |

**A note for embedded engineers:** if you work on firmware, much of §1.2–1.3 will be familiar. Skim, but do not skip the *scaling implications* (what these limits mean for a server handling thousands of connections) and the *networking sections*, which explain what your devices' packets experience after they leave the modem.

---

## 1.1 The Story: "Why Is My API Slow?"

A team ships an endpoint, `GET /profile/42`. In testing on a laptop it returns in **8 ms**. In production, users in Singapore, whose server is in Virginia, report **900 ms**. The team's first instinct: "optimize the code." They profile; the code takes 6 ms. So where did the other ~890 ms go?

### Pause & Predict 1.1
> List every place time could be spent between a user tapping "open profile" in Singapore and the screen updating. Try to name at least eight. Then estimate the order of magnitude for each. (Answer in §1.9.)

By the end of this chapter you'll be able to account for those 900 ms: DNS lookups, TCP and TLS handshakes (each costing full round trips across 15,000 km), queuing, server work, response transfer slowed by TCP's congestion control, rendering, and more. And you'll know which of them *architecture* can reduce (move servers closer, keep connections warm, cache DNS, use CDN) versus code.

---

# PART A: THE COMPUTER

## 1.2 What's inside the machine

### 1.2.1 The problem
You're designing a service. How many requests per second can one machine handle? Why does "just add RAM" sometimes help and sometimes not? To answer, you need a mental model of what the hardware does.

### 1.2.2 The parts and why each matters

| Part | What it does | The one thing a system designer must remember |
|---|---|---|
| **CPU** (processor) | Executes instructions; has multiple **cores** (each runs one stream at a time, maybe 2 with hyper-threading) | Limits *compute* per second. A request needing 5 ms CPU caps one core at ~200 req/s |
| **CPU caches** (L1/L2/L3) | Tiny fast memories on the chip holding recently used data | Data locality matters; this is why arrays beat linked lists in practice |
| **RAM** (main memory) | Fast, volatile working memory | ~100 ns access; contents vanish on power loss; limited and expensive per GB |
| **Storage** (disk) | Persistent data | HDD (~ms), SSD (~tens–100 µs), NVMe (~10–100 µs). Survives power loss (if written properly) |
| **Network interface (NIC)** | Sends/receives packets | Bandwidth ceiling (1/10/25/100 Gbit/s); also a CPU cost to process packets |
| **Operating system (OS)** | Manages CPU, memory, disk, network for programs | Schedules threads; limits open files/sockets; caches disk pages in RAM |

**Mental model: a restaurant kitchen.**
CPU = chefs (how many dishes can be cooked at once). L1–L3 cache = ingredients on the chef's cutting board. RAM = the counter. Disk = the pantry down the hall. Network = a delivery truck to a warehouse in another city. The farther the ingredient, the longer the wait; and a chef who waits for the truck is idle.

### 1.2.3 The latency ladder `[MK]`

Memorize *orders of magnitude* (exact numbers vary by hardware):

| Operation | Approx. time | Relative to L1 (1 ns) | Analogy (if 1 ns = 1 second) |
|---|---|---|---|
| L1 cache reference | ~1 ns | 1× | 1 second |
| Branch mispredict | ~3–5 ns | | |
| L2 cache reference | ~4 ns | 4× | 4 seconds |
| Mutex lock/unlock (uncontended) | ~20–25 ns | | |
| Main memory (RAM) reference | ~100 ns | 100× | 1.5 minutes |
| Compress 1 KB (fast algo) | ~2–3 µs | | |
| Send 1 KB over a 1 Gbit/s network (transmission) | ~10 µs | | |
| Read 4 KB random from NVMe/SSD | ~20–150 µs | 20,000–150,000× | hours–days |
| Read 1 MB sequentially from RAM | ~10–50 µs | | |
| Round trip within a data center | ~0.5 ms | 500,000× | ~6 days |
| Read 1 MB sequentially from SSD | ~100–500 µs | | |
| HDD seek | ~5–10 ms | 5–10 M× | ~2–4 months |
| Read 1 MB sequentially from HDD | ~1–5 ms | | |
| Round trip, same continent | ~20–60 ms | | ~1–2 years |
| Round trip, cross-continent / ocean | ~100–250 ms | 100–250 M× | ~3–8 years |

*(Numbers are conventional rules of thumb, in the spirit of the widely cited "latency numbers every programmer should know". Treat them as orders of magnitude.)*

**Key lessons**
1. Each step *out* is roughly **10–1000× slower**. Network calls dwarf memory accesses.
2. **Sequential beats random**, especially on HDD: one seek buys you MBs of subsequent sequential reads. (Databases and Kafka exploit this heavily: append-only logs.)
3. **Batch** small operations: the *fixed* cost (a seek, a round trip, a handshake) dominates.
4. A "cheap" remote call (0.5 ms in-DC) is **5,000× slower than a RAM read**. A design with 100 sequential service calls per request has a 50 ms floor before any work.

### 1.2.4 Memory in more depth `[MK]`

- **Volatile**: RAM contents vanish on power loss or crash. Anything you only keep in memory is *at risk*. That's why caches are never the source of truth and why databases write to disk.
- **Virtual memory**: each process sees its own address space; the OS maps it to physical RAM, and may **swap** pages to disk when RAM is exhausted. *Swapping makes a server crawl* (RAM-speed becomes disk-speed). In production servers, you size memory so swap isn't used, or disable it.
- **Out-of-memory (OOM)**: the OS may kill a process when memory runs out. A leak or an oversized in-memory cache can make a healthy server die, which is a classic outage cause.
- **Garbage collection pauses** (Java, Go, etc.): the runtime occasionally pauses your program to reclaim memory. A node can *look dead* for hundreds of milliseconds, a subtle trigger of false failure detection later (Ch 14, 16).
- **Page cache**: the OS keeps recently read/written disk blocks in free RAM. Reading a file twice is often fast the second time. That's the OS acting as a cache. Databases rely on it (and sometimes bypass it).

### 1.2.5 Storage in more depth `[MK]`

| | HDD | SATA SSD | NVMe SSD |
|---|---|---|---|
| Mechanism | Spinning platters + moving head | Flash memory | Flash over PCIe |
| Random read latency | 5–10 ms | ~100 µs | ~20–100 µs |
| Random IOPS | ~100–200 | ~50–100 k | ~500 k–1 M+ |
| Sequential throughput | ~100–250 MB/s | ~500 MB/s | ~2–7 GB/s |
| Cost per GB | Lowest | Medium | Higher |
| Typical use | Cold/bulk data, archives, big sequential logs | General | Hot databases, low-latency |

- **IOPS** = I/O operations per second; **throughput** = MB/s. A workload of tiny random reads is IOPS-bound; a large file scan is throughput-bound. They are different bottlenecks.
- **Durability and `fsync`**: when a program "writes" a file, the OS usually just puts data in RAM (page cache) and writes it to disk later. If power fails first, data is lost. `fsync` forces data to stable storage. **A database commit that doesn't fsync isn't durable.** Durability costs latency: that's the central trade-off behind many database settings (and why write-ahead logs are *sequential appends followed by fsync*).
- **Write amplification and wear** (SSD): flash has limited write cycles; heavy random writes degrade it. (Relevant to LSM-tree vs B-tree design later.)
- **RAID, replication, erasure coding**: disks fail (annual failure rates of a few percent are common at scale `[Public: Backblaze publishes drive stats]`). Redundancy is mandatory, not optional.

**Failure modes of storage:** disk dies; sector corruption (silent bit-rot, so checksums matter); disk full (writes fail; often catastrophically for databases); I/O stalls (a failing disk can hang a machine without dying); wrong fsync assumptions after power loss.

### 1.2.6 Why this matters for system design (summary)
- **Compute-bound** work (video encoding, crypto, compression) scales by adding cores/machines.
- **Memory-bound** work (large caches, in-memory databases) scales by RAM size and partitioning.
- **Disk-bound** work (databases) scales by faster disks, caching, replicas, sharding.
- **Network-bound** work (streaming, large transfers) scales by bandwidth, CDNs, compression.
Your first question about any workload is *which resource saturates first?*

---

## 1.3 The operating system: processes, threads, concurrency

### 1.3.1 The problem
A web server must handle thousands of simultaneous clients. A CPU core runs one instruction stream at a time. How does one machine "serve many at once"?

### 1.3.2 Processes and threads `[MK]`

- **Program**: instructions on disk. **Process**: a program in execution, with its own isolated memory space, file descriptors, etc.
- **Thread**: an independent flow of execution *inside* a process. Threads of the same process **share memory** (heap, globals) but each has its own stack and registers.
- **Context switch**: the OS pauses one thread and runs another (saving/restoring registers, switching memory maps, polluting caches). It costs on the order of microseconds plus cache disruption. *Thousands of runnable threads waste significant CPU on switching and memory (each thread stack is ~0.5–1 MB virtual).*

| | Process | Thread |
|---|---|---|
| Memory | Isolated | Shared within the process |
| Creation cost | Heavier | Lighter |
| Communication | IPC (pipes, sockets, shared memory) | Direct memory access |
| Failure isolation | Crash affects only that process | A bug/crash can take down the whole process |

**Concurrency vs parallelism**
- **Concurrency**: *dealing with* many things at once (interleaved). Possible on one core.
- **Parallelism**: *doing* many things at once (simultaneous). Needs multiple cores.

### 1.3.3 Race conditions, locks, deadlocks `[MK]`

**Race condition**: the outcome depends on the unpredictable timing of threads.

```text
counter = 5
Thread A: read 5     Thread B: read 5
Thread A: write 6    Thread B: write 6      → final = 6, expected 7. One increment lost.
```
**Why this is foundational:** the same "lost update" shape reappears as database lost updates (Ch 3), cache races (Ch 8), double-booked tickets, and distributed races (Ch 14).

**Fixes:** mutual exclusion (locks/mutexes), atomic operations (compare-and-swap), immutability, message passing, or avoiding shared state.

**Deadlock**: Thread A holds lock 1 waiting for lock 2; Thread B holds lock 2 waiting for lock 1: both wait forever. Prevention: consistent lock ordering, timeouts, avoiding nested locks.

**Trade-offs of locking:** correctness vs. contention (threads waiting). Heavy locking serializes work and wastes cores (Amdahl's law: the serial part limits speedup).

### 1.3.4 CPU-bound vs I/O-bound `[MK]`

- **CPU-bound**: time is spent *computing* (hashing, encoding, JSON parsing at huge scale). More threads than cores doesn't help.
- **I/O-bound**: time is spent *waiting* (database, disk, network). The CPU is idle during waits, so you can run **many more concurrent tasks than cores**.

Most web services are **I/O-bound**: the handler spends 1 ms computing and 50 ms waiting for the DB. That's why a 8-core server can serve thousands of requests concurrently.

**Little's Law** `[MK]`: `L = λ × W`
- L = average number in the system (concurrent requests), λ = arrival rate (req/s), W = average time each spends in the system (latency).
- Example: 2,000 req/s × 0.1 s = **200 concurrent requests**. With a thread-per-request model you need ≥200 threads. If latency spikes to 1 s (a slow DB), concurrency becomes **2,000**, and your thread pool (say 200) exhausts, requests queue, timeouts follow. *This is how a slow dependency becomes a full outage.*

### 1.3.5 I/O models: how to wait efficiently `[MK/GK]`

**Blocking I/O, thread-per-connection**
```text
accept() → spawn thread → read() blocks until data → process → write() → ...
```
Simple, but each connection costs a thread (memory + context switches). At ~10,000 connections (the "C10K problem") it strains.

**Non-blocking I/O + event loop (readiness notification: epoll/kqueue/IOCP)**
One (or a few) thread asks the OS: "tell me which sockets are ready," then handles those and goes back. A single thread can manage tens/hundreds of thousands of idle connections. Used by Nginx, Node.js, Netty, Redis `[Public]`.

```text
loop:
  ready = epoll_wait()      # which sockets have data?
  for s in ready: handle(s) # must not block!
```
**Trade-offs:** event loops are very efficient for many mostly-idle connections but **any blocking or long CPU task stalls everyone** (a single slow handler delays all). They also make code harder (callbacks/async/await).

**Modern variants:** lightweight/virtual threads (Go goroutines, Java virtual threads) give thread-per-request *style* with event-loop-like efficiency.

**Why this matters for design**
- A **chat gateway** holding 1 M mostly idle WebSocket connections *needs* event-loop/lightweight-thread models plus OS tuning.
- A **CPU-heavy worker** (video encoder) is better with a process/thread pool sized to cores.
- Mixing them (long CPU work in an event loop) is a classic bug.

### 1.3.6 OS resource limits that bite in production `[MK]`
- **File descriptors**: every socket and file uses one. Default per-process limits (e.g., 1024) are far too low for servers (tune to 100k+).
- **Ephemeral ports**: a client making many outbound connections to one server is limited by ~28k–64k source ports per (source IP, dest IP, dest port) tuple. A proxy/LB hitting a backend hard can exhaust them (see TIME_WAIT, §1.5).
- **Memory per connection**, **thread stack size**, **max processes**.
- **CPU scheduler fairness** and **noisy neighbors** on shared hosts.

---

# PART B: THE NETWORK

## 1.4 Networking from scratch

### 1.4.1 The problem
Your phone must exchange data with a computer you've never met, 15,000 km away, across dozens of networks owned by different organizations, over wires that sometimes drop data. How can this possibly work reliably?

The answer: **layering**. Each layer solves one problem and hides the others.

### 1.4.2 Layers `[MK]` (practical 4-layer view)

```text
┌───────────────────────────────────────────────────────────────┐
│ Application  (HTTP, DNS, gRPC, MQTT, SMTP)   "what to say"    │
├───────────────────────────────────────────────────────────────┤
│ Transport    (TCP, UDP, QUIC)                "end-to-end      │
│                                               delivery"       │
├───────────────────────────────────────────────────────────────┤
│ Internet     (IP, routing)                   "which machine,  │
│                                               which route"    │
├───────────────────────────────────────────────────────────────┤
│ Link         (Ethernet, Wi-Fi, cellular)     "next hop on     │
│                                               this wire/air"  │
└───────────────────────────────────────────────────────────────┘
```
Each layer **wraps** the data from above with its own header (**encapsulation**):
```text
[Ethernet hdr][IP hdr][TCP hdr][ HTTP request bytes ........ ][Ethernet trailer]
```
**Why layering matters:** you can swap Wi-Fi for fibre without changing HTTP; you can change HTTP (1.1 → 2) without changing IP.

### 1.4.3 IP addresses, subnets, NAT `[MK]`

- **IP address**: identifies a network interface. IPv4: 32 bits (`203.0.113.7`); IPv6: 128 bits (`2001:db8::1`).
- **Public vs private**: private ranges (`10.0.0.0/8`, `172.16.0.0/12`, `192.168.0.0/16`) aren't routable on the public internet. Your home devices use private IPs.
- **NAT** (Network Address Translation): your router maps many private addresses to one public address, tracking connections by port. Consequence: the outside world can't directly initiate connections to your phone, so *phones must maintain outbound connections to servers* (why push notification services and persistent WebSockets exist).
- **CIDR notation**: `10.0.1.0/24` = first 24 bits fixed → 256 addresses. You'll see this in cloud VPC design (Ch 23).
- **Router**: forwards packets between networks using routing tables; **hop**: each router traversed. `traceroute` shows hops.

### 1.4.4 Ports and sockets `[MK]`
- **Port** (0–65535): identifies a *program/service* on a host. Common: 80 (HTTP), 443 (HTTPS), 22 (SSH), 53 (DNS), 5432 (Postgres), 6379 (Redis), 9092 (Kafka).
- **Socket**: an endpoint = (IP, port, protocol). A **TCP connection** is uniquely identified by the 4-tuple *(src IP, src port, dst IP, dst port)*. That's how one server port (443) serves thousands of clients simultaneously.

### 1.4.5 Packets, MTU, and why latency has components `[MK]`

Data is chopped into **packets**. Typical Ethernet **MTU** = 1500 bytes (≈1460 bytes of TCP payload). A 1 MB file ≈ ~700 packets.

**Four components of network delay (per hop)**
| Component | Meaning | Depends on |
|---|---|---|
| **Propagation** | Signal travel time | Distance (≈ 5 µs per km in fibre) |
| **Transmission** | Pushing bits onto the wire | Packet size ÷ link bandwidth |
| **Queuing** | Waiting in a router buffer | Congestion (variable! the source of jitter) |
| **Processing** | Router/NIC handling | Hardware |

- **Bandwidth** = capacity (bits/s). **Latency** = delay. **Throughput** = achieved rate. A fat pipe can still be slow (a big truck on a long road). *Bandwidth can be bought; latency is bounded by physics.*
- **Bandwidth-delay product (BDP)** = bandwidth × RTT = the amount of data "in flight." 100 Mbit/s × 100 ms = 10 Mbit ≈ **1.25 MB**. To fully use the link, the sender must have that much unacknowledged data outstanding (TCP window).
- **Packet loss** (wireless: ~0.1–5%+) triggers retransmissions and slows TCP drastically.
- **Jitter**: variation in latency (important for voice/video).

---

## 1.5 DNS: the internet's phone book

### 1.5.1 Problem → naive → why it breaks
- **Problem:** humans use names (`example.com`); routers use IPs.
- **Naive:** one giant file mapping names to IPs (indeed, the early internet's `HOSTS.TXT`).
- **Breaks:** millions of names, constant change, one file can't be distributed or updated or kept consistent, and one authority can't manage it all.
- **Better:** a **hierarchical, distributed, cached database**: DNS.

### 1.5.2 How DNS is organized `[MK]`

```text
                   . (root)
        ┌───────────┼───────────┐
      .com        .org         .in  ...         ← top-level domains (TLD)
        │
   example.com                                  ← authoritative name servers for the domain
        │
   www.example.com / api.example.com            ← records
```

**Roles**
- **Stub resolver**: your OS/browser asking for an answer.
- **Recursive resolver**: (your ISP's, or 8.8.8.8 / 1.1.1.1) does the legwork and **caches** results.
- **Root servers**: know who runs each TLD.
- **TLD servers**: know the authoritative servers for each domain.
- **Authoritative servers**: hold the actual records for the domain.

**A lookup of `api.example.com` (cache miss everywhere)**
```text
Browser ─▶ OS cache? ─miss▶ Recursive resolver
Recursive ─▶ Root: "who handles .com?"            ← returns TLD server addresses
Recursive ─▶ .com TLD: "who handles example.com?" ← returns authoritative servers
Recursive ─▶ example.com NS: "A record for api?"  ← returns 93.184.216.34 (TTL 300)
Recursive caches (300 s) ─▶ returns to OS ─▶ Browser connects to 93.184.216.34
```
Typical times: cache hit 0–1 ms; recursive resolution with cold caches 20–200+ ms.

### 1.5.3 Record types `[MK]`
| Record | Meaning | Example |
|---|---|---|
| **A / AAAA** | Name → IPv4 / IPv6 | `api.example.com → 93.184.216.34` |
| **CNAME** | Alias to another name | `www → example.com` (can't coexist with other records at the same name; not allowed at zone apex in standard DNS) |
| **NS** | Name servers for a zone | |
| **MX** | Mail servers | |
| **TXT** | Arbitrary text (SPF, domain verification) | |
| **SRV** | Service host+port | used in service discovery |

### 1.5.4 TTL, caching, and consequences `[MK]`
Every record has a **TTL** (time to live). Resolvers and clients cache for that long.
- **Short TTL (30–60 s)**: faster failover/migration, more DNS queries and latency spikes.
- **Long TTL (hours/day)**: fewer lookups, but changes propagate slowly. If you move a server and the TTL is 24 h, some users hit the old IP for up to a day (and some resolvers ignore TTLs).
- **Operational rule:** lower the TTL *well before* a planned migration, then raise it after.

### 1.5.5 DNS in system design `[MK]`
- **DNS load balancing / round robin**: return multiple A records. Cheap and global, but coarse: no health awareness (dead IPs still returned until TTL expires), clients may pick the first record, caching skews distribution.
- **Geo-DNS / latency-based routing**: return the nearest region's IP based on resolver location (cloud DNS services offer this).
- **Failover**: health-checked DNS removes dead endpoints (bounded by TTL).
- **Service discovery** inside clusters (Kubernetes DNS, Consul).
- **CDNs** use DNS (CNAME to the CDN) or anycast to direct users to the nearest edge.

### 1.5.6 Failure modes `[MK]`
- **DNS outage**: your servers are fine but nobody can find them (this has caused major real-world outages `[Public: widely reported incidents]`). Mitigate: multiple providers/NS, sane TTLs, monitoring.
- **Stale caches** after changes.
- **DNS hijacking / poisoning**: spoofed answers; mitigations: DNSSEC, DoH/DoT, TLS validation downstream.
- **Misconfiguration**: wrong records, expired domain registrations, CNAME loops.
- **Resolver slowness** adds latency to *every new connection*.

**Interview note:** mention DNS TTLs when discussing failover/migration, and note that DNS-based routing is a *coarse, eventually-consistent* mechanism.

---

## 1.6 Transport: TCP and UDP

### 1.6.1 The problem
IP delivers packets **best-effort**: they may be lost, duplicated, reordered, or delayed. Applications (a bank transfer, a web page) need a *reliable, ordered byte stream*. Something must build reliability on top of an unreliable network. That's **TCP**.

### 1.6.2 TCP: how it works `[MK]`

**Connection setup: three-way handshake (costs 1 RTT)**
```text
Client                          Server
  │ ── SYN (seq=x) ───────────▶ │       "I want to talk, my numbering starts at x"
  │ ◀─ SYN-ACK (seq=y, ack=x+1) │       "OK, mine starts at y"
  │ ── ACK (ack=y+1) ─────────▶ │       "Got it" (client can already send data with this)
```
Only after this can the client send the first byte of the HTTP request. **Cost: one full round trip before any data.**

**Reliability mechanisms**
- **Sequence numbers + ACKs**: every byte numbered; receiver acknowledges.
- **Retransmission**: no ACK within a **retransmission timeout (RTO)** (or 3 duplicate ACKs → fast retransmit) → resend.
- **Ordering**: out-of-order segments are held until gaps are filled.
- **Checksums** detect corruption.

**Flow control** (protect the *receiver*): the receiver advertises a **window** = how much more data it can buffer. Sender never exceeds it.

**Congestion control** (protect the *network*): the sender estimates available capacity and adjusts its **congestion window (cwnd)**:
- **Slow start**: begin small (initial window commonly 10 segments ≈ 14 KB), double each RTT until a threshold or loss.
- **Congestion avoidance**: grow slowly (≈ +1 segment per RTT).
- **On loss**: shrink the window (classic: halve; modern algorithms like CUBIC/BBR behave differently).

**Why this matters enormously for system design:** a new TCP connection starts *slow*. Transferring a 1 MB response over a fresh connection with 100 ms RTT:
```text
RTT 1: send 10 segments  (~14 KB)
RTT 2: send 20           (~29 KB)
RTT 3: send 40           (~58 KB)
RTT 4: send 80           (~117 KB)
RTT 5: send 160          (~234 KB)
RTT 6: send 320          (~467 KB)  cumulative ~ 920 KB
RTT 7: remainder
```
→ **~7 RTTs ≈ 700 ms** to deliver 1 MB even on a gigabit link, *because of slow start over a long RTT*. (Real behavior varies by algorithm and initial window, but the lesson holds.) **Reuse connections (keep-alive/pooling) and put servers/CDNs close to users.**

**Connection teardown & TIME_WAIT**
Closing uses FIN/ACK exchanges. The side that closes first enters **TIME_WAIT** (~60 s typically) to absorb stray packets. A proxy/client that opens and closes thousands of short connections per second accumulates TIME_WAIT sockets and can **exhaust ephemeral ports**. Fix: connection reuse/pooling, tuning.

**Keep-alive & timeouts:** idle TCP connections are not detected as dead without traffic. A silent failure (cable cut, NAT table expiry) leaves a "half-open" connection. Use **application-level heartbeats** and **TCP keepalive** with sensible intervals. **NAT/middlebox idle timeouts** (often 30 s–several minutes) silently drop idle flows: mobile/IoT clients should send periodic keep-alives.

**Head-of-line (HOL) blocking:** TCP delivers bytes *in order*. If one packet is lost, **all later data waits** (even if it arrived) until the lost packet is retransmitted. For multiplexed protocols (HTTP/2 over one TCP connection), one lost packet stalls *all* streams, a motivation for QUIC.

**Nagle's algorithm & delayed ACKs** `[GK]`: Nagle batches small writes to reduce tiny packets; combined with delayed ACKs it can add ~40 ms latency to chatty request/response patterns. Latency-sensitive apps set `TCP_NODELAY`.

### 1.6.3 UDP `[MK]`
**UDP**: no connection, no reliability, no ordering, no congestion control: just "send this datagram." Overhead tiny; latency minimal.
**Use when:** late data is useless (voice/video calls, live gaming), you're building your own reliability (QUIC, some telemetry/IoT protocols), or queries are tiny and idempotent (DNS).
**Danger:** you must handle loss, ordering, and congestion yourself; UDP-based floods are a DDoS vector.

| | TCP | UDP |
|---|---|---|
| Connection | Yes (handshake) | No |
| Reliable & ordered | Yes | No |
| Congestion/flow control | Yes | No (app's job) |
| Latency overhead | Handshake + HOL blocking | Minimal |
| Header | 20+ bytes | 8 bytes |
| Typical | HTTP(S), DB protocols, SSH | DNS, VoIP, video, gaming, QUIC, many IoT/telemetry |

### 1.6.4 QUIC (HTTP/3's transport) `[GK]`
Built on UDP in user space; provides reliability, congestion control, **independent streams** (a lost packet only stalls its own stream), **integrated TLS 1.3** (handshake combined with transport setup: **1 RTT** for new connections, **0-RTT** resumption), and **connection migration** (survive a phone switching Wi-Fi ↔ cellular because connections are identified by IDs, not IP tuples). Great for mobile/lossy networks.

### 1.6.5 Embedded/IoT notes (your world) `[GK]`
Devices on cellular face high latency, loss, NAT timeouts, and metered data. Common choices: **MQTT** (lightweight pub/sub over TCP), **CoAP** (UDP-based), custom binary protocols with sequence numbers and acks, **batching + compression**, **store-and-forward**. Design the *device ↔ backend contract* to tolerate disconnects, duplicates, and reordering (Ch 14).

---

## 1.7 Application layer: HTTP, TLS

### 1.7.1 HTTP basics `[MK]`

HTTP is a **request/response** text-based (1.x) protocol. A **stateless** protocol: each request independent.

```text
GET /v1/users/42?fields=name HTTP/1.1          ← method, path, version
Host: api.example.com                          ← which site (virtual hosting)
Authorization: Bearer eyJhbGciOi...            ← credentials
Accept: application/json
User-Agent: ...
                                               ← blank line ends headers
(body, optional)

HTTP/1.1 200 OK                                ← status line
Content-Type: application/json
Content-Length: 61
Cache-Control: max-age=60
ETag: "a1b2"

{"id":42,"name":"Priya"}
```

**Methods:** GET (read), POST (create/act), PUT (replace), PATCH (modify), DELETE, HEAD, OPTIONS.
**Safe** methods don't change state (GET/HEAD); **idempotent** methods give the same result if repeated (GET, PUT, DELETE; POST is *not*). This matters for **retries** (§1.8).

**Status codes**
| Range | Meaning | Common |
|---|---|---|
| 1xx | Informational | 101 Switching Protocols (WebSocket upgrade) |
| 2xx | Success | 200 OK, 201 Created, 202 Accepted (async), 204 No Content |
| 3xx | Redirect | 301 Moved Permanently, 302 Found, 304 Not Modified |
| 4xx | **Client** error | 400, 401 (unauthenticated), 403 (forbidden), 404, 409 Conflict, 410 Gone, 429 Too Many Requests |
| 5xx | **Server** error | 500, 502 Bad Gateway, 503 Service Unavailable, 504 Gateway Timeout |

### 1.7.2 HTTP versions and why they exist `[MK/GK]`

| Version | Key idea | Problem it solved | New problem |
|---|---|---|---|
| **HTTP/1.0** | One request per TCP connection | Simple | Handshake per request, slow start each time |
| **HTTP/1.1** | **Persistent connections** (keep-alive); chunked transfer; Host header | Reuse connections | **Head-of-line blocking** at the application layer (one request at a time per connection); browsers open ~6 parallel connections per host as a workaround |
| **HTTP/2** | **Binary framing, multiplexing** many streams on one TCP connection, header compression (HPACK), server push (rarely used now) | Parallel requests on one connection | **TCP-level HOL blocking**: one lost packet stalls all streams |
| **HTTP/3** | HTTP over **QUIC** (UDP) | Per-stream loss recovery, faster handshakes, migration | Requires UDP allowed; newer tooling |

**Practical consequences:** connection pooling/keep-alive is crucial; HTTP/2 makes many small requests cheaper; gRPC rides on HTTP/2; CDNs terminate HTTP/2/3 at the edge.

### 1.7.3 TLS (HTTPS) `[MK]`

**Problem:** Anyone on the path (café Wi-Fi, ISP, a rogue router) can read or alter HTTP. **TLS** provides: **confidentiality** (encryption), **integrity** (tamper detection), **authentication** (are you really `bank.com`?).

**The TLS 1.3 handshake (1 RTT)**
```text
Client                                   Server
  │ ── ClientHello (supported ciphers, key share, SNI) ─▶ │
  │ ◀─ ServerHello (key share) + Certificate + Finished ─ │   (encrypted from here on)
  │ ── Finished (+ first application data) ─────────────▶ │
```
- Both sides derive shared session keys via **ephemeral Diffie-Hellman** (forward secrecy: stealing the server key later doesn't decrypt old traffic).
- **Certificate**: server's public key signed by a **Certificate Authority (CA)** your OS/browser trusts. The client validates the **chain** and that the cert's name matches the host (via **SNI**, the hostname the client sends so one IP can host many certs).
- TLS 1.2 needed **2 RTTs**; TLS 1.3 needs **1** (and **0-RTT** resumption with caveats: replayable, so only for idempotent requests).
- **Session resumption** avoids full handshakes on reconnect.

**Costs:** handshake round trips; CPU for asymmetric crypto (mostly at handshake; bulk encryption is cheap with AES hardware support); certificate management.
**Operations:** certificate expiry causes *hard outages* (it has taken down many services `[Public: numerous reports]`), so monitor expiry and automate renewal (e.g., ACME/Let's Encrypt). **TLS termination** can happen at the load balancer/CDN; internal traffic should *still* be encrypted in zero-trust designs (mTLS).

### 1.7.4 Proxies (preview) `[MK]`
- **Forward proxy:** sits in front of *clients* (corporate egress).
- **Reverse proxy:** sits in front of *servers* (Nginx, Envoy, cloud LBs): TLS termination, routing, caching, compression. Detailed in Chapter 7.
- Proxies add a hop (latency) but enable pooling (one warm TCP/TLS connection to the backend serving many client requests).

---

## 1.8 Timeouts, retries, and why they're dangerous `[MK]`

### 1.8.1 The problem
Networks and servers fail silently. If a client waits forever for a reply, threads/resources are held forever. If it gives up and retries, it may duplicate work or amplify load.

### 1.8.2 Kinds of timeouts
| Timeout | Guards against |
|---|---|
| **Connect timeout** | Unreachable/dead host (SYN gets no answer). Typically short: 100 ms–3 s |
| **TLS handshake timeout** | Stalled negotiation |
| **Read (response) timeout** | Server accepted but never answers |
| **Write timeout** | Slow/stalled upload |
| **Idle timeout** | Dormant connections hogging resources |
| **Overall/request deadline** | Total time budget for the whole operation |

**Rule:** *Every* network call needs a timeout. No timeout = potential infinite hang = resource exhaustion = cascading failure (Ch 18).

**Deadline propagation:** if the user's request has a 2 s budget and the first call used 1.5 s, downstream calls should know only 0.5 s remains; don't start work that can't finish.

### 1.8.3 What a timeout *means* (the critical insight)
When a call times out, you **do not know** whether the server:
1. never received it,
2. received and is still working,
3. completed it but the response was lost,
4. crashed midway.

You cannot distinguish these. **This ambiguity is the root of idempotency, deduplication, and half of distributed systems (Ch 14).**

### 1.8.4 Retries: help and harm
**Retry when:** the error is **transient** (timeout, connection reset, 503, 429 with Retry-After) *and* the operation is **idempotent** (or protected by an idempotency key).
**Don't retry** blindly: 4xx client errors (they will fail again), non-idempotent operations without protection.

**Dangers**
- **Duplicate side effects** (double charge).
- **Retry storms:** if a service is slow because it's overloaded, retries *add* load → more failures → more retries. With 3 layers each retrying 3×, one request becomes up to **27** backend calls.
- **Synchronized retries:** many clients retry at the same instants → spikes.

**Mitigations**
- **Exponential backoff**: wait 100 ms, 200 ms, 400 ms, 800 ms…
- **Jitter**: randomize delays so clients desynchronize. (E.g., "full jitter": `sleep = random(0, min(cap, base × 2^attempt))`.)
- **Retry budgets/limits**: cap retries per call and as a *fraction of total traffic* (e.g., ≤10%).
- **Retry at one layer only.**
- **Circuit breakers** (Ch 18) stop calling a failing dependency.
- **Idempotency keys** make retried writes safe (Ch 2, 14).

**Pseudocode (backoff with full jitter)**
```java
long delay = base;                 // e.g. 100 ms
for (int attempt = 0; attempt < maxAttempts; attempt++) {
    try { return call(); }
    catch (TransientException e) {
        long sleep = ThreadLocalRandom.current().nextLong(0, Math.min(cap, delay));
        Thread.sleep(sleep);
        delay *= 2;
    }
}
throw new GaveUpException();
```

---

## 1.9 What happens when you open `https://example.com` (the full trace)

**Setup:** Browser in Singapore; server in Virginia (RTT ≈ 230 ms); no cached DNS or connections.

```text
 t(ms)  Event
   0    User presses Enter. Browser checks URL, HSTS list, cache (miss).
   1    DNS: browser cache → OS cache → recursive resolver (say 5 ms away).
        Resolver cold-resolves: root → .com → example.com authoritative. (~60–150 ms)
 ~150   Got IP 93.184.216.34 (TTL 300).
        TCP handshake: SYN →, ← SYN-ACK, ACK →.         cost: 1 RTT ≈ 230 ms
 ~380   TLS 1.3 handshake: ClientHello →, ← ServerHello+Cert+Finished.   1 RTT ≈ 230 ms
 ~610   Client sends HTTP request (GET / HTTP/2).         (request leaves at ~610)
 ~840   Request reaches server (½ RTT ≈ 115 ms) … server work (say 20 ms) …
        first response byte returns (½ RTT ≈ 115 ms)  →  TTFB ≈ 610 + 230 + 20 = 860 ms
 ~860+  HTML (say 60 KB) arrives, limited by TCP slow start (initial window ~14 KB):
        needs ~3 RTTs ≈ extra 460 ms → page document complete at ≈ 1.3 s.
 ~1.3 s Browser parses HTML, discovers CSS/JS/images/API calls → more requests
        (new hostnames → more DNS/TCP/TLS; same host → HTTP/2 multiplexing on the existing connection).
 ...    Render. Interactive maybe 2–4 s depending on resources.
```
**Observe:** *before the server did any work*, ~610 ms were spent on DNS + TCP + TLS handshakes purely due to distance. The server's code (20 ms) is a rounding error.

### Answer to Pause & Predict 1.1 (the 900 ms API)
For a **warm** connection (client already connected): one RTT (230 ms) + server (8 ms) + slow-start effects for the body ≈ 240–300 ms. For a **cold** call (new connection): DNS (~50–150) + TCP (230) + TLS (230) + request/response (230+8) ≈ **750–850 ms**, plus client processing and any redirect/auth calls. That's the 900 ms.
**Architectural fixes:**
1. **Move content closer**: regional servers, CDN/edge caches for cacheable responses.
2. **Keep connections warm**: pooling, keep-alive, HTTP/2, QUIC/0-RTT.
3. **Reduce round trips**: batch requests, fewer redirects, GraphQL/BFF aggregation, smaller payloads.
4. **Terminate TLS at an edge** near the user, which then uses a warm, optimized backbone connection to the origin. This is the main benefit of CDN/edge proxies even for *dynamic* APIs.
5. **Cache DNS**, use a fast resolver, preconnect/prefetch.
Notice: **none of these change the 6 ms of application code.**

---

## 1.10 Network failure modes and what they look like `[MK]`

| Failure | Symptom to the client | Typical cause | Mitigation |
|---|---|---|---|
| **Packet loss** | Slow transfers, retransmits | Congestion, bad radio | Retries, FEC, QUIC, move closer |
| **High latency/jitter** | Slow responses | Distance, queuing | CDN, regional deployment |
| **Connection refused** | Immediate error | Nothing listening / firewall | Health checks, restart |
| **Connect timeout** | Hang then error | Host down, firewall drop, routing issue | Short connect timeouts, failover |
| **Connection reset (RST)** | Mid-stream error | Server crash, LB idle-timeout, proxy closed | Retry (idempotent), keep-alive tuning |
| **Half-open connection** | Writes "succeed", nothing comes back | Peer vanished; no FIN | Heartbeats, TCP keepalive, read timeouts |
| **DNS failure** | "Host not found" | DNS outage/misconfig | Multi-provider DNS, caching |
| **TLS failure** | Handshake error | Expired/invalid cert, clock skew, protocol mismatch | Cert monitoring, NTP |
| **Network partition** | Some nodes can't reach others, though all are "up" | Router/link failure | Design for partitions (Ch 14–15) |
| **Slow (brownout) dependency** | Latency spikes, thread exhaustion | Overload, GC, bad query | Timeouts, circuit breakers, bulkheads |
| **MTU/fragmentation issues** | Large requests hang, small ones work | Path MTU black holes | MSS clamping, PMTUD fixes |
| **NAT timeout** | Long-idle connections die silently | Middlebox state expiry | Keepalives under the NAT timeout |

> **Insight.** A *slow* dependency is usually worse than a *dead* one: dead fails fast; slow holds your resources.

---

## 1.11 Interview perspective

- **"What happens when you type a URL?"** is a favorite. Structure: DNS → TCP → TLS → HTTP → server path (LB/CDN → app → cache/DB) → response → render. Seniors add: caching at each layer, connection reuse, HTTP/2/3, CDN, anycast.
- **When designing for global users:** immediately note RTT physics and mention CDN/edge/regional deployments.
- **When designing for devices:** mention intermittent connectivity, NAT timeouts, batching, and idempotent uploads with sequence numbers.
- **When someone mentions "real-time":** compare polling, long polling, SSE, WebSocket (Ch 2).
- **Say things like:** "A cold call costs DNS + TCP + TLS round trips, so I'll keep connections pooled and terminate TLS at the edge."

---

## 1.12 Code labs (Java 17+)

### Lab 1: Measure the stages of a connection
This program times DNS, TCP connect, TLS handshake, and the first byte for HTTPS endpoints, so you *see* the round trips.

```java
import javax.net.ssl.*;
import java.io.*;
import java.net.*;

public class ConnectionStages {
    public static void main(String[] args) throws Exception {
        String host = args.length > 0 ? args[0] : "example.com";
        int port = 443;

        long t0 = System.nanoTime();
        InetAddress addr = InetAddress.getByName(host);          // DNS (may hit OS cache)
        long t1 = System.nanoTime();

        Socket tcp = new Socket();
        tcp.connect(new InetSocketAddress(addr, port), 5000);    // TCP handshake (1 RTT)
        long t2 = System.nanoTime();

        SSLSocketFactory f = (SSLSocketFactory) SSLSocketFactory.getDefault();
        SSLSocket tls = (SSLSocket) f.createSocket(tcp, host, port, true);
        tls.startHandshake();                                     // TLS handshake (1–2 RTT)
        long t3 = System.nanoTime();

        OutputStream out = tls.getOutputStream();
        out.write(("GET / HTTP/1.1\r\nHost: " + host + "\r\nConnection: close\r\n\r\n").getBytes());
        out.flush();
        InputStream in = tls.getInputStream();
        int firstByte = in.read();                                // time to first byte
        long t4 = System.nanoTime();

        System.out.printf("DNS:        %6.1f ms%n", (t1 - t0) / 1e6);
        System.out.printf("TCP:        %6.1f ms%n", (t2 - t1) / 1e6);
        System.out.printf("TLS:        %6.1f ms%n", (t3 - t2) / 1e6);
        System.out.printf("TTFB(req):  %6.1f ms%n", (t4 - t3) / 1e6);
        System.out.printf("TOTAL:      %6.1f ms%n", (t4 - t0) / 1e6);
        tls.close();
    }
}
```
**Experiments**
1. Run it for a nearby site and a far site. Compare TCP connect times (≈ 1 RTT).
2. Run it twice quickly: DNS time drops (cached). 
3. Predict TLS ≈ 1–2× TCP time. Is it?
4. Think: how would pooling/keep-alive change the *second request's* cost?

### Lab 2: Thread-per-connection echo server vs. what happens under load
```java
import java.io.*;
import java.net.*;
import java.util.concurrent.*;

public class EchoServer {
    public static void main(String[] args) throws Exception {
        int port = 9000;
        ExecutorService pool = Executors.newFixedThreadPool(50);  // try 5, 50, 500
        try (ServerSocket server = new ServerSocket(port)) {
            System.out.println("Listening on " + port);
            while (true) {
                Socket s = server.accept();
                pool.submit(() -> {
                    try (s;
                         BufferedReader r = new BufferedReader(new InputStreamReader(s.getInputStream()));
                         PrintWriter w = new PrintWriter(s.getOutputStream(), true)) {
                        String line;
                        while ((line = r.readLine()) != null) {
                            Thread.sleep(50);                      // simulate a 50 ms DB call (I/O wait)
                            w.println(line);
                        }
                    } catch (Exception ignored) {}
                });
            }
        }
    }
}
```
Client load generator:
```java
import java.io.*;
import java.net.*;
import java.util.concurrent.*;
import java.util.concurrent.atomic.*;

public class EchoLoad {
    public static void main(String[] args) throws Exception {
        int clients = Integer.parseInt(args[0]);     // e.g., 20, 100, 400
        int msgsPerClient = 20;
        AtomicLong totalNanos = new AtomicLong();
        AtomicLong count = new AtomicLong();
        ExecutorService ex = Executors.newFixedThreadPool(clients);
        CountDownLatch done = new CountDownLatch(clients);
        long start = System.nanoTime();
        for (int i = 0; i < clients; i++) {
            ex.submit(() -> {
                try (Socket s = new Socket("localhost", 9000);
                     PrintWriter w = new PrintWriter(s.getOutputStream(), true);
                     BufferedReader r = new BufferedReader(new InputStreamReader(s.getInputStream()))) {
                    for (int j = 0; j < msgsPerClient; j++) {
                        long t0 = System.nanoTime();
                        w.println("hello");
                        r.readLine();
                        totalNanos.addAndGet(System.nanoTime() - t0);
                        count.incrementAndGet();
                    }
                } catch (Exception ignored) {
                } finally { done.countDown(); }
            });
        }
        done.await();
        double secs = (System.nanoTime() - start) / 1e9;
        System.out.printf("clients=%d  throughput=%.0f msg/s  avg latency=%.1f ms%n",
            clients, count.get() / secs, totalNanos.get() / 1e6 / Math.max(1, count.get()));
        ex.shutdownNow();
    }
}
```
**What to predict and observe**
- With a 50-thread pool and 50 ms "work," max throughput ≈ 50 / 0.05 = **1,000 msg/s** (Little's Law). More clients than threads → **queueing** (latency climbs, connections wait to be accepted).
- Increase the pool to 500: throughput rises *because the work is I/O-wait* (sleep), not CPU, until memory/context switching limits appear.
- Replace `Thread.sleep(50)` with CPU burn: now more threads **don't** help beyond core count. 
- **Reflection:** this is exactly how a slow DB (50 ms → 500 ms) exhausts the pool and queues everything. Connect that to timeouts/bulkheads (Ch 18).

### Lab 3: Race condition & fixes
```java
import java.util.concurrent.*;
import java.util.concurrent.atomic.*;

public class RaceDemo {
    static int unsafe = 0;
    static int synced = 0;
    static final Object lock = new Object();
    static AtomicInteger atomic = new AtomicInteger();

    public static void main(String[] args) throws Exception {
        int threads = 8, per = 1_000_000;
        ExecutorService ex = Executors.newFixedThreadPool(threads);
        CountDownLatch done = new CountDownLatch(threads);
        for (int t = 0; t < threads; t++) ex.submit(() -> {
            for (int i = 0; i < per; i++) {
                unsafe++;                                   // race!
                synchronized (lock) { synced++; }           // correct, but contended
                atomic.incrementAndGet();                   // lock-free CAS
            }
            done.countDown();
        });
        done.await(); ex.shutdown();
        System.out.println("expected = " + (threads * per));
        System.out.println("unsafe   = " + unsafe);
        System.out.println("synced   = " + synced);
        System.out.println("atomic   = " + atomic.get());
    }
}
```
**Observe:** on a multi-core machine `unsafe` is *less* than expected: lost updates. (Races need true parallelism to show up reliably. On a 1-core container you may see no loss at all, which does not mean the code is correct; it means the bug is hiding. Try increasing `per`, or run on a multi-core machine. Absence of a failure in testing is not proof of correctness; this is why races are so dangerous.) This is the same bug as "two users buy the last ticket" (Ch 3) and "two servers update the same counter" (Ch 14).

---

## 1.13 Common misconceptions

1. **"RAM is fast so just keep everything in memory."** It's volatile, limited, costly. Durable truth must be persisted; memory is for working sets/caches.
2. **"More threads = more throughput."** Only until the bottleneck (cores for CPU-bound; downstream capacity for I/O-bound). Beyond that: contention, memory, context-switch overhead.
3. **"The network is basically instant."** Intra-DC ~0.5 ms, cross-continent ~150+ ms: *orders of magnitude* slower than memory.
4. **"Bandwidth = speed."** Bandwidth is capacity; latency is delay. Small transfers are latency-bound; TCP slow start punishes new connections.
5. **"HTTPS is slow."** Handshakes cost round trips, but with keep-alive, TLS 1.3, resumption, and hardware AES the steady-state overhead is small.
6. **"A timeout means the request failed."** It means *you don't know*. The operation may have succeeded.
7. **"Retries make systems more reliable."** Naive retries create duplicates and retry storms.
8. **"DNS changes are instant."** TTLs and resolver caches make them gradual and uneven.
9. **"A dead server is the worst case."** A *slow* server is worse.
10. **"UDP is unreliable so it's bad."** It's a building block; reliability can be (and is, in QUIC) built above it.

---

## 1.14 Chapter summary

- Hardware has a **latency ladder** spanning ~10 orders of magnitude: L1 (ns) → RAM (100 ns) → SSD (100 µs) → in-DC network (0.5 ms) → HDD (ms) → intercontinental network (100+ ms). Design to keep data near computation and to minimize round trips.
- **CPU-bound** vs **I/O-bound** workloads scale differently; **Little's Law** links throughput, latency, and concurrency; slow dependencies exhaust concurrency limits.
- **Threads** share memory → races, locks, deadlocks. **Event loops** handle many idle connections efficiently but must never block.
- Networking is **layered**; IP gives best-effort packet delivery; **TCP** adds reliable ordered streams via handshakes, ACKs, flow/congestion control. New connections are *slow* (handshake + slow start).
- **DNS** is a cached hierarchical database; TTL trade-offs affect failover and migration.
- **HTTP** evolved (1.1 keep-alive → 2 multiplexing → 3/QUIC) to reduce round trips and blocking. **TLS 1.3** needs 1 RTT.
- **Timeouts are ambiguous**; **retries** need idempotency, backoff, jitter, and budgets.
- A cold request across the world spends hundreds of milliseconds on handshakes before the server does any work.

### Mental models
| Concept | Mental model |
|---|---|
| Memory hierarchy | Chef's board → counter → pantry → warehouse across the city |
| Latency vs bandwidth | Road length vs road width |
| TCP slow start | A cautious driver accelerating on an unknown road |
| DNS | A phone book with delivery-date stamps (TTL) |
| Timeout | Sending a letter and not hearing back: did they get it? |
| Retry storm | Everyone redialing the busy call center at once |
| Event loop | One waiter watching many tables vs. one waiter per table |

---

## 1.15 Exercises

### A. Conceptual
1. Rank by speed: RAM read, SSD random read, HDD seek, same-DC RTT, cross-ocean RTT. Give approximate times.
2. Why do databases write sequential logs and `fsync` them? What's the trade-off?
3. What does swapping do to a server, and why is it dangerous?
4. Define CPU-bound vs I/O-bound; which benefits from more threads than cores?
5. Using Little's Law: 5,000 req/s at 40 ms average latency → how many concurrent requests? What happens if latency becomes 400 ms and you have a 300-thread pool?
6. Explain the race condition in `counter++`. Give two different fixes and their trade-offs.
7. What is a deadlock? How can you prevent one?
8. Why can an event loop handle 100k connections but a thread-per-connection server struggle? What's the catch?
9. Explain the 4-tuple that identifies a TCP connection and why one server port can serve many clients.
10. What are the four components of network delay? Which can money fix?
11. Compute BDP for 1 Gbit/s and 80 ms RTT. What does it imply about TCP windows?
12. Describe the TCP three-way handshake and what it costs in time.
13. Why is a freshly opened TCP connection slow to deliver a large response? What helps?
14. What is head-of-line blocking in TCP? How does QUIC address it?
15. When is UDP the right choice?
16. Walk through a DNS lookup with cold caches. Which servers are asked, in order?
17. How does DNS TTL affect failover and migration? What should you do before a migration?
18. Name three problems with DNS round-robin as a load-balancing mechanism.
19. How many round trips for TCP + TLS 1.2 vs TLS 1.3 before the first HTTP request is sent?
20. Why do HTTP/2 and HTTP/3 exist, in terms of problems of their predecessors?
21. What does a timeout *not* tell you? Why does that matter?
22. Describe a retry storm and three mitigations.
23. What is "full jitter" backoff and why use it?
24. 502 vs 503 vs 504: what does each typically mean?

### B. Calculations
25. A page needs 25 sequential API calls within the same data center at 0.5 ms each plus 2 ms processing per call. Total latency? What if calls were cross-region at 80 ms RTT?
26. Transferring 2 MB on a fresh TCP connection with 50 ms RTT, initial window 10 × 1,460 B, doubling per RTT, no loss. Approx. number of RTTs and total time? Compare with 5 ms RTT.
27. A server has 8 cores; each request costs 10 ms CPU and 90 ms waiting on I/O. Estimate (a) max throughput if CPU is the limit, (b) concurrency needed at that throughput.
28. A client opens a new connection for each of 2,000 req/s to one backend IP:port. Each connection stays in TIME_WAIT for 60 s. How many TIME_WAIT sockets accumulate? What limit do you hit? What is the fix?

### C. Design thinking
29. Your mobile app's users in Brazil use servers in Virginia (RTT ~120 ms). The home screen needs 6 sequential API calls. Redesign to cut perceived latency. List at least five techniques.
30. A fleet of 1 M IoT devices on cellular must report every 10 s. NATs drop idle connections after ~60 s. Design the transport approach (protocol, keepalive, batching, reconnect strategy).

### D. Debugging
31. A service shows 100% healthy CPU and memory but p99 latency of 8 s. Threads in a dump are all blocked on a DB call. Diagnose and propose mitigations (immediate and structural).
32. After a deploy, clients get random "connection reset" errors every ~5 minutes on long-lived connections. Hypothesize at least four causes.
33. Users in one country report the site is unreachable, others are fine. List five possible network-level causes and how you'd test each.

### E. Interview-style
34. "Explain what happens when I type google.com in a browser" in three minutes, including performance considerations.
35. "Why would you choose UDP over TCP?" and "Why not always use TCP?"

### F. Labs follow-up
36. Run Lab 1 against five hosts at varied distances; tabulate DNS/TCP/TLS. Does TCP ≈ RTT? Does TLS ≈ 1×–2× TCP?
37. In Lab 2, replace sleep with a CPU loop and compare scaling vs pool size. Explain results.
38. In Lab 3, benchmark `synchronized` vs `AtomicInteger` vs `LongAdder` under 8 threads and explain.

### G. Teach-back
39. Teach "why a new connection is slow" to a beginner using an analogy, then give the technical explanation.

---

## 1.16 Full answer key

**1.** RAM ~100 ns; SSD random read ~20–150 µs; same-DC RTT ~0.5 ms; HDD seek ~5–10 ms; cross-ocean RTT ~100–250 ms.

**2.** Sequential appends avoid seeks (fast, especially on HDD) and give a precise recovery log; `fsync` forces data to stable storage so a committed transaction survives a crash. Trade-off: `fsync` latency (ms) limits commit rate; batching commits (group commit) amortizes it; disabling it risks losing recent commits.

**3.** Pages are moved from RAM to disk; access becomes disk-speed (1000×+ slower), causing severe latency, thrashing, and timeouts, often worse than a clean crash because the node limps along holding connections.

**4.** CPU-bound: time spent computing; threads beyond cores don't help. I/O-bound: time waiting; many more threads than cores help (until downstream limits or overhead).

**5.** L = 5000 × 0.04 = **200** concurrent. At 400 ms: 5000 × 0.4 = **2,000** wanted concurrent but the pool has 300 → throughput capped at 300/0.4 = **750 req/s**; the rest queue/time out → outage-like behavior from a mere 10× slowdown of one dependency.

**6.** Both threads read the old value and write back the same increment → one update lost. Fixes: lock (`synchronized`/mutex): simple, correct, contention; atomic CAS (`AtomicInteger`): lock-free, fast for simple counters, harder for multi-variable invariants. Others: per-thread counters merged later (`LongAdder`), message passing.

**7.** Circular wait for locks. Prevent via global lock ordering, lock timeouts/try-lock with backoff, avoiding nested locks, using higher-level concurrency structures.

**8.** The OS notifies which sockets are ready (epoll), so one thread multiplexes many connections without per-connection stacks/context switches. The catch: any blocking or long computation in the loop stalls all connections; code complexity (callbacks/async).

**9.** (src IP, src port, dst IP, dst port). Distinct clients differ in source IP/port, so each connection tuple is unique even though the destination port is the same.

**10.** Propagation (distance), transmission (size/bandwidth), queuing (congestion), processing (hardware). Money can buy bandwidth (reducing transmission), better hardware, and less congested paths; propagation is bounded by physics (move closer instead).

**11.** 1 Gbit/s × 0.08 s = 80 Mbit = **10 MB**. TCP needs ~10 MB in flight (window) to saturate the link; default/small windows or slow start leave the link underutilized.

**12.** SYN → SYN-ACK → ACK; costs **1 RTT** before the client can send application data (the third packet can piggyback data).

**13.** Slow start: begins with a small congestion window (≈10 segments) doubling per RTT; a large transfer over a high-RTT path needs several RTTs. Help: connection reuse, bigger initial window, CDN/edge proximity, compression, fewer bytes, HTTP/2/3.

**14.** Because TCP delivers bytes in order, a lost packet blocks everything after it (all multiplexed streams share one byte stream). QUIC implements independent streams over UDP so loss in one stream doesn't block others.

**15.** When timeliness beats reliability (VoIP, live video, games), tiny query/response (DNS), building custom reliability/multiplexing (QUIC), telemetry where occasional loss is OK, multicast/broadcast use cases.

**16.** Stub → recursive resolver → root server (returns TLD servers) → TLD server (returns authoritative servers) → authoritative server (returns the A/AAAA record) → cached by resolver per TTL.

**17.** Cached answers persist for the TTL; high TTL = slow propagation (users hit old IP) while low TTL = faster change but more queries. Before migration: lower TTL (e.g., 60 s) at least one old-TTL period in advance, migrate, then raise TTL.

**18.** No health checking (dead IPs still returned until TTL), coarse/uneven distribution due to caching and client behavior, slow failover bounded by TTL; no awareness of load; some clients always pick the first record.

**19.** TCP 1 RTT + TLS 1.2 2 RTT = **3 RTTs**; TCP 1 + TLS 1.3 1 = **2 RTTs** (QUIC: 1 RTT total; 0 with resumption).

**20.** HTTP/1.1: one outstanding request per connection (app-level HOL), many connections needed → HTTP/2 multiplexing and header compression. HTTP/2 over TCP: one lost packet stalls all streams → HTTP/3 over QUIC.

**21.** It doesn't tell you whether the server processed the request. This ambiguity forces idempotent operations, deduplication keys, and state-reconciliation endpoints.

**22.** Overloaded service slows; clients retry, adding load, causing more failures. Mitigate: exponential backoff + jitter, retry budgets/limits, retry only at one layer, circuit breakers, load shedding, honoring `Retry-After`.

**23.** `sleep = random(0, min(cap, base×2^attempt))`. Randomization spreads retries over time, avoiding synchronized waves ("thundering herd").

**24.** 502: gateway/proxy received an invalid response from upstream; 503: service unavailable (overloaded/maintenance); 504: gateway timed out waiting for upstream.

**25.** In-DC: 25 × (0.5 + 2) = **62.5 ms**. Cross-region: 25 × (80 + 2) = **2,050 ms**. Lesson: sequential cross-region calls are deadly; batch/parallelize/colocate.

**26.** 2 MB ≈ 1,370 segments of 1,460 B. Window per RTT: 10, 20, 40, 80, 160, 320, 640 → cumulative 10, 30, 70, 150, 310, 630, 1,270; need one more round (RTT 8) for the remaining ~100. Plus handshake (1 RTT). At 50 ms RTT: ~8–9 RTTs ≈ **400–450 ms**; at 5 ms RTT: **~45 ms**. (Real values differ; the shape matters.)

**27.** (a) CPU limit: 8 cores / 0.010 s = **800 req/s**. (b) Little's Law: concurrency = 800 × (0.010 + 0.090) = 800 × 0.1 = **80** concurrent requests: well above 8 threads, showing why I/O-bound services need many concurrent in-flight requests per core.

**28.** 2,000 × 60 = **120,000** TIME_WAIT sockets, far more than the ~28k–64k ephemeral ports available for one (src IP→dst IP:port), so you exhaust ports and new connections fail. Fix: connection pooling/keep-alive (reuse connections), multiple source IPs/backends, tuning (`tcp_tw_reuse`), HTTP/2.

**29.** Techniques: (1) regional deployment/edge presence; (2) aggregate into one call (BFF/GraphQL/batch endpoint) → 1 RTT instead of 6; (3) parallelize independent calls; (4) cache at CDN/edge and on-device; (5) keep-alive/HTTP/2/HTTP/3 with warm connections and TLS 1.3 resumption; (6) prefetch/stale-while-revalidate; (7) compress and minimize payloads; (8) local-first UI with background sync.

**30.** MQTT (or custom binary protocol) over TLS with keepalive interval < NAT timeout (e.g., 30 s); or UDP/CoAP/QUIC with heartbeats; batch records (e.g., 1 min of data per message) with compression; sequence numbers and acknowledgments for idempotent ingest; store-and-forward in device flash with bounded buffers and drop policies; reconnect with exponential backoff + jitter to avoid reconnection storms; device certificates for mutual authentication.

**31.** Threads blocked on the DB → pool exhaustion; CPU idle because the bottleneck is I/O wait. Immediate: add timeouts, shed load/reduce concurrency, kill long queries, scale DB or restart stuck pools. Structural: bulkheads per dependency, circuit breaker, caching, query optimization/indexes, connection pool sizing, async I/O, load testing for latency inflation.

**32.** Possible causes: LB/proxy idle timeout (~300 s) closing connections; NAT/firewall idle expiry; server graceful-shutdown/restarts (deploys closing connections); keep-alive timeout mismatch between client and server; TCP keepalive disabled; connection-per-instance rotation by autoscaler; max-connection-age settings on proxies.

**33.** (1) DNS resolver issues in that country (test with alternate resolvers); (2) BGP/routing problem or ISP outage (traceroute/looking glass); (3) government/ISP blocking (compare IP vs domain access); (4) CDN edge outage in that region (check edge IPs, status pages); (5) TLS/certificate issues (OCSP/CA reachability), MTU/path issues. Test using probes from multiple vantage points (RUM/synthetic checks).

**34/35.** Sample structure in §1.9 and §1.6.3: describe DNS → TCP → TLS → HTTP → LB/CDN → app → cache/DB → response → render; performance: connection reuse, CDN, HTTP/2/3. UDP when timeliness > reliability or building custom transport; not always, because reliability/ordering/congestion control then become the application's burden and UDP may be blocked or abusable.

**36–38.** Self-check. Expect TCP connect ≈ 1 RTT; TLS 1.3 ≈ 1 RTT (≈1×TCP); with CPU-bound work, throughput plateaus at the number of cores; `LongAdder` outperforms `AtomicInteger`, which outperforms `synchronized` under contention.

**39.** Analogy: a new highway ramp metered for safety. The driver (TCP) starts slowly and speeds up only after seeing no accidents. A reused connection is already at highway speed. Technical: slow start + handshake cost, avoided by pooling and proximity.

---

## 1.17 Flashcards

| Front | Back |
|---|---|
| RAM vs SSD vs HDD latency | ~100 ns / ~100 µs / ~5–10 ms |
| In-DC vs cross-ocean RTT | ~0.5 ms / ~100–250 ms |
| Little's Law | concurrency = arrival rate × latency |
| CPU-bound vs I/O-bound | Waiting on compute vs waiting on I/O; I/O-bound benefits from high concurrency |
| Race condition | Outcome depends on thread timing; e.g., lost update on `counter++` |
| Event loop caveat | A blocking handler stalls all connections |
| TCP 4-tuple | src IP, src port, dst IP, dst port |
| TCP handshake cost | 1 RTT |
| TLS 1.3 vs 1.2 | 1 RTT vs 2 RTT handshake |
| Slow start | Window starts ~10 segments, doubles per RTT |
| BDP | bandwidth × RTT = data in flight needed to fill the pipe |
| HOL blocking | Lost packet stalls all later data in an ordered stream |
| QUIC | UDP-based, independent streams, 1-RTT/0-RTT, connection migration |
| DNS TTL trade-off | Short: fast changes, more queries; long: slow propagation |
| Timeout meaning | Unknown outcome: not "it failed" |
| Retry rules | Transient + idempotent + backoff + jitter + budget + one layer |
| 502 / 503 / 504 | Bad upstream response / unavailable / upstream timeout |
| TIME_WAIT | Post-close socket state (~60 s); exhaustion → port starvation |
| Slow vs dead dependency | Slow is worse (holds resources) |
| NAT consequence | Servers can't initiate to phones; devices keep outbound connections |

## 1.18 Glossary additions

| Term | Simple Meaning | Technical Meaning | Why It Exists | Example |
|---|---|---|---|---|
| CPU core | One worker | Independent execution unit | Parallel compute | 8-core server |
| Cache (CPU) | Pocket memory | L1/L2/L3 on-chip memories | Hide RAM latency | Cache locality |
| RAM | Working memory | Volatile main memory | Fast working set | 64 GB |
| IOPS | Disk operations/s | I/O operations per second | Measures random I/O capacity | NVMe 500k IOPS |
| fsync | "Really save it" | Flush OS buffers to stable storage | Durability | DB commit |
| Process / Thread | Program / worker inside | Isolated execution / shared-memory flow | Concurrency | Web server threads |
| Context switch | Changing task | OS swaps running thread | Time-sharing | Overhead at scale |
| Race condition | Timing bug | Result depends on interleaving | Concurrency hazard | `counter++` |
| Deadlock | Mutual waiting | Circular lock wait | Concurrency hazard | A↔B locks |
| Event loop | One waiter, many tables | Readiness-driven single-thread I/O | C10K efficiency | Nginx, Node |
| File descriptor | Handle to open file/socket | OS resource ID | Limits connections | ulimit -n |
| IP / Port | Machine / door | Network address / service id | Routing | 10.0.1.5:443 |
| NAT | Shared public address | Address/port translation | IPv4 scarcity | Home router |
| MTU | Max packet size | Largest link-layer payload | Fragmentation | 1500 B |
| Propagation / transmission / queuing delay | Travel / push / wait | Latency components | Explain latency | Physics + congestion |
| BDP | Bytes in flight | bandwidth × RTT | Window sizing | 1.25 MB |
| DNS / TTL | Phone book / cache time | Hierarchical name system / record lifetime | Naming | A record, 300 s |
| TCP / UDP / QUIC | Reliable / fast / modern | Transport protocols | Delivery models | HTTP / DNS / HTTP-3 |
| Handshake | Hello exchange | Connection setup | Establish state | SYN/SYN-ACK/ACK |
| Slow start / cwnd | Cautious ramp-up | Congestion window growth | Protect network | 10→20→40 |
| HOL blocking | Front-of-line stall | Ordered delivery blocks later data | Protocol limit | TCP loss |
| TLS / Certificate / CA | Encryption / ID card / issuer | Secure channel / signed public key / trust anchor | Security | HTTPS |
| Timeout | Give-up timer | Max wait duration | Bound resource use | 2 s read timeout |
| Retry / backoff / jitter | Try again / wait longer / randomly | Resilience tactics | Transient faults | Full jitter |
| Idempotent | Safe to repeat | Same effect if repeated | Safe retries | PUT, DELETE |

---

**Next: Chapter 2, Web Applications and APIs: How Software Talks.** You'll learn client/server architecture in depth, REST and API design, authentication and sessions, stateless vs stateful servers, real-time channels (polling, WebSockets, SSE), and build a tiny API with idempotency keys.

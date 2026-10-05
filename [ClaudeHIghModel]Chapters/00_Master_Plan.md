# System Design: The Deep-Dive Book
### Master Plan, Table of Contents, and Chapter Standard

This is the long-form version of the course. The earlier `system-design-course/` folder is the *map*; this book is the *territory*. Each chapter is meant to be a self-contained, deep lesson you can study over several sittings.

---

## 1. What every chapter contains (the standard)

Every chapter follows the same skeleton, so you always know where you are:

| Section | Purpose |
|---|---|
| **Chapter card** | Objective, why it exists, prerequisites, tier labels, estimated study time, mastery criteria |
| **The Story** | A concrete scenario that creates the need for the concept *before* the concept appears |
| **Concept sections** | Each concept uses the 13-step sequence: Problem → Naive → Why it breaks → Better → Intuition → Definition → Architecture → Example → Trade-offs → Failure cases → When to use / not → Interview view → Practice |
| **Pause & Predict boxes** | Stop-and-think moments. Answer on paper *before* reading on |
| **Worked examples** | Real numbers, step by step |
| **Code lab** | Small Java programs (C++ noted where useful) to *feel* the concept |
| **Common misconceptions** | The wrong mental models people actually hold |
| **Chapter summary + mental models** | One-page recap |
| **Exercises** | Conceptual (10+), design, debugging, interview, teach-back |
| **Full answer key** | Reasoning, not just answers |
| **Flashcards** | For spaced revision |
| **Glossary additions** | New terms with the 5-column format |

**Labels:** `[MK]` Must Know · `[GK]` Good to Know · `[ADV]` Advanced · `[EXP]` Expert/Optional
**Honesty tags for real systems:** `[Public]` documented by the company · `[Inferred]` strongly implied · `[Approximation]` educational reconstruction.

**How to study a chapter (the loop):**
1. Skim the chapter card and summary first (2 min), so you know the destination.
2. Read each section; at every *Pause & Predict*, write your answer before reading further.
3. Run the code lab (or at least read and predict the output).
4. Do exercises *without* the answer key. Then compare.
5. For every miss, write: *what did I believe, and why was it wrong?*
6. Teach it back aloud. If you stumble, re-read that section.
7. Add flashcards to your spaced-repetition deck (Anki or paper).

**Gate rule:** ≥ 80% on conceptual questions and a clean teach-back before the next chapter.

---

## 2. Table of contents

### Part I: Foundations (how software and data work)
| Ch | Title | Tier | Status |
|---|---|---|---|
| 0 | What Is System Design? Thinking in Systems | MK | ✅ Written |
| 1 | Computers and Networks: The Physical Reality | MK | ✅ Written |
| 2 | Web Applications and APIs: How Software Talks | MK | ✅ Written |
| 3 | Relational Databases: Storing Truth | MK | ✅ Written |
| 4 | NoSQL and Storage Engines | MK/GK | ✅ Written |
| 5 | Performance and Capacity Estimation | MK | ✅ Written |

### Part II: Scaling (how systems grow)
| Ch | Title | Tier | Status |
|---|---|---|---|
| 6 | Scalability and Stateless Design | MK | ✅ Written |
| 7 | Load Balancers, Proxies, and Gateways | MK | ✅ Written |
| 8 | Caching | MK | ✅ Written |
| 9 | Replication | MK | ✅ Written |
| 10 | Partitioning and Sharding | MK | ✅ Written |
| 11 | CDNs and Object Storage | MK | ✅ Written |
| 12 | Asynchronous Processing and Queues | MK | ✅ Written |
| 13 | Event Streaming and Kafka in Depth | GK | ✅ Written |

### Part III: Distributed Systems (how systems fail and agree)
| Ch | Title | Tier | Status |
|---|---|---|---|
| 14 | Distributed Systems Fundamentals | MK | ✅ Written |
| 15 | Consistency Models and the CAP Theorem | MK/GK | ✅ Written |
| 16 | Consensus, Leader Election, and Coordination | ADV | ✅ Written |
| 17 | Distributed Transactions, Sagas, Event Sourcing, CQRS | ADV | ✅ Written |
| 18 | Reliability and Fault Tolerance | MK | ✅ Written |
| 19 | Microservices and Architectural Patterns | GK | ✅ Written |
| 20 | Probabilistic Structures, Clocks, CRDTs, Gossip | EXP | ✅ Written |

### Part IV: Production (how systems are run)
| Ch | Title | Tier | Status |
|---|---|---|---|
| 21 | Observability | GK | ✅ Written |
| 22 | Security for System Designers | MK/GK | ✅ Written |
| 23 | Cloud, Containers, and Infrastructure | GK | ✅ Written |

### Part V: Practice (how you think and perform)
| Ch | Title | Tier | Status |
|---|---|---|---|
| 24 | The Design Method and Interview Framework | MK | ✅ Written |
| 25 | Problem Book I: Beginner (URL shortener, Pastebin, rate limiter, file upload, chat, LRU cache) | MK | ✅ Written |
| 26 | Problem Book II: Intermediate (feed, notifications, Instagram, Dropbox, crawler, typeahead) | MK | ✅ Written |
| 27 | Problem Book III: Upper-intermediate (YouTube, WhatsApp, Uber, Ticketmaster, Yelp, ad aggregator, telematics) | GK | ⏳ Next (paused at user's request) |
| 28 | Problem Book IV: Advanced (Amazon, Netflix, Drive, payments, message queue, Dynamo-style KV) | ADV | ⏳ |
| 29 | Deconstructing Real Systems | GK | ⏳ |
| 30 | Capstone Studio: Unseen Problems | EXP | ⏳ |
| A | Appendix: Master glossary, formula sheet, latency table, reading list, mock-interview scripts | | ⏳ |

Hands-on Java/C++ labs are embedded inside the chapters (LRU cache, token bucket, consistent hashing, Bloom filter, mini message queue, mini KV store, URL shortener v1–v8).

---

## 3. Dependency graph (what must come before what)

```text
Ch0 ─▶ Ch1 ─▶ Ch2 ─▶ Ch3 ─▶ Ch4
                │        └──────▶ Ch5 ─▶ Ch6 ─▶ Ch7 ─▶ Ch8 ─▶ Ch11
                │                          │              │
                │                          └▶ Ch9 ─▶ Ch10 ┘
                └──────────────────────────▶ Ch12 ─▶ Ch13
Ch9 + Ch10 + Ch12 ─▶ Ch14 ─▶ Ch15 ─▶ Ch16 ─▶ Ch17
Ch14 ─▶ Ch18 ─▶ Ch19 ─▶ Ch20(optional)
Ch14–19 ─▶ Ch21, Ch22, Ch23
All ─▶ Ch24 ─▶ Ch25–28 ─▶ Ch29 ─▶ Ch30
```

## 4. Pacing guide (6-month track)
Ch0–2: month 1 · Ch3–5: month 2 · Ch6–11: month 3 · Ch12–15: month 4 · Ch16–23: month 5 · Ch24–30: month 6. Problem books should be interleaved with the chapters that unlock them: try a Problem Book I problem right after Ch8, Level II after Ch13, and so on.

## 5. A note on accuracy
Technology specifics (cloud service names, product features, version numbers) change. Whenever a chapter cites a specific product detail, verify against current documentation. Concepts (replication, consensus, caching) are stable; product details are not.

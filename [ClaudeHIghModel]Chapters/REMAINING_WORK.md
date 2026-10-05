# System Design Book: What Is Left

*A high-level status note. Last updated after Batch 7 (Chapters 24–26). Work is paused at your request.*

---

## 1. Status at a glance

| Part | Chapters | Status |
|---|---|---|
| **I. Foundations** | Ch 0–5 | ✅ Written |
| **II. Scaling** | Ch 6–13 | ✅ Written |
| **III. Distributed Systems** | Ch 14–20 | ✅ Written |
| **IV. Production** | Ch 21–23 | ✅ Written |
| **V. Practice** | Ch 24–26 ✅ written; Ch 27–30 ⏳ | **Partly done** |
| **Appendix** | A | ⏳ **Not started** |

**Done:** 27 chapters (Ch 0–26) plus the master plan, about 530,000 words.
**Left:** 4 chapters (Ch 27–30) plus the appendix. The method (Ch 24) and the first two problem books (Ch 25–26, twelve solved problems) are done; what remains is the harder problems, real-system case studies, and the capstone.

---

## 2. What remains to be written

*(Chapters 24, 25 and 26 are finished: the nine-step method and rubric; Problem Book I (URL shortener, Pastebin, rate limiter, file upload, chat, LRU cache); Problem Book II (news feed, notifications, photo sharing, file sync, web crawler, typeahead).)*

### Chapter 27: Problem Book III, Upper-intermediate `[GK]`
YouTube-style video platform, WhatsApp-style messaging, Uber-style ride matching, Ticketmaster-style ticketing (the hot-inventory problem), Yelp-style proximity search, an ad-click aggregator, and a telematics ingestion platform (the vehicle-data problem tailored to your embedded background).

### Chapter 28: Problem Book IV, Advanced `[ADV]`
Amazon-style e-commerce, Netflix-style streaming, a Google Drive-style collaborative store, a payments system, a distributed message queue, and a Dynamo-style key-value store. These tie together consensus, transactions, and multi-region design.

### Chapter 29: Deconstructing Real Systems `[GK]`
Reverse-engineering how well-known systems work (with honest `[Public]` / `[Inferred]` / `[Approximation]` labeling), so you can reason about designs you have not memorized.

### Chapter 30: Capstone Studio `[EXP]`
Unseen problems with no model answer provided up front: you design, then compare against a rubric and a worked solution. Includes mock-interview scripts and scoring.

### Appendix A
- Master glossary (all terms from every chapter, consolidated and de-duplicated).
- Formula sheet (queueing, availability, sizing, quorum, hashing, Bloom/HLL, burn rate).
- Latency and throughput reference table (the "numbers every engineer should know").
- Reading list (papers and books referenced throughout).
- Mock-interview scripts.

---

## 3. Suggested order when you resume

1. **Ch 27 → 28** (problem books III and IV; difficulty ramps up). Each could be split into two sub-batches (3-4 problems each) to keep chapters reviewable. Ch 27's telematics problem is the one tied to your embedded background.
2. **Ch 29, Ch 30**, then the **Appendix**.

Rough size: Ch 27 and 28 will be similar in length to Ch 25–26 (about 16–18k words including code and outputs), so the rest is roughly 4–5 more working sessions at the pace so far.

---

## 4. Labs that were written but never executed

The build sandbox had no Docker, databases, or Kubernetes, so these labs have **not** been run. They are written and should work, but if one errors or prints something different from the text, the chapter text may need a fix. If you run them, send me the output and I'll correct anything.

| Chapter | Lab | Needs |
|---|---|---|
| 3 | PostgreSQL / JDBC labs | Postgres |
| 4 | Redis lab | Redis |
| 8 | Lab 4: Redis caching in practice | Docker (Redis) |
| 9 | Lab 2: PostgreSQL streaming replication | Docker (Postgres) |
| 10 | Lab 2: native table partitioning in PostgreSQL | Postgres |
| 11 | Lab 2: MinIO and pre-signed URLs | Docker (MinIO) |
| 12 | Lab 2: Postgres as a job queue (`SKIP LOCKED`) | Postgres |
| 13 | Lab 2: real Kafka | Docker (Kafka) |
| 14 | Lab 3: fault injection (Toxiproxy / `tc netem`) | Docker / Linux |
| 15 | Lab 2: Jepsen-style test on etcd / Cassandra | Docker |
| 16 | Lab 2: etcd in practice | Docker |
| 17 | Lab 2: PostgreSQL two-phase commit | Postgres |
| 21 | Lab 2: OpenTelemetry instrumentation | Docker |
| 22 | Lab 2: Keycloak, OAuth 2.0 / OIDC with PKCE | Docker |
| 23 | Lab 2: Kubernetes deployment on kind / minikube | Docker + kind |

Every Java simulation lab (the ones with embedded measured output, including all ten labs of Ch 24–26) **was** run, and its code in the chapter is identical to the code that produced the quoted numbers. Note that timing-based figures (microseconds, operations per second) vary from run to run on a different machine; ratios and orders of magnitude are the lesson.

---

## 5. Known limitations and honest caveats

- **Simulations, not production measurements.** The measured results (queueing, caching, replication, Raft, sagas, observability, security, cloud cost) come from simulations I wrote, often with simplified models. They illustrate mechanisms and orders of magnitude; they are not benchmarks of real systems. Several chapters call out specific model limits.
- **Random testing is evidence, not proof.** The Raft simulator's chaos tests found two of three deliberately injected bugs but missed the subtle one (documented in Ch 16).
- **Prices, limits, and product details change.** Cloud list prices, service limits (S3 part limits, Kubernetes scale limits), and OWASP parameters are labeled illustrative or `[Public]`. Verify against current documentation before relying on any figure.
- **Real-system claims** are tagged `[Public]` where drawn from published papers or documentation, but I did not browse the web while writing, so recent changes in specific products may not be reflected.
- **Not covered in depth** (by design, or deferred): detailed ML/data-platform system design, deep SQL query-optimizer internals, mobile/client architecture, and low-level hardware/firmware design. Ch 27's telematics problem is the main bridge to your embedded domain.

---

## 6. Optional follow-ups (not required, offered if useful)

- **Answer checking:** send your answers to any chapter's exercises and I'll grade them using the teaching-loop style (what you believed, why it was wrong).
- **Spaced-repetition export:** convert all chapter flashcards into one Anki-importable CSV.
- **Single-file build:** merge all chapters into one PDF or EPUB with a table of contents, for offline reading.
- **Consistency pass:** one review sweep across Ch 0–23 for cross-reference accuracy, terminology consistency, and any numbers that appear in more than one chapter.
- **Quick-start path:** a condensed 4-week study plan that selects the must-know (`[MK]`) sections only.

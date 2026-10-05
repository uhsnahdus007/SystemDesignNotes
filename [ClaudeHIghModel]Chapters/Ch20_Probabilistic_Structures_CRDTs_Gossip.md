# Chapter 20: Probabilistic Data Structures, Clocks Revisited, CRDTs, and Gossip

## Chapter card

| | |
|---|---|
| **Objective** | Master the “expert toolbox” for very large and highly distributed systems: probabilistic data structures that trade exactness for tiny memory (Bloom filters, Count-Min sketch, HyperLogLog, quantile sketches, sampling), Merkle trees for cheap reconciliation, version vectors and hybrid clocks, CRDTs that converge without coordination, and gossip protocols for dissemination and membership. Every structure is built and measured in the labs |
| **Why this chapter exists** | At scale, exact answers become too expensive: you cannot keep a set of 100 billion URLs in memory, coordinate every counter increment across continents, or compare billions of keys byte-by-byte. These tools appear inside Cassandra, Dynamo, Redis, Kafka, Elasticsearch, Spark, CDNs, and collaborative editors. They are also favourite “senior-level” interview material |
| **Prerequisites** | Chapters 4 (LSM trees, Bloom filters preview), 8 (cache admission, penetration), 9 (anti-entropy, version vectors), 10 (hashing), 14–15 (clocks, consistency) |
| **Study time** | 12–16 hours including labs |
| **Tier** | `[EXP]` for the internals; the “when to use what” layer is `[GK]` |
| **Mastery criteria** | (1) Size a Bloom filter for a target false-positive rate and explain its guarantees. (2) Explain Count-Min error bounds and use it for heavy hitters. (3) Explain HyperLogLog’s intuition, error formula, and mergeability. (4) Choose among sketches for quantiles, sampling, similarity. (5) Explain Merkle-tree reconciliation and its cost. (6) Contrast version vectors, Lamport, and hybrid clocks. (7) Define CRDT convergence conditions; design counters, sets, registers, and maps; explain add-wins semantics and tombstone costs. (8) Analyze gossip protocols (push/pull/push-pull, fanout, rounds, loss). (9) Recognize which invariants CRDTs cannot provide |

---

## 20.1 The Story: Four Problems That Exact Answers Cannot Solve

**AdMetrics** processes 2 billion ad events per day for 10,000 campaigns on infrastructure spread across five regions.

1. **“How many unique users saw campaign 17 today?”** An exact answer needs a set of every user ID per campaign. At 400 million users for the biggest campaigns, a hash set costs ~16–40 GB *per campaign*. Ten thousand campaigns: impossible.
2. **“Have we already crawled this URL?”** The crawler discovers 50 billion URLs; looking each one up in a database costs a disk read; keeping all in RAM is out of the question.
3. **“What are the top 20 trending hashtags right now?”** 3 million events/s over 500 million distinct tags; counting them all exactly requires a giant, constantly mutated table.
4. **Replicas drift.** A shopping-cart service is replicated in five regions and must keep accepting writes during partitions; two regions add and remove items concurrently. Meanwhile, two replicas of a 1-billion-key table must be compared and synchronized without scanning every key. And 1,000 cluster nodes must learn about each other’s failures without any central coordinator.

### Pause & Predict 20.1
> What would you accept to be wrong about in each case in return for being able to solve the problem at all? Is a *slightly wrong* unique-visitor count acceptable? Is it acceptable to occasionally say “maybe crawled” when a URL is new? Is it acceptable to say “not crawled” for a URL that was? For the replicas: what property would let two concurrent updates be merged in any order with the same result?

---

## 20.2 The trade space `[GK]`

Probabilistic structures trade **exactness** for **space and speed**, with **quantified error**:
| Property | Why it matters |
|---|---|
| **Sublinear space** | Kilobytes to megabytes instead of gigabytes |
| **One-sided or bounded error** | Bloom filter: no false negatives; Count-Min: never underestimates; HLL: relative error ±1.04/√m |
| **Streaming** | One pass over data; constant-time updates |
| **Mergeable** | Sketches from different machines/regions combine (OR, max, add) so aggregation is distributed |
| **Tunable** | Spend more memory to shrink error |
Use them when the **business question tolerates a known error** (unique counts, trending, “probably seen”) or when the structure is a **filter in front of an exact one** (a Bloom filter that avoids a disk read; the exact check still runs on a positive).

### 20.2.1 Hashing foundations `[GK]`
All of these rely on hash functions that behave like random.
- Good non-cryptographic hashes: **MurmurHash3, xxHash, CityHash, SipHash** (SipHash/keyed hashes resist adversarial inputs), the splitmix64 finalizer used in our labs. Cryptographic hashes (SHA-256) are slower than needed unless integrity or security matters.
- **Independence:** a structure with *k* hash functions does not need *k* separate functions: **double hashing** `h_i(x) = h1(x) + i·h2(x) mod m` gives near-equivalent behavior (Kirsch–Mitzenmacher).
- **Collisions:** hashing n items into m buckets yields about n²/(2m) colliding pairs (birthday bound); with a 64-bit hash, 5 billion items collide with probability ≈ n²/2⁶⁵ ≈ 0.07%, so 64-bit hashes are fine for most sketches.
- **Adversaries:** predictable hashes allow “hash flooding” (HashDoS) attacks; use random per-process seeds or keyed hashes on untrusted input. Sketches that must be *mergeable across machines* need the **same seeds everywhere**.

---

## 20.3 Bloom filters `[EXP]`

### 20.3.1 The idea
A **Bloom filter** (Burton Bloom, 1970) answers “is x in the set?” with **“definitely not”** or **“probably yes.”**
- A bit array of *m* bits, all 0; *k* hash functions.
- **Add(x):** set bits `h_1(x), …, h_k(x)`.
- **Query(x):** if *any* of the *k* bits is 0 → **definitely not present**; if all are 1 → **probably present** (false positive possible because other items may have set those bits).
- **No false negatives** (an added item’s bits stay set). **No deletions** (clearing a bit could erase other items).
```text
 add("cat"):  bits 3, 9, 14 → 1         add("dog"): bits 5, 9, 17 → 1
 query("cow") → bits 3, 5, 17 → all 1  → "probably present"  (a false positive: none of these came from "cow")
 query("elk") → bit 8 is 0              → "definitely not present"
```

### 20.3.2 The math
After inserting *n* items, the probability a given bit is still 0 is (1 − 1/m)^{kn} ≈ e^{−kn/m}. A query for an absent item is a false positive if all *k* bits are 1:
> **p ≈ (1 − e^{−kn/m})^k**
For a given m/n (bits per element), the optimal number of hashes is **k = (m/n)·ln 2 ≈ 0.693·(m/n)**, giving **p ≈ 0.6185^{m/n}**. Solving for size given a target false-positive rate *p*:
> **m/n = −ln p / (ln 2)² ≈ 1.44·log₂(1/p) bits per element**
Rules of thumb: **~10 bits/element → ~1% FPR (k = 7)**; **~14.4 bits/element → 0.1%**; each extra ~4.8 bits divides the FPR by 10.

### 20.3.3 Measured (Lab 1, part 1)
1,000,000 keys inserted; 1,000,000 *different* keys queried:
```text
bits/key   hash fns k   memory     false-positive rate: measured     theory (1-e^(-kn/m))^k
    4         3         0.50 MB           14.704%                        14.689%
    6         4         0.75 MB            5.651%                         5.606%
    8         6         1.00 MB            2.140%                         2.158%
   10         7         1.25 MB            0.825%                         0.819%
   12         8         1.50 MB            0.310%                         0.314%
   16        11         2.00 MB            0.045%                         0.046%
check: inserted keys reported absent (must be 0): 0
```
The formula predicts the measured false-positive rate to within a few percent; there were **zero false negatives**. At 10 bits per key the filter occupies **1.25 MB** versus ~16–40 MB for an exact hash set of the same keys (13–30× smaller) and answers “no” correctly 99.2% of the time for absent keys.

**Overfilling is dangerous.** The design FPR holds only for *n* items. Insert 2n into a filter sized for n at 10 bits/key (k = 7) and the effective load is 5 bits/key: p = (1 − e^{−7·2/10})^7 = (0.753)^7 ≈ **13.8%**, 17× worse. Monitor fill ratio (fraction of 1 bits; at optimum it is 50%) and rebuild or use a **scalable Bloom filter** (a chain of filters with tightening FPRs).

### 20.3.4 Variants `[EXP]`
| Variant | Idea | Trade-off |
|---|---|---|
| **Counting Bloom filter** | Counters (3–4 bits) instead of bits → supports delete | 3–4× memory; counter overflow |
| **Blocked Bloom filter** | All *k* bits within one cache-line-sized block → 1 cache miss per query | Slightly higher FPR |
| **Cuckoo filter** | Stores fingerprints in a cuckoo hash table → supports delete, better space below ~3% FPR | Insert can fail when full |
| **Quotient / XOR / Ribbon filters** | Compact fingerprint structures; XOR/Ribbon are static (built once) with ~1.23 bits/key overhead above the information-theoretic minimum | Static or complex |
| **Scalable Bloom filter** | Add new sub-filters as it fills | More lookups per query |
| **Partitioned / distributed** | Shard the filter by hash(key) | Operational complexity |

### 20.3.5 Where Bloom filters are used `[GK]`
- **LSM-trees (Ch 4):** one filter per SSTable; a lookup for a missing key consults filters and skips nearly all disk reads (the MiniLsm lab cut 54,000 disk reads to 527). Cassandra, HBase, RocksDB, LevelDB use them.
- **Cache penetration (Ch 8):** a filter of valid keys in front of the DB rejects requests for non-existent keys without touching the database.
- **Crawlers / dedup:** “have I seen this URL/event ID?” (false positives mean occasionally skipping a new URL: acceptable; false negatives would re-crawl: not allowed by design).
- **Admission control for caches:** only cache an item on its second request (the first sighting recorded in a Bloom filter) to avoid “one-hit wonders” (CDNs).
- **Database joins** (bloom joins): ship a filter of join keys to prune remote scans.
- **Security / blocklists:** quick “definitely safe” check before an expensive lookup.
**Rule:** use a Bloom filter as a **cheap negative filter in front of an expensive exact check**; never as the sole source of truth when a false positive causes harm.

---

## 20.4 Count-Min sketch and heavy hitters `[EXP]`

### 20.4.1 The idea
A **Count-Min sketch** (Cormode & Muthukrishnan, 2005) estimates *frequencies* of items in a stream using a *d × w* array of counters and *d* hash functions.
- **Update(x, c):** for each row *i*, `T[i][h_i(x)] += c`.
- **Estimate(x):** `min_i T[i][h_i(x)]`.
Because every counter holds the true count plus collisions from other items, each row **overestimates**; taking the **minimum** across *d* independent rows gives the tightest estimate. **It never underestimates.**

**Guarantee:** with `w = ⌈e/ε⌉` and `d = ⌈ln(1/δ)⌉`, the estimate is within **ε·N** of the true count with probability ≥ **1 − δ**, where *N* is the total number of events. Memory: *w·d* counters (e.g., ε = 0.1%, δ = 1% → w = 2,719, d = 5 → 13,595 counters ≈ 54 KB with 4-byte counters, regardless of how many distinct items).

### 20.4.2 Measured (Lab 1, part 2)
5,000,000 events over 1,000,000 distinct items with Zipf(1.1) popularity; depth d = 4. Exact counts would take a 4 MB int array here (or ~40 MB as a hash map):
```text
width w     memory     avg relative error, top-100 items     avg ABSOLUTE overcount, all items     top-10 correct?   guarantee: error <= e/w * N  (N=5M)
  1024      0.02 MB             18.10%                                 1492.2                          10 / 10                 13,273
  4096      0.07 MB              3.20%                                  274.5                          10 / 10                  3,318
 16384      0.26 MB              0.59%                                   46.6                          10 / 10                    830
 65536      1.05 MB              0.09%                                    6.6                          10 / 10                    207
```
Observations: (1) the *average* absolute overcount is far below the worst-case bound (1,492 vs 13,273 at w = 1,024) because most counters see little collision traffic; (2) relative error is tiny for **heavy hitters** (0.09% for the top-100 at 1 MB) and large for rare items, whose counts are dominated by collision noise: **Count-Min is good at finding what is frequent, not at counting what is rare**; (3) the top-10 were identified correctly even with a 20 KB sketch.

### 20.4.3 Heavy hitters (top-K) `[EXP]`
Pair the sketch with a small **min-heap of the K best candidates**: on each update, estimate the item’s count; if it exceeds the heap minimum, insert it. Alternatives: **Misra–Gries** and **Space-Saving** (deterministic counters for the k most frequent items, with bounded error N/k), good when K is small. Uses: trending topics, top talkers/DDoS detection, hot-key detection (Ch 10), rate limiting by approximate counts, **TinyLFU** cache admission (Ch 8; Caffeine uses a Count-Min–style frequency sketch with periodic aging). Sketches are **mergeable by addition** (same dimensions and seeds), enabling per-server sketches combined centrally. Variants: **conservative update** (only increment counters equal to the current minimum → lower error), **Count sketch** (signed counters, unbiased, supports negative updates).

---

## 20.5 HyperLogLog `[EXP]`

### 20.5.1 Intuition
To estimate the number of *distinct* items without storing them: hash each item to a uniformly random bit string. In a set of *n* distinct hashes, the **longest run of leading zeros** is about log₂ n (the chance that one hash starts with r zeros is 2^−r, so you need ~2^r items before you see it). One such estimate is very noisy, so HyperLogLog (Flajolet et al., 2007):
1. Splits hashes into **m = 2^p buckets** using the first *p* bits.
2. Each bucket remembers the **maximum rank ρ** (position of the first 1 bit) seen in the remaining bits (6 bits per register is enough).
3. Combines the registers with a **harmonic mean**: `E = α_m · m² / Σ 2^{−M[j]}`, with corrections for small cardinalities (linear counting using the number of empty registers) and large ones.
**Relative standard error ≈ 1.04/√m**, memory ≈ 6m bits, independent of the cardinality (up to ~10⁹+ with 64-bit hashes).

### 20.5.2 Measured (Lab 1, part 3)
20 independent trials per row, each counting 1,000,000 distinct keys (every key added twice, so duplicates don’t matter):
```text
p   registers   memory      mean estimate     typical error (std dev)    theory 1.04/sqrt(m)     (an exact set of 1M keys needs ~16-40 MB)
 6       64        0.0 KB         982,270          12.14%                    13.00%
 8      256        0.2 KB       1,000,809           7.30%                     6.50%
10     1024        0.8 KB       1,012,009           3.26%                     3.25%
12     4096        3.0 KB       1,001,258           1.70%                     1.63%
14    16384       12.0 KB       1,003,784           0.98%                     0.81%
small-range check (p=12, true cardinality 100): estimate = 97.1
merge check: HLL(A) merged with HLL(B) estimate = 1,020,719 ; HLL of the union built directly = 1,020,719 ; true union = 1,000,000
```
With **12 KB** the typical error is **about 1%** while counting a million distinct items (the memory is the same for a billion): a 1,000–3,000× saving over an exact set. The measured error follows 1.04/√m. **Merging** two sketches by taking the register-wise maximum gives *exactly* the sketch of the union (the two estimates are identical: 1,020,719), so per-server or per-region HLLs combine losslessly (this lab’s 2% over the true union is the single-sketch error for p = 12).

### 20.5.3 Practical notes
- **Sizing:** error ε needs m ≥ (1.04/ε)²: 1% → m ≥ 10,816 → p = 14 (16,384 registers, 12 KB); 0.5% → m ≥ 43,264 → p = 16 (48 KB).
- **HLL++** (Google) adds 64-bit hashing, bias correction tables, and a sparse representation for small sets; Redis (`PFADD/PFCOUNT/PFMERGE`, 12 KB per key), BigQuery `APPROX_COUNT_DISTINCT`, Presto/Trino `approx_distinct`, Druid, Elasticsearch `cardinality`, Spark `approx_count_distinct` use it.
- **Set operations:** union is exact on the sketches (max); intersection and difference must be derived by inclusion–exclusion (|A ∩ B| = |A| + |B| − |A ∪ B|), whose errors add up, so they are unreliable when the intersection is small relative to the sets. Use MinHash/theta sketches when you need set algebra.
- **No membership, no deletion, no enumeration.**
- **Time windows:** keep one HLL per time bucket (e.g., per hour); merge buckets to answer day/week queries.

---

## 20.6 Other useful sketches `[GK]`

| Problem | Structure | Idea / properties |
|---|---|---|
| **Quantiles (p50/p99) over huge streams** | **t-digest, DDSketch, HdrHistogram, KLL** | Store compressed summaries (clusters or log-spaced buckets) with relative-error guarantees (DDSketch: α-relative error on every quantile). **Percentiles cannot be averaged across servers (Ch 5), but sketches can be merged**; this is how monitoring systems compute fleet-wide p99 |
| **Uniform sample of a stream of unknown length** | **Reservoir sampling (Algorithm R)** | Keep k items; the i-th item replaces a random reservoir slot with probability k/i; at the end every item had probability k/n of being in the sample. Used for log sampling, trace sampling |
| **Set similarity** | **MinHash + LSH** | Keep the minimum hash under many hash functions; probability two sets agree on a min equals their Jaccard similarity; LSH finds near-duplicates without comparing all pairs |
| **Approximate membership with deletion** | Cuckoo filter | §20.3.4 |
| **Approximate nearest neighbors** | LSH, HNSW graphs, product quantization | Vector search (Ch 28) |
| **Frequency with deletions/ negative counts** | Count sketch | Signed counters |
| **Sliding windows** | Exponential histograms, window sketches | Approximate counts over the last N events |
| **Rate limiting at scale** | Approximate counters per key (Count-Min with decay) | Memory-bounded fairness |

---

## 20.7 Merkle trees and anti-entropy `[GK/EXP]`

### 20.7.1 The idea
A **Merkle (hash) tree**: leaves are hashes of data blocks/key ranges; each internal node is the hash of its children. The root hash summarizes the entire dataset: **if two replicas’ roots are equal, their data is identical** (with overwhelming probability). If they differ, compare the children and descend **only into subtrees whose hashes differ**, reaching exactly the differing ranges.

### 20.7.2 Measured (Lab 2, part 3)
Two replicas, each with 1,048,576 keys (a tree of depth 20), differing in *d* keys placed at random:
```text
differing keys     hash comparisons (tree walk)      naive key-by-key comparisons     leaf ranges to transfer
        0                       1                          1,048,576                          0
        1                      41                          1,048,576                          1
       10                     339                          1,048,576                         10
    1,000                  20,239                          1,048,576                      1,000
  100,000                 740,417                          1,048,576                    100,000
```
One differing key is located with **41 comparisons** (two children per level × 20 levels + the root) instead of a million; 1,000 differences need ~20,000; the savings fade only when a large fraction of keys differ (100,000 differences ≈ 10% of the data, where the walk costs 0.7 of a naive scan, so you would just stream the data).

### 20.7.3 Uses and engineering notes
- **Dynamo/Cassandra/Riak anti-entropy repair:** replicas exchange Merkle trees per key range and sync differing ranges (Ch 9). Building trees is expensive (a full scan/validation compaction), so repairs run periodically and in the background; cost grows with data size.
- **Git** (commit/tree/blob hashes), **blockchains** (transaction trees), **certificate transparency**, **IPFS**, **Btrfs/ZFS checksums**, **rsync-like** block sync, **software update verification**.
- **Merkle proofs:** to prove an element belongs to a set, give the sibling hashes along the path: O(log n) size.
- **Design choices:** branching factor (binary vs 16-ary), leaf granularity (a leaf covering 1,000 keys balances tree size vs transfer granularity), incremental update (update the path to the root on each write; keep trees in memory/disk), hashing cost, concurrency with writes (snapshot or consistent range).

---

## 20.8 Clocks and versioning revisited `[GK]`

Chapter 14 introduced Lamport, vector, and hybrid logical clocks. For replicated data stores:
- **Version vectors** track, per object, a counter per *replica* that updated it (not per client). Comparing two versions yields: *A dominates B* (A saw everything B did: B is obsolete), *B dominates A*, or *concurrent* (neither: **conflict**, keep both as **siblings** or merge). Used in Dynamo, Riak.
- **Dotted version vectors** fix an anomaly where plain version vectors can create false concurrency or lose updates under client-side read-modify-write; they track the single “dot” (replica, counter) of the latest event separately.
- **Interval tree clocks** allow dynamic numbers of replicas.
- **Hybrid logical clocks (HLC)** give causality-respecting timestamps close to wall time (CockroachDB, MongoDB), usable for LWW tie-breaks and snapshot reads.
- **LWW** (last-writer-wins by timestamp) is the cheapest conflict policy and silently loses concurrent updates; with skew (Ch 14: 48.8% wrong at ±200 ms) it can lose *causally later* ones. Prefer version vectors, CRDT merge, or single-writer designs when updates must not vanish.

---

## 20.9 CRDTs: conflict-free replicated data types `[EXP]`

### 20.9.1 The problem and the idea
In an AP system (Ch 15) replicas accept writes independently during partitions and must merge later. If merge is just “last write wins,” updates are lost. A **CRDT** (Shapiro et al., 2011) is a data type designed so that **any replica can be updated locally without coordination, and all replicas that have received the same set of updates are in the same state**, regardless of the order or number of times messages are delivered: **strong eventual consistency (SEC)**. No locks, no consensus, no conflict-resolution UI.

### 20.9.2 Two families
| Family | What is shipped | Requirement | Properties |
|---|---|---|---|
| **State-based (CvRDT)** | The **entire state** (or deltas), merged with `merge(a, b)` | `merge` is **commutative, associative, idempotent** (a *join* on a semilattice) and updates only move the state "up" | Tolerates loss, duplication, reordering; simple; large messages (use **delta-state** CRDTs to ship only changes) |
| **Operation-based (CmRDT)** | **Operations**, applied at each replica | Operations commute (for concurrently generated ops); delivery is **reliable and causal**, exactly-once | Small messages; needs a reliable causal broadcast layer |

### 20.9.3 The building blocks
| CRDT | State | Update | Merge | Notes |
|---|---|---|---|---|
| **G-Counter** (grow-only) | vector of n counters, one per replica | increment own slot | element-wise max | Value = sum of slots |
| **PN-Counter** | two G-Counters (P and N) | inc → P, dec → N | merge each | Value = ΣP − ΣN; cannot enforce `≥ 0` |
| **G-Set** | set | add | union | No removal |
| **2P-Set** | added set + removed set (tombstones) | add, remove | union both | Removed items can never return |
| **OR-Set (Observed-Remove / add-wins)** | element → set of unique **add-tags**; set of removed tags | add: new tag; remove: tombstone *the tags this replica has observed* | union adds, union tombstones | A concurrent add wins over a remove (the remove never saw the new tag) |
| **LWW-Register** | (value, timestamp, replica) | write with timestamp | keep larger | Loses concurrent writes; clock-sensitive |
| **MV-Register** | set of (value, version vector) | write | keep all concurrent values | Application sees siblings (Dynamo cart) |
| **LWW-Map / OR-Map** | key → CRDT | per key | per key | Compose CRDTs; maps of counters etc. |
| **Sequence CRDTs (RGA, Logoot, LSEQ, Yjs, Automerge)** | ordered list with unique position ids | insert/delete at an id | merge by id order | Collaborative text editing; metadata and tombstone overhead |

### 20.9.4 Measured (Lab 2, part 1)
Five replicas perform 120 random operations each run (counter increments/decrements; set adds/removes), exchange **stale copies of state in random order with duplicates**, then do a final all-to-all exchange. 3,000 runs. “Correct” means equal to the semantic meaning of the operations performed:
```text
PN-Counter converges to the exact sum of all increments and decrements:        100.0% of runs
OR-Set (add-wins) equals the semantic truth (adds not observed-removed):      100.0% of runs
Naive set (remove deletes locally, merge = union) equals the truth:            73.4% of runs   (removed elements resurrect)
All three types had all 5 replicas in identical states after anti-entropy:      100.0% of runs
```
**Interpretation.** (1) The CRDTs reach the exact intended outcome **in every run**, though messages were stale, reordered, and duplicated, because merge is commutative, associative, and idempotent. (2) A plain set with “union on merge” also *converges* (all replicas agree, 100% of runs) **but to the wrong answer in 26.6% of runs**: a removed element is resurrected by any replica that never saw the removal. *Convergence alone is not correctness.* (3) The OR-Set’s per-add tags are the price: metadata grows with the number of adds and tombstones until compacted.
(We deliberately did not benchmark LWW sets here: their outcome depends on timestamps and clock skew, which Chapter 14 measured separately, and they define concurrent add/remove differently from add-wins, which would confound the comparison.)

### 20.9.5 Semantics you must choose
- **Concurrent add vs remove:** *add-wins* (OR-Set), *remove-wins*, or LWW. Choose per business meaning (a shopping cart: add-wins avoids losing an item; a revoked permission: remove-wins is safer).
- **Concurrent updates to a register:** LWW (lossy), multi-value (keep siblings), or domain merge (max, union).
- **Counters:** cannot enforce bounds (`balance ≥ 0`, `stock ≥ 0`) without coordination (I-confluence, Ch 15); use escrow/reservations or a coordinated step.
- **Uniqueness constraints:** impossible without coordination.
- **Intention preservation in text:** sequence CRDTs/OT keep users’ intended insertions in place; the interleaving of concurrent inserts at the same position can be surprising (algorithms differ).

### 20.9.6 Costs and engineering
- **Metadata growth:** version vectors (n replicas), tags, tombstones. Garbage collection requires knowing that **all replicas have seen** the operation (causal stability), which needs membership knowledge or periodic coordination.
- **State size:** state-based CRDTs ship large states; use delta CRDTs and compression.
- **Query semantics:** merges produce values that may surprise users (“who deleted my item?”); expose them well.
- **Testing:** property-based tests of commutativity/associativity/idempotence; model checking; fuzzing with random delivery (as in the lab).
- **Production use:** Riak data types, **Redis Enterprise Active-Active (CRDB)**, Akka Distributed Data, AntidoteDB, Automerge and Yjs (local-first/collaborative apps), Figma’s multiplayer (CRDT-inspired), SoundCloud Roshi (LWW-element-set), Apple Notes/others (internal), Azure Cosmos DB conflict policies (LWW or custom merge).
- **CRDT vs OT** (operational transformation, Google Docs): OT transforms concurrent operations against each other, typically with a central server for ordering; CRDTs embed ordering in identifiers so peers can merge without a server. CRDTs favor peer-to-peer/offline; OT has smaller metadata.

---

## 20.10 Gossip protocols `[EXP]`

### 20.10.1 The idea
**Gossip (epidemic) protocols** spread information like a rumor: periodically, each node picks random peers and exchanges information. No central coordinator, no all-to-all broadcast, robust to node and message failures, and convergence time that grows only **logarithmically** with cluster size.
- **Push:** an infected (informed) node sends the update to random peers.
- **Pull:** an uninformed node asks random peers for updates.
- **Push-pull:** both exchange.
- **Rumor mongering:** nodes forward a new update for a while and then stop (reduces traffic; small probability that some nodes are never reached) vs **anti-entropy** gossip (continuously compare state and reconcile, using Merkle trees or digests).

### 20.10.2 Analysis
With *N* nodes and fanout *f*, an update initially at one node:
- Phase 1: the infected set roughly multiplies by (1 + f) per round → ~log_{f+1} N rounds to reach a constant fraction.
- Phase 2: with push alone, the last few uninformed nodes are found slowly (each round, an uninformed node is hit with probability ≈ 1 − e^{−f·fraction informed}); the tail adds ~ln N / f rounds. With **pull** the endgame is fast, since uninformed nodes actively ask (the probability to remain uninformed *squares* each round).
- Total for push with f = 1: **≈ log₂ N + ln N rounds**; push-pull: **≈ log₃ N + O(log log N)** rounds.

### 20.10.3 Measured (Lab 2, part 2)
200 trials per row; each round every informed node (push) and/or uninformed node (pull) contacts `fanout` random peers:
```text
N        mode        fanout  loss  dead    rounds to 99%    rounds to 100%    messages per node    runs reaching everyone    log2(N)
100      push        1       0%    0%       11.4             12.3               5.5               100.0%                   6.6
1000     push        1       0%    0%       15.1             18.0               7.9               100.0%                  10.0
10000    push        1       0%    0%       18.4             23.7              10.3               100.0%                  13.3
1000     push        1       0%    0%       15.1             18.1               8.1               100.0%
1000     pull        1       0%    0%       13.0             13.7              10.0               100.0%
1000     push-pull   1       0%    0%        8.5              9.2               9.2               100.0%
1000     push        2       0%    0%        9.0             10.6               8.4               100.0%
1000     push        3       0%    0%        7.0              8.2               9.0               100.0%
1000     push        2      30%   10%       12.7             14.8              12.5               100.0%   (30% message loss, 10% of nodes dead)
```
**Reading it.** (1) Going from 100 to 10,000 nodes (100×) adds only ~11 rounds: **logarithmic scaling**; each node sends ~5–10 messages in total. (2) The theoretical estimate for push, f = 1, N = 1,000 is log₂ N + ln N = 10 + 6.9 ≈ 16.9 rounds; measured: **18 rounds**. (3) **Push-pull halves the time** (9.2 vs 18.1 rounds) for similar message counts. (4) Increasing fanout from 1 to 3 cuts rounds from 18 to 8 at modest message cost per node (≈8–9 vs 8). (5) With **30% message loss and 10% of the nodes dead**, fanout 2 still informs *every live node in all 200 trials* in ~15 rounds (12.5 messages per node): gossip’s robustness is its selling point. (6) With a round every second (say), a cluster of 1,000 learns an update in about 10–15 seconds: fine for membership and configuration, too slow for real-time notifications.

### 20.10.4 Uses and design
- **Membership and failure detection (SWIM, Ch 14):** gossip membership changes with piggybacked messages; **Consul/Serf (memberlist), Cassandra (gossip of cluster state every second), Redis Cluster bus, Akka Cluster, Dynamo**.
- **Anti-entropy / state reconciliation** with Merkle trees (Cassandra repair).
- **Dissemination of configuration, load information, aggregates** (gossip averaging computes global averages/sums without coordination).
- **Tuning:** gossip interval (e.g., 1 s), fanout (2–3), message size (digests rather than full state), peer selection (random, or topology-aware to prefer local peers), retransmission limits; **bandwidth** per node ≈ fanout × message size / interval (e.g., 3 × 500 B/s = 1.5 KB/s per node, 1.5 MB/s across 1,000 nodes).
- **Weaknesses:** probabilistic guarantees (some nodes may learn later than others), eventual consistency, redundant messages, **security** (malicious nodes can spread falsehoods; use authentication and sign messages), **partition behavior** (each side converges separately), and coordination-needing decisions (use consensus for those, Ch 16).
- **Combining:** Cassandra uses gossip for membership, Merkle trees for repair, Bloom filters for LSM reads, hinted handoff, vector/timestamp LWW for conflicts; Dynamo adds vector clocks and sloppy quorums.

---

## 20.11 Choosing the right tool `[GK]`

| Question | Tool | Error type | Memory |
|---|---|---|---|
| Is x in the set? (negatives matter) | Bloom/cuckoo filter | False positives only | ~10 bits/key for 1% |
| How many distinct items? | HyperLogLog | ±1.04/√m relative | 12 KB for ~1% |
| How often did x occur? What’s hot? | Count-Min + top-K heap | Overestimates by ≤ εN | 10s of KB–MB |
| What is the p99 across the fleet? | DDSketch/t-digest/HdrHistogram | Relative error α | KBs, mergeable |
| Give me a uniform sample | Reservoir sampling | Sampling variance | k items |
| Which items are similar? | MinHash + LSH | Probabilistic | KB per set |
| Do two replicas agree? Where do they differ? | Merkle tree | Hash collision (negligible) | O(n) hashes, log-time diff |
| Merge concurrent updates without coordination | CRDT (counter, OR-Set, register, map, sequence) | None (semantics chosen) | Metadata overhead |
| Spread an update/membership across thousands of nodes | Gossip | Probabilistic delay | Tiny |
| Detect concurrent writes | Version vectors / dotted version vectors | None | O(replicas) |
**Rule of thumb:** *exact where it matters (money, identity, authorization), approximate where it’s analytics, filtering, or eventual by nature.*

---

## 20.12 Failure modes and pitfalls

| Pitfall | Mechanism | Mitigation |
|---|---|---|
| **Bloom filter overfill** | More items than designed → FPR explodes (13.8% at 2× load) | Size for max n, monitor fill ratio, scalable/rebuilt filters |
| **Using a Bloom filter where false positives hurt** | “Probably yes” treated as “yes” | Always verify positives with the exact store |
| **Mismatched sketch parameters/seeds** | Cannot merge | Version and standardize width, depth, seeds, hash function |
| **Weak or predictable hashes** | Skew, collisions, HashDoS | Good hashes, random seeds, keyed hashes for untrusted data |
| **Misreading error bounds** | “1% error” applied to intersections or rare items | Know what the bound covers (relative to N, or to cardinality) |
| **HLL intersections** | Error blows up | Use theta sketches/MinHash or exact methods |
| **Percentile averaging** | Averaging p99s across servers | Mergeable quantile sketches or raw histograms |
| **CRDT metadata growth** | Tombstones/version vectors never GC’d | Causal stability, periodic compaction, bounded membership |
| **Wrong CRDT semantics** | Add-wins vs remove-wins mismatch | Choose per business rule; document |
| **Assuming CRDTs enforce invariants** | Negative balances, duplicate usernames | Escrow/coordination for bounded or unique constraints |
| **Naive “union” merges** | Converges but resurrects deletes (26.6% wrong in lab) | Use OR-Set/tombstones, not plain sets |
| **LWW with skewed clocks** | Silent data loss | Version vectors, HLC with bounds, CRDT/MV registers |
| **Gossip storms** | Fanout/interval too aggressive; large messages | Digests, limits, jitter, adaptive intervals |
| **Slow convergence in large clusters** | Long intervals, lossy networks | Push-pull, topology-aware peers, anti-entropy |
| **Gossip trust** | Malicious/faulty nodes spread bad data | Authentication, signatures, validation |
| **Merkle tree churn** | Frequent updates invalidate subtrees; building trees is costly | Incremental trees, range partitioning, scheduling off-peak |
| **Treating probabilistic as deterministic in tests** | Flaky tests | Seed randomness, test statistically |

---

## 20.13 Labs

### Lab 1: Bloom filter, Count-Min sketch, HyperLogLog (Java 17+)
```java
import java.util.*;

public class ProbLab {
    static long mix(long z) { z += 0x9E3779B97F4A7C15L; z = (z ^ (z >>> 30)) * 0xBF58476D1CE4E5B9L; z = (z ^ (z >>> 27)) * 0x94D049BB133111EBL; return z ^ (z >>> 31); }

    // ------------------------------------------------------------------ Bloom filter
    static class Bloom {
        final BitSet bits; final int m, k;
        Bloom(int m, int k) { this.m = m; this.k = k; bits = new BitSet(m); }
        void add(long key) { long h1 = mix(key), h2 = mix(key ^ 0x5851F42D4C957F2DL) | 1; for (int i = 0; i < k; i++) bits.set((int) Math.floorMod(h1 + i * h2, (long) m)); }
        boolean mightContain(long key) { long h1 = mix(key), h2 = mix(key ^ 0x5851F42D4C957F2DL) | 1; for (int i = 0; i < k; i++) if (!bits.get((int) Math.floorMod(h1 + i * h2, (long) m))) return false; return true; }
    }
    static void bloom() {
        int n = 1_000_000;
        System.out.println("== 1) Bloom filter: 1,000,000 keys inserted; 1,000,000 DIFFERENT keys queried. ==");
        System.out.println("bits/key   hash fns k   memory     false-positive rate: measured     theory (1-e^(-kn/m))^k");
        for (int bpk : new int[]{4, 6, 8, 10, 12, 16}) {
            int k = Math.max(1, (int) Math.round(bpk * Math.log(2))); int m = bpk * n; Bloom b = new Bloom(m, k);
            for (long i = 0; i < n; i++) b.add(i);
            int fp = 0; for (long i = n; i < 2L * n; i++) if (b.mightContain(i)) fp++;
            double theory = Math.pow(1 - Math.exp(-(double) k * n / m), k);
            System.out.printf("  %3d       %3d        %5.2f MB          %7.3f%%                       %7.3f%%%n", bpk, k, m / 8.0 / 1e6, 100.0 * fp / n, 100 * theory);
        }
        // false negatives check
        Bloom b = new Bloom(10 * n, 7); for (long i = 0; i < n; i++) b.add(i); int miss = 0; for (long i = 0; i < n; i++) if (!b.mightContain(i)) miss++;
        System.out.println("check: inserted keys reported absent (must be 0): " + miss);
    }

    // ------------------------------------------------------------------ Count-Min sketch
    static void countMin() {
        int distinct = 1_000_000, stream = 5_000_000; double s = 1.1;
        double[] cdf = new double[distinct]; double sum = 0; for (int i = 1; i <= distinct; i++) { sum += 1.0 / Math.pow(i, s); cdf[i - 1] = sum; }
        Random r = new Random(4); int[] exact = new int[distinct]; int[] stream1 = new int[stream];
        for (int i = 0; i < stream; i++) { double u = r.nextDouble() * sum; int lo = 0, hi = distinct - 1; while (lo < hi) { int mid = (lo + hi) >>> 1; if (cdf[mid] < u) lo = mid + 1; else hi = mid; } stream1[i] = lo; exact[lo]++; }
        System.out.println("\n== 2) Count-Min sketch: 5,000,000 events over 1,000,000 distinct items, Zipf(1.1). depth d=4. Exact counting would need a 4 MB int array (or ~40 MB hash map). ==");
        System.out.println("width w     memory     avg relative error, top-100 items     avg ABSOLUTE overcount, all items     top-10 correct?   guarantee: error <= e/w * N  (N=5M)");
        for (int w : new int[]{1 << 10, 1 << 12, 1 << 14, 1 << 16}) {
            int d = 4; int[][] t = new int[d][w];
            for (int x : stream1) for (int j = 0; j < d; j++) t[j][(int) Math.floorMod(mix(x * 31L + j), (long) w)]++;
            double relErr = 0; int[] top = new int[100]; for (int i = 0; i < 100; i++) top[i] = i;       // ranks 0..99 are the most frequent by construction
            for (int x : top) { int est = Integer.MAX_VALUE; for (int j = 0; j < d; j++) est = Math.min(est, t[j][(int) Math.floorMod(mix(x * 31L + j), (long) w)]); relErr += (double) (est - exact[x]) / exact[x]; }
            double absErr = 0; int sample = 200_000; for (int q = 0; q < sample; q++) { int x = r.nextInt(distinct); int est = Integer.MAX_VALUE; for (int j = 0; j < d; j++) est = Math.min(est, t[j][(int) Math.floorMod(mix(x * 31L + j), (long) w)]); absErr += est - exact[x]; }
            // top-10 by estimate among first 1000 candidate ids
            Integer[] cand = new Integer[1000]; for (int i = 0; i < 1000; i++) cand[i] = i; final int ww = w;
            Arrays.sort(cand, (p, q2) -> { int ep = Integer.MAX_VALUE, eq = Integer.MAX_VALUE; for (int j = 0; j < d; j++) { ep = Math.min(ep, t[j][(int) Math.floorMod(mix(p * 31L + j), (long) ww)]); eq = Math.min(eq, t[j][(int) Math.floorMod(mix(q2 * 31L + j), (long) ww)]); } return Integer.compare(eq, ep); });
            Set<Integer> got = new HashSet<>(Arrays.asList(cand).subList(0, 10)); int right = 0; for (int i = 0; i < 10; i++) if (got.contains(i)) right++;
            System.out.printf("%6d     %5.2f MB            %6.2f%%                              %9.1f                          %2d / 10             %,10.0f%n", w, d * w * 4 / 1e6, 100 * relErr / 100, absErr / sample, right, Math.E / w * stream);
        }
    }

    // ------------------------------------------------------------------ HyperLogLog
    static class Hll {
        final int p, m; final byte[] reg;
        Hll(int p) { this.p = p; m = 1 << p; reg = new byte[m]; }
        void add(long key) { long h = mix(key); int idx = (int) (h >>> (64 - p)); long w = (h << p) | (1L << (p - 1)); int rho = Long.numberOfLeadingZeros(w) + 1; if (rho > reg[idx]) reg[idx] = (byte) rho; }
        double estimate() {
            double alpha = m == 16 ? 0.673 : m == 32 ? 0.697 : m == 64 ? 0.709 : 0.7213 / (1 + 1.079 / m); double z = 0; int zeros = 0;
            for (byte b : reg) { z += Math.pow(2, -b); if (b == 0) zeros++; }
            double e = alpha * m * m / z; if (e <= 2.5 * m && zeros > 0) e = m * Math.log((double) m / zeros); return e;      // small-range correction (linear counting)
        }
    }
    static void hyperLogLog() {
        System.out.println("\n== 3) HyperLogLog: estimate the number of DISTINCT items. 20 independent trials per row, each counting 1,000,000 distinct keys (every key added twice). ==");
        System.out.println("p   registers   memory      mean estimate     typical error (std dev)    theory 1.04/sqrt(m)     (an exact set of 1M keys needs ~16-40 MB)");
        for (int p : new int[]{6, 8, 10, 12, 14}) {
            int trials = 20; double sumErr2 = 0, sumEst = 0; int m = 1 << p;
            for (int t = 0; t < trials; t++) { Hll h = new Hll(p); long off = (long) t * 10_000_000L; for (long i = 0; i < 1_000_000; i++) { h.add(off + i); h.add(off + i); } double est = h.estimate(); sumEst += est; double e = (est - 1_000_000) / 1_000_000; sumErr2 += e * e; }
            System.out.printf("%2d   %6d      %5.1f KB    %,12.0f          %5.2f%%                    %5.2f%%%n", p, m, m * 6 / 8.0 / 1024, sumEst / trials, 100 * Math.sqrt(sumErr2 / trials), 100 * 1.04 / Math.sqrt(m));
        }
        Hll small = new Hll(12); for (long i = 0; i < 100; i++) small.add(i); System.out.printf("small-range check (p=12, true cardinality 100): estimate = %.1f%n", small.estimate());
        // mergeability
        Hll a = new Hll(12), b = new Hll(12), u = new Hll(12); for (long i = 0; i < 600_000; i++) { a.add(i); u.add(i); } for (long i = 400_000; i < 1_000_000; i++) { b.add(i); u.add(i); }
        for (int i = 0; i < a.m; i++) a.reg[i] = (byte) Math.max(a.reg[i], b.reg[i]);
        System.out.printf("merge check: HLL(A) merged with HLL(B) estimate = %,.0f ; HLL of the union built directly = %,.0f ; true union = 1,000,000%n", a.estimate(), u.estimate());
    }

    public static void main(String[] args) { bloom(); countMin(); hyperLogLog(); }
}
```
**Sample output**
```text
== 1) Bloom filter: 1,000,000 keys inserted; 1,000,000 DIFFERENT keys queried. ==
bits/key   hash fns k   memory     false-positive rate: measured     theory (1-e^(-kn/m))^k
    4         3         0.50 MB           14.704%                        14.689%
    6         4         0.75 MB            5.651%                         5.606%
    8         6         1.00 MB            2.140%                         2.158%
   10         7         1.25 MB            0.825%                         0.819%
   12         8         1.50 MB            0.310%                         0.314%
   16        11         2.00 MB            0.045%                         0.046%
check: inserted keys reported absent (must be 0): 0

== 2) Count-Min sketch: 5,000,000 events over 1,000,000 distinct items, Zipf(1.1). depth d=4. Exact counting would need a 4 MB int array (or ~40 MB hash map). ==
width w     memory     avg relative error, top-100 items     avg ABSOLUTE overcount, all items     top-10 correct?   guarantee: error <= e/w * N  (N=5M)
  1024      0.02 MB             18.10%                                 1492.2                          10 / 10                 13,273
  4096      0.07 MB              3.20%                                  274.5                          10 / 10                  3,318
 16384      0.26 MB              0.59%                                   46.6                          10 / 10                    830
 65536      1.05 MB              0.09%                                    6.6                          10 / 10                    207

== 3) HyperLogLog: estimate the number of DISTINCT items. 20 independent trials per row, each counting 1,000,000 distinct keys (every key added twice). ==
p   registers   memory      mean estimate     typical error (std dev)    theory 1.04/sqrt(m)     (an exact set of 1M keys needs ~16-40 MB)
 6       64        0.0 KB         982,270          12.14%                    13.00%
 8      256        0.2 KB       1,000,809           7.30%                     6.50%
10     1024        0.8 KB       1,012,009           3.26%                     3.25%
12     4096        3.0 KB       1,001,258           1.70%                     1.63%
14    16384       12.0 KB       1,003,784           0.98%                     0.81%
small-range check (p=12, true cardinality 100): estimate = 97.1
merge check: HLL(A) merged with HLL(B) estimate = 1,020,719 ; HLL of the union built directly = 1,020,719 ; true union = 1,000,000
```
**Experiments**
1. Insert 2× the designed number of keys into the 10-bits/key Bloom filter and verify the predicted FPR (~13.8%). Then implement a **blocked Bloom filter** and compare FPR and speed.
2. Implement a **counting Bloom filter** (4-bit counters), delete half of the keys, and measure false positives and false negatives (the latter should be 0 unless counters overflow).
3. Implement a **cuckoo filter** and compare the memory needed for a 1% and 0.1% FPR with Bloom.
4. For Count-Min, add **conservative update** and measure the improvement; implement a **top-K tracker** (heap) and compare with Space-Saving on the Zipf stream; test with s = 0.8 (flatter) and 1.5 (skewier).
5. Implement **merge** for Count-Min across 4 shards of the stream and show the merged sketch equals the sketch of the whole stream.
6. For HLL, test with cardinalities 10, 1,000, 10⁶, 10⁸ to see where the small-range correction matters; implement a **sparse representation** for small sets; measure the error of intersection via inclusion–exclusion for |A∩B| = 1%, 10%, 50% of |A|.
7. Implement **reservoir sampling** and verify uniformity with a χ² test. Implement a **DDSketch** with relative error 1% and compare p99 estimates vs exact on a heavy-tailed latency dataset; show that merging 10 sketches gives the fleet-wide p99 while averaging 10 p99s does not.

### Lab 2: CRDTs, gossip, and Merkle trees (Java 17+)
```java
import java.util.*;

public class CrdtGossipLab {
    static long mix(long z) { z += 0x9E3779B97F4A7C15L; z = (z ^ (z >>> 30)) * 0xBF58476D1CE4E5B9L; z = (z ^ (z >>> 27)) * 0x94D049BB133111EBL; return z ^ (z >>> 31); }

    // ================================================================== CRDTs: state-based, merged in arbitrary order with duplication
    static class PNCounter {                                  // two grow-only vectors (one slot per replica)
        long[] inc, dec; PNCounter(int n) { inc = new long[n]; dec = new long[n]; }
        long value() { long v = 0; for (long x : inc) v += x; for (long x : dec) v -= x; return v; }
        void merge(PNCounter o) { for (int i = 0; i < inc.length; i++) { inc[i] = Math.max(inc[i], o.inc[i]); dec[i] = Math.max(dec[i], o.dec[i]); } }
        PNCounter copy() { PNCounter c = new PNCounter(inc.length); c.inc = inc.clone(); c.dec = dec.clone(); return c; }
    }
    static class OrSet {                                      // observed-remove set: element -> set of unique add-tags; removes delete only OBSERVED tags (kept as tombstones)
        Map<Integer, Set<Long>> adds = new HashMap<>(); Set<Long> removed = new HashSet<>();
        void add(int e, long tag) { adds.computeIfAbsent(e, k -> new HashSet<>()).add(tag); }
        void remove(int e) { Set<Long> t = adds.get(e); if (t != null) removed.addAll(t); }        // removes only the tags this replica has SEEN
        Set<Integer> value() { Set<Integer> v = new TreeSet<>(); for (var en : adds.entrySet()) for (long t : en.getValue()) if (!removed.contains(t)) { v.add(en.getKey()); break; } return v; }
        void merge(OrSet o) { for (var en : o.adds.entrySet()) adds.computeIfAbsent(en.getKey(), k -> new HashSet<>()).addAll(en.getValue()); removed.addAll(o.removed); }
        OrSet copy() { OrSet c = new OrSet(); for (var en : adds.entrySet()) c.adds.put(en.getKey(), new HashSet<>(en.getValue())); c.removed = new HashSet<>(removed); return c; }
    }
    static class NaiveSet {                                   // plain set: add inserts, remove deletes, merge = union
        Set<Integer> s = new TreeSet<>(); void merge(NaiveSet o) { s.addAll(o.s); } NaiveSet copy() { NaiveSet c = new NaiveSet(); c.s = new TreeSet<>(s); return c; }
    }
    static void crdts() {
        int runs = 3000, replicas = 5, ops = 40, elems = 6;
        int counterOk = 0, orsetOk = 0, naiveOk = 0, convergedAll = 0;
        for (int run = 0; run < runs; run++) {
            Random r = new Random(run);
            PNCounter[] c = new PNCounter[replicas]; OrSet[] o = new OrSet[replicas]; NaiveSet[] nv = new NaiveSet[replicas];
            for (int i = 0; i < replicas; i++) { c[i] = new PNCounter(replicas); o[i] = new OrSet(); nv[i] = new NaiveSet(); }
            long expectedCounter = 0; Set<Long> allAdded = new HashSet<>(), allRemoved = new HashSet<>(); Map<Long, Integer> tagElem = new HashMap<>(); long tagSeq = 0;
            for (int step = 0; step < ops * 3; step++) {
                int i = r.nextInt(replicas); int kind = r.nextInt(10);
                if (kind < 3) { c[i].inc[i]++; expectedCounter++; } else if (kind < 4) { c[i].dec[i]++; expectedCounter--; }
                else if (kind < 7) { int e = r.nextInt(elems); long tag = ++tagSeq; o[i].add(e, tag); allAdded.add(tag); tagElem.put(tag, e); nv[i].s.add(e); }
                else if (kind < 8) { int e = r.nextInt(elems); Set<Long> seen = o[i].adds.get(e); if (seen != null) allRemoved.addAll(seen); o[i].remove(e); nv[i].s.remove(e); }
                else { int j = r.nextInt(replicas); if (j != i) { c[i].merge(c[j].copy()); o[i].merge(o[j].copy()); nv[i].merge(nv[j].copy()); } }        // random gossip (stale copy: arbitrary order, duplicates)
            }
            // final anti-entropy: everyone merges with everyone, in random order, twice (duplicates are harmless)
            for (int pass = 0; pass < 2; pass++) for (int a = 0; a < replicas; a++) { List<Integer> order = new ArrayList<>(); for (int b = 0; b < replicas; b++) order.add(b); Collections.shuffle(order, r);
                for (int b : order) { c[a].merge(c[b]); o[a].merge(o[b]); nv[a].merge(nv[b]); } }
            for (int pass = 0; pass < 2; pass++) for (int a = 0; a < replicas; a++) for (int b = 0; b < replicas; b++) { c[a].merge(c[b]); o[a].merge(o[b]); nv[a].merge(nv[b]); }
            boolean allEqual = true; for (int i = 1; i < replicas; i++) if (c[i].value() != c[0].value() || !o[i].value().equals(o[0].value()) || !nv[i].s.equals(nv[0].s)) allEqual = false;
            if (allEqual) convergedAll++;
            if (c[0].value() == expectedCounter) counterOk++;
            Set<Integer> truth = new TreeSet<>(); for (long t : allAdded) if (!allRemoved.contains(t)) truth.add(tagElem.get(t));       // element present iff some add was never observed-removed
            if (o[0].value().equals(truth)) orsetOk++;
            if (nv[0].s.equals(truth)) naiveOk++;
           
        }
        System.out.println("== 1) CRDTs vs naive replicated types. 5 replicas, 120 random operations each run (counter inc/dec, set add/remove), random gossip of STALE state copies (reordered, duplicated),");
        System.out.println("      then a final all-to-all exchange. " + runs + " runs. 'Correct' = equals what the operations semantically mean. ==");
        System.out.printf("PN-Counter converges to the exact sum of all increments and decrements:        %5.1f%% of runs%n", 100.0 * counterOk / runs);
        System.out.printf("OR-Set (add-wins) equals the semantic truth (adds not observed-removed):      %5.1f%% of runs%n", 100.0 * orsetOk / runs);
        System.out.printf("Naive set (remove deletes locally, merge = union) equals the truth:           %5.1f%% of runs   (removed elements resurrect)%n", 100.0 * naiveOk / runs);
        System.out.printf("All three types had all 5 replicas in identical states after anti-entropy:      %5.1f%% of runs%n", 100.0 * convergedAll / runs);
    }

    // ================================================================== Gossip (epidemic) dissemination
    static double[] gossip(int n, int fanout, String mode, double loss, double dead, int trials, long seed) {
        Random r = new Random(seed); double sumRounds99 = 0, sumRoundsAll = 0, sumMsgs = 0; int reached = 0;
        for (int t = 0; t < trials; t++) {
            boolean[] alive = new boolean[n]; for (int i = 0; i < n; i++) alive[i] = r.nextDouble() >= dead; alive[0] = true;
            boolean[] inf = new boolean[n]; inf[0] = true; int infected = 1; int aliveCount = 0; for (boolean a : alive) if (a) aliveCount++;
            int round = 0, r99 = -1; long msgs = 0;
            while (infected < aliveCount && round < 200) {
                round++; boolean[] next = inf.clone();
                for (int i = 0; i < n; i++) {
                    if (!alive[i]) continue;
                    if (mode.equals("push") || mode.equals("push-pull")) if (inf[i]) for (int f = 0; f < fanout; f++) { int j = r.nextInt(n); msgs++; if (r.nextDouble() >= loss && alive[j]) next[j] = true; }
                    if (mode.equals("pull") || mode.equals("push-pull")) if (!inf[i]) for (int f = 0; f < fanout; f++) { int j = r.nextInt(n); msgs++; if (r.nextDouble() >= loss && alive[j] && inf[j]) next[i] = true; }
                }
                inf = next; infected = 0; for (int i = 0; i < n; i++) if (inf[i] && alive[i]) infected++;
                if (r99 < 0 && infected >= 0.99 * aliveCount) r99 = round;
            }
            if (infected >= aliveCount) reached++;
            sumRounds99 += r99 < 0 ? round : r99; sumRoundsAll += round; sumMsgs += (double) msgs / aliveCount;
        }
        return new double[]{sumRounds99 / trials, sumRoundsAll / trials, sumMsgs / trials, 100.0 * reached / trials};
    }
    static void gossipExperiments() {
        System.out.println("\n== 2) Gossip dissemination of one update among N nodes (each round, each participating node contacts `fanout` random peers). 200 trials per row. ==");
        System.out.println("N        mode        fanout  loss  dead    rounds to 99%    rounds to 100%    messages per node    runs reaching everyone    log2(N)");
        int[][] ns = {{100}, {1000}, {10000}};
        for (int[] nn : ns) { int n = nn[0]; double[] x = gossip(n, 1, "push", 0, 0, 200, 1);
            System.out.printf("%-8d push        %d       0%%    0%%      %5.1f            %5.1f            %6.1f               %5.1f%%                 %5.1f%n", n, 1, x[0], x[1], x[2], x[3], Math.log(n) / Math.log(2)); }
        for (String mode : new String[]{"push", "pull", "push-pull"}) { double[] x = gossip(1000, 1, mode, 0, 0, 200, 2);
            System.out.printf("%-8d %-10s  %d       0%%    0%%      %5.1f            %5.1f            %6.1f               %5.1f%%%n", 1000, mode, 1, x[0], x[1], x[2], x[3]); }
        for (int f : new int[]{2, 3}) { double[] x = gossip(1000, f, "push", 0, 0, 200, 3);
            System.out.printf("%-8d push        %d       0%%    0%%      %5.1f            %5.1f            %6.1f               %5.1f%%%n", 1000, f, x[0], x[1], x[2], x[3]); }
        double[] y = gossip(1000, 2, "push", 0.3, 0.1, 200, 4);
        System.out.printf("%-8d push        %d      30%%   10%%      %5.1f            %5.1f            %6.1f               %5.1f%%   (30%% message loss, 10%% of nodes dead)%n", 1000, 2, y[0], y[1], y[2], y[3]);
    }

    // ================================================================== Merkle tree anti-entropy
    static void merkle() {
        int leaves = 1 << 20; long[][] levels = new long[21][];
        System.out.println("\n== 3) Merkle-tree anti-entropy. Two replicas each hold 1,048,576 keys. How many hash comparisons are needed to find the keys that differ? ==");
        System.out.println("differing keys     hash comparisons (tree walk)      naive key-by-key comparisons     leaf ranges to transfer");
        for (int d : new int[]{0, 1, 10, 1000, 100_000}) {
            Random r = new Random(d + 1); long[] a = new long[leaves], b = new long[leaves]; for (int i = 0; i < leaves; i++) { a[i] = mix(i); b[i] = a[i]; }
            Set<Integer> diff = new HashSet<>(); while (diff.size() < d) diff.add(r.nextInt(leaves)); for (int i : diff) b[i] ^= 0xDEADBEEFL;
            long[][] ta = build(a), tb = build(b); long[] counter = {0, 0}; walk(ta, tb, 0, 0, counter);
            System.out.printf("%,9d            %,12d                       %,12d                  %,9d%n", d, counter[0], leaves, counter[1]);
        }
    }
    static long[][] build(long[] leaf) { int levels = 21; long[][] t = new long[levels][]; t[levels - 1] = leaf; for (int l = levels - 2; l >= 0; l--) { t[l] = new long[1 << l]; for (int i = 0; i < t[l].length; i++) t[l][i] = mix(t[l + 1][2 * i] * 31 + t[l + 1][2 * i + 1]); } return t; }
    static void walk(long[][] a, long[][] b, int level, int idx, long[] c) { c[0]++; if (a[level][idx] == b[level][idx]) return; if (level == a.length - 1) { c[1]++; return; } walk(a, b, level + 1, 2 * idx, c); walk(a, b, level + 1, 2 * idx + 1, c); }

    public static void main(String[] args) { crdts(); gossipExperiments(); merkle(); }
}
```
**Sample output**
```text
== 1) CRDTs vs naive replicated types. 5 replicas, 120 random operations each run (counter inc/dec, set add/remove), random gossip of STALE state copies (reordered, duplicated),
      then a final all-to-all exchange. 3000 runs. 'Correct' = equals what the operations semantically mean. ==
PN-Counter converges to the exact sum of all increments and decrements:        100.0% of runs
OR-Set (add-wins) equals the semantic truth (adds not observed-removed):      100.0% of runs
Naive set (remove deletes locally, merge = union) equals the truth:            73.4% of runs   (removed elements resurrect)
All three types had all 5 replicas in identical states after anti-entropy:      100.0% of runs

== 2) Gossip dissemination of one update among N nodes (each round, each participating node contacts `fanout` random peers). 200 trials per row. ==
N        mode        fanout  loss  dead    rounds to 99%    rounds to 100%    messages per node    runs reaching everyone    log2(N)
100      push        1       0%    0%       11.4             12.3               5.5               100.0%                   6.6
1000     push        1       0%    0%       15.1             18.0               7.9               100.0%                  10.0
10000    push        1       0%    0%       18.4             23.7              10.3               100.0%                  13.3
1000     push        1       0%    0%       15.1             18.1               8.1               100.0%
1000     pull        1       0%    0%       13.0             13.7              10.0               100.0%
1000     push-pull   1       0%    0%        8.5              9.2               9.2               100.0%
1000     push        2       0%    0%        9.0             10.6               8.4               100.0%
1000     push        3       0%    0%        7.0              8.2               9.0               100.0%
1000     push        2      30%   10%       12.7             14.8              12.5               100.0%   (30% message loss, 10% of nodes dead)

== 3) Merkle-tree anti-entropy. Two replicas each hold 1,048,576 keys. How many hash comparisons are needed to find the keys that differ? ==
differing keys     hash comparisons (tree walk)      naive key-by-key comparisons     leaf ranges to transfer
        0                       1                          1,048,576                          0
        1                      41                          1,048,576                          1
       10                     339                          1,048,576                         10
    1,000                  20,239                          1,048,576                      1,000
  100,000                 740,417                          1,048,576                    100,000
```
**Experiments**
1. Add a **remove-wins set** and an **LWW-element-set** to the CRDT lab and compare semantic outcomes against add-wins for concurrent add/remove. Measure how often the three disagree.
2. Implement an **MV-register** (multi-value, with version vectors) and a **shopping-cart OR-Map** (item → PN-counter); simulate concurrent add/remove/quantity changes across 5 replicas and check convergence and intent.
3. Convert the PN-Counter to **delta-state**: replicas ship only the changed slot; compare bytes transferred per merge.
4. Add **tombstone garbage collection** to the OR-Set using a causal-stability rule (a tombstone can be dropped when every replica has acknowledged it); measure metadata size over time with and without GC.
5. In the gossip lab, add **rumor mongering with a stop probability** (a node stops spreading after hearing the rumor k times) and measure the fraction of nodes missed vs messages saved; add **topology awareness** (prefer same-rack peers 80% of the time) and measure cross-rack traffic and convergence.
6. Run **gossip-based aggregation** (each node averages its value with a random peer each round) and show convergence to the global mean; analyze the rate of convergence vs N.
7. In the Merkle lab, change the **branching factor** (2, 4, 16) and leaf size; plot comparisons vs number of differences; add **incremental updates** (update the path to the root for a single key change) and measure the cost per write.

### Lab 3: Design a unique-visitors pipeline (paper)
Design AdMetrics’ unique-visitor pipeline for 10,000 campaigns × 24 hourly buckets × 5 regions: sketch type and parameters (HLL precision for 1% error; memory per campaign-hour), where sketches are computed (edge/regional collectors), how they are merged (daily, weekly), how to answer “unique users across campaigns A or B” and “unique users in A and B” (and the accuracy of the latter), storage format, late-arriving events, privacy (hashing/salting), and how you validate accuracy against a sampled exact count.

---

## 20.14 Interview perspective

- **Bloom filter** answer template: “Check the filter first; ‘no’ is definite, so we skip the disk read; ‘yes’ we verify. 10 bits/key gives ~1% false positives, k ≈ 7; size for peak n.”
- **Unique counts:** “HyperLogLog: 12 KB gives ~1% error, mergeable across regions by max.” Know why intersections are unreliable.
- **Top-K / heavy hitters:** “Count-Min sketch + heap, or Space-Saving; error ≤ εN; good for hot keys/trending.”
- **Quantiles:** “Don’t average percentiles; merge DDSketch/t-digest/histograms.”
- **Reconciliation:** “Merkle trees: log-time location of differences; Dynamo/Cassandra anti-entropy.”
- **Conflict-free replication:** “CRDTs: commutative, associative, idempotent merges; OR-Set add-wins; counters can’t enforce bounds; tombstone GC needs causal stability.”
- **Gossip:** “Random peer exchange; rounds ∝ log N; robust to loss and failures; used for membership and anti-entropy; eventual and probabilistic.”
- Always state the **error model** (one-sided vs two-sided), **mergeability**, and **where exactness is still required**.

---

## 20.15 Common misconceptions

1. **“A Bloom filter can tell me an item is present.”** Only “probably”; false positives exist.
2. **“Bloom filters can delete.”** Not standard ones; use counting or cuckoo filters.
3. **“Bigger k is always better.”** Past k = (m/n) ln 2 the FPR rises.
4. **“Count-Min counts exactly the top items.”** It overestimates; best for heavy hitters.
5. **“HyperLogLog needs memory proportional to the number of items.”** It is constant (12 KB at 1%).
6. **“I can intersect HLLs.”** Only via inclusion–exclusion with large error.
7. **“Average the p99 from each server.”** That is statistically meaningless; merge sketches/histograms.
8. **“CRDTs solve all consistency problems.”** They give convergence, not invariants like uniqueness or non-negativity.
9. **“Convergence means correctness.”** A union-merge set converges to the wrong answer 26.6% of the time in the lab.
10. **“CRDTs are free.”** Metadata, tombstones, and GC are real costs.
11. **“Gossip is instantaneous.”** It takes O(log N) rounds, with probabilistic tails.
12. **“Merkle trees prevent divergence.”** They *detect* and localize it cheaply; repair is a separate step.

---

## 20.16 Chapter summary

- **Probabilistic structures** trade exactness for sublinear memory with quantified error and (usually) **mergeability**; use them as filters in front of exact checks or where approximation is acceptable.
- **Bloom filter:** no false negatives; FPR ≈ (1 − e^{−kn/m})^k; optimal k = 0.693·(m/n); ~10 bits/key → ~1% (measured 0.825% vs 0.819% predicted); overfilling ruins it (2n → 13.8%); used in LSM trees, cache-penetration guards, crawlers, admission filters.
- **Count-Min sketch:** never underestimates; error ≤ εN with probability 1 − δ; good for heavy hitters (top-100 error 0.09% at 1 MB; top-10 found with 20 KB); mergeable by addition.
- **HyperLogLog:** distinct counts with error 1.04/√m (12 KB ≈ 1%), constant memory, mergeable by max (merged sketch equals union sketch); intersections unreliable.
- **Other sketches:** DDSketch/t-digest for mergeable quantiles, reservoir sampling, MinHash/LSH.
- **Merkle trees:** find differing ranges with O(d·log n) hash comparisons (41 for 1 difference in 1M keys).
- **Version vectors / dotted VVs / HLC** detect concurrency; LWW silently loses data under skew.
- **CRDTs** (G/PN-counters, OR-Set, registers, maps, sequences) merge commutatively, associatively, idempotently → strong eventual consistency without coordination: exact outcomes in 100% of 3,000 chaotic runs, while a naive union set converged to wrong answers in 26.6%; they cannot enforce uniqueness or bounds; metadata/tombstone GC needs causal stability.
- **Gossip:** O(log N) rounds, robust to loss/failures (30% loss + 10% dead nodes: all live nodes informed in ~15 rounds); push-pull halves time; basis of membership and anti-entropy in Cassandra, Consul, Redis Cluster.

### Mental models
| Concept | Mental model |
|---|---|
| Bloom filter | A bouncer with a list of fingerprints: can say “definitely not on the list” or “probably” |
| Count-Min sketch | Several noisy tally boards; the smallest tally is the least polluted |
| HyperLogLog | Guessing a crowd’s size from the longest streak of coin-flip tails anyone saw |
| Reservoir sampling | A stadium raffle where every ticket has an equal chance, however long the line |
| Merkle tree | A tamper-proof table of contents: if the summaries agree, nothing below differs |
| Version vector | Each office keeps a count of how many memos it has seen from every other office |
| CRDT | Notes you can merge in any order, any number of times, and always get the same final notebook |
| OR-Set | Each added item has a unique sticker; removing only peels stickers you have seen |
| Gossip | A rumor spreading through an office, each person telling a couple of random colleagues |

---

## 20.17 Exercises

### A. Conceptual
1. Explain the guarantees of a Bloom filter. Why can’t it have false negatives, and why can’t it delete?
2. Derive (or justify) the false-positive formula and the optimal k. What happens if k is too small or too large?
3. Describe three real systems that use Bloom filters and what they gain.
4. Compare standard, counting, blocked, and cuckoo filters.
5. Explain how a Count-Min sketch works and why it never underestimates. What do ε and δ control?
6. Why is Count-Min good for heavy hitters and bad for rare items?
7. Explain HyperLogLog’s intuition (leading zeros), the role of buckets, and the error formula.
8. Why are HLL sketches mergeable and why is intersection hard?
9. Why can’t percentiles be averaged across servers? How do mergeable quantile sketches solve it?
10. Describe reservoir sampling and prove that each item has probability k/n at the end (sketch).
11. How does a Merkle tree locate differing keys quickly? What does it cost to maintain?
12. Compare Lamport clocks, version vectors, dotted version vectors, and HLC.
13. Define a CRDT, strong eventual consistency, and the algebraic requirements of state-based merge.
14. Describe G-Counter, PN-Counter, G-Set, 2P-Set, OR-Set, LWW-Register, MV-Register, and the sequence CRDT.
15. Explain add-wins semantics in an OR-Set with a concurrent add/remove example.
16. Why can’t CRDT counters enforce `balance ≥ 0`? What alternative exists?
17. Why does a union-merge set converge yet give wrong results? Relate to the lab number.
18. What is causal stability and why is it needed for tombstone GC?
19. Compare push, pull, and push-pull gossip and the shape of their convergence.
20. Give three uses of gossip and three weaknesses.
21. Compare CRDTs and operational transformation for collaborative editing.
22. For each: unique visitors, “seen URL”, top hashtags, fleet-wide p99, replica diff, offline-capable cart: name the structure and the error type.

### B. Calculations
23. Size a Bloom filter for 200 M keys at FPR 0.1%: bits per key, total memory, k. Compare with a 20-byte-per-key hash set.
24. A Bloom filter was sized for 50 M keys at 1% FPR (9.6 bits/key, k = 7). It now holds 75 M keys. What is the new FPR?
25. Count-Min: ε = 0.0005, δ = 0.001. Compute w, d, memory (4-byte counters). For N = 2 B events, what is the maximum overcount bound?
26. HyperLogLog: target error 0.5%; compute the required m, p, and memory (6 bits/register). For error 2%?
27. HLL union and inclusion–exclusion: |A| = 10 M, |B| = 10 M, |A ∪ B| = 19.9 M estimated with 1% error each. Compute the estimated intersection and its error range. Is it useful if the true intersection is 100,000?
28. Merkle tree: 1 B keys, leaf ranges of 1,000 keys, binary tree. Depth? Hash comparisons to locate 1 differing leaf? 10,000 differing leaves (approximate)? Compare with comparing all leaves.
29. Gossip: N = 5,000 nodes, push with fanout 2. Estimate rounds with log_{3} N + ln N / 2. If the period is 1 s, how long until 99% know? Per-node bandwidth for 400-byte messages.
30. Gossip with 30% loss: effective fanout? Estimate the extra rounds relative to loss-free using the effective fanout.
31. CRDT size: OR-Set with 1 M add-tags (16 bytes each) and 900 k tombstones; memory? After GC removes tombstones and the adds they cancel, what remains?
32. Reservoir: stream of 10 M events, reservoir k = 1,000: probability any given event is in the sample; probability that the first event survives to the end.

### C. Design
33. Design a “have we crawled this URL” service for 100 B URLs across 50 machines: Bloom filter sizing, sharding, rebuilds, verification path, and false-positive handling.
34. Design a trending-hashtags system with Count-Min, time decay, and regional merging.
35. Design a shopping-cart service as a CRDT (OR-Map of item → quantity): operations, merge, semantics for concurrent add/remove/change, checkout coordination.
36. Design a collaborative notes app with offline editing: choose CRDT type, sync protocol, GC, and what coordination you need.
37. Design a cluster membership and metadata dissemination layer for 2,000 nodes using gossip: interval, fanout, message content, failure detection, security.
38. Design replica repair for a Dynamo-style store: Merkle trees per range, scheduling, throttling, and what to do when trees are huge.

### D. Debugging
39. Your Bloom filter’s measured false positive rate is 4% but expected 1%. List four causes.
40. Two HLLs of the same data merged give a different estimate than the HLL built directly. What setup mistakes cause that?
41. A Count-Min based hot-key detector reports a hot key that is not hot. Explain and fix.
42. Users report deleted items reappearing in a replicated list. Which merge semantics bug is likely and how do you fix it?
43. CRDT state grew from 20 MB to 4 GB over months. Diagnose and propose fixes.
44. The gossip cluster takes 45 seconds to converge after a change although it was tuned for 10. List causes.

### E. Interview-style
45. “Design a system to count unique visitors per page per day for the whole internet.”
46. “How would you detect and repair divergence between replicas?”
47. “What is a CRDT and where would you use one?”

### F. Teach-back
48. Explain a Bloom filter to a friend using a nightclub guest list that’s been smudged.

---

## 20.18 Full answer key

**1.** Adding an item sets its k bits and they are never cleared, so all of an added item’s bits are 1: no false negatives. Clearing a bit to delete one item could erase bits belonging to other items, creating false negatives, so standard filters cannot delete.

**2.** A given bit is 0 with probability ≈ e^{−kn/m}; a false positive requires all k bits to be 1: (1 − e^{−kn/m})^k. Minimizing over k gives k = (m/n) ln 2, where half the bits are 1. Too small k: too few checks (more false positives); too large k: the array fills with 1s (more false positives).

**3.** LSM-trees (skip SSTables that cannot contain a key: 54,000 → 527 disk reads in the MiniLsm lab), cache-penetration guards (reject nonexistent keys before DB), crawlers/dedup (skip seen URLs), CDN admission (cache on second request), database bloom joins.

**4.** Standard: bits, no delete. Counting: small counters (delete supported, 3–4× memory). Blocked: all k bits in one cache line (fast, slightly higher FPR). Cuckoo: stores fingerprints, supports delete, more space-efficient at low FPR; insertions can fail near capacity.

**5.** d rows of w counters; update increments one counter per row; estimate = min across rows. Every counter ≥ the true count (collisions only add), so the min ≥ the true count and is the tightest bound. ε controls error relative to N (w = e/ε); δ controls failure probability (d = ln 1/δ).

**6.** Collision noise has roughly constant magnitude (~N/w), which is small relative to heavy items’ counts but swamps rare items.

**7.** The maximum number of leading zeros among n random hashes ≈ log₂ n; buckets (m = 2^p) reduce variance: each register holds a max rank, combined by a harmonic mean; relative standard error 1.04/√m.

**8.** Register-wise max gives exactly the sketch of the union. There is no operation that gives the sketch of an intersection; inclusion–exclusion subtracts large estimates with their errors, so small intersections are swamped by noise.

**9.** A percentile of a union is not a function of the percentiles of the parts. Sketches (DDSketch/t-digest/histograms) keep the distribution’s shape and merge by combining buckets/centroids.

**10.** Keep the first k items; the i-th item (i > k) is kept with probability k/i, replacing a random slot. By induction, after n items every item is in the reservoir with probability k/n. (An item at position j survives with probability (k/j) × Π_{i=j+1..n}(1 − 1/i) = k/n.)

**11.** Compare roots; if equal, done; else compare children and descend only into differing subtrees. Cost O(d log n) comparisons for d differences. Maintenance: updating a leaf requires recomputing hashes up to the root; building from scratch requires a scan.

**12.** Lamport: scalar causal order, no concurrency detection. Version vector: per-replica counters; detect dominance/concurrency per object. Dotted VV: add the event’s dot to avoid false concurrency/lost updates. HLC: physical + logical timestamp; causal and near real-time; doesn’t detect concurrency exactly.

**13.** A CRDT is a replicated data type whose replicas can be updated independently and converge when they have seen the same updates. Strong eventual consistency = same updates ⇒ same state. State-based merge must be commutative, associative, idempotent.

**14.** G-Counter: per-replica counters, max-merge. PN-Counter: P and N G-Counters. G-Set: add-only set. 2P-Set: add and remove sets (no re-add). OR-Set: add-tags and tombstones; add-wins. LWW-Register: latest timestamp wins. MV-Register: keeps concurrent values. Sequence CRDT: ordered ids for collaborative lists/text.

**15.** Replica A adds item x with tag t2 while replica B (having only seen tag t1) removes x (tombstoning t1). After merge, t2 isn’t tombstoned, so x is present: the concurrent add wins.

**16.** Bounded invariants are not I-confluent: two replicas can each decrement locally within the bound and together exceed it. Alternative: escrow (pre-allocate per-replica budgets) or a coordinated step for the risky operation.

**17.** Union with no tombstones has an idempotent, commutative merge so all replicas agree, but removals are lost whenever another replica still has the element (resurrection); lab: wrong in 26.6% of runs (73.4% correct).

**18.** A tombstone can be dropped only when every replica has seen the removal (so none will re-introduce the tagged add); that “all replicas have seen it” condition is causal stability, requiring knowledge of membership and acknowledgements.

**19.** Push is fast at first and slow at the tail; pull is slow at first and fast at the tail (probability to stay uninformed squares); push-pull combines (lab: 9.2 rounds vs 18.1 push, 13.7 pull).

**20.** Uses: membership/failure detection, anti-entropy/state reconciliation, dissemination of config/aggregates. Weaknesses: probabilistic/eventual delays, redundant traffic, security (malicious nodes), no strong ordering/consensus.

**21.** OT transforms concurrent operations (needs a server/ordering protocol), smaller metadata; CRDTs encode position/order in identifiers, enabling peer-to-peer merges and offline edits at the cost of more metadata/tombstones.

**22.** Unique visitors: HLL (relative error). Seen URL: Bloom (false positives). Top hashtags: Count-Min + heap/Space-Saving (overestimates). Fleet p99: DDSketch/t-digest (relative error, mergeable). Replica diff: Merkle tree (hash collisions negligible). Offline cart: CRDT (OR-Map/PN-counter; semantics chosen).

**23.** m/n = −ln 0.001/(ln 2)² = 6.908/0.4805 = **14.38 bits/key**; m = 200 M × 14.38 = 2.876 Gbit = **360 MB**; k = 14.38 × 0.693 ≈ **10**. A hash set at 20 bytes/key = 4 GB: the filter is ~11× smaller.

**24.** Load is 75/50 = 1.5× the design, so bits/key = 9.6/1.5 = 6.4. p = (1 − e^{−7/6.4})^7 = (1 − e^{−1.094})^7 = (1 − 0.335)^7 = 0.665^7 ≈ **5.8%** (vs 1% designed, almost 6× worse).

**25.** w = e/0.0005 = 5,437; d = ln(1000) = 6.9 → 7. Counters = 5,437 × 7 = 38,059 × 4 B = **152 KB**. Bound: εN = 0.0005 × 2×10⁹ = **1,000,000** (with probability 99.9%): fine for items with counts in the millions, meaningless for rare items.

**26.** m ≥ (1.04/0.005)² = 208² = 43,264 → p = 16 (65,536 registers); memory = 65,536 × 6 bits = 49,152 B = **48 KB**. For 2%: m ≥ 2,704 → p = 12 (4,096 registers) = **3 KB**.

**27.** |A ∩ B| ≈ 10 + 10 − 19.9 = **0.1 M (100,000)**. But each estimate has ±1% error: ±100,000 on A, ±100,000 on B, ±199,000 on the union; combined standard error ≈ √(0.1² + 0.1² + 0.199²) M ≈ **0.24 M**. The estimate of 100,000 is within noise of 0 or 300,000: **useless** for an intersection of 0.5% of the sets.

**28.** 1 B/1,000 = 1 M leaves ≈ 2²⁰ → depth 20. One differing leaf: ≈ 2 × 20 + 1 = **41** comparisons. 10,000 differing leaves: top ~13 levels nearly all differ (~2^14 nodes) plus ~7 private levels each: ≈ 16,000 + 10,000 × 14 (two children per level) ≈ **150–200 k** comparisons vs **1 M** leaves naive (5–7× fewer).

**29.** Rounds ≈ log₃ 5,000 + ln 5,000/2 = 7.75 + 4.26 ≈ **12 rounds** (to reach everyone; 99% a bit sooner, ~10) → ~10–12 s at a 1 s period. Bandwidth per node: each informed node sends 2 × 400 B = 800 B/s while the rumor is active; steady state (anti-entropy digests) similar per period.

**30.** Effective fanout = 2 × 0.7 = 1.4. Rounds ≈ log_{2.4} 1,000 + ln 1,000/1.4 = 7.9 + 4.9 ≈ 12.8 vs loss-free (f=2): log₃ 1,000 + ln 1,000/2 = 6.3 + 3.45 ≈ 9.8: about **3 extra rounds** (lab: 10.6 → 14.8 with loss and dead nodes).

**31.** Adds: 1 M × 16 B = 16 MB; tombstones: 0.9 M × 16 B = 14.4 MB; total ≈ **30 MB** (plus element ids). After GC drops tombstones and their cancelled adds: only the 100 k live adds remain ≈ **1.6 MB** (~19× smaller).

**32.** Each event is in the final sample with probability k/n = 1,000/10,000,000 = **0.01%**. The first event survives to the end with the same probability, 0.01% (uniform).

**33 (sketch).** At 1% FPR a filter needs ≈ 9.6 bits per URL: 100 B × 9.6 bits = 960 Gbit ≈ 120 GB in total, about 2.4 GB per machine across 50 machines (shard by hash(URL)). A positive means “probably crawled” → skip it, accepting that ~1% of genuinely new URLs are skipped (verify positives against the URL store only for high-value domains); a negative is definite → crawl. Size for the maximum URL count, monitor the fill ratio, rebuild periodically from the crawl database or add a new scalable layer, and never use the filter as the system of record.

**34 (sketch).** Per-region Count-Min sketches per time bucket (e.g., 1-minute) with exponential decay or sliding windows, a candidate heap per region, merge by addition at the center; hashtags normalized; blocklists; handle bursts via conservative update; periodically recompute from raw logs for validation.

**35 (sketch).** OR-Map item → PN-counter plus OR-Set semantics for presence; add increments counter; remove tombstones observed tags; concurrent add/remove → add-wins (item stays with its latest quantity merges via PN-counter); checkout is a coordinated step (read merged state, validate stock, convert into an order through a linearizable service).

**36 (sketch).** Sequence CRDT (RGA/Yjs/Automerge) per note; sync by exchanging updates (delta) with version vectors; storage compaction of tombstones after causal stability or via snapshots; coordination only for sharing/permissions; server acts as a relay and snapshotter.

**37 (sketch).** SWIM-style gossip with 1 s interval, fanout 3, indirect probes k=3, suspicion state, piggybacked membership updates and versioned metadata with digests; authenticate messages (mutual TLS or signatures); size limit per message; anti-entropy every 30 s with Merkle/digest of metadata; gradual rollout of config changes.

**38 (sketch).** Per token range Merkle trees (depth ~15–18) built during validation compactions; schedule repairs per range off-peak with throttling; compare trees with replicas, stream differing ranges; use incremental repair to avoid rebuilding; subdivide ranges when trees are big; monitor repair backlog.

**39.** Overfilled (more keys than designed); poor or correlated hash functions; wrong k; too small m (bit-array sizing bug/truncation); duplicates counted separately; using a filter after many re-adds without reset; test keys not independent.

**40.** Different precisions p, different hash functions/seeds between sketches, merging with sparse vs dense representations incorrectly, double-counting duplicate adds is fine (idempotent) so check seeds and register semantics, or bugs in max-merge (e.g., sum).

**41.** Count-Min overestimates due to collisions with heavy keys in all rows; a cold key colliding with hot keys gets a high estimate. Fix: larger width/depth, conservative update, verify candidates with an exact counter for flagged keys.

**42.** Naive union merge with deletes lost (no tombstones) → resurrection. Fix with OR-Set/tombstones with observed-remove semantics, ensure deletes propagate, and GC after causal stability.

**43.** Unbounded metadata: tombstones, version vector growth (many replicas/clients), per-operation tags. Fix: causal-stability GC, snapshot + truncate, bounded replica identities (don’t use client IDs as replicas), delta-state compaction.

**44.** Longer gossip interval or lost updates; low fanout; large messages causing delays; high loss or partitions; slow nodes/GC; topology-aware peer selection too restrictive; processing queues backing up; security validation costs.

**45 (sketch).** Ingest events at edge collectors; per page-day HLL (p = 12–14), sharded by page; merge per day; storage key `(page, day)`; global dashboards from merged sketches; exact counts only for top pages if needed; hashing and salting for privacy; error ±1%.

**46 (sketch).** Merkle trees per key range for background anti-entropy; read repair for hot data; hinted handoff for recent failures; version vectors/timestamps for conflict handling; throttled streaming of differing ranges; monitoring of repair lag.

**47 (sketch).** A CRDT is a data type whose replicas converge without coordination because merge is commutative, associative, and idempotent. Use for collaborative editing, offline-first apps, shopping carts, counters (likes), presence sets, multi-region active-active data where merges are acceptable.

**48.** A bouncer with a guest list whose ink has smudged: he can say “you’re definitely not on the list” whenever any letter of your name is missing, but if all letters appear (perhaps from other names) he says “probably,” and then checks your ID (the exact lookup).

---

## 20.19 Flashcards

| Front | Back |
|---|---|
| Bloom filter | No false negatives; FPR ≈ (1 − e^{−kn/m})^k |
| Optimal k / size | k = 0.693·m/n; ≈ 1.44·log₂(1/p) bits/key (10 bits → 1%) |
| Overfill | 2× load at 10 bits/key → ~13.8% FPR |
| Count-Min | min over d rows; overestimates; error ≤ εN w.p. 1−δ |
| CM sizes | w = e/ε, d = ln(1/δ) |
| HyperLogLog | Max leading zeros per bucket; error 1.04/√m; 12 KB ≈ 1%; merge = max |
| HLL intersection | Inclusion–exclusion; unreliable |
| Quantile sketches | DDSketch/t-digest: mergeable; never average p99s |
| Reservoir sampling | Keep k; replace with prob k/i |
| Merkle tree | Compare hashes top-down; 41 comparisons for 1 diff in 1M keys |
| Version vector | Per-replica counters; detects concurrency |
| CRDT | Commutative, associative, idempotent merge ⇒ SEC |
| OR-Set | Unique add-tags + observed-remove; add-wins |
| Naive union set | Converges but resurrects deletes (26.6% wrong in lab) |
| CRDT limits | No uniqueness or bounded invariants without coordination |
| Tombstone GC | Needs causal stability |
| Gossip rounds | ≈ log₂N + ln N (push, f=1); push-pull ≈ half |
| Gossip robustness | 30% loss + 10% dead: all live nodes informed (~15 rounds) |
| LWW | Cheap, loses concurrent writes; clock-sensitive |
| Rule | Exact for money/identity; approximate for analytics/filters |

## 20.20 Glossary additions

| Term | Simple Meaning | Technical Meaning | Why It Exists | Example |
|---|---|---|---|---|
| Probabilistic data structure | Small, approximate | Sublinear-space structure with bounded error | Scale | Bloom filter |
| Bloom filter | “Probably in set” bit array | k-hash bit set, no false negatives | Cheap negative filter | LSM SSTables |
| False-positive rate | Wrong “yes” fraction | P(query for absent item says present) | Tuning | 1% |
| Counting / cuckoo filter | Deletable filters | Counters / fingerprint hashing | Deletion | Cuckoo |
| Count-Min sketch | Approx frequency table | d×w counters, min estimate | Heavy hitters | Trending |
| Heavy hitters | Most frequent items | Items above a frequency threshold | Hot keys | Top-K |
| HyperLogLog | Approx distinct count | Max-rank registers, harmonic mean | Unique counts | Redis PFCOUNT |
| Mergeable sketch | Combinable summary | Operation (max/add/OR) merges sketches | Distributed aggregation | HLL max |
| DDSketch / t-digest | Quantile summary | Relative-error mergeable quantiles | Fleet percentiles | p99 |
| Reservoir sampling | Uniform sample | Streaming uniform sampling algorithm | Sampling | Trace sampling |
| MinHash / LSH | Similarity sketch | Jaccard estimate / near-neighbor hashing | Dedup | Near-duplicates |
| Merkle tree | Hash tree | Tree of hashes summarizing data | Reconciliation | Cassandra repair |
| Anti-entropy | Background sync | Periodic replica comparison/repair | Convergence | Dynamo |
| Version vector | Per-replica counters | Causal history summary for objects | Conflict detection | Riak |
| Dotted version vector | Precise VV | VV plus a dot for the latest event | Avoid false concurrency | Riak |
| CRDT | Auto-merging type | Convergent replicated data type | Coordination-free replication | G-Counter |
| CvRDT / CmRDT | State- / operation-based | Merge by join / commuting ops | Two designs | Delta CRDT |
| OR-Set | Observed-remove set | Tagged adds with tombstones | Add-wins semantics | Cart |
| MV-Register | Keeps conflicts | Set of concurrent values | Preserve updates | Siblings |
| Causal stability | Everyone has seen it | Operation delivered to all replicas | Safe GC | Tombstone GC |
| Gossip / epidemic protocol | Rumor spreading | Random peer exchange dissemination | Scalable dissemination | Serf |
| Fanout | Peers per round | Number of gossip targets | Speed vs traffic | 3 |
| SWIM | Gossip membership | Indirect probing + dissemination | Failure detection | memberlist |

---

**Batch 5 complete.** Part III is now finished (Ch 14–20).

**Next: Batch 6, Part IV (Ch 21–23).** Ch 21 Observability (metrics, logs, traces, SLO dashboards, alerting, cardinality), Ch 22 Security for System Designers (authN/Z, OAuth, mTLS, secrets, encryption, rate limiting, abuse, threat modeling), Ch 23 Cloud, Containers, and Infrastructure (Kubernetes, IaC, networking, cost). Then Part V: the design method and problem books.

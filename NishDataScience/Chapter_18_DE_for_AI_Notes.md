# Chapter 18: Data Engineering for AI — Feature Stores, LLM Pipelines, RAG

> **How to read the status tags**
> ✅ **RAN** = I executed this code in a sandbox (Python 3.12, pandas, numpy, pydantic, duckdb) and the output shown is real.
> ⚠️ **NOT RUN** = needs Spark / Redis / MLflow / ChromaDB / a model download, which the sandbox can't provide. Code is written carefully but **run it yourself and trust your output over mine**.

---

## Part A — Concept + Code

### A1. Feature Engineering Pipeline in PySpark

**What / why:** A *feature* is a model input computed from raw data (e.g. "spend in last 7 days"). The DE job is to compute features **identically** for training (batch, historical) and serving (online, low latency). Mismatch = *training-serving skew*.

**[BASIC] Ten features from raw transactions** ⚠️ NOT RUN

```python
from pyspark.sql import SparkSession, functions as F, Window

spark = SparkSession.builder.appName("features").getOrCreate()

txns = spark.createDataFrame(
    [(1, "2024-03-01 10:00:00", 50.0,  "grocery", "IN"),
     (1, "2024-03-05 11:30:00", 70.0,  "fuel",    "IN"),
     (1, "2024-03-12 02:10:00", 900.0, "luxury",  "AE"),
     (2, "2024-03-02 09:00:00", 20.0,  "grocery", "US"),
     (2, "2024-03-14 19:45:00", 25.0,  "grocery", "US")],
    ["user_id", "txn_ts", "amount", "category", "country"],
).withColumn("txn_ts", F.to_timestamp("txn_ts"))

def build_features(txns, as_of_ts: str):
    """All features use ONLY rows strictly before as_of_ts (point-in-time safe)."""
    as_of = F.to_timestamp(F.lit(as_of_ts))
    hist = txns.filter(F.col("txn_ts") < as_of)
    return (hist.groupBy("user_id").agg(
        # Recency
        F.datediff(as_of, F.max("txn_ts")).alias("days_since_last_txn"),
        # Frequency
        F.count("*").alias("txn_count_all"),
        F.sum(F.when(F.col("txn_ts") >= as_of - F.expr("INTERVAL 7 DAYS"), 1).otherwise(0)).alias("txn_count_7d"),
        # Monetary
        F.sum("amount").alias("total_spend"),
        F.avg("amount").alias("avg_amount"),
        F.max("amount").alias("max_amount"),
        F.stddev_pop("amount").alias("std_amount"),
        # Velocity (spend in last 7d vs lifetime average per week)
        F.sum(F.when(F.col("txn_ts") >= as_of - F.expr("INTERVAL 7 DAYS"), F.col("amount")).otherwise(0)).alias("spend_7d"),
        # Diversity
        F.countDistinct("country").alias("distinct_countries"),
        # Behavioural: share of night-time transactions (0-5am)
        F.avg(F.when(F.hour("txn_ts") < 5, 1).otherwise(0)).alias("night_txn_ratio"),
    ))

features = build_features(txns, "2024-03-15 00:00:00")
features.show()
```

**Why this design:** `as_of_ts` is an explicit parameter. Training calls it once per label timestamp; serving calls it with "now". Same code → no skew.

### A2. Point-in-Time Correctness (Data Leakage)

**[INTERMEDIATE]** ✅ **RAN** (pandas version of the logic; the Spark version is the same join + `txn_ts < label_ts` filter)

```python
import pandas as pd

labels = pd.DataFrame({
    "user_id": [1, 1, 2, 3],
    "label_ts": pd.to_datetime(["2024-03-10", "2024-03-20", "2024-03-15", "2024-03-18"]),
    "is_fraud": [0, 1, 0, 1],
})
txns = pd.DataFrame({
    "user_id": [1, 1, 1, 1, 2, 2, 3, 3, 3],
    "txn_ts": pd.to_datetime(["2024-03-01", "2024-03-05", "2024-03-12", "2024-03-19",
                              "2024-03-02", "2024-03-14", "2024-03-01", "2024-03-17", "2024-03-18"]),
    "amount": [50.0, 70.0, 900.0, 40.0, 20.0, 25.0, 10.0, 15.0, 5000.0],
})

# WRONG: aggregate over ALL history -> leaks the future into each label row
wrong = txns.groupby("user_id")["amount"].agg(total_spend="sum", txn_count="count").reset_index()
wrong = labels.merge(wrong, on="user_id")

# RIGHT: only transactions strictly BEFORE label_ts
m = labels.merge(txns, on="user_id")
m = m[m["txn_ts"] < m["label_ts"]]
right = (m.groupby(["user_id", "label_ts"])["amount"]
           .agg(total_spend="sum", txn_count="count").reset_index())
right = labels.merge(right, on=["user_id", "label_ts"], how="left").fillna({"total_spend": 0, "txn_count": 0})
```

**Output (real):**

```
WRONG (leaky):
 user_id   label_ts  is_fraud  total_spend  txn_count
       1 2024-03-10         0       1060.0          4
       1 2024-03-20         1       1060.0          4
       2 2024-03-15         0         45.0          2
       3 2024-03-18         1       5025.0          3

RIGHT (point-in-time):
 user_id   label_ts  is_fraud  total_spend  txn_count
       1 2024-03-10         0        120.0          2
       1 2024-03-20         1       1060.0          4
       2 2024-03-15         0         45.0          2
       3 2024-03-18         1         25.0          2
```

> **KEY INSIGHT:** Look at user 3. The leaky version says total_spend = **5025** because it includes the ₹5000 transaction that *is the fraud itself* (on 03-18). The model "learns" that big spend = fraud with near-perfect accuracy offline, then collapses in production. The right version uses `<` (strictly before) and gives 25.
> Also look at user 1, first label: leaky = 1060 vs right = 120.

> **COMMON MISTAKE:** Using `<=` on the label timestamp, or using the *ingestion* time instead of the *event* time. Also: joining a slowly changing dimension at its *current* value instead of its value *as of* the label time.

> **In Real Production:** Feature stores (Feast, Tecton, Databricks Feature Store) implement this as an **as-of join** (`merge_asof` / `AS OF` join). You pass an "entity DataFrame" of `(entity_id, event_timestamp)` and the store returns features as they were at that time.

### A3. Serving: Delta (batch/offline) + Redis (online)

**[INTERMEDIATE]** ⚠️ NOT RUN

```python
# --- Offline store: Delta table, keyed by (user_id, feature_ts) ---
(features.withColumn("feature_ts", F.current_timestamp())
         .write.format("delta").mode("append")
         .partitionBy()              # small table; partition by date if large
         .saveAsTable("ml.features.user_txn_features"))

# --- Online store: Redis hash per user, written from the latest snapshot ---
import json, redis
r = redis.Redis(host="localhost", port=6379, decode_responses=True)

def push_online(df, ttl_seconds: int = 86400) -> int:
    n = 0
    def _write(rows):
        pool = redis.Redis(host="localhost", port=6379, decode_responses=True)
        pipe = pool.pipeline(transaction=False)
        for row in rows:
            d = {k: ("" if v is None else v) for k, v in row.asDict().items()}
            pipe.hset(f"feat:user:{d['user_id']}", mapping={k: str(v) for k, v in d.items()})
            pipe.expire(f"feat:user:{d['user_id']}", ttl_seconds)
        pipe.execute()
    df.foreachPartition(_write)      # one connection per partition, not per row

def get_online(user_id: int) -> dict:
    return r.hgetall(f"feat:user:{user_id}")   # O(1), ~1ms
```

| | Offline (Delta) | Online (Redis) |
|---|---|---|
| Use | Training, backfills, batch scoring | Real-time inference |
| Latency | seconds–minutes | ~1 ms |
| Data | Full history, point-in-time | Latest value only |
| Cost | Cheap (object storage) | Expensive (RAM) |

> **INTERVIEWER TIP:** Say "one feature definition, two materializations." That sentence signals you understand skew.

### A4. MLflow Basics for DE

**[INTERMEDIATE]** ⚠️ NOT RUN

```python
import mlflow, mlflow.sklearn
from mlflow.tracking import MlflowClient
from sklearn.ensemble import RandomForestClassifier
from sklearn.metrics import roc_auc_score, average_precision_score
from sklearn.model_selection import train_test_split
import pandas as pd, tempfile, os

mlflow.set_tracking_uri("sqlite:///mlflow.db")      # local; use a server in prod
mlflow.set_experiment("fraud-model")

def train(X: pd.DataFrame, y: pd.Series, n_estimators: int = 200, max_depth: int = 8) -> str:
    X_tr, X_te, y_tr, y_te = train_test_split(X, y, test_size=0.2, stratify=y, random_state=42)
    with mlflow.start_run(run_name=f"rf_{n_estimators}_{max_depth}") as run:
        mlflow.log_params({"n_estimators": n_estimators, "max_depth": max_depth,
                           "feature_table_version": 7})            # <- log DATA version too
        model = RandomForestClassifier(n_estimators=n_estimators, max_depth=max_depth,
                                       class_weight="balanced", random_state=42).fit(X_tr, y_tr)
        p = model.predict_proba(X_te)[:, 1]
        mlflow.log_metrics({"auc": roc_auc_score(y_te, p), "pr_auc": average_precision_score(y_te, p)})
        with tempfile.TemporaryDirectory() as d:                   # artifact: feature list
            path = os.path.join(d, "features.txt"); open(path, "w").write("\n".join(X.columns))
            mlflow.log_artifact(path)
        mlflow.sklearn.log_model(model, "model", registered_model_name="fraud_rf")
        return run.info.run_id

# --- Registry transitions (aliases replace the old Staging/Production stages in MLflow 2.9+) ---
client = MlflowClient()
def promote(name: str, version: str, alias: str = "champion"):
    client.set_registered_model_alias(name, alias, version)
# load by alias in serving:  mlflow.sklearn.load_model("models:/fraud_rf@champion")
```

> **COMMON MISTAKE:** Logging the model but not the *feature table version / code commit*. Six months later nobody can reproduce it.
> **Note:** Classic "stage transitions" (`Staging → Production`) are deprecated in recent MLflow in favour of **aliases** and tags. Mention both in an interview; say you'd use aliases on new setups.

### A5. RAG Pipeline

**What / why:** An LLM doesn't know your private data. RAG retrieves relevant chunks at question time and injects them into the prompt. The DE work is everything *before* the LLM call: ingestion, cleaning, chunking, embedding, indexing, refresh.

```
 Docs ─► load ─► clean ─► chunk ─► embed ─► vector store
                                                │
 Question ─► embed ─► top-k similarity search ◄─┘
                         │
                         ▼
          prompt = instructions + chunks + question ─► LLM ─► answer (+ citations)
```

#### A5.1 Document loading ⚠️ NOT RUN

```python
from pathlib import Path
from pypdf import PdfReader

def load_documents(folder: str) -> dict[str, str]:
    docs = {}
    for p in Path(folder).rglob("*"):
        try:
            if p.suffix.lower() == ".pdf":
                docs[str(p)] = "\n".join((pg.extract_text() or "") for pg in PdfReader(p).pages)
            elif p.suffix.lower() in {".txt", ".md"}:
                docs[str(p)] = p.read_text(encoding="utf-8", errors="ignore")
        except Exception as e:                 # one bad file must not kill the batch
            print(f"skip {p}: {e}")
    return docs
```

#### A5.2 Three chunking strategies, embeddings, vector store, eval — ✅ **RAN**

I used a **hashed bag-of-words embedder** so this runs with only numpy. It is a *stand-in*: swap `embed()` for sentence-transformers (shown after the output). The retrieval and evaluation logic is identical either way.

```python
import re, math, hashlib
from collections import Counter
import numpy as np

# ---- Fixed-size chunking with overlap
def fixed_chunks(text: str, size: int = 120, overlap: int = 20) -> list[str]:
    if overlap >= size: raise ValueError("overlap must be < size")
    step, out = size - overlap, []
    for i in range(0, len(text), step):
        out.append(text[i:i + size])
        if i + size >= len(text): break
    return out

# ---- Sentence-based chunking (pack whole sentences up to max_chars)
def sentence_chunks(text: str, max_chars: int = 150) -> list[str]:
    sents = re.split(r"(?<=[.!?])\s+", text.strip())
    out, cur = [], ""
    for s in sents:
        if cur and len(cur) + 1 + len(s) > max_chars:
            out.append(cur); cur = s
        else:
            cur = f"{cur} {s}".strip()
    if cur: out.append(cur)
    return out

# ---- Recursive chunking: try big separators first, fall back to smaller ones
def recursive_chunks(text: str, max_chars: int = 100, seps=("\n\n", "\n", ". ", " ")) -> list[str]:
    if len(text) <= max_chars: return [text.strip()]
    for sep in seps:
        parts = text.split(sep)
        if len(parts) > 1:
            out, cur = [], ""
            for p in parts:
                piece = p if not cur else cur + sep + p
                if len(piece) <= max_chars: cur = piece
                else:
                    if cur: out.append(cur)
                    if len(p) > max_chars:
                        out.extend(recursive_chunks(p, max_chars, seps[seps.index(sep)+1:] or (" ",)))
                    cur = "" if len(p) > max_chars else p
            if cur: out.append(cur)
            return [c.strip() for c in out if c.strip()]
    return [text[i:i+max_chars] for i in range(0, len(text), max_chars)]

# ---- Toy embedder (stand-in)
DIM = 256
STOP = {"the","a","an","to","is","are","you","can","for","of","in","on","your","be","or","as","and","above","within"}
def tokens(t): return [w for w in re.findall(r"[a-z0-9]+", t.lower()) if w not in STOP]
def embed(text: str) -> np.ndarray:
    v = np.zeros(DIM)
    for w, c in Counter(tokens(text)).items():
        h = int(hashlib.md5(w.encode()).hexdigest(), 16)
        v[h % DIM] += (1 + math.log(c)) * (1 if (h >> 8) % 2 == 0 else -1)   # signed hashing
    n = np.linalg.norm(v)
    return v / n if n else v

class VectorStore:
    def __init__(self): self.ids, self.texts, self.mat = [], [], np.zeros((0, DIM))
    def add(self, ids, texts):
        self.ids += ids; self.texts += texts
        self.mat = np.vstack([self.mat, np.array([embed(t) for t in texts])])
    def query(self, q: str, k: int = 5):
        sims = self.mat @ embed(q)                       # cosine, vectors are unit-norm
        top = np.argsort(-sims)[:k]
        return [(self.ids[i], float(sims[i]), self.texts[i]) for i in top]

def build_prompt(question: str, hits) -> str:
    ctx = "\n".join(f"[{cid}] {t}" for cid, _, t in hits)
    return ("Answer ONLY from the context. If the answer is not present, say 'I don't know'.\n\n"
            f"Context:\n{ctx}\n\nQuestion: {question}\nAnswer (cite chunk ids):")

def recall_at_k(ranked, relevant, k): return len(set(ranked[:k]) & relevant) / len(relevant)
def mrr(ranked, relevant):
    for r, cid in enumerate(ranked, 1):
        if cid in relevant: return 1 / r
    return 0.0
```

Test harness (append to the code above to reproduce the output):

```python
DOC = ("Refunds are processed within 5 business days. To request a refund, open the Orders page and click Refund. "
       "Shipping is free for orders above 50 dollars. Standard shipping takes 3 to 7 days. "
       "To reset your password, click Forgot Password on the login page. A reset link is emailed to you. "
       "Two factor authentication can be enabled in Security settings. "
       "Invoices are available under Billing. You can download invoices as PDF files.")

print("fixed:", len(fixed_chunks(DOC)), "chunks")
print("sentence:", len(sentence_chunks(DOC)), "chunks")
print("recursive:", len(recursive_chunks(DOC)), "chunks")

store = VectorStore()
chunks = sentence_chunks(DOC, max_chars=110)
store.add([f"c{i}" for i in range(len(chunks))], chunks)
print("\nIndexed", len(chunks), "chunks")
for q in ["how do I get my money back", "reset password link"]:
    for cid, s, t in store.query(q, k=2): print(f"  {cid} {s:.3f} {t[:60]}")
print("\n" + build_prompt("How do I reset my password?", store.query("reset password", 2)))

def find(kw): return {cid for cid, t in zip(store.ids, store.texts) if kw in t.lower()}
evalset = [("reset password link", find("forgot password")), ("how long do refunds take", find("5 business days")),
           ("free shipping threshold", find("free for orders")), ("download invoice pdf", find("invoices"))]
R = {1: [], 2: [], 5: []}; M = []
for q, rel in evalset:
    ranked = [cid for cid, _, _ in store.query(q, k=5)]
    for k in R: R[k].append(recall_at_k(ranked, rel, k))
    M.append(mrr(ranked, rel))
    print(f"{q!r:32} relevant={sorted(rel)} top5={ranked}")
print("\n" + "  ".join(f"recall@{k}={np.mean(v):.2f}" for k, v in R.items()) + f"  MRR={np.mean(M):.2f}")
```

**Output (real):**

```
fixed: 5 chunks
sentence: 4 chunks
recursive: 6 chunks

Indexed 5 chunks
  c0 0.000 Refunds are processed within 5 business days. To request a r        <- query: "how do I get my money back"
  c2 0.000 To reset your password, click Forgot Password on the login p
  c2 0.739 To reset your password, click Forgot Password on the login p        <- query: "reset password link"
  c0 0.000 Refunds are processed within 5 business days. To request a r

'reset password link'      relevant=['c2'] top5=['c2', 'c0', 'c1', 'c3', 'c4']
'how long do refunds take' relevant=['c0'] top5=['c3', 'c2', 'c4', 'c0', 'c1']
'free shipping threshold'  relevant=['c1'] top5=['c1', 'c0', 'c2', 'c3', 'c4']
'download invoice pdf'     relevant=['c3', 'c4'] top5=['c4', 'c0', 'c2', 'c1', 'c3']

recall@1=0.62  recall@2=0.62  recall@5=1.00  MRR=0.81
```

> **KEY INSIGHT — read the failures, they're the lesson.**
> 1. `"how do I get my money back"` scores **0.000** against the refund chunk: zero shared words. A real embedding model maps "money back" ≈ "refund". Bag-of-words can't. *That is exactly why we use dense embeddings.*
> 2. `"how long do refunds take"` ranks the right chunk **4th** because "refunds" ≠ "refund" (no stemming). MRR drops accordingly.
> 3. `recall@5 = 1.00` is **meaningless** here: we only have 5 chunks, so top-5 is everything. Always report recall at a k that is much smaller than corpus size (and use a bigger eval set). A sloppy interviewee quotes recall@5 on a tiny corpus.

**Swap in real embeddings + ChromaDB** ⚠️ NOT RUN (downloads a model from Hugging Face)

```python
from sentence_transformers import SentenceTransformer
import chromadb

model = SentenceTransformer("all-MiniLM-L6-v2")          # 384-dim, ~80MB, runs on CPU
client = chromadb.PersistentClient(path="./chroma_db")
col = client.get_or_create_collection("faq", metadata={"hnsw:space": "cosine"})

chunks = sentence_chunks(DOC, max_chars=110)
col.upsert(                                               # upsert = idempotent re-runs
    ids=[f"c{i}" for i in range(len(chunks))],
    documents=chunks,
    embeddings=model.encode(chunks, normalize_embeddings=True).tolist(),
    metadatas=[{"source": "faq.md", "chunk": i} for i in range(len(chunks))],
)

res = col.query(query_embeddings=model.encode(["how do I get my money back"],
                                              normalize_embeddings=True).tolist(), n_results=2)
for cid, dist, doc in zip(res["ids"][0], res["distances"][0], res["documents"][0]):
    print(cid, f"similarity={1 - dist:.3f}", doc[:60])    # Chroma returns cosine DISTANCE
```

Expected (not verified): the refund chunk should now rank first for "money back".

**Chunking trade-offs**

| Strategy | Good | Bad | Use when |
|---|---|---|---|
| Fixed + overlap | Trivial, predictable size | Cuts mid-sentence | Logs, unstructured dumps |
| Sentence-based | Keeps meaning intact | Uneven sizes | FAQs, prose |
| Recursive | Respects headings/paragraphs, size-bounded | More code | Markdown, docs, default choice |

> **COMMON MISTAKE:** Choosing chunk size without measuring. Too small → no context; too large → diluted embedding, more tokens, higher cost. Tune it using your recall/MRR eval.

> **In Real Production:** Add metadata (source, page, ACL/tenant id, updated_at) to every chunk. Filter by tenant **at query time** — otherwise user A can retrieve user B's documents.

### A6. LLM Data Pipelines

#### A6.1 End-to-end ingestion flow

```
scrape/ingest ─► clean (strip HTML, boilerplate) ─► dedupe (exact hash, near-dup MinHash)
   ─► chunk ─► embed (batched, rate-limited, retried) ─► upsert to index ─► eval + monitor
```

#### A6.2 Incremental updates: re-embed only what changed — ✅ **RAN**

Embedding API calls cost money. Hash the content; skip unchanged docs; delete vanished docs.

```python
import hashlib

def sha(t: str) -> str: return hashlib.sha256(t.encode()).hexdigest()[:12]

class Index:
    """doc_id -> content_hash. Re-embed only when the hash changes; delete stale docs."""
    def __init__(self): self.state: dict[str, str] = {}; self.embed_calls = 0
    def sync(self, docs: dict[str, str]) -> dict[str, list[str]]:
        report = {"embedded": [], "skipped": [], "deleted": []}
        for doc_id, text in docs.items():
            h = sha(text)
            if self.state.get(doc_id) == h: report["skipped"].append(doc_id); continue
            self.embed_calls += 1                        # <- the expensive API call
            self.state[doc_id] = h; report["embedded"].append(doc_id)
        for gone in set(self.state) - set(docs):
            del self.state[gone]; report["deleted"].append(gone)
        return report

idx = Index()
day1 = {"faq": "Refunds take 5 days.", "ship": "Shipping is free over $50.", "sec": "Enable 2FA in settings."}
print("day1", idx.sync(day1))
day2 = {"faq": "Refunds take 7 days.", "ship": "Shipping is free over $50.", "new": "Invoices are in Billing."}
print("day2", idx.sync(day2), "| total embed calls:", idx.embed_calls)
```

**Output (real):**

```
day1 {'embedded': ['faq', 'ship', 'sec'], 'skipped': [], 'deleted': []}
day2 {'embedded': ['faq', 'new'], 'skipped': ['ship'], 'deleted': ['sec']} | total embed calls: 5
```

Day 2 made 2 calls instead of 3. At millions of documents that's the difference between a $20 and a $2,000 refresh.

> **COMMON MISTAKE:** Forgetting **deletes**. Stale chunks of removed/changed docs stay in the index and the LLM confidently quotes outdated policy. In a real vector DB you also delete *all old chunks of a changed doc* (store `doc_id` in metadata) before upserting new ones, since the new version may have fewer chunks.

#### A6.3 Parsing LLM output into structured data — ✅ **RAN**

LLMs wrap JSON in prose and code fences, and they hallucinate invalid values. Extract, then **validate**.

```python
import json, re
from typing import Optional
from pydantic import BaseModel, Field, ValidationError, field_validator

class Ticket(BaseModel):
    category: str = Field(pattern="^(billing|shipping|auth|other)$")
    priority: int = Field(ge=1, le=5)
    amount_usd: Optional[float] = None
    @field_validator("amount_usd", mode="before")
    @classmethod
    def strip_dollar(cls, v):
        return float(v.replace("$", "").replace(",", "")) if isinstance(v, str) else v

def parse_llm(raw: str) -> Ticket:
    m = re.search(r"\{.*\}", raw, re.S)
    if not m: raise ValueError("no JSON object found")
    return Ticket.model_validate(json.loads(m.group(0)))

good = 'Sure! Here you go:\n```json\n{"category":"billing","priority":2,"amount_usd":"$1,250.50"}\n```'
bad  = '{"category":"refunds","priority":9}'
print(parse_llm(good))
try: parse_llm(bad)
except ValidationError as e: print("rejected:", [(x["loc"][0], x["type"]) for x in e.errors()])
```

**Output (real):**

```
category='billing' priority=2 amount_usd=1250.5
rejected: [('category', 'string_pattern_mismatch'), ('priority', 'less_than_equal')]
```

> **In Real Production:** On `ValidationError`, retry once with the error message appended to the prompt ("your last output failed: …"). After N failures, route to a dead-letter queue — never write unvalidated LLM output to a warehouse table. Prefer provider-native *structured output / tool-calling* modes when available; keep the pydantic validation anyway.

### A7. Vector Databases

**HNSW (Hierarchical Navigable Small World)** — a multi-layer graph. Upper layers are sparse "highways" for big jumps; the bottom layer has every vector. Search starts at the top, greedily walks toward the query, drops a layer, repeats. Gives approximate nearest neighbours (ANN) in roughly O(log N) instead of O(N).

Key knobs: `M` (links per node — recall & memory ↑), `efConstruction` (build quality), `efSearch` (query-time recall vs latency).

| Metric | Formula idea | Use when |
|---|---|---|
| Cosine | angle between vectors | Text embeddings (default) |
| Dot product | magnitude + angle | Embeddings trained for it (some recommenders) |
| Euclidean (L2) | straight-line distance | Image/numeric embeddings |

**Vector DB vs traditional search**

| Need | Pick |
|---|---|
| Exact keywords, IDs, error codes, filters | BM25 / Elasticsearch / SQL |
| Meaning-based ("money back" ≈ "refund") | Vector search |
| Best quality in practice | **Hybrid**: BM25 + vector, merged with reciprocal-rank fusion, then a re-ranker |
| < ~1M vectors, already on Postgres | `pgvector` is often enough |

### A8. Text-to-SQL Pipeline — ✅ **RAN** (LLM call stubbed with a hard-coded response; everything else is real)

Steps: **extract schema → build prompt → LLM → validate → run with guardrails.** Validation is the part interviewers care about: never execute raw LLM SQL.

```python
import re, duckdb
con = duckdb.connect()
con.execute("CREATE TABLE customers(customer_id INT PRIMARY KEY, name VARCHAR, country VARCHAR)")
con.execute("CREATE TABLE orders(order_id INT, customer_id INT, amount DECIMAL(10,2), order_date DATE)")
con.execute("INSERT INTO customers VALUES (1,'Asha','IN'),(2,'Bob','US'),(3,'Chen','US')")
con.execute("INSERT INTO orders VALUES (1,1,100,'2024-01-05'),(2,2,250,'2024-01-06'),(3,2,50,'2024-02-01'),(4,3,75,'2024-02-02')")

def extract_schema(con) -> str:
    rows = con.execute("SELECT table_name, column_name, data_type FROM information_schema.columns "
                       "WHERE table_schema='main' ORDER BY table_name, ordinal_position").fetchall()
    tables: dict[str, list[str]] = {}
    for t, c, d in rows: tables.setdefault(t, []).append(f"{c} {d}")
    return "\n".join(f"TABLE {t}({', '.join(cols)})" for t, cols in tables.items())

def build_sql_prompt(question: str) -> str:
    return f"You write DuckDB SQL. Return ONLY one SELECT statement.\n{extract_schema(con)}\nQuestion: {question}\nSQL:"

FORBIDDEN = re.compile(r"\b(insert|update|delete|drop|alter|create|attach|copy|pragma|call)\b", re.I)
def validate_sql(sql: str, max_rows: int = 100) -> str:
    sql = sql.strip().rstrip(";")
    if ";" in sql: raise ValueError("multiple statements")
    if not sql.lower().startswith(("select", "with")): raise ValueError("only SELECT/WITH allowed")
    if FORBIDDEN.search(sql): raise ValueError("forbidden keyword")
    con.execute(f"EXPLAIN {sql}")                           # parse + bind check without executing
    return f"SELECT * FROM ({sql}) LIMIT {max_rows}"

fake_llm = ("SELECT c.country, SUM(o.amount) AS total FROM orders o "
            "JOIN customers c USING(customer_id) GROUP BY 1 ORDER BY 2 DESC")
print(build_sql_prompt("Total order amount per country?"))
print(con.execute(validate_sql(fake_llm)).fetchall())
for evil in ["DROP TABLE orders", "SELECT * FROM orders; DELETE FROM orders", "SELECT nope FROM orders"]:
    try: validate_sql(evil)
    except Exception as e: print(f"blocked {evil[:32]!r}: {type(e).__name__}")
```

**Output (real):**

```
You write DuckDB SQL. Return ONLY one SELECT statement.
TABLE customers(customer_id INTEGER, name VARCHAR, country VARCHAR)
TABLE orders(order_id INTEGER, customer_id INTEGER, amount DECIMAL(10,2), order_date DATE)
Question: Total order amount per country?
SQL:
[('US', Decimal('375.00')), ('IN', Decimal('100.00'))]
blocked 'DROP TABLE orders': ValueError
blocked 'SELECT * FROM orders; DELETE FROM orders': ValueError
blocked 'SELECT nope FROM orders': BinderException
```

> **COMMON MISTAKE:** Regex blocklists alone are *not* a security boundary. The real control is a **read-only database role** limited to approved schemas/views, plus query timeouts and row limits. Treat the validator as a usability layer (fast, friendly errors), not the lock on the door.
> **Improvements to mention:** only include relevant tables in the prompt (schema retrieval for big warehouses), add column descriptions and 2–3 example queries, feed errors back for one self-repair attempt, and evaluate with *execution accuracy* (do result sets match a gold query?), not string match.

---

## Part B — Guided Practice (10 problems)

*Format: scenario → hint → solution sketch → complexity → interviewer follow-up. Try each for 15 min before reading.*

### Easy (1–4)

**B1. [Stripe] Recency feature.** Given `txns(user_id, txn_ts)`, compute `days_since_last_txn` as of `2024-03-15`.
- *HINT:* filter `< as_of` first, then `max`.
- *Solution:* `hist.groupBy("user_id").agg(F.datediff(F.lit("2024-03-15"), F.max("txn_ts")))`. **O(N)** time, O(users) space.
- *Follow-up:* "Do it for *every day* of the last year without 365 Spark jobs." → Generate a date spine, cross-join with users, `merge_asof`/window `last()` ignoring nulls.

**B2. [Amazon] Chunk counter.** How many chunks does a 10,000-char document produce at size 500, overlap 100?
- *HINT:* step = size − overlap.
- *Solution:* step = 400. Chunks start at 0, 400, …; last start must satisfy `start + 500 ≥ 10000` → start 9600 → starts = 0..9600 step 400 → **25 chunks**. (My `fixed_chunks` stops once a chunk reaches the end, so it also yields 25.)
- *Follow-up:* "What's the token cost of overlap?" → ~25% extra embedding tokens (100/400).

**B3. [Meta] Cosine similarity.** Implement it in NumPy; explain why unit-normalising lets you use a dot product.
- *Solution:* `a @ b / (np.linalg.norm(a) * np.linalg.norm(b))`; if both are unit vectors the denominator is 1. **O(d)**.
- *Follow-up:* "Why is cosine distance not a true metric?" → fails triangle inequality.

**B4. [Databricks] MLflow reproducibility.** Name 5 things you must log so a model can be reproduced.
- *Solution:* params, metrics, **data/feature-table version**, **git commit / code version**, environment (conda/pip freeze), random seeds, plus the model signature.
- *Follow-up:* "Where do you store a 5GB training set?" → don't log it; log a pointer + Delta table version (`VERSION AS OF`).

### Medium (5–8)

**B5. [Uber] Point-in-time join in Spark.** You have `labels(user_id, label_ts)` and `features(user_id, feature_ts, f1)`. Return the latest feature row *at or before* each label.
- *HINT:* join on `user_id`, filter `feature_ts <= label_ts`, `row_number()` over `partition by user_id,label_ts order by feature_ts desc`, keep 1.
- *Solution:* the window approach above. **O(N log N)** per partition due to the sort.
- *Follow-up:* "Skew: one user has 50M rows." → salt the key, or pre-aggregate features to daily snapshots, or use range-join hints in Databricks.

**B6. [Airbnb] Training-serving skew.** Offline AUC is 0.94, online performance is poor. List 5 causes and how to detect each.
- *Solution:* (1) point-in-time leak → re-run with strict `<`. (2) Different code paths for batch vs online → single feature definition. (3) Stale online features → monitor feature age. (4) Different null/default handling → compare distributions of online vs offline per feature (PSI/KS test). (5) Label delay or population shift.
- *Follow-up:* "How do you automate detection?" → log served features, join with offline recompute, alert on drift.

**B7. [Stripe] Dedup for RAG.** 10M scraped pages, ~30% near-duplicates. Design dedup.
- *Solution:* exact dupes via content hash; near dupes via **MinHash + LSH** on shingles (Jaccard threshold ~0.8). Keep the canonical doc with the latest `updated_at`.
- *Complexity:* roughly linear in docs vs O(N²) pairwise comparison.
- *Follow-up:* "How do you dedupe at the *chunk* level across docs?" → hash normalised chunk text; store `chunk_hash → [doc_ids]`.

**B8. [Google] Evaluate retrieval.** Given ranked results per query and gold relevant sets, implement recall@k, precision@k, MRR, and nDCG@k.
- *Solution:* recall@k and MRR above; precision@k = hits/k; nDCG = DCG/IDCG with `rel/log2(rank+1)`.
- *Follow-up:* "Recall is 95% but answers are bad." → retrieval is fine; look at chunk quality, prompt, context ordering ("lost in the middle"), or add a re-ranker. Also evaluate **faithfulness** separately.

### Hard (9–10)

**B9. [FinTech] Feature freshness SLA.** Fraud model needs `txn_count_10min` per card in < 100 ms with ≤ 5 s staleness. Design computation and storage.
- *Solution:* Kafka → Flink/Spark Structured Streaming with sliding window (10 min, slide 10 s) → write to Redis with TTL; keep the same feature spec in the offline store, backfilled via batch from the same logic. For hot keys, pre-aggregate in 10s buckets and sum 60 buckets at read time (O(60) per read, no heavy window state).
- *Follow-up:* "Late events up to 2 min." → watermark of 2 min for offline correction; online accepts approximate, offline is the source of truth for training.

**B10. [Anthropic/OpenAI-style infra] Multi-tenant RAG.** 5,000 tenants; sizes range 10 docs to 10M docs. Index layout?
- *Solution:* shared collection + `tenant_id` metadata filter for small tenants (cost-efficient); dedicated index/namespace for large tenants; enforce filter server-side from the auth token, never from the client. Tombstone + async delete for "right to be forgotten".
- *Follow-up:* "Filtered ANN recall drops." → HNSW with restrictive filters degrades; use pre-filtering / partitioned indexes per tenant for big tenants.

---

## Part C — Interview Simulations

### Sim 1: "Design a RAG pipeline for a customer-support chatbot."

**Think out loud, step by step**

1. **Clarify (≈2 min).** Corpus size and types (help center, tickets, PDFs)? Languages? Latency target? Must answers cite sources? Per-customer data (orders) or only public docs? Update frequency? Hallucination tolerance (refund policy errors are costly)?
2. **Brute force v0.** Dump all docs into the prompt. *Why it fails:* context limit, cost, latency, no ACLs.
3. **v1 (basic RAG).** Chunk (recursive, ~300–500 tokens, 10–15% overlap) → embed → vector DB → top-5 → prompt → LLM.
4. **v2 (what makes it good).**
   - Hybrid retrieval (BM25 + dense) + re-ranker; query rewriting for multi-turn chat.
   - Metadata filters: product, language, tenant, `valid_to`.
   - Prompt: "answer only from context; say you don't know; cite ids."
   - Escalate to a human when retrieval score is below a threshold.
5. **Ingestion.** Change detection by content hash, incremental upsert, delete old chunks per `doc_id`, nightly full reconciliation, DLQ for parse failures.
6. **Evaluation.** Offline: labelled Q→chunk set → recall@k, MRR; LLM-as-judge for faithfulness plus a human-reviewed sample. Online: thumbs up/down, deflection rate, escalation rate, "no answer" rate.
7. **Ops.** Cache frequent questions (semantic cache), log retrieved chunk ids with every answer, cost per conversation dashboard, PII redaction before indexing and before logging.

| Interviewer is evaluating | Green flag | Red flag |
|---|---|---|
| Problem framing | Asks about accuracy cost and update frequency first | Jumps straight to "use LangChain + Pinecone" |
| Retrieval depth | Hybrid + re-rank + filters, justified | "Just embed and cosine" |
| Data engineering | Incremental ingest, deletes, idempotent upserts | Full re-embed every night |
| Evaluation | Separates retrieval metrics from answer quality | "We'll eyeball it" |
| Safety | Tenant isolation, PII handling, refusal path | Not mentioned |

### Sim 2: "Design the data platform for an AI startup that trains and serves custom models."

1. **Clarify.** Model types (LLM fine-tuning vs classical ML)? Data volume and modalities? Number of training runs/week? Online latency SLA? Compliance (customer data in training)?
2. **High-level architecture.**

```
 Sources (app events, DB CDC, files, 3rd-party)
        │
   Ingest: Kafka / Fivetran / Auto Loader
        ▼
   Lakehouse (Delta):  bronze ─► silver ─► gold
        │                         │
        │                   Feature store (offline Delta + online Redis/Dynamo)
        ▼                         │
   Training data builder ─► Training (GPU cluster, Ray/Spark)
        │                         │
        └──── MLflow tracking + registry (data version + code commit + metrics)
                                  ▼
                      CI/CD gate (eval suite) ─► Serving (autoscaled) ─► Monitoring
                                                          ▲                  │
                                                          └── feedback logs ─┘
```

3. **Key decisions to articulate:**
   - **Versioned, immutable training datasets** (Delta time travel) so every model links to an exact data snapshot.
   - **Point-in-time correct** training set generation.
   - **Eval gate:** a model is promoted only if it beats champion on a frozen eval set *and* passes slice/regression checks.
   - **Monitoring:** feature drift (PSI), prediction drift, latency, cost per 1k requests, plus delayed-label quality metrics.
   - **Governance:** lineage from model → dataset → sources; consent flags to exclude opted-out users from training.
4. **Scale/cost:** spot GPUs with checkpointing; cache tokenised/pre-processed datasets; store embeddings with a TTL if re-computable.

**Green flags:** talks about reproducibility, data versioning, eval gates, consent/lineage. **Red flags:** only draws boxes for "model training" with no data contracts, no monitoring, no rollback story.

---

## Chapter 18 Cheat Sheet

- Same feature code for training and serving → kills skew.
- Point-in-time: **strictly before** the label timestamp.
- RAG = ingest quality (clean, dedupe, chunk, metadata) + hybrid retrieval + eval. The LLM call is the small part.
- Report recall@k at k ≪ corpus size; keep retrieval metrics separate from answer quality.
- Incremental embedding: content hash, upsert, **delete stale**.
- Validate every LLM output (pydantic; SQL read-only role + validator).
- Log data version + code version with every model.

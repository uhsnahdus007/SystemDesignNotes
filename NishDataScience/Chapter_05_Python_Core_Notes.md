# Chapter 5: Python Core — From Basics to Production-Grade Code

> **Verification status.** All code was executed on **Python 3.12** (stdlib + `pyarrow`, `aiohttp`, `requests`); outputs are real.
> - **My sandbox has ONE CPU core** (`os.cpu_count() == 1`). That means I **cannot demonstrate multiprocessing speed-ups or the GIL's effect on CPU-bound threads**: both showed ~1.0× (no gain), but on a single core you'd see that *even without a GIL*, so the numbers prove nothing about the GIL. I show them for honesty and explain the theory. I/O-bound concurrency (threads, asyncio) *does* show real speed-ups on one core, because the waiting overlaps.
> - ⚠️ **Not run:** real S3 (no AWS access), real third-party websites. S3 code is shaped correctly but uses a stub reader.
> - Memory numbers come from `tracemalloc` (which adds overhead and measures Python allocations only), so treat them as relative.

---

## Part A — Concepts + Code

### A1. Data structures with DE context

| Structure | Typical DE use | Lookup | Insert | Delete | Notes |
|---|---|---|---|---|---|
| `list` | Ordered batches of records | O(n) search, O(1) index | O(1) append, **O(n)** insert/pop(0) | O(n) | Contiguous array |
| `dict` | Config, lookup tables, JSON | **O(1)** avg | O(1) | O(1) | Insertion-ordered |
| `set` | Dedup, EXCEPT/INTERSECT | **O(1)** | O(1) | O(1) | Elements must be hashable |
| `tuple` / `namedtuple` | Immutable typed rows | O(1) index | n/a | n/a | Hashable (usable as dict key) |
| `deque` | Sliding windows, queues | O(n) middle | **O(1)** both ends | O(1) both ends | `maxlen` auto-evicts |
| `heapq` | Top-K, merge K sorted | O(1) min | O(log n) | O(log n) pop | Min-heap |
| `Counter` / `defaultdict` / `OrderedDict` | Frequencies / grouping | O(1) | O(1) | | `OrderedDict` mostly redundant since 3.7 (still has `move_to_end`) |

**Lists: chunk 10,000 records into batches of 500** ✅
```python
def chunks(items: List[Any], n: int) -> Iterator[List[Any]]:
    for i in range(0, len(items), n): yield items[i:i + n]
# 10,000 records -> 20 batches of 500;  10,001 records -> last two batches [500, 1]  (always test the uneven tail)
```
(Python 3.12 also has `itertools.batched(iterable, n)`.)

**Dicts: a nested schema validator** ✅
```python
def validate_schema(rec: dict, schema: dict, path: str = "") -> List[str]:
    errs = []
    for k, rule in schema.items():
        p = f"{path}{k}"
        if k not in rec:
            if rule.get("required", True): errs.append(f"{p}: missing")
            continue
        v = rec[k]
        if "fields" in rule: errs += validate_schema(v, rule["fields"], p + ".") if isinstance(v, dict) else [f"{p}: expected object"]
        elif not isinstance(v, rule["type"]) or (rule["type"] is int and isinstance(v, bool)): errs.append(f"{p}: expected {rule['type'].__name__}, got {type(v).__name__}")
        elif "min" in rule and v < rule["min"]: errs.append(f"{p}: {v} < {rule['min']}")
    return errs
SCHEMA = {"id": {"type": int}, "user": {"type": dict, "fields": {"email": {"type": str}, "age": {"type": int, "min": 0, "required": False}}}, "amount": {"type": float, "min": 0}}
```
```
valid record                                     -> []
{"id":"1","user":{"age":-3},"amount":-1.0}       -> ['id: expected int, got str', 'user.email: missing', 'user.age: -3 < 0', 'amount: -1.0 < 0']
```
(`bool` is a subclass of `int`, so `True` would pass `isinstance(v, int)`; the extra check handles that classic trap.) It reports **all** errors at once, with dotted paths.

**Sets: records in source but not target (SQL `EXCEPT`)** ✅
```python
src = {(1, "a"), (2, "b"), (3, "c")}; tgt = {(1, "a"), (3, "x")}
src - tgt   # [(2,'b'), (3,'c')]  <- missing or different in target     src ^ tgt  # symmetric difference
```
Rows must be hashable (use tuples, not lists/dicts). `(3,'c')` appears because the *whole tuple* differs from `(3,'x')`; compare on keys for "missing" vs "changed".

**Tuples / namedtuple:** `Rec = namedtuple("Rec", "id name amount")` → immutable, attribute access, `_asdict()` ✅ (assigning raises `AttributeError`).
**Counter:** `Counter(errs).most_common(2)` → `[('TimeoutError', 3), ('KeyError', 2)]` ✅ (most common error types in a log).
**defaultdict:** `g = defaultdict(list); g[x % 3].append(x)` → `{0:[0,3,6], 1:[1,4], 2:[2,5]}` ✅ (group without `if key in d`).
**deque sliding average** (`maxlen=3` over `[1,2,3,4,5]`) → `[1.0, 1.5, 2.0, 3.0, 4.0]` ✅.
**heapq top-K without sorting everything** ✅ (100,000 numbers, top 10): `heapq.nlargest` **12.6 ms** vs full sort **57.0 ms**, same result. O(n log k) vs O(n log n).

### A2. Functions: production patterns  ✅ RAN

```python
def transform_records(records: Iterable[dict], **transforms: Callable[[Any], Any]) -> List[dict]:
    """Each kwarg is column_name=function applied to that column."""
    out = []
    for r in records:
        r = dict(r)                                         # copy: never mutate the caller's data
        for col, fn in transforms.items():
            if col in r: r[col] = fn(r[col])
        out.append(r)
    return out
transform_records([{"name": " asha ", "amt": "10"}], name=lambda s: s.strip().title(), amt=float)
# [{'name': 'Asha', 'amt': 10.0}]
```
```python
list(map(lambda x: x * 2, filter(lambda x: x % 2, range(8))))        # [2, 6, 10, 14]
functools.reduce(lambda acc, r: {**acc, r["k"]: acc.get(r["k"], 0) + r["v"]}, rows, {})    # {'a': 4, 'b': 2}  aggregation pipeline
pipeline = lambda *fns: (lambda x: functools.reduce(lambda acc, f: f(acc), fns, x))        # compose
pipeline(str.strip, str.lower, lambda s: s.replace(" ", "_"))("  Hello World ")           # 'hello_world'
```
**Conventions to follow (and mention):** type hints on every public function; Google-style docstrings (`Args:`, `Returns:`, `Raises:`); `*args/**kwargs` for flexible wrappers, but prefer explicit parameters in core APIs; **never use mutable default arguments** (`def f(x=[])` shares one list across calls); prefer comprehensions/`map` over `reduce` for readability.

### A3. Generators (critical for large data)  ✅ RAN

**Why:** a generator produces values lazily, one at a time, so memory stays **constant** regardless of data size. `yield` pauses the function and resumes it; `return` ends it.

**Memory: list vs generator, 1,000,000 values**
```
sum([i * 2 for i in range(N)])   peak 40.4 MB          sum(i * 2 for i in range(N))   peak 0.0005 MB     (equal results)
```
**CSV reader yielding one row at a time** (300,000-row, 4.9 MB file):
```python
def read_rows(path: str) -> Iterator[dict]:
    with open(path, newline="") as f:
        yield from csv.DictReader(f)                # file stays open until the generator is exhausted or closed
total_stream = sum(float(r["amount"]) for r in read_rows(path))
total_load   = sum(float(r["amount"]) for r in list(read_rows(path)))
```
```
streaming peak 0.05 MB | load-all peak 98.5 MB | same total: True        (a 20x bigger file only changes the second number)
```
**Database cursor wrapper yielding batches:**
```python
def db_batches(rows: Iterable[tuple], size: int) -> Iterator[List[tuple]]:
    it = iter(rows)
    while batch := list(itertools.islice(it, size)): yield batch
[len(b) for b in db_batches(((i,) for i in range(2050)), 1000)]     # [1000, 1000, 50]
# real DB: use a server-side cursor (psycopg2 named cursor / SQLAlchemy stream_results=True), else the driver loads everything client-side first
```
**itertools for DE** ✅: `chain([1,2],[3]) → [1,2,3]` (concatenate files lazily) · `islice(read_rows(p), 3)` (sample the head of a stream) · `groupby(sorted(...))` → `{'a':3,'b':2,'c':1}` (**input must be sorted by the key**, or you get fragmented groups) · `product([8,16], ["zstd","snappy"])` (parameter grids).
> **COMMON MISTAKE:** generators are **single-use**: `gen = (i for i in range(3)); list(gen) → [0,1,2]; list(gen) → []` ✅. Re-iterating a generator silently yields nothing. If you need two passes, re-create it or materialize.

### A4. Decorators for pipeline code  ✅ RAN (all five built from scratch)

Always use `functools.wraps` so the wrapped function keeps its `__name__`/docstring (verified: `flaky.__name__ == 'flaky'`).

```python
def timer(fn):
    @functools.wraps(fn)
    def w(*a, **k):
        t = time.perf_counter(); r = fn(*a, **k); w.last_ms = (time.perf_counter() - t) * 1000; return r
    return w

def retry(max_attempts: int = 3, backoff: float = 2.0, base_delay: float = 1.0, exceptions: tuple = (Exception,)):
    def deco(fn):
        @functools.wraps(fn)
        def w(*a, **k):
            for attempt in range(1, max_attempts + 1):
                try: return fn(*a, **k)
                except exceptions:
                    if attempt == max_attempts: raise                       # out of attempts: re-raise the ORIGINAL error
                    time.sleep(base_delay * backoff ** (attempt - 1))       # exponential backoff
        return w
    return deco

def log_execution(fn):
    @functools.wraps(fn)
    def w(*a, **k):
        log.info("ENTER %s args=%s kwargs=%s", fn.__name__, a, k)
        try: r = fn(*a, **k); log.info("EXIT  %s -> %r", fn.__name__, r); return r
        except Exception as e: log.error("ERROR %s: %s: %s", fn.__name__, type(e).__name__, e); raise
    return w

def validate_schema(required: Dict[str, type]):
    def deco(fn):
        @functools.wraps(fn)
        def w(records, *a, **k):
            for i, r in enumerate(records):
                for col, typ in required.items():
                    if col not in r: raise ValueError(f"record {i}: missing '{col}'")
                    if not isinstance(r[col], typ): raise TypeError(f"record {i}: '{col}' should be {typ.__name__}, got {type(r[col]).__name__}")
            return fn(records, *a, **k)
        return w
    return deco

@functools.lru_cache(maxsize=128)           # @cache (3.9+) is the unbounded version
def lookup(code: str) -> str: ...
```
**Test results:**
```
@timer          : 20.2 ms for a 20 ms sleep
@retry(4, 2.0)  : succeeded on the 3rd call; delays slept = [0.01, 0.02] (exponential); non-matching exception (KeyError) NOT retried -> propagates immediately
@log_execution  : ["ENTER add args=(1,) kwargs={'b': 2}", 'EXIT  add -> 3']
@validate_schema: valid -> 1 | bad -> TypeError: record 1: 'id' should be int, got str
@lru_cache      : first call 10.19 ms, second 0.0017 ms | CacheInfo(hits=1, misses=1, maxsize=128, currsize=1)
```
> **COMMON MISTAKES:** (1) `@retry` with a bare `except:`/`Exception` retries *bugs* (TypeError, KeyError) → restrict to transient types (as tested). (2) Retrying **non-idempotent** operations (e.g. a payment POST) without an idempotency key duplicates side effects. (3) `lru_cache` on methods holds `self` alive (memory leak) and on functions with unhashable args raises; never cache results that depend on time/mutable state without a TTL.

### A5. Context managers & file I/O  ✅ RAN

```python
class DatabaseConnection:                              # commit on success, roll back on error
    def __init__(self, db): self.db = db
    def __enter__(self): self.db.log.append("BEGIN"); return self.db
    def __exit__(self, exc_type, exc, tb):
        self.db.log.append("ROLLBACK" if exc_type else "COMMIT"); return False       # False = do NOT swallow the exception
class TempFile:                                        # auto-delete after processing
    def __enter__(self): fd, self.path = tempfile.mkstemp(suffix=".tmp"); os.close(fd); return self.path
    def __exit__(self, *exc): os.path.exists(self.path) and os.remove(self.path); return False
@contextmanager                                        # generator-based shortcut
def timed(label, sink): t = time.perf_counter(); yield; sink.append((label, round((time.perf_counter() - t) * 1000)))
```
```
DatabaseConnection: ['BEGIN', 'COMMIT', 'BEGIN', 'ROLLBACK']      <- 2nd block raised RuntimeError("boom")
TempFile: existed inside = True | exists after = False
@contextmanager timed: [('sleep', 20)]
```
> **Rule:** `__exit__` returning a truthy value **suppresses** the exception: almost never what you want. In `@contextmanager`, wrap the `yield` in `try/finally` if cleanup must run on error.

**Files: CSV, JSON/JSONL, Parquet, gzip, pathlib** ✅
```python
pl = pathlib.Path(tmp); (pl / "o.jsonl").write_text("\n".join(json.dumps({"i": i}) for i in range(3)))
with gzip.open(pl / "o.jsonl.gz", "wt") as f: f.write((pl / "o.jsonl").read_text())
[json.loads(l)["i"] for l in gzip.open(pl / "o.jsonl.gz", "rt")]      # [0, 1, 2]    suffixes -> ['.jsonl', '.gz']
pq.write_table(pa.table({"id": [1, 2], "v": [1.5, None]}), pl / "o.parquet"); pq.read_table(pl / "o.parquet").to_pylist()   # [{'id':1,'v':1.5},{'id':2,'v':None}]
```
**"Process a 5 GB CSV line by line"** = generator + context manager (A3): constant memory, ~tens of MB; add `gzip.open(..., "rt")` for compressed input (streams without decompressing to disk). Prefer **JSONL** over one giant JSON array (JSONL is streamable and appendable). Use `newline=""` with the `csv` module.

### A6. Error handling for data pipelines  ✅ RAN

```
try/except/else/finally:  no error -> ['try', 'else', 'finally']   |   error -> ['try', 'except', 'finally']
finally even runs after `return` inside try.
```
`else` = code that should run only if `try` succeeded (keeps the `try` block minimal, so you don't catch exceptions from unrelated code). `finally` = cleanup (close files, release locks).

**Custom exceptions** (a hierarchy lets callers decide retry vs reject vs abort):
```python
class PipelineError(Exception): ...
class DataValidationError(PipelineError): ...        # bad RECORD -> dead-letter and continue
class SchemaError(PipelineError): ...                # bad SOURCE shape -> usually stop the run
class SourceUnavailableError(PipelineError): ...     # TRANSIENT -> retry with backoff
class FatalPipelineError(PipelineError): ...         # give up
```
**Pipeline step: retries transient errors, dead-letters bad records, raises on fatal errors**
```python
def process_step(records, transform, max_retries=3, dead_letter=None, sleep=time.sleep):
    out = []
    for rec in records:
        for attempt in range(1, max_retries + 1):
            try: out.append(transform(rec)); break
            except SourceUnavailableError:
                log.warning("transient failure", extra={"record_id": rec.get("id"), "attempt": attempt})
                if attempt == max_retries: raise FatalPipelineError(f"source unavailable after {max_retries} attempts (record {rec.get('id')})")
                sleep(0.1 * 2 ** attempt)
            except (DataValidationError, ValueError, KeyError) as e:
                log.error("bad record -> dead letter", extra={"record_id": rec.get("id")}, exc_info=True)
                dead_letter.append({"record": rec, "error": f"{type(e).__name__}: {e}"}); break
    return out
```
Test: 7 records; id 3 raises `DataValidationError`; id 5's enrichment is down for its first 2 attempts:
```
processed 6 of 7 | dead-lettered [3] | id 5 succeeded after 2 transient failures
always-down source (max_retries=2) -> FatalPipelineError: source unavailable after 2 attempts (record 1)
```
**Structured JSON logging** (machine-parseable; `run_id` for correlation):
```python
class JsonFormatter(logging.Formatter):
    def format(self, r):
        d = {"ts": self.formatTime(r, "%Y-%m-%dT%H:%M:%S"), "level": r.levelname, "logger": r.name, "msg": r.getMessage()}
        d.update({k: v for k, v in r.__dict__.items() if k in ("run_id", "step", "record_id", "attempt", "rows")})
        if r.exc_info: d["exc"] = r.exc_info[0].__name__
        return json.dumps(d)
class Merging(logging.LoggerAdapter):                                  # see the gotcha below
    def process(self, msg, kwargs): kwargs["extra"] = {**self.extra, **kwargs.get("extra", {})}; return msg, kwargs
```
```
{"ts": "...", "level": "ERROR",   "logger": "pipeline", "msg": "bad record -> dead letter", "run_id": "run-42", "record_id": 3, "exc": "DataValidationError"}
{"ts": "...", "level": "WARNING", "logger": "pipeline", "msg": "transient failure",         "run_id": "run-42", "record_id": 5, "attempt": 1}
{"ts": "...", "level": "WARNING", "logger": "pipeline", "msg": "transient failure",         "run_id": "run-42", "record_id": 5, "attempt": 2}
```
> **A real gotcha I hit:** the standard `logging.LoggerAdapter` **replaces** call-level `extra=...` with its own, so my first version silently dropped `record_id` and `attempt` from every log line (only `run_id` appeared). The `Merging` subclass above fixes it. Test your log output, not just your logging code.
> **Practice:** never `except: pass`; log with context (ids, attempt, source), keep the stack trace for unexpected errors (`exc_info=True`), and don't log secrets/PII.

### A7. OOP for pipeline design  ✅ RAN (mini pipeline framework, ~55 lines of core)

```python
@dataclass(frozen=True)
class PipelineConfig:
    source: str; target: str; batch_size: int = 1000; retry_count: int = 3
    def __post_init__(self):
        if self.batch_size < 1: raise ValueError("batch_size must be >= 1")
@dataclass
class DataRecord:
    id: int; payload: Dict[str, Any]; timestamp: float = field(default_factory=time.time); source: str = "unknown"   # default_factory: NEVER a mutable default
class Extractor(ABC):
    @abstractmethod
    def extract(self) -> Iterator[DataRecord]: ...
class Step(ABC):
    @abstractmethod
    def run(self, records: Iterable[DataRecord]) -> Iterable[DataRecord]: ...
class CSVExtractor(Extractor): ...      class APIExtractor(Extractor): ...      class S3Extractor(Extractor): ...     # ⚠️ S3: shape only, stub reader
class CastAmount(Step):
    def run(self, records):
        for r in records: r.payload["amount"] = float(r.payload["amount"]); yield r
class DropNegative(Step):
    def __init__(self): self.dropped = 0
    def run(self, records):
        for r in records:
            if r.payload["amount"] < 0: self.dropped += 1; continue
            yield r
class Pipeline:
    def __init__(self, cfg, extractor, steps, sink): self.cfg, self.extractor, self.steps, self.sink = cfg, extractor, steps, sink; self.metrics = {"read": 0, "written": 0}
    def run(self):
        stream = self._counted(self.extractor.extract())
        for s in self.steps: stream = s.run(stream)                     # lazily chained generators -> constant memory
        batch = []
        for r in stream:
            batch.append(r.payload)
            if len(batch) >= self.cfg.batch_size: self._flush(batch)
        if batch: self._flush(batch)
        return self.metrics
```
```
CSV pipeline (4 rows: 10, -5, 7.5, 1; batch_size=2): {'read': 4, 'written': 3} | dropped: 1
   sink: [{'id':'1','amount':10.0}, {'id':'3','amount':7.5}, {'id':'4','amount':1.0}]
API pipeline (same framework, different extractor):   {'read': 2, 'written': 2}
PipelineConfig(batch_size=0)   -> ValueError: batch_size must be >= 1
cfg.batch_size = 5             -> FrozenInstanceError (frozen dataclass)
Step()                         -> TypeError: Can't instantiate abstract class Step ...  (ABC enforces the contract)
```
**Design points:** *composition over inheritance* (steps are small, swappable objects); *dependency injection* (extractor/sink passed in → trivial to unit-test with fakes); *frozen* config objects (can't be mutated mid-run); *abstract base classes* define the interface; steps are generators so the whole pipeline streams.

### A8. Concurrency for DE  ✅ RAN (with the single-core caveat above)

**Which tool when**

| Workload | Use | Why |
|---|---|---|
| Many network/disk waits (API calls, S3, DB) | **Threads** (`ThreadPoolExecutor`) or **asyncio** | The GIL is released while waiting on I/O |
| Thousands of concurrent I/O ops | **asyncio** (+ `aiohttp`) | Lightweight tasks, no thread-per-request |
| CPU-heavy pure-Python (parsing, transforms) | **Processes** (`ProcessPoolExecutor`/`multiprocessing`) | Each process has its own interpreter and GIL |
| Numeric work in NumPy/pandas/polars/PyArrow | Often threads work (C code releases the GIL), or just vectorize | |

**The GIL:** CPython's Global Interpreter Lock lets **only one thread execute Python bytecode at a time**, so threads don't speed up CPU-bound Python code; they do overlap I/O waits (the GIL is released while blocked). (Python 3.13 has an optional free-threaded build; not mainstream yet.)

**I/O-bound benchmark ✅ (real speed-up even on one core):** 20 tasks × 100 ms sleep
```
sequential 2.01 s | 10 threads 0.20 s (9.9x)
```
**CPU-bound benchmark: INCONCLUSIVE here ✅/⚠️:** 4 tasks × 3 M-iteration Python loops on **1 core**
```
sequential 1.46 s | 4 threads 1.53 s (0.96x) | 2 processes 1.52 s (0.96x)
```
Threads gave no gain (consistent with the GIL), but **processes didn't either, because there is only one core to run on**; the experiment cannot separate "GIL" from "no cores". On a multi-core machine you should expect ~N× for N processes and ~1× for threads. Run it yourself:
```python
with ProcessPoolExecutor(os.cpu_count()) as ex: list(ex.map(cpu_task, [CPU_N] * 4))      # expect ~min(4, cores)x faster than sequential
```
**asyncio + aiohttp ✅ (100 concurrent requests against a local server with 100 ms latency, concurrency capped at 50):**
```python
async def fetch_all(n, limit):
    sem = asyncio.Semaphore(limit)                                   # cap concurrency: don't DDoS the API / exhaust sockets
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as s:
        async def one(i):
            async with sem:
                try:
                    async with s.get(f"{BASE}/item/{i}") as r: return await r.json(content_type=None)
                except Exception as e: return {"error": type(e).__name__}
        return await asyncio.gather(*(one(i) for i in range(n)))
```
```
asyncio (concurrency 50): 1.55 s | sequential requests, 100 calls: ~10.3 s  (~6.6x faster) | successes 100/100
```
(The server and client share my single core, so 1.55 s is far from the ideal 0.2 s.) > **A real gotcha I hit:** my first run returned **0/100 successes**: my mock server omitted a `Content-Type: application/json` header, and `await r.json()` raises `ContentTypeError` in that case. I only noticed because the code *returned error dicts*, which is exactly why you count successes and failures instead of assuming. Use `r.json(content_type=None)` for sloppy servers, but better: fix the header / check `r.status`.

**Parallel file processor with per-file error isolation ✅** (12 CSVs of 2,000 rows each; `f05.csv` deliberately corrupted):
```python
def run_parallel(paths, workers=2):
    ok, failed = [], []
    with ProcessPoolExecutor(workers) as ex:
        futs = {ex.submit(process_file, p): p for p in paths}
        for fu in as_completed(futs):
            try: ok.append(fu.result())
            except Exception as e: failed.append({"file": os.path.basename(futs[fu]), "error": f"{type(e).__name__}: {e}"})
    return ok, failed
```
```
processed 11/12 files, 22,000 rows | failed (isolated): [{'file': 'f05.csv', 'error': "ValueError: could not convert string to float: 'not_a_number'"}]
```
The exception travels back through the future (`fu.result()` re-raises it), so one bad file doesn't take down the run. **Return small results from workers** (they're pickled between processes): write output files in the worker and return only a summary; make worker functions top-level (picklable); write each file to a temp name and rename on success to avoid half-written outputs.

---

## Part B — Practice Problems (13)

*Try each 15 min first. ✅ = solution code was run.*

### Easy (1–5)

**1. [Amazon] Chunk a list.** ✅ `chunks(items, 500)`; test the uneven tail (10,001 → `[500, 1]`).
**2. [Google] Find records in source but not target.** ✅ `src - tgt` (set difference); key on IDs for "missing" vs "changed".
**3. [Meta] Most common error types.** ✅ `Counter(types).most_common(2)`.
**4. [Netflix] Read a big CSV without loading it.** ✅ generator over `csv.DictReader` (0.05 MB vs 98.5 MB).
**5. [Uber] Group records by key without `if key in d`.** ✅ `defaultdict(list)`.

### Medium (6–10)

**6. [Stripe] CSV deduplication tool for 1M+ rows, memory-efficient** ✅
```python
def dedupe_csv(src, dst, key="id") -> Tuple[int, int]:
    seen = set(); kept = dropped = 0
    with open(src, newline="") as fi, open(dst, "w", newline="") as fo:
        r = csv.DictReader(fi); w = csv.DictWriter(fo, fieldnames=r.fieldnames); w.writeheader()
        for row in r:
            h = hashlib.blake2b(row[key].encode(), digest_size=8).digest()        # 8 bytes per key instead of the whole row/string
            if h in seen: dropped += 1; continue
            seen.add(h); w.writerow(row); kept += 1
    return kept, dropped
```
```
1,000,000 rows (27 MB) -> kept 571,121, dropped 428,879 in 20.0 s | peak Python memory 40 MB (the set of key hashes) | output ids unique: True
```
Only **keys** are held in memory, never rows. (20 s on one slow core includes `tracemalloc` overhead.) *Trade-offs:* an 8-byte hash can collide (two different ids treated as duplicates); with 570K keys the collision probability is ~10⁻⁸, but at billions of keys use a 16-byte digest or store full keys. *If even the key set won't fit:* sort by key externally (merge sort on disk) and drop adjacent duplicates, or hash-partition rows into K files and dedupe each one. *Keep-last* semantics need a dict `key → latest` (or a sort), not a set.

**7. [Airbnb] API ingestion with pagination, retry, and rate limiting** ✅ (full production client and results in Chapter 6 §A3: 95/95 records exactly once despite injected 500/429/401; token-bucket limiter verified: burst of 5 instant, then 10/s → 25 requests in 2.0 s). Core here:
```python
@retry(max_attempts=4, exceptions=(ConnectionError, TimeoutError))
def fetch(url, params): ...
for page in itertools.count(1):
    data = fetch(url, {"page": page}); 
    if not data: break
    yield from data; time.sleep(min_interval)               # polite pacing
```

**8. [Google] Log parser: structured data from mixed log formats** ✅ (try formats in order, return `None` for blank/invalid, tag everything else)
```
apache   -> {'ip': '203.0.113.9', 'method': 'GET', 'path': '/a?x=1', 'status': 502, 'bytes': '-', ...}
json     -> {'ts': '2024-03-10T13:55:37', 'level': 'error', 'msg': 'db timeout', 'svc': 'api'}
app      -> {'ts': '2024-03-10 13:55:38,123', 'level': 'ERROR', 'logger': 'app.db', 'msg': 'connection reset'}
kv       -> {'level': 'warn', 'svc': 'api', 'latency_ms': '950', 'path': '/x'}
weird    -> {'_fmt': 'unparsed', 'raw': 'totally weird line'}      blank / '{"broken json' -> skipped
format counts: {'apache':1, 'json':1, 'app':2, 'kv':1, 'unparsed':1, 'skip':2}
```
Notes: order matters (JSON check by leading `{`, regexes most-specific first); **keep unparsed lines** (count them; a rising unparsed rate means a format changed); the `app` parser also handled `WARNING` and a `T` separator; normalize `level` casing and types downstream.
- *Follow-up:* "10 GB of logs?" → generator over lines, compile regexes once, optionally `multiprocessing` by file/byte-range.

**9. [Amazon] Compare two table schemas** ✅
```python
def diff_schema(a: Dict[str, str], b: Dict[str, str]):
    return {"added": sorted(set(b) - set(a)), "removed": sorted(set(a) - set(b)),
            "type_changed": {c: (a[c], b[c]) for c in a.keys() & b.keys() if a[c] != b[c]},
            "breaking": bool((set(a) - set(b)) or any(a[c] != b[c] for c in a.keys() & b.keys()))}
diff_schema({"id": "bigint", "amt": "int", "old": "text"}, {"id": "bigint", "amt": "decimal", "new": "text"})
# {'added': ['new'], 'removed': ['old'], 'type_changed': {'amt': ('int', 'decimal')}, 'breaking': True}
```
Policy to state: **removed columns and type changes are breaking; added columns usually aren't** (consistent with Avro/Delta compat rules in Chapters 9 and 11). Possible extensions: widening vs narrowing type changes (`int → bigint` is safe), nullability, column order.

**10. [Meta] Data validation framework using decorators** ✅ (`@validate_schema` from A4: rejected `record 1: 'id' should be int, got str`). Extension: a registry of rules, `errors="collect"` mode returning *all* violations rather than raising on the first, and a quarantine output for bad records.

### Hard (11–13)

**11. [Amazon] Complete mini-ETL framework** ✅ (config-driven extract / transform / load with error handling, logging, metrics)
Registries map config `type` names to functions: extract `csv_src`/`json_src`; transform `rename`/`cast`/`filter_rows`/`join`; load `to_csv`/`to_parquet`/`to_json`. Each transform step is wrapped so **a bad row is rejected individually** instead of killing the run.
```python
cfg = {"extract": {"type": "csv_src", "text": "order_id,amt,cty\n1,10.5,IN\n2,abc,US\n3,-4,DE\n4,99,IN\n5,,FR\n"},
       "transform": [{"type": "rename", "mapping": {"amt": "amount", "cty": "country"}}, {"type": "cast", "types": {"order_id": "int", "amount": "float"}},
                     {"type": "filter_rows", "expr": "amount > 0"}, {"type": "join", "lookup": {"IN": {"region": "APAC"}, "US": {"region": "AMER"}}, "on": "country"}],
       "load": {"type": "to_parquet", "path": "out.parquet"}}
```
```
metrics: {'rows_read': 5, 'rows_filtered': 1, 'rows_rejected': 2, 'rows_written': 2}        <- 5 = 2 written + 2 rejected + 1 filtered: every row accounted for
   reject: cast {'order_id': '2', 'amount': 'abc', 'country': 'US'} -> ValueError: could not convert string to float: 'abc'
   reject: cast {'order_id': '5', 'amount': '',    'country': 'FR'} -> ValueError: could not convert string to float: ''
parquet: [{'order_id': 1, 'amount': 10.5, 'country': 'IN', 'region': 'APAC'}, {'order_id': 4, 'amount': 99.0, 'country': 'IN', 'region': 'APAC'}]
same framework, JSON source + JSONL sink: {'rows_read': 1, 'rows_written': 1}
```
> **Bug found while testing:** my first version reported `read 5, rejected 2, written 2`, so **one row (the filtered `-4`) vanished from the accounting**. I added a `rows_filtered` metric. A pipeline whose counts don't reconcile (`read = written + rejected + filtered`) is hiding data loss: assert that identity in production.
> **Security note:** the `filter_rows` step uses `eval` on a config string **for demonstration only**; never `eval` untrusted input. Prefer a small expression grammar, `pandas.query`, or SQL.

**12. [Google] Async scraper: 50 URLs concurrently, parse JSON, dedupe, validate schema, write Parquet in batches of 1000.** ✅ The building blocks were run: semaphore-bounded `aiohttp` fan-out (100/100 OK, ~6.6× faster than sequential), schema validation (A1), Parquet writing (A5). Skeleton:
```python
async def scrape(urls, batch=1000, limit=50):
    seen, buf, files = set(), [], 0
    sem = asyncio.Semaphore(limit)
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as s:
        async def one(u):
            async with sem:
                for attempt in range(3):                                           # retry transient errors with backoff
                    try:
                        async with s.get(u) as r:
                            if r.status >= 500: raise aiohttp.ClientError(r.status)
                            r.raise_for_status(); return await r.json()
                    except (aiohttp.ClientError, asyncio.TimeoutError): await asyncio.sleep(0.2 * 2 ** attempt)
            return None
        for coro in asyncio.as_completed([one(u) for u in urls]):                  # process results as they arrive (bounded memory)
            doc = await coro
            if not doc or validate_schema(doc, SCHEMA): continue                   # skip + count invalid
            if doc["id"] in seen: continue                                         # dedupe
            seen.add(doc["id"]); buf.append(doc)
            if len(buf) >= batch: pq.write_table(pa.Table.from_pylist(buf), f"out/part-{files:05d}.parquet"); files += 1; buf.clear()
    if buf: pq.write_table(pa.Table.from_pylist(buf), f"out/part-{files:05d}.parquet")
```
Points: cap concurrency; **timeouts**; retry only transient errors; respect `robots.txt`/rate limits/ToS; dedupe by a stable id (bounded `seen` set or Bloom filter at scale); batch Parquet writes (never one file per record); count and report skipped/invalid/failed.

**13. [Stripe] Real-time event processor: queue → configurable rules → anomaly detection (>3σ) → results + alerts** ✅
```python
class EventProcessor:
    def __init__(self, rules, window=50, sigma=3.0, min_history=20): ...
    def process(self, ev):
        for rule in self.rules:                                   # configurable transformation rules
            ev = rule(ev)
            if ev is None: return                                 # a rule can drop the event
        st = self.stats[ev["user"]]; m, sd = st.mean_std()        # per-user rolling window (deque)
        if m is not None and len(st.q) >= self.min_history and sd > 0 and abs(ev["amount"] - m) > self.sigma * sd:
            self.alerts.append({"event_id": ev["id"], "user": ev["user"], "amount": ev["amount"], "z": round((ev["amount"] - m) / sd, 1)})
        else: st.add(ev["amount"])                                # don't poison the baseline with detected anomalies
        self.out.append(ev)
```
300 simulated events (two users, amounts ~ N(50, 5)) with two injected 400s:
```
alerts: event 120 (z=73.0) ✔ injected | event 200 (z=80.4) ✔ injected
        event 78 (z=-3.3), 159 (z=3.0), 216 (z=-3.9)  <- NOT injected: ordinary noise crossing 3σ
```
> **Honest lesson:** both real anomalies were caught, but **3 of 5 alerts were false positives**. With a 50-sample window the σ estimate is itself noisy, and Gaussian noise crosses 3σ about 0.3% of the time (that's ~1 per 370 events *per user*, before counting the estimation error). Fixes to mention: higher `min_history`, **robust statistics** (median + MAD), a persistence rule (alert only if 2 of the last 3 events are extreme), per-segment baselines, and cost-based thresholds. Excluding detected anomalies from the baseline (done above) prevents a burst from raising the mean/σ and masking itself. Use **O(1) incremental updates** (Welford) rather than recomputing over the window, and keep the processor idempotent/checkpointed in production.

---

## Part C — Interview Simulations

### Q1 [Amazon]: "Design and implement a Python class for a pipeline that reads from S3, applies transformations, and writes to a database. It should support batch processing, error recovery, logging, and metrics. Walk through your design choices."

**Clarify:** file formats/sizes, idempotency requirement, DB target and volume, ordering needs, SLAs, schema drift, who handles reprocessing.
**Design (this is the A7 framework, extended):**
```python
class S3ToDbPipeline:
    def __init__(self, cfg: PipelineConfig, s3, db, steps: List[Step], log: logging.Logger, metrics: Metrics):
        self.cfg, self.s3, self.db, self.steps, self.log, self.m = cfg, s3, db, steps, log, metrics         # dependency injection -> testable with fakes
    def run(self, prefix: str) -> RunReport:
        for key in self._pending_keys(prefix):                      # state table: processed_files(key, etag, status) => resumable + idempotent
            try: self._process_file(key)
            except SourceUnavailableError: self._schedule_retry(key)
            except Exception: self._mark_failed(key); self.log.exception("file failed", extra={"key": key})   # one bad file doesn't stop the run
    def _process_file(self, key):
        batch = []
        for rec in self._read_stream(key):                          # streaming reader (generator), constant memory
            try: out = self._apply(rec)
            except DataValidationError as e: self._dead_letter(key, rec, e); self.m.inc("rejected"); continue
            batch.append(out)
            if len(batch) >= self.cfg.batch_size: self._write_batch(batch, key); batch.clear()
        if batch: self._write_batch(batch, key)
        self._mark_done(key)
    @retry(max_attempts=3, exceptions=(TransientDbError,))
    def _write_batch(self, batch, key):
        with self.db.transaction():                                 # batch commits atomically
            self.db.upsert(batch, key_cols=["id"])                  # idempotent: replays are harmless
```
**Design choices to narrate:**
1. **Streaming + batching:** generators for reading, fixed-size batches for DB writes (balances round trips vs transaction size/memory).
2. **Idempotency:** upsert by key (see Chapter 6's verified `ON CONFLICT … WHERE IS DISTINCT FROM`) + a processed-files state table (key + ETag), so reruns and crashes are safe.
3. **Error recovery tiers:** transient (retry with backoff) → bad record (dead-letter with file/line) → bad file (mark failed, continue, alert) → systemic (fail the run, e.g. `SchemaError` or >X% rejected).
4. **Observability:** JSON logs with `run_id`/`key`; metrics `read = written + rejected + filtered` (assert it); duration; lag.
5. **Testability:** inject `s3`, `db`, `steps`; test with in-memory fakes and a mock S3 (e.g. `moto`).
6. **Scale-out:** the unit of parallelism is the file (thread pool for I/O-bound S3 reads, process pool for CPU-heavy transforms, per A8), with a bounded worker count and DB connection pool sized accordingly (Chapter 10).
⚠️ *Not run against real S3/DB; the components (streaming, batching, retry decorator, dead-letter, per-file isolation, upsert) were each verified separately above and in Chapter 6.*

| Evaluator is looking for | Green flag | Red flag |
|---|---|---|
| Structure | ABCs, dependency injection, small steps | One 300-line function |
| Failure handling | Tiered recovery, dead letters, per-file isolation | try/except around everything, or none |
| Correctness | Idempotent writes, reconciled metrics | Duplicates on retry, silent drops |
| Operability | Structured logs, metrics, resumability | `print` statements |

### Q2 [Meta]: "You have 1 TB of JSON files. Write Python that processes them efficiently on one machine. Explain memory management, parallelization, and error handling."

**Clarify:** JSONL or giant arrays? compressed? file size distribution? what's the output (aggregate, filtered subset, columnar conversion)? machine specs (cores/RAM/disk)? is exact-once required?
**Strategy (design; ⚠️ not run at 1 TB, but each mechanism was measured at small scale above):**
```python
def process_file(path: str, out_dir: str) -> dict:
    rows = bad = 0
    tmp = f"{out_dir}/{os.path.basename(path)}.parquet.tmp"
    writer = None
    with gzip.open(path, "rt") if path.endswith(".gz") else open(path) as f:       # STREAM: one line at a time
        batch = []
        for ln, line in enumerate(f):
            try: batch.append(flatten(json.loads(line)))
            except (json.JSONDecodeError, KeyError): bad += 1; dead_letter(path, ln, line); continue
            if len(batch) >= 50_000:                                               # bounded memory: write row groups incrementally
                t = pa.Table.from_pylist(batch, schema=SCHEMA); writer = writer or pq.ParquetWriter(tmp, SCHEMA, compression="zstd"); writer.write_table(t); rows += len(batch); batch.clear()
        if batch: ...flush...
    if writer: writer.close(); os.replace(tmp, tmp[:-4])                           # atomic rename = file is either complete or absent
    return {"file": path, "rows": rows, "bad": bad}
with ProcessPoolExecutor(os.cpu_count()) as ex:                                     # CPU-bound JSON parsing -> processes
    for fu in as_completed(ex.submit(process_file, p, OUT) for p in todo): record(fu)   # per-file isolation; failures logged, run continues
```
**Narrate:**
- **Memory:** never `json.load` a big file; stream line by line (JSONL), or `ijson` for a giant array; cap batch size; **return summaries** from workers, not data. Memory ≈ `workers × (batch rows + parser overhead)`: size `workers` and `batch` from available RAM.
- **Parallelism:** the **file is the unit of work**; JSON parsing is CPU-bound pure Python → **processes** (threads would be GIL-bound ✅ theory; I couldn't measure the speedup on one core). Faster parsers: `orjson` (several × faster than `json`), `pyarrow.json.read_json` (multithreaded, columnar) when records share a schema, or DuckDB/polars `read_ndjson` (which would likely beat hand-rolled Python for this task). Disk I/O often becomes the bottleneck: monitor, and don't over-subscribe workers.
- **Throughput sanity check:** 1 TB at, say, 100 MB/s per core effective ≈ 10,000 core-seconds ≈ 2.8 core-hours. On 8 cores ≈ 35 min **if** I/O keeps up. Say the arithmetic; state it's an estimate to validate on a 1 GB sample.
- **Error handling:** bad lines → dead-letter file with path/line/error (not fatal); bad files → recorded, continue; systemic failure (schema drift > threshold) → abort. **Resumability:** a manifest of completed files (output exists = done; `os.replace` makes partial outputs impossible); safe to rerun.
- **Output design:** Parquet (zstd), partitioned or sized ~128–512 MB per file, with an explicit schema (types must not drift between files).
- **Verification:** reconcile `lines_in = rows_out + bad`, compare a sample against a reference parse.

| Evaluator is looking for | Green flag | Red flag |
|---|---|---|
| Memory strategy | Streaming, bounded batches, workers × batch sizing | `json.load` everything / pandas `read_json` on the whole file |
| Parallelism | Processes for CPU parse, file-level unit, summaries returned | Threads for CPU-bound work with no justification |
| Reliability | Atomic outputs, manifest, dead letters, reconciliation | No resume story |
| Estimation | Back-of-envelope throughput, validation on a sample | "It'll just work" |

---

## Chapter 5 Cheat Sheet

- **Sets** for dedup/EXCEPT (O(1)); **deque** for windows; **heapq** for top-K (12.6 ms vs 57 ms ✅); **Counter/defaultdict** for counting/grouping.
- **Generators** = constant memory (0.05 MB vs 98.5 MB ✅); they're single-use ✅; `groupby` needs sorted input.
- Decorators: always `functools.wraps`; **retry only transient exceptions** and only idempotent work ✅; `lru_cache` for pure lookups.
- Context managers guarantee cleanup; `__exit__` returning truthy swallows exceptions.
- Errors: **tiered** (transient → retry, bad record → dead-letter, fatal → raise); structured JSON logs with `run_id` ✅; `LoggerAdapter` drops call-level `extra` unless you merge it ✅; reconcile `read = written + rejected + filtered` ✅.
- OOP: dataclasses (frozen config, `default_factory`), ABCs for interfaces, composition + dependency injection, steps as generators ✅.
- Concurrency: I/O-bound → threads/asyncio (9.9× ✅, ~6.6× ✅); CPU-bound → processes (needs multiple cores; not demonstrable on my 1-core sandbox); the GIL blocks CPU-bound threads; cap concurrency with a semaphore; check every response (`ContentTypeError` bit me ✅).
- Memory-efficient dedup: hash the keys, never hold rows (40 MB for 571K keys ✅).
- Anomaly detection: 3σ on small windows yields false positives ✅; use robust stats, persistence, and tune.

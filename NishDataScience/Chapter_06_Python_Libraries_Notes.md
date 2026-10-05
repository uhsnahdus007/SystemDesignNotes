# Chapter 6: Python Libraries Every DE Must Know

> **Verification status.** Everything marked ✅ **RAN** was executed with **pandas 3.0.2, polars 1.44.2, SQLAlchemy 2.x, pydantic 2.x, PyArrow 25, fastavro, Jinja2, requests**, a **real PostgreSQL 16** server, and a **local mock HTTP API** I wrote (so the retry/pagination/OAuth code ran against genuinely failing responses). Output is real.
> - **pandas 3.0 caveat:** my environment has pandas 3.x. Behaviour differs from pandas 1.x/2.x in places (string columns now show dtype `str`, default datetime resolution is `us`, copy-on-write is always on). Your interview/production environment may be pandas 2.x; I note where this matters.
> - **Not run:** real third-party APIs (no outside network), MySQL, Oracle. A requests timeout demo to an unroutable IP did not behave as expected in my sandbox (an egress proxy answered), so I'm showing the *code* only.
> - Dataset: an **11-row messy e-commerce CSV** (not 100K rows; "100K+ conceptual" as in the prompt) with duplicates, NULLs, `$`/`,` in numbers, mixed date formats, inconsistent case/whitespace, and an unparseable date. Benchmarks use synthetic 200K-3M row frames.

---

## Part A — Concepts + Code

### A0. The messy dataset

```python
import io, pandas as pd
csv = """order_id,customer_id,email,order_date,amount,status,country,product
1001,C1, Asha@Example.com ,2024-03-01,120.50,PAID,IN,laptop
1002,C2,bob@example.com,03/02/2024,$75.00,paid,us,mouse
1003,C1,asha@example.com,2024-03-05,,Shipped,IN,keyboard
1004,C3,CHEN@example.com,2024-03-07,310,PAID,DE,monitor
1004,C3,CHEN@example.com,2024-03-07,310,PAID,DE,monitor
1005,C4,,2024-03-09,45.25,cancelled,FR,mouse
1006,C2,bob@example.com,2024-03-10,"1,200.00",PAID,US,laptop
1007,C5,eli@example.com,not_a_date,60,Paid,in,keyboard
1008,C6,fay@example.com,2024-03-12,80,refunded,NO,mouse
1009,,gus@example.com,2024-03-13,95,PAID,IN,monitor
1010,C1,asha@example.com,2024-03-14,15.00,PAID,IN,cable
"""
raw = pd.read_csv(io.StringIO(csv), dtype=str)        # read EVERYTHING as str first: never let pandas guess types on dirty data
```
**Issues planted:** exact duplicate (1004); whitespace/case in email; `$75.00` and `"1,200.00"` amounts; `03/02/2024` vs ISO dates; `not_a_date`; NULL amount, email, customer_id; inconsistent `status`/`country` case.

### A1. pandas  ✅ RAN

#### Exploration
```python
raw.shape            # (11, 8)
raw.nunique()        # {'order_id': 10, 'customer_id': 6, 'email': 7, 'order_date': 10, 'amount': 9, 'status': 6, 'country': 7, 'product': 5}
raw.isna().sum()[lambda s: s > 0]    # {'customer_id': 1, 'email': 1, 'amount': 1}
```
`.head() .info() .describe() .dtypes .shape .columns .nunique()` are your first calls on any new dataset. `status` has **6 distinct values for 4 real statuses**: `nunique` exposes case problems immediately.

#### Cleaning: one chained, readable pipeline
```python
clean = (raw.drop_duplicates()                                                      # exact duplicate row (1004)
    .assign(email=lambda d: d.email.str.strip().str.lower(),
            status=lambda d: d.status.str.lower(),
            country=lambda d: d.country.str.upper(),
            amount=lambda d: pd.to_numeric(d.amount.str.replace(r"[$,]", "", regex=True), errors="coerce"),
            order_date=lambda d: pd.to_datetime(d.order_date, format="mixed", errors="coerce", dayfirst=False))
    .assign(email_domain=lambda d: d.email.str.extract(r"@(.+)$")[0],
            year_month=lambda d: d.order_date.dt.to_period("M").astype(str)))
```
```
 order_id customer_id             email order_date   amount     status country   product year_month
     1001          C1  asha@example.com 2024-03-01   120.50       paid      IN    laptop    2024-03
     1002          C2   bob@example.com 2024-03-02    75.00       paid      US     mouse    2024-03   <- "03/02/2024" read as MARCH 2
     1003          C1  asha@example.com 2024-03-05      NaN    shipped      IN  keyboard    2024-03
     1004          C3  chen@example.com 2024-03-07   310.00       paid      DE   monitor    2024-03
     1005          C4               NaN 2024-03-09    45.25  cancelled      FR     mouse    2024-03
     1006          C2   bob@example.com 2024-03-10  1200.00       paid      US    laptop    2024-03   <- "$"/"," stripped
     1007          C5   eli@example.com        NaT    60.00       paid      IN  keyboard        NaN   <- bad date -> NaT, not a crash
 ...
rows dropped as duplicates: 1 | unparseable dates: 1 | NULL amounts: 1 | missing customer_id: 1
```
> **COMMON MISTAKES:** (1) `errors="coerce"` turns bad values into NULL **silently**: always count NULLs before/after (as above) so you never lose data unnoticed. (2) **Ambiguous dates:** `03/02/2024` is March 2 (US) or 3 February (EU); `dayfirst` must be a *documented decision*, never a default. Prefer ISO-8601 at the source. (3) Float for money: use `Decimal`/integer cents in financial pipelines. (4) Chained indexing (`df[df.a>1]["b"] = 0`) silently fails to modify; use `.loc[mask, "b"] = 0`.

**Null handling:** `fillna(0)` keeps 10 rows (and treats missing as zero revenue!), `dropna(subset=["amount"])` keeps 9; group-wise fill: `d.amount.fillna(d.groupby("country").amount.transform("median"))`. Pick per business meaning; never default to `fillna(0)`.
**`.replace()` / `.map()`:** `replace` for sparse value fixes, `map` (dict/Series) for full lookups (unmapped → NaN, so test `.isna()` after).

#### Selection
```python
clean.loc[clean.status.eq("paid") & (clean.amount > 100), ["order_id", "amount"]]   # label-based + boolean mask: use & | ~ with parentheses
clean.iloc[0:2, 0:2]                                                                 # position-based
clean.query("country == 'IN' and amount.notna()").order_id.tolist()                  # ['1001','1007','1009','1010']
```

#### groupby + named aggregation
```python
(clean.dropna(subset=["amount"]).groupby("country")
      .agg(orders=("order_id", "nunique"), revenue=("amount", "sum"), avg_amount=("amount", "mean"))
      .round(2).sort_values("revenue", ascending=False))
```
```
         orders  revenue  avg_amount
country
US            2  1275.00      637.50
DE            1   310.00      310.00
IN            4   290.50       72.62
NO            1    80.00       80.00
FR            1    45.25       45.25
```
(`groupby` drops NULL keys by default; use `dropna=False` to keep them.)

#### merge: every join type, `indicator`, and the many-to-many trap
```
inner: 5 rows | left: 6 | right: 6 | outer: 7
outer with indicator=True -> {'both': 5, 'left_only': 1, 'right_only': 1}     <- left_only = orphan orders; right_only = customers with no orders
many-to-many danger: 2 orders x a customer table with a duplicate key -> 4 rows (row explosion)
validate='m:1'  -> MergeError raised (catches it before it corrupts totals)
```
`on=` (same name) vs `left_on=/right_on=` (different names); `suffixes=` for clashing columns. **Always** check row counts after a merge, use `indicator=True` to audit, and pass `validate=` ("1:1", "m:1", "1:m") to turn silent explosions into errors.

#### concat, pivot_table, melt
```python
pd.concat([a, b], ignore_index=True)            # stack rows (axis=0) or columns (axis=1); 5 rows in the test
clean.pivot_table(index="country", columns="status", values="amount", aggfunc="sum", fill_value=0)   # long -> wide
pv.reset_index().melt(id_vars="country", var_name="status", value_name="amount")                     # wide -> long
```
```
status   cancelled    paid  refunded
country
DE            0.00   310.0       0.0
FR           45.25     0.0       0.0
IN            0.00   290.5       0.0
NO            0.00     0.0      80.0
US            0.00  1275.0       0.0
```

#### Performance: vectorized vs `apply` vs `iterrows`  ✅ RAN (200,000 rows)
```
vectorized      :       3.3 ms        big.a * big.b + 1
apply(axis=1)   :    3082.8 ms   (929x slower)
iterrows (20k rows only; x10 projected): 830.2 ms -> ~8,302 ms for 200k (2,502x slower)
same answers: True
```
> **KEY INSIGHT:** `apply(axis=1)` and `iterrows` run a **Python function per row**. Vectorized ops run in compiled code over whole columns. The gap here was ~900-2,500×. Use `np.where`/`np.select`, `.str`/`.dt` accessors, `merge`, `groupby().transform()`, and boolean masks first; `apply` last.

**Memory: `category` dtype + downcast ✅ RAN** (500K rows): `13.0 MB → 3.0 MB`
```python
m.assign(country=m.country.astype("category"), qty=pd.to_numeric(m.qty, downcast="unsigned"), price=m.price.astype("float32"))
# dtypes: country=category, qty=uint8, price=float32
```
(Check precision before downcasting money/ids to float32.)
**Chunked reading ✅ RAN:** `for ch in pd.read_csv(path, chunksize=50_000): total += ch.a.sum()` processed 200,000 rows in 50K chunks; the running total equalled the full-load sum. Use it when the file doesn't fit in memory (aggregate per chunk, keep only partial results).

#### I/O cheat sheet
```python
pd.read_csv(path, encoding="utf-8", dtype=str, na_values=["NA", ""], quoting=csv.QUOTE_MINIMAL, compression="infer")   # .gz/.zip auto-detected
df.to_csv("o.csv.gz", index=False, compression="gzip")
pd.read_parquet(path, engine="pyarrow", columns=["a", "b"]); df.to_parquet(path, engine="pyarrow", compression="zstd", index=False)
pd.read_json(path, lines=True)  /  df.to_json("o.jsonl", orient="records", lines=True)       # orient: records | split | index | columns | values | table
pd.read_sql(text("SELECT ..."), engine, params={...});  df.to_sql("tbl", engine, if_exists="append", index=False)   # see A4
```

---

### A2. polars  ✅ RAN

**Why polars:** Rust-based columnar engine, **multithreaded** (no GIL for most work), **lazy evaluation with a query optimizer**, strict types, expression API. Often several-fold faster on large aggregations/joins and uses less memory. pandas still wins on ecosystem breadth and familiarity.

Same logic as the pandas pipeline:
```python
out = (df.with_columns(pl.col("status").str.to_lowercase(), pl.col("country").str.to_uppercase(), pl.col("d").str.to_date("%Y-%m-%d"),
                       pl.when(pl.col("amount") > 100).then(pl.lit("big")).otherwise(pl.lit("small")).alias("size"))
         .filter(pl.col("amount").is_not_null())
         .group_by("country").agg(pl.len().alias("orders"), pl.col("amount").sum().alias("revenue"), pl.col("amount").mean().round(2).alias("avg"))
         .sort("revenue", descending=True))
```
```
┌─────────┬────────┬─────────┬───────┐
│ country ┆ orders ┆ revenue ┆ avg   │
│ US      ┆ 2      ┆ 1275.0  ┆ 637.5 │
│ DE      ┆ 1      ┆ 310.0   ┆ 310.0 │
│ IN      ┆ 1      ┆ 120.5   ┆ 120.5 │
│ FR      ┆ 1      ┆ 45.25   ┆ 45.25 │
```
joins: `{'inner': 5, 'left': 6, 'full': 7, 'anti': 1}` (`how="anti"` / `"semi"` built in).

| pandas | polars |
|---|---|
| `df[df.a > 1]` | `df.filter(pl.col("a") > 1)` |
| `df.assign(b=df.a*2)` | `df.with_columns((pl.col("a")*2).alias("b"))` |
| `groupby().agg(x=("a","sum"))` | `group_by().agg(pl.col("a").sum().alias("x"))` |
| `np.where(c, x, y)` | `pl.when(c).then(x).otherwise(y)` |
| `merge(how="outer")` | `join(how="full")` |
| index-based (`.loc`) | no index; everything is expressions on columns |

**Lazy evaluation ✅ RAN:** `.lazy()` builds a plan; `.collect()` runs it after optimization. Real plans for `filter(a > 5).select(a, b)`:
```
optimized:   FILTER col("a") > 5                          unoptimized:  SELECT [col("a"), col("b")]
             FROM DF ["a","b","c"]; PROJECT["a","b"] 2/3 COLUMNS         FILTER col("a") > 5
                                                                         FROM DF ["a","b","c"]; PROJECT */3 COLUMNS
```
The optimizer **pruned column `c`** (projection pushdown) and pushed the filter to the scan (predicate pushdown), the same ideas as Spark's Catalyst.

**Benchmark ✅ RAN** (3 M rows, **1 CPU core** in my sandbox: filter → join → group_by with 3 aggregations; best of 3):
```
pandas : 773 ms
polars : 272 ms   (2.8x faster)   results equal: True
string op (upper-case 3M strings): pandas 33 ms vs polars 82 ms
```
> **Honest reading:** polars was ~2.8× faster on the join/aggregate pipeline on this **single-core** VM, so that gain came from more efficient algorithms and memory layout, **not parallelism** (on multi-core machines the gap typically grows further), but pandas 3's Arrow-backed string dtype was **faster** on a simple string operation, so "polars is always faster" is false. Benchmark *your* workload. Also: lazy `scan_parquet(...).filter(...).collect()` can avoid loading unneeded data entirely, which is where polars shines for files larger than RAM (use the streaming engine).

---

### A3. requests + API Ingestion  ✅ RAN (against my local mock server)

**Basics:**
```python
s = requests.Session()                                    # reuses TCP/TLS connections: always use a Session for many calls
r = s.get(url, params={"page": 1}, headers={"Authorization": f"Bearer {token}"}, timeout=(3.05, 10))   # (connect, read); requests has NO default timeout!
r.raise_for_status();  r.json()
s.post(url, json={...});  s.put(url, json={...})          # json= sets the body and Content-Type
```
**Status handling:** `2xx` ok · `401` refresh credentials · `429` rate-limited (honor `Retry-After`) · `5xx` transient (retry with backoff) · other `4xx` your bug (**do not retry**).

**Pagination styles:** **offset/limit** (simple; can skip/duplicate rows if data changes mid-crawl), **cursor/token** (stable; follow `next_cursor` until `None`), **page-number** (like offset). Prefer cursor when offered, and use an `updated_since` filter for incrementals.

**The complete ingestion client** (the test server injected: a 500, a 429 with `Retry-After: 1`, another 500, then a server-side token invalidation causing a 401, over 95 records):
```python
class ApiClient:
    def __init__(self, base, client_id, secret, min_interval=0.0, max_retries=5, timeout=(3.05, 10)):
        self.base, self.cid, self.secret = base, client_id, secret
        self.min_interval, self.max_retries, self.timeout = min_interval, max_retries, timeout
        self.s = requests.Session(); self._last = 0.0; self.token = None; self.retries = 0; self.refreshes = 0
    def _refresh(self):                                                    # OAuth2 client-credentials
        r = self.s.post(f"{self.base}/oauth/token", data={"client_id": self.cid, "client_secret": self.secret, "grant_type": "client_credentials"}, timeout=self.timeout)
        r.raise_for_status(); self.token = r.json()["access_token"]; self.refreshes += 1
    def get(self, path, **params):
        if not self.token: self._refresh()
        for attempt in range(self.max_retries + 1):
            wait = self.min_interval - (time.monotonic() - self._last)      # client-side rate limit
            if wait > 0: time.sleep(wait)
            self._last = time.monotonic()
            try: r = self.s.get(f"{self.base}{path}", params=params, headers={"Authorization": f"Bearer {self.token}"}, timeout=self.timeout)
            except (requests.ConnectionError, requests.Timeout) as e:
                delay = min(2 ** attempt, 30) * (0.5 + random.random() / 2); self.retries += 1; time.sleep(delay); continue
            if r.status_code == 401 and attempt < self.max_retries: self._refresh(); continue              # token expired
            if r.status_code == 429: time.sleep(float(r.headers.get("Retry-After", 2 ** attempt))); self.retries += 1; continue
            if r.status_code >= 500 and attempt < self.max_retries:                                       # transient: exponential backoff + jitter
                time.sleep(min(0.05 * 2 ** attempt, 30) * (0.5 + random.random() / 2)); self.retries += 1; continue
            r.raise_for_status()                                            # other 4xx: fail loudly, no retry
            return r.json()
        raise RuntimeError(f"exhausted retries for {path}")

def paginate_offset(c, path, limit=20, **p):
    off = 0
    while True:
        page = c.get(path, offset=off, limit=limit, **p); yield from page["data"]; off += limit
        if off >= page["total"] or not page["data"]: return
def paginate_cursor(c, path, **p):
    cur = "0"
    while cur is not None:
        page = c.get(path, cursor=cur, **p); yield from page["data"]; cur = page["next_cursor"]
```
**Incremental sync with a watermark, writing JSONL as it goes:**
```python
def sync(c, state_file, out_file):
    since = json.load(open(state_file))["last_synced"] if os.path.exists(state_file) else 0
    n, max_ts = 0, since
    with open(out_file, "a") as f:
        for rec in paginate_offset(c, "/orders", since=since):
            f.write(json.dumps(rec) + "\n"); n += 1; max_ts = max(max_ts, rec["updated_at"])
    json.dump({"last_synced": max_ts}, open(state_file, "w")); return n          # advance the watermark ONLY after a successful run
```
**Real results:**
```
WARNING 500 server error; backoff 0.03s (attempt 1)
WARNING 429 rate limited; honoring Retry-After=1.0s
WARNING 500 server error; backoff 0.04s (attempt 1)
WARNING 401 -> refreshing token
records written: 95 | unique ids: 95 | retries: 3 | token refreshes: 2 | server calls: 9
incremental run, nothing new      -> 0 new records
3 new records appear upstream     -> 3 new records | total lines: 98
cursor pagination                 -> 98 events fetched (4 pages of 25 with the last page 23; last cursor None)
non-retryable 4xx (404)           -> HTTPError raised immediately, no retries
```
All **95 records arrived exactly once** despite 4 injected failures, and the incremental runs fetched only new data.
> **Design notes:** (1) **Exponential backoff + jitter** avoids synchronized retry storms; honor `Retry-After` over your own schedule. (2) **Retry only idempotent requests** (GET; POSTs need idempotency keys). (3) The watermark advances after the write, so a crash re-fetches (at-least-once); make the sink **idempotent** (upsert by id, A4) so replays are harmless. (4) Offset pagination plus concurrent upstream writes can skip/duplicate rows; for exactness use cursors or `updated_since` with a small overlap and dedupe. (5) Never log tokens; keep secrets in a secret manager/env vars. (6) Token refresh: preemptively refresh before `expires_in`, and on 401 as a fallback (both shown).

---

### A4. SQLAlchemy  ✅ RAN (PostgreSQL 16, SQLite dialect)

```python
engine = create_engine("postgresql+psycopg2://user:pw@host:5432/db", pool_size=5, max_overflow=2, pool_pre_ping=True)
create_engine("mysql+pymysql://user:pw@host/db");  create_engine("sqlite:///file.db")           # same API, different dialects (MySQL not run)
```
`pool_size` = persistent connections; `max_overflow` = extra temporary ones under bursts (details and exhaustion behaviour measured in Chapter 10: 33× faster than reconnecting per query).

```python
with engine.begin() as conn:            # a TRANSACTION: commits on success, rolls back on exception
    conn.execute(text("INSERT INTO orders (order_id, customer_id, amount, status) VALUES (:i, :c, :a, :s)"),
                 [{"i": 1, "c": "C1", "a": 100, "s": "new"}, {"i": 2, "c": "C2", "a": 50, "s": "new"}])    # list of dicts = executemany
pd.read_sql(text("SELECT order_id, amount, status FROM orders ORDER BY 1"), engine)
orders = Table("orders", MetaData(), autoload_with=engine)                                       # reflect an existing table
with engine.connect() as conn: conn.execute(select(orders.c.order_id, orders.c.amount).where(orders.c.amount > 60)).fetchall()   # Core expression language
```
Rollback proof: `UPDATE orders SET status='x'` followed by an exception inside `engine.begin()` left statuses as `['new']`. (`engine.execute()` no longer exists in SQLAlchemy 2.x: use `with engine.connect()`; always **bind parameters** (`:name`), never f-string values.)

**`to_sql` options ✅ RAN** (5,000 rows): default 136 ms, `method="multi", chunksize=1000` **508 ms**. Conventional wisdom says `multi` is faster; with psycopg2 + SQLAlchemy 2.x the default already uses efficient batched `executemany`, so **`multi` was slower here**. Measure. For real bulk loads on Postgres use `COPY` (`cursor.copy_expert`) which is typically far faster than either. `if_exists="append"` adds rows, `"replace"` **drops and recreates the table (losing constraints and indexes)**, `"fail"` raises.

**A complete database loader: idempotent upsert of a DataFrame into PostgreSQL** ✅ RAN:
```python
def upsert_df(df, table, engine, key, chunk=1000) -> int:
    """INSERT ... ON CONFLICT (key) DO UPDATE ... WHERE <row actually changed>; returns rows inserted+updated."""
    recs = df.astype(object).where(df.notna(), None).to_dict("records")       # NaN -> None so Postgres gets NULL
    value_cols = [c for c in df.columns if c != key]; affected = 0
    with engine.begin() as conn:                                              # one transaction: all chunks commit or none
        for i in range(0, len(recs), chunk):
            stmt = pg_insert(table).values(recs[i:i + chunk])
            changed = " OR ".join(f'"{table.name}"."{c}" IS DISTINCT FROM excluded."{c}"' for c in value_cols)
            stmt = stmt.on_conflict_do_update(index_elements=[key], set_={c: stmt.excluded[c] for c in value_cols}, where=text(changed))
            affected += conn.execute(stmt).rowcount
    return affected
```
```
first run  : rows affected = 3   (1 update [order 2: 50→55, new→paid] + 2 inserts)
same again : rows affected = 0   (nothing changed → nothing written; idempotent)
one changed: rows affected = 1
 order_id customer_id  amount status
        1          C1   100.0    new
        2          C2    55.0   paid
        3          C3    80.0    new
        4          C4    20.0    NaN       <- None stored as SQL NULL
```
`IS DISTINCT FROM` (NULL-safe) in the `WHERE` skips no-op updates (no dead tuples, no write amplification, `updated_at` triggers don't fire). Needs a UNIQUE/PK on the key. Other engines: MySQL `ON DUPLICATE KEY UPDATE`; SQLite `ON CONFLICT`; generic fallback = load to a staging table + `MERGE`.

---

### A5. pydantic  ✅ RAN

```python
class PipelineConfig(BaseModel):
    source: str = Field(pattern=r"^(s3|gs|file)://")
    target_table: str = Field(pattern=r"^[a-z_][a-z0-9_]*\.[a-z_][a-z0-9_]*$")
    batch_size: int = Field(1000, ge=1, le=100_000)
    mode: Literal["append", "overwrite", "merge"] = "append"
    merge_keys: list[str] = []
    @model_validator(mode="after")
    def merge_needs_keys(self):
        if self.mode == "merge" and not self.merge_keys: raise ValueError("mode='merge' requires merge_keys")
        return self
```
```
PipelineConfig(source="ftp://x", target_table="Bad Table", batch_size=0, mode="merge")
config errors: [('source','string_pattern_mismatch'), ('target_table','string_pattern_mismatch'), ('batch_size','greater_than_equal')]
valid: {'source': 's3://b/p', 'target_table': 'sales.orders', 'batch_size': 1000, 'mode': 'merge', 'merge_keys': ['order_id']}
```
(All field errors are reported *together*; the cross-field `merge_keys` check fires only after the field-level checks pass, which is why it isn't listed.)

**Record validator** with normalizers; bad rows are collected, not fatal:
```python
class OrderRow(BaseModel):
    order_id: int; email: Optional[str] = None; amount: float = Field(ge=0); order_date: date
    status: Literal["paid", "shipped", "cancelled", "refunded"]
    @field_validator("email", mode="before")
    @classmethod
    def norm_email(cls, v): return v.strip().lower() if isinstance(v, str) and v.strip() else None       # "" -> None
    @field_validator("status", mode="before")
    @classmethod
    def norm_status(cls, v): return v.strip().lower() if isinstance(v, str) else v
```
```
3 rows in: {"order_id":"1", email:" A@X.com ", amount:"10.5", date:"2024-03-01", status:"PAID"} -> OK
            {"order_id":"two", amount:-5, order_date:"2024-13-45", status:"lost"}                 -> REJECTED
            {"order_id":3, amount:5, order_date:"2024-03-03", status:"Shipped", email:""}         -> OK
validated: 2 good | 1 bad -> [(1, [('order_id','int_parsing'), ('amount','greater_than_equal'), ('order_date','date_from_datetime_parsing'), ('status','literal_error')])]
coerced: {'order_id': 1, 'email': 'a@x.com', 'amount': 10.5, 'order_date': '2024-03-01', 'status': 'paid'}      <- strings coerced to int/float/date
round trip: model_validate_json(model.model_dump_json()) == model  -> True
```
`.model_dump()` / `.model_dump(mode="json")` / `.model_validate()` / `.model_validate_json()` cover (de)serialization. **Speed note:** pydantic v2 (Rust core) validates fast, but for tens of millions of rows validate in **columnar** form (pandas/polars/`pandera`/SQL checks), not per-row model objects.

---

### A6. PyArrow & File Formats  ✅ RAN

```python
schema = pa.schema([pa.field("order_id", pa.int64(), nullable=False), pa.field("amount", pa.decimal128(12, 2)),
                    pa.field("country", pa.string()), pa.field("order_date", pa.date32())])
pq.write_table(tbl, "o.parquet", row_group_size=50_000, compression="zstd")
md = pq.ParquetFile("o.parquet").metadata
```
```
rows=200,000 row_groups=4 | schema=[int64, decimal128(12, 2), string, date32[day]]
row_group0 col order_id: min=0 max=49999     (statistics let readers skip row groups: Chapter 10)
arrow -> pandas -> arrow roundtrip equal: True
write_to_dataset(partition_cols=["country"]) -> ['country=DE', 'country=IN', 'country=US']
dataset filter country=US -> 66,924 rows   (only the US directory is scanned)
avro roundtrip (fastavro, nullable union ["null","string"]): True
```
```python
pq.write_to_dataset(tbl, root_path="part", partition_cols=["country"])
ds.dataset("part", format="parquet", partitioning="hive").to_table(filter=ds.field("country") == "US")
tbl.to_pandas()   # numeric columns convert zero-copy when there are no nulls; strings/decimals need copies
```
**Why Arrow:** a standard in-memory columnar layout shared by pandas, polars, Spark (Arrow-based pandas UDFs), DuckDB, so moving data between them avoids serialization. **Always give an explicit schema** when writing Parquet in pipelines (otherwise types drift between batches: e.g. an all-NULL batch infers `null` type and breaks later reads). Storing money as `decimal128(12,2)` avoids float error.

---

### A7. Jinja2 for SQL Templating  ✅ RAN

```python
env = Environment(undefined=StrictUndefined, trim_blocks=True, lstrip_blocks=True)       # StrictUndefined: a missing variable is an ERROR, not ''
tpl = env.from_string("""SELECT
{% for col in columns %}
    {{ col }}{{ "," if not loop.last }}
{% endfor %}
FROM {{ table }}
WHERE order_date >= '{{ start_date }}'
{% if status %}  AND status = '{{ status }}'
{% endif %}{% if countries %}  AND country IN ({% for c in countries %}'{{ c }}'{{ ", " if not loop.last }}{% endfor %})
{% endif %}""")
```
```
render(columns=[order_id, amount], status="paid", countries=["IN","US"]):
SELECT
    order_id,
    amount
FROM orders
WHERE order_date >= '2024-03-01'
  AND status = 'paid'
  AND country IN ('IN', 'US')

filters omitted -> SELECT order_id FROM orders WHERE order_date >= '2024-03-01'
StrictUndefined caught a missing variable: UndefinedError 'start_date' is undefined
```
> **⚠️ SQL INJECTION (demonstrated):** Jinja does **plain string substitution**. With `status = "x' OR '1'='1"` the rendered SQL ended `... AND status = 'x' OR '1'='1'`, which matches every row. **Use Jinja only for *structure*** (table/column names from an allow-list, optional clauses, loops), and pass **values through bind parameters**:
```python
ALLOWED_TABLES = {"orders"}
def run_templated(sql_tpl: str, params: dict, binds: dict, engine):
    sql = env.from_string(sql_tpl).render(**params)                 # structure only
    return pd.read_sql(text(sql), engine, params=binds)             # values bound safely by the driver
run_templated("SELECT order_id, status FROM {{ table }} WHERE amount >= :min_amt AND status = :st ORDER BY 1",
              {"table": "orders"}, {"min_amt": 20, "st": "paid"}, engine)      # -> [[2, 'paid']]
```
**Templated pipeline runner pattern:** SQL files in git (`sql/daily_revenue.sql.j2`) + a YAML of params per environment + a runner that renders → logs the rendered SQL → executes inside a transaction. (This is the idea dbt/Airflow templating productize; Airflow's `{{ ds }}` is Jinja too.)

---

## Part B — Practice Problems (13)

*Try each 15 min first. ✅ = solution code was run.*

### Easy (1–5)

**1. [Amazon] Clean a column.** `email` has mixed case and spaces; make it a clean key.
- *Solution:* `df.email.str.strip().str.lower()` ✅ (`" Asha@Example.com "` → `asha@example.com`); treat `""` as NULL.
- *Follow-up:* "Gmail dots/plus-addressing?" → business rule, not cleaning; define canonicalization separately.

**2. [Meta] Count and dedupe.** How many duplicate rows? Remove them.
- *Solution:* `df.duplicated().sum()` → 1 ✅; `df.drop_duplicates()`; for "latest per key": `sort_values("ts").drop_duplicates("id", keep="last")`.
- *Follow-up:* "Why `keep`?" → decides which survives; ties are arbitrary unless you add a tiebreaker column.

**3. [Google] Parse money.** `"$75.00"`, `"1,200.00"`, `""` → floats.
- *Solution:* `pd.to_numeric(s.str.replace(r"[$,]", "", regex=True), errors="coerce")` ✅ then **count the new NaNs**.
- *Follow-up:* "Locale where `,` is the decimal?" → don't strip blindly; parse per locale.

**4. [Netflix] Read/write formats.** Write a DataFrame as gzip CSV, Parquet and JSONL, then read each back. 
- *Solution:* `to_csv(..., compression="gzip")`, `to_parquet(..., engine="pyarrow")`, `to_json(orient="records", lines=True)`. Parquet preserves dtypes; CSV/JSON don't (dates return as strings).
- *Follow-up:* "Which for analytics?" → Parquet.

**5. [Uber] A simple API call.** Fetch JSON and load it into a DataFrame.
- *Solution:* `r = requests.get(url, timeout=10); r.raise_for_status(); pd.json_normalize(r.json()["data"])` (`json_normalize` flattens nested dicts).
- *Follow-up:* "No timeout?" → `requests` waits forever by default: always set one.

### Medium (6–10)

**6. [Stripe] Clean a messy CSV into analysis-ready form** ✅ (A1 pipeline): read all as `str`, strip/lower text, parse numbers/dates with `errors="coerce"`, dedupe, standardize categories, **audit the NULLs created**, and split rows into `clean` vs `rejected` (with reasons) instead of silently dropping.
- *Follow-up:* "Mixed date formats in one column?" → `format="mixed"` (pandas ≥ 2) or try known formats in order, log the unparseable ones; never guess day/month order.

**7. [Airbnb] Merge 3 DataFrames and handle many-to-many correctly** ✅
```
orders(order_id 1,2 for C1) × promos(C1: P1, P2) × tier(C1: gold)
naive merge: 4 rows | SUM(amount) = 300   (true total 150)  <- double counting
fixed: join tier with validate="m:1"; collapse promos to one row per customer (list) first -> 2 rows | SUM = 150
```
- *Solution principle:* aggregate the "many" side to the grain of the join key **before** merging; use `validate=` and compare `SUM` before/after.
- *Follow-up:* "Need one row per order × promo?" → then the grain changes; don't sum order amounts across it.

**8. [Google] Data quality report** ✅
```
column   completeness_%  unique_%  nulls  blank_strings  top_value     min        max
order_id          100.0      80.0      0              0         3     1.0        5.0
email              60.0      60.0      1              1   c@x.com
amount             80.0      80.0      1              0       5.0     5.0  1000000.0   <- outlier visible
status            100.0      40.0      0              0      paid
full-row duplicates: 0 | duplicate order_id: 1
```
Completeness counts NULL **and** blank strings (`""` is "missing" too). Pair this profiling with *rules* (Problem 13).
- *Follow-up:* "Large tables?" → compute on a sample or push down to SQL/Spark; track metrics over time and alert on drift.

**9. [Amazon] Compare two DataFrames and generate a diff report** ✅
```python
m = old.merge(new, on=key, how="outer", suffixes=("_old", "_new"), indicator=True)
# new = right_only, deleted = left_only, changed = both where NOT((a == b) | (a.isna() & b.isna()))
```
```
{'new': [5], 'deleted': [4], 'changed': [(2,'city','Austin','Dallas'), (3,'tier',nan,'g')], 'unchanged': 1}
```
NaN trap ✅: `pd.Series([nan]).ne(pd.Series([nan]))` is **True**, so a naive `!=` flags NULL→NULL as a change; the NULL-safe comparison above doesn't. (Same as the SQL `IS DISTINCT FROM` lesson in Chapter 12.)
- *Follow-up:* "100M rows?" → hash rows per key and compare hashes in the warehouse/Spark.

**10. [Stripe] Incremental API ingester that tracks `last_synced`** ✅ (A3): read watermark → fetch `updated_since` → write JSONL → advance the watermark **after** success; verified: first run 95, no-change run 0, then 3 new → 3.
- *Follow-up:* "Records updated while paginating?" → use overlapping `since = watermark − few minutes` and dedupe on id; or snapshot pagination via cursor.

### Hard (11–13)

**11. [Stripe] Payment reconciliation: provider API vs internal DB** ✅
```python
dup = provider[provider.txn_id.duplicated(keep=False)]; p = provider.drop_duplicates("txn_id")
m = p.merge(internal, on="txn_id", how="outer", suffixes=("_prov", "_int"), indicator=True)
# classify: left_only=missing_internally | right_only=missing_at_provider | amount differs | status (after mapping) differs | ok
```
```
txn_id  amount_prov  amount_int  status_prov  status_int   result
t1          1000         1000     settled      paid         ok
t2          2500         2500     settled      paid         ok
t3           990         1000     settled      paid         amount_mismatch(+10)
t4          4000         4000     refunded     paid         status_mismatch
t5           700          NaN     settled      NaN          missing_internally
t6           NaN          300     NaN          paid         missing_at_provider
duplicate provider rows: ['t5'] | summary: {ok: 2, amount_mismatch: 1, status_mismatch: 1, missing_internally: 1, missing_at_provider: 1}
```
Design points: integer **cents** (never floats); **status mapping** table (provider vs internal vocabularies); handle duplicates first (the provider sent `t5` twice); reconcile within a date window and by currency; output an **exceptions table** with reason codes + owner + age, and totals (`SUM(prov) − SUM(int)`) so finance can sign off.
- *Follow-up:* "Provider fees / timing differences (T+2 settlement)?" → match on a reference id with a tolerance window and reconcile aggregates per settlement batch.

**12. [Amazon] Inventory pipeline: 5 CSV formats → one schema with an audit trail.** (Same technique as the 3-source normalization below ✅.)
- *Solution:* One `normalize_<source>()` function per format returning the **same** canonical columns; a registry `{source: fn}`; `try/except` **per row** with a dead-letter list `(file, line, error, raw)`; dedupe on `(sku, as_of)` keeping latest; add `_source_file`, `_ingested_at`, `_row_hash` audit columns; validate with pydantic/expectations; write partitioned Parquet; emit metrics (rows in / out / rejected per source).
- *Follow-up:* "A source adds a column?" → canonical schema is explicit; unknown columns are dropped *with a logged warning* until someone adds them deliberately.

**13. [Airbnb] Data quality framework: expectations → HTML report** ✅
```python
class Expectation:   # name + function returning the FAILING rows + severity
    def run(self, d, ctx): bad = self.fn(d, ctx); return {"name": ..., "severity": ..., "failed_rows": len(bad), "passed": len(bad) == 0, "sample": bad.head(3).to_dict("records")}
not_null(col), unique(col), between(col, lo, hi), ref_integrity(col, ref, refcol), in_set(col, vals, severity="warn")
```
Test frame: `orders` = ids `[1,2,2,4,5]`, a NULL customer, a negative and a 1e7 amount, an orphan customer `C9`, status `lost`:
```
FAIL [error] customer_id not null                           failed_rows=1
FAIL [error] order_id unique                                failed_rows=2
FAIL [error] amount in [0,100000]                           failed_rows=2
FAIL [error] customer_id exists in customers.customer_id    failed_rows=1
FAIL [warn ] status in ['paid', 'refunded', 'shipped']      failed_rows=1
HTML report written (1,657 bytes) | pipeline gate: fail on error-severity failures -> True
```
`html.escape` all data values in the report (data can contain markup). Design: severities (`error` blocks the pipeline, `warn` alerts), failing-row samples for debugging, results stored as a table over time (trend + alerting), and reuse of the same suite in CI and in production. Libraries to mention: Great Expectations, Soda, `pandera`, dbt tests.
- *Follow-up:* "Run on billions of rows?" → push rules down as SQL/Spark aggregates (count of violations), return samples only.

---

## Part C — Interview Simulations

### Q1 [Google]: "Hourly JSON dumps from 3 sources with different schemas. Normalize, dedupe, validate, write partitioned Parquet. Handle schema evolution."  ✅ RAN (small sample)

Sources: **web** `{orderId, ts(ISO), total("10.50"), cust:{id}}`; **mobile** `{order_id, created(epoch), amount_cents, customer_id, [device]}`; **pos** `{id:"P-4", time, amt, cust_id}`.
```python
def n_web(r):    return {"order_id": str(r["orderId"]), "ts": pd.Timestamp(r["ts"]).tz_convert("UTC").tz_localize(None), "amount_cents": round(float(r["total"]) * 100), "customer_id": r["cust"]["id"], "source": "web"}
def n_mobile(r): return {"order_id": str(r["order_id"]), "ts": pd.Timestamp(r["created"], unit="s"), "amount_cents": int(r["amount_cents"]), "customer_id": r["customer_id"], "source": "mobile"}
def n_pos(r):    return {"order_id": str(r["id"]).replace("P-", ""), "ts": pd.to_datetime(r["time"], errors="raise"), "amount_cents": round(float(r["amt"]) * 100), "customer_id": r["cust_id"], "source": "pos"}
NORM = {"web": n_web, "mobile": n_mobile, "pos": n_pos}; good, dead = [], []
for f in sorted(os.listdir(IN)):
    src = f.split("_")[0]                                          # source encoded in the file name
    for ln, line in enumerate(open(f"{IN}/{f}")):
        try: good.append(NORM[src](json.loads(line)))
        except Exception as e: dead.append({"file": f, "line": ln, "error": type(e).__name__, "raw": line.strip()})
out = pd.DataFrame(good).sort_values("ts").drop_duplicates("order_id", keep="last")
out["dt"] = out.ts.dt.strftime("%Y-%m-%d"); out = out[out.amount_cents >= 0]
out.to_parquet(OUT, partition_cols=["dt"], index=False)
```
```
order_id                  ts  amount_cents customer_id source         dt
       1 2024-03-01 10:00:00          1050          C1    web 2024-03-01
       2 2024-03-01 10:20:00           500          C2    web 2024-03-01
       3 2024-03-01 10:46:40           990          C3 mobile 2024-03-01
       4 2024-03-02 09:00:00          1200         NaN    pos 2024-03-02
dead letters: [('pos_2024030110.jsonl', 1, 'DateParseError')]   | output partitions: ['dt=2024-03-01', 'dt=2024-03-02']
```
What this shows: **three different shapes → one canonical schema** (amounts converted to integer cents; timestamps to naive UTC); the row with `time: "bad"` went to a **dead-letter list** rather than crashing the batch; partitioned output by date; mobile's extra `device` field was **ignored** (schema evolution policy: explicit canonical schema; unknown fields are logged and dropped until deliberately added, or kept in a `raw`/`extras` JSON column).
> **A subtle bug I should flag:** order 2 appeared on web *and* mobile with the **same timestamp** (10:20:00). `sort_values("ts")` + `drop_duplicates(keep="last")` doesn't define a winner for ties, so which source "wins" is arbitrary (it happened to pick web). Add an explicit **source priority** or `_ingested_at` tiebreaker.

**Say in the interview:** idempotent writes (overwrite the hour's partition, or write files named by the input hash); an `_extras` JSON column for unknown fields; a **schema registry/contract** and alerting on drift; validation (pydantic / expectations) with a **reject threshold** (fail the run if >X% of rows are rejected); late data (hourly files re-delivered); memory (stream files with `chunksize`/generators, process files in parallel, Chapter 5); and an audit table of `(file, rows_in, rows_out, rows_rejected)`.

### Q2 [Amazon]: "Paginated API, 100 requests/minute. Production-quality ingestion, graceful failures, Parquet partitioned by date."  ✅ (client + sync verified in A3; Parquet write verified in A6)

```python
class RateLimiter:                          # token bucket: allows bursts up to `capacity`, refills at rate/s
    def __init__(self, per_minute=100): self.rate, self.tokens, self.cap, self.t = per_minute / 60, per_minute, per_minute, time.monotonic()
    def acquire(self):
        while True:
            now = time.monotonic(); self.tokens = min(self.cap, self.tokens + (now - self.t) * self.rate); self.t = now
            if self.tokens >= 1: self.tokens -= 1; return
            time.sleep((1 - self.tokens) / self.rate)

def ingest(client, rl, out_dir, since):
    buf, n = [], 0
    for rec in paginate_cursor_with(client, rl, since=since):          # every request calls rl.acquire() first
        buf.append(rec)
        if len(buf) >= 10_000: n += flush(buf, out_dir); buf.clear()   # batch writes: one Parquet file per 10K rows, never one per record
    n += flush(buf, out_dir) if buf else 0
def flush(buf, out_dir):
    df = pd.DataFrame(buf); df["dt"] = pd.to_datetime(df.updated_at, unit="s").dt.strftime("%Y-%m-%d")
    pa_tbl = pa.Table.from_pandas(df, schema=SCHEMA, preserve_index=False)             # explicit schema
    pq.write_to_dataset(pa_tbl, out_dir, partition_cols=["dt"], basename_template=f"{uuid4()}-{{i}}.parquet"); return len(df)
```
Talking points: (1) **client-side rate limit** *and* respect `429/Retry-After` (server is the authority); (2) retries only on transient errors, with **exponential backoff + jitter**, a **max attempts/total deadline**, and fail loudly otherwise (verified behaviours in A3); (3) **checkpointing**: persist cursor + watermark so a crash resumes instead of restarting (and be idempotent: dedupe on id, since resume is at-least-once); (4) **memory**: generator-based pagination, flush every N rows; (5) **partitioned Parquet** with an explicit schema, unique file names (so reruns don't overwrite each other) plus dedupe/compaction later; (6) **observability**: structured logs (requests, retries, 429s, rows written), metrics, and an alert when the retry rate rises; (7) secrets via env/secret manager; (8) tests with a **mock server** (exactly what I used to verify this chapter).

| Evaluator is looking for | Green flag | Red flag |
|---|---|---|
| Resilience | Backoff + jitter, `Retry-After`, no retry on 4xx, token refresh | Bare `while True: requests.get` |
| Correctness | Idempotent sink, watermark after write, dedupe | Duplicates/gaps on rerun |
| Scale | Generators, batched Parquet writes, partitioning | Collect everything into one list/DataFrame |
| Production sense | Timeouts, logging, secrets, tests against a mock | Hard-coded keys, no timeouts |

---

## Chapter 6 Cheat Sheet

- Read dirty data as `str`; coerce explicitly; **count NULLs created**; never silently drop.
- **Vectorize** (900-2,500× faster than `apply`/`iterrows` ✅); `category`/downcast cut memory 4× ✅; `chunksize` for big files ✅.
- `merge`: check row counts, `indicator=True`, `validate="m:1"`; aggregate the many-side first ✅. NULL vs NULL: use NULL-safe comparison ✅.
- polars: lazy + expressions + multithreaded; ~3× on join/aggregate here ✅ but **not** faster on every operation ✅. Benchmark your workload.
- `requests`: Session, **timeouts**, retry only transient errors, exponential backoff + jitter, honor `Retry-After`, refresh tokens, cursor pagination, watermark advanced *after* write ✅.
- SQLAlchemy: `engine.begin()` for transactions, bind parameters, pool sizing; **upsert** via `ON CONFLICT ... WHERE IS DISTINCT FROM` ✅; `to_sql("replace")` drops constraints; `method="multi"` is not always faster ✅.
- pydantic for configs and row validation; collect bad rows; validate large data in columnar form ✅.
- Parquet via PyArrow with an **explicit schema**, partitioning, row-group stats ✅.
- Jinja2 = structure only (allow-list); **values via bind parameters** (injection demonstrated ✅); `StrictUndefined`.

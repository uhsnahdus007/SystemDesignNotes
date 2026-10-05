# Chapter 16: System Design for Data Engineering Interviews

> **What's verified:** All capacity numbers below were computed by a script (not mental math). All SQL DDL was executed in DuckDB (14 tables created without error); the as-of join and dedup-then-count queries were run and their outputs are shown. The Spark/Flink snippets are design illustrations and were **not** run. Architecture choices and **dollar costs are order-of-magnitude estimates** using typical public cloud list prices; say "roughly" in the interview and check current pricing for real planning.

## How to use a design walkthrough in an interview (45 min)

| Minutes | Do |
|---|---|
| 0–5 | Requirements: functional, non-functional, scale. **Ask questions.** |
| 5–10 | Capacity estimate (say assumptions aloud) |
| 10–20 | High-level diagram + data flow |
| 20–32 | Deep-dive on 1–2 hardest parts (interviewer will steer) |
| 32–42 | Scale, failure, monitoring, cost |
| 42–45 | Trade-offs recap |

---

# Design 1 — Real-Time Analytics Dashboard (Google/Meta style)

### 1. Requirements
**Functional:** real-time DAU, page views per page/min, conversion funnel (view → add-to-cart → checkout → purchase); filter by platform/country; historical comparisons.
**Non-functional:** dashboard freshness ≤ 10 s; query p95 < 1 s; tolerate duplicates/late events (mobile can be offline for hours); 99.9% availability.
**Questions to ask:** Exact or approximate uniques OK? (Approx = HLL, 100× cheaper.) How late can events arrive? Retention? Who consumes it (ops dashboards vs product analysts)?

### 2. Capacity (script output)
Assume 100M DAU × 50 events/user/day, 500 B/event, 3× peak, 5× columnar compression.

```
avg 57,870 events/s   peak 173,611 events/s
raw 2.5 TB/day        stored ≈ 500 GB/day      90-day retention ≈ 45 TB
Kafka peak bandwidth ≈ 87 MB/s
```
Kafka sizing: bandwidth alone needs few partitions, but choose **~48 partitions** for consumer parallelism and headroom, keyed by `user_id` (keeps a user's events ordered, so sessionization is local).

### 3. Architecture

```
 Web/Mobile SDK ─► Edge collector (HTTP, batches, ack fast)
                       │
                     Kafka (events, 48 partitions, RF=3)
              ┌────────┴──────────────────────┐
   Spark Structured Streaming         Raw sink (Kafka Connect → S3/Delta bronze)
   dedup + watermark + windows               (replay & backfill source of truth)
              │
     ┌────────┴─────────┐
 Delta (agg tables)   Serving store (Druid / ClickHouse / Pinot)
     │                      │
  BI / ad-hoc           Dashboard API ─► Dashboard
```

### 4. Data model (executed in DuckDB ✅)
```sql
CREATE TABLE bronze_events (
  event_id VARCHAR, user_id BIGINT, session_id VARCHAR, event_type VARCHAR, page VARCHAR,
  platform VARCHAR, event_ts TIMESTAMP, ingest_ts TIMESTAMP, props VARCHAR);
CREATE TABLE agg_page_views_1min (
  window_start TIMESTAMP, page VARCHAR, platform VARCHAR, views BIGINT, uniq_users_hll BLOB,
  PRIMARY KEY (window_start, page, platform));
CREATE TABLE agg_dau_daily (dt DATE, platform VARCHAR, dau BIGINT, PRIMARY KEY (dt, platform));
CREATE TABLE funnel_daily (dt DATE, step INT, step_name VARCHAR, users BIGINT, PRIMARY KEY (dt, step));
```
Key idea: `event_ts` (when it happened) vs `ingest_ts` (when we saw it). Aggregate on **event time**.

### 5. Why each technology
| Component | Why | Alternative & why not |
|---|---|---|
| Kafka | Durable, replayable, ordered per key, decouples producers/consumers | Kinesis (fine on AWS, less ecosystem), Pub/Sub |
| Spark Structured Streaming | Same engine/API as batch, Delta sink, exactly-once to Delta | Flink: lower latency & better state handling, steeper ops |
| Delta bronze | Cheap replay + audit; backfills fix bugs | Skip it → can't fix bad aggregates |
| Druid/Pinot/ClickHouse | Sub-second OLAP on fresh data at high concurrency | Querying Delta directly: seconds-to-minutes, poor concurrency |

### 6. Data flow (with the code that matters)
```python
from pyspark.sql import functions as F
events = (spark.readStream.format("kafka")
    .option("subscribe", "events").option("kafka.bootstrap.servers", BROKERS).load()
    .select(F.from_json(F.col("value").cast("string"), SCHEMA).alias("e")).select("e.*"))

page_views = (events.filter("event_type = 'view'")
    .withWatermark("event_ts", "30 minutes")              # accept 30 min late data
    .dropDuplicates(["event_id", "event_ts"])              # dedupe within watermark
    .groupBy(F.window("event_ts", "1 minute"), "page", "platform")
    .agg(F.count("*").alias("views"), F.approx_count_distinct("user_id").alias("uniq_users")))

(page_views.writeStream.outputMode("update").option("checkpointLocation", CKPT)
    .foreachBatch(upsert_to_serving_and_delta).start())
```
Dedup/DAU logic, tested on a tiny table ✅ RAN:
```sql
WITH d AS (SELECT DISTINCT ON (event_id) * FROM bronze_events)
SELECT CAST(event_ts AS DATE) dt, platform, COUNT(DISTINCT user_id) dau FROM d GROUP BY 1,2;
-- input had a duplicate event_id 'a' (user 1, web) -> counted once
-- output: [(2024-03-01, 'ios', 1), (2024-03-01, 'web', 1)]
```
**Late data policy:** recent windows update in place (sink is an upsert). Events later than the watermark go to a *late* topic and are reconciled by a nightly batch job reading bronze. Dashboards show "final after T+1" for yesterday.

### 7. Scalability
- **10×:** add Kafka partitions + Spark executors; pre-aggregate at the edge (SDK batching); move distinct counting to HLL sketches.
- **100×:** shard by region (regional Kafka clusters, mirror to a global one); reduce cardinality of dimensions; tiered retention (1-min grain 7 days, 1-hour grain 2 years).
- **Hot keys** (viral page): salt key and re-merge, or aggregate in two stages.

### 8. Fault tolerance
| Failure | Mitigation |
|---|---|
| Spark job crash | Checkpoint + Kafka offsets → restart resumes; sink idempotent (upsert by window key) |
| Duplicate events (SDK retries) | `event_id` dedup within watermark |
| Kafka broker loss | RF=3, `min.insync.replicas=2`, `acks=all` |
| Bad deploy corrupts aggregates | Replay from bronze (Delta time travel + Kafka retention) |
| Serving store down | Dashboard falls back to Delta aggregates (slower, stale) |

### 9. Monitoring
Consumer lag (alert > 60 s), end-to-end freshness (`now - max(event_ts)` in serving), events/s vs 7-day baseline (±30%), % late events, duplicate rate, dashboard p95 latency, null rate in key fields.

### 10. Cost (rough)
Storage ~45 TB object storage ≈ **$1,000/month** (at ~$0.023/GB-month). The dominant costs are streaming compute (always-on cluster) and the serving cluster (Druid/ClickHouse nodes) — typically **low tens of thousands $/month** at this scale; the biggest lever is lowering granularity/cardinality and using sketches.

### 11. Trade-offs to discuss (≥3)
1. **Exact vs approximate uniques:** HLL ~1–2% error, tiny state. Exactness needs full user-id sets.
2. **Spark vs Flink:** Spark = unified stack/simplicity; Flink = true event-at-a-time, better for sub-second.
3. **Lambda vs Kappa:** One streaming path with bronze replay (Kappa-ish) vs separate batch + stream (Lambda: double the logic). Prefer one codepath plus a reconciliation job.
4. **Update vs append output mode:** update keeps the dashboard fresh; append waits for watermark (final but delayed).

---

# Design 2 — E-Commerce Data Warehouse (Amazon style)

### 1. Requirements
**Functional:** consolidate orders, inventory, payments, reviews; revenue/margin/returns reporting; customer 360; near-real-time order status (batch + real-time).
**Non-functional:** daily financials must **reconcile to the penny** with the finance system; data available by 7 am; history 3+ years; PII governed.
**Ask:** Source systems (OLTP DBs? SaaS?), CDC allowed? Currency/timezone rules? Who owns "revenue" definition?

### 2. Capacity (script ✅)
2M orders/day × 3 lines × 300 B, 10× peak (sales events):
```
69 lines/s avg, 694/s peak     raw 1.8 GB/day, stored ≈ 450 MB/day
3-year retention ≈ 0.5 TB
```
**Insight to say aloud:** this is *small* data. The hard part is **correctness, not volume**. Don't over-engineer with a massive streaming platform for the core DW.

### 3. Architecture
```
 Orders DB ─┐                         ┌─► Bronze (raw, append, Delta)
 Inventory ─┼─ CDC (Debezium) / API ──┤
 Payments ──┤        + Kafka          └─► Silver (cleaned, deduped, typed, MERGE)
 Reviews ───┘                                   │
                                      dbt: staging → intermediate → marts (gold star)
                                                │
                                   Warehouse (Snowflake/Databricks SQL/BigQuery)
                                                │
                              BI + reverse-ETL + Ops "order status" API (from silver)
 Orchestration: Airflow   |  Quality: dbt tests + reconciliation checks   |  Lineage/Catalog
```

### 4. Data model ✅ (DDL executed)
```sql
CREATE TABLE dim_date (date_key INT PRIMARY KEY, full_date DATE, year INT, month INT, is_weekend BOOLEAN);
CREATE TABLE dim_customer (customer_sk BIGINT PRIMARY KEY, customer_id VARCHAR, city VARCHAR, segment VARCHAR,
  valid_from TIMESTAMP, valid_to TIMESTAMP, is_current BOOLEAN);          -- SCD Type 2
CREATE TABLE dim_product (product_sk BIGINT PRIMARY KEY, sku VARCHAR, category VARCHAR, brand VARCHAR);
CREATE TABLE fact_order_line (
  order_number VARCHAR, line_no INT,                                        -- degenerate dimension
  order_date_key INT REFERENCES dim_date(date_key),
  customer_sk BIGINT REFERENCES dim_customer(customer_sk),
  product_sk BIGINT REFERENCES dim_product(product_sk),
  qty INT, gross_amount DECIMAL(12,2), discount DECIMAL(12,2), net_amount DECIMAL(12,2),
  PRIMARY KEY (order_number, line_no));
```
**Grain:** one row per order line. State this explicitly. Separate fact tables for payments, inventory snapshots (periodic snapshot), returns.

### 5. Technology choices
| Choice | Why |
|---|---|
| CDC (log-based) for orders/payments | Captures updates and deletes with low source load; query-based polling misses deletes |
| Delta/Iceberg lakehouse as storage | Cheap, MERGE support, time travel for audits |
| dbt for transformations | SQL + tests + docs + lineage in version control |
| Airflow | Dependencies, retries, backfills, SLAs |
| Inventory = periodic snapshot fact | Balance changes are better modelled as daily snapshots + transaction facts |

### 6. Data flow
1. CDC events land in bronze (append-only, never modified).
2. Silver: dedupe by `(pk, lsn)`, keep latest per key with `MERGE`, apply deletes as soft-delete flags.
3. dbt staging → marts; **incremental models** with `unique_key = (order_number, line_no)`.
4. Reconciliation task: `SUM(net_amount)` per day vs finance extract; fail the DAG if difference > 0.
5. Publish "data ready" event for downstream (BI refresh, reverse-ETL).

### 7. Scalability
10× orders: nothing structural changes (still GBs/day). The first thing to break is **dimension handling and skew**: e.g. a mega-seller/product or "guest" customer key. At 100×: partition fact by date, Z-order by `customer_sk`/`product_sk`, introduce streaming for real-time ops views only.

### 8. Fault tolerance
Idempotent loads (MERGE on keys, overwrite-by-partition); late-arriving facts (dimension row not yet loaded) → insert **inferred member** (`customer_sk = -1` "Unknown" or placeholder row) and fix when dimension arrives; replay from bronze; source schema change → contract tests alert before breakage.

### 9. Monitoring
Freshness per source (max `updated_at`), row-count deltas vs previous day, null rate/uniqueness tests, revenue reconciliation difference, DAG duration vs SLA, orphan-FK counts, dbt test pass rate.

### 10. Cost (rough)
Storage under 1 TB ≈ **tens of $/month**. Compute (warehouse + Airflow + CDC) dominates: typically **a few thousand $/month**. Cost-saving levers: auto-suspend warehouses, incremental models, clustering instead of full scans.

### 11. Trade-offs
1. **Star schema vs one big table:** star = governed, reusable, smaller; OBT = fastest for a single BI tool but duplicates logic.
2. **ELT in warehouse (dbt) vs Spark ETL:** SQL-heavy business logic → ELT; heavy text/ML parsing → Spark.
3. **SCD2 on customer vs overwrite:** SCD2 gives point-in-time truth (what city *was* the customer in when they ordered) at the cost of joins/complexity.
4. **Batch vs streaming for the DW core:** batch is simpler and reconcilable; stream only the operational view.

---

# Design 3 — Fraud Detection Pipeline (Stripe/FinTech style)

### 1. Requirements
**Functional:** score every transaction (approve / review / decline) before authorization completes; features such as velocity, device, geo, merchant risk; analyst feedback loop (labels); model versioning and rollback.
**Non-functional:** **< 100 ms end-to-end p99** (scoring itself ≤ 30 ms); 99.99% availability — and a defined **fail-open vs fail-closed** policy; auditability (explain every decision).
**Ask:** What's the cost of a false positive vs a missed fraud? Label delay (chargebacks arrive in weeks)? Fail-open acceptable?

### 2. Capacity (script ✅)
5,000 TPS avg, 20,000 peak, 1 KB per decision record (with features), 4× compression:
```
432 GB/day raw, ≈108 GB/day stored, 30-day hot ≈ 3.2 TB
Kafka peak ≈ 20 MB/s  (bandwidth needs ~3 partitions; use 24+ keyed by card_id for parallelism)
Online feature store reads: 20k req/s × ~10 feature lookups = 200k reads/s peak
```

### 3. Architecture
```
 Payment API ──► Scoring Service (stateless, autoscaled)
      │               │  1. fetch online features (Redis/Dynamo, ~1–3 ms)
      │               │  2. compute request-time features (amount vs avg, new device?)
      │               │  3. model inference (in-process ONNX/XGBoost, ~5–15 ms)
      │               │  4. rules engine overlay (hard blocks, allowlists)
      │               ▼
      │          decision (approve/review/decline) ──► Kafka "decisions"
      │                                                   │
      └────► Kafka "transactions" ──► Stream processor (Flink/Spark SS)
                                         │ updates rolling features
                                         ▼
                                  Online store (Redis)   +   Delta offline store
                                                                 │
      Labels (chargebacks, analyst reviews) ──► training set builder (point-in-time)
                                                                 ▼
                                                  Train → eval gate → registry → shadow → canary
```

### 4. Data model ✅ (DDL executed)
```sql
CREATE TABLE txn_decisions (
  txn_id VARCHAR PRIMARY KEY, card_id VARCHAR, merchant_id VARCHAR, amount DECIMAL(12,2), txn_ts TIMESTAMP,
  score DOUBLE, decision VARCHAR, model_version VARCHAR, features_json VARCHAR, latency_ms INT);
CREATE TABLE fraud_labels (txn_id VARCHAR PRIMARY KEY, label VARCHAR, label_ts TIMESTAMP, source VARCHAR);
```
**Log the exact feature values used at decision time** (`features_json`). It gives (a) audit/explainability, (b) *perfect* training data without recomputation (no skew), (c) debugging.

Online store key design: `feat:card:{card_id}` → hash of `txn_cnt_1h, txn_cnt_24h, sum_amt_24h, distinct_merchants_24h, last_country, last_txn_ts`.

### 5. Why each technology
| Component | Why |
|---|---|
| In-process model (ONNX/XGBoost) | Avoids a network hop; 5–15 ms inference |
| Redis/DynamoDB | Single-digit ms key lookup; TTLs for rolling windows |
| Flink (or Spark SS) | Continuous windowed aggregates; Flink preferred when latency/state matter |
| Rules engine | Immediately blockable patterns without retraining; compliance rules |
| Kafka | Decouples scoring from feature updates; replay |

### 6. Data flow & latency budget
| Step | Budget |
|---|---|
| Network + auth | 15 ms |
| Online feature fetch (one pipelined call) | 5 ms |
| Request-time features | 2 ms |
| Inference | 15 ms |
| Rules + decision + async publish | 8 ms |
| **Total p99 target** | **< 100 ms with headroom** |

**Key subtlety:** the feature update from txn N may not be visible when txn N+1 arrives milliseconds later (velocity attack). Mitigation: scoring service itself does an **atomic increment** in Redis (`INCR`/Lua) on the way in, and stream processing is the source for longer windows.

### 7. Scalability
10×: scale scoring pods horizontally (stateless), shard Redis by `card_id`, more Kafka partitions. 100×: regional deployment (data residency + latency), local feature stores per region, model distillation/smaller models, cache merchant-level features locally in the scoring pod.

### 8. Fault tolerance
| Failure | Mitigation |
|---|---|
| Feature store timeout | Per-call deadline (e.g. 10 ms) → score with defaults + rules only; flag `degraded=true` |
| Model service error | Fallback to previous model version / rules-only |
| Policy decision | **Fail-open** for low-value txns, **fail-closed/step-up** (3DS) for high-risk segments |
| Bad model release | Shadow mode → canary 1% → auto-rollback on KPI regression |
| Duplicate txn requests | Idempotency key on `txn_id` |

### 9. Monitoring
p50/p99 latency per stage, timeout/degraded rate, approval rate & decline rate vs baseline, score distribution drift (PSI), feature null rate/freshness, **precision/recall using delayed labels** (cohorted by week), fraud-loss $ vs plan, chargeback rate.

### 10. Cost (rough)
Compute for scoring + Redis cluster (memory-heavy: 100M cards × ~200 B ≈ 20 GB base, ×3 for overhead/replicas ≈ 60 GB — small) → **low thousands to tens of thousands $/month**. Storage ~3 TB hot ≈ **~$70/month**. Largest costs are people and the cost of fraud/false declines, not infrastructure — say that.

### 11. Trade-offs
1. **Precision vs recall** is a business decision: put the threshold in config, not code; tune by $ loss + customer friction.
2. **Real-time features vs latency:** each extra feature adds lookup cost → budget features, precompute heavy aggregates.
3. **Rules vs ML:** rules = fast, explainable, brittle; ML = adaptive, opaque. Use both.
4. **Delayed labels:** train with a label-maturity window (e.g. exclude last 30 days) or you teach the model that recent txns are "not fraud".

---

# Design 4 — Data Lake for a Fast-Growing Startup

### 1. Requirements
**Functional:** ingest app DBs, event streams, SaaS exports; self-serve SQL for analysts; ML datasets; grow from **10 GB/day → 10 TB/day** over ~2 years.
**Non-functional:** low initial cost and ops burden; governance (PII, access) from day one; no re-platform at 100×.
**Ask:** Team size/skills? Compliance (GDPR/SOC2)? Cloud already chosen? Number of consumers?

### 2. Capacity (script ✅)
```
10 GB/day  → ≈ 0.9 TB/year stored   (at ~4× compression)
10 TB/day  → ≈ 0.9 PB/year stored
```
That 1000× range is the design driver: choose **open table formats on object storage** (cheap, decoupled storage/compute) so scaling is mostly "more compute".

### 3. Architecture (medallion)
```
 Sources ─► Ingestion ─► BRONZE (raw, immutable, partitioned by ingest date)
  DB CDC       Fivetran/Airbyte/Debezium+Kafka/Auto Loader │
  Events       Kafka                                        ▼
  SaaS APIs    API connectors                      SILVER (clean, typed, deduped, conformed keys)
                                                            │
                                                            ▼
                                               GOLD (business marts, metrics, features)
 Cross-cutting: Catalog (Unity/Glue) · Access control · Data quality · Lineage · Cost tags
 Consumption:   SQL warehouse/Trino/Athena · BI · notebooks · ML
```

### 4. Data model/metadata ✅ (DDL executed)
```sql
CREATE TABLE dq_results (run_id VARCHAR, table_name VARCHAR, check_name VARCHAR,
                         passed BOOLEAN, observed DOUBLE, run_ts TIMESTAMP);
```
Bronze convention: keep source columns as-is plus `_ingest_ts`, `_source_file`, `_batch_id`; schema evolution on, **never drop data on parse failure** (route to `_rescued_data`/quarantine).

### 5. Why each choice
| Choice | Why | Alternative |
|---|---|---|
| Delta/Iceberg tables | ACID, MERGE, schema evolution, time travel | Plain Parquet: no updates/deletes → GDPR pain |
| Managed ingestion at first (Fivetran/Airbyte) | Engineers' time is scarcest resource at 10 GB/day | Custom Kafka pipelines: build when volume/latency justify |
| Catalog + RBAC from day 1 | Retrofitting permissions later is far harder | "We'll add governance later" |
| Serverless/managed SQL | Pay per use while small | Self-hosted Trino/Spark: ops-heavy early |

### 6. Phased evolution (the key part of this answer)
| Stage | Volume | Do |
|---|---|---|
| 1 | 10 GB/day | Managed ingestion → bronze/silver/gold in one workspace, dbt for gold, daily batch |
| 2 | ~100 GB–1 TB/day | Add Kafka for event streams, incremental/streaming ingestion, partition + clustering strategy, compaction jobs, data contracts |
| 3 | 10 TB/day | Multiple domain-owned pipelines (data mesh-ish), tiered storage/lifecycle, cost allocation, SLOs per dataset, dedicated platform team |

### 7. Scalability
Watch for: **small files** (compaction/OPTIMIZE), **skew**, **too-granular partitions** (partition by date only, cluster by high-cardinality keys), metadata scaling (manifest/log size), and shuffle-heavy joins (broadcast small dims, pre-aggregate).

### 8. Fault tolerance
Immutable bronze = replay anywhere; idempotent loads (MERGE / overwrite partition); checkpoints on streams; DLQ/quarantine tables with alerts; table versioning allows `RESTORE` after bad writes; cross-region replication only for critical tables.

### 9. Monitoring
Freshness & volume per table, schema-drift alerts, DQ pass rate (`dq_results`), job failure/retry rates, cost per team/pipeline, query p95, small-file counts, % tables with owners + docs (governance health).

### 10. Cost (rough)
Storage at 10 TB/day grows ~0.9 PB/yr ≈ **$20k/month per PB-year of accumulated data** at standard object-storage pricing (~$0.02/GB-month) — so *lifecycle policies* (move cold bronze to infrequent/archive tiers) are a major lever. Compute is typically larger than storage; use autoscaling + spot instances for batch.

### 11. Trade-offs
1. **Buy vs build ingestion:** buy early, build selectively later.
2. **Centralized vs domain-owned (mesh):** centralize while <~20 data people; federate with contracts at scale.
3. **Lakehouse vs warehouse:** lakehouse for ML + open formats + cost; warehouse for simplicity of SQL-only shops. Often both.
4. **Strict schema (contracts) vs schema-on-read:** strict contracts at silver, flexible at bronze.

---

# Design 5 — Log Analytics Platform (Uber/Datadog style)

### 1. Requirements
**Functional:** ingest logs from thousands of services; full-text + field search; alerting on patterns/rates; dashboards; retention (hot 3–7 days, cold 90+).
**Non-functional:** **1M events/sec**; searchable within ~10 s; search p95 < 5 s on hot data; ingestion must never block the application (drop > block).
**Ask:** Are logs structured? Per-team quotas? Sensitive data in logs? Retention/compliance?

### 2. Capacity (script ✅)
500 B/event, 1.5× peak, 8× compression:
```
1,000,000 events/s avg  → 1.5M/s peak
raw 43.2 TB/day      stored ≈ 5.4 TB/day     7-day retention ≈ 37.8 TB
Kafka peak ≈ 750 MB/s  → ≥ 76 partitions by bandwidth; plan ~200 for headroom/parallelism
```
**The critical insight:** indexing *every* event in Elasticsearch at 43 TB/day raw is prohibitively expensive. Typical approach:
- **Index in ES/OpenSearch only a subset:** `WARN/ERROR`, plus a sample of INFO (e.g. ≈ 5% → ~2 TB/day raw) and indexed key fields.
- **Everything goes to Delta/Parquet** for cheap columnar scan (Trino/Spark/ClickHouse) when someone needs the full firehose.

### 3. Architecture
```
 Services ─► Agents (Fluent Bit/Vector, local buffer + backpressure drop)
                   │
              Kafka (logs, ~200 partitions, RF=3, topic per tier/priority)
          ┌────────┼───────────────────────────────┐
   Stream processors (Spark/Flink)           Kafka Connect → S3 (raw, compressed)
   parse → enrich → PII scrub → route                  │
      │         │            │                   Delta/Parquet (all logs, partitioned by hour)
      │         │            └─► Metrics/rates ─► Alert engine (rules, anomaly)
      ▼         ▼
 Elasticsearch  Delta "logs_curated"
 (hot, subset)          │
      │                 └─► Trino/Spark for deep search & forensics
      ▼
 Search UI / dashboards / on-call tools
```

### 4. Data model ✅ (DDL executed)
```sql
CREATE TABLE log_events_index (
  ts TIMESTAMP, service VARCHAR, level VARCHAR, host VARCHAR, trace_id VARCHAR, message VARCHAR);
```
Partition lake table by `date, hour`; cluster/Z-order by `service, level`. ES: index-per-day (or data streams) with ILM; map `service`, `level`, `host`, `trace_id` as keyword; `message` as text with limited analyzers.

### 5. Why each technology
| Component | Why |
|---|---|
| Local agent with disk buffer | App never blocked; survives short network loss |
| Kafka | Absorbs bursts (incident = log spike!), replay, multiple consumers |
| Elasticsearch/OpenSearch | Inverted index = fast text search over hot data |
| Delta/Parquet on S3 | 10–50× cheaper than ES for retention; scan-based queries |
| Separate alert path (stream rates) | Alerting shouldn't depend on search cluster health |

### 6. Data flow
1. Agent batches/compresses (e.g. 1 s or 1 MB) → Kafka. Structured JSON preferred; unstructured parsed with grok at the stream layer.
2. Stream job: parse → add `env`, `region`, `team` → **PII scrubbing** (emails, tokens) → route by level/service.
3. Fan-out: ES (subset), Delta (all), rate metrics (count by service/level/1 min) → alert engine.
4. Alert example: `errors/min for service X > 5× its 7-day baseline for 3 consecutive minutes` → page.
5. Search: UI hits ES first; "search older/all logs" button runs a Trino query on Delta.

### 7. Scalability
10×: partition Kafka by service tier, multi-cluster; **per-service quotas and sampling** (rate limit noisy tenants); shift more retention to cheaper tiers. 100×: move to columnar log stores (ClickHouse/Loki-style label-index + object storage) because full inverted indexing doesn't scale economically; index only labels, scan content.

### 8. Fault tolerance
| Failure | Mitigation |
|---|---|
| Incident → 10× log burst | Kafka buffer + dynamic sampling of DEBUG/INFO; **never drop ERROR** |
| ES cluster red | Alerts unaffected (separate path); logs still land in Delta |
| Stream job lag | Autoscale; backlog drained from Kafka (retention ≥ 24–72 h) |
| Agent can't reach Kafka | Local disk buffer with cap, drop oldest low-priority |
| Poison messages | Quarantine topic; don't block the partition |

### 9. Monitoring
Pipeline end-to-end lag, consumer lag per group, bytes ingested per service vs quota, ES indexing rejections/queue, ES heap/disk watermarks, search latency p95, dropped-event counters, alert delivery latency, cost per GB per tier. **Monitor the monitor:** a heartbeat log from each region that must appear in search.

### 10. Cost (rough)
Cold storage ≈ 5.4 TB/day × 90 days ≈ 490 TB ≈ **$11k/month** object storage at list price. Hot ES for ~2 TB/day × 3–7 days, replicated, is a cluster of **dozens of data nodes** — usually the **single largest line item (tens of thousands $/month)**, which is why subsetting/sampling is the biggest cost lever.

### 11. Trade-offs
1. **Index everything vs subset + scan:** cost vs "search any log instantly".
2. **Sampling/dropping vs completeness:** protect pipeline health; keep ERROR/audit logs complete.
3. **ES vs ClickHouse/Loki:** ES = best full-text; ClickHouse/Loki = far cheaper for structured/label queries.
4. **Parse at ingest vs at query:** ingest parsing = fast queries, brittle to format changes; query-time = flexible but slower.

---

# Design 6 — ML Feature Platform (Airbnb/Uber style)

### 1. Requirements
**Functional:** define a feature once; compute batch and real-time versions; train with **point-in-time correct** joins; serve online (< 10 ms) and offline; discovery/registry; reuse across teams.
**Non-functional:** no training-serving skew; feature freshness SLAs; backfills of 1+ years; access control.
**Ask:** Number of models/teams? Online latency? Entities (user, listing, driver)? Streaming features required?

### 2. Capacity (script ✅)
```
50M entities × 100 features × 8 B × 2 (overhead) ≈ 80 GB online footprint
```
Small enough for a Redis/Dynamo cluster; offline history is much bigger (every feature × every timestamp) — that's where Delta + compaction matter.

### 3. Architecture
```
              Feature definitions (code/YAML in git) ──► Registry (owner, TTL, schema, lineage)
                               │
        ┌──────────────────────┼───────────────────────────┐
   Batch pipeline        Streaming pipeline         On-demand (request-time)
   (Spark, daily/hourly) (Flink/Spark SS, windows)  (functions in serving)
        │                       │                          │
        ▼                       ▼                          │
   OFFLINE STORE (Delta: entity, feature, event_ts) ──┐    │
        │                                             │    │
        │  materialize latest values                  │    │
        ▼                                             ▼    ▼
   ONLINE STORE (Redis/Dynamo)  ◄── low-latency get ── Model serving
        ▲
   Training set builder: as-of join (labels × features)  ──► Training/Eval
```

### 4. Data model ✅ (DDL executed)
```sql
CREATE TABLE feature_registry (feature_name VARCHAR PRIMARY KEY, entity VARCHAR, dtype VARCHAR, owner VARCHAR,
  definition_sql VARCHAR, ttl_seconds INT, online_enabled BOOLEAN);
CREATE TABLE feature_values_offline (entity_id VARCHAR, feature_name VARCHAR, value DOUBLE,
  event_ts TIMESTAMP, created_ts TIMESTAMP, PRIMARY KEY (entity_id, feature_name, event_ts));
```
`event_ts` = when the feature value became true; `created_ts` = when we computed it (needed to reproduce what was *knowable* at the time if there are backfills/late data).

### 5. Point-in-time join ✅ RAN (DuckDB `ASOF JOIN`)
Feature `txn_cnt_7d` for user `u1` has values 3 (Mar 1), 9 (Mar 10), 20 (Mar 20). Labels at Feb 1, Mar 12, Mar 25:
```sql
SELECT l.entity_id, l.label_ts, f.value AS txn_cnt_7d
FROM labels l ASOF LEFT JOIN feature_values_offline f
  ON l.entity_id = f.entity_id AND f.feature_name = 'txn_cnt_7d' AND l.label_ts >= f.event_ts
ORDER BY l.label_ts;
```
**Output (real):**
```
('u1', 2024-02-01, None)    -- no feature existed yet -> NULL (do NOT backfill from the future)
('u1', 2024-03-12, 9.0)     -- latest value at/before Mar 12 is Mar 10's value, not Mar 20's
('u1', 2024-03-25, 20.0)
```
In Spark use `row_number()` over `(entity, label_ts)` ordered by `event_ts desc` filtered to `event_ts <= label_ts`, or the feature-store library's `get_historical_features`.

> **Note:** `l.label_ts >= f.event_ts` includes equality. For labels that are themselves the *outcome* of an event at that timestamp, use strict `>` or subtract a small **feature delay** (e.g. features must be ≥ 1 minute old) to model real ingestion latency.

### 6. Why each technology
| Component | Why |
|---|---|
| Single feature definition | One spec → batch & stream implementations tested for parity |
| Delta offline store | Time travel, MERGE, huge history, cheap |
| Redis/Dynamo online | ms reads; TTL = freshness contract |
| Registry | Discovery, ownership, deprecation, lineage; prevents duplicate "user_ltv_v7" features |
| On-demand features | Values only known at request time (e.g. `amount / avg_amount`) |

### 7. Scalability
10×: partition offline tables by date, compact; materialise only features with `online_enabled`; batch writes to online store. 100×: shard online by entity hash, tier features (hot/cold), reduce write amplification by writing only changed values, compress with protobuf/binary packing per entity (one read gets all features).

### 8. Fault tolerance
Materialization jobs idempotent (upsert by `(entity, feature, event_ts)`); online store rebuilt from offline; stale feature guard: serving checks `now - event_ts > ttl` → return default + metric; schema versioning for features (`v1`, `v2`), never mutate a feature's meaning in place.

### 9. Monitoring
Feature freshness vs SLA, null/default rate at serving, **online vs offline parity check** (recompute sample offline, compare), distribution drift (PSI/KS), online p99 latency & cache hit rate, backfill job success, feature usage (to deprecate unused ones).

### 10. Cost (rough)
Online store ~80 GB (+replicas) → **hundreds to low thousands $/month**. Offline storage grows with history: tens of TB ≈ **hundreds of $/month**. Spark compute for batch/backfills is the bigger cost; control with incremental materialization and by pruning unused features.

### 11. Trade-offs
1. **Build vs buy** (Feast/Tecton/Databricks Feature Store vs custom): buy unless you have unusual needs; the *point-in-time logic and governance* are the hard parts.
2. **Stream-computed vs batch-computed features:** streaming for freshness-critical (minutes), batch for everything else — streaming costs far more to run and debug.
3. **Online store as materialized latest-only vs full history:** latest-only is cheap and fast; history lives offline.
4. **Strict parity (same code) vs per-engine optimized code:** parity prevents skew but may be slower; mitigate with tests that compare outputs.

---

# Part C — System Design Framework Cheat Sheet (one page)

**R-C-A-D-S-F-M-C-T**

1. **R**equirements — functional, non-functional (latency, freshness, availability, correctness), constraints. *Ask 4–6 questions.*
2. **C**apacity — events/s (avg & peak), bytes/event, daily raw, stored (compression 4–8×), retention, bandwidth.
3. **A**rchitecture — boxes: ingest → buffer → process → store → serve. One diagram, then walk a single event through it.
4. **D**ata model — grain, keys, partitioning, event time vs ingest time.
5. **S**tack choices — *why* each; name the alternative you rejected.
6. **F**low details — dedup, ordering, late data, idempotency, exactly-once vs at-least-once + idempotent sink.
7. **M**ass scale — what breaks at 10× and 100× (hot keys, small files, metadata, cost).
8. **C**rash — failure modes, retries, DLQ, replay, fail-open vs fail-closed.
9. **T**elemetry & cost — SLIs/alerts; rough $; biggest cost lever.
10. **T**rade-offs — name ≥3 decisions with alternatives and when you'd switch.

**Numbers to memorize**

| Item | Value |
|---|---|
| Seconds/day | 86,400 |
| 1M events/day | ≈ 12/s |
| Peak factor | 2–10× avg (use 3 by default) |
| Parquet compression | ~4–8× vs JSON |
| Kafka partition throughput | ~10 MB/s comfortable per partition (design, don't max) |
| Redis point read | ~0.5–1 ms |
| Object storage | ~$0.02/GB-month (standard) |
| Delivery semantics | At-least-once + idempotent writes ≈ "effectively once" |

**Phrases that signal seniority:** "Let me state the grain." · "What's the cost of a wrong answer here?" · "Bronze is my source of truth, so I can replay." · "I'd make the sink idempotent." · "At this volume the problem is correctness, not scale." · "Here's what I'd drop under load, and what I'd never drop."

**Common red flags:** naming tools before requirements · no numbers · ignoring late/duplicate data · no failure story · "just use Spark/Kafka" without why · designing for 1000× on day 1 (or ignoring growth entirely).

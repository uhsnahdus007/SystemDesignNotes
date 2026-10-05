# Chapter 11: Apache Kafka & Streaming Data Processing

> **Verification status — read this first.**
> There is **no Kafka broker or Schema Registry** in my sandbox, and the Spark–Kafka connector jar can't be downloaded. So:
> - ✅ **RAN (real engine):** Spark 4.0.1 Structured Streaming (local mode, file source standing in for the topic): watermarks, output modes, dedup, checkpoints, stream-stream join. Avro schema evolution with `fastavro`.
> - ✅ **RAN (my in-memory model of Kafka):** partitioning, consumer groups, delivery guarantees, windowing. The partitioner uses Kafka's real **murmur2** hash; I checked my implementation against 6 published Kafka test vectors and all matched.
> - ⚠️ **NOT RUN:** `confluent-kafka` producer/consumer code, Kafka Connect configs, the Kafka→Spark→Delta job. Written carefully, but run them against a real broker before trusting them.

---

## Part A — Concept + Code

### A1. Kafka Architecture

**What / why:** Kafka is a distributed, durable, replayable commit log. Producers append; consumers read at their own pace by tracking **offsets**. That decouples systems and lets you re-read history.

```
                       ┌──────────────── Kafka cluster ────────────────┐
  Producers            │  Topic "payments" (3 partitions, RF=3)        │          Consumer group "fraud"
 ┌─────────┐  key→hash │  P0: [0][1][2][3][4]...  leader=broker1       │   ┌─ c1 ◄── P0, P1
 │ app A   ├──────────►│  P1: [0][1][2]...        leader=broker2       │──►│
 │ app B   │           │  P2: [0][1][2][3]...     leader=broker3       │   └─ c2 ◄── P2
 └─────────┘           │  each partition replicated to 2 followers     │   Consumer group "analytics" reads the
                       └────────────────────────────────────────────────┘   same topic independently (own offsets)
```
- **Topic:** a named stream, split into **partitions** (the unit of parallelism and ordering).
- **Ordering is guaranteed only within a partition**, never across partitions.
- **Offset:** a record's position in its partition. Consumers commit offsets per `(group, topic, partition)`.
- **Replication:** each partition has a leader and followers (the **ISR**, in-sync replicas). If the leader dies, an ISR follower takes over.
- **Retention:** by time/size (e.g. 7 days) or **compaction** (keep the latest record per key).

CLI (⚠️ NOT RUN):
```bash
kafka-topics.sh --bootstrap-server localhost:9092 --create --topic payments --partitions 6 --replication-factor 3 \
  --config min.insync.replicas=2 --config retention.ms=604800000
echo 'user-1:{"amount":100}' | kafka-console-producer.sh --bootstrap-server localhost:9092 --topic payments \
  --property parse.key=true --property key.separator=:
kafka-console-consumer.sh --bootstrap-server localhost:9092 --topic payments --group demo --from-beginning \
  --property print.key=true --property print.partition=true --property print.offset=true
kafka-consumer-groups.sh --bootstrap-server localhost:9092 --describe --group demo     # shows LAG per partition
```

### A2. Python Producer (confluent-kafka)

**Complete producer** ⚠️ NOT RUN — JSON serialization, key partitioning, acks, delivery callbacks, error handling:

```python
import json, logging, sys
from confluent_kafka import Producer, KafkaException

log = logging.getLogger("producer")

def delivery_report(err, msg) -> None:
    """Called from poll()/flush() for every message; THE place to detect failures (send() never raises for broker errors)."""
    if err is not None:
        log.error("delivery failed key=%s: %s", msg.key(), err)         # -> route to a dead-letter store / alert
    else:
        log.debug("delivered %s[%d]@%d", msg.topic(), msg.partition(), msg.offset())

def make_producer(acks: str = "all") -> Producer:
    return Producer({
        "bootstrap.servers": "localhost:9092",
        "acks": acks,                       # 0 = fire&forget, 1 = leader only, all = all in-sync replicas
        "enable.idempotence": True,         # broker dedups retries (needs acks=all)
        "retries": 2147483647,              # retry until delivery.timeout.ms
        "delivery.timeout.ms": 120_000,
        "compression.type": "lz4",
        "linger.ms": 20,                    # wait up to 20ms to build bigger batches (throughput)
        "batch.size": 64 * 1024,
        "max.in.flight.requests.per.connection": 5,    # safe (ordering kept) with idempotence on
    })

def produce(producer: Producer, topic: str, key: str, value: dict) -> None:
    try:
        producer.produce(topic, key=key.encode(), value=json.dumps(value).encode(), on_delivery=delivery_report)
    except BufferError:                      # local queue full: apply backpressure
        producer.poll(1.0); producer.produce(topic, key=key.encode(), value=json.dumps(value).encode(), on_delivery=delivery_report)
    except KafkaException as e:
        log.error("produce failed: %s", e); raise
    producer.poll(0)                         # serve delivery callbacks

if __name__ == "__main__":
    p = make_producer()
    for i in range(100): produce(p, "payments", key=f"user-{i % 10}", value={"id": i, "amount": 10 * i})
    p.flush(30)                              # ALWAYS flush before exit or buffered messages are lost
```

**Partitioning ✅ RAN** — Kafka's default partitioner is `murmur2(key) % numPartitions` (I implemented the real algorithm; it matches Kafka's published test vectors):
```
key=user-1     -> partition 2        key=user-42    -> partition 4
key=user-2     -> partition 2        key=card-9001  -> partition 3
key=user-3     -> partition 5        same key always same partition: True      (6 partitions)
```
| Strategy | Behaviour | Use when |
|---|---|---|
| Key-based (default with a key) | Same key → same partition → per-key ordering | Per-entity ordering (user, card, order) |
| Round-robin / sticky (no key) | Even spread | Order doesn't matter, max throughput |
| Custom partitioner | Your logic (e.g. VIP traffic isolated) | Hot keys, tenant isolation |

Spread test, 200 messages to 4 partitions (my model):
```
key          partition sizes: [30, 33, 94, 43]
round_robin  partition sizes: [50, 50, 50, 50]
custom       partition sizes: [37, 64, 57, 42]
hot key skew, key-partitioned (80% one key): [51, 41, 59, 849]   <- one partition gets ~80%
```
> **COMMON MISTAKE:** Adding partitions to a topic changes `hash(key) % N`, so **keys move partitions** and per-key ordering breaks across the change. Over-provision partitions upfront. Also, a hot key can't be fixed by more partitions (one key = one partition).

**`acks`: reliability vs latency**

| acks | Waits for | Loss risk | Latency |
|---|---|---|---|
| 0 | Nothing | High (no confirmation at all) | Lowest |
| 1 | Leader write | Medium (leader dies before replication → lost) | Low |
| all (`-1`) | All ISR (≥ `min.insync.replicas`) | Lowest | Highest |

Durable setup: `acks=all`, `replication.factor=3`, `min.insync.replicas=2`, `enable.idempotence=true`.

**Idempotent producer ✅ RAN (model).** A lost ACK makes the producer retry; without idempotence the log gets a duplicate:
```
enable.idempotence=False: records in log = 2
enable.idempotence=True : records in log = 1       (broker dedups on producer-id + partition + sequence number)
```

**Avro + Schema Registry** ⚠️ NOT RUN
```python
from confluent_kafka import SerializingProducer
from confluent_kafka.schema_registry import SchemaRegistryClient
from confluent_kafka.schema_registry.avro import AvroSerializer
from confluent_kafka.serialization import StringSerializer

SCHEMA = """{"type":"record","name":"Payment","namespace":"com.shop",
 "fields":[{"name":"id","type":"string"},{"name":"amount","type":"double"},
           {"name":"currency","type":"string","default":"USD"}]}"""
sr = SchemaRegistryClient({"url": "http://localhost:8081"})
producer = SerializingProducer({
    "bootstrap.servers": "localhost:9092",
    "key.serializer": StringSerializer("utf_8"),
    "value.serializer": AvroSerializer(sr, SCHEMA),          # registers schema, prefixes each message with its schema id
    "acks": "all", "enable.idempotence": True})
producer.produce("payments", key="user-1", value={"id": "p1", "amount": 9.5, "currency": "USD"})
producer.flush()
```
Each message carries only a 5-byte header (magic byte + schema id), not the schema. For my 2-field record, Avro used **11 bytes vs 27 bytes as JSON** ✅ (real measurement, `fastavro`).

### A3. Python Consumer (confluent-kafka)

**Consumer groups ✅ RAN (model).** Partitions are split among consumers of a *group*; **max useful consumers = number of partitions**:
```
1 consumer : {'c1': [0,1,2,3,4,5]}
2 consumers: {'c1': [0,1,2], 'c2': [3,4,5]}
4 consumers: {'c1': [0,1], 'c2': [2,3], 'c3': [4], 'c4': [5]}
8 consumers: c0..c5 get one partition each; c6, c7 idle        (6 partitions)
```

**Complete consumer with manual commits** ⚠️ NOT RUN:
```python
import json, logging
from confluent_kafka import Consumer, KafkaError, KafkaException

log = logging.getLogger("consumer")

def process(rec: dict) -> None:
    ...  # must be IDEMPOTENT (upsert by key). See at-least-once below.

def run() -> None:
    c = Consumer({
        "bootstrap.servers": "localhost:9092", "group.id": "fraud-scorer",
        "enable.auto.commit": False,               # commit AFTER processing, never before
        "auto.offset.reset": "earliest",           # only used when the group has no committed offset
        "isolation.level": "read_committed",       # only see committed transactional messages
        "max.poll.interval.ms": 300_000, "session.timeout.ms": 45_000})
    c.subscribe(["payments"], on_assign=lambda c, p: log.info("assigned %s", p),
                on_revoke=lambda c, p: c.commit(asynchronous=False))        # commit before losing partitions (rebalance)
    try:
        while True:
            msg = c.poll(1.0)
            if msg is None: continue
            if msg.error():
                if msg.error().code() == KafkaError._PARTITION_EOF: continue
                raise KafkaException(msg.error())
            if msg.value() is None: continue                                # tombstone
            try:
                process(json.loads(msg.value()))
            except (ValueError, KeyError) as bad:                           # poison message: don't block the partition
                log.error("bad record %s[%d]@%d: %s", msg.topic(), msg.partition(), msg.offset(), bad)
                # send to a dead-letter topic here
            c.commit(message=msg, asynchronous=False)                       # per-message sync commit = safe but slow; batch in prod
    finally:
        c.close()                                                           # leaves the group cleanly (triggers fast rebalance)
```
**Offset management**

| | Auto-commit (`enable.auto.commit=true`) | Manual commit |
|---|---|---|
| Behaviour | Commits periodically in the background | You decide when |
| Risk | Commits offsets of records not yet processed → **loss on crash** | Duplicates if crash between process and commit |
| Use | Prototypes / lossy-OK metrics | Anything that matters |

**Delivery semantics under a crash ✅ RAN (model)** — consumer processes 5 records and crashes on offset 2, restarts:
```
at_most_once   (commit BEFORE process): sink=[0,1,3,4]     lost=[2]   duplicates=[]
at_least_once  (commit AFTER process) : sink=[0,1,2,2,3,4] lost=[]    duplicates=[2]
exactly_once   (sink write + offset commit in ONE atomic txn): sink=[0,1,2,3,4]  lost=[] duplicates=[]
at_least_once + idempotent sink (upsert by key): rows = 5 (no dupes, no loss)
```
> **KEY INSIGHT:** "Exactly-once" is really **at-least-once delivery + idempotent/transactional processing**. Kafka-to-Kafka it's built in (transactions). Kafka-to-external-DB you get it by making the write idempotent (upsert on key / unique offset) or by storing the offset in the *same DB transaction* as the data.

**Kafka transactions (read-process-write)** ⚠️ NOT RUN
```python
from confluent_kafka import Producer, Consumer
p = Producer({"bootstrap.servers": "localhost:9092", "transactional.id": "fraud-scorer-1"})   # stable id per instance
p.init_transactions()
# loop:
p.begin_transaction()
p.produce("scores", key=k, value=score)
p.send_offsets_to_transaction(c.position(c.assignment()), c.consumer_group_metadata())       # offsets commit atomically with output
p.commit_transaction()          # or abort_transaction() on error
```

### A4. Schema Registry & Compatibility  ✅ RAN (`fastavro` reader/writer resolution = the same rules the registry enforces)

Registry stores versioned schemas per *subject* (e.g. `payments-value`) and rejects new versions that violate the subject's compatibility mode. Registering/evolving via REST ⚠️ NOT RUN:
```bash
curl -X POST -H "Content-Type: application/vnd.schemaregistry.v1+json" \
  --data '{"schema": "{\"type\":\"record\",...}"}' http://localhost:8081/subjects/payments-value/versions
curl -X PUT -H "Content-Type: application/vnd.schemaregistry.v1+json" --data '{"compatibility":"BACKWARD"}' \
  http://localhost:8081/config/payments-value
curl -X POST -H "Content-Type: application/vnd.schemaregistry.v1+json" --data @new_schema.json \
  http://localhost:8081/compatibility/subjects/payments-value/versions/latest        # test BEFORE registering
```
I tested real Avro resolution with old schema v1 `{id, amount}`:

```
BACKWARD  (NEW reader reads OLD data; upgrade consumers first)
  v2 add field WITH default : OK   -> {'id':'p1','amount':9.5,'currency':'USD'}
  v2 add field NO default   : FAIL (SchemaResolutionError: No default value for field currency)
  v2 remove field           : OK   -> {'id':'p1'}

FORWARD   (OLD reader reads NEW data; upgrade producers first)
  old reader on v2 data (extra field)        : OK   -> {'id':'p2','amount':3.0}   (extra field ignored)
  old reader on data with a field removed    : FAIL (SchemaResolutionError)       (reader expects 'amount')
```
| Mode | Allowed changes | Upgrade order |
|---|---|---|
| BACKWARD (default) | Add optional (with default) field; delete field | Consumers first |
| FORWARD | Add field; delete *optional* (defaulted) field | Producers first |
| FULL | Add/delete only fields **with defaults** | Any order |
| *_TRANSITIVE | Check against **all** prior versions, not only the last | Needed if old data stays in the topic |

> **COMMON MISTAKE:** BACKWARD (non-transitive) only checks against the *previous* version. With long retention or compacted topics, a v4 reader may meet v1 data. Use `BACKWARD_TRANSITIVE`.

### A5. Kafka Connect

**What / why:** A framework to move data in/out of Kafka with config only (no code): **source** connectors (DB → Kafka), **sink** connectors (Kafka → S3/warehouse). Handles offsets, retries, parallelism (`tasks.max`), converters, and Single Message Transforms (SMTs).

**Debezium PostgreSQL source** ⚠️ NOT RUN (the JSON syntax was validated):
```json
{ "name": "shop-pg-source",
  "config": {
    "connector.class": "io.debezium.connector.postgresql.PostgresConnector",
    "plugin.name": "pgoutput",
    "database.hostname": "${file:/secrets/pg.properties:host}", "database.port": "5432",
    "database.user": "${file:/secrets/pg.properties:user}", "database.password": "${file:/secrets/pg.properties:password}",
    "database.dbname": "shop", "topic.prefix": "shop",
    "table.include.list": "public.orders,public.customers",
    "slot.name": "debezium_shop", "publication.name": "dbz_publication",
    "snapshot.mode": "initial", "heartbeat.interval.ms": "10000",
    "key.converter": "io.confluent.connect.avro.AvroConverter", "key.converter.schema.registry.url": "http://schema-registry:8081",
    "value.converter": "io.confluent.connect.avro.AvroConverter", "value.converter.schema.registry.url": "http://schema-registry:8081" } }
```
**S3 sink** ⚠️ NOT RUN:
```json
{ "name": "orders-s3-sink",
  "config": {
    "connector.class": "io.confluent.connect.s3.S3SinkConnector", "tasks.max": "4",
    "topics": "shop.public.orders", "s3.bucket.name": "my-lake-bronze", "s3.region": "us-east-1",
    "storage.class": "io.confluent.connect.s3.storage.S3Storage",
    "format.class": "io.confluent.connect.s3.format.parquet.ParquetFormat",
    "flush.size": "100000", "rotate.interval.ms": "600000",
    "partitioner.class": "io.confluent.connect.storage.partitioner.TimeBasedPartitioner",
    "path.format": "'dt'=YYYY-MM-dd/'hr'=HH", "partition.duration.ms": "3600000", "locale": "en-US", "timezone": "UTC",
    "timestamp.extractor": "RecordField", "timestamp.field": "ts",
    "errors.tolerance": "all", "errors.deadletterqueue.topic.name": "dlq.orders-s3", "errors.deadletterqueue.context.headers.enable": "true",
    "value.converter": "io.confluent.connect.avro.AvroConverter", "value.converter.schema.registry.url": "http://schema-registry:8081" } }
```
Deploy: `curl -X POST -H "Content-Type: application/json" --data @connector.json http://connect:8083/connectors`. Check: `GET /connectors/<name>/status`.
> **Notes:** `flush.size` + `rotate.interval.ms` control file size vs freshness (small files again!). `errors.tolerance=all` with a DLQ prevents one poison message stopping the pipeline; **always alert on DLQ depth**, or you silently lose data.

### A6. Streaming Concepts (runnable simulations)

#### Event time vs processing time  ✅ RAN
8 events (some out of order, one 60 s late, one 3 minutes late). Bucketing by when we *saw* them gives wrong totals:
```
by EVENT time     : {0: 224, 60: 35, 120: 10}
by PROCESSING time: {0: 35, 60: 120, 120: 15, 180: 99}      <- late events land in the wrong buckets
```
> **KEY INSIGHT:** Business questions ("sales per minute") are about **event time**. Processing time is only for operational metrics (lag).

#### Watermarks  ✅ RAN
Watermark = `max(event_time seen) − allowed lateness`. A window is finalized once the watermark passes its end; events older than that are dropped.
```
emit final window [0,60) = 125   (watermark=115)
DROPPED late event b@20 amount=99: window [0,60) closed, watermark was 115
```
The true event-time total for window [0,60) was 224; the stream reports **125** because the 99 arrived 3 minutes late (lateness budget 10 s). That's the trade-off: bigger lateness → more correct, but more state and delayed output.

#### Windowing: tumbling / sliding / session  ✅ RAN
Event times `[5, 12, 20, 61, 70, 130]` (one user), size 60, slide 30, session gap 20:
```
tumbling : {0: 3, 60: 2, 120: 1}
sliding  : {-30: 3, 0: 3, 30: 2, 60: 2, 90: 1, 120: 1}   (each event is in size/slide = 2 windows)
session  : [(5, 20, 3), (61, 70, 2), (130, 130, 1)]       (start, last_event, count)
```
```python
def tumbling(times, size):
    out = defaultdict(int)
    for t in times: out[t // size * size] += 1
    return dict(sorted(out.items()))

def sliding(times, size, slide):
    out = defaultdict(int)
    for t in times:
        s = t // slide * slide                 # latest window start that contains t
        while s > t - size: out[s] += 1; s -= slide
    return dict(sorted(out.items()))

def sessions(times, gap):
    times = sorted(times); out = []; start = prev = times[0]; n = 1
    for t in times[1:]:
        if t - prev > gap: out.append((start, prev, n)); start = t; n = 0
        prev = t; n += 1
    out.append((start, prev, n)); return out
```
| Window | Overlap | State | Use |
|---|---|---|---|
| Tumbling | none | 1 window per event | Per-minute counts |
| Sliding | yes (size/slide copies) | more | Moving averages, "last 10 min every 1 min" |
| Session | dynamic, data-driven | merges on late events | User sessions |

### A7. Spark Structured Streaming  ✅ RAN (real Spark 4.0.1; the Kafka source replaced by a file source)

**Kafka read** ⚠️ NOT RUN (needs `spark-sql-kafka-0-10` on the classpath):
```python
raw = (spark.readStream.format("kafka").option("kafka.bootstrap.servers", "broker:9092")
       .option("subscribe", "payments").option("startingOffsets", "earliest")
       .option("maxOffsetsPerTrigger", 100_000).option("failOnDataLoss", "false").load())
events = (raw.select(F.from_json(F.col("value").cast("string"), schema).alias("e"), "timestamp").select("e.*"))
```
For runnable tests I used a **file source** (a directory of JSON files, one file ≈ one micro-batch via `maxFilesPerTrigger=1`):
```python
stream = spark.readStream.schema(schema).option("maxFilesPerTrigger", 1).json(IN)
windowed = (stream.withWatermark("event_time", "2 minutes")
            .groupBy(F.window("event_time", "1 minute")).agg(F.sum("amount").alias("total"), F.count("*").alias("n")))
```
Five files = five micro-batches:
```
b0: e1 12:00:10 (10), e2 12:00:40 (20)        b3: e6 12:00:50 (1000, LATE), e7 12:04:30 (3)
b1: e3 12:01:15 (5),  e4 12:02:30 (7)         b4: e8 12:00:55 (777, LATE)
b2: e5 12:05:00 (1)
```

**Output modes (real output)**
```
outputMode("update")   – only windows that CHANGED this batch
   batch0: [12:00→30]     batch1: [12:01→5, 12:02→7]     batch2: [12:05→1]
   batch3: [12:00→1030, 12:04→3]                          batch4: []          <- e8 changed nothing: dropped

outputMode("append")   – a window is emitted ONCE, after the watermark passes its end
   batch0..2: []
   batch3: [12:00→1030(3 events), 12:01→5, 12:02→7]
   batch4: []

outputMode("complete") – the entire result table, every batch (state grows forever; no watermark cleanup)
   batch4: [12:00→1807(4 events), 12:01→5, 12:02→7, 12:04→3, 12:05→1]    <- includes e8 (no watermark => nothing dropped)
```
| Mode | Needs watermark? | Emits | Use |
|---|---|---|---|
| append | Yes for aggregations | Final rows only (delayed by lateness) | Writing immutable results to files/tables |
| update | No (but needed to bound state) | Changed rows | Dashboards, upserting sinks |
| complete | No | Everything | Small aggregates (top-N) only |

> **KEY INSIGHT — a real Spark subtlety I observed.** Late data is judged against the watermark of the **previous** micro-batch. In batch 3, e6 (12:00:50) was already "late" by wall-clock logic (the watermark had reached 12:03:00 after batch 2), yet it **was still aggregated** (window 12:00 = 1030 = 10+20+1000) because the stricter watermark only applies from the *next* batch. In batch 4, e8 for the same window was **dropped** (update mode emitted nothing; append never revised the window). The Spark docs only guarantee that data *within* the lateness bound is kept; data beyond it "may or may not" be dropped. Never build exact-match assertions on the boundary.
> Real listener metrics showed `watermark` stepping 1970-01-01 → 11:58:40 → 12:00:30 → 12:03:00 and a nonzero `numRowsDroppedByWatermark` in the last batch (it reported 2 for what I expected to be one row; I did not investigate why, so treat that counter as indicative).

> **COMMON MISTAKE:** Aggregating in `append` mode without a watermark: Spark refuses to start (it can't know when a window is final).

**Dedup within the watermark ✅ RAN** (source redelivers `d1`):
```python
dd = stream.withWatermark("event_time", "10 minutes").dropDuplicates(["event_id", "event_time"])   # include event_time so state can expire
```
```
delivered ids: ['d1', 'd2', 'd3']   (d1 once, not twice)
```
> Without the event-time column in the dedup key, Spark can't expire the state and it grows forever.

**Checkpointing ✅ RAN**
```
rows processed on restart with same checkpoint     : 0    (offsets + state restored; nothing reprocessed)
rows processed with a NEW checkpoint dir           : 3    (everything replays)
```
A checkpoint stores source offsets, state-store files and the batch commit log. **Never delete or share a checkpoint casually**; changing the query's stateful logic (different aggregation/schema) may make an old checkpoint incompatible.

**Stream-stream join with watermarks and a time bound ✅ RAN**
```python
imp = spark.readStream.schema(s_imp).json(IN2).withWatermark("imp_time", "5 minutes")
clk = spark.readStream.schema(s_clk).json(IN3).withWatermark("click_time", "5 minutes")
j = imp.join(clk, F.expr("ad_id = click_ad_id AND click_time >= imp_time AND click_time <= imp_time + interval 1 minute"), "inner")
```
```
matches (ad, seconds-to-click): [('ad1', 20), ('ad3', 30)]      <- ad2 clicked 4.5 min after impression: outside the bound, no match
```
The **time-bound condition + watermarks** are what let Spark discard old buffered rows. Without them state grows without bound. Outer joins need both, and emit NULL-padded rows only after the watermark passes.

**`foreachBatch` → Delta (idempotent writes)** ⚠️ NOT RUN
```python
from delta.tables import DeltaTable

def upsert_batch(df, batch_id: int):
    latest = df.dropDuplicates(["event_id"])
    (DeltaTable.forName(spark, "silver.payments").alias("t").merge(latest.alias("s"), "t.event_id = s.event_id")
       .whenNotMatchedInsertAll().execute())              # re-running the same batch inserts nothing new -> idempotent

(events.writeStream.foreachBatch(upsert_batch)
   .option("checkpointLocation", "/chk/payments_silver").trigger(processingTime="30 seconds").start())

# Or: plain Delta sink, exactly-once on file-level commits via the checkpoint
(windowed.writeStream.format("delta").outputMode("append").option("checkpointLocation", "/chk/agg").toTable("gold.payments_per_min"))
```
`foreachBatch` lets you reuse batch logic (MERGE, writing to several sinks, JDBC). Because Spark can **re-run a batch after a failure**, the function must be idempotent (use `batch_id` or a MERGE).

**Complete pipeline Kafka → Spark → Delta** ⚠️ NOT RUN
```python
from pyspark.sql import SparkSession, functions as F, types as T
spark = SparkSession.builder.appName("payments-stream").config("spark.sql.shuffle.partitions", "48").getOrCreate()
schema = T.StructType([T.StructField("event_id", T.StringType()), T.StructField("user_id", T.StringType()),
                       T.StructField("event_time", T.TimestampType()), T.StructField("amount", T.DoubleType())])
bronze = (spark.readStream.format("kafka").option("kafka.bootstrap.servers", BROKERS).option("subscribe", "payments")
          .option("startingOffsets", "latest").option("maxOffsetsPerTrigger", 200_000).load()
          .select(F.col("key").cast("string").alias("k"), F.col("value").cast("string").alias("raw"),
                  "partition", "offset", "timestamp"))
bronze.writeStream.format("delta").option("checkpointLocation", "/chk/bronze").toTable("bronze.payments_raw")   # replay source
silver = (spark.readStream.table("bronze.payments_raw")
          .select(F.from_json("raw", schema).alias("e"), "partition", "offset").select("e.*", "partition", "offset")
          .filter("event_id IS NOT NULL")                                   # bad rows go to a quarantine stream in real life
          .withWatermark("event_time", "10 minutes").dropDuplicates(["event_id", "event_time"]))
silver.writeStream.format("delta").option("checkpointLocation", "/chk/silver").toTable("silver.payments")
gold = (spark.readStream.table("silver.payments").withWatermark("event_time", "10 minutes")
        .groupBy(F.window("event_time", "1 minute"), "user_id").agg(F.sum("amount").alias("spend")))
gold.writeStream.format("delta").outputMode("append").option("checkpointLocation", "/chk/gold").toTable("gold.spend_per_user_min")
```

---

## Part B — Practice Problems (13)

*Try each 15 min first. Format: scenario → hint → solution → follow-up.*

### Easy (1–5)

**1. [Stripe] Partition choice.** You need per-card ordering of payment events. What do you use as the message key?
- *Solution:* `card_id` (the entity whose order matters). All events for a card go to one partition (`murmur2(key) % N`) and keep order.
- *Follow-up:* "What if one merchant account is 40% of traffic?" → Don't key by merchant; if you must, salt (`merchant#k`) and accept loss of strict per-merchant order, or isolate that merchant on its own topic.

**2. [Amazon] Max consumers.** Topic has 12 partitions; group has 20 consumers. How many are active?
- *Solution:* 12 (one partition per consumer max); 8 sit idle. To scale consumption beyond 12, increase partitions (which remaps keys, see the warning in A2).
- *Follow-up:* "Two *different* groups read the topic?" → each group gets all messages independently.

**3. [Uber] `acks` choice.** Pick `acks` for (a) click logs, (b) payment ledger.
- *Solution:* (a) `acks=1` (or 0) for throughput, accepting rare loss; (b) `acks=all` + `min.insync.replicas=2` + idempotence.
- *Follow-up:* "What happens with `acks=all` and `min.insync.replicas=2` when 2 of 3 brokers die?" → producers get `NotEnoughReplicas`; writes are refused (consistency over availability).

**4. [Meta] Tumbling vs sliding.** Count of events per 10-minute window, updated every minute — which window type, and how many windows does each event belong to?
- *Solution:* Sliding (size 10 min, slide 1 min); each event is in 10 windows.
- *Follow-up:* "Cost?" → ~10× state and output rows vs tumbling.

**5. [Google] Offset reset.** A new consumer group starts on a topic with 1M existing messages. What decides where it begins?
- *Solution:* `auto.offset.reset`: `earliest` = start of retained log, `latest` = only new messages. It applies only when there's no committed offset (or it's out of range).
- *Follow-up:* "Offsets expired because retention < consumer downtime?" → falls back to the reset policy, possibly skipping data silently; monitor lag and alert early.

### Medium (6–10)

**6. [Netflix] Duplicate processing.** After a rebalance, some records are processed twice. Is this a bug?
- *Solution:* No: at-least-once is expected. Offsets are committed periodically; a rebalance/crash between process and commit replays records. Fix the *consumer* (idempotent writes, commit in `on_revoke`), not Kafka.
- *Follow-up:* "Reduce rebalances?" → static membership (`group.instance.id`), cooperative sticky assignor, avoid long processing without polling (`max.poll.interval.ms`).

**7. [Airbnb] Late data.** Dashboard shows per-minute bookings. 2% of events arrive up to 15 min late (mobile offline). Design the watermark.
- *Solution:* Watermark of 15–20 min (covers p99), `update` mode into an upsert sink so dashboards revise recent windows; a nightly batch recompute from bronze corrects anything later than the watermark. State cost ≈ 20 min of windows, which is small.
- *Follow-up:* "Result freshness vs finality?" → show "provisional" for the last 20 min, "final" after.

**8. [Databricks] Consumer lag growing.** Lag is up 10× in an hour. Diagnose.
- *Solution:* Check in order: (1) did production rate spike (bytes in vs out)? (2) lag **per partition**: one partition = hot key/stuck consumer; all partitions = under-provisioned or slow downstream. (3) rebalance storms (frequent group churn). (4) slow sink (DB locks, throttling). (5) GC/OOM restarts. Fixes: scale consumers (≤ partitions), batch writes, tune `max.poll.records`/`fetch.min.bytes`, fix the slow dependency, add partitions if needed.
- *Follow-up:* "Lag is large but consumers are idle." → they're not assigned partitions (group stuck rebalancing) or are blocked outside `poll()`.

**9. [Stripe] Poison pill.** One malformed record crashes the consumer repeatedly, blocking the partition. Handle it.
- *Solution:* Catch deserialization/validation errors, send the raw bytes + error + offset to a **dead-letter topic**, commit past it, alert on DLQ rate. Retry topics for transient errors (with delay), DLQ for permanent.
- *Follow-up:* "How do you replay DLQ messages later?" → a separate job after fixing the bug/schema; keep original headers/offset for traceability.

**10. [Amazon] Schema change.** Producer team wants to rename `amount` to `total_amount`. What compatibility problem exists and how do you roll it out?
- *Solution:* A rename = remove + add: breaks BACKWARD/FORWARD. Safe path: (1) add `total_amount` with default (compatible), producers write both; (2) migrate consumers; (3) stop writing `amount`; (4) remove it later (it needs a default to be removable under FORWARD). Or use Avro `aliases` on the reader side.
- *Follow-up:* "What if data older than the change must still be readable?" → use `*_TRANSITIVE` modes.

### Hard (11–13)

**11. [FinTech] Exactly-once end-to-end.** Kafka → Spark → Postgres balance table. A crash must never double-apply a payment.
- *Solution:* Make the write idempotent: `INSERT … ON CONFLICT (payment_id) DO NOTHING` plus `UPDATE balance` in the **same DB transaction** as an inserted ledger row (the unique `payment_id` guard makes replays no-ops). Or store `(topic, partition, offset)` in the DB in the same transaction and skip offsets already applied.
- *Follow-up:* "Batch of 1000 fails halfway." → transaction rolls back entirely; Spark/Kafka retries the same offsets; idempotency covers the replay.

**12. [Uber] Idle input stalls the watermark.** You union/join two Kafka topics. Topic A is live; topic B has had no events for an hour. Windows on the combined stream stop finalizing. Why, and what are the fixes?
- *HINT:* How does Spark combine the watermarks of several inputs?
- *Solution:* With multiple input streams, Spark's global watermark is by default the **minimum** of the individual stream watermarks (`spark.sql.streaming.multipleWatermarkPolicy = min`). An idle stream's watermark stops advancing, so it holds back everything. (Within a *single* stream, the watermark follows the max event time across all its partitions, so an idle partition alone does not stall it.) Fixes: have producers send periodic heartbeat events; set the policy to `max` (windows finalize, but you drop more late data from the slow stream); or run separate pipelines.
- *Follow-up:* "How does Flink handle it?" → per-source idleness detection (`WatermarkStrategy.withIdleness`) marks a quiet source idle so it no longer holds back the watermark.

**13. [Netflix] Reprocessing 3 days.** A bug in your enrichment logic corrupted 3 days of Delta silver. Fix without data loss and without stopping live traffic.
- *Solution:* Bronze has raw events (that's why you keep it). Run a *batch* backfill job over the 3-day bronze range with fixed logic into a staging table; validate counts/checksums vs expectations; `MERGE`/`REPLACE WHERE` into silver for that range; ensure the live stream's idempotent MERGE won't be overwritten by stale versions (use event version/LSN). Rolling back a bad write is also possible with Delta `RESTORE`.
- *Follow-up:* "What if Kafka retention is only 24 h and bronze doesn't exist?" → You can't; that's the case for keeping a raw bronze copy.

---

## Part C — Interview Simulations

### Sim 1 — "Design a real-time fraud detection pipeline using Kafka + Spark."

**Clarify:** Decision latency (block the payment inline < 100 ms, or flag within seconds)? Volume (TPS)? Which signals (velocity, geo, device)? Label delay? Fail-open or closed?

**Key insight to say first:** two paths. An **inline path** (API → scoring service with an in-memory model and a fast feature store) for sub-100 ms decisions, and a **streaming path** (Kafka → Spark/Flink) that maintains rolling features and enriches data for training and slower detection. Spark micro-batches (≥ ~1 s) are *not* suitable for the inline decision.

```
 Payments API ──► Scoring svc ──► decision (<100ms) ──► Kafka "decisions"
      │              ▲ features (Redis)
      ▼              │
  Kafka "transactions" (key=card_id, 24+ partitions, RF=3)
      │
 Spark Structured Streaming: dedup(event_id) → sliding-window features (1m/10m/1h per card) → write Redis + Delta(offline)
      │
 Delta bronze/silver/gold → training (point-in-time) → model registry → shadow → canary
```
- **Keying:** by `card_id` → per-card ordering, local window state.
- **Windows:** sliding 10 min (slide 10 s) for velocity; watermark 2 min; `update` output to an upsert sink (Redis).
- **Exactly-once effect:** idempotent Redis `SET` with event-time guard; Delta MERGE keyed by `txn_id`.
- **Late/duplicate events:** dedup on `txn_id` within watermark; a nightly recompute from bronze corrects offline features.
- **Failures:** model/feature timeout → rules fallback + `degraded` flag; Kafka outage → API buffers or fails open for low-risk.
- **Monitoring:** p99 latency, lag per partition, approval/decline rate drift, feature null rate, DLQ depth.

| Evaluator is looking for | Green flag | Red flag |
|---|---|---|
| Latency realism | Separates inline scoring from streaming feature path | Puts Spark micro-batch in the 100 ms path |
| Kafka design | Key = card, partition count reasoning, RF/ISR | Random keys; ignores ordering |
| Correctness | Idempotent sinks, dedup, watermark/late-data policy | "Exactly-once, Kafka handles it" |
| Operations | Lag, DLQ, fallback, shadow/canary | No failure story |

### Sim 2 — "Your Kafka consumer lag is growing. Diagnose and fix."

**Think out loud (the order matters):**
1. **Quantify:** total lag, trend, and **per-partition** distribution (`kafka-consumer-groups --describe`).
2. **Rate comparison:** messages/sec in vs out. If produce rate grew → capacity problem; if consume rate fell → consumer problem.
3. **Pattern → cause**

| Pattern | Likely cause | Fix |
|---|---|---|
| One partition huge | Hot key / skew; one stuck consumer | Salt key, isolate hot tenant, check that consumer's errors |
| All partitions growing, consumers CPU-bound | Under-provisioned | Add consumers (≤ partitions), optimize processing, batch work |
| All growing, consumers idle/waiting | Slow downstream (DB, API) | Batch writes, async IO, backpressure, scale the sink |
| Lag sawtooths / group keeps rebalancing | Long processing > `max.poll.interval.ms`, flapping pods | Lower `max.poll.records`, static membership, cooperative assignor |
| Lag jumps after deploy | Offsets reset or consumer restarted from `earliest` | Check commit logic, `auto.offset.reset` |

4. **Immediate relief:** scale out (if partitions allow), temporarily drop non-critical processing, increase `fetch`/batch sizes. **Prevention:** lag alerts as *time lag* (`now − message timestamp`), autoscaling on lag (KEDA), load tests at 3× peak.
5. **Don't:** "just add partitions" (breaks key ordering, and won't fix a slow sink).

**Green flags:** looks at per-partition lag first; separates producer-rate vs consumer-rate; knows about rebalances and `max.poll.interval.ms`; proposes time-based lag alerts. **Red flags:** immediately says "increase partitions" or "increase consumers" without diagnosing.

---

## Chapter 11 Cheat Sheet

- Ordering is per **partition**; key = the entity that needs ordering. More partitions later = keys move.
- Durable producer: `acks=all`, RF=3, `min.insync.replicas=2`, `enable.idempotence=true`; always `flush()`; check delivery callbacks.
- Consumers: manual commit **after** processing; `max consumers = partitions`; make processing idempotent; commit in `on_revoke`.
- Exactly-once = at-least-once + idempotent/transactional writes.
- Schemas: add fields **with defaults**; use `BACKWARD_TRANSITIVE`/`FULL_TRANSITIVE` for long-lived data.
- Event time ≠ processing time. Watermark = max event time − lateness; late data is dropped (with a one-batch lag in Spark).
- Output modes: append (final only, needs watermark), update (changed rows), complete (everything).
- Stateful operators need watermarks (dedup, windows, joins) or state grows forever. Never share/delete checkpoints casually.
- Lag debugging: per-partition first; produce rate vs consume rate; rebalances; slow sink.

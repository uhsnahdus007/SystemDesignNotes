# Chapter 15: Data Modeling for Data Engineering

> **Verification status.** Every SQL block marked ✅ **RAN** was executed in DuckDB 1.5 and the output shown is real. Spark/Delta code marked ⚠️ **NOT RUN** needs a Spark cluster, so treat it as a carefully written pattern and run it yourself. DuckDB differs from Delta/Snowflake in small ways (e.g. `generate_series`, `->>` JSON syntax); I call these out where they matter.

---

## Part A — Concept + Code

### A1. Normalization: 1NF → 2NF → 3NF  ✅ RAN

**What / why:** Normalization removes redundancy so each fact is stored once, which prevents *update anomalies*. OLTP systems normalize; analytics systems deliberately denormalize (A2).

**Step 0: Unnormalized (UNF).** Products are packed into one column. This violates 1NF.

```sql
CREATE TABLE orders_unf (order_id INT, order_date DATE, customer_id INT, customer_name VARCHAR,
  customer_city VARCHAR, products VARCHAR, qtys VARCHAR);
INSERT INTO orders_unf VALUES
 (1,'2024-03-01',10,'Asha','Pune','P1,P2','2,1'),
 (2,'2024-03-02',11,'Bob','Austin','P2','5'),
 (3,'2024-03-03',10,'Asha','Pune','P3','1');
```
```
order_id | order_date | customer_id | customer_name | customer_city | products | qtys
1 | 2024-03-01 | 10 | Asha | Pune | P1,P2 | 2,1
2 | 2024-03-02 | 11 | Bob | Austin | P2 | 5
3 | 2024-03-03 | 10 | Asha | Pune | P3 | 1
```
> **WRONG:** Querying "how many orders contain P2?" needs string splitting (`LIKE '%P2%'` also matches `P20`). Atomic values are the point of 1NF.

**1NF — atomic values, one row per order line, key = (order_id, product_id):**
```sql
CREATE TABLE orders_1nf (order_id INT, order_date DATE, customer_id INT, customer_name VARCHAR,
  customer_city VARCHAR, product_id VARCHAR, product_name VARCHAR, category VARCHAR, category_manager VARCHAR,
  qty INT, unit_price DECIMAL(8,2), PRIMARY KEY(order_id, product_id));
INSERT INTO orders_1nf VALUES
 (1,'2024-03-01',10,'Asha','Pune','P1','Keyboard','Peripherals','Rao',2,25.00),
 (1,'2024-03-01',10,'Asha','Pune','P2','Mouse','Peripherals','Rao',1,15.00),
 (2,'2024-03-02',11,'Bob','Austin','P2','Mouse','Peripherals','Rao',5,15.00),
 (3,'2024-03-03',10,'Asha','Pune','P3','Monitor','Displays','Kim',1,200.00);
```
Problem left: `customer_name/city` depend only on `order_id` (part of the key), and `product_name/category` depend only on `product_id`. Those are **partial dependencies**.

**2NF — remove partial dependencies on the composite key:**
```sql
CREATE TABLE orders_2nf_order (order_id INT PRIMARY KEY, order_date DATE, customer_id INT, customer_name VARCHAR, customer_city VARCHAR);
CREATE TABLE products_2nf (product_id VARCHAR PRIMARY KEY, product_name VARCHAR, category VARCHAR, category_manager VARCHAR, unit_price DECIMAL(8,2));
CREATE TABLE order_lines_2nf (order_id INT, product_id VARCHAR, qty INT, PRIMARY KEY(order_id, product_id));
```
Problem left: `customer_city` depends on `customer_id` (not the key `order_id`), and `category_manager` depends on `category` (not `product_id`). Those are **transitive dependencies**.

**3NF — remove transitive dependencies:**
```sql
CREATE TABLE customers  (customer_id INT PRIMARY KEY, customer_name VARCHAR, customer_city VARCHAR);
CREATE TABLE categories (category VARCHAR PRIMARY KEY, category_manager VARCHAR);
CREATE TABLE products   (product_id VARCHAR PRIMARY KEY, product_name VARCHAR, category VARCHAR REFERENCES categories(category), unit_price DECIMAL(8,2));
CREATE TABLE orders     (order_id INT PRIMARY KEY, order_date DATE, customer_id INT REFERENCES customers(customer_id));
CREATE TABLE order_lines(order_id INT REFERENCES orders(order_id), product_id VARCHAR REFERENCES products(product_id), qty INT, PRIMARY KEY(order_id, product_id));
```
(The full script also loads each table from `orders_1nf` with `INSERT … SELECT DISTINCT`.)

**Trade-off demo.** Asha (customer 10) moves to Mumbai.

```sql
-- Flat table: careless partial update
UPDATE orders_1nf SET customer_city='Mumbai' WHERE customer_id=10 AND order_id=1;
SELECT DISTINCT customer_id, customer_city FROM orders_1nf WHERE customer_id=10 ORDER BY 2;
-- 3NF: one-row update
UPDATE customers SET customer_city='Mumbai' WHERE customer_id=10;
```
```
flat table, partial update -> inconsistent:      3NF, one-row update -> consistent everywhere:
customer_id | customer_city                      order_id | customer_city
10 | Mumbai                                       1 | Mumbai
10 | Pune                                         3 | Mumbai
```
Same revenue report on both shapes (identical results, very different query shape):
```sql
-- 3NF: 5-table join
SELECT c.customer_name, cat.category, SUM(ol.qty*p.unit_price) AS revenue
FROM order_lines ol JOIN orders o USING(order_id) JOIN customers c USING(customer_id)
JOIN products p USING(product_id) JOIN categories cat USING(category) GROUP BY 1,2 ORDER BY 1,2;
-- Flat: no joins
SELECT customer_name, category, SUM(qty*unit_price) AS revenue FROM orders_1nf GROUP BY 1,2 ORDER BY 1,2;
```
```
customer_name | category    | revenue
Asha | Displays    | 200.00
Asha | Peripherals | 65.00
Bob  | Peripherals | 75.00
```

| | Normalized (3NF) | Denormalized |
|---|---|---|
| Writes/updates | Safe, one place | Anomalies |
| Reads/analytics | Many joins | Fast scans |
| Storage | Smaller | Redundant (cheap in columnar) |
| Best for | OLTP | OLAP / warehouses |

> **KEY INSIGHT:** "Normalize for writes, denormalize for reads." A warehouse is read-heavy and loaded by pipelines you control, so anomalies are prevented by the pipeline, not by the schema.

---

### A2. Star Schema  ✅ RAN

**What / why:** A **fact table** (numeric measurements at a declared *grain*) surrounded by **dimension tables** (descriptive context). Few joins, predictable SQL, great for BI tools.

**Design:** E-commerce. **Grain = one order line item.** State this out loud in interviews.

```sql
CREATE TABLE dim_date (date_key INT PRIMARY KEY, full_date DATE NOT NULL, year INT, quarter INT, month INT, day_name VARCHAR, is_weekend BOOLEAN);
CREATE TABLE dim_customer (customer_key INT PRIMARY KEY, customer_id VARCHAR, name VARCHAR, segment VARCHAR);
CREATE TABLE dim_product  (product_key INT PRIMARY KEY, sku VARCHAR, product_name VARCHAR, category VARCHAR, brand VARCHAR);
CREATE TABLE dim_geography(geo_key INT PRIMARY KEY, city VARCHAR, state VARCHAR, country VARCHAR);
-- JUNK dimension: low-cardinality flags collapsed into ONE dimension (2x2x2 = 8 rows)
CREATE TABLE dim_order_flags (flag_key INT PRIMARY KEY, is_gift BOOLEAN, is_express BOOLEAN, is_international BOOLEAN);
CREATE TABLE fact_orders (
  order_number VARCHAR NOT NULL,                 -- DEGENERATE dimension: an identifier with no attributes, kept in the fact
  line_number  INT NOT NULL,
  order_date_key    INT NOT NULL REFERENCES dim_date(date_key),   -- ROLE-PLAYING: one dim_date, three roles
  ship_date_key     INT          REFERENCES dim_date(date_key),
  delivery_date_key INT          REFERENCES dim_date(date_key),
  customer_key INT NOT NULL REFERENCES dim_customer(customer_key),
  product_key  INT NOT NULL REFERENCES dim_product(product_key),
  geo_key      INT NOT NULL REFERENCES dim_geography(geo_key),
  flag_key     INT NOT NULL REFERENCES dim_order_flags(flag_key),
  quantity INT, unit_price DECIMAL(10,2), discount_amount DECIMAL(10,2), net_amount DECIMAL(12,2),
  PRIMARY KEY (order_number, line_number));
```
Loaded with 14 fact rows (12 orders; two not yet fully shipped), 4 customers, 4 products, 3 geographies, and a date dimension for Q1 2024. Junk dimension populated deterministically:
```sql
INSERT INTO dim_order_flags SELECT row_number() OVER (ORDER BY g,e,i), g, e, i FROM
  (VALUES (false),(true)) a(g), (VALUES (false),(true)) b(e), (VALUES (false),(true)) c(i);
```

**Five analytical queries (outputs real):**

```sql
-- Q1 monthly revenue
SELECT d.year, d.month, SUM(f.net_amount) AS revenue
FROM fact_orders f JOIN dim_date d ON f.order_date_key=d.date_key GROUP BY 1,2 ORDER BY 1,2;
```
```
year | month | revenue
2024 | 1 | 245.00
2024 | 2 | 1750.00
2024 | 3 | 2560.00
```
```sql
-- Q2 top category x country
SELECT p.category, g.country, SUM(f.net_amount) AS revenue, SUM(f.quantity) AS units
FROM fact_orders f JOIN dim_product p USING(product_key) JOIN dim_geography g USING(geo_key)
GROUP BY 1,2 ORDER BY revenue DESC LIMIT 5;
```
```
category | country | revenue | units
Computers | US | 1700.00 | 2
Computers | IN | 900.00 | 1
Computers | DE | 900.00 | 1
Displays | DE | 400.00 | 2
Displays | US | 380.00 | 2
```
```sql
-- Q3 average order value by segment (COUNT DISTINCT on the degenerate dimension!)
SELECT c.segment, COUNT(DISTINCT f.order_number) AS orders,
       ROUND(SUM(f.net_amount)/COUNT(DISTINCT f.order_number),2) AS aov
FROM fact_orders f JOIN dim_customer c USING(customer_key) GROUP BY 1 ORDER BY 1;
```
```
segment | orders | aov
Premium | 6 | 485.83
Standard | 6 | 273.33
```
```sql
-- Q4 slice by the junk dimension
SELECT fl.is_express, fl.is_international, COUNT(DISTINCT f.order_number) AS orders, SUM(f.net_amount) AS revenue
FROM fact_orders f JOIN dim_order_flags fl USING(flag_key) GROUP BY 1,2 ORDER BY 1,2;
```
```
is_express | is_international | orders | revenue
False | False | 5 | 1300.00
False | True  | 3 | 1410.00
True  | False | 2 | 45.00
True  | True  | 2 | 1800.00
```
```sql
-- Q5 role-playing dimension: same dim_date joined three times with different aliases
SELECT f.order_number, od.full_date AS order_dt, sd.full_date AS ship_dt, dd.full_date AS delivery_dt,
       date_diff('day', od.full_date, dd.full_date) AS days_to_deliver
FROM fact_orders f JOIN dim_date od ON f.order_date_key=od.date_key
LEFT JOIN dim_date sd ON f.ship_date_key=sd.date_key LEFT JOIN dim_date dd ON f.delivery_date_key=dd.date_key
WHERE f.line_number=1 ORDER BY f.order_number;
```
```
order_number | order_dt   | ship_dt    | delivery_dt | days_to_deliver
O1  | 2024-01-05 | 2024-01-06 | 2024-01-09 | 4
O2  | 2024-01-10 | 2024-01-11 | 2024-01-15 | 5
O8  | 2024-03-10 | 2024-03-11 | NULL       | NULL
O9  | 2024-03-12 | NULL       | NULL       | NULL
(…12 orders total; the rest elided)
```
> **COMMON MISTAKE (Q3):** `COUNT(*)` counts *lines*, not orders; `AVG(net_amount)` gives average *line* value. Always `COUNT(DISTINCT order_number)` for order-level metrics, because the fact grain is the line.

> **COMMON MISTAKE (Q5):** `NULL` foreign keys (O8, O9 not yet delivered) force `LEFT JOIN`s and silently break `INNER JOIN` reports. Production fix: point unknown/future dates at a reserved dimension row (e.g. `date_key = -1`, "Not yet occurred") so every FK is `NOT NULL`.

> **Why star schemas are fast:** each query touches one large fact table scan plus small dimension lookups. Dimensions are tiny and broadcastable; columnar storage reads only the needed measure/key columns.

**The four dimension patterns you were asked about**

| Pattern | What | Here |
|---|---|---|
| Junk dimension | Combine unrelated low-cardinality flags into one dimension | `dim_order_flags` (8 rows instead of 3 extra columns/dims) |
| Degenerate dimension | Identifier with no descriptive attributes, stored in the fact | `order_number` |
| Role-playing dimension | One physical dimension used several times | `dim_date` as order/ship/delivery |
| Conformed dimension | Shared dimension used by several facts | `dim_customer` across orders and returns |

> **In Real Production:** Surrogate integer keys (not natural keys) join facts to dims; a pipeline step does the key lookup. Late-arriving dimension rows get an "inferred member" placeholder.

---

### A3. SCD Type 2: Full Implementation  ✅ RAN

**What / why:** Type 2 keeps history by adding a new dimension row per change, with validity dates. Facts join to the version that was true *when the event happened*.

```sql
CREATE SEQUENCE sk_seq START 1;
CREATE TABLE dim_customer_scd2 (
  surrogate_key BIGINT PRIMARY KEY DEFAULT nextval('sk_seq'),
  natural_key   VARCHAR NOT NULL,
  name VARCHAR, city VARCHAR, segment VARCHAR,
  valid_from DATE NOT NULL, valid_to DATE NOT NULL DEFAULT DATE '9999-12-31',
  is_current BOOLEAN NOT NULL DEFAULT TRUE);
CREATE TABLE stg_customer (natural_key VARCHAR, name VARCHAR, city VARCHAR, segment VARCHAR);
```

**Initial load** (as of 2023-01-01, 4 customers):
```sql
INSERT INTO dim_customer_scd2 (natural_key,name,city,segment,valid_from)
SELECT natural_key,name,city,segment,DATE '2023-01-01' FROM stg_customer;
```

**Incremental load: new, changed, unchanged, deleted.** I used the portable two-statement form (works on any engine) wrapped in a function. `{load_date}` is a parameter.

```sql
-- Step 1: close rows that CHANGED or whose key was DELETED from the source
UPDATE dim_customer_scd2 d SET valid_to = DATE '{load_date}' - 1, is_current = FALSE
WHERE d.is_current
  AND ( NOT EXISTS (SELECT 1 FROM stg_customer s WHERE s.natural_key=d.natural_key)          -- deleted
     OR EXISTS (SELECT 1 FROM stg_customer s WHERE s.natural_key=d.natural_key
                AND (s.name,s.city,s.segment) IS DISTINCT FROM (d.name,d.city,d.segment)) ); -- changed, NULL-safe

-- Step 2: insert NEW keys and the new version of CHANGED keys (anything with no current row now)
INSERT INTO dim_customer_scd2 (natural_key,name,city,segment,valid_from)
SELECT s.natural_key,s.name,s.city,s.segment, DATE '{load_date}'
FROM stg_customer s
WHERE NOT EXISTS (SELECT 1 FROM dim_customer_scd2 d WHERE d.natural_key=s.natural_key AND d.is_current);
```
Unchanged rows match neither statement, so they are untouched.

**Scenario 2023-06-01:** C1 moves Pune→Mumbai (changed), C2 and C4 unchanged, C3 absent (deleted), C5 new. I then ran the *same* load a second time to prove idempotency:
```
idempotent re-run: 6 -> 6

sk | nk | city   | valid_from | valid_to   | is_current
1 | C1 | Pune   | 2023-01-01 | 2023-05-31 | False
5 | C1 | Mumbai | 2023-06-01 | 9999-12-31 | True
2 | C2 | Austin | 2023-01-01 | 9999-12-31 | True
3 | C3 | Berlin | 2023-01-01 | 2023-05-31 | False
4 | C4 | Pune   | 2023-01-01 | 9999-12-31 | True
6 | C5 | Delhi  | 2023-06-01 | 9999-12-31 | True
```
**Scenario 2024-01-10:** C1 moves to Delhi; C3 returns.
```
sk | nk | city   | valid_from | valid_to   | is_current
1 | C1 | Pune   | 2023-01-01 | 2023-05-31 | False
5 | C1 | Mumbai | 2023-06-01 | 2024-01-09 | False
7 | C1 | Delhi  | 2024-01-10 | 9999-12-31 | True
3 | C3 | Berlin | 2023-01-01 | 2023-05-31 | False
8 | C3 | Berlin | 2024-01-10 | 9999-12-31 | True
```
> **KEY INSIGHT / known gap:** C3 has *no row* covering 2023-06-01 → 2024-01-09. A fact dated in that gap finds no dimension row, and an inner join silently drops it. Decide a policy up front: either model deletions as a closed row with `is_deleted = TRUE` that still spans the gap, or keep the last known version valid until a new one arrives. State the policy in your answer.

**Query pattern: "What was C1's city on 2023-06-15?"**
```sql
SELECT city FROM dim_customer_scd2 WHERE natural_key='C1' AND DATE '2023-06-15' BETWEEN valid_from AND valid_to;
```
```
city
Mumbai
```
**Point-in-time join with a fact table** (4 sales: C1 on 2023-03-01 $100, 2023-07-01 $200, 2024-02-01 $300; C3 on 2023-02-01 $50):

```sql
-- WRONG: join to the current row only -> history rewritten
SELECT d.city, SUM(f.amount) AS revenue FROM fact_sales f
JOIN dim_customer_scd2 d ON f.natural_key=d.natural_key AND d.is_current GROUP BY 1 ORDER BY 1;
-- RIGHT: point-in-time
SELECT d.city, SUM(f.amount) AS revenue FROM fact_sales f
JOIN dim_customer_scd2 d ON f.natural_key=d.natural_key AND f.sale_date BETWEEN d.valid_from AND d.valid_to GROUP BY 1 ORDER BY 1;
```
```
WRONG                    RIGHT
city   | revenue         city   | revenue
Berlin | 50.00           Berlin | 50.00
Delhi  | 600.00          Delhi  | 300.00
                         Mumbai | 200.00
                         Pune   | 100.00
```
The wrong query credits all $600 of C1's spend to Delhi, even though $300 was made while living in Pune and Mumbai.

**The same logic as a single Delta `MERGE`** ⚠️ NOT RUN (DuckDB has `MERGE`, and a trivial upsert worked there, but I did not run this SCD2 form on Delta). The classic trick is a staged union: changed rows appear twice, once with the real key to close the old row and once with a NULL merge key to force an insert.

```sql
MERGE INTO dim_customer_scd2 AS t
USING (
  SELECT s.natural_key AS merge_key, s.* FROM stg_customer s            -- rows that may match/close
  UNION ALL
  SELECT NULL AS merge_key, s.* FROM stg_customer s                      -- rows to insert as new versions
  JOIN dim_customer_scd2 d ON d.natural_key = s.natural_key AND d.is_current
  WHERE (s.name, s.city, s.segment) IS DISTINCT FROM (d.name, d.city, d.segment)
) AS s
ON t.natural_key = s.merge_key AND t.is_current
WHEN MATCHED AND (s.name, s.city, s.segment) IS DISTINCT FROM (t.name, t.city, t.segment) THEN
  UPDATE SET valid_to = current_date() - 1, is_current = false
WHEN NOT MATCHED THEN
  INSERT (natural_key, name, city, segment, valid_from, valid_to, is_current)
  VALUES (s.natural_key, s.name, s.city, s.segment, current_date(), DATE '9999-12-31', true);
-- Deleted keys need a separate clause: WHEN NOT MATCHED BY SOURCE AND t.is_current THEN UPDATE ... (Delta/Databricks syntax)
```
Note the `s.*` in the union expands consistently only if both branches list identical columns; spell the column list out in production.

---

### A4. Data Vault 2.0  ✅ RAN

**What / why:** An insert-only, auditable modelling style for the *raw/integration* layer. Business keys live in **Hubs**, relationships in **Links**, descriptive history in **Satellites**. Great for many source systems and changing schemas; you build marts (star schemas) on top.

```sql
CREATE TABLE hub_customer (customer_hk VARCHAR PRIMARY KEY, customer_bk VARCHAR UNIQUE, load_ts TIMESTAMP, record_source VARCHAR);
CREATE TABLE hub_product  (product_hk  VARCHAR PRIMARY KEY, product_bk  VARCHAR UNIQUE, load_ts TIMESTAMP, record_source VARCHAR);
CREATE TABLE hub_order    (order_hk    VARCHAR PRIMARY KEY, order_bk    VARCHAR UNIQUE, load_ts TIMESTAMP, record_source VARCHAR);
CREATE TABLE link_order_line (order_line_hk VARCHAR PRIMARY KEY, order_hk VARCHAR, customer_hk VARCHAR, product_hk VARCHAR,
                              load_ts TIMESTAMP, record_source VARCHAR);
CREATE TABLE sat_customer (customer_hk VARCHAR, load_ts TIMESTAMP, hashdiff VARCHAR, name VARCHAR, city VARCHAR,
                           record_source VARCHAR, PRIMARY KEY (customer_hk, load_ts));
CREATE TABLE sat_order_line (order_line_hk VARCHAR, load_ts TIMESTAMP, hashdiff VARCHAR, qty INT, price DECIMAL(8,2),
                             record_source VARCHAR, PRIMARY KEY (order_line_hk, load_ts));
```
**Loading patterns (all insert-only and idempotent):**
- **Hash key:** `md5(upper(trim(business_key)))` (for composite keys join parts with a delimiter such as `'||'` so `('A','BC')` ≠ `('AB','C')`).
- **Hubs/Links:** insert only keys that do not exist yet (`NOT EXISTS`).
- **Satellites:** insert a new row only when the **hashdiff** (hash of descriptive columns) differs from the *latest* satellite row for that key.

Test: load 1 (3 staging rows), re-run the identical load, then load 2 (Asha's city changes to Mumbai; a new order O3 arrives from a *second* source system `erp`).
```
after load 1:               {'hub_customer': 2, 'hub_product': 2, 'hub_order': 2, 'link_order_line': 3, 'sat_customer': 2, 'sat_order_line': 3}
after re-run (idempotent):  {'hub_customer': 2, 'hub_product': 2, 'hub_order': 2, 'link_order_line': 3, 'sat_customer': 2, 'sat_order_line': 3}
after load 2:               {'hub_customer': 3, 'hub_product': 2, 'hub_order': 3, 'link_order_line': 4, 'sat_customer': 4, 'sat_order_line': 4}
```
Satellite keeps history; a current view needs "latest satellite row":
```
customer_bk | city   | load_ts
C1 | Pune   | 2024-01-01
C1 | Mumbai | 2024-02-01
C2 | Austin | 2024-01-01
C3 | Berlin | 2024-02-01
```
```sql
WITH cur AS (SELECT *, row_number() OVER (PARTITION BY customer_hk ORDER BY load_ts DESC) rn FROM sat_customer)
SELECT hc.customer_bk, c.city AS current_city, p.product_bk, sol.qty
FROM link_order_line l JOIN hub_customer hc USING(customer_hk) JOIN cur c ON c.customer_hk=l.customer_hk AND c.rn=1
JOIN hub_product p USING(product_hk) JOIN sat_order_line sol USING(order_line_hk) ORDER BY 1,3;
```
```
C1 | Mumbai | P1 | 2
C1 | Mumbai | P2 | 1
C2 | Austin | P2 | 5
C3 | Berlin | P1 | 1
```
> **COMMON MISTAKE:** If the same *new* business key arrives from two sources in one batch with different `record_source`, a naive `SELECT DISTINCT` inserts two hub rows with the same hash key and violates the PK. Pick one source per key (`row_number()` over `load_ts, source priority`) when loading hubs.

> **KEY INSIGHT:** The vault answers "what did we know, from where, and when?" with full lineage. The price is many joins (4 tables for a simple report above), which is why consumers query **marts built on top**, not the vault itself.

**When Data Vault beats Kimball:** many source systems that change often; strict audit/lineage needs; parallel team loading; you want raw history preserved before business rules are decided. **When Kimball wins:** one or a few stable sources, small team, fast time-to-BI.

---

### A5. Medallion Architecture  ✅ RAN (DuckDB logic) / ⚠️ NOT RUN (PySpark)

| Layer | Contract | Mutability |
|---|---|---|
| **Bronze** | Raw as received + ingest metadata; schema-on-read; never lose data | Append-only |
| **Silver** | Parsed, typed, cleaned, deduplicated, conformed keys; bad rows quarantined | Upserted |
| **Gold** | Business-ready marts, star schemas, aggregates, features | Rebuilt/incremental |

I tested the logic on 7 raw JSON events that include an exact duplicate, a later update (`REFUNDED`), a bad quantity, a NULL city, and a padded string (`" Pune "`).

**Bronze:** payload kept raw.
```sql
CREATE TABLE bronze_orders (_ingest_id INT, _ingest_ts TIMESTAMP, _source VARCHAR, payload JSON);
```
**Silver:** parse nested JSON, explode items, keep latest ingest per `(order_id, sku)`, drop invalid lines, quarantine them.
```sql
CREATE TABLE silver_order_lines AS
WITH parsed AS (
  SELECT _ingest_id, payload->>'$.order_id' AS order_id, payload->>'$.customer.id' AS customer_id,
         payload->>'$.customer.name' AS customer_name,
         NULLIF(trim(payload->>'$.customer.city'), '') AS customer_city,
         CAST(payload->>'$.ts' AS TIMESTAMP) AS order_ts, payload->>'$.status' AS status,
         unnest(from_json(payload->'$.items', '["JSON"]')) AS item
  FROM bronze_orders),
flat AS (SELECT *, item->>'$.sku' AS sku, CAST(item->>'$.qty' AS INT) AS qty,
                CAST(item->>'$.price' AS DECIMAL(10,2)) AS price FROM parsed),
latest AS (SELECT *, row_number() OVER (PARTITION BY order_id, sku ORDER BY _ingest_id DESC) AS rn FROM flat)
SELECT order_id, sku, customer_id, customer_name, coalesce(customer_city,'UNKNOWN') AS customer_city,
       order_ts, status, qty, price, qty*price AS line_amount
FROM latest WHERE rn=1 AND qty > 0;
```
```
order_id | sku | customer_id | customer_name | customer_city | order_ts            | status    | qty | price  | line_amount
O1 | K1 | C1 | Asha | Pune    | 2024-03-01 10:00:00 | REFUNDED  | 2 | 25.00  | 50.00
O1 | M1 | C1 | Asha | Pune    | 2024-03-01 10:00:00 | REFUNDED  | 1 | 15.00  | 15.00
O2 | M1 | C2 | Bob  | Austin  | 2024-03-02 09:00:00 | PAID      | 5 | 15.00  | 75.00
O3 | D1 | C3 | Chen | Berlin  | 2024-03-03 12:00:00 | CANCELLED | 1 | 200.00 | 200.00
O5 | L1 | C4 | Dia  | UNKNOWN | 2024-03-05 08:00:00 | PAID      | 1 | 900.00 | 900.00
quarantine: O4 | qty<=0
```
The duplicate O2 collapsed to one row; O1 shows its *latest* status; O4 (bad qty) is quarantined, not silently dropped.

**Gold:** star schema from silver, PAID orders only.
```sql
CREATE TABLE fact_sales AS
SELECT s.order_id, c.customer_key, p.product_key, CAST(s.order_ts AS DATE) AS order_date, s.qty, s.line_amount
FROM silver_order_lines s JOIN dim_customer c USING(customer_id) JOIN dim_product p USING(sku) WHERE s.status = 'PAID';
```
```
city    | revenue | orders
UNKNOWN | 900.00  | 1
Austin  | 75.00   | 1
```
> **COMMON MISTAKE:** O1 vanished from gold revenue because its latest status is `REFUNDED`. Real finance models keep the original sale *and* add a separate negative **refund fact** (or a transaction-type column), so "gross sales" and "net sales" are both answerable. Don't overwrite financial history with the latest status.

**Full PySpark pipeline** ⚠️ NOT RUN

```python
from pyspark.sql import SparkSession, functions as F, Window
from delta.tables import DeltaTable

spark = SparkSession.builder.getOrCreate()
BRONZE, SILVER, GOLD = "lake.bronze.orders", "lake.silver.order_lines", "lake.gold"

# ---------- BRONZE: Auto Loader / structured streaming, raw JSON kept as a string ----------
(spark.readStream.format("cloudFiles").option("cloudFiles.format", "json")
  .option("cloudFiles.schemaLocation", "/chk/schema/orders").option("cloudFiles.inferColumnTypes", "false")
  .load("/landing/orders/")
  .withColumn("_ingest_ts", F.current_timestamp()).withColumn("_source_file", F.input_file_name())
  .writeStream.option("checkpointLocation", "/chk/bronze/orders").trigger(availableNow=True)
  .toTable(BRONZE))                                    # partition by ingest date if large

# ---------- SILVER: parse, clean, dedupe, MERGE ----------
bronze = spark.read.table(BRONZE)
items = (bronze
  .withColumn("item", F.explode("items"))
  .select(F.col("order_id"), F.col("customer.id").alias("customer_id"), F.col("customer.name").alias("customer_name"),
          F.coalesce(F.nullif(F.trim(F.col("customer.city")), F.lit("")), F.lit("UNKNOWN")).alias("customer_city"),
          F.to_timestamp("ts").alias("order_ts"), "status", F.col("item.sku").alias("sku"),
          F.col("item.qty").cast("int").alias("qty"), F.col("item.price").cast("decimal(10,2)").alias("price"), "_ingest_ts"))
valid, bad = items.filter("qty > 0"), items.filter("qty <= 0")
bad.write.mode("append").saveAsTable("lake.silver.quarantine_orders")

w = Window.partitionBy("order_id", "sku").orderBy(F.col("_ingest_ts").desc())
latest = valid.withColumn("rn", F.row_number().over(w)).filter("rn = 1").drop("rn", "_ingest_ts") \
              .withColumn("line_amount", F.col("qty") * F.col("price"))

(DeltaTable.forName(spark, SILVER).alias("t")
   .merge(latest.alias("s"), "t.order_id = s.order_id AND t.sku = s.sku")
   .whenMatchedUpdateAll().whenNotMatchedInsertAll().execute())

# ---------- GOLD: star schema ----------
silver = spark.read.table(SILVER)
dim_customer = silver.groupBy("customer_id").agg(F.first("customer_name").alias("name"), F.first("customer_city").alias("city")) \
                     .withColumn("customer_key", F.xxhash64("customer_id"))
dim_product  = silver.select("sku").distinct().withColumn("product_key", F.xxhash64("sku"))
fact_sales = (silver.filter("status = 'PAID'")
   .join(dim_customer.select("customer_id", "customer_key"), "customer_id")
   .join(dim_product, "sku")
   .select("order_id", "customer_key", "product_key", F.to_date("order_ts").alias("order_date"), "qty", "line_amount"))
fact_sales.write.mode("overwrite").option("replaceWhere", "order_date >= '2024-03-01'").saveAsTable(f"{GOLD}.fact_sales")
```
> **Notes on the PySpark:** `F.nullif` exists in Spark 3.5+. For older versions use `F.when(F.trim(c) == "", None).otherwise(F.trim(c))`. Hash keys (`xxhash64`) are deterministic but can collide; for dimensions that need guaranteed uniqueness, use a sequence/identity column.

> **In Real Production:** Bronze partitioned by ingest date; silver tables keep `_updated_ts`; gold built incrementally; DQ checks (row counts, null rates, uniqueness) gate each layer promotion; failures route to quarantine, not to /dev/null.

---

### A6. One Big Table (OBT)  ✅ RAN

Pre-join the star into one wide table for BI.
```sql
CREATE TABLE obt_sales AS
SELECT f.order_id, f.order_date, f.qty, f.line_amount, c.customer_id, c.name, c.city, p.sku
FROM fact_sales f JOIN dim_customer c USING(customer_key) JOIN dim_product p USING(product_key);
SELECT city, SUM(line_amount) revenue FROM obt_sales GROUP BY 1 ORDER BY 2 DESC;   -- same answer, zero joins
```
```
UNKNOWN | 900.00
Austin  | 75.00
```
| Works when | Fails when |
|---|---|
| One BI tool, stable questions, columnar engine | Dimensions change often (must rebuild/backfill the whole table) |
| Dimension attributes are small relative to facts | Many-to-many relationships explode rows |
| Speed over governance | Multiple teams need one consistent definition of "customer" |

Rule of thumb: build the star schema as the governed layer, and generate OBTs *from it* as a performance convenience.

### A7. Activity Schema  ✅ RAN

**What / why:** Model everything as one time-ordered stream of `(customer, activity, ts, features)`. Analyses become self-joins on the stream ("first X before first Y"), with no per-question join mazes.

```sql
CREATE TABLE activity_stream (activity_id VARCHAR, ts TIMESTAMP, customer VARCHAR, activity VARCHAR,
  feature_1 VARCHAR, feature_2 VARCHAR, revenue_impact DECIMAL(10,2));
-- 9 rows: visited_site / added_to_cart / completed_order for C1, C2, C3
```
```sql
-- First-touch attribution: first visit source -> first purchase
SELECT v.feature_1 AS first_visit_source, count(*) AS customers, SUM(o.revenue_impact) AS first_order_revenue
FROM (SELECT *, row_number() OVER (PARTITION BY customer ORDER BY ts) rn FROM activity_stream WHERE activity='visited_site') v
JOIN (SELECT *, row_number() OVER (PARTITION BY customer ORDER BY ts) rn FROM activity_stream WHERE activity='completed_order') o
  ON v.customer=o.customer AND v.rn=1 AND o.rn=1 AND o.ts>v.ts
GROUP BY 1 ORDER BY 1;
```
```
first_visit_source | customers | first_order_revenue
email      | 1 | 75.00
google_ads | 1 | 65.00

-- funnel from the same stream
visited_site | 3 users
added_to_cart | 2
completed_order | 2
```
Pros: tiny model, easy to extend (add an activity, no new table), great for product analytics. Cons: generic `feature_N` columns hurt readability; not ideal for financial/ledger reporting; needs conventions and discipline.

---

## Part B — Practice Problems (13)

*Try each for 15 min first. Format: scenario → HINT → solution → follow-up. These are design problems, so "complexity" means query/storage cost where relevant.*

### Easy (1–5)

**1. [Amazon] Identify the grain.** A table has one row per `(order_id, product_id, warehouse_id, shipment_id)`. State the grain, then say whether `SUM(order_total)` on it is safe.
- *HINT:* order_total repeats on each line.
- *Solution:* Grain = one row per shipped item per shipment. `order_total` is an **order-level** value repeated per row, so summing it **double counts**. Keep order-level amounts in an order-grain fact (or allocate them to lines).
- *Follow-up:* "How do you allocate shipping fees to lines?" → pro-rata by line amount, with a rounding-remainder rule so allocated amounts sum exactly to the original.

**2. [Flipkart] Fact or dimension?** Classify: `price`, `product_category`, `order_status`, `units_sold`, `customer_email`, `discount_pct`.
- *Solution:* Facts (numeric, additive or semi-additive): `units_sold`; `price` and `discount_pct` are *non-additive* attributes (store as measures but never SUM them; sum the derived amounts). Dimensions: `product_category`, `customer_email`. `order_status` is a low-cardinality attribute → junk/status dimension.
- *Follow-up:* "Is `price` a fact or a dimension attribute?" → Depends: price at time of sale = fact; list price = product dimension attribute (SCD).

**3. [Swiggy] Additivity.** For each, say additive / semi-additive / non-additive: revenue, account balance, unit price, distinct customers.
- *Solution:* revenue = additive; account balance = semi-additive (sum across accounts, **not** across time, take latest/average); unit price = non-additive; distinct customers = non-additive (not summable across groups).
- *Follow-up:* "How do you roll up distinct customers across months?" → store HLL sketches or keep the lowest grain.

**4. [Meta] Spot the 1NF violation.** `users(user_id, name, phone_numbers)` where `phone_numbers = "555-1,555-2"`. Fix it.
- *Solution:* Create `user_phones(user_id, phone_number, phone_type)` with PK `(user_id, phone_number)`.
- *Follow-up:* "In a columnar warehouse, would you ever keep an array column?" → Yes for analytics on semi-structured data (arrays/structs), when you rarely join on individual elements.

**5. [Stripe] Choose the key.** Should `dim_customer`'s primary key be the source `customer_id`? Why/why not?
- *Solution:* Use a **surrogate key**: supports SCD2 (multiple versions per natural key), insulates you from source key changes/reuse, integrates multiple sources, small integer joins.
- *Follow-up:* "Hash or sequence?" → Hash: deterministic, parallel-friendly, no lookup needed. Sequence: smaller, collision-free. Pick per engine and team.

### Medium (6–10)

**6. [Uber] Modeling cancellations.** A rider can cancel before a driver is assigned, so the driver is unknown. How do you model it in `fact_trip`?
- *HINT:* Never leave a NULL FK.
- *Solution:* Add an "Unknown/Not assigned" row in `dim_driver` (`driver_key = -1`), and a `dim_trip_status` with cancel reasons. FK stays `NOT NULL`, inner joins stay safe, and cancellations are first-class rows.
- *Follow-up:* "Cancel rate by city?" → `SUM(status='cancelled')/COUNT(*)` grouped by pickup zone's city.

**7. [Netflix] SCD choice.** `dim_title` has `maturity_rating`, `title_name`, `genre`, `licensed_region`. Which SCD type for each?
- *Solution:* `title_name` typos → Type 1 (overwrite). `maturity_rating` change (content re-rated) → Type 2 if analytics must reflect the rating at viewing time. `genre` → Type 1 or multi-valued bridge. `licensed_region` → separate **bridge/fact** (licensing windows are many-to-many and time-bounded), not an SCD attribute.
- *Follow-up:* "What if only the latest value matters for 95% of queries?" → add a Type-6-style `current_*` column alongside the historical one.

**8. [Airbnb] Many-to-many.** A listing has many amenities; a guest searches by amenity. Model it.
- *Solution:* `dim_listing` + `dim_amenity` + **bridge table** `bridge_listing_amenity(listing_key, amenity_key)`. Avoid fan-out in aggregations (sum measures *before* joining the bridge, or use weighting factors).
- *Follow-up:* "Revenue by amenity double counts. Why?" → one booking joins to N amenities, so revenue repeats N times. Aggregate revenue per listing first, or allocate with a weight of 1/N.

**9. [Google] Late-arriving dimension.** A fact references `customer_id = C99`, which is not in `dim_customer` yet.
- *Solution:* Insert an **inferred member** row (`customer_id=C99`, attributes `UNKNOWN`, `is_inferred=TRUE`) and use its key; when the real record arrives, update that row in place (Type 1 fix for the placeholder), or create a new SCD2 version if its validity must start from the true date.
- *Follow-up:* "Late-arriving *fact* with an old date?" → look up the dimension version valid at that date (`BETWEEN valid_from AND valid_to`).

**10. [Walmart] Snapshot vs transaction fact.** Inventory by store/day: which fact type? Why?
- *Solution:* **Periodic snapshot** (one row per store × SKU × day) for on-hand quantity, plus a **transaction fact** for receipts/sales/adjustments. Balances are semi-additive, so you can't derive "on-hand yesterday" cheaply from transactions alone at scale.
- *Follow-up:* "Storage is exploding (10M SKU × 5k stores)." → snapshot only on change (accumulating/"effective-dated" rows), or only for active SKUs.

### Hard (11–13)

**11. [Stripe] Ledger modeling.** Design a model so finance can prove revenue = payments − refunds − fees, with reconcilable history.
- *Solution:* An immutable **double-entry ledger fact** (`entry_id, txn_id, account_key, debit, credit, currency, effective_ts, posted_ts`). Never update; corrections are reversing entries. Dimensions: account, currency, entry type. Derived facts (revenue, net) are views over the ledger, and a daily reconciliation compares ledger sums with the payment processor's file.
- *Follow-up:* "Multi-currency?" → store original amount + currency, plus `fx_rate_id` referencing a rate dimension keyed by date, so restatements are reproducible.

**12. [Amazon] Bi-temporal.** Finance restates January data in March. Dashboards must show both "what we knew in February" and "the corrected truth".
- *Solution:* **Bi-temporal** columns: valid-time (`effective_from/to`) and transaction-time (`recorded_from/to`). Query "as of knowledge date K" with `recorded_from <= K < recorded_to`. Delta time travel gives table-level transaction time cheaply; row-level bi-temporal needs explicit columns.
- *Follow-up:* "Cost?" → ~2× rows for corrected facts; mitigate by keeping bi-temporal only on financial facts.

**13. [Databricks] Choose a methodology.** 40 source systems, schema changes weekly, regulators ask "who changed this and from where?", and BI needs 20 core dashboards.
- *Solution:* Raw → **Data Vault** (or at least append-only bronze with lineage) as the integration layer to absorb source change and keep lineage; **Kimball marts** (star schemas) as the BI-serving layer; medallion layers map: bronze=staging, silver=vault/conformed, gold=marts. Automate vault loading from metadata, because hand-written vault ETL is its main cost.
- *Follow-up:* "Smaller team of 3?" → skip the vault, use medallion + stars with strong contracts; revisit when source count grows.

---

## Part C — Interview Simulations

### Sim 1 — [BCG] "Design a data model for a ride-sharing platform."

**Think aloud**
1. **Clarify the business questions** (this drives grain): trips per city/hour, driver utilization, surge effectiveness, rider retention, revenue per trip, cancellations.
2. **Declare grains:** `fact_trip` = one trip request; `fact_driver_shift_snapshot` = driver × hour; `fact_surge_zone_minute` = zone × minute.
3. **Dimensions:** date, time-of-day (separate, 24 rows, so date stays small), rider (SCD2 for tier), driver (SCD2 for vehicle/city), zone (**role-playing**: pickup and dropoff), trip status (cancel reasons).
4. **Pitfalls:** unmatched/cancelled trips (unknown driver key), timezone (store UTC + local), surge as a measure vs state fact, tips arriving later (late-arriving measure → update or separate payments fact).

```sql
-- ✅ DDL executed in DuckDB (9 tables)
CREATE TABLE dim_zone   (zone_key INT PRIMARY KEY, zone_id VARCHAR, zone_name VARCHAR, city VARCHAR, country VARCHAR);
CREATE TABLE dim_trip_status (status_key INT PRIMARY KEY, status VARCHAR, cancel_reason VARCHAR);
CREATE TABLE fact_trip (
  trip_id VARCHAR PRIMARY KEY,
  request_date_key INT REFERENCES dim_date(date_key), request_time_key INT REFERENCES dim_time_of_day(time_key),
  rider_key BIGINT REFERENCES dim_rider(rider_key), driver_key BIGINT,
  pickup_zone_key INT REFERENCES dim_zone(zone_key), dropoff_zone_key INT REFERENCES dim_zone(zone_key),
  status_key INT REFERENCES dim_trip_status(status_key),
  wait_seconds INT, trip_seconds INT, distance_km DOUBLE,
  base_fare DECIMAL(10,2), surge_multiplier DECIMAL(4,2), tip DECIMAL(10,2), total_fare DECIMAL(10,2), driver_payout DECIMAL(10,2));
CREATE TABLE fact_driver_shift_snapshot (snapshot_hour TIMESTAMP, driver_key BIGINT, zone_key INT,
  minutes_online INT, minutes_on_trip INT, PRIMARY KEY (snapshot_hour, driver_key));
CREATE TABLE fact_surge_zone_minute (minute_ts TIMESTAMP, zone_key INT, surge_multiplier DECIMAL(4,2),
  open_requests INT, available_drivers INT, PRIMARY KEY (minute_ts, zone_key));
```
(The remaining `dim_date`, `dim_time_of_day`, `dim_rider`, `dim_driver` are in the same executed script.) Driver utilization is then `SUM(minutes_on_trip)/SUM(minutes_online)` from the snapshot fact.

| Interviewer evaluates | Green flag | Red flag |
|---|---|---|
| Grain | States grain per fact before columns | Starts listing columns |
| Dimensions | Role-playing zones, separate time-of-day, SCD2 on driver/rider | One giant `trip` table |
| Edge cases | Cancellations, unknown driver, timezones, late tips | Only the happy path |
| Fact types | Transaction + periodic snapshot | Everything as one transaction fact |
| Business link | Ties model to the questions asked | Technically correct but unmotivated |

### Sim 2 — [Amazon] "Design the dimensional model for a marketplace with buyers, sellers, and products."

**Think aloud**
1. **Clarify:** who is the "customer" for revenue, buyers or sellers? (Platform revenue = marketplace fee/commission, not GMV.) Do sellers compete on the same product (same ASIN)? FBA vs seller-fulfilled?
2. **Key modelling insight:** a *product* (catalog item) is not an *offer*. Many sellers list the same product at different prices/conditions → `dim_offer(seller × product × condition)` as the item sold.
3. **Facts:** `fact_order_line` (grain = one order line; measures: item price, shipping, **marketplace_fee**, seller_net), `fact_return` (separate process, separate grain), `fact_review` (event fact, plus daily rating snapshot if needed).
4. **Conformed dimensions** shared across facts: buyer, seller, product, date.
5. **GMV vs net revenue:** define metrics explicitly: GMV = Σ item_price × qty; platform revenue = Σ marketplace_fee (+ ad/FBA fees from other facts).

```sql
-- ✅ DDL executed in DuckDB (9 tables)
CREATE TABLE dim_offer (offer_key BIGINT PRIMARY KEY, seller_key BIGINT REFERENCES dim_seller(seller_key),
  product_key BIGINT REFERENCES dim_product(product_key), condition VARCHAR,
  valid_from TIMESTAMP, valid_to TIMESTAMP, list_price DECIMAL(10,2), is_current BOOLEAN);
CREATE TABLE fact_order_line (
  order_id VARCHAR, line_no INT, order_date_key INT, buyer_key BIGINT REFERENCES dim_buyer(buyer_key),
  seller_key BIGINT REFERENCES dim_seller(seller_key), product_key BIGINT REFERENCES dim_product(product_key),
  offer_key BIGINT REFERENCES dim_offer(offer_key), fulfillment_key INT REFERENCES dim_fulfillment(fulfillment_key),
  quantity INT, item_price DECIMAL(10,2), shipping_charged DECIMAL(10,2), marketplace_fee DECIMAL(10,2),
  seller_net DECIMAL(10,2), PRIMARY KEY (order_id, line_no));
CREATE TABLE fact_return (return_id VARCHAR PRIMARY KEY, order_id VARCHAR, line_no INT, return_date_key INT, reason VARCHAR, refund_amount DECIMAL(10,2));
```
**Follow-ups to prepare for:** seller tier changes over time (SCD2 on `dim_seller`); category hierarchy (flatten `category_l1..l3` in `dim_product` or use a ragged-hierarchy bridge); counterfeit/fraud flags (a seller-risk snapshot fact); and the double-counting trap when joining reviews (many per product) to order lines (aggregate each fact to a common grain *before* joining; the "drill-across" pattern).

| Interviewer evaluates | Green flag | Red flag |
|---|---|---|
| Domain insight | Product vs offer; platform revenue ≠ GMV | Treats seller as just another customer |
| Process modelling | Orders, returns, reviews as separate facts | One mega-fact |
| Conformance | Shared dimensions, drill-across | Duplicate buyer/seller tables per fact |
| Correctness | Handles fan-out and additivity | Sums repeated order-level amounts |

---

## Chapter 15 Cheat Sheet

- **State the grain first.** Every wrong fact table is a grain mistake.
- Normalize OLTP (writes); denormalize OLAP (reads); **medallion** = bronze raw → silver clean → gold marts.
- Star schema patterns: junk, degenerate, role-playing, conformed dimensions; unknown/inferred members avoid NULL FKs.
- **SCD2:** `valid_from/valid_to/is_current`; join facts on `fact_date BETWEEN valid_from AND valid_to`; joining on `is_current` rewrites history; define a deleted-then-returned policy.
- **Data Vault:** Hubs (keys) + Links (relationships) + Satellites (history, hashdiff); insert-only, idempotent; build marts on top.
- OBT and activity schema are convenience layers *on top of* governed models.
- Don't `SUM` repeated order-level values or non-additive measures; use `COUNT(DISTINCT order_number)` for order counts.

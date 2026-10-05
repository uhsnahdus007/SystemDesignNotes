# Databricks notebook source
# DBTITLE 1,MASTER SYSTEM PROMPT — Paste This FIRST in Every New Claude Chat
# MAGIC %md
# MAGIC # 📕 Claude Book Builder: Data Engineering Interview Bible
# MAGIC ## How to Use This Notebook
# MAGIC
# MAGIC **Step 1:** Start a new Claude chat. Paste the **MASTER SYSTEM PROMPT** (Cell 2) as your first message.
# MAGIC
# MAGIC **Step 2:** Then paste ONE chapter prompt at a time from the cells below. Wait for Claude to finish before pasting the next.
# MAGIC
# MAGIC **Step 3:** Copy Claude's output into a Google Doc / Notion / Markdown file for your book.
# MAGIC
# MAGIC **Step 4:** Actually CODE along. Every chapter has runnable code — open a Python/SQL notebook and type it yourself (don't just read).
# MAGIC
# MAGIC ---
# MAGIC
# MAGIC ### Rules for Maximum Learning:
# MAGIC 1. **Never copy-paste code from the book into your IDE.** Type it manually — muscle memory matters.
# MAGIC 2. **Try the exercise BEFORE reading the solution.** Struggle for 15-20 min minimum.
# MAGIC 3. **After each chapter**, close the book and try to solve 3 problems from LeetCode/DataLemur on that topic WITHOUT looking at the book.
# MAGIC 4. **Each chapter takes 2-4 hours** to study properly. Don't rush.

# COMMAND ----------

# DBTITLE 1,MASTER SYSTEM PROMPT — Paste This FIRST
# MAGIC %md
# MAGIC # MASTER SYSTEM PROMPT
# MAGIC ### ⬇️ Copy everything below this line and paste as your FIRST message in a new Claude chat ⬇️
# MAGIC
# MAGIC ---
# MAGIC
# MAGIC ```
# MAGIC You are writing a comprehensive, code-heavy Data Engineering interview preparation book.
# MAGIC
# MAGIC Target reader: A software engineer with 3-4 years of experience preparing for DE roles at MAANG (Google, Meta, Amazon, Apple, Netflix, Microsoft), top consulting (BCG Gamma, McKinsey QuantumBlack), FinTech (Stripe, Coinbase, Razorpay, PhonePe), and AI startups/product companies (Databricks, Snowflake, Uber, Airbnb).
# MAGIC
# MAGIC CRITICAL RULES FOR EVERY CHAPTER:
# MAGIC
# MAGIC 1. CODE-FIRST APPROACH:
# MAGIC    - Every concept MUST be followed immediately by runnable code (Python, SQL, or PySpark).
# MAGIC    - No concept should exist as pure theory. If you explain "what" something is, you MUST show "how" it works with code within 3 sentences.
# MAGIC    - Code must be production-quality with proper error handling, type hints, docstrings, and comments.
# MAGIC    - Include the FULL code — never say "similar to above" or "as shown earlier". Every code block must be independently runnable.
# MAGIC
# MAGIC 2. CHAPTER STRUCTURE (follow this EXACTLY for every chapter):
# MAGIC
# MAGIC    ## Part A: Concept + Code (60% of chapter)
# MAGIC    For each topic:
# MAGIC    - 2-3 sentence explanation of WHAT and WHY
# MAGIC    - Immediately show a COMPLETE runnable code example
# MAGIC    - Show the EXPECTED OUTPUT below the code
# MAGIC    - Explain the code line-by-line where non-obvious
# MAGIC    - Show a WRONG way to do it (common mistake) + explain why it's wrong
# MAGIC    - Show the CORRECT/OPTIMIZED way
# MAGIC    - "In Real Production" box: how this looks in a real company's pipeline
# MAGIC
# MAGIC    ## Part B: Guided Practice Problems (25% of chapter)
# MAGIC    - 5 EASY problems (warm-up, build confidence)
# MAGIC    - 5 MEDIUM problems (interview level)
# MAGIC    - 3 HARD problems (MAANG/FinTech level)
# MAGIC    For EACH problem:
# MAGIC      - Problem statement as a realistic business scenario (not abstract)
# MAGIC      - Sample input data (CREATE TABLE + INSERT for SQL, or Python data structures)
# MAGIC      - Expected output
# MAGIC      - Hints (collapsible/labeled "HINT" so reader can try first)
# MAGIC      - Complete solution with step-by-step explanation
# MAGIC      - Time & space complexity
# MAGIC      - "Interviewer Follow-up" — a harder variation the interviewer might ask next
# MAGIC      - Company tag: [Google] [Amazon] [Stripe] etc. based on where this type is asked
# MAGIC
# MAGIC    ## Part C: Interview Simulation (15% of chapter)
# MAGIC    - 2 full interview-style questions with:
# MAGIC      - The exact question as an interviewer would ask it
# MAGIC      - "Think out loud" walkthrough showing how to approach it step-by-step
# MAGIC      - The solution evolving from brute force → optimized
# MAGIC      - What the interviewer is evaluating at each step
# MAGIC      - Red flags to avoid
# MAGIC      - Green flags that impress
# MAGIC
# MAGIC 3. DATA FOR EXAMPLES:
# MAGIC    - Always create REALISTIC sample data. Use real-world scenarios:
# MAGIC      - E-commerce: orders, products, customers, transactions
# MAGIC      - FinTech: payments, accounts, fraud_events, balances
# MAGIC      - Social media: users, posts, likes, followers, sessions
# MAGIC      - Ride-sharing: rides, drivers, surge_pricing, locations
# MAGIC    - Include edge cases in sample data: NULLs, duplicates, timezone issues, late-arriving data
# MAGIC    - Minimum 10-20 rows of sample data so patterns are visible
# MAGIC
# MAGIC 4. PROGRESSIVE DIFFICULTY:
# MAGIC    - Start each topic from absolute basics (assume I know nothing)
# MAGIC    - Build up to interview-level complexity within the same section
# MAGIC    - Mark difficulty: [BASIC] [INTERMEDIATE] [ADVANCED] [INTERVIEW-HARD]
# MAGIC
# MAGIC 5. FORMATTING:
# MAGIC    - Use clear markdown headers
# MAGIC    - Code blocks with language tags (```python, ```sql, ```pyspark)
# MAGIC    - Tables for comparisons
# MAGIC    - "KEY INSIGHT" boxes for important takeaways
# MAGIC    - "COMMON MISTAKE" boxes for things interviewers catch
# MAGIC    - "INTERVIEWER TIP" boxes for what impresses
# MAGIC    - Expected output after every code block
# MAGIC
# MAGIC 6. ANTI-PATTERNS (what NOT to do):
# MAGIC    - Never write a paragraph of theory without code within 3 sentences
# MAGIC    - Never show incomplete code or pseudocode (always full, runnable)
# MAGIC    - Never say "you can try this yourself" without first showing the solution
# MAGIC    - Never assume the reader knows a concept from a previous chapter without a 1-line reminder
# MAGIC    - Never give a problem without the complete solution
# MAGIC
# MAGIC Acknowledge these rules and wait for the first chapter prompt.
# MAGIC ```

# COMMAND ----------

# DBTITLE 1,CHAPTER 1 PROMPT: SQL Foundations
# MAGIC %md
# MAGIC # CHAPTER 1: SQL Foundations — From SELECT to Subqueries
# MAGIC ### ⬇️ Paste this as your SECOND message after the system prompt ⬇️
# MAGIC
# MAGIC ---
# MAGIC
# MAGIC ```
# MAGIC Write Chapter 1: SQL Foundations.
# MAGIC
# MAGIC This is the first chapter of the book. The reader knows NOTHING about SQL.
# MAGIC
# MAGIC Part A — Concepts + Code (cover ALL of these, in order, with code for each):
# MAGIC
# MAGIC 1. What is SQL and why every DE must master it
# MAGIC    - Show a simple SELECT * FROM table query
# MAGIC    - Create a sample `employees` table with 15 rows using CREATE TABLE + INSERT INTO
# MAGIC    - All examples in this chapter should use this table (and more tables you add as needed)
# MAGIC
# MAGIC 2. SELECT, FROM, WHERE, ORDER BY, LIMIT
# MAGIC    - Show each clause separately with output
# MAGIC    - WHERE with different operators: =, !=, >, <, >=, <=, BETWEEN, IN, LIKE, NOT
# MAGIC    - Combining conditions: AND, OR, NOT (show operator precedence trap)
# MAGIC    - ORDER BY with ASC, DESC, multiple columns, NULLS FIRST/LAST
# MAGIC
# MAGIC 3. NULL handling (this trips up 50% of candidates)
# MAGIC    - Show why WHERE column = NULL doesn't work (COMMON MISTAKE)
# MAGIC    - IS NULL, IS NOT NULL
# MAGIC    - COALESCE with 3+ arguments
# MAGIC    - NULLIF use case
# MAGIC    - How NULLs behave in: COUNT, SUM, AVG, GROUP BY, ORDER BY, DISTINCT, JOIN
# MAGIC    - Write a query that gives wrong results due to NULL and fix it
# MAGIC
# MAGIC 4. Data types and type casting
# MAGIC    - Show CAST() and :: syntax
# MAGIC    - Common casting: string to date, string to int, timestamp to date
# MAGIC    - TRY_CAST() for safe casting
# MAGIC    - Show a real bug caused by implicit type casting
# MAGIC
# MAGIC 5. String functions (with a log_entries table as sample data)
# MAGIC    - CONCAT, ||, CONCAT_WS
# MAGIC    - SUBSTRING, LEFT, RIGHT
# MAGIC    - LENGTH, CHAR_LENGTH
# MAGIC    - TRIM, LTRIM, RTRIM
# MAGIC    - UPPER, LOWER, INITCAP
# MAGIC    - REPLACE, TRANSLATE
# MAGIC    - SPLIT_PART (Postgres) / SUBSTRING_INDEX (MySQL)
# MAGIC    - REGEXP_EXTRACT / REGEXP_REPLACE
# MAGIC    - Real scenario: Parse a log line "2024-01-15 ERROR [UserService] Failed login for user_id=12345" to extract date, level, service, user_id
# MAGIC
# MAGIC 6. Date and time functions (with an `orders` table spanning 2 years)
# MAGIC    - CURRENT_DATE, CURRENT_TIMESTAMP, NOW()
# MAGIC    - DATE_TRUNC (to day, week, month, quarter, year)
# MAGIC    - EXTRACT (year, month, day, hour, dow)
# MAGIC    - DATEADD / DATE_ADD / INTERVAL arithmetic
# MAGIC    - DATEDIFF / DATE_DIFF
# MAGIC    - DATE_FORMAT / TO_CHAR for formatting
# MAGIC    - TO_DATE / TO_TIMESTAMP for parsing
# MAGIC    - Timezone conversion: AT TIME ZONE
# MAGIC    - Real scenario: "Find all orders placed in the last 30 days" (show 3 different correct ways)
# MAGIC    - COMMON MISTAKE: Using string comparison for dates
# MAGIC
# MAGIC 7. Math functions
# MAGIC    - ROUND, CEIL, FLOOR, TRUNC
# MAGIC    - ABS, MOD, POWER, SQRT
# MAGIC    - Division: integer division trap, CAST to avoid it
# MAGIC    - Percentage calculation patterns
# MAGIC
# MAGIC 8. CASE WHEN (the Swiss Army knife)
# MAGIC    - Simple CASE vs Searched CASE
# MAGIC    - Nested CASE WHEN
# MAGIC    - CASE WHEN inside aggregation (pivoting pattern)
# MAGIC    - CASE WHEN for bucketing: age groups, revenue tiers
# MAGIC    - Real scenario: Categorize customers as 'High', 'Medium', 'Low' value based on total spend
# MAGIC
# MAGIC 9. DISTINCT and deduplication
# MAGIC    - SELECT DISTINCT single column, multiple columns
# MAGIC    - COUNT(DISTINCT column)
# MAGIC    - DISTINCT ON (Postgres) for keeping first/last per group
# MAGIC    - When DISTINCT is a code smell (usually means your JOIN is wrong)
# MAGIC
# MAGIC Part B — Practice Problems:
# MAGIC - 5 EASY: Basic SELECT, WHERE, ORDER BY, string/date functions
# MAGIC - 5 MEDIUM: Complex WHERE with NULL traps, date math, CASE WHEN pivoting
# MAGIC - 3 HARD: Log parsing with regex, timezone-aware date calculations, multi-step CASE logic
# MAGIC
# MAGIC Part C — Interview Simulation:
# MAGIC - Question 1 [Google]: Given a `user_logins` table, find users who logged in on both weekdays and weekends in January 2024. (Tests: date functions, CASE WHEN, HAVING)
# MAGIC - Question 2 [Amazon]: Given a `server_logs` table with raw log strings, parse out the timestamp, error level, service name, and response time. Clean and categorize them. (Tests: string functions, REGEXP, CASE WHEN, type casting)
# MAGIC
# MAGIC Remember: Create ALL sample data upfront, show ALL expected outputs, include ALL solutions.
# MAGIC ```

# COMMAND ----------

# DBTITLE 1,CHAPTER 2 PROMPT: SQL Joins, Aggregations & Grouping
# MAGIC %md
# MAGIC # CHAPTER 2: SQL Joins, Aggregations & Grouping
# MAGIC ### ⬇️ Paste this after Chapter 1 is done ⬇️
# MAGIC
# MAGIC ---
# MAGIC
# MAGIC ```
# MAGIC Write Chapter 2: SQL Joins, Aggregations & Grouping.
# MAGIC
# MAGIC First, create a complete e-commerce database with these tables (15-20 rows each, realistic data):
# MAGIC - customers (customer_id, name, email, city, country, signup_date)
# MAGIC - orders (order_id, customer_id, order_date, total_amount, status, shipping_city)
# MAGIC - order_items (order_item_id, order_id, product_id, quantity, unit_price)
# MAGIC - products (product_id, name, category, price, stock_quantity)
# MAGIC - payments (payment_id, order_id, payment_date, amount, method, status)
# MAGIC
# MAGIC Include edge cases in data: customers with no orders, orders with no payments, duplicate payments, NULL shipping_city, orders from 2023 and 2024.
# MAGIC
# MAGIC Part A — Concepts + Code:
# MAGIC
# MAGIC 1. JOIN fundamentals (visual explanation + code for each):
# MAGIC    - INNER JOIN: show matching rows with Venn diagram description
# MAGIC    - LEFT JOIN: show all left + matching right (highlight NULLs for non-matches)
# MAGIC    - RIGHT JOIN: when to use (almost never, explain why)
# MAGIC    - FULL OUTER JOIN: show both sides with NULLs
# MAGIC    - CROSS JOIN: cartesian product, actual use case (generate date spine)
# MAGIC    - For EACH join type: show the query, the result table, and count of rows
# MAGIC    - COMMON MISTAKE: Joining without understanding cardinality (1:1, 1:N, N:M) causing row multiplication
# MAGIC    - Show a query where LEFT JOIN produces MORE rows than the left table (duplicate keys)
# MAGIC    - How to debug: always COUNT before and after a JOIN
# MAGIC
# MAGIC 2. Self Joins:
# MAGIC    - Employee-manager hierarchy query
# MAGIC    - Find customers who ordered the same product
# MAGIC    - Find consecutive events (order followed by another order within 7 days)
# MAGIC
# MAGIC 3. Non-equi joins (inequality joins):
# MAGIC    - Range join: find which salary band each employee falls into
# MAGIC    - Time-range join: find all events that happened during a user's session
# MAGIC
# MAGIC 4. Multiple table joins:
# MAGIC    - 3-table join: customers + orders + order_items
# MAGIC    - 4-table join: add products to get product names
# MAGIC    - Join order and performance implications
# MAGIC    - COMMON MISTAKE: Accidentally creating a cartesian product with multiple joins
# MAGIC
# MAGIC 5. Aggregations:
# MAGIC    - COUNT(*) vs COUNT(column) vs COUNT(DISTINCT column) — show different results on same data
# MAGIC    - SUM, AVG, MIN, MAX with NULL behavior
# MAGIC    - Aggregation with CASE WHEN (conditional aggregation):
# MAGIC      - Count of orders by status in a single query (pivoting)
# MAGIC      - Revenue from 'completed' vs 'refunded' orders in one row
# MAGIC    - STRING_AGG / GROUP_CONCAT / LISTAGG
# MAGIC
# MAGIC 6. GROUP BY:
# MAGIC    - Single column grouping
# MAGIC    - Multi-column grouping
# MAGIC    - GROUP BY with expressions (DATE_TRUNC, CASE WHEN)
# MAGIC    - HAVING vs WHERE (explain execution order)
# MAGIC    - HAVING with aggregate conditions
# MAGIC    - COMMON MISTAKE: Selecting non-aggregated columns not in GROUP BY
# MAGIC
# MAGIC 7. Advanced grouping (P1):
# MAGIC    - GROUPING SETS: multiple grouping levels in one query
# MAGIC    - ROLLUP: hierarchical subtotals (country > city > total)
# MAGIC    - CUBE: all possible grouping combinations
# MAGIC    - Show equivalent UNION ALL queries to prove they give same results
# MAGIC
# MAGIC 8. Set operations:
# MAGIC    - UNION vs UNION ALL (show the duplicate elimination)
# MAGIC    - INTERSECT: find common elements
# MAGIC    - EXCEPT/MINUS: find differences
# MAGIC    - Real scenario: Find customers who placed orders in 2023 but NOT in 2024
# MAGIC
# MAGIC Part B — Practice Problems:
# MAGIC - 5 EASY: Simple joins, basic GROUP BY + HAVING, COUNT DISTINCT
# MAGIC - 5 MEDIUM: Multi-table joins with aggregation, conditional aggregation, self-joins
# MAGIC   - [Stripe] "Find the total successful payment amount per customer per month"
# MAGIC   - [Amazon] "Find products that have been ordered but never paid for"
# MAGIC   - [Flipkart] "Find the top 3 selling products in each category"
# MAGIC - 3 HARD:
# MAGIC   - [Google] "For each customer, find the longest streak of consecutive months with at least one order"
# MAGIC   - [Meta] "Build a cohort retention table: for each signup month, what % of users placed orders in month 1, 2, 3...12"
# MAGIC   - [Netflix] "Find pairs of customers who have ordered at least 3 of the same products"
# MAGIC
# MAGIC Part C — Interview Simulation:
# MAGIC - Question 1 [Amazon]: "Given orders and returns tables, calculate the return rate by product category, but only for categories with more than 100 orders. Also flag categories where return rate exceeds 15%." Walk through from brute force to optimized.
# MAGIC - Question 2 [BCG Gamma]: "A client has customer, transaction, and product tables. They want a single report showing: top 10 customers by revenue, their most purchased category, and their month-over-month spend trend. Build it step by step."
# MAGIC ```

# COMMAND ----------

# DBTITLE 1,CHAPTER 3 PROMPT: SQL Window Functions (THE most important chapter)
# MAGIC %md
# MAGIC # CHAPTER 3: SQL Window Functions — The #1 Interview Topic
# MAGIC ### ⬇️ This is the MOST IMPORTANT SQL chapter. Spend the most time here. ⬇️
# MAGIC
# MAGIC ---
# MAGIC
# MAGIC ```
# MAGIC Write Chapter 3: SQL Window Functions — The Most Tested Topic in DE Interviews.
# MAGIC
# MAGIC This chapter should be the LONGEST and most detailed because window functions appear in 90%+ of SQL interviews at MAANG, FinTech, and consulting firms.
# MAGIC
# MAGIC Create these tables (20+ rows each):
# MAGIC - employee_salaries (emp_id, name, department, salary, hire_date, manager_id)
# MAGIC - daily_revenue (date, product_id, product_name, category, revenue, units_sold)
# MAGIC - user_sessions (user_id, session_id, session_start, session_end, pages_viewed, device)
# MAGIC - stock_prices (date, ticker, open_price, close_price, volume)
# MAGIC
# MAGIC Part A — Concepts + Code:
# MAGIC
# MAGIC 1. Window function anatomy:
# MAGIC    - FUNCTION() OVER (PARTITION BY ... ORDER BY ... frame_clause)
# MAGIC    - How it differs from GROUP BY: keeps all rows vs collapsing
# MAGIC    - Show same query with GROUP BY vs window function side by side
# MAGIC    - Execution order: window functions run AFTER WHERE, GROUP BY, HAVING
# MAGIC
# MAGIC 2. Ranking functions (with employee_salaries):
# MAGIC    - ROW_NUMBER(): unique sequential number
# MAGIC    - RANK(): same rank for ties, gaps after
# MAGIC    - DENSE_RANK(): same rank for ties, no gaps
# MAGIC    - Show ALL THREE on the same data side-by-side to see differences clearly
# MAGIC    - NTILE(n): divide into n buckets
# MAGIC    - PERCENT_RANK(), CUME_DIST()
# MAGIC    - INTERVIEW PATTERN: "Find the Nth highest salary per department" — show with ROW_NUMBER, RANK, DENSE_RANK and explain which is correct and why
# MAGIC    - INTERVIEW PATTERN: "Find the top 3 earners in each department" with tie-handling
# MAGIC
# MAGIC 3. Value functions (with stock_prices):
# MAGIC    - LAG(column, n, default): access previous row
# MAGIC    - LEAD(column, n, default): access next row
# MAGIC    - FIRST_VALUE(column): first in window
# MAGIC    - LAST_VALUE(column): last in window (COMMON MISTAKE: default frame!)
# MAGIC    - NTH_VALUE(column, n): nth value in window
# MAGIC    - INTERVIEW PATTERN: "Calculate day-over-day price change %" using LAG
# MAGIC    - INTERVIEW PATTERN: "For each stock, find the date with the highest close price" using FIRST_VALUE
# MAGIC
# MAGIC 4. Aggregate window functions (with daily_revenue):
# MAGIC    - SUM() OVER: running total
# MAGIC    - AVG() OVER: moving average
# MAGIC    - COUNT() OVER: running count
# MAGIC    - MIN/MAX() OVER: running min/max
# MAGIC    - Show each with and without ORDER BY to see the difference
# MAGIC    - INTERVIEW PATTERN: "Running total of revenue by month"
# MAGIC    - INTERVIEW PATTERN: "7-day moving average of daily revenue"
# MAGIC    - INTERVIEW PATTERN: "Cumulative percentage of total revenue"
# MAGIC
# MAGIC 5. Window frames (THIS IS WHERE MOST PEOPLE GET CONFUSED):
# MAGIC    - ROWS BETWEEN:
# MAGIC      - UNBOUNDED PRECEDING AND CURRENT ROW (default for ORDER BY)
# MAGIC      - UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING
# MAGIC      - N PRECEDING AND N FOLLOWING
# MAGIC      - N PRECEDING AND CURRENT ROW (fixed-size lookback)
# MAGIC    - RANGE BETWEEN:
# MAGIC      - How RANGE differs from ROWS (handles ties differently)
# MAGIC      - Show same query with ROWS vs RANGE producing DIFFERENT results
# MAGIC    - DEFAULT frames (CRITICAL to know):
# MAGIC      - Without ORDER BY: entire partition
# MAGIC      - With ORDER BY: RANGE BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
# MAGIC      - COMMON MISTAKE: LAST_VALUE gives wrong results because of default frame
# MAGIC      - Fix: explicitly set ROWS BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING
# MAGIC    - Show 5 different frame specifications with the same data
# MAGIC
# MAGIC 6. Advanced patterns — these are what separate good from great:
# MAGIC
# MAGIC    a. Gap and Islands problem:
# MAGIC       - Given user_logins(user_id, login_date), find consecutive login streaks
# MAGIC       - Method 1: ROW_NUMBER difference technique
# MAGIC       - Method 2: LAG + cumulative sum technique
# MAGIC       - Show both methods, explain tradeoffs
# MAGIC
# MAGIC    b. Sessionization:
# MAGIC       - Given clickstream(user_id, event_time, page), group events into sessions
# MAGIC       - A session ends after 30 minutes of inactivity
# MAGIC       - Use LAG to detect gaps, SUM to create session IDs
# MAGIC
# MAGIC    c. Running deduplication:
# MAGIC       - Keep first occurrence of each (user_id, event_type) combination per day
# MAGIC       - ROW_NUMBER + WHERE rn = 1 pattern
# MAGIC
# MAGIC    d. Median calculation:
# MAGIC       - Using PERCENTILE_CONT(0.5)
# MAGIC       - Using ROW_NUMBER (manual method for dialects without PERCENTILE)
# MAGIC
# MAGIC    e. Year-over-Year comparison:
# MAGIC       - Current month revenue vs same month last year
# MAGIC       - LAG with PARTITION BY month ORDER BY year
# MAGIC
# MAGIC    f. Funnel analysis:
# MAGIC       - Given events (user_id, event_type, timestamp), calculate conversion funnel
# MAGIC       - Step 1: page_view → Step 2: add_to_cart → Step 3: purchase
# MAGIC       - Using LEAD to find next step, DATEDIFF for time between steps
# MAGIC
# MAGIC Part B — Practice Problems (MORE than other chapters):
# MAGIC - 5 EASY: Basic ROW_NUMBER, simple LAG/LEAD, running SUM
# MAGIC - 7 MEDIUM:
# MAGIC   - [Amazon] "Find customers whose order amount increased for 3 consecutive orders"
# MAGIC   - [Google] "Calculate the 30-day moving average of daily active users"
# MAGIC   - [Stripe] "For each merchant, find the median transaction amount per month"
# MAGIC   - [Meta] "Find users who were in the top 10% of activity in any month"
# MAGIC   - [Uber] "For each driver, calculate the time gap between consecutive rides"
# MAGIC   - [Coinbase] "Flag transactions where amount is >3x the user's average"
# MAGIC   - [Flipkart] "Rank products by sales within each category, and show the category-level running total"
# MAGIC - 5 HARD:
# MAGIC   - [Google] "Sessionize clickstream data: group events into sessions with 30-min inactivity gap, number sessions, and find the longest session per user"
# MAGIC   - [Meta] "Build a retention cohort: for users who signed up in each week, what % returned in week 1, 2, ..., 8?"
# MAGIC   - [Amazon] "Find the longest streak of days where each warehouse had inventory below safety stock"
# MAGIC   - [Stripe] "Detect potential fraud: flag any transaction that is the 3rd+ transaction from the same card within 10 minutes and total amount exceeds $5000"
# MAGIC   - [Netflix] "For each user, find their binge-watching sessions (3+ episodes of the same show within 4 hours) and rank shows by binge frequency"
# MAGIC
# MAGIC Part C — Interview Simulation:
# MAGIC - Question 1 [Google, 45 min]: "Given a table of user_actions(user_id, action_type, action_timestamp), calculate the daily active users, 7-day rolling average of DAU, week-over-week growth %, and identify the week with the highest growth. Do it in a single query chain."
# MAGIC - Question 2 [Stripe, 45 min]: "Given transactions(txn_id, merchant_id, amount, timestamp, status), build a fraud detection query that flags: (a) transactions >3 standard deviations from merchant's average, (b) more than 5 transactions in 1 hour from same card, (c) the first transaction in a new country for that card. Combine all flags into one result."
# MAGIC
# MAGIC Make this chapter significantly longer than others. This is THE chapter that wins interviews.
# MAGIC ```

# COMMAND ----------

# DBTITLE 1,CHAPTER 4 PROMPT: Advanced SQL for DE
# MAGIC %md
# MAGIC # CHAPTER 4: Advanced SQL — CTEs, Subqueries, MERGE, Performance
# MAGIC
# MAGIC ---
# MAGIC
# MAGIC ```
# MAGIC Write Chapter 4: Advanced SQL for Data Engineering.
# MAGIC
# MAGIC Create these tables (20 rows each):
# MAGIC - source_customers (id, name, email, city, updated_at) — represents CDC source data
# MAGIC - target_customers (id, name, email, city, valid_from, valid_to, is_current) — SCD Type 2 target
# MAGIC - web_events (event_id, user_id, event_type, event_data_json, created_at) — semi-structured
# MAGIC - employee_hierarchy (emp_id, name, manager_id, level) — tree structure, 4 levels deep
# MAGIC
# MAGIC Part A — Concepts + Code:
# MAGIC
# MAGIC 1. Common Table Expressions (CTEs):
# MAGIC    - Basic CTE syntax and when to use vs subquery vs temp table
# MAGIC    - Multiple CTEs chained together
# MAGIC    - CTE for readability: break a 50-line query into 5 readable CTEs
# MAGIC    - CTE vs subquery performance: are CTEs materialized? (depends on engine)
# MAGIC    - Real scenario: Multi-step revenue analysis (base → filter → aggregate → rank)
# MAGIC
# MAGIC 2. Recursive CTEs:
# MAGIC    - Syntax: WITH RECURSIVE base_case UNION ALL recursive_case
# MAGIC    - Employee hierarchy: find all reports under a manager (any depth)
# MAGIC    - Generate a date spine: all dates from 2023-01-01 to 2024-12-31
# MAGIC    - Find the shortest path in a graph (friend-of-friend)
# MAGIC    - COMMON MISTAKE: infinite recursion without termination condition
# MAGIC    - Performance: LIMIT and MAX RECURSION safeguards
# MAGIC
# MAGIC 3. Subqueries deep dive:
# MAGIC    - Scalar subquery: in SELECT, in WHERE
# MAGIC    - Correlated subquery: reference to outer query (show execution model)
# MAGIC    - EXISTS vs IN: performance difference with NULLs
# MAGIC    - NOT EXISTS vs NOT IN: the NULL trap (CRITICAL interview question)
# MAGIC      - Show query where NOT IN gives wrong results because of NULL
# MAGIC      - Show NOT EXISTS giving correct results
# MAGIC    - Subquery in FROM (derived table)
# MAGIC    - Lateral join / CROSS APPLY: when you need to reference outer table
# MAGIC
# MAGIC 4. MERGE / UPSERT (THE core DE SQL statement):
# MAGIC    - MERGE INTO syntax with MATCHED / NOT MATCHED / NOT MATCHED BY SOURCE
# MAGIC    - Simple upsert: insert new, update existing
# MAGIC    - SCD Type 1: overwrite with MERGE
# MAGIC    - SCD Type 2: full implementation with MERGE
# MAGIC      - Step-by-step: close old record (set valid_to, is_current=false)
# MAGIC      - Insert new record with valid_from=now, is_current=true
# MAGIC      - Handle the case where nothing changed (no-op)
# MAGIC      - Show the FULL working query with sample data and before/after state
# MAGIC    - INSERT ON CONFLICT (PostgreSQL) / INSERT ... ON DUPLICATE KEY UPDATE (MySQL)
# MAGIC    - COMMON MISTAKE: MERGE with duplicate source rows causing errors
# MAGIC
# MAGIC 5. Temp tables vs CTEs vs Views vs Materialized Views:
# MAGIC    - CREATE TEMPORARY TABLE: session-scoped, can be indexed
# MAGIC    - CTE: query-scoped, may or may not materialize
# MAGIC    - VIEW: stored query, no data stored
# MAGIC    - MATERIALIZED VIEW: pre-computed, needs refresh
# MAGIC    - When to use each: decision tree with examples
# MAGIC    - Performance comparison on a real scenario
# MAGIC
# MAGIC 6. Working with JSON / Semi-structured data:
# MAGIC    - PostgreSQL: JSONB operators (->>, #>>, @>, ?), JSONB_ARRAY_ELEMENTS
# MAGIC    - Spark SQL: GET_JSON_OBJECT, JSON_TUPLE, FROM_JSON, schema_of_json
# MAGIC    - Snowflake: LATERAL FLATTEN, VARIANT type, :: casting
# MAGIC    - BigQuery: JSON_EXTRACT, JSON_EXTRACT_SCALAR, UNNEST
# MAGIC    - Real scenario: Parse nested event data JSON into a flat table
# MAGIC    - EXPLODE pattern: turning arrays into rows
# MAGIC
# MAGIC 7. PIVOT and UNPIVOT:
# MAGIC    - PIVOT: rows to columns (monthly revenue as columns)
# MAGIC    - UNPIVOT: columns to rows (normalize a wide table)
# MAGIC    - Conditional aggregation as manual pivot (works everywhere)
# MAGIC
# MAGIC 8. Query performance and EXPLAIN:
# MAGIC    - Reading EXPLAIN ANALYZE output (PostgreSQL example)
# MAGIC    - Identifying: sequential scan, index scan, hash join, merge join, nested loop
# MAGIC    - Cost estimation vs actual time
# MAGIC    - Row count estimation errors
# MAGIC    - How to fix: add indexes, rewrite query, add statistics
# MAGIC    - Spark SQL EXPLAIN: reading logical and physical plans
# MAGIC    - 5 most common SQL performance anti-patterns with fixes:
# MAGIC      a. SELECT * in production queries
# MAGIC      b. Functions on indexed columns in WHERE
# MAGIC      c. Correlated subqueries (rewrite as JOIN)
# MAGIC      d. DISTINCT to hide a bad JOIN
# MAGIC      e. OR in JOIN conditions
# MAGIC
# MAGIC Part B — Practice Problems:
# MAGIC - 5 EASY: Basic CTEs, simple subqueries, INSERT ON CONFLICT
# MAGIC - 5 MEDIUM:
# MAGIC   - [Databricks] "Implement SCD Type 2 using MERGE INTO for a customer dimension"
# MAGIC   - [Snowflake] "Parse a JSON column containing nested order items into a flat table"
# MAGIC   - [Google] "Find the depth of each node in an organizational hierarchy using recursive CTE"
# MAGIC   - [Amazon] "Find all products where NOT EXISTS any order in the last 90 days"
# MAGIC   - [BCG] "Create a date spine and fill missing dates with 0 revenue"
# MAGIC - 3 HARD:
# MAGIC   - [Stripe] "Implement a full CDC pipeline in SQL: detect inserts, updates, deletes from source_snapshot vs previous_snapshot, apply as SCD Type 2"
# MAGIC   - [Meta] "Parse a complex nested JSON event (3 levels deep with arrays) into a normalized set of tables"
# MAGIC   - [Google] "Optimize a slow query (given the EXPLAIN output) — identify 3 issues and rewrite"
# MAGIC
# MAGIC Part C — Interview Simulation:
# MAGIC - Question 1 [Databricks, 45 min]: "You have a source table that receives daily full snapshots. Design and implement a SQL pipeline that: (a) detects new, changed, and deleted records, (b) applies SCD Type 2 to a target dimension, (c) handles the first-ever load vs subsequent loads. Write the complete SQL."
# MAGIC - Question 2 [BCG Gamma, 45 min]: "A client's analytics query takes 45 minutes. Here's the EXPLAIN plan [provide realistic plan]. Identify the bottlenecks and rewrite the query to run in under 1 minute."
# MAGIC ```

# COMMAND ----------

# DBTITLE 1,CHAPTER 5 PROMPT: Python Core for DE
# MAGIC %md
# MAGIC # CHAPTER 5: Python Core — From Basics to Production-Grade Code
# MAGIC
# MAGIC ---
# MAGIC
# MAGIC ```
# MAGIC Write Chapter 5: Python Core for Data Engineering.
# MAGIC
# MAGIC This chapter covers Python as a DE tool — not generic Python. Every example must be DE-relevant (data processing, file handling, API calls, pipeline logic).
# MAGIC
# MAGIC Part A — Concepts + Code:
# MAGIC
# MAGIC 1. Data structures with DE context:
# MAGIC    - Lists: processing batches of records, list comprehensions for transformation
# MAGIC      - Build a function that chunks a list of 10,000 records into batches of 500
# MAGIC    - Dicts: mapping/lookup tables, config management, JSON processing
# MAGIC      - Build a schema validator using nested dicts
# MAGIC    - Sets: deduplication, finding differences between datasets
# MAGIC      - Find records in source but not in target (like SQL EXCEPT)
# MAGIC    - Tuples: immutable records, named tuples for typed data
# MAGIC    - DefaultDict, Counter, OrderedDict: practical uses
# MAGIC      - Counter: find the most common error types in a log file
# MAGIC      - DefaultDict: group records by key without checking key existence
# MAGIC    - Deque: sliding window on streaming data
# MAGIC    - Heapq: top-K records from a large dataset without sorting everything
# MAGIC
# MAGIC    For EACH structure: show time complexity of operations in a table.
# MAGIC
# MAGIC 2. Functions — production patterns:
# MAGIC    - Type hints everywhere (List, Dict, Optional, Union, Tuple, Any)
# MAGIC    - Docstrings (Google style)
# MAGIC    - *args, **kwargs: building flexible pipeline functions
# MAGIC    - Build a `transform_records()` function that accepts arbitrary transformations via **kwargs
# MAGIC    - Lambda + map/filter for data transformation chains
# MAGIC    - functools.reduce: building aggregation pipelines
# MAGIC
# MAGIC 3. Generators (CRITICAL for large data):
# MAGIC    - Why: process 10GB file without loading into memory
# MAGIC    - yield vs return
# MAGIC    - Generator expressions vs list comprehensions
# MAGIC    - Build a CSV file reader that yields one row at a time
# MAGIC    - Build a database cursor wrapper that yields batches
# MAGIC    - itertools for DE: chain (concat files), islice (sample), groupby (group records), product (parameter grids)
# MAGIC    - Memory comparison: show memory usage of list vs generator for 1M records
# MAGIC
# MAGIC 4. Decorators for pipeline code:
# MAGIC    - @timer: measure function execution time
# MAGIC    - @retry(max_attempts=3, backoff=2): retry with exponential backoff
# MAGIC    - @log_execution: log function entry/exit with arguments
# MAGIC    - @validate_schema: validate input data shape before processing
# MAGIC    - @cache (functools.lru_cache): memoize expensive lookups
# MAGIC    - Build ALL 5 decorators from scratch with full code
# MAGIC
# MAGIC 5. Context managers and file I/O:
# MAGIC    - with statement and __enter__/__exit__
# MAGIC    - Reading/writing: CSV, JSON, JSONL, Parquet (with pyarrow)
# MAGIC    - Build a custom context manager: DatabaseConnection that auto-commits/rollbacks
# MAGIC    - Build a context manager: TempFile that auto-deletes after processing
# MAGIC    - gzip: reading/writing compressed files
# MAGIC    - pathlib: modern file path handling
# MAGIC    - Processing a 5GB CSV file line by line (generator + context manager)
# MAGIC
# MAGIC 6. Error handling for data pipelines:
# MAGIC    - try/except/else/finally — when each block runs
# MAGIC    - Custom exceptions: DataValidationError, SchemaError, SourceUnavailableError
# MAGIC    - Build a pipeline step that: tries to process, retries on transient errors, dead-letters bad records, and raises on fatal errors
# MAGIC    - Logging: structured logging with JSON format
# MAGIC    - Build a complete logging setup for a data pipeline
# MAGIC
# MAGIC 7. OOP for pipeline design:
# MAGIC    - @dataclass for config and data models:
# MAGIC      - PipelineConfig(source, target, batch_size, retry_count)
# MAGIC      - DataRecord(id, payload, timestamp, source)
# MAGIC    - Abstract base class: BasePipelineStep with extract(), transform(), load()
# MAGIC    - Concrete implementations: CSVExtractor, APIExtractor, S3Extractor
# MAGIC    - Build a mini pipeline framework (50-70 lines) that chains steps
# MAGIC
# MAGIC 8. Concurrency for DE:
# MAGIC    - Threading: parallel API calls to ingest data from 10 endpoints
# MAGIC    - Multiprocessing: parallel file processing (transform 100 CSVs simultaneously)
# MAGIC    - concurrent.futures: ThreadPoolExecutor for I/O, ProcessPoolExecutor for CPU
# MAGIC    - asyncio + aiohttp: async API ingestion (100 concurrent requests)
# MAGIC    - Build a parallel file processor that: reads files from a directory, transforms each in parallel, writes to output, handles errors per file
# MAGIC    - GIL explanation: why threading doesn't help CPU-bound work
# MAGIC    - Benchmark: sequential vs threaded vs multiprocessing on same task
# MAGIC
# MAGIC Part B — Practice Problems:
# MAGIC - 5 EASY: List/dict manipulation, file reading, basic generators
# MAGIC - 5 MEDIUM:
# MAGIC   - Build a CSV deduplication tool that handles 1M+ rows memory-efficiently
# MAGIC   - Build an API data ingestion script with pagination, retry, and rate limiting
# MAGIC   - Build a log parser that extracts structured data from mixed log formats
# MAGIC   - Build a schema comparison tool: compare two table schemas and output diff
# MAGIC   - Build a data validation framework using decorators
# MAGIC - 3 HARD:
# MAGIC   - [Amazon] Build a complete mini-ETL framework: configurable extract (CSV/JSON/API), transform (column rename, type cast, filter, join), load (CSV/Parquet/JSON) with error handling, logging, and metrics
# MAGIC   - [Google] Build an async web scraper that: hits 50 URLs concurrently, parses JSON responses, deduplicates, validates schema, writes to Parquet in batches of 1000
# MAGIC   - [Stripe] Build a real-time event processor: reads from a queue (simulate with a generator), applies transformation rules (configurable), detects anomalies (>3 std dev), writes results and alerts
# MAGIC
# MAGIC Part C — Interview Simulation:
# MAGIC - Question 1 [Amazon]: "Design and implement a Python class for a data pipeline that reads from S3, applies transformations, and writes to a database. It should support: batch processing, error recovery, logging, and metrics. Walk through your design choices."
# MAGIC - Question 2 [Meta]: "You have 1TB of JSON files. Write Python code that processes them efficiently on a single machine. Explain your memory management strategy, parallelization approach, and error handling."
# MAGIC ```

# COMMAND ----------

# DBTITLE 1,CHAPTER 6 PROMPT: Python Libraries for DE (pandas, polars, requests, SQLAlchemy)
# MAGIC %md
# MAGIC # CHAPTER 6: Python Libraries Every DE Must Know
# MAGIC
# MAGIC ---
# MAGIC
# MAGIC ```
# MAGIC Write Chapter 6: Essential Python Libraries for Data Engineering.
# MAGIC
# MAGIC Use a realistic dataset throughout: an e-commerce dataset with orders (100K+ conceptual, show 30 rows), customers, and product data with messy real-world issues (nulls, duplicates, mixed types, inconsistent formats).
# MAGIC
# MAGIC Part A — Concepts + Code:
# MAGIC
# MAGIC 1. pandas (deep dive — most used in interviews):
# MAGIC    - DataFrame creation: from dict, CSV, JSON, Parquet, SQL query
# MAGIC    - Exploration: .head(), .info(), .describe(), .dtypes, .shape, .columns, .nunique()
# MAGIC    - Selection: .loc[], .iloc[], boolean indexing, .query()
# MAGIC    - Cleaning:
# MAGIC      - .isna(), .fillna(), .dropna() with subset
# MAGIC      - .drop_duplicates() with subset and keep='first'/'last'
# MAGIC      - .astype() for type conversion
# MAGIC      - .str accessor: strip, lower, replace, extract, contains
# MAGIC      - .dt accessor: year, month, day, date, normalize
# MAGIC      - .replace() and .map() for value mapping
# MAGIC    - Transformation:
# MAGIC      - .apply() on rows and columns (and why to avoid it when possible)
# MAGIC      - .groupby() + .agg() with named aggregations
# MAGIC      - .merge() — all join types, on vs left_on/right_on, indicator=True
# MAGIC      - .concat() for stacking DataFrames
# MAGIC      - .pivot_table() and .melt()
# MAGIC      - .assign() for chained transformations
# MAGIC    - Performance:
# MAGIC      - Vectorized ops vs .apply() vs iterrows() — benchmark all three
# MAGIC      - Memory: category dtype for strings, downcast for numerics
# MAGIC      - Chunked reading: pd.read_csv(chunksize=10000)
# MAGIC    - I/O:
# MAGIC      - read_csv / to_csv with encoding, quoting, compression
# MAGIC      - read_parquet / to_parquet with engine='pyarrow'
# MAGIC      - read_json / to_json with orient options
# MAGIC      - read_sql / to_sql with SQLAlchemy engine
# MAGIC
# MAGIC 2. polars (modern alternative — showing up in startup interviews):
# MAGIC    - Why polars: Rust-based, lazy evaluation, no GIL, multithreaded
# MAGIC    - Same operations as pandas above but in polars syntax
# MAGIC    - .lazy() and .collect() for query optimization
# MAGIC    - .with_columns(), .filter(), .group_by(), .join()
# MAGIC    - Expression system: pl.col(), pl.lit(), pl.when().then().otherwise()
# MAGIC    - Performance comparison: pandas vs polars on same task with timing
# MAGIC
# MAGIC 3. requests + API data ingestion:
# MAGIC    - GET, POST, PUT with headers and auth
# MAGIC    - Pagination: offset-based, cursor-based, page-token based
# MAGIC    - Build a COMPLETE API ingestion script:
# MAGIC      - Paginate through all pages
# MAGIC      - Retry with exponential backoff on 429/500
# MAGIC      - Rate limiting with time.sleep()
# MAGIC      - Write results incrementally to JSONL
# MAGIC      - Handle authentication (API key, OAuth2 token refresh)
# MAGIC    - Error handling: status codes, timeouts, connection errors
# MAGIC
# MAGIC 4. SQLAlchemy:
# MAGIC    - create_engine() with connection string for PostgreSQL, MySQL, SQLite
# MAGIC    - Connection pooling: pool_size, max_overflow
# MAGIC    - Raw SQL execution: engine.execute(), text()
# MAGIC    - ORM basics: Table, Column, MetaData, select, insert
# MAGIC    - Read into pandas: pd.read_sql(query, engine)
# MAGIC    - Write from pandas: df.to_sql(table, engine, if_exists='append'/'replace', method='multi')
# MAGIC    - Transaction management: with engine.begin() as conn
# MAGIC    - Build a complete database loader: upsert a DataFrame into PostgreSQL
# MAGIC
# MAGIC 5. pydantic (data validation):
# MAGIC    - BaseModel for schema definition
# MAGIC    - Field validation: types, constraints, custom validators
# MAGIC    - Build a pipeline config validator
# MAGIC    - Build a record validator: validate each row of incoming data
# MAGIC    - Serialize/deserialize: .model_dump(), .model_validate()
# MAGIC
# MAGIC 6. PyArrow & file formats:
# MAGIC    - Reading/writing Parquet with schemas
# MAGIC    - Parquet metadata inspection: row groups, column stats
# MAGIC    - Reading/writing Avro
# MAGIC    - Arrow tables: zero-copy conversion to pandas
# MAGIC    - Partitioned writing: write_to_dataset with partition_cols
# MAGIC
# MAGIC 7. Jinja2 for SQL templating:
# MAGIC    - Build a SQL template engine for your pipelines
# MAGIC    - Template: SELECT * FROM {{ table }} WHERE date >= '{{ start_date }}'
# MAGIC    - Conditional SQL: {% if filter %} AND status = '{{ filter }}' {% endif %}
# MAGIC    - Loop: {% for col in columns %} {{ col }}, {% endfor %}
# MAGIC    - Build a complete templated SQL pipeline runner
# MAGIC
# MAGIC Part B — Practice Problems:
# MAGIC - 5 EASY: pandas basics, read/write files, simple API call
# MAGIC - 5 MEDIUM:
# MAGIC   - Clean a messy CSV (mixed date formats, inconsistent names, NULLs) into analysis-ready format
# MAGIC   - Build an incremental API ingester: track last_synced timestamp, only fetch new records
# MAGIC   - Merge 3 DataFrames and handle many-to-many relationships correctly
# MAGIC   - Build a data quality report: completeness, uniqueness, value distributions per column
# MAGIC   - Compare two DataFrames and generate a diff report (new, changed, deleted rows)
# MAGIC - 3 HARD:
# MAGIC   - [Stripe] Build a complete payment reconciliation system: match transactions from payment_provider API with internal database, flag mismatches
# MAGIC   - [Amazon] Build an inventory data pipeline: ingest from 5 different CSV formats, normalize, validate, deduplicate, and load into a single schema with audit trail
# MAGIC   - [Airbnb] Build a data quality framework: define expectations (column not null, values in range, referential integrity), run against a DataFrame, generate HTML report
# MAGIC
# MAGIC Part C — Interview Simulation:
# MAGIC - Question 1 [Google]: "You receive hourly data dumps as JSON files from 3 different sources with different schemas. Write Python code that: normalizes them into a common schema, deduplicates, validates, and writes as partitioned Parquet. Handle schema evolution."
# MAGIC - Question 2 [Amazon]: "An API endpoint returns paginated results with rate limiting (100 requests/minute). Write production-quality Python code to ingest all data, handle failures gracefully, and output as Parquet partitioned by date."
# MAGIC ```

# COMMAND ----------

# DBTITLE 1,CHAPTER 7 PROMPT: DSA for DE OA Rounds
# MAGIC %md
# MAGIC # CHAPTER 7: Data Structures & Algorithms for DE OA Rounds
# MAGIC
# MAGIC ---
# MAGIC
# MAGIC ```
# MAGIC Write Chapter 7: DSA Patterns for Data Engineering Online Assessment Rounds.
# MAGIC
# MAGIC This chapter is PURELY practice-focused. Minimal theory. Maximum problem-solving.
# MAGIC
# MAGIC Target: Easy to Medium LeetCode difficulty. DE roles don't get Hard DP problems, but they DO get tricky hash map, string, and graph problems.
# MAGIC
# MAGIC Part A — Pattern-Based Learning (show EACH pattern with 3 examples):
# MAGIC
# MAGIC 1. Hash Map Pattern (appears in 80% of DE OAs):
# MAGIC    - Pattern explanation: when to use hash maps (O(1) lookup, counting, grouping)
# MAGIC    - Problem 1: Two Sum (classic, warm-up)
# MAGIC    - Problem 2: Group Anagrams (string grouping)
# MAGIC    - Problem 3: Subarray Sum Equals K (prefix sum + hash map)
# MAGIC    - DE-specific: Deduplicate records by composite key
# MAGIC    - DE-specific: Find matching records across two datasets
# MAGIC
# MAGIC 2. Sliding Window Pattern:
# MAGIC    - Fixed-size window: Max sum of k consecutive elements
# MAGIC    - Variable-size window: Longest substring without repeating characters
# MAGIC    - DE-specific: Find peak traffic periods in server logs (window of N minutes)
# MAGIC    - DE-specific: Rate limiter implementation
# MAGIC
# MAGIC 3. Two Pointers Pattern:
# MAGIC    - Sorted array: two sum on sorted array
# MAGIC    - Merge two sorted arrays (merge step)
# MAGIC    - DE-specific: Merge two sorted event streams by timestamp
# MAGIC    - DE-specific: Find overlapping time ranges
# MAGIC
# MAGIC 4. Sorting + Greedy:
# MAGIC    - Merge Intervals (VERY common for DE)
# MAGIC    - Meeting Rooms II (minimum resources)
# MAGIC    - DE-specific: Optimal batch scheduling (minimize idle time)
# MAGIC    - DE-specific: File compaction (merge small files that together fit in target size)
# MAGIC
# MAGIC 5. Stack Pattern:
# MAGIC    - Valid Parentheses
# MAGIC    - Daily Temperatures (monotonic stack)
# MAGIC    - DE-specific: Parse nested JSON manually
# MAGIC    - DE-specific: Validate nested SQL parentheses
# MAGIC
# MAGIC 6. BFS/DFS + Graph:
# MAGIC    - Number of Islands (grid DFS)
# MAGIC    - Course Schedule (topological sort) — CRITICAL for DAG-based pipelines!
# MAGIC    - DE-specific: Determine pipeline execution order from dependency graph
# MAGIC    - DE-specific: Detect circular dependencies in a DAG
# MAGIC
# MAGIC 7. String Processing (DE-heavy):
# MAGIC    - Log parsing: extract fields from structured log lines
# MAGIC    - URL parsing: extract protocol, domain, path, query params
# MAGIC    - Custom CSV parser: handle quoted fields, escaped commas
# MAGIC    - JSON path extractor: given a path like "data.users[0].name", extract value
# MAGIC
# MAGIC 8. Heap / Priority Queue:
# MAGIC    - Top K frequent elements
# MAGIC    - Merge K sorted lists
# MAGIC    - DE-specific: K-way merge of sorted files
# MAGIC    - DE-specific: Top-K records from streaming data
# MAGIC
# MAGIC For EVERY problem above:
# MAGIC - Problem statement
# MAGIC - Brute force approach + complexity
# MAGIC - Optimal approach + complexity  
# MAGIC - Complete Python code with type hints
# MAGIC - Test cases including edge cases
# MAGIC - Company tags
# MAGIC
# MAGIC Part B — Full OA Simulation (timed practice):
# MAGIC
# MAGIC Simulate a real 90-minute OA with 4 problems:
# MAGIC
# MAGIC OA Simulation 1 (MAANG style):
# MAGIC - Q1 [Easy, 15 min]: Given a list of log entries with timestamps and status codes, find the hour with the most 5xx errors.
# MAGIC - Q2 [Medium, 25 min]: Given a list of file sizes and a target batch size, group consecutive files into batches where each batch doesn't exceed the target size. Return the batches.
# MAGIC - Q3 [Medium, 25 min]: Given a DAG represented as adjacency list (task dependencies), return a valid execution order. If circular dependency exists, return an error.
# MAGIC - Q4 [Medium-Hard, 25 min]: Given a stream of (timestamp, user_id, action) events, implement a sessionizer. A session ends after 30 minutes of inactivity. Return (user_id, session_id, session_start, session_end, event_count).
# MAGIC
# MAGIC OA Simulation 2 (FinTech style):
# MAGIC - Q1 [Easy, 15 min]: Given a list of transactions (amount, timestamp, merchant), find the merchant with the highest total in the last 7 days.
# MAGIC - Q2 [Medium, 25 min]: Implement a rate limiter class. rate_limiter.allow(user_id, timestamp) returns True if the user hasn't exceeded 5 requests in the last 60 seconds.
# MAGIC - Q3 [Medium, 25 min]: Given a list of price changes (timestamp, stock, price), for each stock find the maximum profit from one buy and one sell in chronological order.
# MAGIC - Q4 [Medium-Hard, 25 min]: Given two sorted lists of transactions from different systems, reconcile them: find matches (by amount and date ±1 day), find unmatched records from each side.
# MAGIC
# MAGIC Part C — Complexity Cheat Sheet:
# MAGIC - Table of every data structure + operation + time complexity
# MAGIC - Table of every sorting algorithm + when to use
# MAGIC - Common recurrence relations
# MAGIC ```

# COMMAND ----------

# DBTITLE 1,CHAPTERS 8-10 PROMPTS: Spark, Delta Lake, Databases
# MAGIC %md
# MAGIC # CHAPTER 8: Apache Spark — From Zero to Performance Tuning
# MAGIC
# MAGIC ---
# MAGIC
# MAGIC ```
# MAGIC Write Chapter 8: Apache Spark Deep Dive.
# MAGIC
# MAGIC This is a MASSIVE chapter. Cover everything a 3-4 YOE DE needs for Spark interviews.
# MAGIC
# MAGIC Create realistic sample data using PySpark code (the reader will run this in Databricks/local Spark):
# MAGIC - transactions_df: 50 rows (txn_id, user_id, amount, category, txn_date, merchant, city, country)
# MAGIC - users_df: 20 rows (user_id, name, signup_date, tier, city, country)
# MAGIC - events_df: 40 rows (event_id, user_id, event_type, event_timestamp, properties_json)
# MAGIC
# MAGIC Part A — Concepts + Code:
# MAGIC
# MAGIC 1. Spark architecture (with diagrams in text):
# MAGIC    - Driver, Executors, Cluster Manager
# MAGIC    - SparkSession creation with configurations
# MAGIC    - Show: spark.conf.set() for key settings
# MAGIC    - Local mode vs cluster mode
# MAGIC
# MAGIC 2. RDD basics (know for interviews, rarely used in practice):
# MAGIC    - Create RDD from list, from file
# MAGIC    - map, filter, flatMap, reduce, collect
# MAGIC    - Why DataFrames replaced RDDs (Catalyst optimizer)
# MAGIC
# MAGIC 3. DataFrame API — complete reference with code:
# MAGIC    - Creating DataFrames: from list, from CSV, Parquet, JSON, Delta, JDBC
# MAGIC    - Schema: StructType, StructField, explicit vs inferred
# MAGIC    - Select, filter/where, withColumn, withColumnRenamed, drop
# MAGIC    - Column expressions: col(), lit(), when().otherwise(), cast()
# MAGIC    - String functions: concat, substring, regexp_extract, regexp_replace, split, trim, lower
# MAGIC    - Date functions: current_date, date_add, date_sub, datediff, months_between, date_trunc, to_date, to_timestamp
# MAGIC    - Null handling: isNull, isNotNull, na.drop(), na.fill(), coalesce()
# MAGIC    - Complex types:
# MAGIC      - Arrays: array(), explode(), posexplode(), array_contains(), size(), collect_list(), collect_set()
# MAGIC      - Maps: create_map(), map_keys(), map_values(), element_at()
# MAGIC      - Structs: struct(), col("struct_col.field")
# MAGIC      - Nested JSON parsing: from_json() with schema, get_json_object()
# MAGIC    - Aggregations: groupBy().agg(), multiple aggs, pivot
# MAGIC    - Window functions: Window.partitionBy().orderBy(), row_number, rank, lead, lag, running sums
# MAGIC    - UDFs: Python UDF (show it's slow), Pandas UDF / vectorized UDF (show it's fast), benchmark both
# MAGIC
# MAGIC 4. Joins in Spark (with EXPLAIN output for each):
# MAGIC    - Broadcast join: when and why, broadcast() hint, threshold config
# MAGIC    - Sort-merge join: default for large-large
# MAGIC    - Shuffle hash join
# MAGIC    - Show EXPLAIN for each join type
# MAGIC    - Skew handling: salting technique with full code example
# MAGIC    - COMMON MISTAKE: joining on nullable columns without handling NULLs
# MAGIC
# MAGIC 5. Spark SQL:
# MAGIC    - createOrReplaceTempView
# MAGIC    - spark.sql() for running SQL
# MAGIC    - Mixing DataFrame API and SQL
# MAGIC    - When SQL is cleaner vs when API is cleaner
# MAGIC
# MAGIC 6. Reading and writing data:
# MAGIC    - CSV: options (header, delimiter, inferSchema, quote, escape, nullValue)
# MAGIC    - Parquet: schema evolution, predicate pushdown, column pruning
# MAGIC    - JSON: multiline, schema inference, corrupt record handling
# MAGIC    - Delta: read, write, merge, time travel
# MAGIC    - JDBC: read with partitioning, write with batch size
# MAGIC    - Partitioned writes: partitionBy("year", "month")
# MAGIC    - Bucketing: bucketBy() for pre-shuffled joins
# MAGIC
# MAGIC 7. Performance tuning (THE interview differentiator):
# MAGIC    - Spark UI walkthrough: Jobs → Stages → Tasks, what to look for
# MAGIC    - Key configs with when to change them:
# MAGIC      - spark.sql.shuffle.partitions (default 200)
# MAGIC      - spark.sql.autoBroadcastJoinThreshold (default 10MB)
# MAGIC      - spark.executor.memory, spark.executor.cores
# MAGIC      - spark.driver.memory
# MAGIC      - spark.sql.adaptive.enabled (AQE)
# MAGIC    - AQE deep dive: auto-coalesce, skew join, dynamic partition pruning
# MAGIC    - Data skew: how to detect (Spark UI), how to fix (salting, broadcast, isolate)
# MAGIC    - Small file problem: causes, coalesce() vs repartition(), compaction job
# MAGIC    - Caching: cache() vs persist(), storage levels, when to cache, when NOT to cache
# MAGIC    - Catalyst optimizer: how it works, reading EXPLAIN output
# MAGIC    - Tungsten: whole-stage codegen, off-heap memory
# MAGIC    - Show a slow job and optimize it step by step (before and after metrics)
# MAGIC
# MAGIC Part B — Practice Problems:
# MAGIC - 5 EASY: Basic DataFrame ops, simple joins, groupBy
# MAGIC - 5 MEDIUM:
# MAGIC   - Parse nested JSON events into flat DataFrame
# MAGIC   - Implement sessionization using window functions in PySpark
# MAGIC   - Join two DataFrames with heavy skew — implement salting
# MAGIC   - Deduplicate records keeping the latest version per key
# MAGIC   - Build an incremental load: process only new files since last run
# MAGIC - 3 HARD:
# MAGIC   - [Uber] Build a complete surge pricing calculator: join ride_requests with driver_availability using geospatial proximity and time windows
# MAGIC   - [Netflix] Build a user engagement scoring pipeline: aggregate watch history, calculate completions, build rolling metrics, rank users
# MAGIC   - [Databricks] Optimize a given slow Spark job: provided code with 5 performance issues, identify and fix each one, show before/after
# MAGIC
# MAGIC Part C — Interview Simulation:
# MAGIC - Question 1 [Databricks]: "You have a Spark job processing 2TB daily. It's taking 4 hours and failing with OOM. Walk me through your debugging process. Here's the Spark UI metrics [provide realistic metrics]. What would you change?"
# MAGIC - Question 2 [Amazon]: "Write a PySpark pipeline that reads from a Kafka topic of clickstream events, sessionizes them, joins with user profiles, and writes to Delta Lake partitioned by date. Handle late data and schema evolution."
# MAGIC ```
# MAGIC
# MAGIC ---
# MAGIC
# MAGIC # CHAPTER 9: Delta Lake & Lakehouse (use same prompt structure)
# MAGIC
# MAGIC ```
# MAGIC Write Chapter 9: Delta Lake and Lakehouse Architecture.
# MAGIC
# MAGIC Part A must cover with RUNNABLE PySpark/SQL code for each:
# MAGIC 1. Delta table creation: CREATE TABLE, DataFrame write, CTAS
# MAGIC 2. ACID transactions: demonstrate concurrent writes
# MAGIC 3. Schema enforcement: show a write that fails, then fix with mergeSchema
# MAGIC 4. Schema evolution: add column, change type, nested schema changes
# MAGIC 5. Time travel: VERSION AS OF, TIMESTAMP AS OF, RESTORE
# MAGIC 6. MERGE INTO complete patterns:
# MAGIC    - Simple upsert (insert new + update existing)
# MAGIC    - SCD Type 1 (overwrite)
# MAGIC    - SCD Type 2 (full implementation with is_current, valid_from, valid_to)
# MAGIC    - Delete + insert pattern
# MAGIC    - Conditional merge with multiple WHEN clauses
# MAGIC 7. OPTIMIZE: manual compaction, ZORDER, auto-optimize settings
# MAGIC 8. VACUUM: what it does, retention, dangerous operations
# MAGIC 9. Change Data Feed: enable, query changes, build downstream incremental
# MAGIC 10. Liquid Clustering: how it replaces ZORDER, when to use
# MAGIC 11. Delta vs Iceberg vs Hudi: feature comparison with code where possible
# MAGIC
# MAGIC Every concept: code + expected output + production use case.
# MAGIC 13 practice problems (5 easy + 5 medium + 3 hard) around MERGE patterns, time travel, optimization.
# MAGIC 2 interview simulations: (1) Design a complete CDC pipeline with Delta, (2) Optimize a Delta table with 1 million small files.
# MAGIC ```
# MAGIC
# MAGIC ---
# MAGIC
# MAGIC # CHAPTER 10: Database Internals for Interviews
# MAGIC
# MAGIC ```
# MAGIC Write Chapter 10: Database Internals — What Every DE Must Know.
# MAGIC
# MAGIC This is a concepts chapter, but EVERY concept must be demonstrated with SQL code or Python simulation.
# MAGIC
# MAGIC Part A with code:
# MAGIC 1. ACID deep dive: write transactions in Python/SQL that demonstrate each property breaking when violated
# MAGIC 2. Isolation levels: write concurrent transaction examples showing dirty reads, phantom reads, non-repeatable reads at each level
# MAGIC 3. Indexing: CREATE INDEX, show EXPLAIN ANALYZE before and after indexing, composite index ordering matters (show it), covering index (show index-only scan)
# MAGIC 4. MVCC: explain with a timeline diagram + SQL showing two concurrent transactions
# MAGIC 5. Partitioning: create partitioned tables in PostgreSQL, show partition pruning with EXPLAIN
# MAGIC 6. Replication: conceptual but with a Python simulation of leader-follower lag
# MAGIC 7. Columnar vs Row storage: build a simple simulation showing why columnar is faster for analytics
# MAGIC 8. File formats: write and read Parquet, Avro, ORC with Python, inspect metadata
# MAGIC 9. CAP theorem: build scenarios (with pseudocode) showing each tradeoff
# MAGIC 10. Connection pooling: show the problem without pooling, show the fix with SQLAlchemy pool
# MAGIC
# MAGIC 13 practice problems focused on indexing decisions, isolation level scenarios, format selection.
# MAGIC 2 interview simulations: (1) "Why is this query slow?" with EXPLAIN output, (2) "Design the storage layer for a FinTech ledger system."
# MAGIC ```

# COMMAND ----------

# DBTITLE 1,CHAPTERS 11-14 PROMPTS: Streaming, Kafka, Orchestration, Cloud
# MAGIC %md
# MAGIC # CHAPTER 11: Kafka & Streaming Fundamentals
# MAGIC
# MAGIC ```
# MAGIC Write Chapter 11: Apache Kafka & Streaming Data Processing.
# MAGIC
# MAGIC Part A must cover with code for EACH concept:
# MAGIC 1. Kafka architecture: broker, topic, partition, replication — draw with ASCII art, then show kafka-console-producer/consumer commands
# MAGIC 2. Python Kafka producer: complete code with confluent-kafka library
# MAGIC    - Serialization: JSON, Avro with Schema Registry
# MAGIC    - Partitioning: key-based, round-robin, custom partitioner
# MAGIC    - Acks: 0, 1, all — show reliability vs performance tradeoff
# MAGIC    - Error handling, delivery callbacks
# MAGIC 3. Python Kafka consumer: complete code
# MAGIC    - Consumer groups, partition assignment
# MAGIC    - Offset management: auto-commit vs manual commit
# MAGIC    - Deserialization
# MAGIC    - At-least-once vs exactly-once processing pattern
# MAGIC 4. Schema Registry: register schema, evolve schema, compatibility modes (backward, forward, full)
# MAGIC 5. Kafka Connect: conceptual + sample connector config JSON for:
# MAGIC    - Debezium PostgreSQL source connector
# MAGIC    - S3 sink connector
# MAGIC 6. Streaming concepts with code simulations:
# MAGIC    - Event time vs processing time (Python simulation)
# MAGIC    - Watermarks (Python simulation showing late data handling)
# MAGIC    - Windowing: tumbling, sliding, session (implement each in Python)
# MAGIC    - Exactly-once semantics: idempotent producer + transactional consumer pattern
# MAGIC 7. Spark Structured Streaming:
# MAGIC    - readStream from Kafka topic
# MAGIC    - Transformations on streaming DataFrames
# MAGIC    - withWatermark() for late data
# MAGIC    - Window aggregations on streaming data
# MAGIC    - Output modes: append, update, complete
# MAGIC    - writeStream to Delta Lake (and to Kafka)
# MAGIC    - foreachBatch: apply batch logic to micro-batches
# MAGIC    - Checkpointing for fault tolerance
# MAGIC    - Stream-stream joins with watermarks
# MAGIC    - Complete working example: Kafka → Spark Streaming → Delta Lake
# MAGIC
# MAGIC 13 practice problems: producer/consumer code, windowed aggregations, late data handling.
# MAGIC Interview sims: (1) "Design a real-time fraud detection pipeline using Kafka + Spark" (2) "Your Kafka consumer lag is growing. Diagnose and fix."
# MAGIC ```
# MAGIC
# MAGIC ---
# MAGIC
# MAGIC # CHAPTER 12: CDC (Change Data Capture)
# MAGIC
# MAGIC ```
# MAGIC Write Chapter 12: Change Data Capture — The Bridge Between OLTP and OLAP.
# MAGIC
# MAGIC Part A with complete runnable code:
# MAGIC 1. What is CDC: log-based vs query-based, pros/cons of each
# MAGIC 2. Manual CDC in SQL: detect inserts, updates, deletes using snapshots
# MAGIC    - Full code: compare yesterday's snapshot with today's, classify each row
# MAGIC 3. Manual CDC in Python: build a CDC detector class
# MAGIC 4. Debezium conceptual: architecture, event structure (before/after/op/source)
# MAGIC    - Show a sample Debezium CDC event JSON for INSERT, UPDATE, DELETE
# MAGIC    - Parse Debezium events into a Delta Lake MERGE statement
# MAGIC 5. Applying CDC to Delta Lake:
# MAGIC    - Raw CDC events → Bronze (append-only)
# MAGIC    - Bronze → Silver (MERGE INTO applying changes)
# MAGIC    - Handle ordering: multiple changes to same key in one batch
# MAGIC    - Handle deletes: soft delete vs hard delete patterns
# MAGIC 6. SCD Type 2 from CDC: complete pipeline
# MAGIC 7. Delta Change Data Feed: enable, read changes, build incremental downstream
# MAGIC 8. Initial load + incremental: handle the first-ever full load plus subsequent CDC
# MAGIC
# MAGIC 10 practice problems focused on CDC implementation patterns.
# MAGIC Interview sim: "Design a real-time CDC pipeline from PostgreSQL to a data lake with SCD Type 2."
# MAGIC ```
# MAGIC
# MAGIC ---
# MAGIC
# MAGIC # CHAPTER 13: Airflow & dbt
# MAGIC
# MAGIC ```
# MAGIC Write Chapter 13: Apache Airflow & dbt — Orchestration and Transformation.
# MAGIC
# MAGIC Part A with COMPLETE runnable code:
# MAGIC
# MAGIC AIRFLOW (60%):
# MAGIC 1. DAG definition: basic DAG with BashOperator and PythonOperator
# MAGIC 2. Task dependencies: >> operator, set_upstream, set_downstream
# MAGIC 3. Operators: BashOperator, PythonOperator, SqlSensor, S3KeySensor, SparkSubmitOperator, DatabricksRunNowOperator
# MAGIC 4. XCom: passing data between tasks (and its size limitation)
# MAGIC 5. Connections and Variables: setting up, accessing in DAGs
# MAGIC 6. TaskFlow API: @task decorator, automatic XCom, cleaner syntax
# MAGIC 7. Dynamic task mapping: process N files in parallel where N is unknown at DAG write time
# MAGIC 8. Error handling: retries, on_failure_callback, SLA alerts
# MAGIC 9. Idempotency: design tasks that are safe to re-run
# MAGIC 10. Build 3 complete production DAGs:
# MAGIC     - Daily ETL: extract from API → load to S3 → Spark transform → quality check → load to warehouse
# MAGIC     - CDC pipeline: detect changes → process → merge → downstream refresh
# MAGIC     - ML pipeline: feature compute → train → evaluate → deploy if metrics pass
# MAGIC
# MAGIC dbt (40%):
# MAGIC 1. Project setup: models/, seeds/, tests/, macros/, snapshots/
# MAGIC 2. Models: SQL file = one model, ref() for dependencies, source() for raw tables
# MAGIC 3. Materializations: view, table, incremental (with merge strategy), ephemeral
# MAGIC 4. Incremental models: complete code with is_incremental(), unique_key, merge
# MAGIC 5. Testing: not_null, unique, accepted_values, relationships, custom generic tests
# MAGIC 6. Macros: write 3 reusable macros (date_spine, safe_divide, pivot_table)
# MAGIC 7. Snapshots: SCD Type 2 with dbt snapshot
# MAGIC 8. Packages: dbt_utils (star, pivot, date_spine, get_column_values)
# MAGIC 9. Build a complete dbt project: staging → intermediate → marts for e-commerce data
# MAGIC
# MAGIC 13 practice problems, 2 interview sims.
# MAGIC ```
# MAGIC
# MAGIC ---
# MAGIC
# MAGIC # CHAPTER 14: Cloud Platform Essentials
# MAGIC
# MAGIC ```
# MAGIC Write Chapter 14: Cloud Platform for Data Engineering (Azure primary, AWS/GCP mapping).
# MAGIC
# MAGIC Part A with code/CLI commands for each:
# MAGIC 1. Azure ADLS Gen2: create container, upload/download files, list paths (Python SDK + CLI)
# MAGIC 2. Azure Data Factory: create a pipeline conceptually (JSON definition), linked service, trigger types
# MAGIC 3. Azure Databricks: workspace setup, cluster config, notebook execution via API
# MAGIC 4. Azure Event Hubs: produce/consume events with Python SDK, Kafka protocol compatibility
# MAGIC 5. Azure Key Vault: store and retrieve secrets from Python
# MAGIC 6. AWS equivalent for each Azure service (conceptual + SDK code snippets):
# MAGIC    - S3 with boto3: create bucket, upload, download, list, lifecycle policies
# MAGIC    - Glue: crawler setup, ETL job definition
# MAGIC    - Redshift: COPY command, Spectrum for S3 queries
# MAGIC    - Lambda: event-driven data processing function
# MAGIC 7. GCP equivalent (conceptual):
# MAGIC    - BigQuery: query, load, export (bq CLI + Python client)
# MAGIC    - Pub/Sub: publish/subscribe pattern
# MAGIC    - Dataflow: Apache Beam concepts
# MAGIC 8. Multi-cloud cost optimization patterns
# MAGIC 9. IAM and security: roles, policies, service principals (code for each cloud)
# MAGIC
# MAGIC 10 practice problems: cross-cloud design decisions, cost optimization scenarios.
# MAGIC Interview sim: "Design the cloud infrastructure for a data lake serving 50 analysts and 10 ML engineers."
# MAGIC ```

# COMMAND ----------

# DBTITLE 1,CHAPTERS 15-18 PROMPTS: Data Modeling, System Design, Governance, AI Pipelines
# MAGIC %md
# MAGIC # CHAPTER 15: Data Modeling — Kimball, Data Vault, Modern Patterns
# MAGIC
# MAGIC ```
# MAGIC Write Chapter 15: Data Modeling for Data Engineering.
# MAGIC
# MAGIC Part A with COMPLETE SQL/PySpark code for every model:
# MAGIC 1. Normalization: show a denormalized table, normalize it step-by-step (1NF → 2NF → 3NF), show the CREATE TABLE + INSERT statements for each form, then query both to show tradeoff
# MAGIC 2. Star schema: design for an e-commerce business
# MAGIC    - Fact table: fact_orders (grain = one order line item)
# MAGIC    - Dimensions: dim_customer, dim_product, dim_date, dim_geography
# MAGIC    - Show full CREATE TABLE statements with PKs, FKs
# MAGIC    - Show 5 analytical queries that demonstrate why star schema is fast
# MAGIC    - Junk dimension: combine flags (is_gift, is_express, is_international) into one dimension
# MAGIC    - Degenerate dimension: order_number in fact table
# MAGIC    - Role-playing dimension: order_date, ship_date, delivery_date all linking to dim_date
# MAGIC 3. SCD Type 2 complete implementation:
# MAGIC    - Table design with surrogate_key, natural_key, valid_from, valid_to, is_current
# MAGIC    - Initial load SQL
# MAGIC    - Incremental load with MERGE INTO (handle new, changed, unchanged, deleted)
# MAGIC    - Query patterns: "what was this customer's city on 2023-06-15?"
# MAGIC    - Point-in-time join with fact table
# MAGIC 4. Data Vault 2.0: Hub + Link + Satellite for same e-commerce model
# MAGIC    - Full SQL for each table type
# MAGIC    - Loading patterns
# MAGIC    - When Data Vault beats Kimball
# MAGIC 5. Medallion architecture:
# MAGIC    - Bronze: raw JSON ingest (show schema, partition strategy)
# MAGIC    - Silver: cleaned and conformed (show transformation code)
# MAGIC    - Gold: business aggregates (show star schema built from silver)
# MAGIC    - Full PySpark pipeline from bronze → silver → gold
# MAGIC 6. One Big Table pattern: when it works, when it doesn't
# MAGIC 7. Activity schema: event-centric modeling with code
# MAGIC
# MAGIC 13 practice problems: design schemas for given business scenarios.
# MAGIC Interview sims: (1) [BCG] "Design a data model for a ride-sharing platform" (2) [Amazon] "Design the dimensional model for a marketplace with buyers, sellers, and products."
# MAGIC ```
# MAGIC
# MAGIC ---
# MAGIC
# MAGIC # CHAPTER 16: System Design for Data Engineering
# MAGIC
# MAGIC ```
# MAGIC Write Chapter 16: System Design for Data Engineering Interviews.
# MAGIC
# MAGIC This is a PURE interview chapter. Each design should be a complete walkthrough.
# MAGIC
# MAGIC For EACH design, follow this structure:
# MAGIC 1. Requirements gathering: functional + non-functional, questions to ask interviewer
# MAGIC 2. Capacity estimation: data volume, QPS, storage needs
# MAGIC 3. High-level architecture: ASCII diagram of components
# MAGIC 4. Data model: table schemas with SQL
# MAGIC 5. Component deep dive: why each technology was chosen
# MAGIC 6. Data flow: step-by-step with code snippets where relevant
# MAGIC 7. Scalability: what happens at 10x, 100x scale
# MAGIC 8. Fault tolerance: failure modes and mitigation
# MAGIC 9. Monitoring: key metrics and alerts
# MAGIC 10. Cost estimation: rough cloud cost
# MAGIC 11. Trade-offs discussed: at least 3 major design decisions with alternatives
# MAGIC
# MAGIC Design Problems (write the COMPLETE walkthrough for all 6):
# MAGIC
# MAGIC 1. Real-Time Analytics Dashboard (Google/Meta style):
# MAGIC    - Clickstream from web/mobile → real-time DAU/MAU, page views, conversion funnel
# MAGIC    - Tech: Kafka → Spark Streaming → Delta Lake → Serving layer → Dashboard
# MAGIC
# MAGIC 2. E-Commerce Data Warehouse (Amazon style):
# MAGIC    - Multiple source systems (orders, inventory, payments, reviews)
# MAGIC    - Batch + real-time, star schema, dbt transformations
# MAGIC
# MAGIC 3. Fraud Detection Pipeline (Stripe/FinTech style):
# MAGIC    - Real-time transaction scoring, feature computation, model serving
# MAGIC    - <100ms latency requirement
# MAGIC
# MAGIC 4. Data Lake for a Fast-Growing Startup (Unicorn startup style):
# MAGIC    - Start with 10GB/day, design for 10TB/day
# MAGIC    - Medallion architecture, governance, self-serve analytics
# MAGIC
# MAGIC 5. Log Analytics Platform (Uber/Datadog style):
# MAGIC    - Ingest 1M events/sec, search, alerting
# MAGIC    - Kafka → Spark → Elasticsearch + Delta Lake
# MAGIC
# MAGIC 6. ML Feature Platform (Airbnb/Uber style):
# MAGIC    - Batch + real-time features, point-in-time correctness
# MAGIC    - Feature store, online/offline serving
# MAGIC
# MAGIC Part C: System design framework cheat sheet — one page summary of the approach.
# MAGIC ```
# MAGIC
# MAGIC ---
# MAGIC
# MAGIC # CHAPTER 17: Data Governance & Security
# MAGIC
# MAGIC ```
# MAGIC Write Chapter 17: Data Governance, Security, and Compliance for DE.
# MAGIC
# MAGIC Part A with code:
# MAGIC 1. Unity Catalog: GRANT, REVOKE, CREATE CATALOG/SCHEMA, row-level security
# MAGIC 2. Column-level security: dynamic masking with SQL (show mask function)
# MAGIC 3. PII detection: Python code to scan a DataFrame for PII columns (regex for email, phone, SSN, credit card)
# MAGIC 4. Tokenization: build a tokenizer in Python (hash-based, lookup-based)
# MAGIC 5. Data encryption: field-level encryption with Python (Fernet from cryptography library)
# MAGIC 6. Right to be forgotten: complete GDPR deletion pipeline in PySpark
# MAGIC 7. Data quality framework: build a mini Great Expectations-like validator in Python
# MAGIC 8. Audit logging: log every data access with timestamp, user, query
# MAGIC 9. Data lineage: build a simple column-level lineage tracker
# MAGIC
# MAGIC 10 practice problems, 1 interview sim: "Design the data governance framework for a FinTech company handling PCI data."
# MAGIC ```
# MAGIC
# MAGIC ---
# MAGIC
# MAGIC # CHAPTER 18: DE in the AI Era — LLM Pipelines, Feature Stores, RAG
# MAGIC
# MAGIC ```
# MAGIC Write Chapter 18: Data Engineering for AI — Feature Stores, LLM Pipelines, RAG.
# MAGIC
# MAGIC Part A with COMPLETE working code:
# MAGIC 1. Feature engineering pipeline in PySpark:
# MAGIC    - Build 10 features from raw transaction data (recency, frequency, monetary, velocity, etc.)
# MAGIC    - Point-in-time correctness: show wrong way (data leakage) and right way
# MAGIC    - Serve features from Delta Lake (batch) and Redis (online)
# MAGIC
# MAGIC 2. MLflow basics for DE:
# MAGIC    - Log parameters, metrics, artifacts
# MAGIC    - Model registry: stage transitions
# MAGIC    - Code: complete training pipeline with MLflow tracking
# MAGIC
# MAGIC 3. RAG (Retrieval-Augmented Generation) pipeline:
# MAGIC    - Document loading: read PDFs/text files
# MAGIC    - Chunking: implement fixed-size, sentence-based, and recursive chunking in Python
# MAGIC    - Embeddings: generate embeddings using sentence-transformers (local, free)
# MAGIC    - Vector store: store in and query from ChromaDB (local, free)
# MAGIC    - Retrieval: similarity search with scores
# MAGIC    - Generation: prompt template with context injection
# MAGIC    - Build the COMPLETE pipeline end-to-end (50-80 lines)
# MAGIC    - Evaluation: calculate retrieval recall@5, MRR
# MAGIC
# MAGIC 4. Data pipelines for LLM:
# MAGIC    - Web scraping → cleaning → deduplication → chunking → embedding → indexing pipeline
# MAGIC    - Incremental updates: only re-embed changed documents
# MAGIC    - LLM output parsing: extract structured data from LLM responses using pydantic
# MAGIC
# MAGIC 5. Vector databases:
# MAGIC    - ChromaDB: create collection, add documents, query (full code)
# MAGIC    - Concept: HNSW indexing, ANN search, distance metrics
# MAGIC    - When to use vector DB vs traditional search
# MAGIC
# MAGIC 6. Text-to-SQL pipeline:
# MAGIC    - Build a simple text-to-SQL system: natural language → SQL query
# MAGIC    - Schema extraction, prompt engineering, SQL validation
# MAGIC
# MAGIC 10 practice problems.
# MAGIC Interview sims: (1) "Design a RAG pipeline for a customer support chatbot" (2) "Design the data platform for an AI startup that trains and serves custom models."
# MAGIC ```

# COMMAND ----------

# DBTITLE 1,CHAPTERS 19-20 + Bonus: Behavioral & Study Plan
# MAGIC %md
# MAGIC # CHAPTER 19: Behavioral Interview Prep
# MAGIC
# MAGIC ```
# MAGIC Write Chapter 19: Behavioral Interview Preparation for Data Engineers.
# MAGIC
# MAGIC This chapter must include CODE where relevant (showing the project you're describing).
# MAGIC
# MAGIC Part A:
# MAGIC 1. STAR method deep dive with 2 fully written sample answers
# MAGIC 2. Build 8 complete STAR stories that cover ALL behavioral themes:
# MAGIC
# MAGIC    Story 1 — Complex Pipeline Design:
# MAGIC    "Tell me about the most complex data pipeline you built."
# MAGIC    - STAR framework filled in (reader fills in their own details, but show a complete example)
# MAGIC    - Include: architecture decisions, tradeoffs, scale, outcome
# MAGIC    - Bonus: include a simplified architecture diagram in text
# MAGIC
# MAGIC    Story 2 — Production Incident:
# MAGIC    "Tell me about a time you debugged a critical production issue."
# MAGIC    - Timeline: alert → investigation → root cause → fix → prevention
# MAGIC    - Show: actual SQL/Python you'd write to diagnose (count checks, data quality queries)
# MAGIC
# MAGIC    Story 3 — Performance Optimization:
# MAGIC    "Tell me about a time you significantly improved performance."
# MAGIC    - Before metrics → investigation → changes → after metrics
# MAGIC    - Show: Spark configs changed, query rewrites, architecture changes
# MAGIC
# MAGIC    Story 4 — Cross-Team Collaboration:
# MAGIC    "Tell me about working with a difficult stakeholder."
# MAGIC
# MAGIC    Story 5 — Ambiguity and Changing Requirements:
# MAGIC    "Tell me about a time requirements changed mid-project."
# MAGIC
# MAGIC    Story 6 — Mentoring / Technical Leadership:
# MAGIC    "Tell me about a time you mentored someone or drove a technical decision."
# MAGIC
# MAGIC    Story 7 — Data Quality Crisis:
# MAGIC    "Tell me about a time bad data caused a business impact."
# MAGIC    - Show: the data quality checks that should have caught it
# MAGIC
# MAGIC    Story 8 — Scaling Challenge:
# MAGIC    "Tell me about a time you had to scale a system significantly."
# MAGIC
# MAGIC 3. Amazon Leadership Principles mapped to DE scenarios:
# MAGIC    - Ownership: "I don't say 'that's not my job'"
# MAGIC    - Dive Deep: "I traced a data discrepancy to a timezone bug in the source system"
# MAGIC    - Bias for Action: "When the pipeline was down, I..."
# MAGIC    - Deliver Results: quantified outcomes
# MAGIC    - Customer Obsession: "The analysts needed..."
# MAGIC
# MAGIC 4. Questions to ask the interviewer (10 smart questions for DE):
# MAGIC    - About data stack, team structure, on-call, technical debt, data quality
# MAGIC
# MAGIC Part B: Mock interview script — write a complete 30-minute behavioral interview simulation with interviewer questions and candidate responses.
# MAGIC ```
# MAGIC
# MAGIC ---
# MAGIC
# MAGIC # CHAPTER 20: 6-Month Study Plan + Quick Reference
# MAGIC
# MAGIC ```
# MAGIC Write Chapter 20: The 6-Month Study Plan and Quick Reference Cards.
# MAGIC
# MAGIC Part A — 6-Month Study Plan:
# MAGIC
# MAGIC Create a detailed week-by-week study plan assuming 3 hours/day on weekdays, 5 hours on weekends.
# MAGIC The reader has 6 full months and wants to go DEEP on every topic — no shortcuts.
# MAGIC For each week: specify which chapter to study, whether to use Claude or self-study, daily tasks, and weekend projects.
# MAGIC
# MAGIC === MONTH 1: SQL MASTERY (Chapters 1-4) — Build with Claude ===
# MAGIC
# MAGIC Week 1: Chapter 1 (SQL Foundations)
# MAGIC - Self-study alongside: install PostgreSQL locally, run every query yourself
# MAGIC - Daily: 10 DataLemur Easy problems
# MAGIC - Weekend: timed mock — 3 SQL problems in 45 minutes
# MAGIC
# MAGIC Week 2: Chapter 2 (Joins & Aggregations)
# MAGIC - Daily: 10 DataLemur Medium problems
# MAGIC - Weekend: build e-commerce schema from scratch and write 20 queries
# MAGIC
# MAGIC Week 3: Chapter 3 (Window Functions) — FIRST HALF
# MAGIC - Spend TWO FULL WEEKS here — #1 tested topic
# MAGIC - Day-by-day: ranking functions, value functions, aggregate windows
# MAGIC - Daily: 5 window function problems from LeetCode SQL + StrataScratch
# MAGIC - Weekend: solve 15 window function problems in one sitting (timed)
# MAGIC
# MAGIC Week 4: Chapter 3 (Window Functions) SECOND HALF + Chapter 4 (Advanced SQL)
# MAGIC - Day-by-day: window frames, gap-and-island, sessionization, CTEs, MERGE
# MAGIC - Weekend project: implement SCD Type 2 pipeline in PostgreSQL
# MAGIC - End-of-month: 50+ SQL problems solved, timed SQL mock interview
# MAGIC
# MAGIC === MONTH 2: PYTHON + DSA (Chapters 5-7) — Claude + self-practice ===
# MAGIC
# MAGIC Week 5: Chapter 5 (Python Core) — Build with Claude
# MAGIC - Type every code example manually — do NOT copy-paste
# MAGIC - Daily: build one utility (retry decorator, batch processor, config validator)
# MAGIC - Weekend project: CSV file processor with generators + error handling
# MAGIC
# MAGIC Week 6: Chapter 6 (Python Libraries) — SELF-STUDY with Kaggle datasets
# MAGIC - Claude output is reference only — learn by fighting real messy data
# MAGIC - Pick 3 messy Kaggle datasets and clean them end-to-end
# MAGIC - Weekend project: API ingestion script (pagination, retry, rate limiting, write Parquet)
# MAGIC
# MAGIC Week 7-8: Chapter 7 (DSA for OA Rounds) — Claude + LeetCode daily
# MAGIC - 2 LeetCode problems per day (hash map, sliding window, sorting, strings)
# MAGIC - Weekend: timed OA simulation (4 problems in 90 minutes)
# MAGIC - End-of-month project: build mini ETL framework in Python
# MAGIC - End-of-month: 40+ DSA problems solved, 2 timed OA simulations
# MAGIC
# MAGIC === MONTH 3: BIG DATA + STORAGE (Chapters 8-10) — Claude + Databricks ===
# MAGIC
# MAGIC Week 9-10: Chapter 8 (Spark Deep Dive) — Build with Claude, run on Databricks
# MAGIC - Week 9: architecture, DataFrame API, joins, UDFs
# MAGIC - Week 10: performance tuning, Spark UI, AQE, skew handling, caching
# MAGIC - Run EVERY example on Databricks Community Edition yourself
# MAGIC - Weekend project (Wk 10): end-to-end Spark pipeline — CSV → Delta with MERGE + OPTIMIZE
# MAGIC
# MAGIC Week 11: Chapter 9 (Delta Lake) — Claude + Databricks hands-on
# MAGIC - ACID, schema evolution, time travel, MERGE patterns, OPTIMIZE, VACUUM, CDF
# MAGIC - Weekend: incremental pipeline with Change Data Feed
# MAGIC
# MAGIC Week 12: Chapter 10 (Database Internals) — READ DDIA BOOK (self-study)
# MAGIC - Read "Designing Data-Intensive Applications" chapters 1-3, 5-7
# MAGIC - DDIA cannot be replaced by Claude — builds mental models for system design
# MAGIC - Use Claude only for practice problems
# MAGIC - Setup: local PostgreSQL, practice EXPLAIN ANALYZE, indexes
# MAGIC - End-of-month: can you read a Spark UI and diagnose performance issues?
# MAGIC
# MAGIC === MONTH 4: STREAMING + ORCHESTRATION (Chapters 11-14) — Mostly labs ===
# MAGIC
# MAGIC Week 13: Chapter 11 (Kafka & Streaming) — Docker lab FIRST, then Claude
# MAGIC - Setup: docker-compose with Kafka + Zookeeper + Schema Registry
# MAGIC - You MUST see consumer lag, rebalancing, offset commits live
# MAGIC - Days 1-3: Kafka basics with Docker (produce, consume, observe)
# MAGIC - Days 4-5: Spark Structured Streaming (Kafka → Delta)
# MAGIC - Weekend project: Kafka producer → Spark Streaming → Delta Lake pipeline
# MAGIC
# MAGIC Week 14: Chapter 12 (CDC) — Docker lab: Postgres + Debezium + Kafka
# MAGIC - Make changes in Postgres, watch CDC events flow through Kafka
# MAGIC - Use Claude only for MERGE patterns (applying CDC to Delta Lake)
# MAGIC - Weekend: complete CDC → Delta Lake → SCD Type 2 pipeline
# MAGIC
# MAGIC Week 15: Chapter 13 (Airflow & dbt) — Official courses + Claude
# MAGIC - Days 1-2: dbt Learn free course (https://courses.getdbt.com/)
# MAGIC - Days 3-4: Astronomer Airflow tutorial, local docker Airflow
# MAGIC - Day 5: Claude for complete DAG + dbt project code
# MAGIC - Weekend: dbt project (staging → intermediate → marts) + Airflow DAG
# MAGIC
# MAGIC Week 16: Chapter 14 (Cloud Platform) — Azure free tier hands-on
# MAGIC - Days 1-2: ADLS Gen2 (Python SDK)
# MAGIC - Day 3: Azure Event Hubs
# MAGIC - Day 4: Azure Key Vault
# MAGIC - Day 5: Azure Data Factory
# MAGIC - Use Claude for AWS/GCP mapping reference only
# MAGIC - End-of-month: can you build Kafka → Spark → Delta and orchestrate with Airflow?
# MAGIC
# MAGIC === MONTH 5: ARCHITECTURE + AI ERA (Chapters 15-18) — Claude + whiteboard ===
# MAGIC
# MAGIC Week 17: Chapter 15 (Data Modeling) — Build with Claude
# MAGIC - Generate 5 schema designs with Claude, implement each in SQL
# MAGIC - Weekend: design + implement star schema for a FinTech use case
# MAGIC
# MAGIC Week 18: Chapter 16 (System Design) — MOST IMPORTANT WEEK
# MAGIC - Daily: one full system design problem (45 min on paper, then check with Claude)
# MAGIC - Cover all 6 designs from the prompt
# MAGIC - Weekend: redo 2 weakest designs from memory without notes
# MAGIC
# MAGIC Week 19: Chapter 17 (Governance) — Self-study + Claude for PII code
# MAGIC - Practice GRANT/REVOKE on your Databricks Unity Catalog
# MAGIC - Weekend: implement row-level and column-level security
# MAGIC
# MAGIC Week 20: Chapter 18 (AI Era: RAG, Feature Stores, LLM Pipelines)
# MAGIC - Days 1-3: BUILD a RAG pipeline end-to-end with LangChain + ChromaDB
# MAGIC - Days 4-5: Claude for feature store concepts, MLOps, LLM pipeline architecture
# MAGIC - Also read: remaining DDIA chapters (7-12) throughout this month
# MAGIC - End-of-month: can you whiteboard a complete system design in 45 min?
# MAGIC
# MAGIC === MONTH 6: INTERVIEW MODE (Chapters 19-20 + full mocks) ===
# MAGIC
# MAGIC Week 21: Chapter 19 (Behavioral Prep) — Build with Claude
# MAGIC - Write all 8 STAR stories, practice OUT LOUD daily, record yourself
# MAGIC - Weekend: 2 full behavioral mock interviews
# MAGIC
# MAGIC Week 22: Chapter 20 (Quick Reference Cards) + Revision
# MAGIC - Generate all 10 cheat sheets with Claude, print them
# MAGIC - Revisit weakest 3 topics from Months 1-5
# MAGIC - Weekend: 1 SQL mock + 1 system design mock
# MAGIC
# MAGIC Week 23-24: FULL MOCK INTERVIEW SPRINT
# MAGIC - Monday: SQL mock (45 min), Tuesday: Python/DSA mock (45 min)
# MAGIC - Wednesday: System Design mock (60 min), Thursday: Spark technical deep dive
# MAGIC - Friday: Behavioral mock (30 min, recorded)
# MAGIC - Weekend: review all results, targeted practice on weak areas
# MAGIC - Repeat Week 23 pattern in Week 24 with different problems
# MAGIC - Start applying to companies — real interviews as warm-up practice
# MAGIC
# MAGIC Week 25-26 (Buffer): Active interviewing + targeted fixing
# MAGIC - Interview at 2-3 less-preferred companies as warm-up
# MAGIC - After each: write every question asked, research correct answers, fix gaps
# MAGIC - Then interview at target companies
# MAGIC
# MAGIC Part B — Quick Reference Cards (one page each):
# MAGIC 1. SQL Window Functions cheat sheet (all functions + frame types)
# MAGIC 2. Spark Config cheat sheet (top 20 configs with default values and when to change)
# MAGIC 3. Delta Lake commands cheat sheet
# MAGIC 4. Kafka commands cheat sheet (producer, consumer, topics CLI)
# MAGIC 5. Airflow DAG template
# MAGIC 6. dbt model template (incremental + test + doc)
# MAGIC 7. System design template (blank framework to fill)
# MAGIC 8. Behavioral STAR template (blank for each story)
# MAGIC 9. Python complexity cheat sheet (all data structure operations)
# MAGIC 10. File format decision tree
# MAGIC
# MAGIC Part C — Month-by-Month Milestone Checklist:
# MAGIC For each month, generate a self-assessment checklist of 10 things the reader should be able to do.
# MAGIC Example Month 1: "Can you write a window function with custom frame from memory?"
# MAGIC Example Month 3: "Can you read a Spark UI stage view and identify a data skew?"
# MAGIC Example Month 5: "Can you whiteboard a fraud detection pipeline in 45 min?"
# MAGIC If any answer is 'No', specify which chapter section to revisit.
# MAGIC ```
# MAGIC
# MAGIC ---
# MAGIC
# MAGIC # BONUS: Ongoing Practice Prompts
# MAGIC
# MAGIC Use these prompts AFTER completing the book for continued practice:
# MAGIC
# MAGIC ```
# MAGIC ## Daily SQL Practice (paste every day):
# MAGIC "Give me 1 hard SQL interview problem that was asked at [company]. Include the table schema, sample data, expected output, and solution. Make it involve window functions, CTEs, or a tricky join."
# MAGIC
# MAGIC ## Weekly System Design Practice:
# MAGIC "Give me a data engineering system design problem that [company] would ask. Walk me through the complete design. After I attempt it, critique my design and suggest improvements."
# MAGIC
# MAGIC ## Mock Interview:
# MAGIC "Conduct a 45-minute DE technical interview for a [company] position. Ask me SQL, Python, and system design questions. Wait for my answers before providing feedback. Be tough but fair. Grade me at the end."
# MAGIC ```

# COMMAND ----------

# DBTITLE 1,PRO TIPS: Getting Maximum Quality from Claude
# MAGIC %md
# MAGIC # PRO TIPS: Getting the Best Output from Claude
# MAGIC
# MAGIC ---
# MAGIC
# MAGIC ## 1. Use Claude's Project Feature (if available)
# MAGIC Upload the Master System Prompt as a "Project Knowledge" file. This way you don't need to re-paste it every conversation.
# MAGIC
# MAGIC ## 2. Chapter Length Management
# MAGIC Claude has output limits. If a chapter gets cut off mid-way:
# MAGIC ```
# MAGIC Continue exactly where you left off. Do not repeat anything already generated.
# MAGIC You were in the middle of: [paste the last heading/section title]
# MAGIC ```
# MAGIC
# MAGIC ## 3. If Claude Gets Lazy (shorter answers)
# MAGIC ```
# MAGIC Your response was too short. Remember the rules:
# MAGIC - Every concept needs FULL runnable code
# MAGIC - Every problem needs COMPLETE solution (not hints)
# MAGIC - Show expected output for every code block
# MAGIC - Minimum 13 practice problems per chapter
# MAGIC Regenerate Part [A/B/C] with full detail.
# MAGIC ```
# MAGIC
# MAGIC ## 4. Generate More Practice Problems
# MAGIC After each chapter, you can ask:
# MAGIC ```
# MAGIC Generate 10 additional practice problems for Chapter [N] at interview difficulty.
# MAGIC Focus on [company] style questions. Include full solutions.
# MAGIC Make problems 1-3 about [specific sub-topic] and problems 4-7 about [another sub-topic].
# MAGIC ```
# MAGIC
# MAGIC ## 5. Get Interview-Specific Variants
# MAGIC ```
# MAGIC Adapt the Chapter [N] content specifically for [Stripe/Google/Amazon] interviews.
# MAGIC What specific variations would they ask? What do they emphasize?
# MAGIC Generate 5 company-specific practice problems.
# MAGIC ```
# MAGIC
# MAGIC ## 6. Review and Critique Your Code
# MAGIC After you practice, paste your solution:
# MAGIC ```
# MAGIC I wrote this solution for [problem description]. Review it:
# MAGIC [paste your code]
# MAGIC
# MAGIC Tell me:
# MAGIC 1. Is it correct?
# MAGIC 2. What's the time and space complexity?
# MAGIC 3. What would an interviewer critique?
# MAGIC 4. How would you improve it?
# MAGIC 5. What edge cases did I miss?
# MAGIC ```
# MAGIC
# MAGIC ## 7. Mock Interview Mode
# MAGIC ```
# MAGIC Conduct a mock [SQL/Python/System Design] interview for a Data Engineer role at [company].
# MAGIC Rules:
# MAGIC - Ask one question at a time
# MAGIC - Wait for my answer before responding
# MAGIC - After each answer, give brief feedback and score (1-5)
# MAGIC - If I'm stuck, give a hint (not the answer)
# MAGIC - At the end, give overall feedback and areas to improve
# MAGIC - Be as tough as a real interviewer
# MAGIC Start now.
# MAGIC ```
# MAGIC
# MAGIC ## 8. Estimated Book Statistics
# MAGIC | Metric | Estimate |
# MAGIC |--------|----------|
# MAGIC | Total chapters | 20 |
# MAGIC | Total pages (if printed) | \~650-800 |
# MAGIC | Total practice problems | \~250+ |
# MAGIC | Total interview simulations | \~40 |
# MAGIC | Total code examples | \~500+ |
# MAGIC | Study time to complete | \~6 months (3 hrs/day weekdays, 5 hrs weekends) |
# MAGIC | Claude conversations needed | \~25-30 (one per chapter + extras) |
# MAGIC
# MAGIC ## 9. 6-Month Roadmap Summary
# MAGIC | Month | Focus | Source | Key Milestone |
# MAGIC |-------|-------|--------|---------------|
# MAGIC | **Month 1** | SQL Mastery (Ch 1-4) | Claude + PostgreSQL | 50+ SQL problems, window functions from memory |
# MAGIC | **Month 2** | Python + DSA (Ch 5-7) | Claude + LeetCode + Kaggle | Mini ETL framework built, 40+ DSA problems |
# MAGIC | **Month 3** | Spark + Delta + DB (Ch 8-10) | Claude + Databricks + DDIA book | E2E Spark pipeline running, can read Spark UI |
# MAGIC | **Month 4** | Streaming + Orchestration (Ch 11-14) | Docker labs + free courses + Claude | CDC pipeline, Airflow DAG + dbt project built |
# MAGIC | **Month 5** | Architecture + AI Era (Ch 15-18) | Claude + whiteboard + RAG project | System design in 45 min, RAG pipeline built |
# MAGIC | **Month 6** | Interview Mode (Ch 19-20) | Claude + real interviews | 8 STAR stories, 10+ mock interviews done |
# MAGIC
# MAGIC ## 10. Claude vs Self-Study Split
# MAGIC | Build with Claude (concepts + practice) | Self-Study (hands-on + books) |
# MAGIC |----------------------------------------|-------------------------------|
# MAGIC | Ch 1-4 (SQL) — all practice problems | PostgreSQL local — run queries yourself |
# MAGIC | Ch 5 (Python Core) — patterns + code | Ch 6 (Libraries) — Kaggle datasets, real messy data |
# MAGIC | Ch 7 (DSA) — pattern teaching | LeetCode/DataLemur — solve problems timed |
# MAGIC | Ch 8-9 (Spark + Delta) — concepts | Databricks Community Edition — run everything |
# MAGIC | Ch 15 (Data Modeling) — schema designs | DDIA book — chapters 1-3, 5-7 (irreplaceable) |
# MAGIC | Ch 16 (System Design) — walkthroughs | Whiteboard practice — draw, don't just read |
# MAGIC | Ch 18 (AI/RAG) — architecture | Build one RAG pipeline yourself end-to-end |
# MAGIC | Ch 19 (Behavioral) — story structuring | Ch 11-14 (Kafka, CDC, Airflow, Cloud) — Docker labs |
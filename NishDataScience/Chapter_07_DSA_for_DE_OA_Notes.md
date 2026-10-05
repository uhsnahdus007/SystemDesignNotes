# Chapter 7: DSA Patterns for Data Engineering Online Assessments

> **Verification status.** All code in this chapter is pure Python 3.12 and **was executed**. Each function has `assert` tests (edge cases included); `subarray_sum_k` was also checked against a brute-force version on 200 random inputs. Printed outputs are real. Complexity tables are standard analysis, except the last table, which I **measured** (µs per operation on a small VM; trust the *ratios*, not the absolute numbers).
> Company tags like [Amazon] reflect problem *types* commonly reported for these companies; I can't verify any company's actual question bank, so treat tags as guidance.
> **Format:** minimal theory. For each problem: statement → brute force → optimal → code → tests.

---

## Part A — Pattern-Based Learning

### Pattern 1: Hash Map (appears in most DE OAs)
**Use when:** you need O(1) lookup, counting, grouping, or "have I seen this before?". Trade memory for time.

**Problem 1: Two Sum** [Amazon][Google]. Return indices of two numbers that add to `target`.
- *Brute force:* all pairs, **O(n²)** time, O(1) space.
- *Optimal:* one pass, store `value → index`; for each `x` check whether `target − x` was seen. **O(n)** time, O(n) space.
```python
def two_sum(nums: List[int], target: int) -> Optional[Tuple[int, int]]:
    seen: Dict[int, int] = {}
    for i, x in enumerate(nums):
        if target - x in seen: return seen[target - x], i
        seen[x] = i                      # insert AFTER checking: prevents using the same element twice
    return None
```
✅ Tests: `[2,7,11,15],9 → (0,1)` · `[3,2,4],6 → (1,2)` · `[3,3],6 → (0,1)` (duplicates) · `[1,2],7 → None`.

**Problem 2: Group Anagrams** [Amazon][Meta].
- *Brute force:* compare every pair, O(n² · k log k). *Optimal:* key = sorted letters (or a 26-count tuple), group in a dict; **O(n · k log k)**.
```python
def group_anagrams(words: List[str]) -> List[List[str]]:
    g = defaultdict(list)
    for w in words: g[tuple(sorted(w))].append(w)
    return list(g.values())
```
✅ `["eat","tea","tan","ate","nat","bat"] → [['ate','eat','tea'], ['bat'], ['nat','tan']]` (after sorting for display). Use a 26-int count tuple as key to get **O(n·k)**.

**Problem 3: Subarray Sum Equals K** [Meta][Google]. Count contiguous subarrays summing to `k` (negatives allowed).
- *Brute force:* all (i, j), O(n²) (O(n³) if you re-sum).
- *Optimal:* **prefix sums + hash map**: a subarray ending here sums to `k` iff some earlier prefix equals `prefix − k`. **O(n)** time and space.
```python
def subarray_sum_k(nums: List[int], k: int) -> int:
    count, prefix, seen = 0, 0, Counter({0: 1})        # empty prefix has sum 0 (counts subarrays starting at index 0)
    for x in nums:
        prefix += x
        count += seen[prefix - k]                      # how many earlier prefixes make a sum-k subarray ending here
        seen[prefix] += 1
    return count
```
✅ `[1,1,1],2 → 2` · `[1,2,3],3 → 2` · `[1,-1,0],0 → 3` · `[] → 0` · matches brute force on 200 random arrays (negatives, zeros). > **Common mistake:** a sliding window fails here because negatives break monotonicity; and forgetting `Counter({0:1})` misses subarrays that start at index 0.

**DE-specific A: Deduplicate by composite key, keep latest** (the SQL `ROW_NUMBER() ... = 1` in Python).
```python
def dedupe_by_key(rows, keys, ts):
    best = {}
    for r in rows:
        k = tuple(r[c] for c in keys)
        if k not in best or r[ts] > best[k][ts]: best[k] = r
    return list(best.values())
```
✅ Rows `(id=1,ts=1,'old')`, `(id=1,ts=5,'new')`, `(id=2,ts=2,'x')` → `[(1,'new'), (2,'x')]`. O(n). State the tie rule (`>` keeps the first of equal timestamps; `>=` keeps the last).

**DE-specific B: Match records across two datasets** (join / anti-join):
```python
def match_across(a, b, key):
    idx = defaultdict(list)
    for r in b: idx[r[key]].append(r)                        # build side = hash of B
    matched = [(r, m) for r in a for m in idx.get(r[key], [])]
    bkeys, akeys = {r[key] for r in b}, {r[key] for r in a}
    return matched, [r for r in a if r[key] not in bkeys], [r for r in b if r[key] not in akeys]
```
✅ A=`[1,2,3]`, B=`[2,2,4]` → 2 matches (many-to-one preserved), only-in-A `[1,3]`, only-in-B `[4]`. O(n+m): a **hash join**, the same idea Spark uses.

---

### Pattern 2: Sliding Window
**Use when:** contiguous subarray/substring + a running condition. Expand right, shrink left; each element enters/leaves once → **O(n)**.

**Problem 4: Max sum of k consecutive elements (fixed window)** [Amazon]. *Brute:* sum each window, O(nk). *Optimal:* add the new element, drop the old, O(n).
```python
def max_sum_k(nums, k):
    if k > len(nums): raise ValueError("k > len")
    cur = best = sum(nums[:k])
    for i in range(k, len(nums)):
        cur += nums[i] - nums[i - k]; best = max(best, cur)
    return best
```
✅ `[2,1,5,1,3,2],3 → 9` · `[-1,-2,-3],2 → -3` (all negative; don't initialise `best = 0`).

**Problem 5: Longest substring without repeating characters (variable window)** [Meta][Amazon]. *Brute:* all substrings, O(n²–n³). *Optimal:* map char → last index; move `left` past a repeat. O(n), O(min(n, alphabet)).
```python
def longest_unique_substring(s: str) -> int:
    last, left, best = {}, 0, 0
    for r, ch in enumerate(s):
        if ch in last and last[ch] >= left: left = last[ch] + 1      # only if the previous occurrence is INSIDE the window
        last[ch] = r; best = max(best, r - left + 1)
    return best
```
✅ `"abcabcbb"→3` · `"bbbbb"→1` · `"pwwkew"→3` · `""→0` · `"abba"→2`. > **Common mistake:** without `last[ch] >= left`, `"abba"` returns 3 (the left pointer jumps backwards).

**DE-specific A: Peak traffic window in server logs.** Max events in any window of N time units:
```python
def peak_window(timestamps, window):
    ts = sorted(timestamps); left = 0; best = (0, 0)
    for r in range(len(ts)):
        while ts[r] - ts[left] >= window: left += 1
        if r - left + 1 > best[0]: best = (r - left + 1, ts[left])
    return best
```
✅ `[1,2,3,10,11,12,13,50], window=5 → (4, 10)` (four events in `[10,15)`). O(n log n) for the sort, O(n) sweep.

**DE-specific B: Sliding-window rate limiter** (also OA2-Q2): per-user deque of timestamps; evict expired, allow if under the limit.
```python
class SlidingWindowRateLimiter:
    def __init__(self, limit, window): self.limit, self.window, self.q = limit, window, defaultdict(deque)
    def allow(self, user, ts):
        q = self.q[user]
        while q and q[0] <= ts - self.window: q.popleft()
        if len(q) < self.limit: q.append(ts); return True
        return False
```
✅ limit 3 per 10 s, requests at `1,2,3,4,11,12,13` → `[True, True, True, False, True, True, True]`: the 4th is blocked; by 11, 12, 13 the earlier requests at 1, 2, 3 have each aged out (`q[0] <= ts - window`, so a request exactly `window` seconds old is expired; **state this boundary**). Rejected requests are *not* recorded. O(1) amortized per call, O(limit) memory per user.

---

### Pattern 3: Two Pointers
**Use when:** sorted data, or merging/intersecting two ordered sequences. O(n) time, O(1) extra space.

**Problem 6: Two Sum on a sorted array** [Amazon]. *Brute:* O(n²); *Hash:* O(n) space; *Two pointers:* O(n) time, O(1) space.
```python
def two_sum_sorted(a, target):
    i, j = 0, len(a) - 1
    while i < j:
        s = a[i] + a[j]
        if s == target: return i, j
        i, j = (i + 1, j) if s < target else (i, j - 1)
    return None
```
✅ `[1,2,3,4,6],6 → (1,3)`.

**Problem 7: Merge two sorted arrays** (the merge step of merge sort):
```python
def merge_sorted(a, b):
    i = j = 0; out = []
    while i < len(a) and j < len(b):
        if a[i] <= b[j]: out.append(a[i]); i += 1          # '<=' keeps it stable (ties from a first)
        else: out.append(b[j]); j += 1
    return out + a[i:] + b[j:]
```
✅ `[1,4,9]+[2,3,10] → [1,2,3,4,9,10]` · `[]+[1] → [1]`.

**Problem 8 / DE-specific A: Merge two event streams by timestamp** (same algorithm, generic key):
✅ `[(1,a1),(5,a2)]` + `[(2,b1),(3,b2),(9,b3)]` → `[(1,'a1'),(2,'b1'),(3,'b2'),(5,'a2'),(9,'b3')]`. For streams too large for memory use generators / `heapq.merge`.

**DE-specific B: Find overlapping time ranges** between two sorted lists of disjoint intervals:
```python
def overlapping_ranges(a, b):
    i = j = 0; out = []
    while i < len(a) and j < len(b):
        lo, hi = max(a[i][0], b[j][0]), min(a[i][1], b[j][1])
        if lo < hi: out.append((lo, hi))
        if a[i][1] < b[j][1]: i += 1                      # advance whichever interval ends first
        else: j += 1
    return out
```
✅ `[(0,5),(8,12)]` ∩ `[(3,9),(11,15)]` → `[(3,5),(8,9),(11,12)]`. (Use `lo <= hi` if touching intervals should count.)

---

### Pattern 4: Sorting + Greedy
**Problem 9: Merge Intervals** [Amazon][Meta][Google] (very common). *Brute:* repeatedly merge pairs, O(n²). *Optimal:* sort by start, extend the last interval when `start <= last_end`. **O(n log n)**.
```python
def merge_intervals(iv):
    out = []
    for s, e in sorted(iv):
        if out and s <= out[-1][1]: out[-1][1] = max(out[-1][1], e)       # max(): handles fully-contained intervals
        else: out.append([s, e])
    return out
```
✅ `[[1,3],[2,6],[8,10],[15,18]] → [[1,6],[8,10],[15,18]]` · `[[1,4],[4,5]] → [[1,5]]` (touching merges) · `[] → []`.

**Problem 10: Meeting Rooms II** (min resources) [Amazon][Google]. *Brute:* check overlap per time point. *Optimal:* sort starts and ends separately; sweep.
```python
def min_meeting_rooms(iv):
    starts, ends = sorted(s for s, _ in iv), sorted(e for _, e in iv); rooms = best = j = 0
    for s in starts:
        while ends[j] <= s: j += 1; rooms -= 1             # a meeting ending exactly at s frees its room
        rooms += 1; best = max(best, rooms)
    return best
```
✅ `[[0,30],[5,10],[15,20]] → 2` · `[[7,10],[2,4]] → 1` · `[[1,5],[5,8]] → 1` (back-to-back reuse). DE reading: *minimum concurrent Spark executors/DB connections needed for a set of jobs.*

**DE-specific A: Optimal batch scheduling (minimize idle/makespan)** with a min-heap of worker-free times:
```python
def schedule_batches(jobs, workers):          # jobs: (name, ready_time, duration)
    free = [(0, w) for w in range(workers)]; heapq.heapify(free); plan = []
    for name, ready, dur in sorted(jobs, key=lambda j: j[1]):
        t, w = heapq.heappop(free); start = max(t, ready)
        plan.append((name, w, start, start + dur)); heapq.heappush(free, (start + dur, w))
    return max(p[3] for p in plan), plan
```
✅ jobs `a(0,4) b(0,3) c(1,2) d(6,2)`, 2 workers → makespan **8**: `a→w0 0-4, b→w1 0-3, c→w1 3-5, d→w0 6-8`. This greedy (earliest-free worker) is a heuristic, not always optimal (the general problem is NP-hard); say so.

**DE-specific B: File compaction (first-fit-decreasing bin packing)** into target-size groups:
```python
def compact_files(sizes, target):
    bins = []
    for s in sorted(sizes, reverse=True):
        for b in bins:
            if sum(b) + s <= target: b.append(s); break
        else: bins.append([s])
    return bins
```
✅ sizes `[100,20,60,70,8,50,30]`, target 128 → `[[100,20,8],[70,50],[60,30]]` (3 compacted files from 7). FFD is a good approximation (≤ 11/9·OPT + 6/9). **Edge case:** a file larger than `target` gets its own bin.

---

### Pattern 5: Stack
**Problem 11: Valid Parentheses** [Amazon][Meta]. Push openers, pop and match on closers; valid only if the stack ends empty. O(n).
```python
def valid_parentheses(s):
    pairs = {")": "(", "]": "[", "}": "{"}; st = []
    for ch in s:
        if ch in "([{": st.append(ch)
        elif ch in pairs:
            if not st or st.pop() != pairs[ch]: return False
    return not st
```
✅ `"()[]{}"→T` · `"(]"→F` · `"("→F` · `""→T` · `")("→F`.

**Problem 12: Daily Temperatures (monotonic stack)** [Amazon][Google]. *Brute:* scan forward each day, O(n²). *Optimal:* stack of indices with decreasing temperatures; a warmer day resolves everything colder on the stack. O(n).
```python
def daily_temperatures(t):
    ans, st = [0] * len(t), []
    for i, x in enumerate(t):
        while st and t[st[-1]] < x: j = st.pop(); ans[j] = i - j
        st.append(i)
    return ans
```
✅ `[73,74,75,71,69,72,76,73] → [1,1,4,2,1,1,0,0]`.

**Problem 13 / DE-specific A: Parse nested JSON manually** (recursive descent; the call stack *is* the stack). Handles objects, arrays, strings with escapes, numbers, `true/false/null`, reports position on errors.
```python
def parse_json_manual(s: str): ...      # ~35 lines; full code in my test file; uses a shared index i and mutually recursive val()/string()
```
✅ A document with nested objects/arrays, an escaped `\n`, a float `4.5e1`, `true`, `null` parses **equal to `json.loads`**; `'{"a": [1, 2}'` raises `expected ']' at 11`. (Interview answer: *use `json.loads`; implement this only if asked to reason about parsing.*)

**DE-specific B: Validate parentheses in SQL, ignoring strings and comments:**
```python
def balanced_sql(q):  # track depth; skip quoted '...' (with '' escapes) and -- comments
```
✅ `SELECT (a + (b*2)) FROM t WHERE x = ')' -- )` → **True** (the `)` in the string and the comment are ignored) · `SELECT (a FROM t` → False · `SELECT a) FROM t` → False. A naive counter would get the first wrong.

---

### Pattern 6: BFS/DFS & Graphs
**Problem 14: Number of Islands** [Amazon][Meta]. Each unvisited `'1'` starts a DFS/BFS flooding its component; count the starts. O(R·C). Use an **explicit stack** to avoid Python's recursion limit.
```python
def num_islands(g):
    if not g: return 0
    R, C, seen, n = len(g), len(g[0]), set(), 0
    for r in range(R):
        for c in range(C):
            if g[r][c] == "1" and (r, c) not in seen:
                n += 1; st = [(r, c)]; seen.add((r, c))
                while st:
                    y, x = st.pop()
                    for dy, dx in ((1,0),(-1,0),(0,1),(0,-1)):
                        ny, nx = y + dy, x + dx
                        if 0 <= ny < R and 0 <= nx < C and g[ny][nx] == "1" and (ny, nx) not in seen: seen.add((ny, nx)); st.append((ny, nx))
    return n
```
✅ `["11000","11000","00100","00011"] → 3` · `[] → 0` · `[["0"]] → 0`. (DE analogue: connected components = clustering linked records for entity resolution.)

**Problem 15: Course Schedule / Topological Sort** [Amazon][Google]: **critical for DAG-based pipelines.** Kahn's algorithm: repeatedly run nodes with in-degree 0; if you can't process all nodes, there's a cycle. O(V+E).
```python
def topo_order(deps):                    # deps: task -> tasks it DEPENDS ON
    nodes = set(deps) | {d for ds in deps.values() for d in ds}
    indeg = {n: 0 for n in nodes}; out = defaultdict(list)
    for t, ds in deps.items():
        for d in ds: out[d].append(t); indeg[t] += 1
    q = deque(sorted(n for n in nodes if indeg[n] == 0)); order = []
    while q:
        n = q.popleft(); order.append(n)
        for m in sorted(out[n]):
            indeg[m] -= 1
            if indeg[m] == 0: q.append(m)
    if len(order) != len(nodes): raise ValueError(f"circular dependency among: {sorted(n for n in nodes if indeg[n] > 0)}")
    return order
```
✅ Pipeline `load_dw ← {transform, quality}`, `transform ← {extract_a, extract_b}`, `quality ← {transform}`, `report ← {load_dw}` →
`['extract_a','extract_b','transform','quality','load_dw','report']`. (`sorted(...)` makes the order deterministic; any valid topological order is accepted in interviews.) `can_finish(2, [[1,0]]) = True`; `can_finish(2, [[1,0],[0,1]]) = False`. Note **Course Schedule's `[a, b]` means "b before a"**: easy to invert.

**DE-specific: Detect and *report* circular dependencies** (DFS with 3 colours returns the actual cycle path, far more useful than "cycle detected"):
```python
def find_cycle(deps):   # WHITE/GRAY/BLACK DFS; a GRAY neighbour = back edge = cycle
```
✅ `{a→b, b→c, c→a, d}`: `topo_order` raises `circular dependency among: ['a','b','c']`, and `find_cycle` returns `['a','b','c','a']`; on the valid pipeline it returns `None`. (This is what Airflow does when it refuses to load a DAG with a cycle.)

---

### Pattern 7: String Processing (DE-heavy)
**Log parsing** (Apache/nginx-style): a compiled regex with named groups, returning `None` for garbage (never crash a pipeline on one bad line):
```python
LOG_RE = re.compile(r'^(?P<ip>\S+) \S+ \S+ \[(?P<ts>[^\]]+)\] "(?P<method>[A-Z]+) (?P<path>\S+) \S+" (?P<status>\d{3}) (?P<bytes>\d+|-)$')
```
✅ `203.0.113.9 - - [10/Mar/2024:13:55:36 +0000] "GET /api/orders?id=7 HTTP/1.1" 502 -` →
`{'ip':'203.0.113.9','ts':'10/Mar/2024:13:55:36 +0000','method':'GET','path':'/api/orders?id=7','status':502,'bytes':0}`; `parse_log("garbage line") → None`. Note `-` means 0 bytes. Compile the regex once, outside the loop.

**URL parsing** (`urllib.parse.urlsplit`, `parse_qs`):
✅ `https://shop.example.com:8443/a/b?x=1&y=2&y=3#top` → `{'protocol':'https','domain':'shop.example.com','port':8443,'path':'/a/b','query':{'x':'1','y':['2','3']},'fragment':'top'}` (repeated params become lists).

**Custom CSV parser** (quotes, escaped quotes `""`, embedded delimiters, empty fields) as a small state machine:
```python
def parse_csv_line(line, delim=","):
    out, cur, inq, i = [], [], False, 0
    while i < len(line):
        c = line[i]
        if inq:
            if c == '"' and line[i+1:i+2] == '"': cur.append('"'); i += 1
            elif c == '"': inq = False
            else: cur.append(c)
        elif c == '"': inq = True
        elif c == delim: out.append("".join(cur)); cur = []
        else: cur.append(c)
        i += 1
    if inq: raise ValueError("unterminated quote")
    out.append("".join(cur)); return out
```
✅ `1,"Smith, John","He said ""hi""",,end` → `['1','Smith, John','He said "hi"','','end']` and **identical to Python's `csv` module** on that input. (Multi-line quoted fields need a stateful reader across lines; production code uses `csv`.)

**JSON path extractor** (`"data.users[0].name"`):
```python
def json_path(obj, path):
    cur = obj
    for t in re.findall(r"[^.\[\]]+|\[\d+\]", path):
        cur = cur[int(t[1:-1])] if t.startswith("[") else cur[t]
    return cur
```
✅ `json_path(doc, "data.users[0].name") → 'Asha'`. Extend with a default value / `KeyError` handling for missing keys.

---

### Pattern 8: Heap / Priority Queue
**Problem 16: Top K Frequent Elements** [Amazon][Meta]. *Brute:* sort all by count, O(n log n). *Optimal:* count + `heapq.nlargest` O(n log k), or bucket sort O(n).
✅ `top_k_frequent([1,1,1,2,2,3], 2) → [1, 2]`.
**Problem 17: Merge K Sorted Lists** [Amazon][Google]. *Brute:* concatenate + sort O(N log N). *Optimal:* min-heap of `(value, list_idx, elem_idx)`: **O(N log K)**, O(K) space.
```python
def merge_k_sorted(lists):
    h = [(l[0], i, 0) for i, l in enumerate(lists) if l]; heapq.heapify(h); out = []
    while h:
        v, i, j = heapq.heappop(h); out.append(v)
        if j + 1 < len(lists[i]): heapq.heappush(h, (lists[i][j + 1], i, j + 1))
    return out
```
✅ `[[1,4,7],[2,5],[3,6,9]] → [1,2,3,4,5,6,7,9]`.
**DE-specific A: K-way merge of sorted files:** `heapq.merge(*files, key=...)` is lazy (O(K) memory): the engine of external merge sort and compaction. ✅ `[(1,a),(4,a)] + [(2,b),(3,b)] + [(0,c)] → [(0,c),(1,a),(2,b),(3,b),(4,a)]`.
**DE-specific B: Top-K from a stream** with a bounded **min**-heap (the heap's root is the smallest of the current top K, so a new item only replaces it if it's larger):
```python
class TopK:
    def __init__(self, k): self.k, self.h = k, []
    def add(self, score, item):
        if len(self.h) < self.k: heapq.heappush(self.h, (score, item))
        elif score > self.h[0][0]: heapq.heapreplace(self.h, (score, item))
    def result(self): return sorted(self.h, reverse=True)
```
✅ scores `[5,1,9,3,7,2,8]`, k=3 → `[(9,'item9'),(8,'item8'),(7,'item7')]`. O(n log k) time, **O(k) memory regardless of stream length**. (A common error: using a max-heap of everything.)

---

## Part B — Full OA Simulations (90 minutes, 4 problems each)

*Time yourself. Read the problem fully, state assumptions (ties, boundaries, empty input) out loud/in comments, write the simple version first, then test edge cases. All solutions below ran with assertions.*

### OA Simulation 1 (MAANG style)

**Q1 [Easy, 15 min] — Hour with the most 5xx errors.**
```python
def worst_hour(logs):                           # logs: (timestamp "YYYY-MM-DD HH:MM:SS", status)
    c = Counter()
    for ts, status in logs:
        if 500 <= status <= 599: c[ts[:13]] += 1
    if not c: return None
    best = max(c.values()); return min(h for h, n in c.items() if n == best), best      # tie -> earliest hour
```
✅ Sample (6xx status ignored, 599 counted): `('2024-03-01 11', 3)`; no 5xx → `None`. O(n). *Pitfalls:* status `600` isn't 5xx; define the tie rule; hour bucketing by string slice vs `datetime.replace(minute=0)`.

**Q2 [Medium, 25 min] — Batch consecutive files under a target size.**
```python
def batch_files(sizes, target):
    batches, cur, tot = [], [], 0
    for s in sizes:
        if s > target: raise ValueError(f"file of size {s} exceeds target {target}")
        if tot + s > target: batches.append(cur); cur, tot = [], 0
        cur.append(s); tot += s
    if cur: batches.append(cur)
    return batches
```
✅ `[4,3,5,2,8,1,1,6], 10 → [[4,3],[5,2],[8,1,1],[6]]` · `[] → []` · `[10,10], 10 → [[10],[10]]` · `[11], 10 → ValueError`. O(n). Greedy is optimal for *consecutive* grouping (order must be kept); contrast with bin packing (Part A) when order is free. *Pitfall:* an oversized file: ask whether it errors or gets its own batch.

**Q3 [Medium, 25 min] — Valid execution order for a DAG, or error on a cycle.** ✅ Kahn's algorithm (adjacency = "after" edges):
```python
execution_order({"extract": ["clean","audit"], "clean": ["load"], "audit": ["load"], "load": []})
# -> ['extract', 'audit', 'clean', 'load']
execution_order({"a": ["b"], "b": ["a"]})        # -> ValueError: circular dependency detected among ['a', 'b']
```
Also `{} → []`, `{"a": []} → ["a"]`. O(V+E). *Pitfalls:* nodes that appear only as dependencies (not as keys); edge direction; disconnected components.

**Q4 [Medium-Hard, 25 min] — Sessionizer (30-minute inactivity).**
```python
def sessionize(events, gap=30 * 60):                 # events: (epoch_seconds, user_id, action), possibly unsorted
    by_user = defaultdict(list)
    for ts, u, _ in events: by_user[u].append(ts)
    out = []
    for u in sorted(by_user):
        ts = sorted(by_user[u]); sid = 1; start = prev = ts[0]; n = 1
        for t in ts[1:]:
            if t - prev > gap: out.append((u, sid, start, prev, n)); sid += 1; start = t; n = 0
            prev = t; n += 1
        out.append((u, sid, start, prev, n))
    return out
```
✅ Output: `('u1',1,3600,5400,3)`, `('u1',2,7301,7301,1)`, `('u2',1,400,500,2)`. Boundary test: events exactly 30:00 apart stay in **one** session; 30:01 starts a new one (`gap > 30 min`). O(n log n). *Pitfalls:* unsorted input (sort per user), single-event sessions (start = end), state the boundary rule. The same logic in PySpark is `lag` + cumulative `sum` (Chapter 8) or `session_window`.

### OA Simulation 2 (FinTech style)

**Q1 [Easy, 15 min] — Merchant with the highest total in the last 7 days.**
```python
def top_merchant_7d(txns, now):                      # txns: (amount, timestamp, merchant)
    tot = defaultdict(float); cutoff = now - 7 * 86400
    for amt, ts, m in txns:
        if cutoff < ts <= now: tot[m] += amt        # window (now-7d, now]
    return max(tot.items(), key=lambda kv: (kv[1], kv[0])) if tot else None
```
✅ Merchant B (50 + 30 + 40 = 120) beats A (100): `('B', 120.0)`; C's 500 is 8 days old; D's 25 is *exactly* 7 days old and is excluded by the `(now−7d, now]` rule. **Always state window boundaries and what "now" is.** For money, use `Decimal`/integer cents, not floats.

**Q2 [Medium, 25 min] — Rate limiter: ≤ 5 requests per 60 s per user.**
```python
class RateLimiter:
    def __init__(self, limit=5, window=60): self.limit, self.window, self.hist = limit, window, defaultdict(deque)
    def allow(self, user_id, ts):
        q = self.hist[user_id]
        while q and q[0] <= ts - self.window: q.popleft()
        if len(q) >= self.limit: return False         # rejected requests are NOT recorded
        q.append(ts); return True
```
✅ requests at `0,10,20,30,40,50,59,60,61,100` → `[T,T,T,T,T,F,F,T,F,T]`: the 6th and 7th are blocked; at 60 the request from t=0 has expired so one more is allowed; 61 is blocked again; by 100 only the t=60 request remains. Per-user isolation verified. Discuss alternatives: fixed window (cheap, allows 2× bursts at boundaries), token bucket (smooth bursts), sliding-window counter (approximate, O(1) memory); distributed version → Redis `INCR`/Lua or sorted sets.

**Q3 [Medium, 25 min] — Max profit from one buy then one sell, per stock.**
```python
def max_profit_per_stock(changes):                   # (timestamp, stock, price)
    st = {}                                          # stock -> [min_price_so_far, best_profit]
    for ts, s, p in sorted(changes):
        if s not in st: st[s] = [p, 0.0]; continue
        st[s][1] = max(st[s][1], p - st[s][0]); st[s][0] = min(st[s][0], p)
    return {s: round(v[1], 2) for s, v in st.items()}
```
✅ AAPL prices by time `180, 170, 190, 175` → **20.0** (buy 170, sell 190); TSLA falls only → **0.0**; MSFT single price → **0.0**. O(n log n) for the sort. *Pitfalls:* input unsorted (sort by time first); sell must come *after* buy; never negative profit (don't trade).

**Q4 [Medium-Hard, 25 min] — Reconcile two sorted transaction lists (match by amount and date ±1 day).**
```python
def reconcile(a, b, tol=1):                          # records (day, amount_cents, id), sorted by day
    used_b = [False] * len(b); matches, un_a = [], []; lo = 0
    for da, amt, ia in a:
        while lo < len(b) and b[lo][0] < da - tol: lo += 1            # two-pointer window start
        j = lo; found = None
        while j < len(b) and b[j][0] <= da + tol:
            if not used_b[j] and b[j][1] == amt: found = j; break
            j += 1
        if found is None: un_a.append(ia)
        else: used_b[found] = True; matches.append((ia, b[found][2]))
    return matches, un_a, [b[j][2] for j in range(len(b)) if not used_b[j]]
```
✅ matched `[(a1,b1),(a2,b2),(a3,b3),(a4,b4)]`, unmatched A `['a5']`, unmatched B `['b5']`; edge: one A against two identical B → matches the first, leaves the other unmatched (one-to-one). Roughly O(n + m) when few candidates per window. *State the policy:* one-to-one greedy by earliest candidate is **not** guaranteed to maximize the number of matches (a/b conflicts need bipartite matching); say so, then offer the upgrade. *Pitfalls:* float amounts (use cents), duplicates, tolerance boundaries, not marking matches as used.

**Self-scoring rubric (per question):** correct on samples (30%) · edge cases handled (30%) · complexity stated and acceptable (20%) · readable code + stated assumptions (20%). **Time hygiene:** 3 min to read/clarify, 10 min to code, 5 min to test edge cases; if stuck at 10 min, write the brute force so you have *something* that passes.

---

## Part C — Complexity Cheat Sheet

### Data structures (Python)
| Structure | Access | Search | Insert | Delete | Notes |
|---|---|---|---|---|---|
| `list` | O(1) index | O(n) | O(1) amortized append; **O(n)** insert/pop at front | O(n) (O(1) pop end) | Contiguous array |
| `dict` / `set` | n/a | **O(1)** avg | O(1) avg | O(1) avg | Hash table; worst O(n) on collisions; insertion-ordered (3.7+) |
| `deque` | O(n) middle | O(n) | **O(1)** both ends | **O(1)** both ends | Use for queues/sliding windows |
| `heapq` (binary heap) | O(1) peek min | O(n) | O(log n) | O(log n) pop min | `heapify` O(n) |
| Sorted list + `bisect` | O(1) | O(log n) | O(n) (shift) | O(n) | Good for read-heavy |
| Balanced BST (not built in) | O(log n) | O(log n) | O(log n) | O(log n) | `sortedcontainers.SortedList` is the practical choice |
| Stack (list) / Queue (deque) | | | O(1) | O(1) | |
| Trie | | O(L) | O(L) | O(L) | L = key length |
| Graph adj. list | | | O(1) add edge | | BFS/DFS O(V+E) |

**Measured on this VM (n = 100,000) ✅ — the ratios are the point:**
```
operation (n=100,000)              µs/op
list  x in lst (last elem)        1058.486     <- O(n) scan
set   x in st                        0.060     <- O(1)          (~17,000x faster)
dict  dc[k]                          0.067
list  lst.append/pop                 0.036
list  lst.pop(0)  (shifts)          39.130     <- O(n)
deque dq.append/popleft              0.045     <- O(1)          (~870x faster than list.pop(0))
heap  push+pop                       0.415     <- O(log n)
bisect on sorted list                0.253
```
> **Takeaways:** `x in list` inside a loop is the classic hidden O(n²); use a `set`. `list.pop(0)` as a queue is O(n); use `deque`.

### Sorting
| Algorithm | Best | Average | Worst | Space | Stable | Use when |
|---|---|---|---|---|---|---|
| Timsort (Python `sorted`, `list.sort`) | O(n) | O(n log n) | O(n log n) | O(n) | Yes | Default; exploits existing runs |
| Merge sort | O(n log n) | O(n log n) | O(n log n) | O(n) | Yes | External sorting, linked lists, guaranteed bound |
| Quicksort | O(n log n) | O(n log n) | O(n²) | O(log n) | No | In-memory general purpose (random pivot) |
| Heap sort | O(n log n) | O(n log n) | O(n log n) | O(1) | No | Memory-tight, worst-case guarantee |
| Counting / radix / bucket | O(n + k) | O(n + k) | O(n + k) | O(n + k) | Yes | Small integer range / fixed-width keys |
| Insertion | O(n) | O(n²) | O(n²) | O(1) | Yes | Tiny or nearly sorted inputs |
Python sort keys: stable, so sort by secondary key first or use a tuple key.

### Common recurrences & patterns
| Recurrence / pattern | Result | Example |
|---|---|---|
| T(n) = T(n/2) + O(1) | O(log n) | Binary search |
| T(n) = T(n−1) + O(1) | O(n) | Linear recursion |
| T(n) = 2T(n/2) + O(n) | O(n log n) | Merge sort |
| T(n) = 2T(n/2) + O(1) | O(n) | Tree traversal |
| T(n) = T(n−1) + T(n−2) | O(2ⁿ) (O(n) with memoization) | Naive Fibonacci |
| Two nested loops over n | O(n²) | Brute-force pairs |
| Sort then linear scan | O(n log n) | Merge intervals |
| Heap of size k over n items | O(n log k) | Top-K |
| k-way merge of N total | O(N log k) | Merge sorted files |
| Sliding window / two pointers | O(n) | Each pointer moves once |
| Hash-map single pass | O(n) time, O(n) space | Two sum |
| BFS/DFS | O(V + E) | Topological sort, islands |

### DE rules of thumb for OAs
- Say the **time and space complexity** unprompted, and the **trade-off** you chose (time vs memory).
- "Sorted" in the prompt → think two pointers / binary search / merge. "Contiguous" → sliding window / prefix sums. "Dependencies" → topological sort. "Top K / merge K" → heap. "Count/group/duplicate" → hash map. "Intervals" → sort by start.
- Ask: input size? duplicates? negatives? empty input? tie-breaking? timestamp boundaries (inclusive/exclusive)? unsorted input? Then **state your assumptions**.
- Test with: empty, single element, all equal, already sorted, reverse sorted, duplicates, boundary values, and a hand-traced sample.

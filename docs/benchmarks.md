# Benchmarks

**Everything here was measured on one development laptop. These are not production numbers.** Each section
gives the exact command, so every number can be reproduced.

| | |
|---|---|
| Date | 2026-10-30 (UTC) |
| Machine | AMD Ryzen 5 5500U (6 cores / 12 threads), 7.3 GB RAM (0.7–1.1 GB free during runs), Windows 11 Home |
| Versions | Python 3.13.3, Django 5.2.18, PostgreSQL 16.14 (native Windows service, default config: 128 MB shared_buffers) |
| Code | The status-page baseline ran at `f64d2bd`. Everything else ran on the working tree that became commit `4c3cbb2` (same models, services and scripts). |
| Dataset | `manage.py seed_bulk --seed 1`: 20 orgs × 20 monitors, 30 days. Took 328 s to generate. |

**Dataset details:**

- **Size:** 6,175,120 check results (6,179,363 rows in the table including earlier dev data), 944 MB on
  disk including indexes, plus 528 incidents.
- **Intervals:** 60 s (25% of monitors), 300 s (50%), 900 s (25%).
- **Outages:** 0–4 per monitor, each lasting 5–180 min. About 0.5% of other checks fail at random.
- **Benchmarked org:** `bulk1-01`, 20 monitors with 345,620 checks in the last 30 days.

**What "in-process" means:** requests go through Django's test client, which runs the full middleware,
URL routing, DRF and serialization against the real Postgres, but not over a TCP socket. The worker
benchmarks use **real processes and real HTTP** on 127.0.0.1.

---

## C2. Public status page: uncached vs cached

```powershell
.\.venv\Scripts\python.exe scripts\bench_status.py --slug bulk1-01      # 200 timed runs after 20 warmup
.\.venv\Scripts\python.exe scripts\explain_uptime.py --slug bulk1-01     # EXPLAIN (ANALYZE, BUFFERS), rolled back
```

### Before the index change (baseline, `f64d2bd`)

| mode | p50 ms | p95 ms | max ms | queries |
|---|---|---|---|---|
| uncached (cache off) | 243.9 | 253.0 | 283.7 | 10 |
| cache miss (build + store) | 247.3 | 252.8 | 259.0 | 17 |
| cache hit | 9.3 | 10.8 | 13.8 | 7 |

The repeat run gave uncached 243.9 / 252.1 ms and a cache hit of 9.6 / 12.7 ms (p50 / p95).

### Where the time goes

From `EXPLAIN (ANALYZE, BUFFERS)` on the uptime aggregate (one query for all 20 monitors and all three
windows):

```
GroupAggregate (actual time=32.587..243.168 rows=20 loops=1)
  Buffers: shared hit=3201
  ->  Index Scan using monitors_checkresult_monitor_id_52f569aa (actual time=0.031..96.707 rows=345550 loops=1)
Execution Time: 243.228 ms
```

- **No disk I/O:** all 3,201 buffers were cache hits. About 97 ms goes to scanning 345,550 rows and
  about 146 ms to aggregating them.
- **The planner picks the FK index:** it uses Django's automatic index on `monitor_id`, which is
  redundant with the composite `(monitor, -checked_at)` index.

### Index experiments (all before/after numbers recorded)

| Experiment | Result | Kept? |
|---|---|---|
| Covering index `(monitor_id, checked_at) INCLUDE (ok)` in a rolled-back transaction | The planner didn't use it; 241 ms | no |
| Same index built for real (239 MB, 23.7 s to build) plus `VACUUM ANALYZE` so index-only scans are possible | Still not chosen; 240–254 ms. Dropped. | no |
| Drop the redundant FK index on `monitor_id` (rolled back, 3 runs) | The composite index with a parallel bitmap scan (2 workers): 155–164 ms warm. The first cold run was 607 ms while the composite index was read into cache. | **yes** (migration `monitors/0002`) |

After dropping the FK index for real (`4c3cbb2`), two runs:

| mode | p50 ms | p95 ms | max ms | queries |
|---|---|---|---|---|
| uncached (cache off) | 172.3 | 190.9 | 238.1 | 10 |
| cache miss (build + store) | 177.6 | 199.6 | 227.9 | 17 |
| cache hit | 10.0 | 11.2 | 14.7 | 7 |
| *repeat:* uncached | 172.0 | 189.4 | 224.2 | 10 |
| *repeat:* cache hit | 10.1 | 11.7 | 16.1 | 7 |

### Conclusions

- **The cache stays.** A hit is about 10 ms against 172 ms uncached, roughly 17× faster at p50. A miss
  costs about the same as uncached, plus storing the payload.
- **The uncached path aggregates every check in the 30-day window.** About 345k rows for this org, so
  the cost grows linearly with check frequency. An org whose 20 monitors all run every 60 s would
  aggregate 864k rows.
  - The next step, **not built** because it isn't needed at this size, would be daily rollups
    `(monitor, day, checks, successes)`, so 30-day uptime reads at most about 31 rows per monitor.
  - Retention (Part D) keeps 30 days of raw rows, which covers the longest window, so rollups are not
    needed for correctness.
- **The FK-index win is mostly parallelism.** It came from a plan that uses 2 parallel workers, not from
  less work. On a 1-vCPU database the gain would be smaller. Dropping the index is justified anyway:
  every lookup by monitor uses the composite index, and every check insert now updates one index fewer.
- **Query counts** are higher than the 4 pinned in tests because DRF's throttle uses the DB cache. Every
  request costs about 6 statements for a throttle get plus set. A cache hit is 1 SELECT on the cache
  table plus the throttle's statements.

---

## C3. Worker throughput

```powershell
.\.venv\Scripts\python.exe scripts\bench_worker.py --checks 1000 --noops 3000                 # threads 1,4,8
.\.venv\Scripts\python.exe scripts\bench_worker.py --checks 1000 --noops 3000 --threads 4 --processes 2
.\.venv\Scripts\python.exe scripts\bench_worker.py --checks 1000 --noops 3000 --threads 1,4 --pgoptions "-c synchronous_commit=off"   # diagnostic only
```

### Method

- **Worker:** the real `manage.py run_worker --no-scheduler` process.
- **Burst:** N jobs are inserted at once, as if every monitor were due at the same moment. The script
  waits until all have finished.
- **Throughput:** N ÷ (last `finished_at` − first `created_at`).
- **Latency:** enqueue → finished per job. Because it's a burst, this mostly measures **queue wait**, not
  service time.
- **Target:** a Python `ThreadingHTTPServer` on 127.0.0.1 returning instant 200s.
- **CPU:** CPU seconds of the worker process tree, all `postgres.exe` processes, and the bench process
  (which hosts the fake target) are sampled before and after. 100% means one full core.
- **Queries per job** (claim + handler + finish, counted in-process): **run_check = 13, noop = 5.**

### Run 1

| job | threads | jobs | jobs/s | per minute | latency p50 ms | latency p95 ms | worker CPU % | postgres CPU % |
|---|---|---|---|---|---|---|---|---|
| run_check | 1 | 1000 | 25 | 1,482 | 21,280 | 38,603 | 46 | 22 |
| run_check | 4 | 1000 | 57 | 3,439 | 9,297 | 16,566 | 131 | 48 |
| run_check | 8 | 1000 | 55 | 3,325 | 9,760 | 17,199 | 168 | 48 |
| noop | 1 | 3000 | 86 | 5,176 | 20,001 | 33,334 | 54 | 45 |
| noop | 4 | 3000 | 148 | 8,871 | 10,819 | 19,276 | 106 | 76 |
| noop | 8 | 3000 | 150 | 8,991 | 10,142 | 18,933 | 118 | 82 |

### Repeat run

| job | threads | jobs/s | per minute | worker CPU % | postgres CPU % | bench + fake target CPU % |
|---|---|---|---|---|---|---|
| run_check | 1 | 25 | 1,525 | 50 | 19 | 9 |
| run_check | 4 | 55 | 3,270 | 129 | 43 | 14 |
| run_check | 8 | 51 | 3,086 | 165 | 42 | 15 |
| noop | 1 | 81 | 4,881 | 53 | 47 | 10 |
| noop | 4 | 150 | 8,971 | 104 | 72 | 11 |
| noop | 8 | 148 | 8,899 | 114 | 81 | 11 |

A third run at 4 threads gave run_check 57/s and noop 152/s.

### Finding the limit

Each candidate was tested rather than assumed.

| Hypothesis | Test | Result |
|---|---|---|
| Commit latency: 2 commits per job, and Windows WAL flushes are slow | Worker sessions with `synchronous_commit=off` (PGOPTIONS, diagnostic only) | run_check 4 threads 55 → 59/s, noop 150 → 154/s. **A negligible gain, so not the limit.** |
| The fake target server (Python, same GIL as the bench process) | Its CPU was sampled | 9–15% of one core. **Not the limit.** |
| Postgres | Its CPU was sampled | 42–82%, spread over several backend processes. **Not saturated.** |
| CPU in the worker process (Python + GIL) | Two worker processes × 4 threads | run_check **101/s (6,045/min)**, noop **273/s (16,385/min)**, about **1.8×** one process. Worker CPU 302% (run_check), 203% (noop). Postgres 79% / 151%. |

- **Conclusion:** one worker process tops out at about 55 checks/s (about 3,300 checks/min) at 4
  threads, and 8 threads add nothing.
- **Cost per job:** each check costs about 20 ms of worker CPU (0.50 core ÷ 25 jobs/s at 1 thread). That
  covers a new httpx client and connection pool per check, the TLS-free loopback request, and 13 ORM
  queries. Threads can't run Python bytecode in parallel, so throughput stops rising once Python work
  fills about one core. The CPU figures above 100% are time spent outside the GIL: psycopg, sockets and
  the kernel.
- **Scaling:** **more worker processes** scale almost linearly. The SKIP LOCKED queue makes that safe
  (tested), and Postgres still had headroom at 2 processes.

### What 3,300 checks/min means

At the default 60 s interval, one worker process covers about **3,300 monitors** (about 13,000 at 5-minute
intervals) before checks start running late. That's two orders of magnitude above the 20-monitor-per-org
limit for a handful of orgs. This is a laptop measurement with a fake local target. A real target with
100–500 ms latency adds wall time per check but no CPU, so more threads would then help until the CPU
limit above is reached.

### Dispatcher

The dispatcher claims and enqueues (one SKIP LOCKED transaction per batch of 100, with an INSERT and
savepoint per job) **1,000 due monitors in 2.15–2.46 s, about 400–460/s**, measured twice.

- **This found a real bottleneck:** the scheduler dispatched only **one** batch of 100 per 10 s tick. That
  capped the whole system at **600 checks/min**, below a single worker's capacity.
- **Fix:** the tick now keeps dispatching while batches come back full, up to `DISPATCH_MAX_BATCHES=10`.
  That allows 6,000/min, and one tick takes about 2.5 s at most. Tested in
  `test_scheduler_tick_dispatches_full_batches_until_none_are_due`.

### Not measured

- Throughput against slow real targets.
- Long-running steady-state latency at a constant arrival rate. Only bursts were measured.
- Throughput on the deploy host (Phase 4).

---

## C4. Alert delivery under injected failures

```powershell
.\.venv\Scripts\python.exe scripts\bench_alerts.py --incidents 200 --rates 0.3,0.6 --seed 7
.\.venv\Scripts\python.exe scripts\bench_alerts.py --incidents 200 --rates 0.3,0.6 --seed 11   # repeat
```

### Method

- **Load:** 200 incidents × 2 channels = **400 deliveries per run**, queued through the real
  `queue_alerts`.
- **Worker:** the real `Worker`, run **in-process** with 8 threads and no scheduler. It POSTs through
  `safe_http` to a fake receiver on 127.0.0.1.
- **Failure injection:** the receiver fails `rate` of requests, split evenly between:
  - HTTP 500;
  - a **timeout**: it sleeps 0.8 s, longer than the client's 0.5 s budget, then answers 500;
  - HTTP 429 with `Retry-After: 1`.
- **Backoff was shortened for the run via settings:** base 0.2 s, cap 2 s, webhook budget 0.5 s, poll
  0.05 s. Max attempts stays at the production value of 5.
- **Duplicates** are counted **at the receiver**, as messages accepted (2xx) more than once per channel.
- **Not triggered:** the receiver never accepts a request the client timed out on. So the documented
  crash-between-send-and-commit duplicate (D14) cannot occur here, as the brief asked.

### Results

| failure rate | run | delivered | model 1 − p⁵ | failed (5 attempts) | duplicates at receiver | attempts needed by delivered alerts |
|---|---|---|---|---|---|---|
| 30% | seed 7 | 399 / 400 (99.75%) | 99.76% | 1 | **0** | 1: 264, 2: 79, 3: 43, 4: 8, 5: 5 |
| 30% | seed 11 | 400 / 400 (100.00%) | 99.76% | 0 | **0** | 1: 271, 2: 96, 3: 23, 4: 7, 5: 3 |
| 60% | seed 7 | 369 / 400 (92.25%) | 92.22% | 31 | **0** | 1: 155, 2: 93, 3: 63, 4: 42, 5: 16 |
| 60% | seed 11 | 367 / 400 (91.75%) | 92.22% | 33 | **0** | 1: 145, 2: 105, 3: 58, 4: 43, 5: 16 |

- **Receiver tally at 60%, seed 7:** 369 accepted, 182 × 500, 197 × 429, 185 timeouts.
- **Every undelivered alert** ended `failed` with 5 attempts, and its job `dead`. None were left pending.
- **Runtime:** each run took 12–19 s.
- **Reading the results:** delivery matches what independent retries predict. When each attempt fails
  with probability p, 5 attempts deliver 1 − p⁵. There are zero duplicates at both failure rates in both
  runs.
- **Honest caveat:** p = 60% with 5 attempts still loses about 8% of alerts. More attempts or longer
  backoff would help against a long webhook outage. The production backoff (10 s base, up to 15 min) is
  much longer than this test's.

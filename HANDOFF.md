# HANDOFF: StatusPing Checkpoint 2 (Phases 3-4)

- **Status: done.** Parts A–F are complete, pushed and green in CI. **Deployed and verified live:**
  https://statusping-3xwa.onrender.com
  - **Hosting:** option (c), one free Render web service that runs the API plus the worker and
    scheduler threads (`EMBEDDED_WORKER=true`), on Supabase Free through the session pooler. It costs
    $0/month.
  - **No keep-alive pinger,** so **monitoring only runs while the instance is awake** (D30, D31). The
    24/7 upgrade is the Render Starter worker (about $7/month), as a config change only.
  - **Requirement change:** `test_render_blueprint_shape` now asserts the single-service option (c)
    Blueprint instead of web + worker (D31).
  - Checkpoint 1's handoff is in git history (`git show 8fcb570:HANDOFF.md`).
- **Versions:** Python 3.13.3, Django 5.2.18, DRF 3.18.3, httpx 0.28.1 / httpcore 1.0.9, PostgreSQL 16.14
  locally and 16 in CI.
- **Machine:** Ryzen 5 5500U, 7.3 GB RAM, Windows 11. Free RAM was 0.68 GB at the start and 0.7–1.1 GB
  during runs. Full suites were never run in parallel.

## What was built

- **A. Total time budget per request** (`apps/core/safe_http.py`). httpcore's network backend is wrapped
  so that DNS, connect, TLS, write and every read share **one** monotonic deadline. A drip-fed response
  ends at the budget and is recorded as `error="timeout"`. A system check (`apps/core/checks.py`) refuses
  to start if `JOB_LEASE_SECONDS < 3 × max(10 s check, ALERT_TIMEOUT_SECONDS)`. Decision D24.
- **B. Public status page.**
  - `GET /status/{slug}/` returns JSON and `GET /status/{slug}` returns HTML (no JS, refreshes every
    60 s, accessible). Both share one service.
  - Uptime for 24h/7d/30d comes from **one** SQL aggregate, and is null when a window has no checks.
  - The page shows no URLs and no emails. Owners can opt out with `status_page_public`, which returns 404.
  - The payload is cached 30 s in the DB cache and invalidated on commit. Cache failures degrade to
    uncached.
  - It is throttled per IP and costs 4 queries on a miss (pinned). Decision D25.
- **C. Benchmarks** in `docs/benchmarks.md`:
  - `seed_bulk` (6.18M check results);
  - the status page uncached vs cached, with EXPLAIN-driven index work;
  - worker throughput at 1/4/8 threads and 2 processes, the raw queue, and the dispatcher;
  - alert delivery under 30% and 60% injected failures.

  Two real fixes came out of the measurements: the redundant CheckResult FK index was dropped, and the
  dispatcher's 600/min cap was raised to 6,000/min.
- **D. Hardening:**
  - CheckResult retention: 30 days, per monitor, hourly and bounded. No rollups are needed (D26).
  - `/api/health/` returns **503** when the worker heartbeat is older than 60 s, and the new
    `/api/health/live/` is the web process's own check (D27).
  - One structured log line per job, with webhook URLs masked at the source and by a handler filter (D28).
- **E. Deploy prep:**
  - `docs/deploy.md`: three options with researched prices and limits.
  - `gunicorn.conf.py`, including an `EMBEDDED_WORKER` switch for option (c).
  - `bin/render-build.sh`, a draft `render.yaml`, and `deploy/` systemd units, a Caddyfile and an env
    example.
- **F. Deploy and docs:**
  - The live deployment (above).
  - `scripts/verify_live.py`: a reproducible live check.
  - The README: live links, a Mermaid diagram, why the queue is Postgres, a results table linked to the
    evidence, and known limits.
  - `docs/resume-bullets.md`: 3 bullets, every number linked.

## Key numbers (all in docs/benchmarks.md, with commands, machine and repeat runs)

| What | Result |
|---|---|
| Status page, 20 monitors, 345k checks in 30 days (in-process) | uncached p50 **172 ms** (was 244 ms before dropping the redundant index); cache hit p50 **~10 ms** |
| One worker process, run_check against a local fake target | **~55 checks/s, about 3,300/min** at 4 threads; 8 threads add nothing |
| Raw queue (no-op jobs) | **~150 jobs/s** for one process; **273 jobs/s** with 2 processes |
| Bottleneck | worker CPU/GIL per process (2 processes give 1.8×). Commit latency and Postgres ruled out by experiment |
| Alerts at 30% / 60% injected failures (400 deliveries each, 2 seeds) | **99.75–100% / 91.75–92.25%** delivered (model 1 − p⁵: 99.76% / 92.22%); **0 duplicates** at the receiver |

## Decisions beyond the prompt

D24–D31 in docs/decisions.md. The main ones:

- **Hosting went through three choices, all at Humayun's direction, with the research kept:**
  - Neon Launch, about $27/month (D29). Neon Free can't run an always-on worker for a full month.
  - Then Supabase Free plus a $7 worker (D30).
  - Then the single free service with no pinger (D31). FixFlow shares the workspace's 750 free hours, and
    Render's AUP is a risk for pingers.
- **`WORKER_POLL_SECONDS=3` in production.** At a 1 s poll, idle polling alone measured 2.56 GB/month
  against Supabase Free's 5 GB egress (D31).
- **The redundant `CheckResult.monitor` FK index was dropped,** with before/after evidence. The gain is
  mostly a parallel plan (stated).
- **Retention purges per monitor** through the existing index instead of adding a `checked_at` index
  (measured).
- **Health split:** `/api/health/` (whole service, 503 for a dead worker) and `/api/health/live/` (the web
  platform's check), so a dead worker never makes the platform restart a healthy web process.
- **Cache failures never fail a write.** This was found by the worker-process test.
- **The `/status/{slug}` HTML is a DRF view** (TemplateHTMLRenderer), so it shares the JSON's throttle.

## Verification (real output, trimmed)

```
pytest (local, 931f14b tree): 461 passed in 80.49s, Total coverage: 97.93%  (free RAM 0.93 GB before)
CI (931f14b):  461 passed in 36.99s, Total coverage: 97.83%   run 41108707709, success
ruff check: All checks passed! | ruff format --check: 118 files already formatted
makemigrations --check: No changes detected
check --deploy (prod, --fail-level WARNING): System check identified no issues (0 silenced).
spectacular --validate --fail-on-warn: exit 0
```

### Live verification (`scripts/verify_live.py https://statusping-3xwa.onrender.com`, 2026-10-30 UTC)

```
[18:31:43] /api/health/live/ 200 in 0.4s: {'status': 'ok', 'database': 'ok'}
[18:31:43] /api/health/ 200: {'status': 'ok', 'database': 'ok', 'worker': {'status': 'ok', 'last_seen_seconds_ago': 5}}
[18:31:44] /api/docs/ 200 (4647 bytes)
[18:31:46] /api/schema/ 200 (49440 bytes)
[18:31:46] /status/demo/ 200
[18:31:46]   overall=degraded; Example homepage: up, 24h=None; httpbin 200 (HEAD): up, 24h=None; httpbin always 500: down, 24h=None
[18:31:47] /status/demo (HTML) 200, text/html; charset=utf-8
[18:31:53] registered live-verify-20261030183147@example.com, org live-verify-20261030183147
[18:31:54] monitors 4 (example.com) and 5 (httpbin 500); channel 1
[18:33:11] incident 2 OPEN, cause=unexpected_status, after 2 failed checks: 18:32:08 500 159ms, 18:33:09 500 159ms
[18:33:11] delivery opened: status=failed attempts=1 last_error='HTTP 404 (not retried)'
[18:33:11] pointed the failing monitor at /status/200
[18:35:14] incident 2 RESOLVED at 2026-10-30T18:35:07.954098Z
[18:35:14] delivery resolved: status=failed attempts=1
[18:35:14] example.com: 4 checks, all ok=True, gaps between checks (s): [60, 58, 61], response_ms: [33, 31, 41, 76]
[18:35:15] cleaned up: monitors and channel deleted
[18:35:44] 11 failed logins with forged X-Forwarded-For: [401, 401, 401, 401, 401, 401, 401, 401, 401, 401, 429]
[18:35:44] PASSED
```

What this shows:

- **The embedded worker runs under gunicorn on Render.** The heartbeat is fresh, checks run about every
  60 s (gaps 58–61 s), and the httpbin monitor opened an incident after exactly 2 failed checks (threshold
  2). Pointing it at `/status/200` resolved the incident after 2 successes.
- **Alerts go through the delivery log** (no real webhook, per Humayun). The channel was a well-formed but
  invalid Discord webhook. Discord answered 404, and the delivery was recorded `failed`, 1 attempt,
  "not retried", so a 4xx isn't retried (D15). Both the opened and resolved events were attempted
  exactly once.
- **The proxy depth isn't spoofable:** 11 failed logins, each with a different forged `X-Forwarded-For`,
  were throttled at the 11th. `NUM_PROXIES=3` therefore doesn't pick the forged entry. The other
  direction (that two real networks aren't throttled together) needs a second network and wasn't tested.
- **The `24h=None` on the first lines was a real bug:** see below. After the fix was deployed, the live
  JSON showed `Example homepage 24h=100.0`, `httpbin 200 (HEAD) 24h=100.0`, and
  `httpbin always 500 24h=0.0`.
- **Not checked:** the Render log lines (the structured per-job line and secret masking). Render logs need
  Humayun's dashboard. Both are covered by tests and the local e2e run.

### Sleep and wake, observed live (no requests from 18:43:09 to 19:00:09 UTC)

```
idle from 18:43:09
wake request at 19:00:09
health/live: 200 in 33.672448s
{"status":"ok","database":"ok","worker":{"status":"ok","last_seen_seconds_ago":2}}
generated 2026-10-30T19:00:48.154571Z
Example homepage 2026-10-30T19:00:39.064199Z
httpbin 200 (HEAD) 2026-10-30T19:00:39.068862Z
httpbin always 500 2026-10-30T19:00:39.849074Z
```

- **The instance did sleep.** After 17 idle minutes the first request took **33.7 s**, against 0.4 s when
  awake. Render's own health checks didn't keep it up.
- **The worker came back with it:** the heartbeat was 2 s old, and all three demo monitors were checked
  again **30 s after the wake request**.
- **Not shown directly:** that no checks ran while it slept. The check list needs a demo login, and the
  verification org's monitors were already deleted. The cold start is strong evidence that the process
  wasn't running.

### Mutation check (`scripts/mutation_check.py`, 8 mutations, all caught, tree restored)

M1/M1b (job claim lock), M2/M2b (dispatch lock), **M5 (total deadline removed → 4 deadline tests fail)**,
M3 (locked_by guard), M4a (delivery row lock), M4b (delivery uniqueness).

### Live e2e after Parts A–D (`scripts/e2e.py`, real processes)

```
health: {'status': 'ok', 'database': 'ok', 'worker': {'status': 'ok', 'last_seen_seconds_ago': 4}}
incident 1337 OPEN after 118s, 2 failed checks (threshold 2), cause=unexpected_status
webhook received exactly 1 message: '[DOWN] Fake target is down ...'
incident RESOLVED after 107s
webhook received exactly 1 more message: '[UP] Fake target is back up ...'
deliveries: opened=sent (attempts 1), resolved=sent (attempts 1)
cleaned up: monitor and alert channel deleted
worker exited with code 0 | peak working set: web 78 MB, worker 78 MB | PASSED
worker log: ... job id=450665 kind=deliver_alert attempt=1/5 outcome=succeeded duration_ms=33
webhook path occurrences in web + worker logs: 0
```

## Bugs found, test changes, surprises

- **Real bug (found by live verification):** `GET /status/{slug}/` returned `null` for every uptime
  window.
  - **Cause:** `UptimeSerializer` renamed its `_24h` fields to `24h` after DRF had bound them, so they kept
    `source="_24h"`. `allow_null` then turned the missing key into null.
  - **Why the tests missed it:** they asserted uptime on `build_payload` and on the HTML page (which
    renders the payload directly), never on the JSON response.
  - **Fix (`5cedd5a`):** the fields are built in `get_fields()`. A JSON regression test failed before the
    fix and passes after.
- **Real bug (found by the worker-process test):** the status-page cache invalidation ran in `on_commit`.
  With the cache table missing, it raised after a committed check and made the job fail. Cache operations
  are now best-effort, and a test covers it.
- **Requirement changes, not weakened tests:**
  - The health tests went from "200 degraded" to 503 (D27).
  - `test_default_rates_and_shared_cache` gained the new `status_page` scope.
  - The tick-summary test gained `purged_checks`.
- **Test mistakes fixed:**
  - The fake DNS didn't know the IP literal in one deadline test.
  - Two helper filters in `test_logging.py` matched too broadly.
- **Unexplained, one-off hang:** 1 of 7 local full runs hung. It was CPU-bound with an idle-in-transaction
  test DB session, and no lock wait. It has not reproduced in 6 runs since, including the identical
  command. `faulthandler_timeout=120` is configured, so a recurrence will dump stacks. **Not resolved.**
- **seed_bulk writes rows monitor by monitor,** so the heap isn't in time order. BRIN could therefore not
  be evaluated fairly (D26).

## Known gaps and risks

- **Monitoring only runs while the instance is awake.** It sleeps 15 minutes after the last request, and
  missed intervals aren't replayed. This is stated in the README, deploy.md and render.yaml. The fix is the
  Starter worker (about $7/month; confirm the price on render.com/pricing first).
- **Supabase Free may pause the database** after about a week with no activity, which is likely if nobody
  visits. It emails a warning first, and the database is restored from the dashboard.
- **Supabase limits:** 500 MB (about 40–45 monitors at 60 s with 30 days of history) and 5 GB/month
  egress. Check the usage page after a few active days.
- **The status page's uncached cost** grows linearly with checks in the 30-day window. Rollups are the
  documented next step, not built.
- **`NUM_PROXIES=3`** is verified against forged headers only. A second network is needed to rule out
  "too low" (everyone sharing one throttle bucket).
- **The demo password** isn't published (Humayun's choice). The README says it's available on request.
- **The CI annotations** (Node 20 actions deprecated, ubuntu-latest moving to Ubuntu 26) are warnings
  only. Bumping the action versions is a small follow-up.
- **The one-off test-suite hang** (above) is unresolved.

## Repo

- https://github.com/MhBaigtw/statusping, `main`. CI is green on `931f14b` (run 41108707709). The final
  handoff commit follows it.
- Live: https://statusping-3xwa.onrender.com (docs at `/api/docs/`, demo status page at `/status/demo`).

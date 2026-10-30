# HANDOFF: StatusPing Checkpoint 2 (Phases 3-4)

- **Status: partly done.**
  - **Done and pushed, CI green:** Part A (review fix), B (status page and cache), C (measurements), D
    (retention and hardening) and E (deploy prep).
  - **Waiting:** Part F (deploy) needs Humayun's Render and Neon accounts (see "Actions for Humayun").
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

## Key numbers (all in docs/benchmarks.md, with commands, machine and repeat runs)

| What | Result |
|---|---|
| Status page, 20 monitors, 345k checks in 30 days (in-process) | uncached p50 **172 ms** (was 244 ms before dropping the redundant index); cache hit p50 **~10 ms** |
| One worker process, run_check against a local fake target | **~55 checks/s, about 3,300/min** at 4 threads; 8 threads add nothing |
| Raw queue (no-op jobs) | **~150 jobs/s** for one process; **273 jobs/s** with 2 processes |
| Bottleneck | worker CPU/GIL per process (2 processes give 1.8×). Commit latency and Postgres ruled out by experiment |
| Alerts at 30% / 60% injected failures (400 deliveries each, 2 seeds) | **99.75–100% / 91.75–92.25%** delivered (model 1 − p⁵: 99.76% / 92.22%); **0 duplicates** at the receiver |

## Decisions beyond the prompt

D24–D29 in docs/decisions.md. The main ones:

- **Neon Free can't host the always-on worker's DB** for a full month (182.5 of 100 CU-hours). Humayun
  chose option (a) with Neon Launch, about $27/month (D29).
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
pytest (local):  452 passed, Total coverage: 97.93%   (3 extra soak runs: 452 passed each, ~65 s)
CI (6ae2bc1):    460 passed in 32.37s, Total coverage: 97.83%   run 40985948174, all steps green
ruff check: All checks passed! | ruff format --check: 113 files already formatted
makemigrations --check: No changes detected
check --deploy (prod, --fail-level WARNING): System check identified no issues (0 silenced).
spectacular --validate --fail-on-warn: exit 0
```

The extra 8 tests in CI are `tests/test_deploy.py`, added after the local full run and verified on their
own locally (13 passed with test_settings).

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

- **The Render worker price ($7)** comes from several 2026 third-party sources. Render's pricing page is
  JS-rendered and couldn't be read, so **confirm it before paying**.
- **The status page's uncached cost** grows linearly with checks in the 30-day window. Rollups are the
  documented next step, not built.
- **Free Render web sleeps,** so the first API or status page request after 15 idle minutes takes about a
  minute. Monitoring isn't affected (separate worker).
- **`NUM_PROXIES=3`** is FixFlow's measured Render value. It must be re-measured on this service.
- **The CI annotations** (Node 20 actions deprecated, ubuntu-latest moving to Ubuntu 26) are warnings
  only. Bumping the action versions is a small follow-up.

## Actions for Humayun (needed before Part F)

I can't create accounts or enter billing details, so these steps are yours:

1. **Neon:** create a new project (Launch plan) in **AWS us-west-2 (Oregon)**, then:
   - In the compute settings, **disable scale-to-zero**.
   - Copy the **direct** connection string (not the `-pooler` one).
   - Don't paste it to me.
2. **Render:** make sure billing is set up (the Starter worker is paid), and **confirm the Starter worker
   price** on render.com/pricing. Then New → **Blueprint** → repo `MhBaigtw/statusping` (`render.yaml`
   creates `statusping` and `statusping-worker`).
3. **In Render, for both services:** set `DATABASE_URL` to the Neon string.
4. **Web service only:** set `DEMO_PASSWORD` to a **new demo-only password**. It will be published in the
   README, so don't reuse anything.
5. **Reply "continue"** with the `*.onrender.com` URL. I will then:
   - verify live: health, docs, the status page, scheduled checks, an httpbin 500 incident and its
     delivery log, and proxy depth with forged `X-Forwarded-For`;
   - write the README and `docs/resume-bullets.md`;
   - run the final verification, ask before pushing, and finalize this handoff.

## Repo

- https://github.com/MhBaigtw/statusping. Pushed: `6ae2bc1` (CI green).
- Local commits after that (docs only: tasks, deploy decision, this handoff) will be pushed with Part F.

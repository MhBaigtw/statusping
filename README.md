# StatusPing

A multi-tenant uptime monitoring API. Organizations register HTTP monitors. A scheduler enqueues due
checks, and workers run them with strict timeouts. An incident opens after N consecutive failures and
resolves after M consecutive successes, so flapping sites don't page anyone. Alerts go to Discord or Slack
webhooks, with retries and no duplicates.

**Status: all four phases complete** ([SPEC.md](SPEC.md)): the API, the job queue, checks, incidents,
alerts, a public status page with caching, measured benchmarks, and a live deployment.

## Live demo

The service runs on Render's free tier, so **the first request after 15 idle minutes takes about a minute**
while it wakes up. Monitoring only runs while it is awake (see [Known limits](#known-limits-of-the-free-deployment)).

| | |
|---|---|
| API docs (Swagger) | https://statusping-3xwa.onrender.com/api/docs/ |
| Public status page (HTML) | https://statusping-3xwa.onrender.com/status/demo |
| Status page (JSON) | https://statusping-3xwa.onrender.com/status/demo/ |
| Health (web + worker heartbeat) | https://statusping-3xwa.onrender.com/api/health/ |

**Demo logins:** `owner@statusping.example` (owner) and `viewer@statusping.example` (read-only). The
password isn't published, but it's available on request. Without it, register your own account with
`POST /api/auth/register/` and create an org. Then get a token with `POST /api/auth/token/` and use
**Authorize** in the docs.

The public demo status page needs no login. Its org has three monitors, one of which
(`httpbin.org/status/500`) is always down, so there is always an open incident to look at.

## How it fits together

```mermaid
flowchart LR
    client([API clients / browsers]) -->|HTTPS| web
    subgraph render[Render free web service: one gunicorn process]
        web[Django + DRF<br/>API, docs, status page]
        sched[scheduler thread<br/>dispatch due checks, reap leases, retention]
        workers[job threads<br/>run_check, deliver_alert]
    end
    subgraph pg[(PostgreSQL: Supabase, session pooler)]
        data[orgs, monitors, checks,<br/>incidents, alert deliveries]
        queue[jobs table:<br/>the queue]
        cache[DB cache:<br/>throttles, status page]
    end
    web --> data
    web --> cache
    sched -->|"enqueue (same transaction)"| queue
    workers -->|"claim: FOR UPDATE SKIP LOCKED"| queue
    workers --> data
    workers -->|"safe_http: SSRF-checked, IP-pinned, one time budget"| targets([monitored URLs])
    workers -->|webhook POST, retried| hooks([Discord / Slack])
```

In development the same code runs as two processes: `runserver` and `run_worker`. With the paid upgrade
it runs as a web service plus a separate background worker. The queue's guarantees don't depend on how
many processes run it.

## Why the queue is PostgreSQL, not Celery + Redis

The full reasoning is in [docs/decisions.md](docs/decisions.md) D1. In short:

- **Transactional enqueue.** A job is a row, so an incident, its alert rows and their delivery jobs
  commit together or not at all. With a separate broker, a crash between "commit" and "publish" loses or
  duplicates work.
- **One less system to run.** Postgres is already required. Redis would add a service, a failure mode and
  a bill.
- **The scale fits.** One worker process runs about 3,300 checks a minute (measured below), and Postgres
  wasn't the bottleneck at 2 processes.
- **The mechanics are the interesting part:** SKIP LOCKED claims, leases, a reaper, backoff, dedupe keys
  and idempotent handlers. All of them are tested against a real Postgres with real threads.

## Measured results

All on one laptop (Ryzen 5 5500U, Windows 11, local Postgres 16), with the exact commands in
[docs/benchmarks.md](docs/benchmarks.md). These are not production numbers.

| What | Result | Evidence |
|---|---|---|
| Public status page, 20 monitors, 345k checks in 30 days | uncached p50 **172 ms** (244 ms before dropping a redundant index); cache hit p50 **~10 ms** | [C2](docs/benchmarks.md#c2-public-status-page-uncached-vs-cached) |
| One worker process, 4 threads | **~55 checks/s (~3,300/min)**; 8 threads add nothing; 2 processes give **1.8×** | [C3](docs/benchmarks.md#finding-the-limit) |
| Dispatcher | found a cap of 600 checks/min; fixed to 6,000/min | [C3 dispatcher](docs/benchmarks.md#dispatcher) |
| Alerts at 30% / 60% injected failures (400 deliveries per run, 2 runs each) | **99.75–100% / 91.75–92.25%** delivered, matching the 1 − p⁵ model; **0 duplicates** | [C4](docs/benchmarks.md#results) |
| Tests | **461 tests, 97.8% coverage** in CI (Postgres 16); 8 mutation checks all caught | [CI](https://github.com/MhBaigtw/statusping/actions), [D23](docs/decisions.md#d23-what-the-mutation-checks-prove-precisely) |

**Live verification** on the deployed service: `scripts/verify_live.py`, with output in
[HANDOFF.md](HANDOFF.md).

## What's interesting here

- **A job queue built from scratch on PostgreSQL** ([apps/jobs/queue.py](apps/jobs/queue.py)). There is no
  Celery and no Redis, and the reasons are in [docs/decisions.md](docs/decisions.md) D1.
  - **Enqueue** is a plain INSERT, so it is transactional. An incident, its alert rows and their delivery
    jobs commit together or not at all.
  - **Claims** use `SELECT … FOR UPDATE SKIP LOCKED`. Workers never get the same job and never wait for
    each other.
  - **Leases:** each claimed job has a lease. Finishing is guarded by `locked_by`, so a worker whose lease
    expired cannot overwrite a job someone else reclaimed. A reaper returns crashed workers' jobs to the
    queue.
  - **Retries** use exponential backoff with jitter, or the handler's own retry time (HTTP `Retry-After`).
    A job is marked dead after `max_attempts`.
- **No check runs twice at once.** The dispatcher claims due monitors with SKIP LOCKED and enqueues their
  checks in the same transaction. A dedupe key prevents check pile-ups, and a unique `job_id` on each
  result makes a re-run of the same job a no-op.
- **Reliable alerts.** Each (incident, channel, event) delivery can exist only once. The delivery row is
  locked during the POST, so a duplicate run sends nothing. 5xx, 429 and timeouts are retried; other 4xx
  are not.
- **SSRF protection** ([apps/core/safe_http.py](apps/core/safe_http.py)):
  - Every resolved address is checked: private, loopback, link-local/metadata, multicast, reserved,
    CGNAT, and IPv4-mapped/6to4/Teredo forms.
  - The connection is pinned to the validated IP, with the original Host header and TLS SNI and
    certificate check. That closes the DNS-rebinding window.
  - The check runs at create time and before every request.
- **Verified with real concurrency.**
  - Tests use real threads against a real Postgres, plus the real `run_worker` process stopped by a real
    signal.
  - A mutation check removes each guarantee and confirms a test fails.
  - A live end-to-end run uses fake servers.

## Stack

Python 3.13, Django 5.2 LTS, Django REST Framework, SimpleJWT, drf-spectacular, django-filter,
django-environ, psycopg 3, httpx, PostgreSQL 16. Tests use pytest, pytest-django, factory_boy, respx and
time-machine.

## Setup (Windows, native PostgreSQL; no Docker or WSL needed)

```powershell
py -3.13 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
copy .env.example .env        # then edit DATABASE_URL, SECRET_KEY and DEMO_PASSWORD
```

Create the database role and database once, in psql as the `postgres` superuser
(`"C:\Program Files\PostgreSQL\16\bin\psql.exe" -U postgres`):

```sql
CREATE ROLE statusping LOGIN CREATEDB PASSWORD 'choose-a-password';
CREATE DATABASE statusping OWNER statusping;
```

`CREATEDB` lets the test runner create `test_statusping`. Then:

```powershell
.\.venv\Scripts\python.exe manage.py migrate
.\.venv\Scripts\python.exe manage.py createcachetable   # throttle counters (shared DB cache)
.\.venv\Scripts\python.exe manage.py seed_demo          # optional demo org (password = DEMO_PASSWORD)
```

## Running: two terminals

```powershell
# Terminal 1: the API
.\.venv\Scripts\python.exe manage.py runserver

# Terminal 2: the worker (job threads + scheduler loop). Ctrl+C stops it gracefully.
.\.venv\Scripts\python.exe manage.py run_worker --threads 4
```

- **API docs:** http://127.0.0.1:8000/api/docs/ (OpenAPI schema at `/api/schema/`).
- **Health:** `/api/health/` reports the database and the age of the newest worker heartbeat.
- **Multiple workers:** you can run several `run_worker` processes. They share the queue safely, and only
  one scheduler tick runs at a time (Postgres advisory lock).
- **Production** runs gunicorn (Linux only) with `gunicorn.conf.py`. See [docs/deploy.md](docs/deploy.md)
  for the hosting options and `render.yaml` for the live setup.

## Environment variables

See [.env.example](.env.example) for the full list with defaults.

| Variable | Purpose |
|---|---|
| `DATABASE_URL`, `SECRET_KEY`, `DEBUG`, `ALLOWED_HOSTS` | the usual Django settings (prod requires `SECRET_KEY`) |
| `DEMO_PASSWORD` | password for the `seed_demo` users (never printed) |
| `JWT_ACCESS_MINUTES`, `JWT_REFRESH_DAYS` | token lifetimes (15 min / 1 day) |
| `THROTTLE_*_RATE`, `NUM_PROXIES` | login/refresh/register/write rate limits; trusted proxy count |
| `MONITORS_PER_ORG`, `MONITOR_DEFAULT_*` | per-org limit (20), default interval (60 s) and timeout (5 s) |
| `WORKER_*`, `SCHEDULER_INTERVAL_SECONDS` | threads, polling, heartbeat, shutdown timeout, tick (10 s) |
| `JOB_*` | lease (60 s), attempts, backoff base/cap, retention (7 days) |
| `ALERT_MAX_ATTEMPTS`, `ALERT_TIMEOUT_SECONDS` | webhook retries (5) and timeout (5 s) |
| `SSRF_ALLOW_LOOPBACK` | **dev only**, for `scripts/e2e.py`; ignored unless `DEBUG`, forced off in prod |

## API overview (all under `/api/`)

| Endpoint | Who |
|---|---|
| `auth/register/`, `auth/token/`, `auth/token/refresh/`, `me/` | anyone / signed in |
| `orgs/` (list mine, create) · `orgs/{slug}/` (detail; rename = owner) | members |
| `orgs/{slug}/members/` (list, invite by email) · `members/{id}/` (change role, remove) | owner/admin write |
| `orgs/{slug}/monitors/` CRUD + `{id}/pause/`, `{id}/resume/` | owner/admin write, viewer read |
| `orgs/{slug}/monitors/{id}/checks/` (cursor-paginated, `ok`, `checked_after/before`) | members |
| `orgs/{slug}/incidents/` (`status=open\|resolved`) · `monitors/{id}/incidents/` | members |
| `orgs/{slug}/alert-channels/` CRUD (webhook URL masked) + `{id}/test/` | owner/admin write |
| `orgs/{slug}/incidents/{id}/deliveries/` | members |
| `health/` (503 if the worker is dead), `health/live/`, `schema/`, `docs/` | public |
| `/status/{slug}/` (JSON) · `/status/{slug}` (HTML): uptime 24h/7d/30d, recent incidents, no URLs | public, cached 30 s, throttled per IP |

- **Scoping:** a non-member gets **404** for anything in an org, a viewer gets **403** for writes, and an
  anonymous caller gets **401**.
- **Errors** always look like `{"error": {"code", "message", "details"}}`.

## Tests and checks

```powershell
.\.venv\Scripts\python.exe -m pytest                 # 460+ tests, coverage gate 85%
.\.venv\Scripts\python.exe -m ruff check . ; .\.venv\Scripts\python.exe -m ruff format --check .
.\.venv\Scripts\python.exe manage.py makemigrations --check --dry-run
.\.venv\Scripts\python.exe manage.py spectacular --validate --fail-on-warn --file NUL
.\.venv\Scripts\python.exe scripts\mutation_check.py # removes each guarantee, confirms a test fails
.\.venv\Scripts\python.exe scripts\e2e.py            # live ~5-minute run, logs in e2e-logs/
.\.venv\Scripts\python.exe scripts\verify_live.py https://<host>   # against a deployment (~10 min)
```

- **No real network:** tests never reach real DNS or HTTP. An autouse fake resolver and respx block it.
- **CI:** GitHub Actions runs ruff, the migration check, `check --deploy` with prod settings, schema
  validation and pytest with coverage against Postgres 16.

## Known limits of the free deployment

The planned hosting is free: one Render free web service that also runs the worker and scheduler, plus a
Supabase Free database ([docs/deploy.md](docs/deploy.md), option (c), D31). That has real costs:

- **Monitoring only runs while the instance is awake.**
  - Render puts a free service to sleep after 15 minutes without inbound requests, and may restart it at
    any time.
  - While it sleeps, no checks run and no alerts are sent. Missed intervals are not replayed, and uptime
    figures only cover time when checks ran.
  - Any request (the API, `/api/docs/`, a status page) wakes it in about a minute, and monitoring resumes.
  - There is deliberately no keep-alive pinger. It would use up the free instance hours this Render
    workspace shares with another service, and Render's acceptable-use policy can be read as prohibiting
    it.
- **Upgrade path for 24/7 monitoring:** add Render's Starter background worker (about $7/month) running
  `manage.py run_worker`, and turn off `EMBEDDED_WORKER`. No code changes
  ([docs/deploy.md](docs/deploy.md), option (c)).
- **Capacity:**
  - The 500 MB database holds roughly 40–45 monitors at a 60-second interval with 30 days of history.
  - Supabase's 5 GB/month egress allowance is why the worker polls the queue every 3 s, not every 1 s.
- **Database pause:** if nothing wakes the service for about a week, Supabase Free may pause the database
  (after a warning email). It is restored from the Supabase dashboard.

## Project docs

- [SPEC.md](SPEC.md): the full four-phase specification
- [TASKS.md](TASKS.md): the progress checklist
- [docs/decisions.md](docs/decisions.md): every design decision, with alternatives and reasons
- [docs/benchmarks.md](docs/benchmarks.md): every measurement, with commands
- [docs/deploy.md](docs/deploy.md): hosting options, prices checked, and the chosen setup
- [docs/resume-bullets.md](docs/resume-bullets.md): a summary with every number linked to its evidence
- [HANDOFF.md](HANDOFF.md): the latest checkpoint report, with verification output
- [CLAUDE.md](CLAUDE.md): working rules for AI-assisted sessions

# StatusPing

A multi-tenant uptime monitoring API. Organizations register HTTP monitors. A scheduler enqueues due
checks, and workers run them with strict timeouts. An incident opens after N consecutive failures and
resolves after M consecutive successes, so flapping sites don't page anyone. Alerts go to Discord or Slack
webhooks, with retries and no duplicates.

**Status: Phase 2 of 4 complete** (API, job queue, checks, incidents, alerts). Phase 3 (status page,
caching, benchmarks) and Phase 4 (deployment) are planned in [SPEC.md](SPEC.md).

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
- **Linux hosting:** in production (Phase 4) the web process would be gunicorn, which does not run on
  Windows.

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
| `health/`, `schema/`, `docs/` | public |

- **Scoping:** a non-member gets **404** for anything in an org, a viewer gets **403** for writes, and an
  anonymous caller gets **401**.
- **Errors** always look like `{"error": {"code", "message", "details"}}`.

## Tests and checks

```powershell
.\.venv\Scripts\python.exe -m pytest                 # 380+ tests, coverage gate 85%
.\.venv\Scripts\python.exe -m ruff check . ; .\.venv\Scripts\python.exe -m ruff format --check .
.\.venv\Scripts\python.exe manage.py makemigrations --check --dry-run
.\.venv\Scripts\python.exe manage.py spectacular --validate --fail-on-warn --file NUL
.\.venv\Scripts\python.exe scripts\mutation_check.py # removes each guarantee, confirms a test fails
.\.venv\Scripts\python.exe scripts\e2e.py            # live ~5-minute run, logs in e2e-logs/
```

- **No real network:** tests never reach real DNS or HTTP. An autouse fake resolver and respx block it.
- **CI:** GitHub Actions runs ruff, the migration check, `check --deploy` with prod settings, schema
  validation and pytest with coverage against Postgres 16.

## Project docs

- [SPEC.md](SPEC.md): the full four-phase specification
- [TASKS.md](TASKS.md): the progress checklist
- [docs/decisions.md](docs/decisions.md): every design decision, with alternatives and reasons
- [HANDOFF.md](HANDOFF.md): the latest checkpoint report, with verification output
- [CLAUDE.md](CLAUDE.md): working rules for AI-assisted sessions

# StatusPing: specification

A multi-tenant uptime monitoring API. Organizations register HTTP monitors. A scheduler enqueues due checks,
and workers run them with strict timeouts. An incident opens after N consecutive failures and resolves after
M consecutive successes (flap protection). Alerts go to Discord or Slack webhooks, with retries and no
duplicates.

The engineering focus is:

- a PostgreSQL-backed job queue built from scratch;
- never running the same check twice at once;
- crash recovery;
- reliable alert delivery;
- SSRF protection;
- in Phase 3, caching and measured throughput.

Portfolio project 2 of 3 (Humayun, GitHub `MhBaigtw`). Project 1 is FixFlow; StatusPing copies its patterns
but never imports from it.

## Environment (locked)

- Windows 11 Home with about 7.5 GB RAM, often under memory pressure. **No Docker, no WSL, no Redis, no
  Celery** for local development. Everything runs natively.
- Python 3.13 via the `py` launcher, with a venv. Native PostgreSQL 16, using its own role and database
  `statusping`.

## Stack (locked)

- **Runtime:** Python 3.13, Django 5.2 LTS, DRF, SimpleJWT, drf-spectacular, django-filter, django-environ,
  psycopg 3, httpx, gunicorn, whitenoise.
- **Dev:** pytest, pytest-django, pytest-cov, factory_boy, ruff, respx, time-machine.
- No Celery, no Redis, no third-party queue library.

## Conventions

- Business rules live in services and read queries in selectors. Views and serializers stay thin.
- Scoping is done in querysets plus permission classes. An out-of-scope object returns 404; a wrong role on
  an in-scope object returns 403.
- One error shape: `{"error": {"code", "message", "details"}}`.
- All times are UTC. Every business rule has a test.
- Job handlers are thin and call service functions. They are idempotent, because delivery is at-least-once.
- No speculative abstractions.

---

## Phase 1: foundation, organizations, monitors

### Settings and apps

- **Settings:** `config/settings/{base,dev,test,prod}.py`. Env vars cover the DB, secret, debug, hosts, JWT
  lifetimes, worker settings and check defaults. `prod.py` is hardened.
- **Apps:**
  - `core`: errors, pagination, health, SSRF-safe HTTP, seed
  - `accounts`
  - `organizations`
  - `monitors`: Monitor plus, in Phase 2, CheckResult and Incident
  - `jobs`
  - `alerts`: added in Phase 2

### Accounts and organizations

- **Custom User:** email login, no username, emails stored lowercase, case-insensitive login.
- **Register:** `POST /api/auth/register/` (email, password, Django validators), with a scoped throttle.
  Throttle counters live in the DB-backed shared cache.
- **Organization:** name, slug (unique, immutable, used by the public status page), created_at. The creator
  becomes the owner.
- **Membership:** user, organization, role (owner/admin/viewer), with a unique constraint on
  (user, organization).
  - Owners and admins manage monitors and alert channels, and invite existing users by email.
  - Viewers can only read.
  - An org always keeps at least one owner.

### Monitors

- **Fields:** name, url, method (GET/HEAD), interval_seconds (60–3600, default 60), timeout_seconds (1–10,
  default 5), expected_status (default 200), failure_threshold (default 3), recovery_threshold (default 2),
  is_paused, next_check_at, last_status (up/down/unknown), consecutive_failures, consecutive_successes,
  created_at, updated_at.
- **Limit:** 20 monitors per org (a setting).

### SSRF protection

- **URL rules:** http/https only, no credentials, ports 80, 443 or 1024–65535.
- **Address rules:** resolve the host and reject it if **any** address is private, loopback, link-local
  (including 169.254.169.254), multicast, reserved, unspecified, or an IPv4-mapped, 6to4, Teredo or NAT64
  form of one of these.
- **When:** validated at create/update **and** at every check. Redirects are not followed.
- **DNS rebinding:** closed by connecting to the validated IP while keeping the Host header and TLS SNI and
  certificate checks on the original hostname (see decisions.md).
- **Tests:** never touch real DNS or the network.

### Endpoints (under `/api/`)

- **Auth:** `auth/token/`, `auth/token/refresh/`, `auth/register/`
- **Me:** `me/`
- **Orgs:** `orgs/` (list mine, create), `orgs/{slug}/` (detail; rename by owner)
- **Members:** `orgs/{slug}/members/` (list, invite by email, change role, remove)
- **Monitors:** `orgs/{slug}/monitors/` (CRUD, plus `pause` and `resume` actions)
- **Other:** `health/` (DB and worker heartbeat), `schema/`, `docs/`

### Admin, seed data and CI

- **Admin and lists:** admin registration, pagination, and monitor filters (last_status, is_paused).
- **`seed_demo`:** idempotent. Creates a demo org with monitors on safe public targets. The password comes
  from `DEMO_PASSWORD` and is never printed.
- **CI** (GitHub Actions, Postgres 16): ruff check, ruff format --check, makemigrations --check, check
  --deploy (prod), spectacular --validate --fail-on-warn, and pytest with coverage at least 85%.

---

## Phase 2: Postgres job queue, checks, incidents, alerts

### Job queue (`apps/jobs`)

- **Job model:** kind, payload, status (queued/running/succeeded/failed/dead), run_after, attempts,
  max_attempts, locked_by, locked_until, last_error, dedupe_key, created_at, finished_at.
- **`enqueue()`:** a plain INSERT, so it is transactional. The optional `dedupe_key` has a partial unique
  constraint that applies while the job is queued or running.
- **Claim:** one short transaction runs `SELECT … FOR UPDATE SKIP LOCKED`, then sets running, the lease and
  attempts+1. The handler runs outside that transaction.
- **Finish:** guarded by `locked_by`, so a worker whose lease expired cannot overwrite a job that was
  reclaimed.
- **Retry:** exponential backoff with jitter, or the handler's explicit retry time. After `max_attempts` the
  job is marked dead.
- **Reaper:** returns expired leases to queued.
- **Worker:** `run_worker --threads N`, with one DB connection per thread. It shuts down gracefully on Ctrl+C
  or SIGTERM and writes a heartbeat row.
- **Scheduler:** a loop in the same command. Every 10 s it runs dispatch, the reaper and retention, and it is
  safe with two processes.
- **Retention:** succeeded jobs older than 7 days are deleted in batches.

### Models

- **CheckResult:** monitor, checked_at, ok, status_code, response_ms, error (timeout, dns, connection, ssl,
  unexpected_status, blocked_target), indexed on (monitor, checked_at DESC).
- **Incident:** monitor, started_at, resolved_at, cause. At most one open incident per monitor (partial
  unique constraint).
- **AlertChannel:** organization, kind (discord/slack), name, webhook_url, is_active.
  - The webhook URL must be https on discord.com, discordapp.com or hooks.slack.com, and passes SSRF checks.
  - It is masked in every API response and never logged.
- **AlertDelivery:** incident, channel, event (opened/resolved), status (pending/sent/failed), attempts,
  last_error, sent_at. Unique on (incident, channel, event).

### Behaviour

- **dispatch_due_checks:** `SELECT … FOR UPDATE SKIP LOCKED` on due, unpaused monitors. It advances
  next_check_at and enqueues run_check **in the same transaction**.
- **run_check:**
  1. Re-validate the target with the SSRF checks.
  2. Make the request with httpx.
  3. Record a CheckResult.
  4. With the monitor row locked, update the counters and last_status, open or resolve the incident, create
     the AlertDelivery rows and enqueue the deliver_alert jobs.

  A stale result is dropped safely.
- **deliver_alert:**
  1. Lock the delivery row. If it is already sent, do nothing.
  2. POST with a 5 s timeout.
  3. Network errors, timeouts, 429 (honouring Retry-After) and 5xx are retried, up to 5 attempts.
  4. Any other 4xx marks the delivery failed.

### Endpoints (Phase 2)

- `orgs/{slug}/monitors/{id}/checks/`: newest first; filters ok and a date range
- `orgs/{slug}/monitors/{id}/incidents/` and `orgs/{slug}/incidents/`: open/resolved filter
- `orgs/{slug}/alert-channels/`: CRUD by owner or admin, masked URL; a `test` action returns a job id
- `orgs/{slug}/incidents/{id}/deliveries/`

---

## Phase 3: measure and harden (NOT BUILT YET)

- **Public status page:** `GET /status/{org_slug}/` returns JSON plus a minimal HTML page, with current state
  and 24h/7d/30d uptime per monitor.
- **Caching:** DB cache, or per-process with a short TTL, invalidated on incident change. Measure whether it
  helps.
- **Status page benchmark:** cached vs uncached p50/p95.
- **Throughput benchmark:** jobs/second and checks/minute that one worker process sustains against a local
  fake target, with the method and hardware documented.
- **Failure-injection test:** a fake webhook that fails X% of the time, reporting delivery success and zero
  duplicates.
- **CheckResult retention:** delete or roll up results older than 30 days, in batches.
- **Query-count tests.**

## Phase 4: ship (NOT BUILT YET)

- The architect and Humayun choose hosting for a web process, an always-on worker process and Postgres.
- Deploy, then verify live.
- README with a Mermaid diagram.
- `docs/resume-bullets.md`, with every number linked to evidence.

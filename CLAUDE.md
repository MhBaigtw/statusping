# CLAUDE.md: rules for every session on StatusPing

Read these first: SPEC.md (what to build), TASKS.md (where we are), docs/decisions.md (why) and HANDOFF.md
(the last checkpoint).

## Environment (Windows, locked)

- **Python:** always use the venv interpreter, `.\.venv\Scripts\python.exe -m pip|pytest|...`. Bare `python`
  fails (it is the Microsoft Store alias). Create the venv with `py -3.13 -m venv .venv`.
- **Postgres 16** runs natively as the service `postgresql-x64-16`. psql is at
  `C:\Program Files\PostgreSQL\16\bin\psql.exe` and is not on PATH. The role and database are both
  `statusping`. **Never touch the `fixflow` database.**
- **No Docker, no WSL, no Redis, no Celery.** The job queue is Postgres (see D1 in docs/decisions.md).
- **RAM is tight** (~7.5 GB, often <1 GB free). Never run two full test suites at once.
- **Secrets** live in `.env`, which is gitignored. Never print, log or commit passwords, tokens, webhook URLs
  or the contents of `.env`.

## Conventions

- **Layers:**
  - Business rules live in `services.py`.
  - Read queries live in `selectors.py`.
  - Views and serializers stay thin.
  - Job handlers (`handlers.py`) are thin wrappers that call services.
- **Scoping** lives in querysets plus permission classes (`apps/organizations/permissions.py`). An
  out-of-scope object returns 404, never 403. A wrong role on an in-scope object returns 403.
- **One error shape:** `{"error": {"code": "<machine_code>", "message": "<human>", "details": {...}}}`.
- **Time:** all times are UTC. Every business rule has a test.
- **Jobs:** every job handler must be safe to run twice, because delivery is at-least-once.
- **Outbound HTTP** goes through `apps/core/safe_http.py`, which handles SSRF validation and IP pinning.
  Never call httpx directly from a service.
- **Tests** never hit the real network or DNS. The autouse `fake_dns` fixture blocks DNS, and respx mocks
  HTTP.
- No speculative abstractions, no base-class hierarchies, no plugin systems.

## Working rules

- Fix failing tests by fixing code, never by weakening or deleting the test. If a test was wrong, explain why
  in HANDOFF.md.
- Never claim something works without running it.
- Use conventional commits (`feat:`, `fix:`, `test:`, `docs:`, `chore:`, `ci:`).
- **Ask before:** creating GitHub repos, pushing, deleting anything outside this folder, dropping databases,
  force-pushing, anything paid, or any external account.

## Commands

```powershell
.\.venv\Scripts\python.exe -m pytest                       # tests + coverage (fails under 85%)
.\.venv\Scripts\python.exe -m ruff check . ; .\.venv\Scripts\python.exe -m ruff format --check .
.\.venv\Scripts\python.exe manage.py makemigrations --check --dry-run
.\.venv\Scripts\python.exe manage.py migrate ; .\.venv\Scripts\python.exe manage.py createcachetable
.\.venv\Scripts\python.exe manage.py runserver             # terminal 1: web
.\.venv\Scripts\python.exe manage.py run_worker --threads 4 # terminal 2: worker + scheduler
.\.venv\Scripts\python.exe manage.py seed_demo
.\.venv\Scripts\python.exe scripts\e2e.py                  # live end-to-end run (~5 min)
```

# Tasks

## Step 0
- [x] SPEC.md, TASKS.md, CLAUDE.md, HANDOFF.md, docs/decisions.md

## Phase 1: foundation, organizations, monitors
- [x] Settings package (base/dev/test/prod), .env.example, pinned requirements, pyproject (ruff, pytest)
- [x] Local Postgres role + database `statusping` (via pgpass, D2)
- [x] core: error shape, pagination, health, 404/500 handlers, strict boolean filter
- [x] accounts: custom User (email, lowercase), register, token (case-insensitive), refresh, me, throttles
- [x] organizations: Organization, Membership, services (create, rename, invite, change role, remove, last owner)
- [x] monitors: Monitor model, CRUD, pause/resume, filters, per-org limit (409 under org lock)
- [x] SSRF validation + pinned-IP HTTP client (D4)
- [x] Admin registration (webhook URL masked, read-only)
- [x] seed_demo (idempotent)
- [x] CI workflow
- [x] Phase 1 tests

## Phase 2: queue, checks, incidents, alerts
- [x] jobs: Job model + index, enqueue (transactional, dedupe), claim, complete, fail/retry, reaper, retention
- [x] Worker command: threads, heartbeat, graceful shutdown (SIGINT/SIGTERM/SIGBREAK), scheduler loop (advisory lock)
- [x] dispatch_due_checks (SKIP LOCKED, atomic enqueue)
- [x] CheckResult, Incident; run_check service + handler (idempotent per job)
- [x] AlertChannel, AlertDelivery; deliver_alert service + handler; test action
- [x] Endpoints: checks, incidents (monitor + org), alert-channels, deliveries
- [x] Health reports worker heartbeat age
- [x] Phase 2 tests incl. real-thread concurrency and worker command integration (subprocess + real signal)
- [x] EXPLAIN check for the claim index (D9; partial index dropped)
- [x] Mutation checks (SKIP LOCKED x2, no-lock x2, locked_by guard, delivery lock, delivery uniqueness)
- [x] scripts/e2e.py live run with fake target + fake webhook
- [x] README, HANDOFF, decisions
- [ ] Humayun approves creating MhBaigtw/statusping and pushing main
- [ ] Confirm the GitHub Actions result after the push

## Phase 3 (not started)
- [ ] Public status page GET /status/{org_slug}/ (JSON + minimal HTML) with 24h/7d/30d uptime
- [ ] Caching (DB or per-process, short TTL) invalidated on incident change; measure whether it helps
- [ ] Status page benchmark (p50/p95 cached vs uncached)
- [ ] Throughput benchmark (jobs/s, checks/min for one worker process) vs local fake target
- [ ] Failure-injection test for alert delivery (webhook failing X%): success rate, zero duplicates
- [ ] CheckResult retention (delete or roll up >30 days, in batches)
- [ ] Query-count tests
- [ ] Consider: hard total deadline per check (D4 limitation)

## Phase 4 (not started)
- [ ] Choose hosting (web + always-on worker + Postgres), checking current free/cheap options
- [ ] Deploy + live verification
- [ ] README Mermaid diagram (API, Postgres incl. jobs table, worker + scheduler, targets, webhooks)
- [ ] docs/resume-bullets.md with every number linked to evidence

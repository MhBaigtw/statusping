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
- [x] Humayun approved; created MhBaigtw/statusping and pushed main
- [x] GitHub Actions green (run 40970739665: 379 passed, 97.53%)

## Checkpoint 2, Part A: architect review fix
- [x] A1 total time budget per request (DNS + connect + TLS + read), socket-level deadline
- [x] A2 system check: lease >= 3x longest job budget (fails fast)
- [x] A3 real-socket tests: drip, TLS stall, slow DNS, redirect loop, job finishes inside lease

## Phase 3: measure and harden
- [x] Public status page GET /status/{org_slug}/ (JSON + minimal HTML) with 24h/7d/30d uptime
- [x] status_page_public opt-out (owner only)
- [x] Caching (DB cache, 30 s) invalidated on commit; measured: kept (about 17x at p50)
- [x] Status page benchmark (p50/p95 cached vs uncached) + EXPLAIN; redundant FK index dropped
- [x] Throughput benchmark (jobs/s, checks/min, threads 1/4/8, 2 processes) vs local fake target
- [x] Dispatcher bottleneck found and fixed (600/min cap -> up to 6,000/min)
- [x] Failure-injection benchmark for alert delivery (30% and 60%, zero duplicates)
- [x] CheckResult retention (per monitor, hourly, bounded); rollups not needed (D26)
- [x] Query-count test for the status page
- [x] Health 503 on stale worker + /api/health/live/
- [x] Structured per-job log line, webhook URLs masked
- [x] Pushed Parts A-D (approved); CI green: run 40985948174, 460 passed, 97.83%

## Phase 4
- [x] Research hosting options with current pricing (docs/deploy.md)
- [x] Deploy config: gunicorn.conf.py, bin/render-build.sh, render.yaml draft, systemd + Caddy files
- [x] Humayun's choices (2026-10-30): (a) Render free web + Starter worker + Neon Launch;
      no real Discord webhook (verify via the delivery log); demo logins public
- [x] Revised (2026-10-29 session): Supabase Free (session pooler) replaces Neon Launch, ~$7/month (D30)
- [ ] Humayun: Supabase project, Render billing + Blueprint deploy, secrets (see HANDOFF)
- [ ] Deploy + live verification (health, docs, status page, scheduled checks, 500 incident + delivery, proxy depth)
- [ ] README: live links, Mermaid diagram, measured results, hosting limits, demo logins
- [ ] docs/resume-bullets.md with every number linked to evidence
- [ ] Final verification, push (ask), CI, HANDOFF Checkpoint 2

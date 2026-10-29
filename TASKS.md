# Tasks

## Step 0
- [ ] SPEC.md, TASKS.md, CLAUDE.md, HANDOFF.md, docs/decisions.md

## Phase 1: foundation, organizations, monitors
- [ ] Settings package (base/dev/test/prod), .env.example, pinned requirements, pyproject (ruff, pytest)
- [ ] Local Postgres role + database `statusping`
- [ ] core: error shape, pagination, health, 404/500 handlers
- [ ] accounts: custom User (email, lowercase), register, token (case-insensitive), refresh, me, throttles
- [ ] organizations: Organization, Membership, services (create, rename, invite, change role, remove, last owner)
- [ ] monitors: Monitor model, CRUD, pause/resume, filters, per-org limit
- [ ] SSRF validation + pinned-IP HTTP client
- [ ] Admin registration
- [ ] seed_demo
- [ ] CI workflow
- [ ] Phase 1 tests

## Phase 2: queue, checks, incidents, alerts
- [ ] jobs: Job model + indexes, enqueue, claim, complete, fail/retry, reaper, retention
- [ ] Worker command: threads, heartbeat, graceful shutdown, scheduler loop (advisory lock)
- [ ] dispatch_due_checks (SKIP LOCKED, atomic enqueue)
- [ ] CheckResult, Incident; run_check service + handler
- [ ] AlertChannel, AlertDelivery; deliver_alert service + handler; test action
- [ ] Endpoints: checks, incidents (monitor + org), alert-channels, deliveries
- [ ] Health reports worker heartbeat age
- [ ] Phase 2 tests incl. real-thread concurrency and worker command integration
- [ ] EXPLAIN check for the claim index
- [ ] Mutation checks (SKIP LOCKED x2, locked_by guard, delivery uniqueness/lock)
- [ ] scripts/e2e.py live run with fake target + fake webhook
- [ ] README, HANDOFF, decisions
- [ ] Ask to create repo + push; confirm CI

## Phase 3 (not started)
- [ ] Public status page (JSON + HTML) with 24h/7d/30d uptime
- [ ] Caching with invalidation on incident change; measure
- [ ] Status page benchmark (p50/p95 cached vs uncached)
- [ ] Throughput benchmark (jobs/s, checks/min) vs local fake target
- [ ] Failure-injection test for alert delivery
- [ ] CheckResult retention (batches)
- [ ] Query-count tests

## Phase 4 (not started)
- [ ] Choose hosting (web + always-on worker + Postgres)
- [ ] Deploy + live verification
- [ ] README Mermaid diagram
- [ ] docs/resume-bullets.md with evidence links

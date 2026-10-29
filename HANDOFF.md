# HANDOFF: StatusPing Checkpoint 1 (Phases 1-2)

- **Status:** done locally. Not pushed yet: creating the public repo `MhBaigtw/statusping` and pushing
  `main` wait for Humayun's approval, so the CI result is **not verified yet**.
- **Versions:** Python 3.13.3 (CI pins 3.13), Django 5.2.18, DRF 3.18.3, SimpleJWT 5.5.1, drf-spectacular
  0.30.0, psycopg 3.3.6, httpx 0.28.1, PostgreSQL 16.14 (native Windows service).
- **Machine:** Windows 11 Home, 7.33 GB RAM. Free RAM at the start was **0.90 GB**, and 0.35–0.43 GB
  during test runs. Full suites were never run in parallel.

## What was built

### Phase 1

- **Settings:** split into base/dev/test/prod. prod is hardened like FixFlow: no default SECRET_KEY,
  forced `DEBUG=False`, HSTS, TLS to the DB, secure cookies, and a health-check exemption from the HTTPS
  redirect.
- **Accounts:** custom email User stored lowercase, with case-insensitive login. `POST /api/auth/register/`
  uses Django's password validators. Throttles (register, login, refresh, writes) share the DB cache.
- **Organizations:** organizations and memberships with owner/admin/viewer roles. Members can be invited
  by email (existing users only), have their role changed, or be removed.
  - Only owners manage the owner role.
  - The last owner is protected (409 `last_owner`, checked under an org row lock).
- **Monitors:** CRUD plus pause/resume, filters (`last_status`, `is_paused`), and a 20-per-org limit
  (409, under an org lock).
- **SSRF-safe outbound HTTP** (`apps/core/safe_http.py`):
  - URL rules: scheme, credentials and ports.
  - Every resolved address is checked: private, loopback, link-local/metadata, multicast, reserved,
    CGNAT, and IPv4-mapped/6to4/Teredo/NAT64 forms.
  - The connection is **pinned to the validated IP**, with the original Host header and SNI/certificate
    check, so the DNS-rebinding window is closed.
  - No redirects, no proxy environment. Validation runs at create/update and before every request.
- **Admin, seed and CI:** admin for every model (webhook URL masked, read-only), idempotent `seed_demo`,
  and a GitHub Actions workflow.

### Phase 2

- **Postgres job queue** (`apps/jobs`):
  - Transactional `enqueue()` with a partial-unique `dedupe_key`.
  - `claim()` with `FOR UPDATE SKIP LOCKED`, with the handler run outside the claim transaction.
  - Lease-guarded `complete()` and `fail()`, so a stale worker cannot overwrite a reclaimed job.
  - Exponential backoff with jitter, or the handler's `retry_at` (for example Retry-After), capped at
    15 min. Out of attempts means `dead`; a permanent error means `failed`.
  - A reaper for expired leases, and batched retention of succeeded jobs older than 7 days.
- **`run_worker --threads N`:**
  - N job threads plus a control thread. The control thread writes the heartbeat every 5 s and runs the
    scheduler tick every 10 s: dispatch, reap, purge, under a Postgres advisory lock.
  - Each thread has its own DB connection.
  - Graceful shutdown on SIGINT, SIGTERM and SIGBREAK (Windows): it stops claiming and finishes in-flight
    jobs up to a timeout.
  - `--no-scheduler` runs a job-only worker.
- **`dispatch_due_checks`:** claims due monitors with SKIP LOCKED and enqueues run_check in the same
  transaction.
- **`run_check`:**
  - Re-validates the target (SSRF) and makes the request.
  - Records a CheckResult, idempotently via a unique `job_id`.
  - Under the monitor row lock: updates counters and last_status, opens or resolves the incident
    (threshold and flap protection), and creates AlertDelivery rows plus deliver_alert jobs.
  - Stale results (monitor deleted or paused) are dropped.
- **`deliver_alert`:**
  - Holds the row lock for the POST, so a second run is a no-op.
  - Retries network errors, timeouts, 5xx and 429 (honouring Retry-After), up to 5 attempts. Other 4xx
    are failed without retry.
  - Discord mentions are disabled and Slack text is escaped.
- **Endpoints:**
  - checks (cursor-paginated, `ok` and date filters);
  - incidents (org-level and monitor-level, `status=open|resolved`);
  - alert channels (CRUD, masked URL, `test` action returns 202 with `job_id`);
  - deliveries;
  - `health/` with worker heartbeat age (`ok` / `stale` / `missing`).

## Key files

| File | What it does |
|---|---|
| `apps/core/safe_http.py` | SSRF validation + pinned-IP request (the only place outbound HTTP happens) |
| `apps/jobs/queue.py` | enqueue / claim / complete / fail / run_job / reap_expired / purge_succeeded |
| `apps/jobs/worker.py` | Worker threads, heartbeat, `scheduler_tick` (advisory lock), graceful shutdown |
| `apps/jobs/registry.py` | job kind → handler dotted path |
| `apps/monitors/services.py` | monitor CRUD rules, `dispatch_due_checks`, `run_check`, `apply_result` (incidents) |
| `apps/alerts/services.py` | channel URL rules, `queue_alerts`, `deliver_alert`, Retry-After parsing, message building |
| `apps/organizations/permissions.py` | org scoping: 401 → 404 (non-member) → 403 (role) |
| `apps/organizations/services.py` | org creation, invites, role changes, last-owner rule |
| `tests/conftest.py` | autouse fake DNS + respx (no real network), role fixtures, error-shape assert |
| `tests/concurrency.py` | real-thread helpers (barrier start, lock holder in its own connection) |
| `tests/fake_servers.py` | fake target + fake webhook on 127.0.0.1 (worker-process test and e2e) |
| `scripts/e2e.py` | live end-to-end run (web + worker processes, real timings) |
| `scripts/mutation_check.py` | removes each guarantee, runs the tests, restores via git |
| `scripts/explain_claim.py` | rolled-back EXPLAIN of the claim query with each index set |

## Decisions beyond the prompt

All are in docs/decisions.md. The main ones:

- **D1:** why Postgres instead of Celery + Redis, and what would make me switch.
- **D4:** DNS rebinding is **closed** (IP pinning with `sni_hostname`), not just documented. The remaining
  limitations are recorded: one address is tried, and timeouts are per phase rather than a total
  deadline.
- **D5:** the loopback switch for e2e needs `DEBUG=True` **and** `SSRF_ALLOW_LOOPBACK=True`, covers
  loopback only, and prod and test hard-code it off. Tested.
- **D6:** `failed` = permanent error, `dead` = out of attempts.
- **D8:** run_check jobs use `max_attempts=1` and a per-monitor dedupe key, so there is no pile-up and no
  retries that would distort the failure streak.
- **D9:** the partial queued-job index was **dropped** after EXPLAIN showed no gain.
- **D10:** the advisory lock is per tick, not leader election. Ticks would still be correct without it.
- **D12:** a unique `CheckResult.job_id` makes run_check idempotent.
- **D13:** `last_status` is the raw last check. `started_at` is the first failure of the streak.
- **D14:** the delivery row lock is held during the POST. The POST-ok/commit-fail window is documented
  (at-least-once). Test pings are the one deliberately non-idempotent handler.
- **D17:** an invite needs an existing account. An admin can therefore learn whether an email is
  registered, but registration reveals that anyway.
- **D19:** cursor pagination for checks; strict boolean filters; health returns "degraded" (200) rather
  than 503 for a stale worker.

## Verification output (real, trimmed)

### pytest

```
379 passed in 50.67s
Required test coverage of 85% reached. Total coverage: 97.66%
apps\jobs\management\commands\run_worker.py   11   11   0%
```

`run_worker.py` shows 0% only because it runs in a subprocess, which coverage doesn't track;
`test_run_worker_command_end_to_end_and_stops_on_signal` executes it.

### Lint, migrations, deploy check, schema

```
== ruff check                → All checks passed!
== ruff format --check       → 91 files already formatted
== makemigrations --check    → No changes detected
== check --deploy (prod, --fail-level WARNING) → System check identified no issues (0 silenced).
== spectacular --validate --fail-on-warn → (no warnings) exit 0
```

### Mutation checks (`scripts/mutation_check.py`; all restored, working tree clean)

```
== M1 remove SKIP LOCKED from the job claim         1 failed  test_claim_skips_rows_locked_by_another_transaction_without_waiting
== M2 remove SKIP LOCKED from the monitor dispatch  1 failed  test_dispatch_skips_a_locked_monitor_without_waiting
== M1b remove the row lock from the job claim       2 failed  test_concurrent_claimers_never_get_the_same_job (+ the no-wait test)
== M2b remove the row lock from the dispatch        2 failed  test_concurrent_dispatchers_never_claim_the_same_monitor (+ no-wait)
== M3 remove the locked_by guard on finish          1 failed  test_stale_worker_cannot_finish_a_reclaimed_job
== M4a remove the delivery row lock                 1 failed  test_concurrent_runs_of_one_delivery_send_one_webhook
== M4b remove the delivery uniqueness constraint    1 failed  test_database_refuses_a_duplicate_delivery
All mutations caught.
```

**Note:** removing only SKIP LOCKED does not cause double claims, because plain FOR UPDATE blocks and
then re-checks the row. So the "never twice" tests survive M1 and M2, and the no-waiting tests are what
catch them. M1b and M2b show the double-claim tests do fail without a lock (D23).

### Live end-to-end run (`scripts/e2e.py`, final run)

The run used separate runserver and run_worker processes, the dev DB, real 60 s intervals, and
`failure_threshold=2`, `recovery_threshold=2`.

```
[22:26:21] health: {'status': 'ok', 'database': 'ok', 'worker': {'status': 'ok', 'last_seen_seconds_ago': 3}}
[22:26:24] registered e2e-20261029222621@example.com and logged in (with an upper-cased email)
[22:26:25] org e2e-20261029222621, monitor 3, channel masked as http://127.0.0.1:62947/****
[22:26:32] first check: ok=True status=200 791ms
[22:26:32] target flipped to 500
[22:28:30] incident 4 OPEN after 118s, 2 failed checks (threshold 2), cause=unexpected_status
[22:28:45] webhook received exactly 1 message: '[DOWN] Fake target is down\nURL: http://127.0.0.1:62946/health\nCause: Unexpected status code\nSince: 2026-10-29 22:27:29 UTC'
[22:28:45] target flipped back to 200
[22:30:34] incident RESOLVED after 108s
[22:30:49] webhook received exactly 1 more message: '[UP] Fake target is back up\n...Was down for about 3 min (Unexpected status code)\nResolved: 2026-10-29 22:30:29 UTC'
[22:30:49] deliveries: opened=sent (attempts 1), resolved=sent (attempts 1)
[22:30:49] checks recorded: 5, fake target hits: 5
[22:30:54] cleaned up: monitor and alert channel deleted
[22:30:55] web exited with code 3221225786 (STATUS_CONTROL_C_EXIT, expected)
[22:30:55] worker exited with code 0
[22:30:56] peak working set: web 78 MB, worker 77 MB
[22:30:56] PASSED.
```

Worker log excerpt (UTC):

```
22:26:18.231Z INFO apps.jobs.worker [MainThread] worker MuhammadLaptop:54732:61b5f2 started: 2 job thread(s), scheduler on
22:28:28.816Z INFO apps.monitors.services [job-2] monitor 3: incident 4 opened (unexpected_status)
22:28:28.887Z INFO apps.alerts.services [job-2] delivery 6 (channel 3, opened): attempt 1 -> sent
22:30:29.284Z INFO apps.monitors.services [job-2] monitor 3: incident 4 resolved
22:30:29.331Z INFO apps.alerts.services [job-2] delivery 7 (channel 3, resolved): attempt 1 -> sent
22:30:55.373Z INFO apps.jobs.worker [MainThread] worker ...: shutting down, waiting up to 30.0s for in-flight jobs
22:30:55.483Z INFO apps.jobs.worker [MainThread] worker ... stopped cleanly
```

- **Logs:** the webhook path appeared 0 times in the web and worker logs.
- **Memory:** the peak working set (process plus children) was **78 MB** for web (runserver) and
  **77 MB** for the worker (2 job threads).

**Unplanned live evidence of retries (second e2e run).** The first run's monitor had been left active,
pointing at that run's dead fake target. During the second run it opened its own incident, and its alert
to the dead fake webhook was retried with growing delays, then failed:

```
22:19:55Z delivery 3 attempt 1 -> pending Network error (ConnectError)
22:20:05Z attempt 2   22:20:18Z attempt 3   22:20:51Z attempt 4
22:21:45Z attempt 5 -> failed Network error (ConnectError)
```

That leftover was a bug in the script. e2e.py now deletes its monitor and channel at the end (verified in
the final run), and the leftover rows were paused.

### git log --oneline

```
4b07ea0 fix(e2e): delete the run's monitor and channel afterwards; note repeat EXPLAIN run
8d45ba0 fix: UTC log timestamps; e2e measures the real interpreter's memory, not the venv launcher
2c67091 chore: add no-lock mutations to the mutation check
8467e37 chore: mutation-check script for the queue, dispatch and delivery guarantees
6bb6277 perf: drop partial queued-job index after EXPLAIN showed no gain; test concurrent delivery
1ea5c1a feat: Postgres job queue, worker, checks, incidents and alerts with tests
eb519d6 feat: foundation, accounts, organizations, monitors and SSRF protection
```

(The commit adding this HANDOFF follows.)

### CI

**Not run yet.** It needs the repo to exist and `main` to be pushed (waiting for approval).

## Bugs found and test changes

- **Real bug:** `_is_tls_error` looped forever on a cyclic exception chain. respx creates such cycles.
  It is fixed with a visited set and has a regression test.
- **TLS test injection point:** the TLS-error check test injects the error by patching `safe_http.send`
  instead of a respx side effect, because respx replaces the exception's `__cause__` and loses the SSL
  error. The assertion itself is unchanged (D22).
- **Two test expectations were wrong; the code was right:**
  - `hooks.slack.com` was missing from the fake DNS.
  - An `http://127.0.0.1` webhook is correctly rejected by the "must use https" rule before the host
    rule.
- **One weak assertion removed:** a meaningless `locked_by` check in the two-worker test, replaced by the
  per-job `attempts == 1` check, which is the real guarantee.
- **e2e measurement bug:** memory first read about 4 MB because a venv's python.exe on Windows is a
  launcher stub. It now sums the process tree, checked against a known 30 MB allocation (44 MB measured).

## Known gaps, risks, surprises

- **Timeouts are per phase:** httpx timeouts apply to connect, write and each read, not as a total
  deadline. A slow-drip target can stretch a check (D4). Candidate fix for Phase 3.
- **One address per check:** only one resolved address is tried (IPv4 preferred). There is no fallback to
  a second address.
- **First-check latency:** the first check after worker start took about 790 ms against localhost (later
  checks ~15 ms). This is likely cold-start cost: imports and building the SSL context, which `send()`
  does even for http. Not investigated further.
- **At-least-once alerts:** if the webhook POST succeeds but the DB commit fails, the alert is sent again.
  This can't be avoided without receiver idempotency.
- **Clock:** lease and run_after times use the worker's clock (fine on one host; D7).
- **Not done:** a real Discord or Slack webhook was not tried. Tests and e2e use fakes, and the optional
  manual check needs a webhook Humayun would create.
- **gunicorn is not exercised locally** because it doesn't run on Windows. That is a Phase 4 concern.
- **Dev DB leftovers:** it contains a few `e2e-*` users and orgs from the three runs. Their monitors are
  deleted (final run) or paused (earlier runs). They are harmless.

## Actions for Humayun

1. **Approve creating the public repo and pushing `main`.** On approval I would run:
   ```powershell
   gh repo create MhBaigtw/statusping --public --source . --remote origin --description "Multi-tenant uptime monitoring API with a from-scratch Postgres job queue"
   git push -u origin main
   gh run watch   # then report the CI result
   ```
2. **Optional:** create a Discord webhook (Server Settings → Integrations → Webhooks) if you want one
   manual real-delivery check. Don't paste the URL anywhere public. Add it through the API as an alert
   channel and use its `test` action.
3. **To run it yourself:** see README "Running: two terminals".

## Repo URL and latest commit

- Repo: not created yet (pending approval). Intended: https://github.com/MhBaigtw/statusping
- Latest commit: the HANDOFF commit on top of `4b07ea0` (`git log -1` gives the hash).

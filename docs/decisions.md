# Decision log

Each entry gives the choice, the alternatives considered, and the reason.

## D1. The job queue is built on PostgreSQL, not Celery + Redis

- **Choice:** the queue is a plain `jobs_job` table (`apps/jobs/queue.py`). Workers claim jobs with
  `SELECT … FOR UPDATE SKIP LOCKED`, and `run_worker` is a threaded Django management command.
- **Alternatives:**
  - Celery + Redis (or RabbitMQ).
  - A ready-made Postgres queue library (procrastinate, django-q2, pgqueuer).
- **Why Postgres:**
  1. **The machine.** It's Windows 11 Home with about 7.5 GB RAM, no Docker and no WSL. Redis has no
     supported native Windows build, and Celery dropped official Windows support. Postgres is already
     installed and running.
  2. **Fewer moving parts.** It's one datastore to run, back up and deploy (Phase 4 hosting gets cheaper),
     and there's no broker/database consistency to reason about.
  3. **Transactional enqueue.** `enqueue()` is an INSERT in the caller's transaction. The incident, its
     AlertDelivery rows and their deliver_alert jobs commit together or not at all, and the same holds for
     a monitor claim and its run_check job. With Celery you need `transaction.on_commit` and still accept
     a gap: if the commit succeeds and the publish fails, the job is lost. Here that gap doesn't exist,
     and it is tested (`test_enqueue_in_rolled_back_transaction_leaves_no_job`,
     `test_claim_and_enqueue_are_atomic`, `test_incident_and_its_alerts_roll_back_together`).
  4. **The queue is part of the engineering story.** Leases, SKIP LOCKED, reaping, backoff and idempotent
     handlers are what this project is meant to demonstrate. A library would hide them.
- **What would make me switch to Celery/Redis (or a hosted queue):**
  - Sustained throughput where queue polling becomes a measurable share of Postgres load. Phase 3 measures
    jobs/second.
  - Needing fan-out, priorities or rate-limited queues across many job types.
  - Many worker hosts, where hundreds of pollers cost more than a push-based broker.
  - A team already operating Redis. Switching would keep the handler and service code: handlers are thin
    and idempotent already.

## D2. Local DB role created through the existing pgpass superuser entry

- **Choice:** role `statusping` (LOGIN, CREATEDB) and database `statusping`, created by connecting as
  `postgres` via `%APPDATA%\postgresql\pgpass.conf` (same as FixFlow D1). A random password was written
  only to the gitignored `.env`. The `fixflow` database was not touched.
- **Reason:** no superuser password had to be typed or shown. CREATEDB lets pytest create
  `test_statusping`.

## D3. App layout: checks and incidents live in `monitors`, alerts in `alerts`

- **Choice:**
  - `monitors` holds Monitor, CheckResult, Incident and the check/incident services.
  - `alerts` holds AlertChannel and AlertDelivery.
  - `jobs` holds the queue and the worker.
- **Alternatives:** separate `checks` and `incidents` apps.
- **Reason:** counters, results and incidents change together, under one monitor row lock, in one
  transaction (`run_check` → `apply_result`). Splitting them would only add cross-app imports. Alerts are
  a separate concern with their own secrets and delivery rules.

## D4. DNS rebinding is closed by pinning the connection to the validated IP

- **Choice:** `safe_http.send()` resolves the host, rejects it if **any** address is non-public, then
  connects to one validated address:
  - The URL host is replaced by the IP.
  - The `Host` header is set to the original host (with its port, if not the default).
  - For https, `extensions={"sni_hostname": host}` makes httpcore send SNI and verify the certificate
    against the original hostname, using the default verifying SSL context.
- **Also:**
  - Redirects are never followed (`follow_redirects=False`). A monitor whose site redirects records the
    3xx; set `expected_status` to it if that is normal.
  - Proxy environment variables are ignored (`trust_env=False`), because a proxy would make the real
    connection target something we did not validate.
- **Reason:** with resolve-then-connect, a DNS answer can change between validation and the socket
  connect. Pinning removes that window and also makes URL-parser differences irrelevant, since the socket
  goes to the IP we checked. Tests assert the pinned IP, Host and SNI (`tests/test_ssrf.py`).
- **Limitations:**
  - Only one address is tried. IPv4 is preferred, falling back to the first IPv6; there is no
    happy-eyeballs fallback to another address.
  - ~~httpx timeouts are per phase, not a total deadline.~~ Fixed in Checkpoint 2: one total budget per
    request, enforced at the socket level (D24).

## D5. The loopback switch for the e2e run exists only in DEBUG

- **Choice:** `SSRF_ALLOW_LOOPBACK` allows `127.0.0.0/8` and `::1` only (never private, link-local or
  metadata addresses). It is honoured only when `settings.DEBUG` is also True. `prod.py` and `test.py`
  hard-code it to False, and `prod.py` forces `DEBUG=False`.
- **Webhooks:** in that mode alert channels may also use `http://127.0.0.1…`, so the e2e fake receiver
  works.
- **Tested:** `test_loopback_targets_cannot_be_enabled_in_prod` sets the env var to True and imports prod
  settings; `test_loopback_switch_needs_debug` covers the DEBUG check.

## D6. Job status meanings: `failed` vs `dead`

- **Choice:**
  - `failed`: the handler raised `PermanentError`, meaning a retry cannot help (for example a webhook
    returning 404).
  - `dead`: the job used up `max_attempts`, through errors or expired leases.
  - Both are kept for inspection, and neither is retried.
- **Reason:** the spec lists both. Giving them distinct meanings lets an operator tell "misconfigured" from
  "flaky / down".

## D7. Claim, finish and lease details

- **Claim:** ORM `select_for_update(skip_locked=True)` plus `bulk_update`, in one transaction, then
  commit. The handler runs outside that transaction, so a slow handler never holds the claim lock.
- **Finish:** `complete()` and `fail()` filter on `status='running' AND locked_by=<this worker>`. A worker
  whose lease expired gets 0 rows updated, logs "lease lost" and changes nothing.
  `locked_by = <host>:<pid>:<random>/<thread>`, unique per thread.
- **Lease and reaper:** the lease is 60 s (`JOB_LEASE_SECONDS`). The reaper requeues expired leases, or
  marks them dead on the final attempt. A lost attempt still counts towards `max_attempts`, so a job that
  crashes the worker cannot loop forever.
- **Clock:** lease and run_after times come from the worker's clock (`timezone.now()`). That is fine for
  one host or NTP-synced hosts. With badly skewed hosts, DB `now()` would be safer; revisit if deployed on
  several machines.

## D8. Dedupe keys and check retries

- **run_check:**
  - `dedupe_key="run_check:<monitor_id>"`. If workers fall behind, a monitor gets at most one pending
    check instead of a pile-up (`test_dispatch_does_not_pile_up_jobs_for_a_slow_monitor`).
  - `max_attempts=1`: a failed or crashed check is not retried, because the next scheduled check comes
    within `interval_seconds` and a retry would distort the consecutive-failure count.
- **deliver_alert:** `dedupe_key="deliver_alert:<delivery_id>"` and `max_attempts=5`.
- **Dedupe mechanics:**
  - On conflict, `enqueue()` catches the IntegrityError inside a savepoint, so the caller's transaction
    survives, and returns the existing pending job.
  - The key is `NULL` (not `""`) when unused, so unkeyed jobs never collide.

## D9. Claim index chosen by EXPLAIN (rolled-back experiment)

- **Setup:** 200,000 succeeded jobs and 2,000 queued jobs (1,000 due), inserted and ANALYZEd inside a
  transaction that was rolled back. Script:
  `scripts/explain_claim.py` (prints all four plans).
- **Results** for the claim query:

  | Index set | Plan | Execution |
  |---|---|---|
  | (status, run_after) + partial (run_after) WHERE queued | Index Scan on partial, Incremental Sort | 0.184 ms |
  | only (status, run_after) | Index Scan, Index Cond on status and run_after | 0.157 ms |
  | only the partial index | Index Scan on partial | 0.150 ms |
  | no job index | Bitmap scan via the dedupe partial unique index, then a full Sort | 1.583 ms |

- **Repeat run:** after migration 0002, the script was re-run. Every index variant took 0.11–0.20 ms, and
  no index took 1.62 ms. The differences between index variants are noise at this scale.
- **Choice:** keep only `(status, run_after)`. The partial index gave no measurable gain, and the composite
  index also serves the reaper (`status='running'`). Dropped in migration `jobs/0002`.
- **Note:** even with no job index, the dedupe constraint's partial index (pending rows only) keeps the
  claim fast. It would degrade if pending rows grew large.

## D10. The scheduler is a thread in every worker, serialised by an advisory lock

- **Choice:** each `run_worker` has a control thread that writes the heartbeat every 5 s and runs a
  scheduler tick every 10 s: dispatch, reap, purge, and prune old heartbeats.
- **Lock:** a tick runs only if `pg_try_advisory_lock(key)` succeeds; it unlocks afterwards. A worker that
  loses the race just skips that tick.
- **Safety without the lock:** two overlapping ticks are still correct. Dispatch uses SKIP LOCKED, and
  reap and purge are single UPDATE/DELETE statements. The lock only avoids wasted work. This is per tick,
  not leader election, so no failover logic is needed.
- **Flag:** `--no-scheduler` runs a job-only worker.
- **Tests:** `test_scheduler_tick_is_skipped_while_another_holds_the_lock` and
  `test_two_workers_share_the_queue_without_duplicates`.

## D11. Retention of jobs

- **Choice:** each tick deletes succeeded jobs whose `finished_at` is older than
  `JOB_SUCCEEDED_RETENTION_DAYS` (7), in batches of `JOB_PURGE_BATCH_SIZE` (1,000), at most 10 batches per
  tick. That is up to 10k rows per 10 s.
- **Kept:** failed and dead jobs stay. They are rare and are the debugging record.
- **Why batches:** bounded batches keep each DELETE short, so it never holds many row locks or bloats one
  transaction.

## D12. run_check is idempotent per job

- **Choice:** `CheckResult.job_id` is unique (a plain bigint, not a FK, because jobs are purged).
  - A rerun of a job that already recorded its result returns early, before any HTTP request.
  - A concurrent rerun that slipped past the early check hits the unique constraint, rolls back its whole
    transaction (counters included) and is dropped.
- **Tests:** `test_running_the_same_job_twice_records_one_result` and
  `test_concurrent_duplicate_run_is_dropped_by_the_unique_job_id`.

## D13. Status and incident semantics

- **`last_status`:** the raw outcome of the most recent check (up/down). The flap-protected state is
  "is there an open incident".
- **`started_at`:** the time of the first failed check in the streak (when the downtime began), not when
  the threshold was crossed.
- **`resolved_at`:** the time of the check that completed the recovery streak.
- **`cause`:** the error of the check that crossed the threshold.
- **Pausing:** a paused monitor keeps any open incident, and resuming checks it immediately. A result that
  arrives after a pause or delete is dropped entirely: no CheckResult, no counter change.

## D14. Alert delivery holds the row lock during the POST

- **Choice:** `deliver_alert` runs `SELECT … FOR UPDATE` on the AlertDelivery row, POSTs (5 s timeout),
  records the outcome and commits. A second concurrent run blocks on the lock, then sees `sent` and does
  nothing (`test_concurrent_runs_of_one_delivery_send_one_webhook`).
- **Cost:** one DB connection and transaction held for up to the webhook timeout per in-flight delivery.
  That is fine at a few threads.
- **Remaining window:** if the POST succeeds but the commit fails (crash or DB loss between the two), the
  retry sends again. Exactly-once delivery to a third-party webhook is impossible without the receiver's
  cooperation. Discord and Slack webhooks have no idempotency keys.
- **Test alerts** (`send_test_alert`) have no delivery row: running that job twice sends two test pings.
  That is deliberate, the only exception to "handlers are idempotent", and harmless.

## D15. Alert retry rules

- **Retryable** (via the queue's backoff): network errors, timeouts, DNS failure, 5xx, and 429.
  - A 429's `Retry-After` (seconds or HTTP date) sets `run_after`, capped at `JOB_RETRY_MAX_SECONDS`
    (15 min), so one bad header cannot park an alert for a day.
- **Not retryable:** other 4xx, and a webhook host that now resolves to a private address. The delivery is
  marked `failed` and the job `failed`.
- **Out of attempts:** after the 5th attempt the delivery is `failed` and the job `dead`.
- **Attempt record:** each attempt's outcome is saved on the row (`attempts`, `last_error`).
- **Injection safety:**
  - Discord messages send `allowed_mentions: {"parse": []}`, so a monitor named `@everyone` pings nobody.
  - Slack text escapes `& < >`, so `<!channel>` is inert.

## D16. Webhook URLs are secrets

- **API:** the URL is write-only; responses show `webhook_url_masked = "https://<host>/****"`.
- **Admin:** shows only the masked form and cannot edit or add channels.
- **Errors:** strings are built from status codes and exception class names, never `str(exc)` from httpx,
  which can include the URL.
- **Logging:** the `httpx` and `httpcore` loggers are pinned to WARNING, because httpx logs every request
  URL at INFO.
- **Tests:** `test_webhook_url_never_appears_in_logs_or_errors` captures all logs at DEBUG through a
  failing and then succeeding delivery and asserts the token appears nowhere. API responses are checked in
  `test_webhook_url_is_masked_in_every_response`.

## D17. Organizations and memberships

- **Slug:** taken from the request or `slugify(name)`. On a collision the API returns 400 and asks for
  another slug rather than appending `-2` silently. The slug is immutable because it will be the public
  status page URL.
- **Invites:** an invite needs an existing account (spec: "invite existing users by email"). An unknown
  email returns 400.
  - **Trade-off:** an org admin can learn whether an email has an account. Registration already reveals
    that ("already exists"), so nothing new leaks.
- **Who may do what:**
  - Only owners can grant, change or remove the owner role; an admin gets 403.
  - Admins manage admins and viewers.
- **Last owner:** the check locks the org row so concurrent demotions serialise. Demoting or removing the
  last owner returns 409 `last_owner`.
- **Order of checks:** 401 (anonymous), then 404 (not a member of this org, or an object outside it), then
  403 (role). This is the same principle as FixFlow D4 and D11.

## D18. Monitor limit returns 409 under an org row lock

- **Choice:** the 21st monitor returns 409 `monitor_limit_reached` with `details.limit`. The count and
  insert run with the org row locked, so two concurrent creates cannot both pass.
- **Reason:** the request is well formed and allowed. Only the org's current state blocks it (FixFlow D5).

## D19. Smaller API choices

- **Check results** use cursor pagination (newest first, no `COUNT(*)`, no deep OFFSET), because the table
  is high volume by design. Other lists use page-number pagination like FixFlow.
- **Boolean filters** (`ok`, `is_paused`) accept only `true` or `false`; anything else is 400. This is
  FixFlow D12: django-filter's BooleanFilter silently ignores typos.
- **Health:** ~~200 "degraded" for a stale worker~~. Superseded in Checkpoint 2 by D27: `/api/health/`
  returns 503 for a stale worker, and the web-only check moved to `/api/health/live/`.
- **Throttles:** login 10/min and refresh 10/min per IP; register 5/hour per IP; writes 60/min per user
  (unsafe methods only). All use the DB-backed shared cache, and `NUM_PROXIES=0` ignores
  `X-Forwarded-For` (FixFlow D24).
- **PUT** is disabled everywhere; PATCH is the only update verb.

## D20. Running locally on Windows

- **Worker:** uses threads, not processes (no fork on Windows). Threads are daemon threads, so a job
  stuck past the shutdown timeout cannot keep the process alive; its lease expires and the reaper
  requeues it.
- **Shutdown signals:** SIGINT (Ctrl+C), SIGTERM, and SIGBREAK (Ctrl+Break, which is what
  `os.kill(pid, CTRL_BREAK_EVENT)` delivers on Windows). The subprocess test uses that real signal.
- **Web server:** gunicorn does not run on Windows, so the local web process is `runserver`. gunicorn stays
  in requirements for Linux hosting (Phase 4).

## D21. seed_demo creates monitors without the create-time DNS lookup

- **Choice:** the targets are fixed, safe public URLs (example.com, httpbin.org). The command must work
  offline, and every check re-validates before connecting, so safety is unchanged.

## D22. Test strategy for the network

- **No real network in tests:** two autouse fixtures (`fake_dns` and respx with `assert_all_mocked=True`)
  make it impossible for a test to reach real DNS or HTTP. Tests declare host → IP mappings and mock the
  pinned-IP URL.
- **TLS-error test:** respx replaces a side effect's `__cause__`, which destroys the
  `httpx ← httpcore ← ssl.SSLError` chain, and in fact produced a cyclic chain. That cycle exposed a real
  infinite loop in `_is_tls_error`, which is now fixed. The TLS test therefore patches `safe_http.send` to
  raise an exception chain built the way httpx builds it.
- **Concurrency tests** use real threads against a transactional test DB, each with its own connection
  (`tests/concurrency.py`).

## D23. What the mutation checks prove, precisely

`scripts/mutation_check.py` applies each mutation, runs the relevant test files, and restores the code with
`git checkout`. All seven mutations were caught:

| Mutation | Test that fails |
|---|---|
| M1: no SKIP LOCKED on job claim | `test_claim_skips_rows_locked_by_another_transaction_without_waiting` |
| M1b: no lock at all on job claim | that test, plus `test_concurrent_claimers_never_get_the_same_job` |
| M2: no SKIP LOCKED on dispatch | `test_dispatch_skips_a_locked_monitor_without_waiting` |
| M2b: no lock at all on dispatch | that test, plus `test_concurrent_dispatchers_never_claim_the_same_monitor` |
| M3: no `locked_by` guard on finish | `test_stale_worker_cannot_finish_a_reclaimed_job` |
| M4a: no delivery row lock | `test_concurrent_runs_of_one_delivery_send_one_webhook` |
| M4b: no delivery uniqueness constraint | `test_database_refuses_a_duplicate_delivery` |

**Why M1 and M2 need their own tests:** removing only SKIP LOCKED does **not** produce double claims. Plain
`FOR UPDATE` makes the second claimer wait, then re-check the row, which by then is running or no longer
due. So the "never twice" tests still pass under M1 and M2. What SKIP LOCKED buys is that claimers never
wait for each other, and the no-waiting tests check exactly that. M1b and M2b remove the lock entirely, to
show that the double-claim tests do fail without it.

---

# Checkpoint 2

## D24. One total time budget per outbound request, checked against the job lease (review fix A1, A2)

- **Problem (architect review):** httpx timeouts apply per operation. A server that sends one response
  header byte every few seconds never trips a 5 s read timeout, so a check could run past
  `JOB_LEASE_SECONDS` (60). The reaper would then treat a live job as crashed, and the check would run
  twice.
- **Choice:** `safe_http.send(..., timeout=T)` treats `T` as the **total** budget for DNS, TCP connect,
  TLS handshake, writing the request and reading the response head. On expiry it raises
  `httpx.TimeoutException`, which a check records as `error="timeout"`.
- **How:**
  - A monotonic deadline is set when the request starts.
  - httpcore's network backend is wrapped (`_DeadlineBackend` / `_DeadlineStream`) so that every
    `connect_tcp`, `start_tls`, `read` and `write` gets `min(its own timeout, time left)`. Once no time
    is left it fails immediately. A drip-fed response therefore ends at the deadline, not after the
    hundredth fresh 1-byte read.
  - DNS cannot be given a timeout (`getaddrinfo` blocks), so the lookup runs in a small thread pool and
    the caller stops waiting at the deadline. Any DNS time is taken from the same budget.
  - Redirects are still never followed (D4), so a redirect chain costs exactly one request.
  - The response body is never read, so its size cost is zero bytes. httpcore bounds the size of the
    response head it buffers.
- **Alternatives:**
  - A watchdog thread that closes the socket. Closing a socket under a blocked `recv` is unreliable
    across platforms, and it is racy.
  - Running each request in a thread and abandoning it on timeout. That leaks threads and connections
    and doesn't stop the I/O.
  - Async httpx with `asyncio.timeout`. That would mean rewriting the worker around an event loop.
- **Trade-off:** httpx has no public hook for the network backend, so `_DeadlineTransport` replaces
  `HTTPTransport._pool` with an identical `ConnectionPool` on the deadline backend. That's a private
  attribute. httpx is pinned (0.28.1), and real-socket tests fail if the swap stops taking effect.
- **Tests** (`tests/test_deadline.py`, real sockets on 127.0.0.1 against misbehaving servers):
  - a drip-fed response is cut off at 1.0 s, with a control test showing plain httpx with the same
    timeout keeps reading;
  - a stalled TLS handshake is cut off;
  - slow DNS counts against the budget;
  - DNS time reduces the time left for the request;
  - a 302 loop costs one request;
  - a worst-case check job finishes well inside a 5 s lease.
  - Mutation M5, which restores per-read timeouts, makes the drip tests fail.
- **Lease relationship (A2):** each job's worst-case network time is now a known constant:
  - `run_check` ≤ `MONITOR_MAX_TIMEOUT_SECONDS` (10, the model's ceiling for `timeout_seconds`);
  - `deliver_alert` and `send_test_alert` ≤ `ALERT_TIMEOUT_SECONDS` (5).
  - Around that sit a few short DB statements. The delivery row lock is held during the POST, so that
    POST is bounded by the same budget.
- **Startup check:** a Django system check (`apps/core/checks.py`, which runs before `run_worker`,
  `runserver`, `migrate` and `check --deploy`) refuses to start when
  `JOB_LEASE_SECONDS < 3 × max(MONITOR_MAX_TIMEOUT_SECONDS, ALERT_TIMEOUT_SECONDS)`. With the defaults
  that is 60 ≥ 30.
  - The factor of 3 covers DB round trips, GC pauses and a loaded machine.
  - The check also errors if the default monitor timeout exceeds the ceiling, and warns when
    `WORKER_SHUTDOWN_TIMEOUT_SECONDS` is shorter than the longest job, because a graceful shutdown would
    then abandon in-flight jobs to the reaper.

## D25. Public status page

- **Endpoints:**
  - `GET /status/{slug}/` returns JSON.
  - `GET /status/{slug}` (no trailing slash, as the spec asks) returns server-rendered HTML.
  - Both are DRF views over **one** service function (`apps/statuspage/services.get_status_payload`),
    so they can never disagree.
  - Neither needs auth. Both return 404 for an unknown slug, and also when the owner sets
    `status_page_public=false`, so a private page is indistinguishable from a missing one.
  - Only owners can toggle the setting (`PATCH /api/orgs/{slug}/`). Admins get 403.
- **Privacy:** monitor names only, never URLs, which can be internal or carry tokens. No member data.
  Tests assert that neither the URL nor an "@" appears in either format.
- **Uptime:** `100 × ok / total` over CheckResults with `checked_at >= now - window`, rounded to 2
  decimals. A window with no checks is **null ("no data"), not 100%**.
  - All three windows plus `last_checked_at` for every monitor come from **one** aggregate query with
    conditional `COUNT … FILTER` and `MAX`, so no rows are loaded into Python.
  - Paused time produces no checks, so pausing neither helps nor hurts uptime.
- **Public status** is flap-protected: `down` only while an incident is open. One failed check still
  shows `up` (Monitor.last_status stays the raw last result, D13).
  - Overall state: `down` when every unpaused monitor is down, `degraded` when some are, and
    `operational` otherwise, including when nothing is active.
- **Queries:** a cache miss costs 4 queries:
  1. org lookup;
  2. monitors;
  3. the last 5 incidents per monitor (a sliced Prefetch, which Django runs as a window function);
  4. the uptime aggregate.

  A hit costs 0 with the in-memory cache, or 1 SELECT on the cache table with the production DB cache.
  The count is pinned in tests.
- **Cache:** the payload is cached per slug for `STATUS_PAGE_CACHE_SECONDS` (30; 0 disables it) in the
  existing DB cache, which is shared by all web processes.
  - **Invalidation:** on incident open/resolve, monitor create/update/delete/pause/resume, and org
    rename or `status_page_public` change.
  - Invalidation runs in `transaction.on_commit`, so a concurrent reader can't re-cache data from before
    the change.
  - **Accepted staleness:** plain checks (uptime, last checked) don't invalidate, so they can be up to
    30 s stale. Invalidating per check would churn the cache at the check rate.
  - **Remaining race:** a reader that started building before the commit can still store slightly
    stale data, for at most one TTL.
  - **Failure handling:** cache failures are best-effort. A failing `get`, `set` or `delete` logs a
    warning and the page is served uncached. Invalidation can never fail a check, an incident or an API
    write.
    - This was found by the worker-process test, where a missing cache table made a committed run_check
      job fail in its `on_commit` callback.
    - **Limit:** DRF's throttles use the same DB cache, so if the cache table is missing, throttled
      endpoints fail. `createcachetable` is therefore a deploy step, as in FixFlow.
  - Whether the cache helps at all is measured in docs/benchmarks.md (Part C).
- **Throttle:** scope `status_page`, 120/min per client IP by default (`THROTTLE_STATUS_PAGE_RATE`).
  The HTML and JSON views share the bucket. The client IP comes from DRF with `NUM_PROXIES` (FixFlow
  D27: verify the depth live).
- **HTML:** a Django template with no JS, `<meta http-equiv="refresh" content="60">`, and a viewport meta
  for mobile. It uses landmarks and headings and a table with `scope` headers, and every status has a
  text label as well as a colour. Dark mode follows `prefers-color-scheme`.

## D26. CheckResult retention: per monitor, hourly, bounded; no rollups

- **Choice:** raw check results are kept `CHECK_RESULT_RETENTION_DAYS` (30) days.
  `purge_check_results()` deletes older rows **monitor by monitor**, in batches of 1,000, at most 20,000
  rows per run. The scheduler runs it at most once per `CHECK_RESULT_PURGE_INTERVAL_SECONDS` (3600). The
  next-run time is shared through the DB cache.
  - **Backlog:** a run that hits the row bound reports itself incomplete, and the next tick (10 s later)
    continues. Deletion capacity during a backlog is about 120k rows/min, far above ingestion (about
    3.3k/min per worker, benchmarks C3).
  - **Cache outage:** if the cache is unavailable the purge runs every tick, which is correct, just more
    queries.
- **Why per monitor:** evidence from `scripts/explain_retention.py` on 6.18M rows.
  - A global `checked_at < cutoff` has no supporting index: a parallel seq scan took about 950 ms **even
    when nothing has expired**, which is the usual case.
  - A new B-tree on `checked_at` answers in 0.04 ms, but adds an index write to every check insert
    (42 MB on this data). I had just removed a redundant index for exactly that cost.
  - BRIN was not chosen by the planner on the seeded data. seed_bulk writes rows monitor by monitor, so
    the heap isn't in time order. That says nothing about BRIN on real append-ordered data, which
    remains an option.
  - Per-monitor range scans on the **existing** composite index cost 188 ms across 406 monitors when
    nothing has expired (0.46 ms each, mostly round trips). Hourly, that's negligible.
- **No rollup table:** retention (30 days) covers the longest uptime window (30 days), so the 30-day
  figure never needs data that was deleted. A system check (`statusping.E003`) refuses a retention below
  30 days.
  - Rollups would only be a **speed** optimisation for the uncached status page (benchmarks C2), and the
    30 s cache already covers that.

## D27. Health: 503 when the worker is dead; a separate liveness path for the web process

- **Choice (review D2):** `/api/health/` returns **503** `worker_unavailable` when the newest heartbeat is
  older than `WORKER_HEARTBEAT_STALE_SECONDS` (default raised from 30 to 60 s, which is 12 missed 5 s
  beats) or there is no heartbeat at all. It also returns 503 when the DB is down. The worker is the
  product, so an external uptime check on this path detects a dead worker.
- **Added:** `/api/health/live/` (web plus DB only, ignoring the worker) for the **web service's own**
  platform health check. Without it, a dead worker would make the platform restart a healthy web process
  or refuse to finish its deploys, which doesn't fix the worker and takes the API down too. Both paths are
  exempt from the HTTPS redirect.
- **Test change:** Checkpoint 1's `test_health_without_worker_is_degraded` (200) became
  `test_health_without_any_worker_is_503`. The requirement changed (D19 is superseded); this is not a
  weakened test.

## D28. One structured log line per job; secrets masked twice

- **Log line:** `run_job` logs exactly one line per job:
  `job id=42 kind=deliver_alert attempt=2/5 outcome=queued duration_ms=12 error="HTTP 503"`.
  - **Level:** INFO for succeeded or queued (a retry is normal); WARNING for failed, dead or lease_lost.
  - **Structured fields:** the same values ride on the record as `extra` (`job_id`, `job_kind`,
    `attempt`, `outcome`, `duration_ms`) for a JSON handler later.
  - **Tracebacks:** unexpected exceptions also log a traceback (WARNING). The worker's separate
    "attempt N" line was removed.
- **Masking at the source:** error text is passed through `mask_secrets()` before it is logged **or
  stored** in `Job.last_error`. Discord and Slack webhook paths become `…/api/webhooks/****` and
  `…/services/****`; host and scheme stay visible.
- **Masking at the sink:** the console handler has a `SecretMaskingFilter` that masks every message and
  traceback, including from library code. It rewrites the shared record, so every other handler sees the
  masked version too.
- **Tests:** an exception that quotes the webhook URL ends up masked in the job's `last_error`, the
  summary line and the formatted traceback (`tests/test_logging.py`). This comes on top of the existing
  httpx/httpcore WARNING pin (D16).

## D29. Hosting: Render (free web + Starter worker) with Neon Launch

- **Choice (Humayun, 2026-10-30):** option (a) in docs/deploy.md. Render runs the web service on the free
  plan, which sleeps after 15 min idle so the API wakes in about a minute, and the background worker on
  Starter (about $7/month; the price was to be confirmed on Render's pricing page). Neon Launch hosts the
  database with scale-to-zero disabled (about $19.70/month at 0.25 CU), for an estimated **~$27/month**.
- **Why not Neon Free:** the always-on worker keeps the database busy, so it never scales to zero. 0.25
  CU × 730 h = 182.5 CU-hours against the free 100, so it would be suspended after about 17 days.
- **Why not option (c):** monitoring would stop whenever the web service sleeps.
- **Why not option (b):** a free Oracle VM means owning patching and backups, plus idle-reclamation risk.
- **Health checks:** Render's web health check uses `/api/health/live/`, and an external check on
  `/api/health/` detects a dead worker (D27).
- **Demo logins:** public, as in FixFlow, with a demo-only password chosen for production (never the local
  `.env` one).
- **No real Discord webhook:** live alert verification uses the deliveries endpoint and the worker's
  structured log lines.

## D30. Database: Supabase Free (session pooler) instead of Neon Launch

- **Change (Humayun, 2026-10-29 session):** Supabase Free hosts Postgres. Render is unchanged (free web +
  Starter worker), so the estimate drops from ~$27 to **~$7/month**. This supersedes D29's database choice.
- **Checked on Supabase's pages before creating anything:**
  - Free has no compute-hour quota (Nano, $0/hour, "subject to change").
  - It pauses only after a week without "sufficient user database activity", which the worker's
    every-second polling rules out.
  - Details and links are in docs/deploy.md, "Supabase Free".
- **Connection: the Supavisor session pooler, port 5432.**
  - The direct connection is IPv6 without a paid add-on, and Render can't reach it.
  - Transaction mode (6543) doesn't keep session state between transactions.
  - Session mode keeps one server connection per client, so the queue's locks and SKIP LOCKED claims
    behave exactly as on a direct connection. No code changes.
- **New limits, accepted:**
  - The 500 MB database holds about 40–45 monitors at 60 s (~9 MB each, measured row sizes, 30-day
    retention).
  - The session pooler's client cap (Pool Size, reportedly ~15 on Nano; unofficial) must stay above our
    ~9 connections plus deploy overlap, so it is checked and raised to 20 if needed.

## D31. Hosting: option (c), one free Render web service with the worker inside it

- **Change (Humayun, 2026-10-29 session):** no paid Render worker. One free web service runs the API and
  also the job worker and scheduler as threads in its single gunicorn process (`EMBEDDED_WORKER=true`,
  2 job threads). Postgres stays on Supabase Free through the session pooler (D30). Cost: **$0/month**.
  This supersedes D29's Render layout.
- **Why it's correct with more than one copy:** during deploys two instances overlap. Claims use SKIP
  LOCKED, finishing is guarded by `locked_by`, and the scheduler takes an advisory lock. These are the
  same guarantees that make several `run_worker` processes safe (D1, D2), so no code changes are needed.
- **Trade-off, stated plainly everywhere (README, deploy.md, render.yaml):** monitoring pauses whenever
  the instance sleeps, restarts or redeploys. Missed intervals are not replayed.
- **Staying awake:**
  - Render spins a free service down after 15 min without inbound HTTP, so an external pinger
    (cron-job.org) requests `/api/health/live/` every 10 min.
  - 750 free instance hours per workspace cover one service for a full month (at most 744 h), and no
    second free service.
- **Terms risk:**
  - Render's AUP prohibits attempts to "bypass, circumvent, or defeat any access or usage restrictions".
    A keep-alive pinger can reasonably be read as that.
  - The free-tier docs neither allow nor forbid it explicitly.
  - Enforcement is at Render's discretion. Fallback: the $7 Starter worker (option a).
- **Egress budget (measured):**
  - Supabase Free allows 5 GB/month, and pooler traffic counts.
  - Idle polling with 2 threads at a 1 s poll measured 2.56 GB/month of server-to-client bytes (empty
    claim 419 B, heartbeat 380 B, idle tick 730 B; no TLS).
  - `WORKER_POLL_SECONDS=3` brings that to about 1.1 GB/month. The cost is up to 3 s of extra delay
    before a due job starts.
- **Alternatives rejected:**
  - Option (a) costs $7/month, which Humayun declined for now.
  - Option (b) means operating a VM.
  - Adding `LISTEN/NOTIFY` to avoid polling would be new code, and it needs a session-mode connection held
    open. It's only worth doing if egress proves too high.

# Resume bullets

Every number links to the place it was measured. All benchmarks ran on one laptop, so they describe
this code on that machine, not a production fleet.

- Built **StatusPing**, a multi-tenant uptime-monitoring API (Django, DRF, PostgreSQL), with a job queue
  written from scratch on Postgres instead of Celery and Redis.
  - Claims use `FOR UPDATE SKIP LOCKED`, alongside leases, a reaper, backoff and transactional enqueue.
  - One worker process sustained
    [~3,300 checks/min](benchmarks.md#run-1), with 2 processes scaling
    [1.8×](benchmarks.md#finding-the-limit).
  - Profiling found and fixed a scheduler cap of
    [600 checks/min (now 6,000)](benchmarks.md#dispatcher).
- Made alert delivery exactly-once at the receiver under retries.
  - Delivery rows are locked during the POST, and each (incident, channel, event) is unique.
  - With 30% / 60% injected failures (500s, 429s and timeouts),
    [99.75–100% / 91.75–92.25%](benchmarks.md#results) of 400 alerts per run were delivered, matching the
    1 − p⁵ retry model, with [0 duplicates](benchmarks.md#results).
- Hardened and measured the outbound and public paths.
  - SSRF protection checks every resolved IP and pins the connection to it.
  - One total time budget covers DNS, connect, TLS and the response
    ([D24](decisions.md#d24-one-total-time-budget-per-outbound-request-checked-against-the-job-lease-review-fix-a1-a2)).
  - Status-page uptime is a single SQL aggregate. Dropping a redundant index took it from
    [244 to 172 ms p50](benchmarks.md#index-experiments-all-beforeafter-numbers-recorded) on 345k checks,
    and the cache serves it in [~10 ms](benchmarks.md#index-experiments-all-beforeafter-numbers-recorded).
  - Verified by [461 tests at 97.8% coverage](https://github.com/MhBaigtw/statusping/actions) and
    [8 mutation checks](decisions.md#d23-what-the-mutation-checks-prove-precisely), and deployed and
    verified live ([HANDOFF](../HANDOFF.md)).

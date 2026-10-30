# Deploying StatusPing

StatusPing needs three things running all the time:

1. **A web process:** API, docs and the public status page.
2. **A worker process:** job threads plus the scheduler. **The worker is the product: if it sleeps, monitoring
   stops**, and nobody gets an alert.
3. **PostgreSQL:** data plus the job queue.

Research date: **2026-10-30**. Prices and limits change, so re-check the linked pages before paying for
anything.

## The finding that changes the plan: a free Neon database can't run all month

- Neon's Free plan gives **100 CU-hours per project per month**. Scale-to-zero after 5 idle minutes is
  "always enabled on Free" and can't be turned off. When the hours run out, compute is **suspended until
  the next billing period** ([Neon plans](https://neon.com/docs/introduction/plans),
  [pricing](https://neon.com/pricing)).
- StatusPing's worker queries the database every second (an idle job thread polls), writes a heartbeat
  every 5 s, and runs checks every minute. **The database is never idle for 5 minutes, so it never scales
  to zero.**
- At the smallest size (0.25 CU) that is 0.25 × 730 h = **182.5 CU-hours a month**. 100 CU-hours last
  about **400 hours, roughly 17 days**. After that the database is suspended and monitoring stops until
  the next month.
- **Paid Neon (Launch)** has no minimum fee: $0.106 per CU-hour and $0.35 per GB-month, and scale-to-zero
  can be disabled. 0.25 CU always on plus 1 GB comes to about **$19.70/month**.

So the cheapest-looking version of option (a), with a free Neon database, does **not** work for a full
month. The table below prices the variants honestly.

## Options compared

| | (a) Render web + Render worker + managed Postgres | (b) Oracle Cloud Always Free VM (everything on one VM) | (c) Render free web, worker inside it |
|---|---|---|---|
| **Monthly cost** | Worker **$7** (Starter) + web **$0** (free) + DB: Neon Launch **~$19.70**, so **~$27/month**. Neon Free only lasts about 17 days. | **$0** within Always Free limits. A card is needed for sign-up verification. | **$0** |
| **Setup effort** | Low: Blueprint (`render.yaml`, drafted) plus one DB URL | High: VM, firewall, Postgres, Caddy (TLS), systemd, backups, OS updates | Lowest: one service plus a DB URL |
| **What breaks** | Free web sleeps after 15 min idle, so the API and status page take about a minute to wake up. **Monitoring continues** (the worker is a separate paid service). | Oracle may **reclaim idle** Always Free VMs. Free A1 capacity is often unavailable in popular regions. You own patching, backups and recovery. | **Monitoring stops whenever the web service sleeps** (15 min without inbound traffic). Keeping it awake with an external pinger then makes the DB never idle, which brings back the Neon Free 17-day limit. Render may restart free services at any time. |
| **Dead-worker detection** | External check on `/api/health/` (503 when the heartbeat is > 60 s old). Render's own web health check uses `/api/health/live/`. Render restarts a crashed worker process. | systemd `Restart=always` plus an external check on `/api/health/` | `/api/health/` is 503 once the sleeping process stops beating, but the check itself wakes the service, so it only proves the worker is alive while someone is looking. |

**My recommendation:**
- **(a) with Neon Launch, about $27/month,** if a small bill is acceptable. It's the only option that is
  always on with almost no operations work.
- **(b)** if it must be free and you accept doing the server administration. Upgrade the Oracle account to
  Pay As You Go to avoid idle reclamation (see below).
- **(c)** only demonstrates the code. It isn't a monitoring service, and the README would say so.

The architect's estimate of "~$7/month for (a)" assumed a free database. Neon's free compute allowance
makes that a ~17-day setup.

---

## (a) Render: web (free) + background worker (Starter) + Postgres

**Facts:**
- **Free web services** spin down after **15 minutes** without inbound traffic, and spin-up takes about
  a minute. They get **750 free instance hours** per workspace per month. Render "might restart a Free web
  service at any time". There's no shell, and no persistent disk
  ([Render free instances](https://render.com/docs/free)).
- **Background workers have no Free plan.** The smallest is Starter (0.5 CPU, 512 MB)
  ([compute plans](https://render.com/docs/compute-plans)).
- **Worker price: $7/month.** Several 2026 sources quote that for Starter (Standard $25), for example
  [DeployHandbook](https://deployhandbook.com/pricing/render) and
  [CostBench](https://costbench.com/software/developer-tools/render/). I couldn't read Render's own
  JS-rendered [pricing page](https://render.com/pricing). **Confirm $7 there before choosing.**
- **Free Render Postgres** expires **30 days** after creation (with 14 days' grace to upgrade) and holds
  1 GB, so it's not usable here. A paid Render Postgres instance is an alternative to Neon Launch; I did
  not verify its price.
- **Measured fit:** our worker used about 77 MB in the e2e run, so 512 MB is plenty.

**Steps** (once the option is chosen):

1. **Database.** Neon: create a project in AWS us-west-2 (to match Render Oregon) on the **Launch** plan,
   and disable scale-to-zero. Copy the **direct** (non-pooled) connection string; `prod.py` adds
   `sslmode=require` if it's missing.
2. **Blueprint.** In Render: New → Blueprint → this repo. `render.yaml` creates:
   - the `statusping` web service (free), with the build step `bin/render-build.sh`: pip install,
     collectstatic, **migrate**, createcachetable, and optional `seed_demo`;
   - the `statusping-worker` service (Starter): `python manage.py run_worker --threads 4`;
   - an env group shared by both: settings module, Python 3.13, a generated `SECRET_KEY`, `NUM_PROXIES`,
     `DB_CONN_MAX_AGE`.
3. **Secrets.** Paste `DATABASE_URL` into both services, and `DEMO_PASSWORD` if demo logins are wanted.
   **Never commit either.**
4. **Health checks:**
   - Render's web health check: `/api/health/live/` (web plus DB).
   - An external uptime check, for example a free UptimeRobot monitor, on `/api/health/`, which returns
     503 if the worker is dead. StatusPing can't alert on its own worker being down, because that alert
     would come from the worker.
5. **Deploy ordering:** migrations run in the web build only. If the worker deploys first, it may hit an
   unmigrated table for a moment. It logs a `DatabaseError`, reconnects and recovers (tested path).

## (b) Oracle Cloud Always Free: one VM runs web, worker and Postgres

**Facts** ([Always Free resources](https://docs.oracle.com/en-us/iaas/Content/FreeTier/freetier_topic-Always_Free_Resources.htm)):

- **Ampere A1 (Arm):** 1,500 OCPU-hours and 9,000 GB-hours per month, which equals **2 OCPUs and 12 GB
  RAM** always on. There are also up to two **AMD E2.1.Micro** VMs (1/8 OCPU, 1 GB RAM).
- **Storage and traffic:** 200 GB total block and boot volume, and 10 TB/month outbound.
- **Idle reclamation:** Oracle may reclaim an Always Free instance if, over 7 days, its 95th-percentile CPU
  is below 20%, network below 20% and (A1 only) memory below 20%. StatusPing on a 2-OCPU / 12 GB VM would
  likely look "idle" by that definition.
  - Community reports and third-party guides say **upgrading to Pay As You Go** stops reclamation while
    Always Free resources stay free. Oracle's official page only says you *can* upgrade, so verify this.
    ([guide](https://oracommit.blogspot.com/2026/02/understanding-oci-always-free-compute.html),
    [discussion](https://lowendspirit.com/discussion/6792/oracle-cloud-how-to-avoid-inactivity-cancellation))
- **Card:** sign-up requires a **credit/debit card for verification**. Users report temporary holds
  (for example about $100 or €93) that are released later
  ([Oracle community](https://community.oracle.com/customerconnect/discussion/674112/upgrade-from-alwaysfree-to-payasyougo-to-avoid-deletion-with-an-initial-charge-of-93)).
- **Why Postgres on the VM, not Neon Free:** Neon Free has the 17-day limit described above.

**Setup outline** (files in `deploy/`):

1. Create an A1 VM (Ubuntu 24.04, 2 OCPU / 12 GB). Open 80 and 443 in the VCN security list **and** the
   VM's iptables.
2. `apt install postgresql python3.13-venv caddy`. Create the `statusping` role and database. Clone to
   `/opt/statusping`, create the venv, and run `pip install -r requirements.txt`.
3. `/etc/statusping/statusping.env` from `deploy/statusping.env.example` (chmod 600). Then run `migrate`,
   `createcachetable`, `collectstatic`, and optionally `seed_demo`.
4. Install `deploy/statusping-web.service` (gunicorn on 127.0.0.1:8000) and
   `deploy/statusping-worker.service` (SIGTERM gives a graceful stop, `Restart=always`). Then
   `systemctl enable --now` both.
5. `deploy/Caddyfile`: Caddy gets a Let's Encrypt certificate for a DNS name that points at the VM. A free
   `<ip>.sslip.io` name works without buying a domain. Set `NUM_PROXIES=1` and verify it (section 4).
6. **Backups:** nothing is backed up by default. Run a nightly `pg_dump` to block storage or Object Storage
   (Always Free includes some).
7. **External uptime check** on `/api/health/`.

## (c) Render free web service with the worker inside it

- **Config only, no code changes:** set `EMBEDDED_WORKER=true` on the web service and delete the worker
  service from `render.yaml`. `gunicorn.conf.py` then starts the job threads and scheduler inside the
  single gunicorn process, and stops them gracefully on shutdown. Tested in `tests/test_deploy.py`.
- **Stated plainly: while the service is asleep, nothing is checked and no alerts are sent.** It sleeps
  after 15 minutes without inbound requests. Background job traffic doesn't count, because only inbound
  HTTP keeps it awake.
- **Keeping it awake** with an external pinger just moves the problem: the database then never idles and
  Neon Free stops after about 17 days. It also leans on a free tier in a way it isn't meant to be used.

---

## Configuration reference (all options)

| Env var | Purpose |
|---|---|
| `DJANGO_SETTINGS_MODULE=config.settings.prod` | hardened settings (no default `SECRET_KEY`, `DEBUG` forced off, HSTS, secure cookies) |
| `SECRET_KEY`, `DATABASE_URL` | secrets: never commit or print them |
| `DB_SSLMODE` | defaults to `require`. Set `disable` only for a Postgres on the same host |
| `DB_CONN_MAX_AGE` | web connection reuse in seconds (default 60) with health checks |
| `NUM_PROXIES` | proxies in front of the app, for the client IP used by the login and status-page throttles. **Measure it live** (section 4) |
| `ALLOWED_HOSTS`, `CSRF_TRUSTED_ORIGINS` | extra hosts. On Render the `*.onrender.com` host is added automatically |
| `WEB_CONCURRENCY`, `PORT` | gunicorn processes and port |
| `EMBEDDED_WORKER`, `WORKER_THREADS` | option (c) only |
| `DEMO_PASSWORD` | optional: seeds demo users at build time |

- **Start commands:** web `gunicorn config.wsgi:application -c gunicorn.conf.py`; worker
  `python manage.py run_worker --threads 4`.
- **Migrations:** `bin/render-build.sh` (option a or c) or step 3 (option b).

## 4. Verifying the proxy depth (do this live; it's FixFlow's lesson, D27 there)

DRF takes the client IP from `X-Forwarded-For` at depth `NUM_PROXIES`. A wrong value either lets anyone
dodge the per-IP throttles by forging the header, or throttles everyone together.

To verify, after deploying, run each command from your own machine:

1. Send 11 failed logins with **forged, different** `X-Forwarded-For` values. The 11th must return
   **429**; if it doesn't, the value is too low.
2. Then confirm that a request from another network isn't throttled; if it is, the value is too high.

FixFlow measured **3** on Render (client, Cloudflare edge, Render internal). On Oracle with Caddy it
should be **1**.

## Live verification checklist (Part F)

- `/api/health/` returns 200 with `worker.last_seen_seconds_ago` < 10, and `/api/health/live/` returns 200.
- `/api/docs/` returns 200, and `/status/<demo-slug>` renders.
- A monitor on a real public URL gets a check about every interval (look at `checks/` timestamps).
- A monitor on `https://httpbin.org/status/500` opens an incident after 3 checks and delivers the alert
  (to a real Discord webhook if one was set up; otherwise the delivery log).
- Forged `X-Forwarded-For` logins hit 429 (section 4).

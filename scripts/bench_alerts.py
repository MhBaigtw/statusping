# ruff: noqa: E501  (long report lines read better unwrapped)
"""Alert delivery under injected failures (docs/benchmarks.md, C4).

REAL HTTP + IN-PROCESS WORKER: a fake webhook receiver on 127.0.0.1 fails a given share
of requests, split evenly between HTTP 500, timeouts (it sleeps longer than the client's
budget, then answers 500) and HTTP 429 with Retry-After: 1. The real worker code
(apps.jobs.worker.Worker, 8 threads, no scheduler) runs in this process against the dev
database; real deliver_alert jobs POST through safe_http.

Backoff is SHORTENED for the run via settings (base 0.2 s, cap 2 s; webhook budget 0.5 s)
so 5 attempts take seconds, not minutes. Max attempts stays at the production value (5).

The receiver counts every request it ACCEPTED (2xx) per (channel path, message body).
Anything accepted twice is a duplicate. A timeout here never counts as accepted: the
receiver answers 500 after sleeping, so the "receiver processed it but the response was
lost" case (D14, the only allowed duplicate) is not triggered, as the brief asks.

    .\\.venv\\Scripts\\python.exe scripts\\bench_alerts.py --incidents 200 --rates 0.3,0.6
"""

import argparse
import json
import os
import random
import sys
import threading
import time
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.dev")
os.environ["DEBUG"] = "True"
os.environ["SSRF_ALLOW_LOOPBACK"] = "True"

import django  # noqa: E402

django.setup()

from django.db import transaction  # noqa: E402
from django.test import override_settings  # noqa: E402
from django.utils import timezone  # noqa: E402

from apps.alerts.models import AlertChannel, AlertDelivery  # noqa: E402
from apps.alerts.services import queue_alerts  # noqa: E402
from apps.jobs.models import Job  # noqa: E402
from apps.jobs.worker import Worker  # noqa: E402
from apps.monitors.models import Incident, Monitor  # noqa: E402
from apps.organizations.models import Organization  # noqa: E402

SLUG = "bench-alerts"
CLIENT_BUDGET = 0.5
TIMEOUT_SLEEP = 0.8


class FlakyReceiver:
    def __init__(self, failure_rate, seed):
        self.failure_rate = failure_rate
        self.rng = random.Random(seed)
        self.lock = threading.Lock()
        self.accepted = Counter()  # (path, body) -> times accepted with 2xx
        self.outcomes = Counter()  # what the receiver answered
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):  # noqa: N802
                body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                with owner.lock:
                    roll = owner.rng.random()
                    kind = owner.rng.choice(["500", "timeout", "429"])
                if roll >= owner.failure_rate:
                    with owner.lock:
                        owner.accepted[(self.path, json.loads(body)["content"])] += 1
                        owner.outcomes["204 accepted"] += 1
                    self.send_response(204)
                    self.end_headers()
                    return
                with owner.lock:
                    owner.outcomes[kind] += 1
                if kind == "timeout":
                    time.sleep(TIMEOUT_SLEEP)
                    code, headers = 500, {}
                elif kind == "429":
                    code, headers = 429, {"Retry-After": "1"}
                else:
                    code, headers = 500, {}
                try:
                    self.send_response(code)
                    for name, value in headers.items():
                        self.send_header(name, value)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                except OSError:
                    pass  # the client gave up (timeout case)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


def run(rate, incidents, seed):
    Organization.objects.filter(slug=SLUG).delete()
    receiver = FlakyReceiver(rate, seed)
    org = Organization.objects.create(name="Bench alerts", slug=SLUG)
    for name in ("a", "b"):
        AlertChannel.objects.create(
            organization=org,
            kind="discord",
            name=f"channel {name}",
            webhook_url=f"http://127.0.0.1:{receiver.port}/hook/{name}",
        )
    far = timezone.now() + timezone.timedelta(days=365)
    with transaction.atomic():
        for n in range(incidents):
            monitor = Monitor.objects.create(
                organization=org,
                name=f"Service {n:03d}",
                url="https://example.com/",
                next_check_at=far,
            )
            incident = Incident.objects.create(
                monitor=monitor, started_at=timezone.now(), cause="timeout"
            )
            queue_alerts(incident, AlertDelivery.Event.OPENED)
    deliveries = AlertDelivery.objects.filter(channel__organization=org)
    total = deliveries.count()
    worker = Worker(threads=8, scheduler=False)
    started = time.monotonic()
    worker.start()
    try:
        while Job.objects.filter(
            kind="deliver_alert", status__in=["queued", "running"],
            payload__delivery_id__in=list(deliveries.values_list("pk", flat=True)),
        ).exists():  # fmt: skip
            time.sleep(0.5)
    finally:
        worker.stop()
        worker.join(30)
    elapsed = time.monotonic() - started
    receiver.close()

    sent = deliveries.filter(status="sent").count()
    failed = deliveries.filter(status="failed").count()
    pending = deliveries.filter(status="pending").count()
    attempts = Counter(deliveries.filter(status="sent").values_list("attempts", flat=True))
    failed_attempts = Counter(deliveries.filter(status="failed").values_list("attempts", flat=True))
    duplicates = sum(count - 1 for count in receiver.accepted.values() if count > 1)
    accepted_unique = len(receiver.accepted)
    jobs = Counter(
        Job.objects.filter(kind="deliver_alert", payload__delivery_id__in=list(deliveries.values_list("pk", flat=True)))
        .values_list("status", flat=True)
    )  # fmt: skip
    Organization.objects.filter(slug=SLUG).delete()
    expected = 100 * (1 - rate**5)
    return {
        "rate": rate,
        "total": total,
        "sent": sent,
        "failed": failed,
        "pending": pending,
        "delivered_pct": 100 * sent / total,
        "expected_pct": expected,
        "attempts": dict(sorted(attempts.items())),
        "failed_attempts": dict(sorted(failed_attempts.items())),
        "receiver": dict(receiver.outcomes),
        "accepted_unique": accepted_unique,
        "duplicates": duplicates,
        "jobs": dict(jobs),
        "seconds": elapsed,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--incidents", type=int, default=200)
    parser.add_argument("--rates", default="0.3,0.6")
    parser.add_argument("--seed", type=int, default=7)
    args = parser.parse_args()
    overrides = {
        "JOB_RETRY_BASE_SECONDS": 0.2,
        "JOB_RETRY_MAX_SECONDS": 2,
        "ALERT_TIMEOUT_SECONDS": CLIENT_BUDGET,
        "WORKER_POLL_SECONDS": 0.05,
        "WORKER_HEARTBEAT_SECONDS": 1,
    }
    print(f"settings for this run: {overrides}; ALERT_MAX_ATTEMPTS stays 5; worker 8 threads")
    with override_settings(**overrides):
        for rate in (float(r) for r in args.rates.split(",")):
            r = run(rate, args.incidents, args.seed)
            print(f"\n== failure rate {r['rate']:.0%}: {r['total']} deliveries "
                  f"({args.incidents} incidents x 2 channels) in {r['seconds']:.0f} s")  # fmt: skip
            print(f"   delivered: {r['sent']} ({r['delivered_pct']:.2f}%; "
                  f"independent-failure model 1-p^5 predicts {r['expected_pct']:.2f}%)")  # fmt: skip
            print(f"   failed after 5 attempts: {r['failed']}  still pending: {r['pending']}")
            print(f"   attempts needed by delivered alerts: {r['attempts']}")
            print(f"   attempts of failed alerts: {r['failed_attempts']}")
            print(f"   receiver answered: {r['receiver']}")
            print(f"   receiver accepted {r['accepted_unique']} distinct messages; "
                  f"DUPLICATES: {r['duplicates']}")  # fmt: skip
            print(f"   job statuses: {r['jobs']}")


if __name__ == "__main__":
    main()

"""Bulk benchmark data: orgs, monitors, 30 days of CheckResults and matching incidents.

DEBUG only (or --force). Deterministic for a given --seed. Every row is tagged by the
seed: orgs are `bulk<seed>-NN`, so re-running with the same seed skips existing orgs,
and --reset deletes them first.

Distributions (documented in docs/benchmarks.md):
- check interval per monitor: 60 s (25%), 300 s (50%), 900 s (25%)
- outages per monitor over the period: 0-4 (weights 30/30/20/12/8), each lasting
  5-180 minutes. Checks inside an outage fail (alternating unexpected_status / timeout).
  Outages long enough to cross a failure threshold of 3 become resolved Incidents.
- outside outages, ~0.5% of checks fail at random (isolated flaps, no incident)
- response time of successful checks: 40-240 ms
Rows are generated inside Postgres with generate_series, so nothing big passes
through Python.
"""

import random
from datetime import timedelta

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import connection, transaction
from django.utils import timezone

from apps.monitors.models import Incident, Monitor
from apps.organizations.models import Organization

INTERVALS = [(60, 25), (300, 50), (900, 25)]
OUTAGE_COUNTS = [(0, 30), (1, 30), (2, 20), (3, 12), (4, 8)]
FLAP_RATE = 0.005
THRESHOLD = 3

INSERT_CHECKS = """
INSERT INTO monitors_checkresult (monitor_id, checked_at, ok, status_code, response_ms, error)
SELECT %(monitor)s, t,
       NOT (down OR flap),
       CASE WHEN down OR flap THEN (CASE WHEN n %% 2 = 0 THEN 500 END) ELSE 200 END,
       CASE WHEN down OR flap THEN NULL ELSE (40 + random() * 200)::int END,
       CASE WHEN NOT (down OR flap) THEN ''
            WHEN n %% 2 = 0 THEN 'unexpected_status' ELSE 'timeout' END
FROM (
    SELECT t, row_number() OVER () AS n,
           COALESCE(t <@ %(outages)s::tstzmultirange, false) AS down,
           random() < %(flap)s AS flap
    FROM generate_series(%(start)s::timestamptz, %(end)s::timestamptz,
                         make_interval(secs => %(interval)s)) AS t
) s
"""


def weighted(rng, options):
    return rng.choices([v for v, _ in options], weights=[w for _, w in options])[0]


class Command(BaseCommand):
    help = "Create bulk benchmark data (DEBUG only). See the module docstring."

    def add_arguments(self, parser):
        parser.add_argument("--orgs", type=int, default=20)
        parser.add_argument("--monitors", type=int, default=20, help="per org")
        parser.add_argument("--days", type=int, default=30)
        parser.add_argument("--seed", type=int, default=1)
        parser.add_argument("--reset", action="store_true", help="delete this seed's data first")
        parser.add_argument("--force", action="store_true", help="allow without DEBUG")

    def handle(self, *args, orgs, monitors, days, seed, reset, force, **options):
        if not (settings.DEBUG or force):
            raise CommandError("seed_bulk refuses to run without DEBUG (or pass --force).")
        prefix = f"bulk{seed}-"
        if reset:
            deleted = Organization.objects.filter(slug__startswith=prefix).delete()[0]
            self.stdout.write(f"deleted {deleted} rows for seed {seed}")
        rng = random.Random(seed)
        end = timezone.now().replace(microsecond=0)
        start = end - timedelta(days=days)
        with connection.cursor() as cursor:
            cursor.execute("SELECT setseed(%s)", [(seed % 1000) / 1000])
        created_rows = created_incidents = 0
        for o in range(1, orgs + 1):
            slug = f"{prefix}{o:02d}"
            if Organization.objects.filter(slug=slug).exists():
                continue
            with transaction.atomic():
                org = Organization.objects.create(name=f"Bulk {seed} Org {o:02d}", slug=slug)
                for m in range(1, monitors + 1):
                    rows, incidents = self._monitor(rng, org, m, start, end)
                    created_rows += rows
                    created_incidents += incidents
            self.stdout.write(f"{slug}: {monitors} monitors")
        self.stdout.write(
            self.style.SUCCESS(
                f"created {created_rows} check results and {created_incidents} incidents "
                f"(seed {seed}, {days} days)"
            )
        )

    def _monitor(self, rng, org, number, start, end):
        interval = weighted(rng, INTERVALS)
        monitor = Monitor.objects.create(
            organization=org,
            name=f"Service {number:02d}",
            url=f"https://example.com/bulk/{org.slug}/{number}",
            interval_seconds=interval,
            next_check_at=end + timedelta(days=365),  # never due: benchmarks must not check it
            last_status=Monitor.Status.UP,
            consecutive_successes=5,
        )
        outages = []
        for _ in range(weighted(rng, OUTAGE_COUNTS)):
            length = timedelta(minutes=rng.randint(5, 180))
            begin = start + timedelta(
                seconds=rng.uniform(0, (end - start - length).total_seconds())
            )
            outages.append((begin, begin + length))
        outages.sort()
        ranges = "{" + ",".join(f"[{a.isoformat()},{b.isoformat()})" for a, b in outages) + "}"
        with connection.cursor() as cursor:
            cursor.execute(
                INSERT_CHECKS,
                {
                    "monitor": monitor.pk,
                    "outages": ranges,
                    "flap": FLAP_RATE,
                    "start": start,
                    "end": end,
                    "interval": interval,
                },
            )
            rows = cursor.rowcount
        incidents = [
            Incident(monitor=monitor, started_at=a, resolved_at=b, cause="unexpected_status")
            for a, b in outages
            if (b - a).total_seconds() >= THRESHOLD * interval
        ]
        Incident.objects.bulk_create(incidents)
        return rows, len(incidents)

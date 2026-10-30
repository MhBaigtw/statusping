"""Benchmark GET /status/{slug}/ uncached vs cached (docs/benchmarks.md, C2).

IN-PROCESS: requests go through Django's test client (full middleware, URL routing,
DRF, serialization) against the dev Postgres database, but not over a socket. The
cached mode uses the real DatabaseCache backend, so a hit still costs one SQL query.

    .\\.venv\\Scripts\\python.exe manage.py seed_bulk --seed 1
    .\\.venv\\Scripts\\python.exe scripts\\bench_status.py --slug bulk1-01
"""

import argparse
import os
import platform
import statistics
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.dev")

import django  # noqa: E402

django.setup()

from django.conf import settings  # noqa: E402
from django.core.cache import cache  # noqa: E402
from django.db import connection  # noqa: E402
from django.test import Client, override_settings  # noqa: E402
from django.test.utils import CaptureQueriesContext  # noqa: E402
from rest_framework.throttling import SimpleRateThrottle  # noqa: E402

from apps.monitors.models import CheckResult  # noqa: E402
from apps.statuspage.services import cache_key  # noqa: E402


def machine():
    cpu = subprocess.run(
        ["powershell", "-NoProfile", "-Command", "(Get-CimInstance Win32_Processor).Name"],
        capture_output=True,
        text=True,
    ).stdout.strip()
    ram = subprocess.run(
        [
            "powershell",
            "-NoProfile",
            "-Command",
            "[math]::Round((Get-CimInstance Win32_ComputerSystem).TotalPhysicalMemory/1GB,1)",
        ],
        capture_output=True,
        text=True,
    ).stdout.strip()
    with connection.cursor() as cursor:
        cursor.execute("SHOW server_version")
        pg = cursor.fetchone()[0]
    commit = subprocess.run(
        ["git", "rev-parse", "--short", "HEAD"], cwd=BASE, capture_output=True, text=True
    ).stdout.strip()
    return {
        "date": datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC"),
        "commit": commit,
        "machine": f"{cpu}, {ram} GB RAM, {platform.system()} {platform.release()}",
        "versions": f"Python {platform.python_version()}, Django {django.get_version()}, "
        f"PostgreSQL {pg}",
    }


def percentile(values, p):
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, round(p / 100 * (len(ordered) - 1)))]


def measure(client, url, runs, warmup, before_each=None):
    for _ in range(warmup):
        if before_each:
            before_each()
        assert client.get(url).status_code == 200
    timings = []
    for _ in range(runs):
        if before_each:
            before_each()
        started = time.perf_counter()
        response = client.get(url)
        timings.append((time.perf_counter() - started) * 1000)
        assert response.status_code == 200
    with CaptureQueriesContext(connection) as queries:
        if before_each:
            before_each()
        client.get(url)
    return {
        "p50": statistics.median(timings),
        "p95": percentile(timings, 95),
        "max": max(timings),
        "queries": len(queries),
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--slug", default="bulk1-01")
    parser.add_argument("--runs", type=int, default=200)
    parser.add_argument("--warmup", type=int, default=20)
    args = parser.parse_args()

    SimpleRateThrottle.THROTTLE_RATES = {**settings.REST_FRAMEWORK["DEFAULT_THROTTLE_RATES"],
                                         "status_page": "1000000/min"}  # fmt: skip
    url = f"/status/{args.slug}/"
    rows_org = CheckResult.objects.filter(monitor__organization__slug=args.slug).count()
    rows_all = CheckResult.objects.count()
    info = machine()
    with override_settings(DEBUG=False, ALLOWED_HOSTS=["testserver"]):
        client = Client()
        with override_settings(STATUS_PAGE_CACHE_SECONDS=0):
            uncached = measure(client, url, args.runs, args.warmup)
        with override_settings(STATUS_PAGE_CACHE_SECONDS=3600):
            cache.delete(cache_key(args.slug))
            cached = measure(client, url, args.runs, args.warmup)
            # A miss with the cache enabled: build + store (what every 30 s costs).
            miss = measure(
                client, url, max(20, args.runs // 4), 5, lambda: cache.delete(cache_key(args.slug))
            )
    print(f"date: {info['date']}  commit: {info['commit']}")
    print(f"machine: {info['machine']}")
    print(f"versions: {info['versions']}")
    print(f"dataset: {rows_all:,} check results in total; {rows_org:,} for {args.slug}")
    print(f"runs: {args.runs} timed after {args.warmup} warmup, in-process test client")
    print("| mode | p50 ms | p95 ms | max ms | queries |")
    print("|---|---|---|---|---|")
    for label, r in (
        ("uncached (cache off)", uncached),
        ("cache miss (build + store)", miss),
        ("cache hit", cached),
    ):
        print(f"| {label} | {r['p50']:.1f} | {r['p95']:.1f} | {r['max']:.1f} | {r['queries']} |")


if __name__ == "__main__":
    main()

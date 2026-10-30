"""Worker throughput benchmark (docs/benchmarks.md, C3).

REAL PROCESSES + REAL HTTP: a fake target server (127.0.0.1, instant 200s) runs in this
process; the worker is the real `manage.py run_worker --no-scheduler` in its own
process; Postgres is the native dev database. For each --threads value:

1. N run_check jobs are inserted at once (a burst: every monitor due at the same time);
2. we wait until all have finished and read created_at/finished_at from the Job rows;
3. CPU seconds of the worker process tree and of all postgres.exe processes are
   sampled before and after, so the bottleneck is visible (100% = one full core).

Then the same for N no-op jobs (the raw queue, no HTTP). Queries per job are counted
in-process for one job of each kind. The data is cleaned up afterwards.

    .\\.venv\\Scripts\\python.exe scripts\\bench_worker.py --checks 1000 --noops 3000
"""

import argparse
import os
import signal
import statistics
import subprocess
import sys
import time
from datetime import timedelta
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.dev")
os.environ["DEBUG"] = "True"
os.environ["SSRF_ALLOW_LOOPBACK"] = "True"

import django  # noqa: E402

django.setup()

from django.db import connection  # noqa: E402
from django.test.utils import CaptureQueriesContext  # noqa: E402
from django.utils import timezone  # noqa: E402

from apps.jobs import queue  # noqa: E402
from apps.jobs.models import Job, WorkerHeartbeat  # noqa: E402
from apps.monitors.models import CheckResult, Monitor  # noqa: E402
from apps.organizations.models import Organization  # noqa: E402
from tests.fake_servers import FakeTarget  # noqa: E402

SLUG = "bench-worker"


def cpu_seconds(where):
    """Total CPU seconds (user + kernel) of matching processes, via CIM."""
    script = (
        f"Get-CimInstance Win32_Process | Where-Object {{ {where} }} | "
        "ForEach-Object { $_.KernelModeTime + $_.UserModeTime } | Measure-Object -Sum | "
        "ForEach-Object { $_.Sum }"
    )
    out = subprocess.run(
        ["powershell", "-NoProfile", "-Command", script], capture_output=True, text=True
    ).stdout.strip()
    return int(out or 0) / 1e7  # 100 ns units


def worker_cpu(pid):
    return cpu_seconds(f"$_.ProcessId -eq {pid} -or $_.ParentProcessId -eq {pid}")


def postgres_cpu():
    return cpu_seconds("$_.Name -eq 'postgres.exe'")


def percentile(values, p):
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, round(p / 100 * (len(ordered) - 1)))]


def start_worker(threads, pgoptions=None):
    env = {**os.environ, "PYTHONUNBUFFERED": "1"}
    if pgoptions:
        # Diagnostic only: libpq applies PGOPTIONS to the worker's DB sessions.
        env["PGOPTIONS"] = pgoptions
    proc = subprocess.Popen(
        [sys.executable, "manage.py", "run_worker", "--threads", str(threads), "--no-scheduler"],
        cwd=BASE,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
    )
    deadline = time.monotonic() + 30
    while not WorkerHeartbeat.objects.exists():
        if time.monotonic() > deadline:
            raise SystemExit("worker did not start")
        time.sleep(0.2)
    time.sleep(1.5)  # let every job thread reach its idle poll
    return proc


def stop_worker(proc):
    os.kill(proc.pid, signal.CTRL_BREAK_EVENT)
    proc.wait(60)


def run(kind, payloads, threads, pgoptions=None, processes=1):
    WorkerHeartbeat.objects.all().delete()
    procs = [start_worker(threads, pgoptions) for _ in range(processes)]
    try:
        cpu_w0, cpu_p0 = sum(worker_cpu(p.pid) for p in procs), postgres_cpu()
        cpu_b0 = time.process_time()  # this process: the fake target server
        now = timezone.now()
        jobs = Job.objects.bulk_create(
            Job(kind=kind, payload=p, run_after=now, max_attempts=1) for p in payloads
        )
        ids = [j.pk for j in jobs]
        started = time.monotonic()
        while Job.objects.filter(pk__in=ids, status__in=["queued", "running"]).exists():
            time.sleep(0.25)
        wall = time.monotonic() - started
        cpu_w1, cpu_p1 = sum(worker_cpu(p.pid) for p in procs), postgres_cpu()
        cpu_b1 = time.process_time()
    finally:
        for proc in procs:
            stop_worker(proc)
    rows = list(Job.objects.filter(pk__in=ids).values_list("status", "created_at", "finished_at"))
    statuses = {s for s, _, _ in rows}
    first = min(c for _, c, _ in rows)
    last = max(f for _, _, f in rows)
    span = (last - first).total_seconds()
    latencies = [(f - c).total_seconds() * 1000 for _, c, f in rows]
    Job.objects.filter(pk__in=ids).delete()
    return {
        "threads": f"{processes}x{threads}" if processes > 1 else threads,
        "n": len(ids),
        "statuses": statuses,
        "per_minute": len(ids) / span * 60,
        "per_second": len(ids) / span,
        "lat_p50": statistics.median(latencies),
        "lat_p95": percentile(latencies, 95),
        "worker_cpu": (cpu_w1 - cpu_w0) / wall * 100,
        "postgres_cpu": (cpu_p1 - cpu_p0) / wall * 100,
        "bench_cpu": (cpu_b1 - cpu_b0) / wall * 100,
    }


def queries_per_job(kind, payload):
    job = queue.enqueue(kind, payload, max_attempts=1)
    with CaptureQueriesContext(connection) as captured:
        [claimed] = queue.claim("bench")
        queue.run_job(claimed, "bench")
    Job.objects.filter(pk=job.pk).delete()
    return len(captured)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--checks", type=int, default=1000)
    parser.add_argument("--noops", type=int, default=3000)
    parser.add_argument("--threads", default="1,4,8")
    parser.add_argument("--processes", type=int, default=1, help="worker processes per run")
    parser.add_argument(
        "--pgoptions",
        default=None,
        help='DIAGNOSTIC ONLY, e.g. "-c synchronous_commit=off" to test commit latency',
    )
    args = parser.parse_args()
    thread_counts = [int(t) for t in args.threads.split(",")]

    Organization.objects.filter(slug=SLUG).delete()
    org = Organization.objects.create(name="Bench worker", slug=SLUG)
    results = {"run_check": [], "noop": []}
    with FakeTarget(status=200) as target:
        far = timezone.now() + timedelta(days=365)
        monitors = Monitor.objects.bulk_create(
            Monitor(
                organization=org,
                name=f"bench {n}",
                url=target.url(f"/m/{n}"),
                next_check_at=far,
                interval_seconds=3600,
            )
            for n in range(args.checks)
        )
        # Dispatcher speed: make every bench monitor due and time the scheduler's dispatch
        # loop (batches of DISPATCH_BATCH_SIZE, each its own SKIP LOCKED transaction).
        from apps.monitors.services import dispatch_due_checks

        Monitor.objects.filter(organization=org).update(next_check_at=timezone.now())
        started = time.perf_counter()
        dispatched = 0
        while batch := dispatch_due_checks():
            dispatched += len(batch)
        dispatch_seconds = time.perf_counter() - started
        Job.objects.filter(
            kind="run_check", payload__monitor_id__in=[m.pk for m in monitors]
        ).delete()
        Monitor.objects.filter(organization=org).update(next_check_at=far)
        print(
            f"dispatch: {dispatched} due monitors claimed + enqueued in {dispatch_seconds:.2f} s "
            f"({dispatched / dispatch_seconds:.0f}/s, batches of 100)"
        )
        q_check = queries_per_job("run_check", {"monitor_id": monitors[0].pk})
        q_noop = queries_per_job("noop", {})
        for threads in thread_counts:
            results["run_check"].append(
                run(
                    "run_check",
                    [{"monitor_id": m.pk} for m in monitors],
                    threads,
                    args.pgoptions,
                    args.processes,
                )
            )
            CheckResult.objects.filter(monitor__organization=org).delete()
        for threads in thread_counts:
            results["noop"].append(
                run("noop", [{}] * args.noops, threads, args.pgoptions, args.processes)
            )
        hits = target.hits
    Organization.objects.filter(slug=SLUG).delete()

    print(f"queries per job (claim + run + finish): run_check={q_check}, noop={q_noop}")
    print(f"fake target requests served: {hits}; worker PGOPTIONS: {args.pgoptions or '(none)'}")
    print("| job | threads | jobs | jobs/s | per minute | latency p50 ms | latency p95 ms | "
          "worker CPU % | postgres CPU % | bench+target CPU % | statuses |")  # fmt: skip
    print("|---|---|---|---|---|---|---|---|---|---|---|")
    for kind, rows in results.items():
        for r in rows:
            print(
                f"| {kind} | {r['threads']} | {r['n']} | {r['per_second']:.0f} | "
                f"{r['per_minute']:.0f} | {r['lat_p50']:.0f} | {r['lat_p95']:.0f} | "
                f"{r['worker_cpu']:.0f} | {r['postgres_cpu']:.0f} | {r['bench_cpu']:.0f} | "
                f"{','.join(r['statuses'])} |"
            )


if __name__ == "__main__":
    main()

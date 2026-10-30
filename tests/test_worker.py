"""The worker: threads processing jobs end to end, heartbeat, scheduler, graceful shutdown."""

import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
from django.core.management import call_command
from django.db import connection
from django.test import override_settings
from django.utils import timezone

from apps.alerts.models import AlertDelivery
from apps.jobs import queue
from apps.jobs.models import Job, WorkerHeartbeat
from apps.jobs.worker import Worker, scheduler_tick
from apps.monitors.models import CheckResult, Incident, Monitor

from .concurrency import HoldLock
from .factories import DISCORD_IP, PUBLIC_IP, AlertChannelFactory, MonitorFactory
from .fake_servers import FakeTarget, FakeWebhook

pytestmark = pytest.mark.django_db(transaction=True)

DB_CACHE = {
    "default": {
        "BACKEND": "django.core.cache.backends.db.DatabaseCache",
        "LOCATION": "django_cache",
    }
}
BASE_DIR = Path(__file__).resolve().parent.parent
STARTED = threading.Event()


def slow_handler(job):
    STARTED.set()
    time.sleep(job.payload["seconds"])


@pytest.fixture(autouse=True)
def fast_worker(settings, monkeypatch):
    settings.WORKER_POLL_SECONDS = 0.05
    settings.WORKER_HEARTBEAT_SECONDS = 0.2
    settings.SCHEDULER_INTERVAL_SECONDS = 0.2
    settings.WORKER_SHUTDOWN_TIMEOUT_SECONDS = 10
    monkeypatch.setitem(queue.HANDLERS, "slow", "tests.test_worker.slow_handler")
    STARTED.clear()


def wait_for(condition, timeout=15, message="condition"):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return
        time.sleep(0.05)
    raise AssertionError(f"timed out waiting for {message}")


class Running:
    """Run worker.run() in a background thread; stop it on exit."""

    def __init__(self, worker):
        self.worker = worker
        self.clean = None
        self.thread = threading.Thread(target=self._run, daemon=True)

    def _run(self):
        try:
            self.clean = self.worker.run(install_signals=False)
        finally:
            connection.close()

    def __enter__(self):
        self.thread.start()
        return self

    def stop(self):
        self.worker.stop()
        self.thread.join(20)
        return self.clean

    def __exit__(self, *exc):
        if self.thread.is_alive():
            self.stop()


def test_worker_processes_checks_and_alerts_end_to_end(http):
    """Scheduler dispatches -> job thread runs the check -> incident -> webhook delivered."""
    monitor = MonitorFactory(failure_threshold=2)
    AlertChannelFactory(organization=monitor.organization, webhook_url="https://discord.com/x/y")
    http.get(f"https://{PUBLIC_IP}/health").respond(500)
    hook = http.post(f"https://{DISCORD_IP}/x/y").respond(204)

    with Running(Worker(threads=2)) as running:
        wait_for(lambda: WorkerHeartbeat.objects.exists(), message="heartbeat")
        wait_for(lambda: CheckResult.objects.count() == 1, message="first check")
        Monitor.objects.filter(pk=monitor.pk).update(next_check_at=timezone.now())
        wait_for(lambda: AlertDelivery.objects.filter(status="sent").exists(), message="alert sent")
        assert running.stop() is True

    assert Incident.objects.filter(monitor=monitor, resolved_at__isnull=True).count() == 1
    assert hook.call_count == 1
    assert set(Job.objects.values_list("status", flat=True)) == {"succeeded"}
    assert not WorkerHeartbeat.objects.exists()  # removed on clean shutdown


def test_graceful_shutdown_finishes_in_flight_job_and_stops_claiming():
    in_flight = queue.enqueue("slow", {"seconds": 1})
    with Running(Worker(threads=1, scheduler=False)) as running:
        assert STARTED.wait(10)
        waiting = queue.enqueue("slow", {"seconds": 0})
        started = time.monotonic()
        assert running.stop() is True
        assert time.monotonic() - started >= 0.5  # it waited for the job in hand
    in_flight.refresh_from_db()
    waiting.refresh_from_db()
    assert in_flight.status == "succeeded"
    assert waiting.status == "queued"  # never claimed after the stop signal


def test_shutdown_timeout_leaves_job_for_the_reaper(settings):
    settings.WORKER_SHUTDOWN_TIMEOUT_SECONDS = 0.2
    job = queue.enqueue("slow", {"seconds": 2})
    worker = Worker(threads=1, scheduler=False)
    with Running(worker) as running:
        assert STARTED.wait(10)
        assert running.stop() is False  # not clean: a job was still running
    job.refresh_from_db()
    assert job.status == "running"  # its lease will expire and the reaper re-queues it
    # Let the daemon thread finish before the test database is torn down.
    worker.join(10)
    job.refresh_from_db()
    assert job.status == "succeeded"


def test_two_workers_share_the_queue_without_duplicates():
    jobs = [queue.enqueue("slow", {"seconds": 0.05}) for _ in range(20)]
    with Running(Worker(threads=2)) as a, Running(Worker(threads=2)) as b:
        wait_for(lambda: not Job.objects.exclude(status="succeeded").exists(), message="drain")
        assert a.stop() and b.stop()
    for job in jobs:
        job.refresh_from_db()
        assert (job.status, job.attempts) == ("succeeded", 1)  # each ran exactly once


def test_scheduler_tick_is_skipped_while_another_holds_the_lock():
    from apps.jobs.worker import SCHEDULER_LOCK_KEY

    MonitorFactory()

    def take_lock():
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_advisory_xact_lock(%s)", [SCHEDULER_LOCK_KEY])

    with HoldLock(take_lock):
        assert scheduler_tick() is None
        assert not Job.objects.exists()
    summary = scheduler_tick()
    assert summary == {"dispatched": 1, "requeued": 0, "dead": 0, "purged": 0}


def test_heartbeat_upserts_one_row():
    worker = Worker(threads=3)
    worker.heartbeat()
    worker.heartbeat()
    beat = WorkerHeartbeat.objects.get()
    assert (beat.worker_id, beat.threads, beat.pid) == (worker.id, 3, os.getpid())
    assert str(beat) == worker.id


# The real management command in a separate process, with real signals


def _database_url():
    s = connection.settings_dict
    host = f"{s['HOST'] or 'localhost'}:{s['PORT'] or 5432}"
    return f"postgres://{s['USER']}:{s['PASSWORD']}@{host}/{s['NAME']}"


def test_run_worker_command_end_to_end_and_stops_on_signal():
    """`manage.py run_worker` in its own process against the test DB and local fake servers."""
    # The child process uses the real DatabaseCache (dev settings); tests use LocMem, so the
    # test database has no cache table until we create it, as a deploy does.
    with override_settings(CACHES=DB_CACHE):
        call_command("createcachetable")
    with FakeTarget(status=500) as target, FakeWebhook() as webhook:
        monitor = MonitorFactory(url=target.url("/health"), failure_threshold=1)
        AlertChannelFactory(organization=monitor.organization, webhook_url=webhook.url("/hook"))
        env = {
            **os.environ,
            "DJANGO_SETTINGS_MODULE": "config.settings.dev",
            "DATABASE_URL": _database_url(),
            "DEBUG": "True",
            "SSRF_ALLOW_LOOPBACK": "True",  # DEBUG-only switch: the fake servers are on 127.0.0.1
            "SCHEDULER_INTERVAL_SECONDS": "0.5",
            "WORKER_POLL_SECONDS": "0.1",
            "WORKER_HEARTBEAT_SECONDS": "0.5",
        }
        windows = sys.platform == "win32"
        proc = subprocess.Popen(
            [sys.executable, "manage.py", "run_worker", "--threads", "2"],
            cwd=BASE_DIR,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if windows else 0,
        )
        try:
            try:
                wait_for(lambda: len(webhook.received) == 1, timeout=60, message="webhook")
            except AssertionError:
                proc.kill()
                output = proc.communicate()[0]
                raise AssertionError(f"no webhook; worker output:\n{output}") from None
            assert WorkerHeartbeat.objects.count() == 1
            if windows:
                os.kill(proc.pid, signal.CTRL_BREAK_EVENT)  # SIGBREAK in the child
            else:
                proc.send_signal(signal.SIGTERM)
            output, _ = proc.communicate(timeout=30)
        finally:
            if proc.poll() is None:
                proc.kill()
    assert proc.returncode == 0, output
    assert "stopped cleanly" in output
    assert "[DOWN]" in webhook.messages()[0]
    assert AlertDelivery.objects.get().status == "sent"
    assert Incident.objects.get().monitor_id == monitor.pk
    assert not WorkerHeartbeat.objects.exists()

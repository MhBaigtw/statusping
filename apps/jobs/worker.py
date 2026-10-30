"""The worker process: N job threads plus one control thread (heartbeat + scheduler).

Threads, not processes, so it runs natively on Windows (no fork). Each thread gets its
own Django DB connection (connections are per-thread) and closes it on exit.

Graceful shutdown: on SIGINT/SIGTERM (SIGBREAK on Windows) the stop event is set. Job
threads stop claiming and finish the job in hand. The main thread waits up to
WORKER_SHUTDOWN_TIMEOUT_SECONDS for them. A job still running after that keeps its lease
and is re-queued by the reaper once the lease expires.
"""

import logging
import os
import signal
import socket
import threading
import time
from datetime import timedelta
from secrets import token_hex

from django.conf import settings
from django.db import DatabaseError, connection
from django.utils import timezone

from . import queue
from .models import WorkerHeartbeat

logger = logging.getLogger(__name__)

# pg_try_advisory_lock key: only one scheduler tick runs at a time across all workers.
SCHEDULER_LOCK_KEY = 0x53_74_50_67  # "StPg"
HEARTBEAT_RETENTION = timedelta(days=1)


def scheduler_tick():
    """Dispatch due checks, reap expired leases, purge old jobs. Safe to run concurrently.

    The advisory lock only avoids wasted work: dispatch uses SKIP LOCKED and the reaper
    and purge are single UPDATE/DELETE statements, so overlapping ticks stay correct.
    Returns a summary dict, or None if another worker holds the scheduler lock.
    """
    from apps.monitors.services import dispatch_due_checks

    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_try_advisory_lock(%s)", [SCHEDULER_LOCK_KEY])
        if not cursor.fetchone()[0]:
            return None
    try:
        dispatched = []
        # Keep dispatching while batches come back full, so the dispatch rate is not capped
        # at DISPATCH_BATCH_SIZE per tick (100 per 10 s = 600 checks/min, measured in
        # docs/benchmarks.md as far below one worker's capacity). Each batch is its own
        # short transaction; DISPATCH_MAX_BATCHES bounds how long one tick can take.
        for _ in range(settings.DISPATCH_MAX_BATCHES):
            batch = dispatch_due_checks()
            dispatched += batch
            if len(batch) < settings.DISPATCH_BATCH_SIZE:
                break
        requeued, dead = queue.reap_expired()
        purged = queue.purge_succeeded()
        WorkerHeartbeat.objects.filter(last_seen__lt=timezone.now() - HEARTBEAT_RETENTION).delete()
    finally:
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_advisory_unlock(%s)", [SCHEDULER_LOCK_KEY])
    summary = {"dispatched": len(dispatched), "requeued": requeued, "dead": dead, "purged": purged}
    if any(summary.values()):
        logger.info("scheduler: %s", summary)
    return summary


class Worker:
    def __init__(self, *, threads=None, scheduler=True):
        self.threads = threads or settings.WORKER_THREADS
        self.scheduler = scheduler
        self.id = f"{socket.gethostname()}:{os.getpid()}:{token_hex(3)}"
        self.started_at = timezone.now()
        self.stop_event = threading.Event()
        self._threads = []

    # Lifecycle

    def start(self):
        for n in range(1, self.threads + 1):
            self._spawn(self._work_loop, f"job-{n}", f"{self.id}/{n}")
        self._spawn(self._control_loop, "control")
        logger.info(
            "worker %s started: %s job thread(s), scheduler %s",
            self.id,
            self.threads,
            "on" if self.scheduler else "off",
        )

    def _spawn(self, target, name, *args):
        # Daemon threads: if a job outlives the shutdown timeout, the process can still exit.
        thread = threading.Thread(target=target, name=name, args=args, daemon=True)
        thread.start()
        self._threads.append(thread)

    def stop(self, *_signal_args):
        if not self.stop_event.is_set():
            logger.info(
                "worker %s: shutting down, waiting up to %ss for in-flight jobs",
                self.id,
                settings.WORKER_SHUTDOWN_TIMEOUT_SECONDS,
            )
        self.stop_event.set()

    def join(self, timeout):
        """Wait for every thread to exit. Returns the names of threads still running."""
        deadline = time.monotonic() + timeout
        for thread in self._threads:
            thread.join(max(0, deadline - time.monotonic()))
        return [t.name for t in self._threads if t.is_alive()]

    def run(self, *, install_signals=True):
        """Start, block until stopped, then shut down gracefully. Returns True if clean."""
        if install_signals:
            for name in ("SIGINT", "SIGTERM", "SIGBREAK"):
                if hasattr(signal, name):
                    signal.signal(getattr(signal, name), self.stop)
        self.start()
        while not self.stop_event.wait(0.5):
            pass
        stuck = self.join(settings.WORKER_SHUTDOWN_TIMEOUT_SECONDS)
        try:
            WorkerHeartbeat.objects.filter(worker_id=self.id).delete()
        except DatabaseError:
            logger.exception("worker %s: could not remove heartbeat", self.id)
        finally:
            connection.close()
        if stuck:
            logger.warning(
                "worker %s: %s still running after timeout; the reaper will re-queue their jobs",
                self.id,
                ", ".join(stuck),
            )
        else:
            logger.info("worker %s stopped cleanly", self.id)
        return not stuck

    # Threads

    def _work_loop(self, locked_by):
        try:
            while not self.stop_event.is_set():
                try:
                    jobs = queue.claim(locked_by)
                    for job in jobs:
                        logger.info("job %s %s: attempt %s", job.pk, job.kind, job.attempts)
                        queue.run_job(job, locked_by)
                except DatabaseError:
                    logger.exception("%s: database error; reconnecting", locked_by)
                    connection.close()
                    jobs = []
                if not jobs:
                    self.stop_event.wait(settings.WORKER_POLL_SECONDS)
        finally:
            connection.close()

    def _control_loop(self):
        next_beat = next_tick = 0.0
        try:
            while not self.stop_event.is_set():
                now = time.monotonic()
                try:
                    if now >= next_beat:
                        self.heartbeat()
                        next_beat = now + settings.WORKER_HEARTBEAT_SECONDS
                    if self.scheduler and now >= next_tick:
                        scheduler_tick()
                        next_tick = now + settings.SCHEDULER_INTERVAL_SECONDS
                except DatabaseError:
                    logger.exception("control: database error; reconnecting")
                    connection.close()
                except Exception:
                    logger.exception("control: scheduler tick failed")
                wake = min(next_beat, next_tick if self.scheduler else next_beat)
                self.stop_event.wait(max(0.05, wake - time.monotonic()))
        finally:
            connection.close()

    def heartbeat(self):
        WorkerHeartbeat.objects.update_or_create(
            worker_id=self.id,
            defaults={
                "hostname": socket.gethostname(),
                "pid": os.getpid(),
                "threads": self.threads,
                "started_at": self.started_at,
                "last_seen": timezone.now(),
            },
        )

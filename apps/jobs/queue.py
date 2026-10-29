"""A job queue on a plain Postgres table, built from scratch.

Lifecycle: queued -> running -> succeeded
                           |-> queued again (retry with backoff)
                           |-> failed (permanent error) / dead (out of attempts)

- enqueue() is a plain INSERT. Inside a transaction, the job exists only if that
  transaction commits: no on_commit hooks and no "job points at a row that was rolled back".
- claim() locks due rows with FOR UPDATE SKIP LOCKED in one short transaction, so
  concurrent workers never get the same job and never wait for each other. The handler
  runs after that transaction has committed.
- complete()/fail() only touch a job still leased to the caller (status=running AND
  locked_by=worker), so a worker whose lease expired can't overwrite a reclaimed job.
- reap_expired() returns jobs whose lease ran out (a crashed or stalled worker) to the queue.

Delivery is at-least-once, so every handler must be safe to run twice.
"""

import logging
import random
from datetime import timedelta

from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import F, Q
from django.utils import timezone

from .exceptions import PermanentError, RetryLater
from .models import Job
from .registry import HANDLERS, get_handler

logger = logging.getLogger(__name__)

S = Job.Status
PENDING = (S.QUEUED, S.RUNNING)


def enqueue(kind, payload, *, run_after=None, dedupe_key=None, max_attempts=None):
    """Insert a job. With `dedupe_key`, return the existing pending job instead of a duplicate."""
    if kind not in HANDLERS:
        raise ValueError(f"Unknown job kind {kind!r}.")
    fields = {
        "kind": kind,
        "payload": payload,
        "run_after": run_after or timezone.now(),
        "max_attempts": max_attempts or settings.JOB_DEFAULT_MAX_ATTEMPTS,
        "dedupe_key": dedupe_key,
    }
    if dedupe_key is None:
        return Job.objects.create(**fields)
    try:
        # A savepoint: a duplicate must not abort the caller's transaction.
        with transaction.atomic():
            return Job.objects.create(**fields)
    except IntegrityError:
        existing = Job.objects.filter(dedupe_key=dedupe_key, status__in=PENDING).first()
        if existing is None:  # the conflicting job finished in the meantime
            return Job.objects.create(**fields)
        return existing


def claim(worker_id, *, limit=1, lease_seconds=None):
    """Lease up to `limit` due jobs to `worker_id`. Returns them (status=running)."""
    now = timezone.now()
    locked_until = now + timedelta(seconds=lease_seconds or settings.JOB_LEASE_SECONDS)
    with transaction.atomic():
        jobs = list(
            Job.objects.select_for_update(skip_locked=True)
            .filter(status=S.QUEUED, run_after__lte=now)
            .order_by("run_after", "id")[:limit]
        )
        for job in jobs:
            job.status = S.RUNNING
            job.locked_by = worker_id
            job.locked_until = locked_until
            job.attempts += 1
        Job.objects.bulk_update(jobs, ["status", "locked_by", "locked_until", "attempts"])
    return jobs


def _leased_to(job, worker_id):
    return Job.objects.filter(pk=job.pk, status=S.RUNNING, locked_by=worker_id)


def complete(job, worker_id):
    """Mark the job succeeded. False if the lease was lost (the job was reclaimed)."""
    updated = _leased_to(job, worker_id).update(
        status=S.SUCCEEDED, finished_at=timezone.now(), locked_by="", locked_until=None
    )
    if not updated:
        logger.warning("job %s: lease lost before completion; result discarded", job.pk)
    return bool(updated)


def backoff_seconds(attempts):
    """Exponential backoff with jitter: base * 2^(attempts-1), capped, times 0.5-1.0."""
    delay = min(
        settings.JOB_RETRY_MAX_SECONDS, settings.JOB_RETRY_BASE_SECONDS * 2 ** (attempts - 1)
    )
    return delay * random.uniform(0.5, 1.0)


def fail(job, worker_id, error, *, retry_at=None, permanent=False):
    """Record a failed attempt: re-queue with backoff, or mark failed/dead.

    Returns the new status, or None if the lease was lost.
    """
    now = timezone.now()
    if permanent:
        status = S.FAILED
    elif job.attempts >= job.max_attempts:
        status = S.DEAD
    else:
        status = S.QUEUED
    fields = {"status": status, "last_error": error[:2000], "locked_by": "", "locked_until": None}
    if status == S.QUEUED and retry_at:
        # Honour the handler's time (e.g. Retry-After), but never park a job for longer
        # than the backoff cap.
        fields["run_after"] = min(retry_at, now + timedelta(seconds=settings.JOB_RETRY_MAX_SECONDS))
    elif status == S.QUEUED:
        fields["run_after"] = now + timedelta(seconds=backoff_seconds(job.attempts))
    else:
        fields["finished_at"] = now
    if not _leased_to(job, worker_id).update(**fields):
        logger.warning("job %s: lease lost before recording failure", job.pk)
        return None
    return status


def run_job(job, worker_id):
    """Run one claimed job's handler and record the outcome. Returns the new status."""
    try:
        handler = get_handler(job.kind)
        handler(job)
    except RetryLater as exc:
        logger.info("job %s %s: retry later (%s)", job.pk, job.kind, exc)
        return fail(job, worker_id, str(exc), retry_at=exc.retry_at)
    except PermanentError as exc:
        logger.warning("job %s %s: permanent failure (%s)", job.pk, job.kind, exc)
        return fail(job, worker_id, str(exc), permanent=True)
    except Exception as exc:
        logger.exception("job %s %s: attempt %s raised", job.pk, job.kind, job.attempts)
        return fail(job, worker_id, f"{type(exc).__name__}: {exc}")
    return S.SUCCEEDED if complete(job, worker_id) else None


def reap_expired(now=None):
    """Return running jobs whose lease expired to the queue (or mark them dead).

    Returns (requeued, dead).
    """
    now = now or timezone.now()
    expired = Q(status=S.RUNNING, locked_until__lt=now)
    released = {"locked_by": "", "locked_until": None}
    dead = Job.objects.filter(expired, attempts__gte=F("max_attempts")).update(
        status=S.DEAD,
        finished_at=now,
        last_error="Lease expired on the final attempt (worker crashed or stalled).",
        **released,
    )
    requeued = Job.objects.filter(expired).update(
        status=S.QUEUED,
        run_after=now,
        last_error="Lease expired (worker crashed or stalled); re-queued.",
        **released,
    )
    if requeued or dead:
        logger.warning("reaper: %s job(s) re-queued, %s marked dead", requeued, dead)
    return requeued, dead


def purge_succeeded(now=None, *, batch_size=None, max_batches=10):
    """Delete succeeded jobs older than the retention period, in batches. Returns the count.

    Failed and dead jobs are kept for inspection.
    """
    now = now or timezone.now()
    batch_size = batch_size or settings.JOB_PURGE_BATCH_SIZE
    cutoff = now - timedelta(days=settings.JOB_SUCCEEDED_RETENTION_DAYS)
    deleted = 0
    for _ in range(max_batches):
        ids = list(
            Job.objects.filter(status=S.SUCCEEDED, finished_at__lt=cutoff).values_list(
                "pk", flat=True
            )[:batch_size]
        )
        if not ids:
            break
        deleted += Job.objects.filter(pk__in=ids).delete()[0]
        if len(ids) < batch_size:
            break
    return deleted

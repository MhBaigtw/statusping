"""The Postgres job queue: transactional enqueue, SKIP LOCKED claims, leases, retries."""

import time
from datetime import timedelta

import pytest
import time_machine
from django.db import transaction
from django.utils import timezone

from apps.jobs import queue
from apps.jobs.exceptions import PermanentError, RetryLater
from apps.jobs.models import Job

from .concurrency import HoldLock, run_in_threads

S = Job.Status
CALLS = []


def ok_handler(job):
    CALLS.append(job.pk)


def crash_handler(job):
    raise RuntimeError("boom")


def retry_handler(job):
    raise RetryLater("try again", retry_at=timezone.now() + timedelta(seconds=job.payload["wait"]))


def permanent_handler(job):
    raise PermanentError("never going to work")


@pytest.fixture(autouse=True)
def handlers(monkeypatch):
    CALLS.clear()
    for kind in ("ok", "crash", "retry", "permanent"):
        monkeypatch.setitem(queue.HANDLERS, kind, f"tests.test_queue.{kind}_handler")


def due_job(kind="ok", **fields):
    return queue.enqueue(kind, {}, **fields)


# Enqueue


@pytest.mark.django_db(transaction=True)
def test_enqueue_in_rolled_back_transaction_leaves_no_job():
    with pytest.raises(RuntimeError), transaction.atomic():
        queue.enqueue("ok", {"n": 1})
        assert Job.objects.count() == 1  # visible inside the transaction...
        raise RuntimeError("rollback")
    assert Job.objects.count() == 0  # ...and gone with it


@pytest.mark.django_db
def test_enqueue_defaults_and_unknown_kind(settings):
    job = queue.enqueue("ok", {"a": 1})
    assert (job.status, job.attempts, job.max_attempts) == (S.QUEUED, 0, 3)
    assert job.run_after <= timezone.now()
    with pytest.raises(ValueError):
        queue.enqueue("nope", {})


@pytest.mark.django_db
def test_dedupe_key_returns_the_pending_job():
    first = queue.enqueue("ok", {}, dedupe_key="k")
    with transaction.atomic():  # the duplicate must not break the caller's transaction
        second = queue.enqueue("ok", {}, dedupe_key="k")
        Job.objects.count()  # the transaction is still usable
    assert second.pk == first.pk
    assert Job.objects.count() == 1
    # Once the job is no longer pending, the key is free again.
    Job.objects.filter(pk=first.pk).update(status=S.SUCCEEDED)
    third = queue.enqueue("ok", {}, dedupe_key="k")
    assert third.pk != first.pk
    # A running job also blocks duplicates.
    Job.objects.filter(pk=third.pk).update(status=S.RUNNING)
    assert queue.enqueue("ok", {}, dedupe_key="k").pk == third.pk
    # Jobs without a key never collide.
    queue.enqueue("ok", {})
    queue.enqueue("ok", {})


# Claim


@pytest.mark.django_db
def test_claim_takes_only_due_queued_jobs_in_order():
    now = timezone.now()
    later = due_job(run_after=now - timedelta(seconds=1))
    first = due_job(run_after=now - timedelta(seconds=5))
    due_job(run_after=now + timedelta(minutes=5))  # not due yet
    Job.objects.filter(pk=due_job().pk).update(status=S.SUCCEEDED)
    claimed = queue.claim("w1", limit=10, lease_seconds=30)
    assert [j.pk for j in claimed] == [first.pk, later.pk]
    job = Job.objects.get(pk=first.pk)
    assert job.status == S.RUNNING
    assert job.locked_by == "w1"
    assert job.attempts == 1
    assert timedelta(seconds=29) < job.locked_until - timezone.now() <= timedelta(seconds=30)
    assert queue.claim("w2") == []


@pytest.mark.django_db(transaction=True)
def test_concurrent_claimers_never_get_the_same_job():
    ids = {due_job().pk for _ in range(60)}

    def drain(index):
        mine = []
        while batch := queue.claim(f"worker-{index}", limit=3):
            mine.extend(job.pk for job in batch)
        return mine

    results = run_in_threads(drain, 8)
    claimed = [pk for result in results for pk in result]
    assert len(claimed) == len(set(claimed)) == 60  # no job claimed twice
    assert set(claimed) == ids
    assert sum(1 for r in results if r) > 1  # the work was actually shared
    assert Job.objects.filter(status=S.RUNNING, attempts=1).count() == 60


@pytest.mark.django_db(transaction=True)
def test_claim_skips_rows_locked_by_another_transaction_without_waiting():
    """SKIP LOCKED: a claimer never blocks on a job another transaction has locked."""
    locked = due_job(run_after=timezone.now() - timedelta(minutes=1))
    free = due_job()

    def lock_first_job():
        list(Job.objects.select_for_update().filter(pk=locked.pk))

    with HoldLock(lock_first_job):
        started = time.monotonic()
        claimed = queue.claim("w1", limit=5)
        elapsed = time.monotonic() - started
    assert [j.pk for j in claimed] == [free.pk]
    assert elapsed < 2, f"claim waited {elapsed:.1f}s for a locked row"


# Finish, retry, dead


@pytest.mark.django_db
def test_successful_job_is_marked_succeeded():
    job = due_job()
    [claimed] = queue.claim("w1")
    assert queue.run_job(claimed, "w1") == S.SUCCEEDED
    job.refresh_from_db()
    assert job.status == S.SUCCEEDED
    assert job.finished_at is not None
    assert (job.locked_by, job.locked_until) == ("", None)
    assert [job.pk] == CALLS


@pytest.mark.django_db
def test_failure_requeues_with_exponential_backoff(settings):
    settings.JOB_RETRY_BASE_SECONDS = 10
    job = due_job("crash", max_attempts=5)
    delays = []
    for _ in range(3):
        Job.objects.filter(pk=job.pk).update(run_after=timezone.now())
        [claimed] = queue.claim("w1")
        before = timezone.now()
        assert queue.run_job(claimed, "w1") == S.QUEUED
        job.refresh_from_db()
        delays.append((job.run_after - before).total_seconds())
        assert job.last_error == "RuntimeError: boom"
        assert job.locked_by == ""
    # base * 2^(attempt-1) with 50-100% jitter: 5-10s, 10-20s, 20-40s
    for delay, (low, high) in zip(delays, [(5, 10), (10, 20), (20, 40)], strict=True):
        assert low - 0.5 <= delay <= high + 0.5, delays
    assert job.attempts == 3


def test_backoff_is_capped(settings):
    settings.JOB_RETRY_BASE_SECONDS = 10
    settings.JOB_RETRY_MAX_SECONDS = 60
    assert all(30 <= queue.backoff_seconds(20) <= 60 for _ in range(50))
    assert all(5 <= queue.backoff_seconds(1) <= 10 for _ in range(50))


@pytest.mark.django_db
def test_out_of_attempts_is_dead():
    job = due_job("crash", max_attempts=2)
    for expected in (S.QUEUED, S.DEAD):
        Job.objects.filter(pk=job.pk).update(run_after=timezone.now())
        [claimed] = queue.claim("w1")
        assert queue.run_job(claimed, "w1") == expected
    job.refresh_from_db()
    assert (job.status, job.attempts, job.last_error) == (S.DEAD, 2, "RuntimeError: boom")
    assert job.finished_at is not None
    assert queue.claim("w1") == []


@pytest.mark.django_db
def test_handler_retry_time_is_honoured_and_capped(settings):
    settings.JOB_RETRY_MAX_SECONDS = 900
    job = queue.enqueue("retry", {"wait": 120})
    [claimed] = queue.claim("w1")
    queue.run_job(claimed, "w1")
    job.refresh_from_db()
    assert 119 <= (job.run_after - timezone.now()).total_seconds() <= 120
    assert job.last_error == "try again"

    capped = queue.enqueue("retry", {"wait": 86400})
    [claimed] = queue.claim("w1")
    queue.run_job(claimed, "w1")
    capped.refresh_from_db()
    assert (capped.run_after - timezone.now()).total_seconds() <= 900


@pytest.mark.django_db
def test_permanent_error_and_unknown_kind_are_failed_without_retry():
    job = due_job("permanent", max_attempts=5)
    [claimed] = queue.claim("w1")
    assert queue.run_job(claimed, "w1") == S.FAILED
    job.refresh_from_db()
    assert (job.status, job.attempts, job.last_error) == (S.FAILED, 1, "never going to work")

    orphan = Job.objects.create(kind="removed_kind", payload={})
    [claimed] = queue.claim("w1")
    assert queue.run_job(claimed, "w1") == S.FAILED
    orphan.refresh_from_db()
    assert "Unknown job kind" in orphan.last_error


# Leases: crash recovery and stale workers


@pytest.mark.django_db
def test_reaper_requeues_a_job_whose_worker_never_finished():
    job = due_job(max_attempts=3)
    queue.claim("crashed-worker", lease_seconds=60)  # ...and then the worker dies
    assert queue.reap_expired() == (0, 0)  # lease still valid: left alone
    with time_machine.travel(timezone.now() + timedelta(seconds=61)):
        assert queue.reap_expired() == (1, 0)
        job.refresh_from_db()
        assert job.status == S.QUEUED
        assert job.locked_by == ""
        assert job.attempts == 1  # the lost attempt still counts
        assert "Lease expired" in job.last_error
        [reclaimed] = queue.claim("healthy-worker")
    assert reclaimed.pk == job.pk
    assert queue.run_job(reclaimed, "healthy-worker") == S.SUCCEEDED


@pytest.mark.django_db
def test_reaper_marks_dead_when_the_final_attempt_expires():
    job = due_job(max_attempts=1)
    queue.claim("crashed-worker", lease_seconds=60)
    with time_machine.travel(timezone.now() + timedelta(seconds=61)):
        assert queue.reap_expired() == (0, 1)
    job.refresh_from_db()
    assert job.status == S.DEAD
    assert "final attempt" in job.last_error


@pytest.mark.django_db
def test_stale_worker_cannot_finish_a_reclaimed_job():
    job = due_job(max_attempts=3)
    [stale_copy] = queue.claim("slow-worker", lease_seconds=60)
    with time_machine.travel(timezone.now() + timedelta(seconds=61)):
        queue.reap_expired()
        [fresh_copy] = queue.claim("new-worker", lease_seconds=60)
        # The slow worker finally finishes: neither outcome may overwrite the new lease.
        assert queue.complete(stale_copy, "slow-worker") is False
        assert queue.fail(stale_copy, "slow-worker", "late error") is None
        job.refresh_from_db()
        assert (job.status, job.locked_by, job.attempts) == (S.RUNNING, "new-worker", 2)
        assert queue.run_job(fresh_copy, "new-worker") == S.SUCCEEDED
    job.refresh_from_db()
    assert job.status == S.SUCCEEDED


# Retention


@pytest.mark.django_db
def test_purge_deletes_only_old_succeeded_jobs_in_batches(settings):
    settings.JOB_SUCCEEDED_RETENTION_DAYS = 7
    now = timezone.now()
    old = now - timedelta(days=8)
    for _ in range(5):
        Job.objects.create(kind="ok", status=S.SUCCEEDED, finished_at=old)
    recent = Job.objects.create(kind="ok", status=S.SUCCEEDED, finished_at=now - timedelta(days=6))
    dead = Job.objects.create(kind="ok", status=S.DEAD, finished_at=old)
    failed = Job.objects.create(kind="ok", status=S.FAILED, finished_at=old)
    queued = Job.objects.create(kind="ok")
    assert queue.purge_succeeded(batch_size=2, max_batches=2) == 4  # bounded per call
    assert queue.purge_succeeded(batch_size=2) == 1
    assert queue.purge_succeeded(batch_size=2) == 0
    assert set(Job.objects.values_list("pk", flat=True)) == {
        recent.pk,
        dead.pk,
        failed.pk,
        queued.pk,
    }


@pytest.mark.django_db
def test_job_str():
    assert "queued" in str(due_job())

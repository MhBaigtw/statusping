from django.db import models
from django.db.models import Q
from django.utils import timezone


class Job(models.Model):
    """A unit of background work. The table *is* the queue (see apps/jobs/queue.py)."""

    class Status(models.TextChoices):
        QUEUED = "queued", "Queued"
        RUNNING = "running", "Running"
        SUCCEEDED = "succeeded", "Succeeded"
        FAILED = "failed", "Failed"  # permanent error: retrying cannot help
        DEAD = "dead", "Dead"  # gave up after max_attempts (errors or expired leases)

    kind = models.CharField(max_length=50)
    payload = models.JSONField(default=dict)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.QUEUED)
    run_after = models.DateTimeField(default=timezone.now)
    attempts = models.PositiveSmallIntegerField(default=0)
    max_attempts = models.PositiveSmallIntegerField(default=3)
    locked_by = models.CharField(max_length=100, blank=True)
    locked_until = models.DateTimeField(null=True, blank=True)
    last_error = models.TextField(blank=True)
    # NULL (not "") means "no key": NULLs never collide in the unique constraint below.
    dedupe_key = models.CharField(max_length=100, null=True, blank=True)  # noqa: DJ001
    created_at = models.DateTimeField(auto_now_add=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["id"]
        indexes = [
            # Serves the claim (status='queued' AND run_after <= now ORDER BY run_after) and
            # the reaper (status='running'). A partial index on queued jobs was measured and
            # gave no gain over this one, so it was dropped (docs/decisions.md D9).
            models.Index(fields=["status", "run_after"], name="job_status_run_after_idx"),
        ]
        constraints = [
            # At most one pending (queued or running) job per dedupe key.
            models.UniqueConstraint(
                fields=["dedupe_key"],
                condition=Q(status__in=["queued", "running"]),
                name="job_unique_pending_dedupe_key",
            ),
        ]

    def __str__(self):
        return f"Job {self.pk} {self.kind} ({self.status})"


class WorkerHeartbeat(models.Model):
    """One row per running worker process, refreshed every few seconds."""

    worker_id = models.CharField(max_length=100, primary_key=True)
    hostname = models.CharField(max_length=255)
    pid = models.PositiveIntegerField()
    threads = models.PositiveSmallIntegerField()
    started_at = models.DateTimeField()
    last_seen = models.DateTimeField()

    def __str__(self):
        return self.worker_id

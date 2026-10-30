from django.conf import settings
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.db.models import Q
from django.utils import timezone

from apps.organizations.models import Organization

# Hard ceiling for a monitor's timeout_seconds. It is also the worst-case duration of one
# check (safe_http enforces it as a total budget), which apps/core/checks.py compares
# with JOB_LEASE_SECONDS.
MONITOR_MAX_TIMEOUT_SECONDS = 10


def default_interval():
    return settings.MONITOR_DEFAULT_INTERVAL_SECONDS


def default_timeout():
    return settings.MONITOR_DEFAULT_TIMEOUT_SECONDS


class Monitor(models.Model):
    class Method(models.TextChoices):
        GET = "GET", "GET"
        HEAD = "HEAD", "HEAD"

    class Status(models.TextChoices):
        UP = "up", "Up"
        DOWN = "down", "Down"
        UNKNOWN = "unknown", "Unknown"

    organization = models.ForeignKey(
        Organization, on_delete=models.CASCADE, related_name="monitors"
    )
    name = models.CharField(max_length=100)
    url = models.URLField(max_length=2000)
    method = models.CharField(max_length=4, choices=Method.choices, default=Method.GET)
    interval_seconds = models.PositiveIntegerField(
        default=default_interval, validators=[MinValueValidator(60), MaxValueValidator(3600)]
    )
    timeout_seconds = models.PositiveSmallIntegerField(
        default=default_timeout,
        validators=[MinValueValidator(1), MaxValueValidator(MONITOR_MAX_TIMEOUT_SECONDS)],
    )
    expected_status = models.PositiveSmallIntegerField(
        default=200, validators=[MinValueValidator(100), MaxValueValidator(599)]
    )
    failure_threshold = models.PositiveSmallIntegerField(
        default=3, validators=[MinValueValidator(1), MaxValueValidator(10)]
    )
    recovery_threshold = models.PositiveSmallIntegerField(
        default=2, validators=[MinValueValidator(1), MaxValueValidator(10)]
    )
    is_paused = models.BooleanField(default=False)
    # New monitors are due immediately.
    next_check_at = models.DateTimeField(default=timezone.now)
    # The raw outcome of the most recent check. Whether an incident is open (the
    # flap-protected view) is a separate question answered by the Incident table.
    last_status = models.CharField(max_length=7, choices=Status.choices, default=Status.UNKNOWN)
    consecutive_failures = models.PositiveIntegerField(default=0)
    consecutive_successes = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["id"]
        indexes = [
            # The dispatcher's query: due, unpaused monitors in next_check_at order.
            models.Index(
                fields=["next_check_at"], condition=Q(is_paused=False), name="monitor_due_idx"
            ),
        ]

    def __str__(self):
        return f"{self.name} ({self.url})"


class CheckResult(models.Model):
    """One check of one monitor. High volume by design (append-only)."""

    class Error(models.TextChoices):
        TIMEOUT = "timeout", "Timeout"
        DNS = "dns", "DNS failure"
        CONNECTION = "connection", "Connection error"
        SSL = "ssl", "TLS/SSL error"
        UNEXPECTED_STATUS = "unexpected_status", "Unexpected status code"
        BLOCKED_TARGET = "blocked_target", "Blocked target (SSRF protection)"

    # No separate FK index: check_monitor_time_idx (monitor, -checked_at) serves every
    # lookup by monitor. Measured in docs/benchmarks.md (C2): the redundant index cost a
    # write per check and made the planner pick a slower plan for the uptime aggregate.
    monitor = models.ForeignKey(
        Monitor, on_delete=models.CASCADE, related_name="checks", db_index=False
    )
    checked_at = models.DateTimeField()
    ok = models.BooleanField()
    status_code = models.PositiveSmallIntegerField(null=True, blank=True)
    response_ms = models.PositiveIntegerField(null=True, blank=True)
    error = models.CharField(max_length=20, choices=Error.choices, blank=True)
    # The run_check job that produced this result. Unique, so re-running the same job
    # (at-least-once delivery) can never record a second result. Not a FK: succeeded
    # jobs are purged after 7 days but results are kept.
    job_id = models.BigIntegerField(null=True, blank=True, unique=True)

    class Meta:
        ordering = ["-checked_at", "-id"]
        indexes = [
            models.Index(fields=["monitor", "-checked_at"], name="check_monitor_time_idx"),
        ]

    def __str__(self):
        return f"{self.monitor_id} {'ok' if self.ok else self.error} at {self.checked_at}"


class Incident(models.Model):
    monitor = models.ForeignKey(Monitor, on_delete=models.CASCADE, related_name="incidents")
    started_at = models.DateTimeField()
    resolved_at = models.DateTimeField(null=True, blank=True)
    cause = models.CharField(max_length=20, choices=CheckResult.Error.choices)

    class Meta:
        ordering = ["-started_at", "-id"]
        constraints = [
            models.UniqueConstraint(
                fields=["monitor"],
                condition=Q(resolved_at__isnull=True),
                name="one_open_incident_per_monitor",
            ),
        ]

    def __str__(self):
        state = "open" if self.resolved_at is None else "resolved"
        return f"Incident {self.pk} on monitor {self.monitor_id} ({state})"

    @property
    def is_open(self):
        return self.resolved_at is None

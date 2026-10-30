import logging
import ssl
from datetime import timedelta

import httpx
from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from apps.alerts.models import AlertDelivery
from apps.alerts.services import queue_alerts
from apps.core import safe_http
from apps.core.exceptions import Conflict
from apps.jobs.queue import enqueue
from apps.organizations.models import Organization
from apps.statuspage.services import invalidate as invalidate_status_page

from .models import CheckResult, Incident, Monitor

logger = logging.getLogger(__name__)

Error = CheckResult.Error


# Monitor management


def validate_monitor_url(url):
    """SSRF validation at create/update time (it runs again before every check)."""
    try:
        safe_http.validate_target(url)
    except safe_http.BlockedTarget as exc:
        raise ValidationError(str(exc), code="blocked_target") from exc
    except safe_http.DNSFailure as exc:
        raise ValidationError(str(exc), code="dns") from exc
    return url


def create_monitor(*, organization, **fields):
    with transaction.atomic():
        # Lock the org row so two concurrent creates cannot both pass the limit check.
        Organization.objects.select_for_update().get(pk=organization.pk)
        limit = settings.MONITORS_PER_ORG
        if Monitor.objects.filter(organization=organization).count() >= limit:
            raise Conflict(
                f"This organization already has the maximum of {limit} monitors.",
                code="monitor_limit_reached",
                details={"limit": limit},
            )
        monitor = Monitor.objects.create(organization=organization, **fields)
        invalidate_status_page(organization)
        return monitor


def update_monitor(monitor, **fields):
    for name, value in fields.items():
        setattr(monitor, name, value)
    monitor.save()
    invalidate_status_page(monitor.organization)  # the name is shown publicly
    return monitor


def delete_monitor(monitor):
    organization = monitor.organization
    monitor.delete()
    invalidate_status_page(organization)


def pause_monitor(monitor):
    monitor.is_paused = True
    monitor.save(update_fields=["is_paused", "updated_at"])
    invalidate_status_page(monitor.organization)
    return monitor


def resume_monitor(monitor):
    if monitor.is_paused:
        monitor.is_paused = False
        monitor.next_check_at = timezone.now()
        monitor.save(update_fields=["is_paused", "next_check_at", "updated_at"])
        invalidate_status_page(monitor.organization)
    return monitor


# Scheduling


def dispatch_due_checks(*, batch_size=None):
    """Claim due, unpaused monitors and enqueue one run_check job each. Returns their ids.

    The claim (FOR UPDATE SKIP LOCKED + advancing next_check_at) and the enqueue are one
    transaction: either both happen or neither. Concurrent dispatchers skip each other's
    locked rows, so a monitor is never claimed twice for the same slot.
    """
    now = timezone.now()
    with transaction.atomic():
        monitors = list(
            Monitor.objects.select_for_update(skip_locked=True)
            .filter(is_paused=False, next_check_at__lte=now)
            .order_by("next_check_at")
            .only("id", "interval_seconds", "next_check_at")[
                : batch_size or settings.DISPATCH_BATCH_SIZE
            ]
        )
        for monitor in monitors:
            monitor.next_check_at = now + timedelta(seconds=monitor.interval_seconds)
            enqueue(
                "run_check",
                {"monitor_id": monitor.pk},
                dedupe_key=f"run_check:{monitor.pk}",
                max_attempts=1,  # a missed check is not retried; the next one is due soon
            )
        Monitor.objects.bulk_update(monitors, ["next_check_at"])
    return [m.pk for m in monitors]


# Running a check


def _is_tls_error(exc):
    """True if an ssl.SSLError is anywhere in the exception chain (httpx wraps it twice)."""
    seen = set()
    while exc is not None and id(exc) not in seen:  # chains can contain cycles
        if isinstance(exc, ssl.SSLError):
            return True
        seen.add(id(exc))
        exc = exc.__cause__ or exc.__context__
    return False


def perform_check(monitor):
    """Make the HTTP request. Returns CheckResult field values (no DB writes)."""
    try:
        result = safe_http.send(monitor.method, monitor.url, timeout=monitor.timeout_seconds)
    except safe_http.BlockedTarget:
        return {"ok": False, "error": Error.BLOCKED_TARGET}
    except safe_http.DNSFailure:
        return {"ok": False, "error": Error.DNS}
    except httpx.TimeoutException:
        return {"ok": False, "error": Error.TIMEOUT}
    except httpx.HTTPError as exc:
        return {"ok": False, "error": Error.SSL if _is_tls_error(exc) else Error.CONNECTION}
    ok = result.status_code == monitor.expected_status
    return {
        "ok": ok,
        "status_code": result.status_code,
        "response_ms": result.elapsed_ms,
        "error": "" if ok else Error.UNEXPECTED_STATUS,
    }


def run_check(monitor_id, *, job_id=None):
    """Check one monitor and apply the result. Idempotent per job_id.

    Returns the new CheckResult, or None if the check was skipped (monitor gone or
    paused, or this job already recorded its result).
    """
    monitor = Monitor.objects.filter(pk=monitor_id, is_paused=False).first()
    if monitor is None:
        return None
    if job_id is not None and CheckResult.objects.filter(job_id=job_id).exists():
        return None  # this job already ran to completion once
    checked_at = timezone.now()
    outcome = perform_check(monitor)
    try:
        with transaction.atomic():
            monitor = Monitor.objects.select_for_update().filter(pk=monitor_id).first()
            if monitor is None or monitor.is_paused:
                logger.info("monitor %s deleted or paused during its check; dropped", monitor_id)
                return None
            result = CheckResult.objects.create(
                monitor=monitor, checked_at=checked_at, job_id=job_id, **outcome
            )
            apply_result(monitor, result)
    except IntegrityError:
        if job_id is not None and CheckResult.objects.filter(job_id=job_id).exists():
            return None  # a concurrent run of the same job recorded first
        raise
    return result


def apply_result(monitor, result):
    """Update counters and open/resolve incidents. Caller holds the monitor row lock."""
    if result.ok:
        monitor.consecutive_successes += 1
        monitor.consecutive_failures = 0
        monitor.last_status = Monitor.Status.UP
    else:
        monitor.consecutive_failures += 1
        monitor.consecutive_successes = 0
        monitor.last_status = Monitor.Status.DOWN
    monitor.save(
        update_fields=[
            "consecutive_successes",
            "consecutive_failures",
            "last_status",
            "updated_at",
        ]
    )

    open_incident = Incident.objects.filter(monitor=monitor, resolved_at__isnull=True).first()
    if (
        not result.ok
        and open_incident is None
        and monitor.consecutive_failures >= monitor.failure_threshold
    ):
        incident = Incident.objects.create(
            monitor=monitor, started_at=_streak_start(monitor), cause=result.error
        )
        logger.info("monitor %s: incident %s opened (%s)", monitor.pk, incident.pk, result.error)
        queue_alerts(incident, AlertDelivery.Event.OPENED)
        invalidate_status_page(monitor.organization)
    elif (
        result.ok
        and open_incident is not None
        and monitor.consecutive_successes >= monitor.recovery_threshold
    ):
        open_incident.resolved_at = result.checked_at
        open_incident.save(update_fields=["resolved_at"])
        logger.info("monitor %s: incident %s resolved", monitor.pk, open_incident.pk)
        queue_alerts(open_incident, AlertDelivery.Event.RESOLVED)
        invalidate_status_page(monitor.organization)


def _streak_start(monitor):
    """When the current failure streak began: the oldest of the last N failed checks."""
    first_failure = (
        CheckResult.objects.filter(monitor=monitor)
        .order_by("-checked_at", "-id")
        .values_list("checked_at", flat=True)[monitor.consecutive_failures - 1]
    )
    return first_failure


# Retention


def purge_check_results(now=None, *, batch_size=None, max_rows=None):
    """Delete CheckResults older than CHECK_RESULT_RETENTION_DAYS. Returns (deleted, complete).

    Works monitor by monitor so every query is a short range scan on the existing
    (monitor, -checked_at) index; a global `checked_at < cutoff` would need another index
    on a table that grows with every check (measured in docs/benchmarks.md, D1). Deletes
    in batches of `batch_size` and stops after `max_rows`, so one run can't hold the
    scheduler lock for long; `complete` is False when it stopped early (run again soon).
    """
    now = now or timezone.now()
    batch_size = batch_size or settings.CHECK_RESULT_PURGE_BATCH_SIZE
    max_rows = max_rows or settings.CHECK_RESULT_PURGE_MAX_ROWS
    cutoff = now - timedelta(days=settings.CHECK_RESULT_RETENTION_DAYS)
    deleted = 0
    for monitor_id in Monitor.objects.order_by("pk").values_list("pk", flat=True):
        while True:
            ids = list(
                CheckResult.objects.filter(monitor_id=monitor_id, checked_at__lt=cutoff)
                .order_by("checked_at")
                .values_list("pk", flat=True)[:batch_size]
            )
            if not ids:
                break
            deleted += CheckResult.objects.filter(pk__in=ids).delete()[0]
            if deleted >= max_rows:
                return deleted, False
            if len(ids) < batch_size:
                break
    return deleted, True

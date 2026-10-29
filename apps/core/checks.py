"""Startup checks (Django system checks): refuse to run with timing settings that could let
the reaper re-queue a job that is still running.

Every job's network time is capped by a total budget that safe_http enforces:
  run_check     <= MONITOR_MAX_TIMEOUT_SECONDS (each monitor's timeout_seconds, max 10)
  deliver_alert <= ALERT_TIMEOUT_SECONDS
plus a little database work. The lease must comfortably exceed the longest budget, so a
slow-but-alive job always finishes (or times out) long before its lease expires.
See docs/decisions.md D24.
"""

from django.conf import settings
from django.core.checks import Error, Warning, register

# The lease must be at least this many times the longest network budget. The headroom
# covers DB round trips, GC pauses and a busy machine.
LEASE_SAFETY_FACTOR = 3


def longest_job_budget():
    from apps.monitors.models import MONITOR_MAX_TIMEOUT_SECONDS

    return max(MONITOR_MAX_TIMEOUT_SECONDS, settings.ALERT_TIMEOUT_SECONDS)


@register()
def check_timing_settings(app_configs, **kwargs):
    from apps.monitors.models import MONITOR_MAX_TIMEOUT_SECONDS

    problems = []
    budget = longest_job_budget()
    if LEASE_SAFETY_FACTOR * budget > settings.JOB_LEASE_SECONDS:
        problems.append(
            Error(
                f"JOB_LEASE_SECONDS={settings.JOB_LEASE_SECONDS} is too short: the longest job "
                f"may take up to {budget}s (max(MONITOR_MAX_TIMEOUT_SECONDS="
                f"{MONITOR_MAX_TIMEOUT_SECONDS}, ALERT_TIMEOUT_SECONDS="
                f"{settings.ALERT_TIMEOUT_SECONDS})), and the lease must be at least "
                f"{LEASE_SAFETY_FACTOR}x that. A live job would be reaped and run twice.",
                hint=f"Set JOB_LEASE_SECONDS >= {LEASE_SAFETY_FACTOR * budget} "
                "or lower ALERT_TIMEOUT_SECONDS.",
                id="statusping.E001",
            )
        )
    if not 1 <= settings.MONITOR_DEFAULT_TIMEOUT_SECONDS <= MONITOR_MAX_TIMEOUT_SECONDS:
        problems.append(
            Error(
                f"MONITOR_DEFAULT_TIMEOUT_SECONDS={settings.MONITOR_DEFAULT_TIMEOUT_SECONDS} "
                f"must be between 1 and {MONITOR_MAX_TIMEOUT_SECONDS}.",
                id="statusping.E002",
            )
        )
    if budget > settings.WORKER_SHUTDOWN_TIMEOUT_SECONDS:
        problems.append(
            Warning(
                f"WORKER_SHUTDOWN_TIMEOUT_SECONDS={settings.WORKER_SHUTDOWN_TIMEOUT_SECONDS} is "
                f"shorter than the longest job ({budget}s): a graceful shutdown may abandon "
                "in-flight jobs to the reaper.",
                id="statusping.W001",
            )
        )
    return problems

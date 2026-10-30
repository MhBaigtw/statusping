"""D1: CheckResult retention (raw rows kept 30 days, deleted in batches by the scheduler)."""

from datetime import timedelta

import pytest
import time_machine
from django.core.cache import cache
from django.utils import timezone

from apps.core.checks import check_timing_settings
from apps.jobs.worker import RETENTION_NEXT_RUN_KEY, scheduler_tick
from apps.monitors.models import CheckResult
from apps.monitors.services import purge_check_results

from .factories import MonitorFactory

pytestmark = pytest.mark.django_db

NOW = timezone.now()


def rows(monitor, *ages_in_days):
    CheckResult.objects.bulk_create(
        CheckResult(monitor=monitor, checked_at=NOW - timedelta(days=age), ok=True)
        for age in ages_in_days
    )


def test_deletes_only_rows_older_than_the_retention_period(settings):
    settings.CHECK_RESULT_RETENTION_DAYS = 30
    a, b = MonitorFactory(), MonitorFactory()
    rows(a, 1, 29.9, 30.1, 45, 400)
    rows(b, 31, 2)
    assert purge_check_results(NOW) == (4, True)
    remaining = sorted(
        round((NOW - c.checked_at).total_seconds() / 86400, 1) for c in CheckResult.objects.all()
    )
    assert remaining == [1.0, 2.0, 29.9]


def test_batches_and_the_per_run_bound(settings):
    settings.CHECK_RESULT_RETENTION_DAYS = 30
    a, b = MonitorFactory(), MonitorFactory()
    rows(a, *[40 + n for n in range(7)])
    rows(b, *[40 + n for n in range(5)])
    # batches of 3, stop after 6 rows: the run reports it is not complete
    assert purge_check_results(NOW, batch_size=3, max_rows=6) == (6, False)
    assert purge_check_results(NOW, batch_size=3, max_rows=6) == (6, False)
    assert purge_check_results(NOW, batch_size=3, max_rows=6) == (0, True)
    assert not CheckResult.objects.exists()


def test_oldest_rows_go_first_within_a_monitor(settings):
    settings.CHECK_RESULT_RETENTION_DAYS = 30
    monitor = MonitorFactory()
    rows(monitor, 50, 60, 70)
    purge_check_results(NOW, batch_size=2, max_rows=2)
    [left] = CheckResult.objects.all()
    assert round((NOW - left.checked_at).days) == 50


def test_scheduler_runs_retention_at_most_once_per_interval(settings):
    settings.CHECK_RESULT_RETENTION_DAYS = 30
    settings.CHECK_RESULT_PURGE_INTERVAL_SECONDS = 3600
    monitor = MonitorFactory(next_check_at=NOW + timedelta(days=1))
    rows(monitor, 40)
    assert scheduler_tick()["purged_checks"] == 1
    rows(monitor, 41)
    assert scheduler_tick()["purged_checks"] == 0  # not due again for an hour
    with time_machine.travel(timezone.now() + timedelta(seconds=3601)):
        assert scheduler_tick()["purged_checks"] == 1


def test_a_backlog_continues_on_the_next_tick(settings):
    settings.CHECK_RESULT_RETENTION_DAYS = 30
    settings.CHECK_RESULT_PURGE_MAX_ROWS = 2
    settings.CHECK_RESULT_PURGE_BATCH_SIZE = 2
    monitor = MonitorFactory(next_check_at=NOW + timedelta(days=1))
    rows(monitor, 40, 41, 42, 43, 44)
    assert [scheduler_tick()["purged_checks"] for _ in range(4)] == [2, 2, 1, 0]
    assert cache.get(RETENTION_NEXT_RUN_KEY) > timezone.now()


def test_retention_still_runs_if_the_cache_is_down(settings, monkeypatch):
    settings.CHECK_RESULT_RETENTION_DAYS = 30
    monitor = MonitorFactory(next_check_at=NOW + timedelta(days=1))
    rows(monitor, 40)

    def broken(*args, **kwargs):
        raise RuntimeError("cache down")

    monkeypatch.setattr("apps.jobs.worker.cache.get", broken)
    monkeypatch.setattr("apps.jobs.worker.cache.set", broken)
    assert scheduler_tick()["purged_checks"] == 1


def test_retention_must_cover_the_longest_uptime_window(settings):
    settings.CHECK_RESULT_RETENTION_DAYS = 29
    assert [e.id for e in check_timing_settings(None)] == ["statusping.E003"]
    settings.CHECK_RESULT_RETENTION_DAYS = 30
    assert check_timing_settings(None) == []


def test_defaults(settings):
    assert settings.CHECK_RESULT_RETENTION_DAYS == 30
    assert settings.CHECK_RESULT_PURGE_INTERVAL_SECONDS == 3600

from io import StringIO

import pytest
from django.core.management import CommandError, call_command
from django.db.models import Count, Max, Min, Q

from apps.monitors.models import CheckResult, Incident, Monitor
from apps.organizations.models import Organization

pytestmark = pytest.mark.django_db


def seed(**options):
    out = StringIO()
    call_command("seed_bulk", orgs=2, monitors=3, days=2, seed=5, force=True, stdout=out, **options)
    return out.getvalue()


def test_refuses_without_debug_or_force(settings):
    settings.DEBUG = False
    with pytest.raises(CommandError, match="DEBUG"):
        call_command("seed_bulk", orgs=1, monitors=1, days=1)


def test_creates_consistent_data_and_is_idempotent():
    seed()
    assert set(Organization.objects.values_list("slug", flat=True)) == {"bulk5-01", "bulk5-02"}
    assert Monitor.objects.count() == 6
    rows = CheckResult.objects.count()
    assert rows > 0
    # Every monitor's checks are evenly spaced at its interval over the period.
    for monitor in Monitor.objects.annotate(
        n=Count("checks"), first=Min("checks__checked_at"), last=Max("checks__checked_at")
    ):
        assert (
            monitor.n
            == (monitor.last - monitor.first).total_seconds() // monitor.interval_seconds + 1
        )
        assert monitor.interval_seconds in (60, 300, 900)
        assert monitor.next_check_at > monitor.last  # never due: benchmarks must not check it
    # Failures carry an error and no response time; successes the opposite.
    assert not CheckResult.objects.filter(ok=True).exclude(error="").exists()
    assert not CheckResult.objects.filter(ok=False, error="").exists()
    assert not CheckResult.objects.filter(ok=False, response_ms__isnull=False).exists()
    assert set(CheckResult.objects.filter(ok=False).values_list("error", flat=True)) <= {
        "timeout",
        "unexpected_status",
    }
    # Incidents are resolved and every one covers failed checks.
    for incident in Incident.objects.all():
        assert incident.resolved_at > incident.started_at
        window = Q(checked_at__gte=incident.started_at, checked_at__lt=incident.resolved_at)
        assert not incident.monitor.checks.filter(window, ok=True).exists()
    assert "created 0 check results" in seed()  # same seed again: nothing new
    assert CheckResult.objects.count() == rows


def test_reset_replaces_the_seed():
    seed()
    out = seed(reset=True)
    assert "deleted" in out
    assert Organization.objects.filter(slug__startswith="bulk5-").count() == 2

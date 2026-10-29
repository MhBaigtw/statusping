"""Read endpoints for check results and incidents: scoping, permissions, filters."""

from datetime import timedelta

import pytest
from django.utils import timezone

from apps.monitors.models import CheckResult, Incident

from .conftest import READ_MATRIX, assert_error, check_status
from .factories import MonitorFactory

pytestmark = pytest.mark.django_db

ACTORS = ["owner", "admin", "viewer", "outsider", "anon"]


@pytest.fixture
def history(team):
    monitor = MonitorFactory(organization=team["org"])
    other = MonitorFactory(organization=team["org"])
    now = timezone.now()
    checks = [
        CheckResult.objects.create(
            monitor=monitor,
            checked_at=now - timedelta(minutes=n),
            ok=n % 2 == 0,
            status_code=200 if n % 2 == 0 else 500,
            error="" if n % 2 == 0 else "unexpected_status",
        )
        for n in range(6)
    ]
    CheckResult.objects.create(monitor=other, checked_at=now, ok=True)
    resolved = Incident.objects.create(
        monitor=monitor,
        started_at=now - timedelta(days=1),
        resolved_at=now - timedelta(hours=20),
        cause="timeout",
    )
    open_ = Incident.objects.create(monitor=other, started_at=now, cause="dns")
    return {
        "monitor": monitor,
        "other": other,
        "checks": checks,
        "resolved": resolved,
        "open": open_,
    }


@pytest.mark.parametrize("actor", ACTORS)
def test_checks_matrix(as_role, history, actor):
    url = f"/api/orgs/acme/monitors/{history['monitor'].pk}/checks/"
    check_status(as_role(actor).get(url), READ_MATRIX[actor])


def test_checks_newest_first_cursor_paginated(as_role, history):
    client = as_role("viewer")
    url = f"/api/orgs/acme/monitors/{history['monitor'].pk}/checks/?page_size=4"
    page = client.get(url).json()
    assert [r["id"] for r in page["results"]] == [c.pk for c in history["checks"][:4]]
    assert "count" not in page  # cursor pagination: no COUNT(*) on a high-volume table
    rest = client.get(page["next"]).json()
    assert [r["id"] for r in rest["results"]] == [c.pk for c in history["checks"][4:]]
    assert rest["next"] is None


def test_checks_filters(as_role, history):
    client = as_role("viewer")
    base = f"/api/orgs/acme/monitors/{history['monitor'].pk}/checks/"
    oks = client.get(f"{base}?ok=true").json()["results"]
    assert {r["id"] for r in oks} == {c.pk for c in history["checks"] if c.ok}
    fails = client.get(f"{base}?ok=false").json()["results"]
    assert all(r["error"] == "unexpected_status" for r in fails) and len(fails) == 3
    cutoff = history["checks"][2].checked_at
    after = client.get(base, {"checked_after": cutoff.isoformat()}).json()["results"]
    assert {r["id"] for r in after} == {c.pk for c in history["checks"][:3]}
    before = client.get(base, {"checked_before": cutoff.isoformat()}).json()["results"]
    assert {r["id"] for r in before} == {c.pk for c in history["checks"][2:]}
    assert_error(client.get(f"{base}?ok=yes"), 400, "validation_error")
    assert_error(client.get(f"{base}?checked_after=yesterday"), 400, "validation_error")


def test_checks_of_other_orgs_monitor_are_404(team, as_role):
    foreign = MonitorFactory(organization=team["other_org"])
    assert_error(as_role("owner").get(f"/api/orgs/acme/monitors/{foreign.pk}/checks/"), 404)
    assert_error(as_role("owner").get("/api/orgs/acme/monitors/999999/checks/"), 404)
    assert_error(as_role("owner").get(f"/api/orgs/acme/monitors/{foreign.pk}/incidents/"), 404)


@pytest.mark.parametrize("actor", ACTORS)
def test_incident_endpoints_matrix(as_role, history, actor):
    client = as_role(actor)
    check_status(client.get("/api/orgs/acme/incidents/"), READ_MATRIX[actor])
    check_status(client.get(f"/api/orgs/acme/incidents/{history['open'].pk}/"), READ_MATRIX[actor])
    url = f"/api/orgs/acme/monitors/{history['monitor'].pk}/incidents/"
    check_status(client.get(url), READ_MATRIX[actor])


def test_org_incidents_with_status_filter(as_role, history):
    client = as_role("viewer")

    def ids(query=""):
        return {i["id"] for i in client.get(f"/api/orgs/acme/incidents/{query}").json()["results"]}

    assert ids() == {history["open"].pk, history["resolved"].pk}
    assert ids("?status=open") == {history["open"].pk}
    assert ids("?status=resolved") == {history["resolved"].pk}
    assert_error(client.get("/api/orgs/acme/incidents/?status=closed"), 400)
    body = client.get(f"/api/orgs/acme/incidents/{history['open'].pk}/").json()
    assert body["is_open"] is True
    assert body["cause"] == "dns"
    assert body["monitor_name"] == history["other"].name


def test_monitor_incidents_only_that_monitor(as_role, history):
    url = f"/api/orgs/acme/monitors/{history['monitor'].pk}/incidents/"
    results = as_role("viewer").get(url).json()["results"]
    assert [i["id"] for i in results] == [history["resolved"].pk]


def test_incidents_are_read_only(as_role, history):
    url = f"/api/orgs/acme/incidents/{history['open'].pk}/"
    assert_error(as_role("owner").patch(url, {"cause": "ssl"}, format="json"), 405)
    assert_error(as_role("owner").delete(url), 405)


def test_other_orgs_incident_is_404(team, as_role):
    foreign = Incident.objects.create(
        monitor=MonitorFactory(organization=team["other_org"]),
        started_at=timezone.now(),
        cause="dns",
    )
    assert_error(as_role("owner").get(f"/api/orgs/acme/incidents/{foreign.pk}/"), 404)
    ids = {i["id"] for i in as_role("owner").get("/api/orgs/acme/incidents/").json()["results"]}
    assert foreign.pk not in ids

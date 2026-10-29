import pytest
from django.utils import timezone

from apps.monitors.models import Monitor

from .conftest import READ_MATRIX, WRITE_MATRIX, assert_error, check_status
from .factories import MonitorFactory

pytestmark = pytest.mark.django_db

ACTORS = ["owner", "admin", "viewer", "outsider", "anon"]
BASE = "/api/orgs/acme/monitors/"


@pytest.fixture
def monitor(team):
    return MonitorFactory(organization=team["org"], name="Homepage")


def test_create_with_defaults(as_role, team):
    resp = as_role("admin").post(BASE, {"name": "Site", "url": "https://example.com/"})
    assert resp.status_code == 201, resp.content
    body = resp.json()
    assert body["method"] == "GET"
    assert body["interval_seconds"] == 60
    assert body["timeout_seconds"] == 5
    assert body["expected_status"] == 200
    assert body["failure_threshold"] == 3
    assert body["recovery_threshold"] == 2
    assert body["last_status"] == "unknown"
    assert body["is_paused"] is False
    monitor = Monitor.objects.get(pk=body["id"])
    assert monitor.organization == team["org"]
    assert monitor.next_check_at <= timezone.now()  # due immediately


@pytest.mark.parametrize("actor", ACTORS)
def test_create_matrix(as_role, actor):
    resp = as_role(actor).post(BASE, {"name": "Site", "url": "https://example.com/"})
    check_status(resp, WRITE_MATRIX[actor])
    assert Monitor.objects.exists() == (WRITE_MATRIX[actor] == 2)


@pytest.mark.parametrize("actor", ACTORS)
def test_list_and_detail_matrix(as_role, monitor, actor):
    check_status(as_role(actor).get(BASE), READ_MATRIX[actor])
    check_status(as_role(actor).get(f"{BASE}{monitor.pk}/"), READ_MATRIX[actor])


@pytest.mark.parametrize("actor", ACTORS)
def test_update_matrix(as_role, monitor, actor):
    resp = as_role(actor).patch(f"{BASE}{monitor.pk}/", {"name": "Renamed"}, format="json")
    check_status(resp, WRITE_MATRIX[actor])
    monitor.refresh_from_db()
    assert (monitor.name == "Renamed") == (WRITE_MATRIX[actor] == 2)


@pytest.mark.parametrize("actor", ACTORS)
def test_delete_matrix(as_role, monitor, actor):
    check_status(as_role(actor).delete(f"{BASE}{monitor.pk}/"), WRITE_MATRIX[actor])
    assert Monitor.objects.filter(pk=monitor.pk).exists() == (WRITE_MATRIX[actor] != 2)


@pytest.mark.parametrize("actor", ACTORS)
def test_pause_resume_matrix(as_role, monitor, actor):
    check_status(as_role(actor).post(f"{BASE}{monitor.pk}/pause/"), WRITE_MATRIX[actor])
    monitor.refresh_from_db()
    assert monitor.is_paused == (WRITE_MATRIX[actor] == 2)


def test_other_orgs_monitor_is_404_even_for_its_url(team, as_role):
    foreign = MonitorFactory(organization=team["other_org"])
    owner = as_role("owner")
    assert_error(owner.get(f"{BASE}{foreign.pk}/"), 404)
    assert_error(owner.patch(f"{BASE}{foreign.pk}/", {"name": "x"}, format="json"), 404)
    assert_error(owner.delete(f"{BASE}{foreign.pk}/"), 404)
    assert_error(owner.post(f"{BASE}{foreign.pk}/pause/"), 404)
    assert owner.get(BASE).json()["count"] == 0


def test_pause_and_resume(as_role, monitor):
    owner = as_role("owner")
    assert owner.post(f"{BASE}{monitor.pk}/pause/").json()["is_paused"] is True
    later = timezone.now() + timezone.timedelta(hours=1)
    Monitor.objects.filter(pk=monitor.pk).update(next_check_at=later)
    body = owner.post(f"{BASE}{monitor.pk}/resume/").json()
    assert body["is_paused"] is False
    monitor.refresh_from_db()
    assert monitor.next_check_at <= timezone.now()  # checked right away after resuming
    # Resuming an active monitor changes nothing.
    assert owner.post(f"{BASE}{monitor.pk}/resume/").json()["is_paused"] is False


def test_state_fields_are_read_only(as_role, monitor):
    resp = as_role("owner").patch(
        f"{BASE}{monitor.pk}/",
        {"last_status": "down", "consecutive_failures": 9, "is_paused": True},
        format="json",
    )
    assert resp.status_code == 200
    monitor.refresh_from_db()
    assert (monitor.last_status, monitor.consecutive_failures, monitor.is_paused) == (
        "unknown",
        0,
        False,
    )


@pytest.mark.parametrize(
    "field,value",
    [
        ("interval_seconds", 59),
        ("interval_seconds", 3601),
        ("timeout_seconds", 0),
        ("timeout_seconds", 11),
        ("method", "POST"),
        ("expected_status", 99),
        ("expected_status", 600),
        ("failure_threshold", 0),
        ("recovery_threshold", 0),
        ("url", "not-a-url"),
    ],
)
def test_validation(as_role, field, value):
    payload = {"name": "Site", "url": "https://example.com/", field: value}
    error = assert_error(as_role("owner").post(BASE, payload, format="json"), 400)
    assert field in error["details"]


@pytest.mark.parametrize("value", [60, 3600])
def test_interval_bounds_are_inclusive(as_role, value):
    payload = {"name": "Site", "url": "https://example.com/", "interval_seconds": value}
    assert as_role("owner").post(BASE, payload, format="json").status_code == 201


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/",
        "http://169.254.169.254/latest/meta-data/",
        "http://internal.example/",
        "https://user:pw@example.com/",
        "https://example.com:22/",
        "ftp://example.com/",
    ],
)
def test_ssrf_blocked_urls_rejected_on_create(as_role, fake_dns, url):
    fake_dns["internal.example"] = ["10.0.0.8"]
    error = assert_error(as_role("owner").post(BASE, {"name": "x", "url": url}), 400)
    assert "url" in error["details"]
    assert not Monitor.objects.exists()


def test_unresolvable_url_rejected(as_role):
    error = assert_error(
        as_role("owner").post(BASE, {"name": "x", "url": "https://nx.example/"}), 400
    )
    assert "resolve" in error["details"]["url"][0]


def test_ssrf_checked_on_update_too(as_role, fake_dns, monitor):
    fake_dns["internal.example"] = ["192.168.0.2"]
    resp = as_role("owner").patch(
        f"{BASE}{monitor.pk}/", {"url": "http://internal.example/"}, format="json"
    )
    assert "url" in assert_error(resp, 400)["details"]
    monitor.refresh_from_db()
    assert monitor.url == "https://example.com/health"


def test_monitor_limit_per_org(as_role, team, settings):
    settings.MONITORS_PER_ORG = 2
    MonitorFactory.create_batch(2, organization=team["org"])
    MonitorFactory.create_batch(2, organization=team["other_org"])  # other orgs don't count
    resp = as_role("owner").post(BASE, {"name": "Third", "url": "https://example.com/"})
    error = assert_error(resp, 409, "monitor_limit_reached")
    assert error["details"] == {"limit": 2}
    assert Monitor.objects.filter(organization=team["org"]).count() == 2


def test_default_limit_is_20(settings):
    assert settings.MONITORS_PER_ORG == 20


def test_filters(as_role, team):
    up = MonitorFactory(organization=team["org"], last_status="up")
    down = MonitorFactory(organization=team["org"], last_status="down", is_paused=True)
    owner = as_role("owner")

    def ids(query):
        return {m["id"] for m in owner.get(f"{BASE}?{query}").json()["results"]}

    assert ids("last_status=up") == {up.pk}
    assert ids("last_status=down") == {down.pk}
    assert ids("is_paused=true") == {down.pk}
    assert ids("is_paused=false") == {up.pk}
    assert_error(owner.get(f"{BASE}?is_paused=maybe"), 400, "validation_error")
    assert_error(owner.get(f"{BASE}?last_status=sideways"), 400, "validation_error")


def test_pagination(as_role, team):
    MonitorFactory.create_batch(3, organization=team["org"])
    body = as_role("viewer").get(f"{BASE}?page_size=2").json()
    assert body["count"] == 3
    assert len(body["results"]) == 2
    assert body["next"]


def test_str(monitor):
    assert "Homepage" in str(monitor)

"""Public status page: content, privacy, uptime math, opt-out, caching, throttle, queries."""

from datetime import timedelta

import httpx
import pytest
from django.core.cache import cache
from django.utils import timezone
from rest_framework.throttling import SimpleRateThrottle

from apps.monitors import services as monitor_services
from apps.monitors.models import CheckResult, Incident, Monitor
from apps.statuspage.services import build_payload, cache_key, percent

from .conftest import assert_error
from .factories import PUBLIC_IP, MonitorFactory

pytestmark = pytest.mark.django_db

NOW = timezone.now().replace(microsecond=0)


def results(monitor, *specs):
    """specs: (age as timedelta, ok) pairs, relative to NOW."""
    CheckResult.objects.bulk_create(
        CheckResult(monitor=monitor, checked_at=NOW - age, ok=ok, error="" if ok else "timeout")
        for age, ok in specs
    )


def hours(n):
    return timedelta(hours=n)


def days(n):
    return timedelta(days=n)


@pytest.fixture
def page(team):
    org = team["org"]
    org.name = "Acme Corp"
    org.save()
    api = MonitorFactory(
        organization=org, name="API", url="https://example.com/secret-token", last_status="up"
    )
    MonitorFactory(organization=team["other_org"], name="Not ours")
    return {"org": org, "api": api}


# Content and privacy


def test_json_shape_and_no_private_data(anon, page, team):
    results(page["api"], (hours(1), True))
    resp = anon.get("/status/acme/")
    assert resp.status_code == 200
    body = resp.json()
    assert body["organization"] == {"name": "Acme Corp", "slug": "acme"}
    assert body["status"] == "operational"
    [monitor] = body["monitors"]
    assert set(monitor) == {"name", "status", "last_checked_at", "uptime", "incidents"}
    assert monitor["name"] == "API"
    assert monitor["status"] == "up"
    assert set(monitor["uptime"]) == {"24h", "7d", "30d"}
    text = resp.content.decode()
    assert "secret-token" not in text and "example.com" not in text  # never the URL
    assert "@" not in text  # no member emails
    assert "Not ours" not in text  # another org's monitor


def test_json_carries_the_uptime_numbers(anon, page):
    # Regression (found live): the serializer renamed its "_24h" fields after binding, so
    # they kept reading the "_24h" key and the JSON showed null for every window.
    results(page["api"], (hours(1), True), (hours(2), False), (days(3), True), (days(20), True))
    [monitor] = anon.get("/status/acme/").json()["monitors"]
    assert monitor["uptime"] == {"24h": 50.0, "7d": 66.67, "30d": 75.0}


def test_html_page_renders_the_same_data_accessibly(anon, page):
    results(page["api"], (hours(1), True), (hours(2), False))
    resp = anon.get("/status/acme")
    assert resp.status_code == 200
    assert resp["Content-Type"].startswith("text/html")
    html = resp.content.decode()
    assert '<html lang="en">' in html
    assert '<meta http-equiv="refresh" content="60">' in html
    assert 'name="viewport"' in html
    assert "Acme Corp" in html and "API" in html
    assert "All systems operational" in html
    assert "50.00%" in html  # 24 h uptime
    assert "secret-token" not in html and "example.com" not in html
    assert '<th scope="col">' not in html  # no incidents yet, so no table


def test_unknown_org_is_404_in_both_formats(anon):
    assert_error(anon.get("/status/nope/"), 404, "not_found")
    assert anon.get("/status/nope").status_code == 404


def test_no_auth_needed_and_bad_tokens_ignored(anon, page):
    anon.credentials(HTTP_AUTHORIZATION="Bearer garbage")
    assert anon.get("/status/acme/").status_code == 200


# Uptime math


def test_uptime_windows_with_hand_built_data(page, team):
    org = page["org"]
    a = page["api"]
    results(
        a,
        (hours(1), True),
        (hours(2), True),
        (hours(3), True),
        (hours(23), False),  # 24 h: 3 of 4
        (days(3), False),
        (days(3) + hours(1), False),
        (days(4), False),
        (days(6), False),  # 7 d: 3 of 8
        (days(20), True),
        (days(29), True),  # 30 d: 5 of 10
        (days(31), True),  # outside every window
        (days(60), False),
    )
    MonitorFactory(organization=org, name="B never checked")
    old_only = MonitorFactory(organization=org, name="C quiet lately", last_status="up")
    results(old_only, (days(10), True), (days(12), False))
    payload = build_payload(org, now=NOW)
    by_name = {m["name"]: m for m in payload["monitors"]}
    assert by_name["API"]["uptime"] == {"24h": 75.0, "7d": 37.5, "30d": 50.0}
    # No checks in a window is "no data", never 100%.
    assert by_name["B never checked"]["uptime"] == {"24h": None, "7d": None, "30d": None}
    assert by_name["B never checked"]["last_checked_at"] is None
    assert by_name["B never checked"]["status"] == "unknown"
    assert by_name["C quiet lately"]["uptime"] == {"24h": None, "7d": None, "30d": 50.0}
    assert by_name["API"]["last_checked_at"] == NOW - hours(1)


def test_window_start_is_inclusive_and_rounding(page):
    a = page["api"]
    results(a, (hours(24), False), (hours(1), True), (hours(2), True))
    payload = build_payload(page["org"], now=NOW)
    assert payload["monitors"][0]["uptime"]["24h"] == 66.67  # 2/3, the boundary check counts
    assert percent(1, 3) == 33.33
    assert percent(0, 0) is None
    assert percent(5, 5) == 100.0


# Statuses and incidents


def test_statuses_and_overall_state(page):
    org = page["org"]
    api = page["api"]
    payload = build_payload(org, now=NOW)
    assert payload["status"] == "operational"

    down = MonitorFactory(organization=org, name="Down one", last_status="down")
    Incident.objects.create(monitor=down, started_at=NOW - hours(1), cause="timeout")
    MonitorFactory(organization=org, name="Paused one", is_paused=True)
    # One failed check without an incident is still "up" publicly (flap protection).
    flappy = MonitorFactory(organization=org, name="Flappy", last_status="down")
    payload = build_payload(org, now=NOW)
    statuses = {m["name"]: m["status"] for m in payload["monitors"]}
    assert statuses == {"API": "up", "Down one": "down", "Paused one": "paused", "Flappy": "up"}
    assert payload["status"] == "degraded"
    assert [m["name"] for m in payload["monitors"]] == ["API", "Down one", "Flappy", "Paused one"]

    for monitor in (api, flappy):
        Incident.objects.create(monitor=monitor, started_at=NOW, cause="dns")
    assert build_payload(org, now=NOW)["status"] == "down"  # every unpaused monitor is down
    Monitor.objects.filter(organization=org).update(is_paused=True)
    assert build_payload(org, now=NOW)["status"] == "operational"  # nothing active


def test_last_five_incidents_newest_first_with_durations(page):
    api = page["api"]
    for n in range(7):
        start = NOW - days(7 - n)
        Incident.objects.create(
            monitor=api,
            started_at=start,
            resolved_at=start + timedelta(minutes=10 + n),
            cause="dns",
        )
    Incident.objects.create(monitor=api, started_at=NOW - hours(1), cause="timeout")  # open
    [monitor] = build_payload(page["org"], now=NOW)["monitors"]
    incidents = monitor["incidents"]
    assert len(incidents) == 5
    assert incidents[0] == {
        "started_at": NOW - hours(1),
        "resolved_at": None,
        "duration_seconds": None,
    }
    assert [i["duration_seconds"] for i in incidents[1:]] == [16 * 60, 15 * 60, 14 * 60, 13 * 60]
    assert monitor["status"] == "down"


def test_html_shows_incidents_and_ongoing(anon, page):
    start = NOW - hours(5)
    Incident.objects.create(
        monitor=page["api"],
        started_at=start,
        resolved_at=start + hours(3) + timedelta(minutes=5),
        cause="dns",
    )
    Incident.objects.create(monitor=page["api"], started_at=NOW - hours(1), cause="timeout")
    html = anon.get("/status/acme").content.decode()
    assert "Recent incidents" in html
    assert "3 h 5 min" in html and "ongoing" in html
    assert "All systems are down" in html


# Opt-out


def test_opted_out_org_is_404(anon, page):
    page["org"].status_page_public = False
    page["org"].save()
    assert_error(anon.get("/status/acme/"), 404)
    assert anon.get("/status/acme").status_code == 404


@pytest.mark.parametrize("actor,expected", [("owner", 200), ("admin", 403), ("viewer", 403)])
def test_only_owners_toggle_the_status_page(as_role, page, actor, expected):
    resp = as_role(actor).patch("/api/orgs/acme/", {"status_page_public": False}, format="json")
    assert resp.status_code == expected
    page["org"].refresh_from_db()
    assert page["org"].status_page_public is (expected != 200)
    if expected == 200:
        assert resp.json()["status_page_public"] is False


# Caching


def test_cache_hit_serves_the_stored_payload(anon, page):
    first = anon.get("/status/acme/").json()
    Monitor.objects.filter(pk=page["api"].pk).update(name="Changed behind the cache's back")
    assert anon.get("/status/acme/").json() == first  # hit: same payload, not rebuilt
    cache.delete(cache_key("acme"))
    assert anon.get("/status/acme/").json()["monitors"][0]["name"].startswith("Changed")


def test_cache_can_be_disabled(anon, page, settings):
    settings.STATUS_PAGE_CACHE_SECONDS = 0
    anon.get("/status/acme/")
    Monitor.objects.filter(pk=page["api"].pk).update(name="Fresh")
    assert anon.get("/status/acme/").json()["monitors"][0]["name"] == "Fresh"
    assert cache.get(cache_key("acme")) is None


def test_not_found_is_not_cached(anon, page):
    page["org"].status_page_public = False
    page["org"].save()
    anon.get("/status/acme/")
    assert cache.get(cache_key("acme")) is None


@pytest.fixture
def warm(anon, page):
    """Warm the cache, then return a function telling whether it is still warm."""

    def is_warm():
        return cache.get(cache_key("acme")) is not None

    anon.get("/status/acme/")
    assert is_warm()
    return is_warm


def test_invalidated_when_a_monitor_is_created(as_role, warm, django_capture_on_commit_callbacks):
    with django_capture_on_commit_callbacks(execute=True):
        resp = as_role("owner").post(
            "/api/orgs/acme/monitors/", {"name": "N", "url": "https://example.com/"}
        )
    assert resp.status_code == 201
    assert not warm()


def test_invalidated_when_a_monitor_is_deleted(
    as_role, page, warm, django_capture_on_commit_callbacks
):
    with django_capture_on_commit_callbacks(execute=True):
        assert (
            as_role("owner").delete(f"/api/orgs/acme/monitors/{page['api'].pk}/").status_code == 204
        )
    assert not warm()


@pytest.mark.parametrize("action", ["pause", "resume"])
def test_invalidated_on_pause_and_resume(
    as_role, page, warm, action, django_capture_on_commit_callbacks
):
    if action == "resume":
        Monitor.objects.filter(pk=page["api"].pk).update(is_paused=True)
    with django_capture_on_commit_callbacks(execute=True):
        resp = as_role("owner").post(f"/api/orgs/acme/monitors/{page['api'].pk}/{action}/")
    assert resp.status_code == 200
    assert not warm()


def test_invalidated_when_a_monitor_is_renamed(
    as_role, page, warm, django_capture_on_commit_callbacks
):
    with django_capture_on_commit_callbacks(execute=True):
        as_role("owner").patch(
            f"/api/orgs/acme/monitors/{page['api'].pk}/", {"name": "X"}, format="json"
        )
    assert not warm()


@pytest.mark.parametrize("payload", [{"status_page_public": False}, {"name": "Renamed"}])
def test_invalidated_when_the_org_changes(
    as_role, warm, payload, django_capture_on_commit_callbacks
):
    with django_capture_on_commit_callbacks(execute=True):
        assert as_role("owner").patch("/api/orgs/acme/", payload, format="json").status_code == 200
    assert not warm()


def test_invalidated_when_an_incident_opens_and_resolves(
    page, warm, http, django_capture_on_commit_callbacks
):
    Monitor.objects.filter(pk=page["api"].pk).update(failure_threshold=1, recovery_threshold=1)
    route = http.get(f"https://{PUBLIC_IP}/secret-token")
    route.side_effect = [httpx.Response(500), httpx.Response(200)]
    with django_capture_on_commit_callbacks(execute=True):
        monitor_services.run_check(page["api"].pk)
    assert Incident.objects.filter(resolved_at__isnull=True).count() == 1
    assert not warm()

    cache.set(cache_key("acme"), {"stale": True}, 30)  # warm it again before the recovery
    with django_capture_on_commit_callbacks(execute=True):
        monitor_services.run_check(page["api"].pk)
    assert Incident.objects.get().resolved_at is not None
    assert not warm()


def test_a_plain_check_does_not_invalidate(page, warm, http, django_capture_on_commit_callbacks):
    """Checks without an incident change rely on the short TTL instead (no churn per check)."""
    http.get(f"https://{PUBLIC_IP}/secret-token").respond(200)
    with django_capture_on_commit_callbacks(execute=True):
        monitor_services.run_check(page["api"].pk)
    assert warm()


def test_invalidation_waits_for_commit(as_role, page, warm, django_capture_on_commit_callbacks):
    with django_capture_on_commit_callbacks(execute=False) as callbacks:
        as_role("owner").post(f"/api/orgs/acme/monitors/{page['api'].pk}/pause/")
        assert warm()  # nothing deleted before commit
    assert callbacks


# Throttle and query count


def test_status_page_is_throttled_per_ip(anon, page, monkeypatch):
    monkeypatch.setattr(SimpleRateThrottle, "THROTTLE_RATES", {"status_page": "2/min"})
    assert anon.get("/status/acme/").status_code == 200
    assert anon.get("/status/acme").status_code == 200  # HTML shares the bucket
    error = assert_error(anon.get("/status/acme/"), 429, "throttled")
    assert error["details"]["retry_after_seconds"] > 0
    assert anon.get("/status/acme/", REMOTE_ADDR="10.9.9.9").status_code == 200


def test_default_throttle_rate():
    from config.settings import base

    assert base.REST_FRAMEWORK["DEFAULT_THROTTLE_RATES"]["status_page"] == "120/min"
    assert base.STATUS_PAGE_CACHE_SECONDS == 30


def test_query_count_is_constant(anon, page, django_assert_num_queries):
    org = page["org"]
    for n in range(5):
        monitor = MonitorFactory(organization=org, name=f"M{n}")
        results(monitor, (hours(1), True), (days(2), False))
        Incident.objects.create(monitor=monitor, started_at=NOW - days(1), cause="dns")
    # Miss: org + monitors + recent incidents (one prefetch) + one uptime aggregate.
    with django_assert_num_queries(4):
        assert anon.get("/status/acme/").status_code == 200
    # Hit: no database query at all (tests use the in-memory cache; the production DB
    # cache backend costs one SELECT on its cache table instead).
    with django_assert_num_queries(0):
        assert anon.get("/status/acme/").status_code == 200


def test_schema_documents_the_uptime_definition(anon):
    schema = anon.get("/api/schema/?format=json").json()
    assert "/status/{org_slug}/" in schema["paths"]
    text = str(schema["components"]["schemas"])
    assert "no data is not 100%" in text


def test_a_broken_cache_never_breaks_the_page_or_a_write(
    anon, page, monkeypatch, caplog, django_capture_on_commit_callbacks
):
    """Status-page caching is an optimisation: if it fails, serve uncached, keep writes working.

    (Only the status page's cache calls are broken here. DRF's throttles use the same
    cache, so a cache that is down for everything fails throttled endpoints; that is why
    createcachetable is a deploy step, see D25.)
    """
    import apps.statuspage.services as statuspage

    class BrokenCache:
        def get(self, *args):
            raise RuntimeError("cache table missing")

        set = delete = get

    monkeypatch.setattr(statuspage, "cache", BrokenCache())
    assert anon.get("/status/acme/").json()["monitors"][0]["name"] == "API"
    with django_capture_on_commit_callbacks(execute=True):
        monitor_services.pause_monitor(page["api"])  # invalidation fails quietly
    page["api"].refresh_from_db()
    assert page["api"].is_paused
    assert caplog.text.count("status page cache") == 3  # get, set, delete each logged

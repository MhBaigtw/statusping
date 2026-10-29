from datetime import timedelta
from io import StringIO

import pytest
from django.core.management import CommandError, call_command
from django.db import DatabaseError
from django.utils import timezone

from apps.accounts.models import User
from apps.jobs.models import WorkerHeartbeat
from apps.monitors.models import Monitor
from apps.organizations.models import Membership

from .conftest import assert_error
from .factories import UserFactory

pytestmark = pytest.mark.django_db


def beat(seconds_ago):
    now = timezone.now()
    WorkerHeartbeat.objects.create(
        worker_id=f"w{seconds_ago}",
        hostname="h",
        pid=1,
        threads=4,
        started_at=now,
        last_seen=now - timedelta(seconds=seconds_ago),
    )


def test_health_without_worker_is_degraded(anon):
    resp = anon.get("/api/health/")
    assert resp.status_code == 200
    assert resp.json() == {
        "status": "degraded",
        "database": "ok",
        "worker": {"status": "missing", "last_seen_seconds_ago": None},
    }


def test_health_reports_newest_heartbeat(anon):
    beat(120)
    beat(3)
    body = anon.get("/api/health/").json()
    assert body["status"] == "ok"
    assert body["worker"]["status"] == "ok"
    assert 2 <= body["worker"]["last_seen_seconds_ago"] <= 5


def test_health_stale_worker(anon):
    beat(120)
    body = anon.get("/api/health/").json()
    assert body["status"] == "degraded"
    assert body["worker"] == {"status": "stale", "last_seen_seconds_ago": 120}


def test_health_database_down_is_503(anon, monkeypatch):
    def broken():
        raise DatabaseError("down")

    monkeypatch.setattr("apps.core.views.worker_health", broken)
    assert_error(anon.get("/api/health/"), 503, "service_unavailable")


def test_unknown_url_uses_error_shape(anon):
    assert_error(anon.get("/api/nope/"), 404, "not_found")


def test_server_error_handler():
    from apps.core.views import server_error

    assert server_error(None).status_code == 500


def test_schema_and_docs(anon):
    assert anon.get("/api/schema/").status_code == 200
    assert anon.get("/api/docs/").status_code == 200


def test_seed_demo_is_idempotent(monkeypatch):
    monkeypatch.setenv("DEMO_PASSWORD", "demo-pass-for-tests")
    out = StringIO()
    call_command("seed_demo", stdout=out)
    call_command("seed_demo", stdout=out)
    assert "demo-pass-for-tests" not in out.getvalue()
    assert User.objects.count() == 2
    assert Membership.objects.count() == 2
    assert Monitor.objects.count() == 3
    owner = User.objects.get(email="owner@statusping.example")
    assert owner.check_password("demo-pass-for-tests")
    assert Membership.objects.get(user=owner).role == "owner"


def test_seed_demo_requires_password(monkeypatch):
    monkeypatch.delenv("DEMO_PASSWORD", raising=False)
    with pytest.raises(CommandError):
        call_command("seed_demo")


@pytest.mark.parametrize(
    "path",
    [
        "accounts/user",
        "organizations/organization",
        "organizations/membership",
        "monitors/monitor",
        "monitors/checkresult",
        "monitors/incident",
        "jobs/job",
        "jobs/workerheartbeat",
        "alerts/alertchannel",
        "alerts/alertdelivery",
    ],
)
def test_admin_changelists_load(client, path):
    admin = UserFactory(is_staff=True, is_superuser=True)
    client.force_login(admin)
    assert client.get(f"/admin/{path}/").status_code == 200
    assert client.get(f"/admin/{path}/add/").status_code in (200, 403)

"""Alert channels (API, URL rules, masking) and alert delivery (retries, idempotency)."""

import logging
from datetime import timedelta

import httpx
import pytest
from django.utils import timezone

from apps.alerts import services
from apps.alerts.models import AlertChannel, AlertDelivery
from apps.jobs import queue
from apps.jobs.models import Job
from apps.monitors.models import Incident

from .conftest import READ_MATRIX, WRITE_MATRIX, assert_error, check_status
from .factories import DISCORD_IP, SLACK_IP, AlertChannelFactory, MonitorFactory

pytestmark = pytest.mark.django_db

ACTORS = ["owner", "admin", "viewer", "outsider", "anon"]
BASE = "/api/orgs/acme/alert-channels/"
SECRET = "https://discord.com/api/webhooks/123/very-secret-token"
HOOK = f"https://{DISCORD_IP}/api/webhooks/123/very-secret-token"


@pytest.fixture
def channel(team):
    return AlertChannelFactory(organization=team["org"], webhook_url=SECRET, name="Ops")


# Channel API


@pytest.mark.parametrize("actor", ACTORS)
def test_create_matrix(as_role, actor):
    payload = {"kind": "discord", "name": "Ops", "webhook_url": SECRET}
    check_status(as_role(actor).post(BASE, payload), WRITE_MATRIX[actor])
    assert AlertChannel.objects.exists() == (WRITE_MATRIX[actor] == 2)


@pytest.mark.parametrize("actor", ACTORS)
def test_read_matrix(as_role, channel, actor):
    check_status(as_role(actor).get(BASE), READ_MATRIX[actor])
    check_status(as_role(actor).get(f"{BASE}{channel.pk}/"), READ_MATRIX[actor])


@pytest.mark.parametrize("actor", ACTORS)
def test_update_and_delete_matrix(as_role, channel, actor):
    resp = as_role(actor).patch(f"{BASE}{channel.pk}/", {"is_active": False}, format="json")
    check_status(resp, WRITE_MATRIX[actor])
    check_status(as_role(actor).delete(f"{BASE}{channel.pk}/"), WRITE_MATRIX[actor])
    assert AlertChannel.objects.filter(pk=channel.pk).exists() == (WRITE_MATRIX[actor] != 2)


@pytest.mark.parametrize("actor", ACTORS)
def test_test_action_matrix(as_role, channel, actor):
    resp = as_role(actor).post(f"{BASE}{channel.pk}/test/")
    check_status(resp, 202 if WRITE_MATRIX[actor] == 2 else WRITE_MATRIX[actor])
    assert Job.objects.exists() == (WRITE_MATRIX[actor] == 2)


def test_other_orgs_channel_is_404(team, as_role):
    foreign = AlertChannelFactory(organization=team["other_org"])
    assert_error(as_role("owner").get(f"{BASE}{foreign.pk}/"), 404)
    assert_error(as_role("owner").post(f"{BASE}{foreign.pk}/test/"), 404)


def test_webhook_url_is_masked_in_every_response(as_role, channel):
    owner = as_role("owner")
    created = owner.post(
        BASE,
        {"kind": "slack", "name": "S", "webhook_url": "https://hooks.slack.com/services/T/B/x"},
    )
    responses = [
        created,
        owner.get(BASE),
        owner.get(f"{BASE}{channel.pk}/"),
        owner.patch(f"{BASE}{channel.pk}/", {"name": "Renamed"}, format="json"),
    ]
    for resp in responses:
        assert resp.status_code in (200, 201), resp.content
        text = resp.content.decode()
        assert "very-secret-token" not in text
        assert "services/T/B/x" not in text
    assert created.json()["webhook_url_masked"] == "https://hooks.slack.com/****"
    assert "webhook_url" not in created.json()
    assert owner.get(f"{BASE}{channel.pk}/").json()["webhook_url_masked"] == (
        "https://discord.com/****"
    )


@pytest.mark.parametrize(
    "kind,url,message",
    [
        ("discord", "http://discord.com/api/webhooks/1/x", "https"),
        ("discord", "https://evil.example/api/webhooks/1/x", "discord.com"),
        ("discord", "https://hooks.slack.com/services/x", "discord.com"),
        ("slack", "https://discord.com/api/webhooks/1/x", "hooks.slack.com"),
        ("discord", "https://user:pw@discord.com/api/webhooks/1/x", "credentials"),
        ("discord", "https://discord.com:22/api/webhooks/1/x", "ports"),
        ("discord", "https://127.0.0.1/hook", "discord.com"),
    ],
)
def test_webhook_url_rules(as_role, kind, url, message):
    resp = as_role("owner").post(BASE, {"kind": kind, "name": "x", "webhook_url": url})
    error = assert_error(resp, 400, "validation_error")
    assert message in error["details"]["webhook_url"][0]


def test_webhook_host_that_resolves_privately_is_rejected(as_role, fake_dns):
    fake_dns["discord.com"] = ["10.1.1.1"]
    resp = as_role("owner").post(BASE, {"kind": "discord", "name": "x", "webhook_url": SECRET})
    assert "non-public" in assert_error(resp, 400)["details"]["webhook_url"][0]


def test_discordapp_and_slack_hosts_allowed(as_role, fake_dns):
    fake_dns["discordapp.com"] = [DISCORD_IP]
    owner = as_role("owner")
    payload = {
        "kind": "discord",
        "name": "a",
        "webhook_url": "https://discordapp.com/api/webhooks/1/t",
    }
    assert owner.post(BASE, payload).status_code == 201
    payload = {
        "kind": "slack",
        "name": "b",
        "webhook_url": "https://hooks.slack.com/services/A/B/C",
    }
    assert owner.post(BASE, payload).status_code == 201


def test_update_revalidates_the_url(as_role, channel):
    resp = as_role("owner").patch(
        f"{BASE}{channel.pk}/", {"webhook_url": "https://evil.example/x"}, format="json"
    )
    assert_error(resp, 400)
    resp = as_role("owner").patch(f"{BASE}{channel.pk}/", {"kind": "slack"}, format="json")
    assert_error(resp, 400)  # a discord URL is not a valid slack webhook
    channel.refresh_from_db()
    assert (channel.webhook_url, channel.kind) == (SECRET, "discord")


def test_loopback_webhook_allowed_only_in_debug_e2e_mode(settings):
    settings.SSRF_ALLOW_LOOPBACK = True
    with pytest.raises(Exception, match="https"):
        services.validate_webhook_url("discord", "http://127.0.0.1:9000/hook")
    settings.DEBUG = True
    services.validate_webhook_url("discord", "http://127.0.0.1:9000/hook")


def test_test_action_enqueues_job_and_handler_sends(as_role, channel, http):
    route = http.post(HOOK).respond(204)
    resp = as_role("admin").post(f"{BASE}{channel.pk}/test/")
    job = Job.objects.get(pk=resp.json()["job_id"])
    assert (job.kind, job.payload) == ("send_test_alert", {"channel_id": channel.pk})
    [claimed] = queue.claim("w1")
    assert queue.run_job(claimed, "w1") == Job.Status.SUCCEEDED
    assert "test message" in route.calls.last.request.content.decode()


def test_test_alert_retries_and_failures(channel, http):
    http.post(HOOK).respond(503)
    queue.enqueue("send_test_alert", {"channel_id": channel.pk})
    [claimed] = queue.claim("w1")
    assert queue.run_job(claimed, "w1") == Job.Status.QUEUED
    http.post(HOOK).respond(404)
    Job.objects.update(run_after=timezone.now())
    [claimed] = queue.claim("w1")
    assert queue.run_job(claimed, "w1") == Job.Status.FAILED
    services.send_test_alert(999_999)  # deleted channel: nothing to do


# Delivery


@pytest.fixture
def incident(team):
    monitor = MonitorFactory(organization=team["org"], name="Shop <!channel>")
    start = timezone.now() - timedelta(minutes=12)
    return Incident.objects.create(monitor=monitor, started_at=start, cause="timeout")


@pytest.fixture
def delivery(incident, channel):
    services.queue_alerts(incident, "opened")
    return AlertDelivery.objects.get()


def run_delivery_job(attempt_until=None):
    """Claim the (single) due deliver_alert job and run it; return the new job status."""
    Job.objects.filter(status="queued").update(run_after=timezone.now())
    [claimed] = queue.claim("w1")
    return queue.run_job(claimed, "w1")


def test_delivery_sends_once_and_marks_sent(delivery, http):
    route = http.post(HOOK).respond(204)
    assert run_delivery_job() == Job.Status.SUCCEEDED
    delivery.refresh_from_db()
    assert (delivery.status, delivery.attempts, delivery.last_error) == ("sent", 1, "")
    assert delivery.sent_at is not None
    body = route.calls.last.request.content.decode()
    assert "Shop" in body and "Timeout" in body and "DOWN" in body
    assert '"allowed_mentions": {"parse": []}' in body or '"allowed_mentions":{"parse":[]}' in body


def test_running_the_delivery_job_twice_sends_one_webhook(delivery, http):
    route = http.post(HOOK).respond(204)
    services.deliver_alert(delivery.pk, final_attempt=False)
    services.deliver_alert(delivery.pk, final_attempt=False)
    assert route.call_count == 1
    delivery.refresh_from_db()
    assert delivery.attempts == 1


def test_database_refuses_a_duplicate_delivery(delivery):
    from django.db import IntegrityError

    with pytest.raises(IntegrityError):
        AlertDelivery.objects.create(
            incident=delivery.incident, channel=delivery.channel, event="opened"
        )


@pytest.mark.parametrize(
    "first_failure",
    [
        httpx.Response(500),
        httpx.Response(502),
        httpx.ReadTimeout("slow"),
        httpx.ConnectError("refused"),
    ],
)
def test_retryable_failure_then_success(delivery, http, first_failure):
    route = http.post(HOOK)
    route.side_effect = [first_failure, httpx.Response(204)]
    assert run_delivery_job() == Job.Status.QUEUED
    delivery.refresh_from_db()
    assert (delivery.status, delivery.attempts) == ("pending", 1)
    assert delivery.last_error
    assert run_delivery_job() == Job.Status.SUCCEEDED
    delivery.refresh_from_db()
    assert (delivery.status, delivery.attempts, delivery.last_error) == ("sent", 2, "")
    assert route.call_count == 2


def test_429_honours_retry_after(delivery, http):
    route = http.post(HOOK)
    route.side_effect = [httpx.Response(429, headers={"Retry-After": "42"}), httpx.Response(204)]
    before = timezone.now()
    assert run_delivery_job() == Job.Status.QUEUED
    job = Job.objects.get()
    assert 41 <= (job.run_after - before).total_seconds() <= 43
    assert job.last_error == "HTTP 429 (rate limited)"
    assert run_delivery_job() == Job.Status.SUCCEEDED


def test_parse_retry_after_forms():
    now = timezone.now().replace(microsecond=0)
    assert services.parse_retry_after("10", now) == now + timedelta(seconds=10)
    date = now + timedelta(minutes=2)
    assert services.parse_retry_after(date.strftime("%a, %d %b %Y %H:%M:%S GMT"), now) == date
    assert services.parse_retry_after("soon", now) is None
    assert services.parse_retry_after(None, now) is None


@pytest.mark.parametrize("status", [400, 401, 403, 404])
def test_other_4xx_is_not_retried(delivery, http, status):
    route = http.post(HOOK).respond(status)
    assert run_delivery_job() == Job.Status.FAILED
    delivery.refresh_from_db()
    assert delivery.status == "failed"
    assert delivery.last_error == f"HTTP {status} (not retried)"
    assert route.call_count == 1
    assert not Job.objects.filter(status="queued").exists()


def test_gives_up_after_five_attempts(delivery, http):
    route = http.post(HOOK).respond(500)
    statuses = [run_delivery_job() for _ in range(5)]
    assert statuses == ["queued"] * 4 + ["dead"]
    delivery.refresh_from_db()
    assert (delivery.status, delivery.attempts, delivery.last_error) == ("failed", 5, "HTTP 500")
    assert route.call_count == 5


def test_inactive_channel_is_not_sent(delivery, http):
    AlertChannel.objects.update(is_active=False)
    services.deliver_alert(delivery.pk, final_attempt=False)
    delivery.refresh_from_db()
    assert delivery.status == "failed"
    assert not http.calls
    assert services.deliver_alert(999_999, final_attempt=False) is None


def test_webhook_that_became_private_is_failed_not_sent(delivery, fake_dns, http):
    fake_dns["discord.com"] = ["10.0.0.1"]
    assert run_delivery_job() == Job.Status.FAILED
    delivery.refresh_from_db()
    assert delivery.status == "failed"
    assert "blocked" in delivery.last_error
    assert not http.calls


def test_dns_failure_is_retryable(delivery, fake_dns):
    del fake_dns["discord.com"]
    assert run_delivery_job() == Job.Status.QUEUED


def test_webhook_url_never_appears_in_logs_or_errors(delivery, http, caplog):
    caplog.set_level(logging.DEBUG)
    http.post(HOOK).mock(side_effect=[httpx.ConnectError("refused"), httpx.Response(500)])
    run_delivery_job()
    run_delivery_job()
    http.post(HOOK).respond(204)
    run_delivery_job()
    assert caplog.records, "expected some log output to inspect"
    assert "very-secret-token" not in caplog.text
    delivery.refresh_from_db()
    for job in Job.objects.all():
        assert "very-secret-token" not in job.last_error
    assert "very-secret-token" not in delivery.last_error


def test_resolved_message_and_slack_escaping(team, incident, http):
    slack = AlertChannelFactory(
        organization=team["org"], kind="slack", webhook_url="https://hooks.slack.com/services/A/B/C"
    )
    incident.resolved_at = incident.started_at + timedelta(minutes=12)
    incident.save()
    services.queue_alerts(incident, "resolved")
    route = http.post(f"https://{SLACK_IP}/services/A/B/C").respond(200)
    services.deliver_alert(AlertDelivery.objects.get(channel=slack).pk, final_attempt=False)
    body = route.calls.last.request.content.decode()
    assert "back up" in body and "12 min" in body
    assert "<!channel>" not in body and "&lt;!channel&gt;" in body


def test_masked_url_and_strs(channel, delivery):
    assert channel.masked_webhook_url == "https://discord.com/****"
    assert AlertChannel(webhook_url="::nonsense").masked_webhook_url == "****"
    assert "Ops" in str(channel)
    assert "opened" in str(delivery)


@pytest.mark.parametrize("actor", ACTORS)
def test_deliveries_endpoint_matrix(as_role, delivery, actor):
    url = f"/api/orgs/acme/incidents/{delivery.incident_id}/deliveries/"
    resp = as_role(actor).get(url)
    check_status(resp, READ_MATRIX[actor])
    if resp.status_code == 200:
        [row] = resp.json()["results"]
        assert (row["event"], row["status"], row["channel_name"]) == ("opened", "pending", "Ops")
        assert "very-secret-token" not in resp.content.decode()


def test_other_orgs_incident_deliveries_404(team, as_role):
    foreign = Incident.objects.create(
        monitor=MonitorFactory(organization=team["other_org"]),
        started_at=timezone.now(),
        cause="dns",
    )
    assert_error(as_role("owner").get(f"/api/orgs/acme/incidents/{foreign.pk}/deliveries/"), 404)

"""Dispatching due checks, running a check, and incidents with flap protection."""

import ssl
import time
from datetime import timedelta

import httpx
import pytest
from django.db import IntegrityError
from django.utils import timezone

from apps.alerts.models import AlertDelivery
from apps.jobs import queue
from apps.jobs.models import Job
from apps.monitors import services
from apps.monitors.models import CheckResult, Incident, Monitor

from .concurrency import HoldLock, run_in_threads
from .factories import PUBLIC_IP, AlertChannelFactory, MonitorFactory

TARGET = f"https://{PUBLIC_IP}/health"


def past(seconds=1):
    return timezone.now() - timedelta(seconds=seconds)


# Dispatcher


@pytest.mark.django_db
def test_dispatch_claims_only_due_unpaused_monitors():
    due = MonitorFactory(next_check_at=past(), interval_seconds=120)
    MonitorFactory(next_check_at=past(), is_paused=True)
    MonitorFactory(next_check_at=timezone.now() + timedelta(seconds=30))
    before = timezone.now()
    assert services.dispatch_due_checks() == [due.pk]
    due.refresh_from_db()
    advanced = (due.next_check_at - before).total_seconds()
    assert 119 <= advanced <= 121
    [job] = Job.objects.all()
    assert (job.kind, job.payload, job.dedupe_key) == (
        "run_check",
        {"monitor_id": due.pk},
        f"run_check:{due.pk}",
    )
    assert job.max_attempts == 1
    assert services.dispatch_due_checks() == []  # no longer due


@pytest.mark.django_db
def test_dispatch_respects_batch_size_oldest_first():
    monitors = [MonitorFactory(next_check_at=past(10 - n)) for n in range(5)]
    assert services.dispatch_due_checks(batch_size=2) == [monitors[0].pk, monitors[1].pk]


@pytest.mark.django_db
def test_dispatch_does_not_pile_up_jobs_for_a_slow_monitor():
    monitor = MonitorFactory(next_check_at=past())
    services.dispatch_due_checks()
    Monitor.objects.filter(pk=monitor.pk).update(next_check_at=past())
    services.dispatch_due_checks()  # the first check hasn't run yet
    assert Job.objects.count() == 1


@pytest.mark.django_db(transaction=True)
def test_claim_and_enqueue_are_atomic(monkeypatch):
    monitors = [MonitorFactory(next_check_at=past(5 - n)) for n in range(3)]
    real_enqueue = services.enqueue
    calls = []

    def flaky_enqueue(*args, **kwargs):
        calls.append(1)
        if len(calls) == 2:
            raise RuntimeError("database hiccup")
        return real_enqueue(*args, **kwargs)

    monkeypatch.setattr(services, "enqueue", flaky_enqueue)
    with pytest.raises(RuntimeError):
        services.dispatch_due_checks()
    assert Job.objects.count() == 0  # the first job rolled back with the claim
    for monitor in monitors:
        old = monitor.next_check_at
        monitor.refresh_from_db()
        assert monitor.next_check_at == old  # still due: nothing was lost
    monkeypatch.setattr(services, "enqueue", real_enqueue)
    assert len(services.dispatch_due_checks()) == 3
    assert Job.objects.count() == 3


@pytest.mark.django_db(transaction=True)
def test_concurrent_dispatchers_never_claim_the_same_monitor(monkeypatch):
    ids = {MonitorFactory(next_check_at=past()).pk for _ in range(30)}
    real_enqueue = services.enqueue

    def slow_enqueue(*args, **kwargs):
        time.sleep(0.02)  # keep each dispatcher's transaction open so they overlap
        return real_enqueue(*args, **kwargs)

    monkeypatch.setattr(services, "enqueue", slow_enqueue)
    results = run_in_threads(lambda i: services.dispatch_due_checks(batch_size=10), 4)
    claimed = [pk for result in results for pk in result]
    assert len(claimed) == len(set(claimed)) == 30, results
    assert set(claimed) == ids
    assert Job.objects.count() == 30


@pytest.mark.django_db(transaction=True)
def test_dispatch_skips_a_locked_monitor_without_waiting():
    locked = MonitorFactory(next_check_at=past(10))
    free = MonitorFactory(next_check_at=past(5))

    def lock_monitor():
        list(Monitor.objects.select_for_update().filter(pk=locked.pk))

    with HoldLock(lock_monitor):
        started = time.monotonic()
        claimed = services.dispatch_due_checks()
        elapsed = time.monotonic() - started
    assert claimed == [free.pk]
    assert elapsed < 2, f"dispatch waited {elapsed:.1f}s for a locked monitor"
    assert services.dispatch_due_checks() == [locked.pk]  # picked up next tick


# Check outcomes


@pytest.fixture
def monitor(db):
    return MonitorFactory(expected_status=200)


def test_ok_check(monitor, http):
    http.get(TARGET).respond(200)
    result = services.run_check(monitor.pk)
    assert (result.ok, result.status_code, result.error) == (True, 200, "")
    assert isinstance(result.response_ms, int)
    monitor.refresh_from_db()
    assert (monitor.last_status, monitor.consecutive_successes) == ("up", 1)


def test_head_method(monitor, http):
    Monitor.objects.filter(pk=monitor.pk).update(method="HEAD")
    route = http.head(TARGET).respond(200)
    assert services.run_check(monitor.pk).ok
    assert route.called


def test_wrong_status(monitor, http):
    http.get(TARGET).respond(503)
    result = services.run_check(monitor.pk)
    assert (result.ok, result.status_code, result.error) == (False, 503, "unexpected_status")
    monitor.refresh_from_db()
    assert (monitor.last_status, monitor.consecutive_failures) == ("down", 1)


def test_expected_status_other_than_200(monitor, http):
    Monitor.objects.filter(pk=monitor.pk).update(expected_status=301)
    http.get(TARGET).respond(301, headers={"Location": "/elsewhere"})
    assert services.run_check(monitor.pk).ok


@pytest.mark.parametrize(
    "exc,error",
    [
        (httpx.ConnectTimeout("connect timed out"), "timeout"),
        (httpx.ReadTimeout("read timed out"), "timeout"),
        (httpx.ConnectError("connection refused"), "connection"),
        (httpx.RemoteProtocolError("server hung up"), "connection"),
    ],
)
def test_network_failures(monitor, http, exc, error):
    http.get(TARGET).mock(side_effect=exc)
    result = services.run_check(monitor.pk)
    assert (result.ok, result.error, result.status_code, result.response_ms) == (
        False,
        error,
        None,
        None,
    )


def tls_error():
    """What httpx raises on a bad certificate: httpx <- httpcore <- ssl (built the same way)."""
    import httpcore

    try:
        try:
            try:
                raise ssl.SSLCertVerificationError("certificate verify failed")
            except ssl.SSLError as exc:
                raise httpcore.ConnectError(str(exc)) from exc
        except httpcore.ConnectError as exc:
            raise httpx.ConnectError(str(exc)) from exc
    except httpx.ConnectError as exc:
        return exc


def test_tls_failure(monitor, monkeypatch):
    # respx replaces a side effect's __cause__, so inject the error one layer up.
    def bad_certificate(*args, **kwargs):
        raise tls_error()

    monkeypatch.setattr(services.safe_http, "send", bad_certificate)
    assert services.run_check(monitor.pk).error == "ssl"


def test_tls_classifier_survives_cyclic_chains():
    assert services._is_tls_error(tls_error())
    a, b = httpx.ConnectError("a"), RuntimeError("b")
    a.__cause__, b.__cause__ = b, a  # respx produces exactly this kind of cycle
    assert services._is_tls_error(a) is False


def test_dns_failure(monitor, fake_dns, http):
    del fake_dns["example.com"]
    assert services.run_check(monitor.pk).error == "dns"
    assert not http.calls


def test_target_that_became_private_is_blocked_at_check_time(monitor, fake_dns, http):
    fake_dns["example.com"] = ["10.0.0.9"]  # DNS changed after the monitor was created
    result = services.run_check(monitor.pk)
    assert (result.ok, result.error) == (False, "blocked_target")
    assert not http.calls  # nothing was sent


def test_running_the_same_job_twice_records_one_result(monitor, http):
    route = http.get(TARGET).respond(200)
    assert services.run_check(monitor.pk, job_id=41) is not None
    assert services.run_check(monitor.pk, job_id=41) is None
    assert CheckResult.objects.count() == 1
    assert route.call_count == 1
    monitor.refresh_from_db()
    assert monitor.consecutive_successes == 1


def test_concurrent_duplicate_run_is_dropped_by_the_unique_job_id(monitor, http):
    """Both runs passed the early check; the second insert hits the unique job_id."""

    def respond(request):
        CheckResult.objects.create(monitor=monitor, checked_at=timezone.now(), ok=True, job_id=7)
        return httpx.Response(200)

    http.get(TARGET).mock(side_effect=respond)
    assert services.run_check(monitor.pk, job_id=7) is None
    assert CheckResult.objects.count() == 1
    monitor.refresh_from_db()
    assert monitor.consecutive_successes == 0  # the duplicate changed nothing


def test_stale_result_for_deleted_monitor_is_dropped(monitor, http):
    def delete_meanwhile(request):
        Monitor.objects.filter(pk=monitor.pk).delete()
        return httpx.Response(200)

    http.get(TARGET).mock(side_effect=delete_meanwhile)
    assert services.run_check(monitor.pk) is None
    assert not CheckResult.objects.exists()


def test_stale_result_for_paused_monitor_is_dropped(monitor, http):
    def pause_meanwhile(request):
        Monitor.objects.filter(pk=monitor.pk).update(is_paused=True)
        return httpx.Response(500)

    http.get(TARGET).mock(side_effect=pause_meanwhile)
    assert services.run_check(monitor.pk) is None
    assert not CheckResult.objects.exists()
    monitor.refresh_from_db()
    assert monitor.consecutive_failures == 0


def test_paused_or_missing_monitor_is_not_checked(monitor, http):
    Monitor.objects.filter(pk=monitor.pk).update(is_paused=True)
    assert services.run_check(monitor.pk) is None
    assert services.run_check(999_999) is None
    assert not http.calls


def test_run_check_job_through_the_queue(monitor, http):
    http.get(TARGET).respond(200)
    job = queue.enqueue("run_check", {"monitor_id": monitor.pk})
    [claimed] = queue.claim("w1")
    assert queue.run_job(claimed, "w1") == Job.Status.SUCCEEDED
    assert CheckResult.objects.get().job_id == job.pk


# Incidents


@pytest.fixture
def site(http):
    """site.status = N makes the next checks answer N (0 = connection refused)."""

    class Site:
        status = 200

    def respond(request):
        if Site.status == 0:
            raise httpx.ConnectError("refused")
        return httpx.Response(Site.status)

    http.get(TARGET).mock(side_effect=respond)
    return Site


def run(monitor, site, *statuses):
    for status in statuses:
        site.status = status
        services.run_check(monitor.pk)
    monitor.refresh_from_db()


def open_incidents(monitor):
    return Incident.objects.filter(monitor=monitor, resolved_at__isnull=True)


def test_incident_opens_exactly_at_failure_threshold(monitor, site):
    run(monitor, site, 500, 500)
    assert not Incident.objects.exists()  # 2 < threshold 3
    run(monitor, site, 0)
    [incident] = Incident.objects.all()
    assert incident.cause == "connection"  # the cause of the check that crossed it
    first_failure = CheckResult.objects.order_by("checked_at").first()
    assert incident.started_at == first_failure.checked_at  # downtime began then
    run(monitor, site, 500, 500, 500)
    assert Incident.objects.count() == 1  # still the one incident
    assert monitor.consecutive_failures == 6


def test_threshold_of_one(monitor, site):
    Monitor.objects.filter(pk=monitor.pk).update(failure_threshold=1)
    run(monitor, site, 500)
    assert open_incidents(monitor).count() == 1


def test_incident_resolves_only_after_recovery_threshold(monitor, site):
    run(monitor, site, 500, 500, 500)
    run(monitor, site, 200)
    assert open_incidents(monitor).count() == 1  # 1 < recovery threshold 2
    run(monitor, site, 500, 200)
    assert open_incidents(monitor).count() == 1  # the success streak was broken
    run(monitor, site, 200)
    assert not open_incidents(monitor).exists()
    incident = Incident.objects.get()
    assert incident.resolved_at == CheckResult.objects.order_by("-checked_at").first().checked_at
    assert monitor.last_status == "up"


def test_flapping_site_never_opens_an_incident(monitor, site):
    run(monitor, site, *([500, 200] * 10))
    assert not Incident.objects.exists()
    run(monitor, site, 500, 500, 200, 500, 500, 200)
    assert not Incident.objects.exists()


def test_new_incident_after_previous_resolved(monitor, site):
    run(monitor, site, 500, 500, 500, 200, 200, 500, 500, 500)
    assert Incident.objects.count() == 2
    assert open_incidents(monitor).count() == 1


def test_database_allows_only_one_open_incident(monitor):
    Incident.objects.create(monitor=monitor, started_at=timezone.now(), cause="timeout")
    with pytest.raises(IntegrityError):
        Incident.objects.create(monitor=monitor, started_at=timezone.now(), cause="timeout")


def test_alerts_queued_once_per_active_channel_per_event(monitor, site):
    channels = AlertChannelFactory.create_batch(2, organization=monitor.organization)
    AlertChannelFactory(organization=monitor.organization, is_active=False)
    AlertChannelFactory()  # another org's channel
    run(monitor, site, 500, 500, 500, 500)
    opened = AlertDelivery.objects.filter(event="opened")
    assert {d.channel_id for d in opened} == {c.pk for c in channels}
    assert all(d.status == "pending" for d in opened)
    jobs = Job.objects.filter(kind="deliver_alert")
    assert {j.payload["delivery_id"] for j in jobs} == {d.pk for d in opened}
    assert all(j.max_attempts == 5 for j in jobs)
    run(monitor, site, 200, 200, 200)
    assert AlertDelivery.objects.filter(event="resolved").count() == 2
    assert Job.objects.filter(kind="deliver_alert").count() == 4


def test_queue_alerts_is_idempotent(monitor):
    from apps.alerts.services import queue_alerts

    AlertChannelFactory(organization=monitor.organization)
    incident = Incident.objects.create(monitor=monitor, started_at=timezone.now(), cause="dns")
    queue_alerts(incident, "opened")
    queue_alerts(incident, "opened")
    assert AlertDelivery.objects.count() == 1
    assert Job.objects.count() == 1


def test_incident_and_its_alerts_roll_back_together(monitor, site, monkeypatch):
    """If queuing alerts fails, the incident (and the check) are not committed either."""
    AlertChannelFactory(organization=monitor.organization)
    run(monitor, site, 500, 500)

    def broken(*args, **kwargs):
        raise RuntimeError("enqueue failed")

    monkeypatch.setattr("apps.alerts.services.enqueue", broken)
    site.status = 500
    with pytest.raises(RuntimeError):
        services.run_check(monitor.pk)
    assert not Incident.objects.exists()
    assert not AlertDelivery.objects.exists()
    assert CheckResult.objects.count() == 2
    monitor.refresh_from_db()
    assert monitor.consecutive_failures == 2


def test_model_strs(monitor):
    incident = Incident.objects.create(monitor=monitor, started_at=timezone.now(), cause="dns")
    result = CheckResult.objects.create(monitor=monitor, checked_at=timezone.now(), ok=False)
    assert "open" in str(incident)
    assert str(monitor.pk) in str(result)

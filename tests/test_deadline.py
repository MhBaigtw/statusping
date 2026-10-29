"""A1-A3: one total time budget per outbound request, so no check can outlive its lease.

These use real sockets against misbehaving servers on 127.0.0.1 (allowed through the
DEBUG-only loopback switch for the duration of each test), not mocks: the point is to
prove the budget holds against real slow I/O.
"""

import contextlib
import threading
import time

import httpx
import pytest
from django.core.checks import run_checks
from django.core.management import call_command
from django.core.management.base import SystemCheckError

from apps.core import safe_http
from apps.core.checks import LEASE_SAFETY_FACTOR, check_timing_settings
from apps.jobs import queue
from apps.jobs.models import Job
from apps.monitors import services
from apps.monitors.models import CheckResult

from .factories import MonitorFactory
from .fake_servers import SlowServer

BUDGET = 1.0
SLACK = 0.6  # scheduling slop allowed on top of the budget on a busy laptop


@pytest.fixture
def loopback(settings, http):
    settings.DEBUG = True
    settings.SSRF_ALLOW_LOOPBACK = True
    http.route(host="127.0.0.1").pass_through()


def timed(fn, *args, **kwargs):
    started = time.monotonic()
    try:
        fn(*args, **kwargs)
    except Exception as exc:
        return time.monotonic() - started, exc
    return time.monotonic() - started, None


def test_drip_fed_headers_are_cut_off_at_the_total_budget(loopback):
    with SlowServer("drip", interval=0.2) as server:
        elapsed, exc = timed(safe_http.send, "GET", server.url("/"), timeout=BUDGET)
    assert isinstance(exc, httpx.TimeoutException), exc
    assert BUDGET - 0.05 <= elapsed <= BUDGET + SLACK, elapsed


def test_per_read_timeouts_alone_would_not_stop_the_drip(loopback):
    """Control experiment: plain httpx with the same timeout keeps reading."""
    finished = threading.Event()

    def plain_httpx(url):
        with contextlib.suppress(httpx.HTTPError):
            httpx.get(url, timeout=BUDGET)
        finished.set()

    with SlowServer("drip", interval=0.2, lifetime=3.0) as server:
        threading.Thread(target=plain_httpx, args=(server.url("/"),), daemon=True).start()
        assert not finished.wait(2.5), "plain httpx gave up; the drip server isn't dripping"
        assert finished.wait(5)  # it only ends because the server stops


def test_a_stalled_tls_handshake_is_cut_off(loopback):
    with SlowServer("stall") as server:
        elapsed, exc = timed(safe_http.send, "GET", server.url("/", "https"), timeout=BUDGET)
    assert isinstance(exc, httpx.TimeoutException), exc
    assert elapsed <= BUDGET + SLACK, elapsed


def test_slow_dns_counts_against_the_budget(monkeypatch, http):
    def slow_lookup(host, port):
        time.sleep(2)
        raise OSError("too late anyway")

    monkeypatch.setattr(safe_http, "_getaddrinfo", slow_lookup)
    elapsed, exc = timed(safe_http.send, "GET", "https://slow-dns.example/", timeout=BUDGET)
    assert isinstance(exc, httpx.TimeoutException), exc
    assert elapsed <= BUDGET + SLACK, elapsed
    assert not http.calls


def test_time_spent_on_dns_is_taken_from_the_request_budget(monkeypatch, loopback):
    """0.7 s of DNS leaves 0.3 s for the drip server, not a fresh 1 s."""
    import socket

    def slowish_lookup(host, port):
        time.sleep(0.7)
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", port))]

    monkeypatch.setattr(safe_http, "_getaddrinfo", slowish_lookup)
    with SlowServer("drip") as server:
        url = f"http://localhost:{server.port}/"
        elapsed, exc = timed(safe_http.send, "GET", url, timeout=BUDGET)
    assert isinstance(exc, httpx.TimeoutException), exc
    assert elapsed <= BUDGET + SLACK, elapsed


def test_a_redirect_chain_is_not_followed_so_it_cannot_exceed_the_budget(loopback):
    with SlowServer("redirect") as server:
        started = time.monotonic()
        result = safe_http.send("GET", server.url("/"), timeout=BUDGET)
        elapsed = time.monotonic() - started
        requests = server.requests
    assert result.status_code == 302
    assert requests == 1  # one request; the Location is never fetched
    assert elapsed < BUDGET


def test_a_normal_fast_response_is_unaffected(loopback):
    from .fake_servers import FakeTarget

    with FakeTarget(status=204) as target:
        result = safe_http.send("GET", target.url("/"), timeout=BUDGET)
    assert result.status_code == 204
    assert result.elapsed_ms < BUDGET * 1000


@pytest.mark.django_db
def test_worst_case_check_job_finishes_well_inside_its_lease(loopback, settings):
    """The drip server is the worst case: the check job still ends quickly, as a timeout."""
    settings.JOB_LEASE_SECONDS = 5
    with SlowServer("drip") as server:
        monitor = MonitorFactory(url=server.url("/health"), timeout_seconds=1)
        job = queue.enqueue("run_check", {"monitor_id": monitor.pk}, max_attempts=1)
        [claimed] = queue.claim("w1")
        started = time.monotonic()
        status = queue.run_job(claimed, "w1")
        elapsed = time.monotonic() - started
    job.refresh_from_db()
    assert status == job.status == Job.Status.SUCCEEDED  # the check ran; the site timed out
    assert elapsed < settings.JOB_LEASE_SECONDS / 2, elapsed
    assert job.finished_at < claimed.locked_until  # finished before the lease expired
    result = CheckResult.objects.get(monitor=monitor)
    assert (result.ok, result.error) == (False, "timeout")


@pytest.mark.django_db
def test_monitor_timeout_is_the_check_budget(loopback):
    with SlowServer("drip") as server:
        monitor = MonitorFactory(url=server.url("/"), timeout_seconds=2)
        started = time.monotonic()
        result = services.run_check(monitor.pk)
        elapsed = time.monotonic() - started
    assert result.error == "timeout"
    assert 1.9 <= elapsed <= 2 + SLACK, elapsed


# A2: startup checks on the timing settings


def test_default_settings_pass_the_timing_check():
    assert check_timing_settings(None) == []
    assert not [m for m in run_checks() if m.id and m.id.startswith("statusping.")]


def test_lease_too_short_for_the_longest_job_is_an_error(settings):
    settings.JOB_LEASE_SECONDS = 29  # longest budget is 10 s, so at least 30 is required
    [error] = check_timing_settings(None)
    assert error.id == "statusping.E001"
    assert "JOB_LEASE_SECONDS=29" in error.msg
    assert (
        error.hint
        == f"Set JOB_LEASE_SECONDS >= {LEASE_SAFETY_FACTOR * 10} or lower ALERT_TIMEOUT_SECONDS."
    )


def test_long_webhook_timeout_needs_a_longer_lease(settings):
    settings.ALERT_TIMEOUT_SECONDS = 25
    assert [e.id for e in check_timing_settings(None)] == ["statusping.E001"]
    settings.JOB_LEASE_SECONDS = 75
    assert check_timing_settings(None) == []


def test_default_monitor_timeout_must_fit_the_ceiling(settings):
    settings.MONITOR_DEFAULT_TIMEOUT_SECONDS = 11
    assert [e.id for e in check_timing_settings(None)] == ["statusping.E002"]


def test_short_shutdown_timeout_is_a_warning(settings):
    settings.WORKER_SHUTDOWN_TIMEOUT_SECONDS = 3
    assert [e.id for e in check_timing_settings(None)] == ["statusping.W001"]


def test_misconfiguration_stops_commands_from_starting(settings):
    """Management commands (run_worker, runserver, migrate) run system checks first."""
    settings.JOB_LEASE_SECONDS = 10
    with pytest.raises(SystemCheckError, match="statusping.E001"):
        call_command("check")

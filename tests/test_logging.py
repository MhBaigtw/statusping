"""D3: one structured log line per job; webhook URLs never appear in logs or errors."""

import logging

import httpx
import pytest
from django.utils import timezone

from apps.alerts import services as alert_services
from apps.alerts.models import AlertDelivery
from apps.core.logging import SecretMaskingFilter, mask_secrets
from apps.jobs import queue
from apps.jobs.models import Job
from apps.monitors.models import Incident

from .factories import DISCORD_IP, AlertChannelFactory, MonitorFactory

pytestmark = pytest.mark.django_db

SECRET = "https://discord.com/api/webhooks/123/very-secret-token"
HOOK = f"https://{DISCORD_IP}/api/webhooks/123/very-secret-token"


@pytest.mark.parametrize(
    "text,expected",
    [
        (SECRET, "https://discord.com/api/webhooks/****"),
        (
            "POST https://hooks.slack.com/services/T0/B1/xyz failed",
            "POST https://hooks.slack.com/services/**** failed",
        ),
        (f"url='{SECRET}'", "url='https://discord.com/api/webhooks/****'"),
        (
            "https://discordapp.com/api/webhooks/9/t?wait=true)",
            "https://discordapp.com/api/webhooks/****)",
        ),
        ("HTTP 503", "HTTP 503"),
        ("https://example.com/health", "https://example.com/health"),  # monitor URLs untouched
        ("", ""),
        (None, None),
    ],
)
def test_mask_secrets(text, expected):
    assert mask_secrets(text) == expected


@pytest.fixture
def delivery():
    monitor = MonitorFactory()
    AlertChannelFactory(organization=monitor.organization, webhook_url=SECRET)
    incident = Incident.objects.create(monitor=monitor, started_at=timezone.now(), cause="dns")
    alert_services.queue_alerts(incident, "opened")
    return AlertDelivery.objects.get()


def job_lines(caplog):
    return [r for r in caplog.records if hasattr(r, "outcome")]


def run_one():
    Job.objects.filter(status="queued").update(run_after=timezone.now())
    [claimed] = queue.claim("w1")
    return claimed, queue.run_job(claimed, "w1")


def test_one_structured_line_per_job(delivery, http, caplog):
    caplog.set_level(logging.DEBUG)
    http.post(HOOK).mock(side_effect=[httpx.Response(503), httpx.Response(204)])
    job, _ = run_one()
    job, _ = run_one()
    first, second = job_lines(caplog)
    assert first.getMessage().startswith(
        f"job id={job.pk} kind=deliver_alert attempt=1/5 outcome=queued duration_ms="
    )
    assert first.getMessage().endswith(' error="HTTP 503"')
    assert first.levelno == logging.INFO
    assert second.getMessage().startswith(
        f"job id={job.pk} kind=deliver_alert attempt=2/5 outcome=succeeded duration_ms="
    )
    assert "error=" not in second.getMessage()
    # Machine-readable fields ride along on the record for structured handlers.
    assert (second.job_id, second.job_kind, second.attempt, second.outcome) == (
        job.pk,
        "deliver_alert",
        2,
        "succeeded",
    )
    assert isinstance(second.duration_ms, int)


def test_failed_dead_and_lease_lost_outcomes_are_warnings(caplog, monkeypatch):
    caplog.set_level(logging.DEBUG)
    monkeypatch.setitem(queue.HANDLERS, "crash", "tests.test_queue.crash_handler")
    monkeypatch.setitem(queue.HANDLERS, "permanent", "tests.test_queue.permanent_handler")
    queue.enqueue("crash", {}, max_attempts=1)
    _, status = run_one()
    assert status == "dead"
    queue.enqueue("permanent", {})
    run_one()
    queue.enqueue("noop", {})
    [claimed] = queue.claim("w1")
    Job.objects.filter(pk=claimed.pk).update(locked_by="someone-else")
    queue.run_job(claimed, "w1")
    outcomes = [(r.outcome, r.levelno) for r in job_lines(caplog)]
    assert outcomes == [
        ("dead", logging.WARNING),
        ("failed", logging.WARNING),
        ("lease_lost", logging.WARNING),
    ]


def test_a_webhook_url_inside_an_exception_is_masked_everywhere(delivery, monkeypatch, caplog):
    """Even an unexpected exception that quotes the URL can't leak it."""
    caplog.set_level(logging.DEBUG)

    def leaky(channel, text):
        raise ValueError(f"cannot post to {channel.webhook_url}")

    monkeypatch.setattr(alert_services, "post_to_webhook", leaky)
    job, _ = run_one()
    job.refresh_from_db()
    assert "very-secret-token" not in job.last_error
    assert "api/webhooks/****" in job.last_error
    [line] = job_lines(caplog)
    assert "very-secret-token" not in line.getMessage()
    # The traceback record: the console handler's filter (settings.LOGGING) masks the
    # shared record in place, so every handler, caplog included, gets the masked text.
    [traceback_record] = [r for r in caplog.records if "raised" in r.getMessage()]
    handler = next(
        h
        for h in logging.getLogger().handlers
        if any(isinstance(f, SecretMaskingFilter) for f in h.filters)
    )
    assert traceback_record.exc_info is None  # replaced by the masked exc_text
    assert handler.filter(traceback_record)
    output = handler.format(traceback_record)
    assert "Traceback" in output and "ValueError" in output
    assert "very-secret-token" not in output
    assert "api/webhooks/****" in output


def test_masking_filter_handles_args_and_plain_messages():
    record = logging.LogRecord("x", logging.INFO, __file__, 1, "posting to %s", (SECRET,), None)
    SecretMaskingFilter().filter(record)
    assert record.getMessage() == "posting to https://discord.com/api/webhooks/****"
    record = logging.LogRecord("x", logging.INFO, __file__, 1, "nothing secret %s", ("here",), None)
    SecretMaskingFilter().filter(record)
    assert record.getMessage() == "nothing secret here"
    record.exc_text = f"Traceback ... {SECRET}"
    SecretMaskingFilter().filter(record)
    assert "very-secret-token" not in record.exc_text

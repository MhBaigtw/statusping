"""Alert channels and alert delivery.

Webhook URLs are secrets. Nothing here logs them or puts them in an error message:
errors are built from status codes and exception class names only.
"""

import logging
from datetime import timedelta
from email.utils import parsedate_to_datetime

import httpx
from django.conf import settings
from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from apps.core import safe_http
from apps.jobs.exceptions import PermanentError, RetryLater
from apps.jobs.queue import enqueue
from apps.monitors.models import CheckResult

from .models import AlertChannel, AlertDelivery

logger = logging.getLogger(__name__)

LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1")


# Channels


def check_webhook_host(kind, url):
    """Shape rules for a webhook URL (no DNS): https on the provider's own host."""
    parsed, host, _ = safe_http.parse_url(url)
    if safe_http.loopback_allowed() and host in LOOPBACK_HOSTS:
        return  # DEV ONLY: scripts/e2e.py points channels at a local fake receiver
    if parsed.scheme != "https":
        raise safe_http.BlockedTarget("Webhook URLs must use https.")
    allowed = settings.ALERT_WEBHOOK_HOSTS[kind]
    if host not in allowed:
        raise safe_http.BlockedTarget(f"A {kind} webhook must be on {' or '.join(allowed)}.")


def validate_webhook_url(kind, url):
    """Create/update-time validation: provider host rules plus full SSRF checks."""
    try:
        check_webhook_host(kind, url)
        safe_http.validate_target(url)
    except safe_http.BlockedTarget as exc:
        raise ValidationError({"webhook_url": [str(exc)]}, code="blocked_target") from exc
    except safe_http.DNSFailure as exc:
        raise ValidationError({"webhook_url": [str(exc)]}, code="dns") from exc


def create_channel(*, organization, kind, name, webhook_url, is_active=True):
    validate_webhook_url(kind, webhook_url)
    return AlertChannel.objects.create(
        organization=organization,
        kind=kind,
        name=name,
        webhook_url=webhook_url,
        is_active=is_active,
    )


def update_channel(channel, **fields):
    if "webhook_url" in fields or "kind" in fields:
        validate_webhook_url(
            fields.get("kind", channel.kind), fields.get("webhook_url", channel.webhook_url)
        )
    for name, value in fields.items():
        setattr(channel, name, value)
    channel.save()
    return channel


def request_test_alert(channel):
    """Enqueue a test message to the channel. Returns the job."""
    return enqueue(
        "send_test_alert", {"channel_id": channel.pk}, max_attempts=settings.ALERT_MAX_ATTEMPTS
    )


# Queueing deliveries (called inside the check transaction)


def queue_alerts(incident, event):
    """One pending delivery + one deliver_alert job per active channel, idempotently.

    Runs in the caller's transaction (the one that opened or resolved the incident), so
    the incident, its deliveries and their jobs commit together or not at all.
    """
    channels = AlertChannel.objects.filter(
        organization_id=incident.monitor.organization_id, is_active=True
    )
    for channel in channels:
        delivery, created = AlertDelivery.objects.get_or_create(
            incident=incident, channel=channel, event=event
        )
        if created:
            enqueue(
                "deliver_alert",
                {"delivery_id": delivery.pk},
                dedupe_key=f"deliver_alert:{delivery.pk}",
                max_attempts=settings.ALERT_MAX_ATTEMPTS,
            )


# Messages


def _escape_slack(text):
    # Slack treats <...> as links/mentions (e.g. <!channel>); escape per Slack's docs.
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _fmt(dt):
    return dt.strftime("%Y-%m-%d %H:%M:%S UTC")


def build_message(delivery):
    incident = delivery.incident
    monitor = incident.monitor
    cause = CheckResult.Error(incident.cause).label
    if delivery.event == AlertDelivery.Event.OPENED:
        return (
            f"[DOWN] {monitor.name} is down\n"
            f"URL: {monitor.url}\n"
            f"Cause: {cause}\n"
            f"Since: {_fmt(incident.started_at)}"
        )
    minutes = max(1, round((incident.resolved_at - incident.started_at).total_seconds() / 60))
    return (
        f"[UP] {monitor.name} is back up\n"
        f"URL: {monitor.url}\n"
        f"Was down for about {minutes} min ({cause})\n"
        f"Resolved: {_fmt(incident.resolved_at)}"
    )


def _payload(kind, text):
    if kind == AlertChannel.Kind.SLACK:
        return {"text": _escape_slack(text)}
    # Discord: never let a monitor name trigger @everyone / role mentions.
    return {"content": text, "allowed_mentions": {"parse": []}}


# Sending


class PostOutcome:
    def __init__(self, sent=False, retryable=False, error="", retry_at=None):
        self.sent = sent
        self.retryable = retryable
        self.error = error
        self.retry_at = retry_at


def parse_retry_after(value, now):
    """Retry-After as seconds or an HTTP date -> an aware datetime, or None."""
    if not value:
        return None
    value = value.strip()
    if value.isdigit():
        return now + timedelta(seconds=int(value))
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    return when if when.tzinfo else None


def post_to_webhook(channel, text):
    """POST one message. Classifies the result; never raises for HTTP/network errors."""
    try:
        check_webhook_host(channel.kind, channel.webhook_url)
        result = safe_http.send(
            "POST",
            channel.webhook_url,
            timeout=settings.ALERT_TIMEOUT_SECONDS,
            json=_payload(channel.kind, text),
        )
    except safe_http.BlockedTarget as exc:
        return PostOutcome(error=f"Webhook target blocked: {exc}")
    except safe_http.DNSFailure:
        return PostOutcome(retryable=True, error="DNS lookup failed")
    except httpx.TimeoutException:
        return PostOutcome(retryable=True, error="Timed out")
    except httpx.HTTPError as exc:
        return PostOutcome(retryable=True, error=f"Network error ({type(exc).__name__})")

    status = result.status_code
    if 200 <= status < 300:
        return PostOutcome(sent=True)
    if status == 429:
        retry_at = parse_retry_after(result.headers.get("Retry-After"), timezone.now())
        return PostOutcome(retryable=True, error="HTTP 429 (rate limited)", retry_at=retry_at)
    if status >= 500:
        return PostOutcome(retryable=True, error=f"HTTP {status}")
    return PostOutcome(error=f"HTTP {status} (not retried)")


def deliver_alert(delivery_id, *, final_attempt):
    """Send one AlertDelivery. Safe to run twice: a sent delivery is never sent again.

    The delivery row stays locked (SELECT ... FOR UPDATE) for the whole POST, so a
    second run of the same job waits, then sees status=sent and does nothing.
    Raises RetryLater for retryable failures and PermanentError for permanent ones, after
    the attempt has been recorded on the row.
    """
    with transaction.atomic():
        delivery = (
            AlertDelivery.objects.select_for_update(of=("self",))
            .select_related("channel", "incident__monitor")
            .filter(pk=delivery_id)
            .first()
        )
        if delivery is None or delivery.status != AlertDelivery.Status.PENDING:
            return delivery
        if not delivery.channel.is_active:
            delivery.status = AlertDelivery.Status.FAILED
            delivery.last_error = "Channel was deactivated before delivery."
            delivery.save(update_fields=["status", "last_error"])
            return delivery

        outcome = post_to_webhook(delivery.channel, build_message(delivery))
        delivery.attempts += 1
        if outcome.sent:
            delivery.status = AlertDelivery.Status.SENT
            delivery.sent_at = timezone.now()
            delivery.last_error = ""
        else:
            delivery.last_error = outcome.error
            if not outcome.retryable or final_attempt:
                delivery.status = AlertDelivery.Status.FAILED
        delivery.save(update_fields=["status", "attempts", "last_error", "sent_at"])

    logger.info(
        "delivery %s (channel %s, %s): attempt %s -> %s %s",
        delivery.pk,
        delivery.channel_id,
        delivery.event,
        delivery.attempts,
        delivery.status,
        delivery.last_error,
    )
    if not outcome.sent:
        if outcome.retryable:
            raise RetryLater(outcome.error, retry_at=outcome.retry_at)
        raise PermanentError(outcome.error)
    return delivery


def send_test_alert(channel_id):
    """Send a test message. Running it twice sends two test messages (harmless, by design)."""
    channel = AlertChannel.objects.filter(pk=channel_id).first()
    if channel is None:
        return
    outcome = post_to_webhook(
        channel, f"StatusPing test message for channel '{channel.name}'. Alerts will arrive here."
    )
    logger.info("test alert channel %s: %s %s", channel.pk, outcome.sent, outcome.error)
    if not outcome.sent:
        if outcome.retryable:
            raise RetryLater(outcome.error, retry_at=outcome.retry_at)
        raise PermanentError(outcome.error)

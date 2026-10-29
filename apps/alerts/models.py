from django.db import models

from apps.core.safe_http import parse_url
from apps.monitors.models import Incident
from apps.organizations.models import Organization


class AlertChannel(models.Model):
    class Kind(models.TextChoices):
        DISCORD = "discord", "Discord"
        SLACK = "slack", "Slack"

    organization = models.ForeignKey(
        Organization, on_delete=models.CASCADE, related_name="alert_channels"
    )
    kind = models.CharField(max_length=10, choices=Kind.choices)
    name = models.CharField(max_length=100)
    # A secret: anyone holding it can post to the channel. Masked in every API response
    # after creation and never logged.
    webhook_url = models.URLField(max_length=500)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["id"]

    def __str__(self):
        return f"{self.name} ({self.kind})"

    @property
    def masked_webhook_url(self):
        try:
            parsed, _, _ = parse_url(self.webhook_url)
        except Exception:
            return "****"
        return f"{parsed.scheme}://{parsed.netloc.decode('ascii')}/****"


class AlertDelivery(models.Model):
    """One alert, for one incident event, to one channel. Can never exist twice."""

    class Event(models.TextChoices):
        OPENED = "opened", "Opened"
        RESOLVED = "resolved", "Resolved"

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        SENT = "sent", "Sent"
        FAILED = "failed", "Failed"

    incident = models.ForeignKey(Incident, on_delete=models.CASCADE, related_name="deliveries")
    channel = models.ForeignKey(AlertChannel, on_delete=models.CASCADE, related_name="deliveries")
    event = models.CharField(max_length=10, choices=Event.choices)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.PENDING)
    attempts = models.PositiveSmallIntegerField(default=0)
    last_error = models.CharField(max_length=500, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    sent_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["id"]
        verbose_name_plural = "alert deliveries"
        constraints = [
            models.UniqueConstraint(
                fields=["incident", "channel", "event"], name="unique_alert_delivery"
            ),
        ]

    def __str__(self):
        return f"{self.event} alert for incident {self.incident_id} -> {self.channel_id}"

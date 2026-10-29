from rest_framework import serializers

from .models import AlertChannel, AlertDelivery


class AlertChannelSerializer(serializers.ModelSerializer):
    # The real URL is write-only: after creation only the masked form is ever returned.
    webhook_url = serializers.URLField(max_length=500, write_only=True)
    webhook_url_masked = serializers.CharField(source="masked_webhook_url", read_only=True)

    class Meta:
        model = AlertChannel
        fields = [
            "id",
            "kind",
            "name",
            "webhook_url",
            "webhook_url_masked",
            "is_active",
            "created_at",
        ]
        read_only_fields = ["created_at"]


class TestAlertSerializer(serializers.Serializer):
    job_id = serializers.IntegerField()


class AlertDeliverySerializer(serializers.ModelSerializer):
    channel_name = serializers.CharField(source="channel.name", read_only=True)

    class Meta:
        model = AlertDelivery
        fields = [
            "id",
            "channel",
            "channel_name",
            "event",
            "status",
            "attempts",
            "last_error",
            "created_at",
            "sent_at",
        ]
        read_only_fields = fields

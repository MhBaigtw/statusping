from rest_framework import serializers

from . import services
from .models import CheckResult, Incident, Monitor


class MonitorSerializer(serializers.ModelSerializer):
    class Meta:
        model = Monitor
        fields = [
            "id",
            "name",
            "url",
            "method",
            "interval_seconds",
            "timeout_seconds",
            "expected_status",
            "failure_threshold",
            "recovery_threshold",
            "is_paused",
            "next_check_at",
            "last_status",
            "consecutive_failures",
            "consecutive_successes",
            "created_at",
            "updated_at",
        ]
        read_only_fields = [
            "is_paused",
            "next_check_at",
            "last_status",
            "consecutive_failures",
            "consecutive_successes",
            "created_at",
            "updated_at",
        ]

    def validate_url(self, value):
        return services.validate_monitor_url(value)


class CheckResultSerializer(serializers.ModelSerializer):
    class Meta:
        model = CheckResult
        fields = ["id", "checked_at", "ok", "status_code", "response_ms", "error"]
        read_only_fields = fields


class IncidentSerializer(serializers.ModelSerializer):
    monitor_name = serializers.CharField(source="monitor.name", read_only=True)
    is_open = serializers.BooleanField(read_only=True)

    class Meta:
        model = Incident
        fields = ["id", "monitor", "monitor_name", "started_at", "resolved_at", "is_open", "cause"]
        read_only_fields = fields

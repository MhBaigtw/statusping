from rest_framework import serializers

UPTIME_HELP = (
    "Percentage of successful checks among all checks with checked_at in the window "
    "[now - window, now], rounded to 2 decimals. null when the window has no checks "
    "(no data is not 100%)."
)


class UptimeSerializer(serializers.Serializer):
    _24h = serializers.FloatField(allow_null=True, help_text=UPTIME_HELP)
    _7d = serializers.FloatField(allow_null=True, help_text=UPTIME_HELP)
    _30d = serializers.FloatField(allow_null=True, help_text=UPTIME_HELP)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # "24h" etc. are not valid Python identifiers; expose the fields under those names.
        for name in ("24h", "7d", "30d"):
            self.fields[name] = self.fields.pop(f"_{name}")


class PublicIncidentSerializer(serializers.Serializer):
    started_at = serializers.DateTimeField()
    resolved_at = serializers.DateTimeField(allow_null=True)
    duration_seconds = serializers.IntegerField(allow_null=True, help_text="null while open")


class PublicMonitorSerializer(serializers.Serializer):
    name = serializers.CharField()
    status = serializers.ChoiceField(
        choices=["up", "down", "unknown", "paused"],
        help_text="down = an incident is open (flap-protected), not just one failed check.",
    )
    last_checked_at = serializers.DateTimeField(allow_null=True)
    uptime = UptimeSerializer()
    incidents = PublicIncidentSerializer(many=True, help_text="The 5 most recent, newest first.")


class PublicOrganizationSerializer(serializers.Serializer):
    name = serializers.CharField()
    slug = serializers.SlugField()


class StatusPageSerializer(serializers.Serializer):
    organization = PublicOrganizationSerializer()
    status = serializers.ChoiceField(
        choices=["operational", "degraded", "down"],
        help_text="down: every unpaused monitor is down; degraded: some are.",
    )
    generated_at = serializers.DateTimeField(help_text="Payloads are cached for up to 30 s.")
    monitors = PublicMonitorSerializer(many=True)

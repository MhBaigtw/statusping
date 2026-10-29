from django.contrib import admin

from .models import AlertChannel, AlertDelivery


@admin.register(AlertChannel)
class AlertChannelAdmin(admin.ModelAdmin):
    # webhook_url is a secret: the admin shows only the masked form and cannot edit it.
    list_display = ["name", "organization", "kind", "masked_webhook_url", "is_active"]
    list_filter = ["kind", "is_active"]
    fields = ["organization", "kind", "name", "masked_webhook_url", "is_active", "created_at"]
    readonly_fields = ["organization", "kind", "masked_webhook_url", "created_at"]

    def has_add_permission(self, request):
        return False  # created through the API, which validates the URL


@admin.register(AlertDelivery)
class AlertDeliveryAdmin(admin.ModelAdmin):
    list_display = ["id", "incident", "channel", "event", "status", "attempts", "sent_at"]
    list_filter = ["event", "status"]
    readonly_fields = [f.name for f in AlertDelivery._meta.fields]

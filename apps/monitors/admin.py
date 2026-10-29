from django.contrib import admin

from .models import CheckResult, Incident, Monitor


@admin.register(Monitor)
class MonitorAdmin(admin.ModelAdmin):
    list_display = ["name", "organization", "url", "last_status", "is_paused", "next_check_at"]
    list_filter = ["last_status", "is_paused"]
    search_fields = ["name", "url", "organization__slug"]
    raw_id_fields = ["organization"]


@admin.register(CheckResult)
class CheckResultAdmin(admin.ModelAdmin):
    list_display = ["monitor", "checked_at", "ok", "status_code", "response_ms", "error"]
    list_filter = ["ok", "error"]
    raw_id_fields = ["monitor"]
    readonly_fields = [f.name for f in CheckResult._meta.fields]


@admin.register(Incident)
class IncidentAdmin(admin.ModelAdmin):
    list_display = ["id", "monitor", "started_at", "resolved_at", "cause"]
    list_filter = ["cause"]
    raw_id_fields = ["monitor"]

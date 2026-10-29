from django.contrib import admin

from .models import Job, WorkerHeartbeat


@admin.register(Job)
class JobAdmin(admin.ModelAdmin):
    list_display = ["id", "kind", "status", "attempts", "max_attempts", "run_after", "locked_by"]
    list_filter = ["status", "kind"]
    search_fields = ["dedupe_key", "locked_by"]
    readonly_fields = [f.name for f in Job._meta.fields]

    def has_add_permission(self, request):
        return False  # jobs are created by services through queue.enqueue()


@admin.register(WorkerHeartbeat)
class WorkerHeartbeatAdmin(admin.ModelAdmin):
    list_display = ["worker_id", "threads", "started_at", "last_seen"]
    readonly_fields = [f.name for f in WorkerHeartbeat._meta.fields]

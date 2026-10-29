from django.conf import settings
from django.db import DatabaseError
from django.db.models import Max
from django.http import JsonResponse
from django.utils import timezone
from drf_spectacular.utils import extend_schema, inline_serializer
from rest_framework import serializers, status
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.jobs.models import WorkerHeartbeat

from .exceptions import error_body


def worker_health():
    """Age of the newest worker heartbeat: ok, stale, or missing."""
    last_seen = WorkerHeartbeat.objects.aggregate(last=Max("last_seen"))["last"]
    if last_seen is None:
        return {"status": "missing", "last_seen_seconds_ago": None}
    age = max(0, round((timezone.now() - last_seen).total_seconds()))
    fresh = age <= settings.WORKER_HEARTBEAT_STALE_SECONDS
    return {"status": "ok" if fresh else "stale", "last_seen_seconds_ago": age}


class HealthView(APIView):
    """200 while the database answers. A stale or missing worker makes it "degraded",
    not 503: the web process is healthy and must not be restarted for a worker problem."""

    authentication_classes = []
    permission_classes = [AllowAny]

    @extend_schema(
        responses={
            200: inline_serializer(
                "Health",
                {
                    "status": serializers.CharField(),
                    "database": serializers.CharField(),
                    "worker": inline_serializer(
                        "WorkerHealth",
                        {
                            "status": serializers.CharField(),
                            "last_seen_seconds_ago": serializers.IntegerField(allow_null=True),
                        },
                    ),
                },
            )
        }
    )
    def get(self, request):
        try:
            worker = worker_health()
        except DatabaseError:
            return Response(
                error_body(
                    "service_unavailable",
                    "Database is unavailable.",
                    {"database": "unavailable"},
                ),
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        overall = "ok" if worker["status"] == "ok" else "degraded"
        return Response({"status": overall, "database": "ok", "worker": worker})


def not_found(request, exception=None):
    return JsonResponse(error_body("not_found", "Not found."), status=404)


def server_error(request):
    return JsonResponse(error_body("server_error", "Internal server error."), status=500)

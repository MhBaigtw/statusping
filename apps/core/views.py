from django.conf import settings
from django.db import DatabaseError, connection
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


def database_unavailable():
    return Response(
        error_body("service_unavailable", "Database is unavailable.", {"database": "unavailable"}),
        status=status.HTTP_503_SERVICE_UNAVAILABLE,
    )


WorkerHealthSerializer = inline_serializer(
    "WorkerHealth",
    {
        "status": serializers.ChoiceField(choices=["ok", "stale", "missing"]),
        "last_seen_seconds_ago": serializers.IntegerField(allow_null=True),
    },
)


class HealthView(APIView):
    """The whole service: database AND a live worker. 503 if either is down.

    The worker IS the product (no worker = no monitoring), so a host's health check on
    this path detects a dead worker: 503 when the newest heartbeat is older than
    WORKER_HEARTBEAT_STALE_SECONDS, or there is none. Use /api/health/live/ for the WEB
    process's own health check, so a dead worker doesn't make the platform restart (or
    refuse to deploy) a healthy web process.
    """

    authentication_classes = []
    permission_classes = [AllowAny]

    @extend_schema(
        responses={
            200: inline_serializer(
                "Health",
                {
                    "status": serializers.CharField(),
                    "database": serializers.CharField(),
                    "worker": WorkerHealthSerializer,
                },
            ),
            503: inline_serializer(
                "HealthError",
                {
                    "error": inline_serializer(
                        "HealthErrorBody",
                        {
                            "code": serializers.CharField(),
                            "message": serializers.CharField(),
                            "details": serializers.DictField(),
                        },
                    )
                },
            ),
        }
    )
    def get(self, request):
        try:
            worker = worker_health()
        except DatabaseError:
            return database_unavailable()
        if worker["status"] != "ok":
            return Response(
                error_body(
                    "worker_unavailable",
                    f"No worker heartbeat in the last {settings.WORKER_HEARTBEAT_STALE_SECONDS} s.",
                    {"database": "ok", "worker": worker},
                ),
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        return Response({"status": "ok", "database": "ok", "worker": worker})


class LivenessView(APIView):
    """The web process only: 200 while it can reach the database (ignores the worker)."""

    authentication_classes = []
    permission_classes = [AllowAny]

    @extend_schema(
        responses={
            200: inline_serializer(
                "Liveness", {"status": serializers.CharField(), "database": serializers.CharField()}
            )
        }
    )
    def get(self, request):
        try:
            with connection.cursor() as cursor:
                cursor.execute("SELECT 1")
        except DatabaseError:
            return database_unavailable()
        return Response({"status": "ok", "database": "ok"})


def not_found(request, exception=None):
    return JsonResponse(error_body("not_found", "Not found."), status=404)


def server_error(request):
    return JsonResponse(error_body("server_error", "Internal server error."), status=500)

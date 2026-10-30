from django.shortcuts import get_object_or_404
from drf_spectacular.utils import extend_schema
from rest_framework import generics, mixins, viewsets
from rest_framework.decorators import action
from rest_framework.pagination import CursorPagination
from rest_framework.response import Response

from apps.core.throttles import WriteThrottle
from apps.organizations.permissions import OrgScopedMixin

from . import selectors, services
from .filters import CheckResultFilter, IncidentFilter, MonitorFilter
from .models import CheckResult, Incident
from .serializers import CheckResultSerializer, IncidentSerializer, MonitorSerializer


class MonitorViewSet(OrgScopedMixin, viewsets.ModelViewSet):
    serializer_class = MonitorSerializer
    filterset_class = MonitorFilter
    http_method_names = ["get", "post", "patch", "delete", "head", "options"]
    throttle_classes = [WriteThrottle]

    def get_queryset(self):
        return selectors.monitors_for(self.organization)

    def perform_create(self, serializer):
        serializer.instance = services.create_monitor(
            organization=self.organization, **serializer.validated_data
        )

    def perform_update(self, serializer):
        services.update_monitor(serializer.instance, **serializer.validated_data)

    def perform_destroy(self, instance):
        services.delete_monitor(instance)

    @extend_schema(request=None, responses=MonitorSerializer)
    @action(detail=True, methods=["post"])
    def pause(self, request, org_slug, pk):
        return Response(MonitorSerializer(services.pause_monitor(self.get_object())).data)

    @extend_schema(request=None, responses=MonitorSerializer)
    @action(detail=True, methods=["post"])
    def resume(self, request, org_slug, pk):
        return Response(MonitorSerializer(services.resume_monitor(self.get_object())).data)


class MonitorChildMixin(OrgScopedMixin):
    """For views under orgs/{org_slug}/monitors/{monitor_id}/: the monitor, scoped (404)."""

    def get_monitor(self):
        return get_object_or_404(
            selectors.monitors_for(self.organization), pk=self.kwargs["monitor_id"]
        )


class NewestFirstPagination(CursorPagination):
    """Cursor pagination: CheckResult is high volume, so no COUNT(*) and no deep OFFSET."""

    ordering = ("-checked_at", "-id")
    page_size = 50
    page_size_query_param = "page_size"
    max_page_size = 200


class CheckResultListView(MonitorChildMixin, generics.ListAPIView):
    queryset = CheckResult.objects.none()  # for schema generation; see get_queryset
    serializer_class = CheckResultSerializer
    filterset_class = CheckResultFilter
    pagination_class = NewestFirstPagination

    def get_queryset(self):
        return selectors.checks_for(self.get_monitor())


class MonitorIncidentListView(MonitorChildMixin, generics.ListAPIView):
    queryset = Incident.objects.none()  # for schema generation; see get_queryset
    serializer_class = IncidentSerializer
    filterset_class = IncidentFilter

    def get_queryset(self):
        return selectors.incidents_for(self.organization, monitor=self.get_monitor())


class IncidentViewSet(
    OrgScopedMixin, mixins.ListModelMixin, mixins.RetrieveModelMixin, viewsets.GenericViewSet
):
    serializer_class = IncidentSerializer
    filterset_class = IncidentFilter

    def get_queryset(self):
        return selectors.incidents_for(self.organization)

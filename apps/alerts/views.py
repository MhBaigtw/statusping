from django.shortcuts import get_object_or_404
from drf_spectacular.utils import extend_schema
from rest_framework import generics, status, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from apps.core.throttles import WriteThrottle
from apps.monitors.selectors import incidents_for
from apps.organizations.permissions import OrgScopedMixin

from . import services
from .models import AlertChannel, AlertDelivery
from .serializers import AlertChannelSerializer, AlertDeliverySerializer, TestAlertSerializer


class AlertChannelViewSet(OrgScopedMixin, viewsets.ModelViewSet):
    serializer_class = AlertChannelSerializer
    http_method_names = ["get", "post", "patch", "delete", "head", "options"]
    throttle_classes = [WriteThrottle]

    def get_queryset(self):
        return AlertChannel.objects.filter(organization=self.organization)

    def perform_create(self, serializer):
        serializer.instance = services.create_channel(
            organization=self.organization, **serializer.validated_data
        )

    def perform_update(self, serializer):
        services.update_channel(serializer.instance, **serializer.validated_data)

    @extend_schema(request=None, responses={202: TestAlertSerializer})
    @action(detail=True, methods=["post"])
    def test(self, request, org_slug, pk):
        job = services.request_test_alert(self.get_object())
        return Response({"job_id": job.pk}, status=status.HTTP_202_ACCEPTED)


class IncidentDeliveryListView(OrgScopedMixin, generics.ListAPIView):
    queryset = AlertDelivery.objects.none()  # for schema generation; see get_queryset
    serializer_class = AlertDeliverySerializer

    def get_queryset(self):
        incident = get_object_or_404(incidents_for(self.organization), pk=self.kwargs["pk"])
        return AlertDelivery.objects.select_related("channel").filter(incident=incident)

from django.http import Http404
from drf_spectacular.utils import extend_schema
from rest_framework.permissions import AllowAny
from rest_framework.renderers import TemplateHTMLRenderer
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from .serializers import StatusPageSerializer
from .services import get_status_payload


class _PublicStatusView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]
    throttle_classes = [ScopedRateThrottle]  # per client IP (NUM_PROXIES-aware)
    throttle_scope = "status_page"

    def payload(self, org_slug):
        payload = get_status_payload(org_slug)
        if payload is None:
            raise Http404  # unknown org, or the owner made the page private
        return payload


class StatusPageView(_PublicStatusView):
    """Public JSON status of an organization. No auth; 404 if unknown or not public."""

    @extend_schema(responses=StatusPageSerializer, tags=["status page"])
    def get(self, request, org_slug):
        return Response(StatusPageSerializer(self.payload(org_slug)).data)


class StatusPageHTMLView(_PublicStatusView):
    """The same payload as a small server-rendered page (refreshes itself every 60 s)."""

    renderer_classes = [TemplateHTMLRenderer]
    template_name = "statuspage/status.html"

    @extend_schema(exclude=True)
    def get(self, request, org_slug):
        return Response({"page": self.payload(org_slug)})

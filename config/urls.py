from django.contrib import admin
from django.urls import include, path
from drf_spectacular.views import SpectacularAPIView, SpectacularSwaggerView
from rest_framework.routers import SimpleRouter

from apps.accounts.views import (
    EmailTokenObtainPairView,
    MeView,
    RegisterView,
    ThrottledTokenRefreshView,
)
from apps.alerts.views import AlertChannelViewSet, IncidentDeliveryListView
from apps.core.views import HealthView
from apps.monitors.views import (
    CheckResultListView,
    IncidentViewSet,
    MonitorIncidentListView,
    MonitorViewSet,
)
from apps.organizations.views import (
    MemberDetailView,
    MemberListCreateView,
    OrganizationDetailView,
    OrganizationListCreateView,
)

ORG = "orgs/<slug:org_slug>/"

router = SimpleRouter()
router.register(r"orgs/(?P<org_slug>[-\w]+)/monitors", MonitorViewSet, basename="monitor")
router.register(r"orgs/(?P<org_slug>[-\w]+)/incidents", IncidentViewSet, basename="incident")
router.register(
    r"orgs/(?P<org_slug>[-\w]+)/alert-channels", AlertChannelViewSet, basename="alert-channel"
)

api_patterns = [
    path("auth/token/", EmailTokenObtainPairView.as_view(), name="token_obtain"),
    path("auth/token/refresh/", ThrottledTokenRefreshView.as_view(), name="token_refresh"),
    path("auth/register/", RegisterView.as_view(), name="register"),
    path("me/", MeView.as_view(), name="me"),
    path("health/", HealthView.as_view(), name="health"),
    path("schema/", SpectacularAPIView.as_view(), name="schema"),
    path("docs/", SpectacularSwaggerView.as_view(url_name="schema"), name="docs"),
    path("orgs/", OrganizationListCreateView.as_view(), name="org-list"),
    path(ORG, OrganizationDetailView.as_view(), name="org-detail"),
    path(f"{ORG}members/", MemberListCreateView.as_view(), name="member-list"),
    path(f"{ORG}members/<int:pk>/", MemberDetailView.as_view(), name="member-detail"),
    path(
        f"{ORG}monitors/<int:monitor_id>/checks/",
        CheckResultListView.as_view(),
        name="monitor-checks",
    ),
    path(
        f"{ORG}monitors/<int:monitor_id>/incidents/",
        MonitorIncidentListView.as_view(),
        name="monitor-incidents",
    ),
    path(
        f"{ORG}incidents/<int:pk>/deliveries/",
        IncidentDeliveryListView.as_view(),
        name="incident-deliveries",
    ),
    *router.urls,
]

urlpatterns = [
    path("admin/", admin.site.urls),
    path("api/", include(api_patterns)),
]

handler404 = "apps.core.views.not_found"
handler500 = "apps.core.views.server_error"

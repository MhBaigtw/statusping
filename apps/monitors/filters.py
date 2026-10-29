import django_filters

from apps.core.filters import StrictBooleanFilter

from .models import CheckResult, Incident, Monitor


class MonitorFilter(django_filters.FilterSet):
    last_status = django_filters.ChoiceFilter(choices=Monitor.Status.choices)
    is_paused = StrictBooleanFilter()

    class Meta:
        model = Monitor
        fields = ["last_status", "is_paused"]


class CheckResultFilter(django_filters.FilterSet):
    ok = StrictBooleanFilter()
    checked_after = django_filters.IsoDateTimeFilter(field_name="checked_at", lookup_expr="gte")
    checked_before = django_filters.IsoDateTimeFilter(field_name="checked_at", lookup_expr="lte")

    class Meta:
        model = CheckResult
        fields = ["ok", "checked_after", "checked_before"]


class IncidentFilter(django_filters.FilterSet):
    status = django_filters.ChoiceFilter(
        choices=[("open", "open"), ("resolved", "resolved")], method="filter_status"
    )

    class Meta:
        model = Incident
        fields = ["status"]

    def filter_status(self, queryset, name, value):
        return queryset.filter(resolved_at__isnull=(value == "open"))

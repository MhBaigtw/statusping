from .models import CheckResult, Incident, Monitor


def monitors_for(organization):
    return Monitor.objects.filter(organization=organization)


def checks_for(monitor):
    return CheckResult.objects.filter(monitor=monitor).order_by("-checked_at", "-id")


def incidents_for(organization, *, monitor=None):
    incidents = Incident.objects.select_related("monitor").filter(
        monitor__organization=organization
    )
    if monitor is not None:
        incidents = incidents.filter(monitor=monitor)
    return incidents

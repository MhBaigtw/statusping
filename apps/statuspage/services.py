"""The public status page: one payload per organization, built by SQL aggregation and cached.

Uptime definition (also in the OpenAPI schema and README):

    uptime(window) = 100 * successful checks / all checks, over the CheckResults whose
                     checked_at falls in [now - window, now], rounded to 2 decimals.
    A window with no checks has uptime null ("no data"), never 100%.

Checks of paused periods simply don't exist, so pausing neither helps nor hurts uptime.
All three windows (24h, 7d, 30d) and the last-checked time for every monitor of the
org come from ONE aggregate query (conditional COUNTs), never by loading rows.

Public monitor status (flap-protected, unlike Monitor.last_status):
    paused  - the monitor is paused
    down    - an incident is open
    unknown - never checked
    up      - otherwise
Overall: "down" if every unpaused monitor is down, "degraded" if some are,
"operational" otherwise (including an org with no unpaused monitors).

Privacy: monitor URLs and member emails are never included (a URL may be internal or
carry tokens).
"""

import logging
from datetime import timedelta

from django.conf import settings
from django.core.cache import cache
from django.db import transaction
from django.db.models import Count, Max, Prefetch, Q
from django.utils import timezone

from apps.monitors.models import CheckResult, Incident, Monitor
from apps.organizations.models import Organization

logger = logging.getLogger(__name__)

WINDOWS = {"24h": timedelta(hours=24), "7d": timedelta(days=7), "30d": timedelta(days=30)}
RECENT_INCIDENTS = 5


def cache_key(slug):
    return f"statuspage:v1:{slug}"


def _quietly(operation, *args):
    """Run a cache operation; a broken cache only costs freshness, never the request.

    The cache is an optimisation: a failure here (e.g. the cache table is missing, or the
    DB hiccups) must not fail a check, an incident or an API write. The TTL bounds any
    staleness this causes.
    """
    try:
        return operation(*args)
    except Exception:
        logger.warning("status page cache %s failed", operation.__name__, exc_info=True)
        return None


def invalidate(organization):
    """Drop the org's cached payload once the current transaction commits.

    After commit, so a reader can't re-cache the pre-change data from a transaction
    that hasn't committed yet. Without a transaction it runs immediately.
    """
    key = cache_key(organization.slug)
    transaction.on_commit(lambda: _quietly(cache.delete, key))


def uptime_by_monitor(monitor_ids, now):
    """{monitor_id: {"24h": (ok, total), ..., "last_checked_at": dt}} from one query."""
    oldest = now - max(WINDOWS.values())
    aggregates = {"last_checked_at": Max("checked_at")}
    for label, span in WINDOWS.items():
        since = Q(checked_at__gte=now - span)
        aggregates[f"total_{label}"] = Count("id", filter=since)
        aggregates[f"ok_{label}"] = Count("id", filter=since & Q(ok=True))
    rows = (
        CheckResult.objects.filter(monitor_id__in=monitor_ids, checked_at__gte=oldest)
        .values("monitor_id")
        .annotate(**aggregates)
        .order_by()
    )
    return {row["monitor_id"]: row for row in rows}


def percent(ok, total):
    return round(100 * ok / total, 2) if total else None


def _public_status(monitor, recent):
    if monitor.is_paused:
        return "paused"
    if recent and recent[0].resolved_at is None:
        return "down"  # the open incident is always the newest one
    if monitor.last_status == Monitor.Status.UNKNOWN:
        return "unknown"
    return "up"


def _overall(statuses):
    active = [s for s in statuses if s != "paused"]
    down = sum(1 for s in active if s == "down")
    if active and down == len(active):
        return "down"
    return "degraded" if down else "operational"


def build_payload(organization, now=None):
    now = now or timezone.now()
    monitors = list(
        Monitor.objects.filter(organization=organization)
        .order_by("name", "id")
        .only("id", "name", "is_paused", "last_status")
        .prefetch_related(
            Prefetch(
                "incidents",
                queryset=Incident.objects.order_by("-started_at", "-id")[:RECENT_INCIDENTS],
                to_attr="recent_incidents",
            )
        )
    )
    stats = uptime_by_monitor([m.pk for m in monitors], now)
    entries = []
    for monitor in monitors:
        row = stats.get(monitor.pk, {})
        recent = monitor.recent_incidents
        entries.append(
            {
                "name": monitor.name,
                "status": _public_status(monitor, recent),
                "last_checked_at": row.get("last_checked_at"),
                "uptime": {
                    label: percent(row.get(f"ok_{label}", 0), row.get(f"total_{label}", 0))
                    for label in WINDOWS
                },
                "incidents": [
                    {
                        "started_at": incident.started_at,
                        "resolved_at": incident.resolved_at,
                        "duration_seconds": (
                            round((incident.resolved_at - incident.started_at).total_seconds())
                            if incident.resolved_at
                            else None
                        ),
                    }
                    for incident in recent
                ],
            }
        )
    return {
        "organization": {"name": organization.name, "slug": organization.slug},
        "status": _overall([e["status"] for e in entries]),
        "generated_at": now,
        "monitors": entries,
    }


def get_status_payload(slug):
    """The cached payload for a public status page, or None (unknown or opted out -> 404)."""
    ttl = settings.STATUS_PAGE_CACHE_SECONDS
    if ttl:
        payload = _quietly(cache.get, cache_key(slug))
        if payload is not None:
            return payload
    organization = Organization.objects.filter(slug=slug, status_page_public=True).first()
    if organization is None:
        return None
    payload = build_payload(organization)
    if ttl:
        _quietly(cache.set, cache_key(slug), payload, ttl)
    return payload

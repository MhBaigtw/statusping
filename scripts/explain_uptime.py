"""EXPLAIN (ANALYZE, BUFFERS) of the status page uptime aggregate (docs/benchmarks.md, C2).

Prints the exact SQL Django generates for one org, its plan, and the same plan with a
candidate covering index created inside a transaction that is rolled back.

    .\\.venv\\Scripts\\python.exe scripts\\explain_uptime.py --slug bulk1-01
"""

import argparse
import os
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.dev")

import django  # noqa: E402

django.setup()

from django.db import connection, transaction  # noqa: E402
from django.utils import timezone  # noqa: E402

from apps.monitors.models import Monitor  # noqa: E402

CANDIDATE = (
    "CREATE INDEX bench_covering ON monitors_checkresult (monitor_id, checked_at) INCLUDE (ok)"
)


class Rollback(Exception):
    pass


def explain(cursor, sql, params, label):
    cursor.execute("EXPLAIN (ANALYZE, BUFFERS, COSTS OFF) " + sql, params)
    print(f"--- {label}")
    for (line,) in cursor.fetchall():
        print("   ", line)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--slug", default="bulk1-01")
    parser.add_argument("--candidate", action="store_true", help="also try the covering index")
    parser.add_argument(
        "--try-sql", action="append", default=[], help="extra DDL to run first (rolled back)"
    )
    args = parser.parse_args()
    ids = list(Monitor.objects.filter(organization__slug=args.slug).values_list("pk", flat=True))
    # Rebuild the queryset uptime_by_monitor() runs, to get its SQL.
    from datetime import timedelta

    from django.db.models import Count, Max, Q

    from apps.monitors.models import CheckResult
    from apps.statuspage.services import WINDOWS

    now = timezone.now()
    aggregates = {"last_checked_at": Max("checked_at")}
    for label, span in WINDOWS.items():
        since = Q(checked_at__gte=now - span)
        aggregates[f"total_{label}"] = Count("id", filter=since)
        aggregates[f"ok_{label}"] = Count("id", filter=since & Q(ok=True))
    qs = (
        CheckResult.objects.filter(monitor_id__in=ids, checked_at__gte=now - timedelta(days=30))
        .values("monitor_id")
        .annotate(**aggregates)
        .order_by()
    )
    sql, params = qs.query.sql_with_params()
    print(sql % tuple(repr(p) for p in params) if len(sql) < 2000 else sql[:2000])
    try:
        with transaction.atomic(), connection.cursor() as cursor:
            explain(cursor, sql, params, "as migrated")
            explain(cursor, sql, params, "as migrated (repeat, warm)")
            for statement in args.try_sql:
                cursor.execute(statement)
                explain(cursor, sql, params, f"after: {statement}")
                explain(cursor, sql, params, f"after: {statement} (repeat, warm)")
            if args.candidate:
                cursor.execute(CANDIDATE)
                cursor.execute("ANALYZE monitors_checkresult")
                explain(
                    cursor, sql, params, "with covering index (monitor_id, checked_at) INCLUDE (ok)"
                )
                explain(cursor, sql, params, "with covering index (repeat, warm)")
            raise Rollback
    except Rollback:
        print("rolled back")


if __name__ == "__main__":
    main()

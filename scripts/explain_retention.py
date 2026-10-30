"""EXPLAIN (ANALYZE, BUFFERS) of the CheckResult retention query, with and without a BRIN
index on checked_at (created inside a transaction that is rolled back). Part D1.

    .\\.venv\\Scripts\\python.exe scripts\\explain_retention.py
"""

import os
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.dev")

import django  # noqa: E402

django.setup()

from django.db import connection, transaction  # noqa: E402

# The batch query purge_check_results() runs: ids of the oldest expired rows.
QUERY = (
    "SELECT id FROM monitors_checkresult WHERE checked_at < now() - %s::interval "
    "ORDER BY checked_at LIMIT 1000"
)
CASES = [("29 days", "some rows expired"), ("31 days", "nothing expired (the usual case)")]


class Rollback(Exception):
    pass


def explain(cursor, label):
    for age, what in CASES:
        cursor.execute("EXPLAIN (ANALYZE, BUFFERS, COSTS OFF) " + QUERY, [age])
        lines = [line for (line,) in cursor.fetchall()]
        keep = [ln for ln in lines if any(k in ln for k in ("Scan", "Execution Time", "Buffers"))]
        print(f"--- {label}: cutoff {age} ({what})")
        for line in keep:
            print("   ", line)


try:
    with transaction.atomic(), connection.cursor() as cursor:
        explain(cursor, "no index on checked_at")
        cursor.execute("CREATE INDEX bench_brin ON monitors_checkresult USING brin (checked_at)")
        cursor.execute("ANALYZE monitors_checkresult")
        cursor.execute("SELECT pg_size_pretty(pg_relation_size('bench_brin'))")
        print("BRIN index size:", cursor.fetchone()[0])
        explain(cursor, "BRIN (checked_at)")
        cursor.execute("DROP INDEX bench_brin")
        # Option without a new index: purge monitor by monitor through the existing
        # composite (monitor, -checked_at) index, as purge_check_results() does.
        import time

        cursor.execute("SELECT id FROM monitors_monitor")
        monitor_ids = [row[0] for row in cursor.fetchall()]
        per_monitor = (
            "SELECT id FROM monitors_checkresult WHERE monitor_id = %s "
            "AND checked_at < now() - %s::interval ORDER BY checked_at LIMIT 1000"
        )
        for age, what in CASES:
            started = time.perf_counter()
            found = 0
            for monitor_id in monitor_ids:
                cursor.execute(per_monitor, [monitor_id, age])
                found += len(cursor.fetchall())
            elapsed = (time.perf_counter() - started) * 1000
            print(
                f"--- per monitor via composite index: cutoff {age} ({what}): "
                f"{len(monitor_ids)} monitors, {found} ids, {elapsed:.0f} ms total"
            )
        cursor.execute("EXPLAIN (ANALYZE, COSTS OFF) " + per_monitor, [monitor_ids[0], "29 days"])
        print("    plan for one monitor:", " / ".join(line for (line,) in cursor.fetchall()[:2]))
        cursor.execute("CREATE INDEX bench_btree ON monitors_checkresult (checked_at)")
        cursor.execute("SELECT pg_size_pretty(pg_relation_size('bench_btree'))")
        print("B-tree index size:", cursor.fetchone()[0])
        explain(cursor, "B-tree (checked_at)")
        raise Rollback
except Rollback:
    print("rolled back")

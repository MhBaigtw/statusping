"""Rolled-back EXPLAIN (ANALYZE) of the claim query with different index sets (D9).

Inserts 200k succeeded + 2k queued jobs into the dev DB inside a transaction that is
always rolled back, so the database is left unchanged.

    .\\.venv\\Scripts\\python.exe scripts\\explain_claim.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.dev")
import django  # noqa: E402

django.setup()
from django.db import connection, transaction  # noqa: E402

CLAIM = """
EXPLAIN (ANALYZE, BUFFERS, COSTS OFF, TIMING ON)
SELECT * FROM jobs_job WHERE status = 'queued' AND run_after <= now()
ORDER BY run_after, id LIMIT 1 FOR UPDATE SKIP LOCKED
"""


class Rollback(Exception):
    pass


def plan(cur, label):
    cur.execute(CLAIM)
    print(f"--- {label}")
    for (line,) in cur.fetchall():
        print("   ", line)


try:
    with transaction.atomic(), connection.cursor() as cur:
        cur.execute(
            "INSERT INTO jobs_job (kind, payload, status, run_after, attempts, max_attempts,"
            " locked_by, last_error, created_at, finished_at)"
            " SELECT 'run_check', '{}', 'succeeded', now() - (g || ' seconds')::interval, 1, 1,"
            " '', '', now(), now() FROM generate_series(1, 200000) g"
        )
        cur.execute(
            "INSERT INTO jobs_job (kind, payload, status, run_after, attempts, max_attempts,"
            " locked_by, last_error, created_at)"
            " SELECT 'run_check', '{}', 'queued', now() + ((g - 1000) || ' seconds')::interval,"
            " 0, 1, '', '', now() FROM generate_series(1, 2000) g"
        )
        cur.execute("ANALYZE jobs_job")
        plan(cur, "only (status, run_after) (as migrated)")
        cur.execute("CREATE INDEX job_queued_idx ON jobs_job (run_after) WHERE status = 'queued'")
        cur.execute("ANALYZE jobs_job")
        plan(cur, "plus partial (run_after) WHERE queued")
        cur.execute("DROP INDEX job_status_run_after_idx")
        plan(cur, "only partial (run_after) WHERE queued")
        cur.execute("DROP INDEX job_queued_idx")
        plan(cur, "no index")
        cur.execute(
            "SELECT pg_size_pretty(pg_relation_size('jobs_job_pkey')), count(*) FROM jobs_job"
        )
        print("rows:", cur.fetchone())
        raise Rollback
except Rollback:
    print("rolled back")

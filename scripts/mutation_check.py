# ruff: noqa: E501  (the mutation table reads better unwrapped)
"""Mutation check: remove each key concurrency/idempotency guarantee and confirm tests fail.

Each mutation edits source files, runs the relevant tests, and restores the files with
`git checkout` (so run it on a clean working tree). Usage:

    .\\.venv\\Scripts\\python.exe scripts\\mutation_check.py
"""

import re
import subprocess
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent

MUTATIONS = [
    (
        "M1 remove SKIP LOCKED from the job claim",
        [("apps/jobs/queue.py", "Job.objects.select_for_update(skip_locked=True)", "Job.objects.select_for_update()")],
        ["tests/test_queue.py", "tests/test_worker.py"],
    ),
    (
        "M2 remove SKIP LOCKED from the monitor dispatch",
        [("apps/monitors/services.py", "Monitor.objects.select_for_update(skip_locked=True)", "Monitor.objects.select_for_update()")],
        ["tests/test_checks.py"],
    ),
    (
        "M1b remove the row lock from the job claim entirely",
        [("apps/jobs/queue.py", "Job.objects.select_for_update(skip_locked=True)", "Job.objects")],
        ["tests/test_queue.py"],
    ),
    (
        "M2b remove the row lock from the monitor dispatch entirely",
        [("apps/monitors/services.py", "Monitor.objects.select_for_update(skip_locked=True)", "Monitor.objects")],
        ["tests/test_checks.py"],
    ),
    (
        "M5 drop the total deadline (per-read timeouts only, like plain httpx)",
        [("apps/core/safe_http.py", "return self._stream.read(max_bytes, self._budget(timeout, httpcore.ReadTimeout))", "return self._stream.read(max_bytes, timeout)")],
        ["tests/test_deadline.py"],
    ),
    (
        "M3 remove the locked_by guard on finish",
        [("apps/jobs/queue.py", "status=S.RUNNING, locked_by=worker_id)", "status=S.RUNNING)")],
        ["tests/test_queue.py"],
    ),
    (
        "M4a remove the delivery row lock",
        [("apps/alerts/services.py", 'AlertDelivery.objects.select_for_update(of=("self",))', "AlertDelivery.objects")],
        ["tests/test_alerts.py"],
    ),
    (
        "M4b remove the delivery uniqueness constraint (model + migration)",
        [
            ("apps/alerts/models.py", 'models.UniqueConstraint(\n                fields=["incident", "channel", "event"], name="unique_alert_delivery"\n            ),', ""),
            ("apps/alerts/migrations/0001_initial.py", "models.UniqueConstraint(fields=('incident', 'channel', 'event'), name='unique_alert_delivery')", ""),
        ],
        ["tests/test_alerts.py", "tests/test_checks.py"],
    ),
]  # fmt: skip


def main():
    python = sys.executable
    dirty = subprocess.run(
        ["git", "status", "--porcelain"], cwd=BASE, capture_output=True, text=True
    )
    if dirty.stdout.strip():
        sys.exit("Working tree is not clean; commit first (files are restored with git checkout).")
    survived = []
    for title, edits, tests in MUTATIONS:
        files = sorted({path for path, _, _ in edits})
        try:
            for path, old, new in edits:
                source = (BASE / path).read_text()
                if old not in source:
                    sys.exit(f"{title}: pattern not found in {path}")
                (BASE / path).write_text(source.replace(old, new, 1))
            result = subprocess.run(
                [python, "-m", "pytest", *tests, "-q", "--no-cov", "-p", "no:cacheprovider"],
                cwd=BASE,
                capture_output=True,
                text=True,
            )
        finally:
            subprocess.run(["git", "checkout", "--", *files], cwd=BASE, check=True)
        failed = re.findall(r"^FAILED (\S+)", result.stdout, re.M)
        summary = result.stdout.strip().splitlines()[-1] if result.stdout.strip() else "(no output)"
        print(f"\n== {title}\n   {summary}")
        for name in failed:
            print(f"   FAILED {name}")
        if not failed:
            survived.append(title)
    print("\nAll mutations caught." if not survived else f"\nSURVIVED: {survived}")
    return 1 if survived else 0


if __name__ == "__main__":
    sys.exit(main())

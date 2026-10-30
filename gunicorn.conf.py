"""gunicorn settings for the web process (Linux hosting; gunicorn does not run on Windows).

    gunicorn config.wsgi:application -c gunicorn.conf.py

Environment:
  PORT             port to bind (the platform sets it; default 8000)
  WEB_CONCURRENCY  gunicorn worker processes (default 2)
  EMBEDDED_WORKER  "true" = ALSO run the job worker + scheduler as threads inside the web
                   process (hosting option (c) in docs/deploy.md: one free service). Forces
                   a single gunicorn process, so there is exactly one job runner. If the
                   platform puts the web service to sleep, MONITORING STOPS with it.
  WORKER_THREADS   job threads for the embedded worker (default 2 to fit 512 MB)
"""

import os

bind = f"0.0.0.0:{os.environ.get('PORT', '8000')}"
workers = int(os.environ.get("WEB_CONCURRENCY", "2"))
timeout = 30
graceful_timeout = 30
accesslog = "-"
errorlog = "-"

EMBEDDED_WORKER = os.environ.get("EMBEDDED_WORKER", "").strip().lower() in ("1", "true", "yes")
if EMBEDDED_WORKER:
    workers = 1


def post_worker_init(worker):
    """Runs in the gunicorn worker process after Django is loaded."""
    if not EMBEDDED_WORKER:
        return
    from apps.jobs.worker import Worker

    job_worker = Worker(threads=int(os.environ.get("WORKER_THREADS", "2")))
    job_worker.start()
    worker.statusping_job_worker = job_worker


def worker_exit(server, worker):
    """Graceful stop: no new claims; in-flight jobs get up to 25 s (gunicorn allows 30)."""
    job_worker = getattr(worker, "statusping_job_worker", None)
    if job_worker is not None:
        job_worker.stop()
        job_worker.join(25)

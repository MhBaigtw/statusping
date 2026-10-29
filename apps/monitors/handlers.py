from . import services


def run_check(job):
    services.run_check(job.payload["monitor_id"], job_id=job.pk)

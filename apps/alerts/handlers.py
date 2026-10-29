from . import services


def deliver_alert(job):
    services.deliver_alert(
        job.payload["delivery_id"], final_attempt=job.attempts >= job.max_attempts
    )


def send_test_alert(job):
    services.send_test_alert(job.payload["channel_id"])

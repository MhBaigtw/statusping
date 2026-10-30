"""Job kind -> handler (dotted path). Imported lazily so apps can enqueue each other's jobs
without import cycles. A handler takes the claimed Job and must be idempotent."""

from django.utils.module_loading import import_string

from .exceptions import PermanentError

HANDLERS = {
    "run_check": "apps.monitors.handlers.run_check",
    "deliver_alert": "apps.alerts.handlers.deliver_alert",
    "send_test_alert": "apps.alerts.handlers.send_test_alert",
    "noop": "apps.jobs.handlers.noop",  # queue benchmark only
}


def get_handler(kind):
    try:
        return import_string(HANDLERS[kind])
    except KeyError:
        raise PermanentError(f"Unknown job kind {kind!r}.") from None

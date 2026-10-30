import logging
import re
import time

# Webhook URLs are bearer secrets: anyone holding one can post to the channel. These
# patterns cover the paths of the providers StatusPing accepts (D16). Host and scheme
# stay visible; everything after the provider's path prefix becomes ****.
_SECRET_URL = re.compile(
    r"(?P<keep>https?://[^\s/'\"]+/(?:api/webhooks|services))/[^\s'\"<>)]+",
    re.IGNORECASE,
)


def mask_secrets(text):
    """'https://discord.com/api/webhooks/1/abc' -> 'https://discord.com/api/webhooks/****'."""
    if not text:
        return text
    return _SECRET_URL.sub(r"\g<keep>/****", str(text))


class UTCFormatter(logging.Formatter):
    """Log timestamps in UTC (the project's one timezone), marked with a Z."""

    converter = time.gmtime
    default_time_format = "%Y-%m-%dT%H:%M:%S"
    default_msec_format = "%s.%03dZ"


class SecretMaskingFilter(logging.Filter):
    """Defense in depth on the console handler: mask webhook URLs in every message and
    traceback, even from library code. Application code also masks at the source."""

    def filter(self, record):
        message = record.getMessage()
        masked = mask_secrets(message)
        if masked != message:
            record.msg, record.args = masked, None
        if record.exc_info:
            record.exc_text = mask_secrets(logging.Formatter().formatException(record.exc_info))
            record.exc_info = None  # the formatter prints the masked exc_text instead
        elif record.exc_text:
            record.exc_text = mask_secrets(record.exc_text)
        return True

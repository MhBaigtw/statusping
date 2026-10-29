import logging
import time


class UTCFormatter(logging.Formatter):
    """Log timestamps in UTC (the project's one timezone), marked with a Z."""

    converter = time.gmtime
    default_time_format = "%Y-%m-%dT%H:%M:%S"
    default_msec_format = "%s.%03dZ"

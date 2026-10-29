class RetryLater(Exception):  # noqa: N818 (a control-flow signal, not an error)
    """Raised by a handler: this attempt failed but may succeed later.

    `retry_at` overrides the queue's exponential backoff (e.g. an HTTP Retry-After).
    """

    def __init__(self, reason, retry_at=None):
        super().__init__(reason)
        self.retry_at = retry_at


class PermanentError(Exception):
    """Raised by a handler: retrying cannot help. The job is marked failed."""

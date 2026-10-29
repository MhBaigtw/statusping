import threading

from django.db import connection


def run_in_threads(target, count, *args):
    """Start `count` threads together (barrier) running target(index, *args); return results.

    Each thread has its own DB connection and closes it, so the test DB can be dropped.
    """
    barrier = threading.Barrier(count)
    results = [None] * count
    errors = []

    def wrapper(index):
        try:
            barrier.wait()
            results[index] = target(index, *args)
        except Exception as exc:  # surfaced in the main thread below
            errors.append(exc)
        finally:
            connection.close()

    threads = [threading.Thread(target=wrapper, args=(i,)) for i in range(count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)
    assert not errors, errors
    return results


class HoldLock(threading.Thread):
    """In its own connection: run `lock()` inside a transaction and hold it until released."""

    def __init__(self, lock):
        super().__init__(daemon=True)
        self.lock = lock
        self.locked = threading.Event()
        self.release = threading.Event()

    def run(self):
        from django.db import transaction

        try:
            with transaction.atomic():
                self.lock()
                self.locked.set()
                self.release.wait(10)
        finally:
            connection.close()

    def __enter__(self):
        self.start()
        assert self.locked.wait(10), "lock holder never acquired its lock"
        return self

    def __exit__(self, *exc):
        self.release.set()
        self.join(10)

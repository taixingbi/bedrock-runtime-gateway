"""Keep processing ownership alive while a blocking operation runs."""
import logging
import threading
from contextlib import contextmanager


@contextmanager
def heartbeat(renew, interval_s=20):
    stop = threading.Event()
    failures = []
    renew()  # Fail before execution if initial renewal is unavailable.

    def run():
        while not stop.wait(interval_s):
            try:
                renew()
            except Exception as exc:
                failures.append(exc)
                logging.getLogger(__name__).exception("processing heartbeat failed")
                return

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    try:
        yield
        if failures:
            raise failures[0]  # Do not acknowledge the SQS message.
    finally:
        stop.set()
        thread.join()

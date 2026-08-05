import asyncio
import logging
import sys

from worker.client import run
from worker.logs import setup_logging
from worker.process_lock import acquire_exclusive_lock
from worker.settings import get_settings


def main() -> None:
    settings = get_settings()
    setup_logging(settings.log_format)
    log = logging.getLogger("potocolom.worker")
    lock_handle = None
    if settings.worker_lock:
        try:
            lock_handle = acquire_exclusive_lock(settings.worker_lock)
        except BlockingIOError:
            log.error(
                "another worker already holds %s; refuse to start a second local worker",
                settings.worker_lock,
            )
            sys.exit(1)
    log.info(
        "starting: id=%s device=%s api_url=%s",
        settings.worker_id, settings.device, settings.api_url)
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass
    finally:
        if lock_handle is not None:
            lock_handle.close()


if __name__ == "__main__":
    main()

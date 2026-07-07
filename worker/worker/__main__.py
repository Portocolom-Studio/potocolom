import asyncio
import logging

from worker.client import run
from worker.logs import setup_logging
from worker.settings import get_settings


def main() -> None:
    settings = get_settings()
    setup_logging(settings.log_format)
    logging.getLogger("potocolom.worker").info(
        "starting: id=%s device=%s api_url=%s",
        settings.worker_id, settings.device, settings.api_url)
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()

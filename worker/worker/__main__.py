import asyncio

from worker.client import run
from worker.settings import get_settings


def main() -> None:
    settings = get_settings()
    print(f"potocolom worker: id={settings.worker_id} device={settings.device} "
          f"api_url={settings.api_url}", flush=True)
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()

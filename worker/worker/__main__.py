from worker.settings import get_settings


def main() -> None:
    settings = get_settings()
    print(f"potocolom worker: device={settings.device} api_url={settings.api_url}")
    print("The inference pipeline and fleet connection arrive with issue #15.")


if __name__ == "__main__":
    main()

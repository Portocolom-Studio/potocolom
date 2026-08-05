import importlib.util
from contextlib import nullcontext
from pathlib import Path
from types import ModuleType


class RecordingOperations:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple, dict]] = []

    def get_context(self) -> "RecordingOperations":
        return self

    def autocommit_block(self):
        return nullcontext()

    def drop_index(self, *args, **kwargs) -> None:
        self.calls.append(("drop", args, kwargs))

    def create_index(self, *args, **kwargs) -> None:
        self.calls.append(("create", args, kwargs))


def load_migration() -> ModuleType:
    path = Path(__file__).parents[1] / "migrations/versions/0012_job_source_asset_index.py"
    spec = importlib.util.spec_from_file_location("migration_0012", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_upgrade_replaces_an_existing_index_concurrently() -> None:
    migration = load_migration()
    operations = RecordingOperations()
    migration.op = operations

    migration.upgrade()

    assert operations.calls == [
        (
            "drop",
            ("jobs_source_asset",),
            {
                "table_name": "jobs",
                "postgresql_concurrently": True,
                "if_exists": True,
            },
        ),
        (
            "create",
            ("jobs_source_asset", "jobs", ["source_asset_id"]),
            {"postgresql_concurrently": True},
        ),
    ]

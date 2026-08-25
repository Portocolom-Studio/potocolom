"""A dropped column is the one migration a downgrade cannot undo.

0022 removes `assets.share_token`. Running it destroys whatever the column
held, so the downgrade owes an older release a schema it boots against and
nothing more; these tests pin both halves of that.
"""

import importlib.util
from pathlib import Path
from types import ModuleType

import sqlalchemy as sa


class RecordingOperations:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple, dict]] = []

    def drop_column(self, *args, **kwargs) -> None:
        self.calls.append(("drop_column", args, kwargs))

    def add_column(self, *args, **kwargs) -> None:
        self.calls.append(("add_column", args, kwargs))


def load_migration() -> ModuleType:
    path = Path(__file__).parents[1] / "migrations/versions/0022_drop_asset_share_token.py"
    spec = importlib.util.spec_from_file_location("migration_0022", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_upgrade_drops_only_the_retired_share_column() -> None:
    migration = load_migration()
    operations = RecordingOperations()
    migration.op = operations

    migration.upgrade()

    assert operations.calls == [("drop_column", ("assets", "share_token"), {})]


def test_downgrade_restores_a_column_the_previous_release_can_start_against() -> None:
    migration = load_migration()
    operations = RecordingOperations()
    migration.op = operations

    migration.downgrade()

    (name, args, _), = operations.calls
    assert name == "add_column"
    table, column = args
    assert table == "assets"
    assert column.name == "share_token"
    assert isinstance(column.type, sa.Text)
    # Nullable and unfilled: the rows are already there and the values are
    # gone, so anything stricter would refuse to run at all.
    assert column.nullable
    assert column.server_default is None

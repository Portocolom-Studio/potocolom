from app.db import _postgres_version_supported


def test_postgres_version_comparison():
    assert not _postgres_version_supported((12, 99))
    assert _postgres_version_supported((13, 0))
    assert _postgres_version_supported((16, 3))

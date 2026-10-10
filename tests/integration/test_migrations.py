from revos.db import migrate


def test_migrate_is_idempotent(conn) -> None:
    assert migrate(conn) == []

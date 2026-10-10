import os

import psycopg
import pytest

from revos.db import migrate

PORT = os.environ.get("POSTGRES_PORT", "5434")
BASE = f"postgresql://revos:revos@localhost:{PORT}"
TEST_DB = "revos_test"


@pytest.fixture(scope="session")
def test_db_url() -> str:
    """A separate database so tests never touch dev data. Real Postgres, no mocks."""
    try:
        admin = psycopg.connect(f"{BASE}/postgres", autocommit=True, connect_timeout=3)
    except psycopg.OperationalError as exc:
        pytest.fail(f"Postgres not reachable on port {PORT}: run `docker compose up -d --wait db` ({exc})")
    with admin:
        exists = admin.execute("SELECT 1 FROM pg_database WHERE datname = %s", (TEST_DB,)).fetchone()
        if not exists:
            admin.execute(f"CREATE DATABASE {TEST_DB}")
    url = f"{BASE}/{TEST_DB}"
    with psycopg.connect(url) as conn:
        migrate(conn)
    return url


@pytest.fixture
def conn(test_db_url: str):
    """Autocommit connection on a clean database; tests open their own transactions."""
    with psycopg.connect(test_db_url, autocommit=True) as c:
        tables = [r[0] for r in c.execute(
            "SELECT tablename FROM pg_tables WHERE schemaname = 'public' "
            "AND tablename NOT IN ('schema_migrations', 'settings')")]
        c.execute("TRUNCATE " + ", ".join(tables) + " RESTART IDENTITY CASCADE")
        c.execute("INSERT INTO segments (id, pack_version) VALUES ('test_seg', '1')")
        yield c

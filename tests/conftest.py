import os

import psycopg
import pytest

from revos.adapters.clock import FakeClock
from revos.db import migrate

# The tests never read the developer's .env. Only tests/live does, and only with `-m live`.
os.environ["REVOS_NO_DOTENV"] = "1"

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
        c.execute("UPDATE settings SET value = 'false' WHERE key = 'kill_switch'")
        c.execute("UPDATE settings SET value = '\"autonomous\"' WHERE key = 'autonomy_mode'")
        yield c


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def conn2(test_db_url: str):
    """A second connection, to model a second worker or a restarted process."""
    with psycopg.connect(test_db_url, autocommit=True) as c:
        yield c


# --- segment packs (no database needed) ---------------------------------------------------

import shutil  # noqa: E402
from pathlib import Path  # noqa: E402

PACKS_DIR = Path(__file__).resolve().parent.parent / "packs"
PACK_ID = "franchise_brands"


@pytest.fixture(scope="session")
def pack():
    from revos.segments import load_pack
    return load_pack(PACKS_DIR / PACK_ID)


@pytest.fixture
def pack_copy(tmp_path: Path) -> Path:
    """A throwaway copy of the real pack that a test can break."""
    target = tmp_path / PACK_ID
    shutil.copytree(PACKS_DIR / PACK_ID, target)
    return target


@pytest.fixture
def ro_conn(test_db_url: str):
    """Read-only connection, the kind agent tools are given."""
    from revos.db import connect_read_only
    with connect_read_only(test_db_url) as c:
        yield c

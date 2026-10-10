import os

import psycopg
import pytest

PORT = os.environ.get("POSTGRES_PORT", "5434")
URL = os.environ.get("DATABASE_URL", f"postgresql://revos:revos@localhost:{PORT}/revos")


@pytest.mark.db
def test_postgres_reachable() -> None:
    try:
        conn = psycopg.connect(URL, connect_timeout=3)
    except psycopg.OperationalError:
        pytest.skip("Postgres not running: docker compose up -d db")
    with conn:
        assert conn.execute("select 1").fetchone() == (1,)

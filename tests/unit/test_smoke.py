from fastapi.testclient import TestClient

from revos.api.main import app


def test_health() -> None:
    assert TestClient(app).get("/health").json() == {"status": "ok"}

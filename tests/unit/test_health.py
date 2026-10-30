from fastapi.testclient import TestClient

from app.main import create_app
from tests.fakes import fake_container


def test_health_ok() -> None:
    with TestClient(create_app(fake_container())) as client:
        assert client.get("/health").json() == {"status": "ok"}

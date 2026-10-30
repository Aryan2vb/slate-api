from fastapi.testclient import TestClient

from app.main import create_app
from tests.fakes import fake_container


def test_health_ok() -> None:
    with TestClient(create_app(fake_container())) as client:
        assert client.get("/health").json() == {"status": "ok"}


def test_docs_hidden_in_prod() -> None:
    from app.config import Settings

    with TestClient(create_app(fake_container(settings=Settings(env="prod", run_worker_in_process=False)))) as client:
        assert client.get("/docs").status_code == 404
        assert client.get("/openapi.json").status_code == 404
        assert client.get("/health").status_code == 200

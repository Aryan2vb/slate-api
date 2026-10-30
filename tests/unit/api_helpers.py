"""Small helpers shared by the API tests in this package."""

import uuid
from collections.abc import Iterator
from contextlib import contextmanager

from fastapi.testclient import TestClient

from app.container import Container
from app.main import create_app
from tests.fakes import fake_container


def auth(user_id: uuid.UUID) -> dict[str, str]:
    return {"Authorization": f"Bearer user:{user_id}"}


@contextmanager
def client(container: Container | None = None) -> Iterator[tuple[TestClient, Container]]:
    container = container or fake_container()
    with TestClient(create_app(container)) as c:
        yield c, container


def create_meeting(c: TestClient, user: uuid.UUID, **body: object) -> dict:
    r = c.post("/meetings", json={"title": "Weekly sync", **body}, headers=auth(user))
    assert r.status_code == 201, r.text
    return r.json()

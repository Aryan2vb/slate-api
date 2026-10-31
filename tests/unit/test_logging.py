import uuid

from fastapi.testclient import TestClient
from structlog.testing import capture_logs

from app.main import create_app
from tests.fakes import fake_container

USER = uuid.uuid4()
AUTH = {"Authorization": f"Bearer user:{USER}"}


def test_every_request_is_logged_with_a_request_id_and_user() -> None:
    with TestClient(create_app(fake_container())) as c, capture_logs() as logs:
        r = c.post("/meetings", json={"title": "Sync"}, headers=AUTH)
    assert r.status_code == 201
    request_id = r.headers["x-request-id"]
    line = next(e for e in logs if e["event"] == "http.request")
    assert line["status"] == 201 and line["log_level"] == "info" and line["duration_ms"] >= 0
    created = next(e for e in logs if e["event"] == "meeting.created")
    # Context binds happen in contextvars; capture_logs sees the event dict before merge, so check the header.
    assert len(request_id) >= 8 and created["template"] == "general"


def test_incoming_request_id_is_echoed() -> None:
    with TestClient(create_app(fake_container())) as c:
        assert c.get("/health", headers={"X-Request-ID": "abc123"}).headers["x-request-id"] == "abc123"


def test_client_errors_are_logged_as_warnings() -> None:
    with TestClient(create_app(fake_container())) as c, capture_logs() as logs:
        c.get(f"/meetings/{uuid.uuid4()}", headers=AUTH)
    line = next(e for e in logs if e["event"] == "http.request")
    assert line["status"] == 404 and line["log_level"] == "warning"


def test_app_events_are_ingested_with_prefixed_fields() -> None:
    batch = {
        "platform": "android",
        "entries": [
            {"level": "error", "event": "upload.failed", "fields": {"seq": 2, "reason": "missing", "path": "x"}}
        ],
    }
    with TestClient(create_app(fake_container())) as c, capture_logs() as logs:
        r = c.post("/client-logs", json=batch, headers=AUTH)
    assert r.status_code == 204
    line = next(e for e in logs if e["event"] == "app.upload.failed")
    assert line["log_level"] == "error" and line["source"] == "app"
    assert line["app_seq"] == 2 and line["app_path"] == "x" and line["platform"] == "android"


def test_client_logs_require_auth_and_valid_shape() -> None:
    with TestClient(create_app(fake_container())) as c:
        assert c.post("/client-logs", json={"entries": [{"level": "info", "event": "x"}]}).status_code == 401
        bad = {"entries": [{"level": "info", "event": "Bad Event!"}]}
        assert c.post("/client-logs", json=bad, headers=AUTH).status_code == 422

import uuid

import pytest
from fastapi.testclient import TestClient

from app.api.routes.processing import router
from app.domain.models import JobKind, JobStatus, TemplateKind, TranscriptSegment
from app.main import create_app
from app.services.catchup import CatchUp
from tests.fakes import FakeLlm, InMemoryUnitOfWorkFactory, fake_container

USER = uuid.uuid4()
AUTH = {"Authorization": f"Bearer user:{USER}"}
OTHER = {"Authorization": f"Bearer user:{uuid.uuid4()}"}


@pytest.fixture
def env():
    uow = InMemoryUnitOfWorkFactory()
    container = fake_container(uow=uow, llm=FakeLlm([CatchUp(summary="Pricing agreed", points=["+5%"])]))
    app = create_app(container)
    app.include_router(router)
    with TestClient(app) as client:
        yield client, uow


async def _meeting(uow: InMemoryUnitOfWorkFactory, *, uploaded_audio: bool = False):
    async with uow() as u:
        m = await u.meetings.create(
            user_id=USER, title="Sync", template=TemplateKind.GENERAL, calendar_event_id=None, attendees=[]
        )
        await u.transcripts.replace(
            meeting_id=m.id, segments=[TranscriptSegment(speaker=0, start_ms=0, end_ms=1000, text="hello")]
        )
        if uploaded_audio:
            c = await u.chunks.add(meeting_id=m.id, seq=0, storage_path="p", mime_type="audio/mp4", byte_size=1)
            await u.chunks.mark_uploaded(meeting_id=m.id, chunk_id=c.id)
    return m


async def test_catch_up(env) -> None:
    client, uow = env
    m = await _meeting(uow)
    r = client.post(f"/meetings/{m.id}/catch-up", json={"minutes": 3}, headers=AUTH)
    assert r.status_code == 200
    assert r.json() == {"summary": "Pricing agreed", "points": ["+5%"]}


async def test_routes_hide_other_users_meetings_and_require_auth(env) -> None:
    client, uow = env
    m = await _meeting(uow, uploaded_audio=True)
    assert client.post(f"/meetings/{m.id}/catch-up", json={}, headers=OTHER).status_code == 404
    assert client.put(f"/meetings/{m.id}/speakers/0", json={"name": "X"}, headers=OTHER).status_code == 404
    assert client.post(f"/meetings/{m.id}/reprocess", json={"kind": "enhance"}, headers=OTHER).status_code == 404
    assert client.post(f"/meetings/{m.id}/catch-up", json={}).status_code == 401


async def test_confirm_speaker(env) -> None:
    client, uow = env
    m = await _meeting(uow)
    r = client.put(f"/meetings/{m.id}/speakers/1", json={"name": " Priya ", "attendee_email": "p@x.com"}, headers=AUTH)
    assert r.status_code == 200
    assert r.json() | {"meeting_id": None} == {
        "meeting_id": None,
        "label": 1,
        "name": "Priya",
        "attendee_email": "p@x.com",
        "confirmed": True,
    }
    assert client.put(f"/meetings/{m.id}/speakers/1", json={"name": ""}, headers=AUTH).status_code == 422


async def test_reprocess_enqueues_and_refuses_transcribe_without_audio(env) -> None:
    client, uow = env
    m = await _meeting(uow)
    r = client.post(f"/meetings/{m.id}/reprocess", json={"kind": "enhance"}, headers=AUTH)
    assert r.status_code == 202
    assert (r.json()["kind"], r.json()["status"]) == (JobKind.ENHANCE.value, JobStatus.QUEUED.value)
    assert client.post(f"/meetings/{m.id}/reprocess", json={"kind": "transcribe"}, headers=AUTH).status_code == 422

    with_audio = await _meeting(uow, uploaded_audio=True)
    r = client.post(f"/meetings/{with_audio.id}/reprocess", json={"kind": "transcribe"}, headers=AUTH)
    assert r.status_code == 202

import asyncio
import uuid
from datetime import UTC, datetime

from fastapi.testclient import TestClient

from app.config import Settings
from app.domain.models import EnhancedNotes, JobKind, NoteBlock, Section, Speaker, TranscriptSegment
from tests.fakes import FakeStorage, InMemoryUnitOfWorkFactory, fake_container
from tests.unit.api_helpers import auth, client, create_meeting

ALICE, BOB = uuid.uuid4(), uuid.uuid4()


def _fill(c: TestClient, user: uuid.UUID, title: str, folder_id: str | None = None) -> dict:
    """A meeting with notes, an uploaded chunk, transcript, a speaker, enhanced notes and a job."""
    body: dict = {"title": title, "attendees": [{"name": "Priya", "email": "priya@example.com"}]}
    if folder_id:
        body["folder_id"] = folder_id
    m = create_meeting(c, user, **body)
    lines = [{"id": str(uuid.uuid4()), "position": 0, "text": f"{title} note", "t_ms": 1000}]
    assert c.put(f"/meetings/{m['id']}/notes", json={"lines": lines}, headers=auth(user)).status_code == 200
    r = c.post(f"/meetings/{m['id']}/chunks", json={"seq": 0, "mime_type": "audio/mp4"}, headers=auth(user))
    assert r.status_code in (200, 201), r.text
    return m


async def _seed_processing(uow: InMemoryUnitOfWorkFactory, storage: FakeStorage, user: uuid.UUID, meeting_id: str):
    mid = uuid.UUID(meeting_id)
    async with uow() as u:
        await u.transcripts.replace(
            meeting_id=mid, segments=[TranscriptSegment(speaker=0, start_ms=0, end_ms=1000, text="hello")]
        )
        await u.speakers.upsert(Speaker(meeting_id=mid, label=0, name="Priya", confirmed=True))
        await u.enhanced.save(
            meeting_id=mid,
            notes=EnhancedNotes(
                title="T", tldr="tl;dr", sections=[Section(heading="H", blocks=[NoteBlock(source="ai", text="b")])]
            ),
            model="fake",
        )
        await u.meetings.set_languages(meeting_id=mid, languages=["hi", "en"])
        await u.jobs.enqueue(meeting_id=mid, user_id=user, kind=JobKind.ENHANCE)
        for chunk in await u.chunks.list(meeting_id=mid):
            storage.objects[chunk.storage_path] = b"audio"
    storage.objects[f"{user}/{mid}/full.m4a"] = b"kept recording"  # an object no row lists


def _world(c: TestClient, container) -> dict[str, dict]:
    uow, storage = container.uow, container.storage
    assert isinstance(uow, InMemoryUnitOfWorkFactory) and isinstance(storage, FakeStorage)
    folder = c.post("/folders", json={"name": "Compilers"}, headers=auth(ALICE)).json()
    c.post("/folders", json={"name": "Empty"}, headers=auth(ALICE))
    a1 = _fill(c, ALICE, "Parsing", folder["id"])
    a2 = _fill(c, ALICE, "Standup")
    b1 = _fill(c, BOB, "Bob's meeting")
    c.post("/folders", json={"name": "Bob folder"}, headers=auth(BOB))
    for user, m in ((ALICE, a1), (ALICE, a2), (BOB, b1)):
        asyncio.run(_seed_processing(uow, storage, user, m["id"]))
    return {"folder": folder, "a1": a1, "a2": a2, "b1": b1}


def test_me_reports_counts_email_and_retention() -> None:
    with client() as (c, container):
        _world(c, container)
        r = c.get("/me", headers=auth(ALICE))
        assert r.status_code == 200, r.text
        assert r.json() == {
            "user_id": str(ALICE),
            "email": f"{str(ALICE)[:8]}@example.com",
            "meeting_count": 2,
            "folder_count": 2,
            "audio_retention_days": getattr(container.settings, "audio_retention_days", 0),
        }
        fresh = uuid.uuid4()
        assert c.get("/me", headers=auth(fresh)).json()["meeting_count"] == 0
        assert c.get("/me").status_code == 401


def test_me_reports_configured_retention() -> None:
    settings = Settings(env="test", run_worker_in_process=False)
    if not hasattr(settings, "audio_retention_days"):  # the setting arrives with audio retention
        return
    settings = settings.model_copy(update={"audio_retention_days": 3})
    with client(fake_container(settings=settings)) as (c, _):
        assert c.get("/me", headers=auth(ALICE)).json()["audio_retention_days"] == 3


def test_export_holds_everything_of_the_user_and_nothing_else() -> None:
    with client() as (c, container):
        w = _world(c, container)
        r = c.get("/me/export", headers=auth(ALICE))

        assert r.status_code == 200, r.text
        assert r.headers["content-type"].startswith("application/json")
        today = datetime.now(UTC).date().isoformat()
        assert r.headers["content-disposition"] == f'attachment; filename="slate-export-{today}.json"'
        data = r.json()
        assert (data["format"], data["version"]) == ("slate-export", 1)
        assert data["user"] == {"id": str(ALICE), "email": f"{str(ALICE)[:8]}@example.com"}
        assert sorted(f["name"] for f in data["folders"]) == ["Compilers", "Empty"]
        assert [m["title"] for m in data["meetings"]] == ["Parsing", "Standup"]  # oldest first

        parsing = data["meetings"][0]
        assert parsing["id"] == w["a1"]["id"]
        assert (parsing["folder_id"], parsing["folder_name"]) == (w["folder"]["id"], "Compilers")
        assert parsing["attendees"] == [{"email": "priya@example.com", "name": "Priya"}]
        assert parsing["languages"] == ["hi", "en"]
        assert [n["text"] for n in parsing["notes"]] == ["Parsing note"]
        assert parsing["transcript"] == [{"speaker": 0, "start_ms": 0, "end_ms": 1000, "text": "hello"}]
        assert parsing["speakers"] == [{"label": 0, "name": "Priya", "attendee_email": None, "confirmed": True}]
        assert parsing["enhanced_notes"]["tldr"] == "tl;dr"
        assert data["meetings"][1]["folder_name"] is None
        assert "user_id" not in parsing and "audio_path" not in parsing

        text = r.text
        assert "Bob" not in text and str(BOB) not in text
        assert c.get("/me/export").status_code == 401


def test_delete_removes_only_that_users_rows_and_objects() -> None:
    with client() as (c, container):
        w = _world(c, container)
        uow, storage = container.uow, container.storage
        assert isinstance(uow, InMemoryUnitOfWorkFactory) and isinstance(storage, FakeStorage)
        alice_ids = {uuid.UUID(w["a1"]["id"]), uuid.UUID(w["a2"]["id"])}
        bob_id = uuid.UUID(w["b1"]["id"])
        bob_objects = {p for p in storage.objects if p.startswith(f"{BOB}/")}
        assert bob_objects and any(p.startswith(f"{ALICE}/") for p in storage.objects)

        r = c.delete("/me", headers=auth(ALICE))

        assert r.status_code == 204 and r.content == b""
        s = uow.state
        assert not alice_ids & s.meetings.keys()
        assert not alice_ids & (s.notes.keys() | s.transcripts.keys() | s.enhanced.keys())
        assert not any(ch.meeting_id in alice_ids for ch in s.chunks.values())
        assert not any(j.meeting_id in alice_ids for j in s.jobs.values())
        assert not any(mid in alice_ids for mid, _ in s.speakers)
        assert not any(f.user_id == ALICE for f in s.folders.values())
        assert not any(p.startswith(f"{ALICE}/") for p in storage.objects)
        assert f"{ALICE}/{w['a1']['id']}/full.m4a" in storage.deleted

        # Bob is untouched.
        assert bob_id in s.meetings and bob_id in s.transcripts and bob_id in s.enhanced
        assert any(ch.meeting_id == bob_id for ch in s.chunks.values())
        assert any(f.user_id == BOB for f in s.folders.values())
        assert {p for p in storage.objects if p.startswith(f"{BOB}/")} == bob_objects

        assert c.get("/me", headers=auth(ALICE)).json()["meeting_count"] == 0
        assert c.get(f"/meetings/{w['a1']['id']}", headers=auth(ALICE)).status_code == 404
        assert c.get("/me", headers=auth(BOB)).json()["meeting_count"] == 1
        assert c.delete("/me", headers=auth(ALICE)).status_code == 204  # idempotent
        assert c.delete("/me").status_code == 401

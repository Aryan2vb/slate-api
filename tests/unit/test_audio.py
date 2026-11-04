import uuid
from datetime import UTC, datetime, timedelta
from typing import cast

import pytest

from app.container import Container
from app.domain.models import Attendee, ChunkStatus, Meeting, TemplateKind
from app.jobs.runner import PURGE_AUDIO_EVERY, JobRunner
from app.services.audio import PLAYBACK_URL_TTL_S, AudioService, kept_audio_path
from app.services.transcription import TranscriptionService
from tests.fakes import FakeAudio, FakeStorage, FakeStt, InMemoryState, InMemoryUnitOfWorkFactory, fake_container
from tests.unit.api_helpers import auth, client, create_meeting

ALICE, BOB = uuid.uuid4(), uuid.uuid4()
NOW = datetime(2026, 11, 4, 12, 0, tzinfo=UTC)


async def _meeting_with_chunks(uow: InMemoryUnitOfWorkFactory, storage: FakeStorage) -> Meeting:
    async with uow() as u:
        meeting = await u.meetings.create(
            user_id=ALICE,
            title="Pricing review",
            template=TemplateKind.GENERAL,
            calendar_event_id=None,
            attendees=[Attendee(name="Priya")],
        )
        for seq in (0, 1):
            path = f"{ALICE}/{meeting.id}/{seq:05d}.m4a"
            chunk = await u.chunks.add(
                meeting_id=meeting.id, seq=seq, storage_path=path, mime_type="audio/mp4", byte_size=2
            )
            await u.chunks.mark_uploaded(meeting_id=meeting.id, chunk_id=chunk.id)
            storage.objects[path] = f"c{seq}".encode()
    return meeting


def _transcriber(uow, storage, days: int) -> TranscriptionService:
    return TranscriptionService(uow, storage, FakeAudio(), FakeStt(), audio_retention_days=days, clock=lambda: NOW)


async def _keep_audio(uow: InMemoryUnitOfWorkFactory, *, user_id=ALICE, expires_at=NOW + timedelta(days=7)) -> Meeting:
    async with uow() as u:
        meeting = await u.meetings.create(
            user_id=user_id, title="Kept", template=TemplateKind.GENERAL, calendar_event_id=None, attendees=[]
        )
        await u.meetings.set_audio(
            meeting_id=meeting.id, path=kept_audio_path(user_id, meeting.id), expires_at=expires_at, duration_ms=9000
        )
        got = await u.meetings.get(user_id=user_id, meeting_id=meeting.id)
    assert got is not None
    return got


# --- Keeping the recording after transcription ----------------------------------------------------------------


async def test_retention_keeps_the_assembled_recording_and_deletes_the_chunks() -> None:
    uow, storage = InMemoryUnitOfWorkFactory(), FakeStorage()
    meeting = await _meeting_with_chunks(uow, storage)

    await _transcriber(uow, storage, days=7).transcribe(user_id=ALICE, meeting_id=meeting.id)

    path = f"{ALICE}/{meeting.id}/full.m4a"
    assert storage.objects == {path: b"c0c1"}  # chunks gone, assembled recording kept
    assert storage.content_types[path] == "audio/mp4"
    kept = uow.state.meetings[meeting.id]
    assert kept.audio_path == path
    assert kept.audio_expires_at == NOW + timedelta(days=7)
    assert kept.audio_duration_ms == 9000  # the last segment's end when the provider reports no duration
    assert all(c.status == ChunkStatus.DELETED for c in uow.state.chunks.values())


async def test_retention_zero_deletes_everything_as_before() -> None:
    uow, storage = InMemoryUnitOfWorkFactory(), FakeStorage()
    meeting = await _meeting_with_chunks(uow, storage)

    await _transcriber(uow, storage, days=0).transcribe(user_id=ALICE, meeting_id=meeting.id)

    assert storage.objects == {}
    assert uow.state.meetings[meeting.id].audio_path is None


async def test_a_failed_upload_does_not_fail_transcription() -> None:
    class _NoUpload(FakeStorage):
        async def upload(self, path, data, content_type):
            raise RuntimeError("storage down")

    uow, storage = InMemoryUnitOfWorkFactory(), _NoUpload()
    meeting = await _meeting_with_chunks(uow, storage)

    await _transcriber(uow, storage, days=7).transcribe(user_id=ALICE, meeting_id=meeting.id)

    assert uow.state.transcripts[meeting.id]
    assert uow.state.meetings[meeting.id].audio_path is None
    assert storage.objects == {}  # the chunks are still deleted


async def test_meeting_deleted_while_transcribing_leaves_no_kept_object() -> None:
    class _DeleteMeetingOnUpload(FakeStorage):
        async def upload(self, path, data, content_type):
            await super().upload(path, data, content_type)
            del uow.state.meetings[meeting.id]

    uow = InMemoryUnitOfWorkFactory()
    storage = _DeleteMeetingOnUpload()
    meeting = await _meeting_with_chunks(uow, storage)

    await _transcriber(uow, storage, days=7).transcribe(user_id=ALICE, meeting_id=meeting.id)

    assert f"{ALICE}/{meeting.id}/full.m4a" not in storage.objects


# --- Playback and deletion over HTTP --------------------------------------------------------------------------


def _state(container: Container) -> InMemoryState:
    return cast(InMemoryUnitOfWorkFactory, container.uow).state


def _with_kept_audio(container: Container, meeting_id: str, *, expires_at: datetime) -> str:
    state = _state(container)
    mid = uuid.UUID(meeting_id)
    path = kept_audio_path(ALICE, mid)
    m = state.meetings[mid]
    state.meetings[mid] = m.model_copy(
        update={"audio_path": path, "audio_expires_at": expires_at, "audio_duration_ms": 61_000}
    )
    cast(FakeStorage, container.storage).objects[path] = b"audio"
    return path


def test_detail_reports_audio_availability() -> None:
    with client() as (c, container):
        m = create_meeting(c, ALICE)
        assert c.get(f"/meetings/{m['id']}", headers=auth(ALICE)).json()["audio"] == {
            "available": False,
            "expires_at": None,
        }
        expires = datetime.now(UTC) + timedelta(days=3)
        _with_kept_audio(container, m["id"], expires_at=expires)
        audio = c.get(f"/meetings/{m['id']}", headers=auth(ALICE)).json()["audio"]
        assert audio["available"] is True
        assert datetime.fromisoformat(audio["expires_at"]) == expires


def test_get_audio_returns_a_signed_url() -> None:
    with client() as (c, container):
        m = create_meeting(c, ALICE)
        expires = datetime.now(UTC) + timedelta(days=3)
        path = _with_kept_audio(container, m["id"], expires_at=expires)

        r = c.get(f"/meetings/{m['id']}/audio", headers=auth(ALICE))

        assert r.status_code == 200, r.text
        body = r.json()
        assert body["url"] == f"https://storage.test/download/{path}?ttl={PLAYBACK_URL_TTL_S}"
        assert body["duration_ms"] == 61_000
        assert datetime.fromisoformat(body["expires_at"]) == expires
        url_expires = datetime.fromisoformat(body["url_expires_at"])
        assert timedelta(minutes=59) < url_expires - datetime.now(UTC) <= timedelta(hours=1)


def test_get_audio_is_404_when_none_expired_or_someone_elses() -> None:
    with client() as (c, container):
        m = create_meeting(c, ALICE)
        assert c.get(f"/meetings/{m['id']}/audio", headers=auth(ALICE)).status_code == 404
        _with_kept_audio(container, m["id"], expires_at=datetime.now(UTC) - timedelta(minutes=1))
        assert c.get(f"/meetings/{m['id']}/audio", headers=auth(ALICE)).status_code == 404
        assert c.get(f"/meetings/{m['id']}", headers=auth(ALICE)).json()["audio"]["available"] is False
        _with_kept_audio(container, m["id"], expires_at=datetime.now(UTC) + timedelta(days=1))
        assert c.get(f"/meetings/{m['id']}/audio", headers=auth(BOB)).status_code == 404
        assert c.delete(f"/meetings/{m['id']}/audio", headers=auth(BOB)).status_code == 404
        assert c.get(f"/meetings/{m['id']}/audio").status_code == 401


def test_delete_audio_removes_the_object_now() -> None:
    with client() as (c, container):
        m = create_meeting(c, ALICE)
        path = _with_kept_audio(container, m["id"], expires_at=datetime.now(UTC) + timedelta(days=3))

        assert c.delete(f"/meetings/{m['id']}/audio", headers=auth(ALICE)).status_code == 204

        assert path not in cast(FakeStorage, container.storage).objects
        assert _state(container).meetings[uuid.UUID(m["id"])].audio_path is None
        assert c.get(f"/meetings/{m['id']}/audio", headers=auth(ALICE)).status_code == 404
        assert c.delete(f"/meetings/{m['id']}/audio", headers=auth(ALICE)).status_code == 204  # idempotent


def test_deleting_the_meeting_deletes_its_kept_audio() -> None:
    with client() as (c, container):
        m = create_meeting(c, ALICE)
        path = _with_kept_audio(container, m["id"], expires_at=datetime.now(UTC) + timedelta(days=3))

        assert c.delete(f"/meetings/{m['id']}", headers=auth(ALICE)).status_code == 204

        assert path in cast(FakeStorage, container.storage).deleted
        assert path not in cast(FakeStorage, container.storage).objects


# --- Purge ----------------------------------------------------------------------------------------------------


async def test_purge_removes_only_expired_recordings_and_is_idempotent() -> None:
    uow, storage = InMemoryUnitOfWorkFactory(), FakeStorage()
    expired = await _keep_audio(uow, expires_at=NOW - timedelta(seconds=1))
    on_the_dot = await _keep_audio(uow, user_id=BOB, expires_at=NOW)
    fresh = await _keep_audio(uow, expires_at=NOW + timedelta(hours=1))
    for m in (expired, on_the_dot, fresh):
        assert m.audio_path
        storage.objects[m.audio_path] = b"audio"
    service = AudioService(uow, storage, clock=lambda: NOW)

    assert await service.purge_expired() == 2

    assert set(storage.objects) == {fresh.audio_path}
    assert uow.state.meetings[expired.id].audio_path is None
    assert uow.state.meetings[on_the_dot.id].audio_expires_at is None
    assert uow.state.meetings[fresh.id].audio_path == fresh.audio_path
    assert await service.purge_expired() == 0


async def test_purge_leaves_columns_when_storage_fails_so_the_next_run_retries() -> None:
    class _Down(FakeStorage):
        async def delete(self, paths):
            raise RuntimeError("storage down")

    uow = InMemoryUnitOfWorkFactory()
    expired = await _keep_audio(uow, expires_at=NOW - timedelta(days=1))
    service = AudioService(uow, _Down(), clock=lambda: NOW)

    with pytest.raises(RuntimeError):
        await service.purge_expired()
    assert uow.state.meetings[expired.id].audio_path is not None


async def test_worker_runs_the_purge_about_hourly_and_survives_failures() -> None:
    calls: list[datetime] = []
    clock = [NOW]

    async def purge() -> int:
        calls.append(clock[0])
        if len(calls) == 1:
            raise RuntimeError("storage down")
        return 0

    runner = JobRunner(
        InMemoryUnitOfWorkFactory(),
        {},
        poll_interval_s=0.01,
        stale_after_s=900,
        clock=lambda: clock[0],
        purge_audio=purge,
    )
    await runner.run_maintenance()  # first loop: runs (and fails, which is only logged)
    await runner.run_maintenance()  # not due again yet
    clock[0] = NOW + PURGE_AUDIO_EVERY - timedelta(seconds=1)
    await runner.run_maintenance()
    clock[0] = NOW + PURGE_AUDIO_EVERY
    await runner.run_maintenance()

    assert calls == [NOW, NOW + PURGE_AUDIO_EVERY]


def test_runner_from_container_wires_the_purge() -> None:
    runner = JobRunner.from_container(fake_container())
    assert runner._purge_audio is not None

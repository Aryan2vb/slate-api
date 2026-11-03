import uuid
from datetime import datetime

import pytest

from app.domain.errors import InvalidInputError, NotFoundError, ProviderError
from app.domain.models import Attendee, ChunkStatus, Meeting, MeetingStatus, NoteLine, TemplateKind
from app.services.transcription import MAX_KEYTERMS, TranscriptionService, build_keyterms
from tests.fakes import FakeAudio, FakeStorage, FakeStt, InMemoryUnitOfWorkFactory

USER = uuid.uuid4()


class _OrderRecordingStorage(FakeStorage):
    """Fails deletes on request and records whether the transcript existed when delete was called."""

    def __init__(self, uow: InMemoryUnitOfWorkFactory, fail_delete: bool = False) -> None:
        super().__init__()
        self.uow = uow
        self.fail_delete = fail_delete
        self.transcript_saved_before_delete: bool | None = None

    async def delete(self, paths):
        self.transcript_saved_before_delete = any(self.uow.state.transcripts.values())
        if self.fail_delete:
            raise RuntimeError("storage down")
        await super().delete(paths)


class _BrokenStt(FakeStt):
    async def transcribe(self, audio, mime_type, options):
        raise ProviderError("stt", "timeout", retryable=True)


async def _setup(*, recording: bool = False, fail_delete: bool = False, stt=None, uploaded=(0, 1), pending=(2,)):
    uow = InMemoryUnitOfWorkFactory()
    storage = _OrderRecordingStorage(uow, fail_delete) if recording else FakeStorage()
    async with uow() as u:
        meeting = await u.meetings.create(
            user_id=USER,
            title="Acme onboarding",
            template=TemplateKind.GENERAL,
            calendar_event_id=None,
            attendees=[Attendee(name="Priya Shah")],
        )
        for seq in (*uploaded, *pending):
            path = f"{seq}.m4a"
            chunk = await u.chunks.add(
                meeting_id=meeting.id, seq=seq, storage_path=path, mime_type="audio/mp4", byte_size=1
            )
            storage.objects[path] = bytes([65 + seq])
            if seq in uploaded:
                await u.chunks.mark_uploaded(meeting_id=meeting.id, chunk_id=chunk.id)
    stt = stt or FakeStt()
    return TranscriptionService(uow, storage, FakeAudio(), stt), uow, storage, stt, meeting


async def test_transcribes_uploaded_chunks_in_order_and_deletes_audio_after_saving() -> None:
    service, uow, storage, stt, meeting = await _setup(recording=True)

    transcript = await service.transcribe(user_id=USER, meeting_id=meeting.id)

    assert uow.state.transcripts[meeting.id] == transcript.segments
    assert isinstance(storage, _OrderRecordingStorage)
    assert storage.transcript_saved_before_delete is True
    assert storage.deleted == ["0.m4a", "1.m4a"]  # the never-uploaded chunk is not touched
    statuses = {c.seq: c.status for c in uow.state.chunks.values()}
    assert statuses[0] == statuses[1] == ChunkStatus.DELETED
    assert stt.calls[0].diarize is True
    assert stt.calls[0].keyterms[0] == "Priya Shah"
    assert uow.state.meetings[meeting.id].status == MeetingStatus.PROCESSING


async def test_failed_delete_keeps_transcript_and_does_not_raise() -> None:
    service, uow, _, _, meeting = await _setup(recording=True, fail_delete=True)

    await service.transcribe(user_id=USER, meeting_id=meeting.id)

    assert uow.state.transcripts[meeting.id]
    assert all(c.status != ChunkStatus.DELETED for c in uow.state.chunks.values())


async def test_provider_failure_keeps_audio() -> None:
    service, uow, storage, _, meeting = await _setup(stt=_BrokenStt())

    with pytest.raises(ProviderError):
        await service.transcribe(user_id=USER, meeting_id=meeting.id)

    assert storage.deleted == []
    assert meeting.id not in uow.state.transcripts


async def test_no_uploaded_audio_is_invalid() -> None:
    service, *_, meeting = await _setup(uploaded=(), pending=(0,))
    with pytest.raises(InvalidInputError):
        await service.transcribe(user_id=USER, meeting_id=meeting.id)


async def test_other_users_meeting_is_not_found() -> None:
    service, *_, meeting = await _setup()
    with pytest.raises(NotFoundError):
        await service.transcribe(user_id=uuid.uuid4(), meeting_id=meeting.id)


def test_keyterms_put_attendees_first_dedupe_and_skip_stopwords() -> None:
    now = datetime.now()
    meeting = Meeting(
        id=uuid.uuid4(),
        user_id=USER,
        title="Weekly sync with Kubernetes team about Priya",
        created_at=now,
        updated_at=now,
        attendees=[Attendee(name="Priya"), Attendee(email="no-name@example.com")],
    )
    assert build_keyterms(meeting) == ["Priya", "Kubernetes", "team"]


def test_keyterms_include_jargon_from_the_users_notes() -> None:
    now = datetime.now()
    meeting = Meeting(id=uuid.uuid4(), user_id=USER, title="Compiler design", created_at=now, updated_at=now)

    def line(position: int, text: str) -> NoteLine:
        return NoteLine(id=uuid.uuid4(), meeting_id=meeting.id, position=position, text=text, updated_at=now)

    notes = [line(1, "LR(1) vs LALR parsers, see PT-01"), line(0, "eVTOL is the category. compute FIRST/FOLLOW sets")]

    assert build_keyterms(meeting, notes) == [
        "eVTOL",
        "FIRST/FOLLOW",
        "LR(1)",
        "LALR",
        "PT-01",
        "Compiler",
        "design",
    ]


def test_keyterms_put_folder_vocabulary_after_attendees_and_before_note_jargon() -> None:
    now = datetime.now()
    meeting = Meeting(
        id=uuid.uuid4(),
        user_id=USER,
        title="Compiler design",
        created_at=now,
        updated_at=now,
        attendees=[Attendee(name="Dr Rao")],
    )
    notes = [NoteLine(id=uuid.uuid4(), meeting_id=meeting.id, position=0, text="LALR and lr(1)", updated_at=now)]

    assert build_keyterms(meeting, notes, ["LR(1)", "Shift-reduce"]) == [
        "Dr Rao",
        "LR(1)",
        "Shift-reduce",
        "LALR",  # "lr(1)" from the notes is a duplicate of the vocabulary term
        "Compiler",
        "design",
    ]


def test_keyterms_stay_capped_with_a_full_vocabulary() -> None:
    now = datetime.now()
    meeting = Meeting(id=uuid.uuid4(), user_id=USER, title="Compilers", created_at=now, updated_at=now)
    terms = build_keyterms(meeting, (), [f"term{i}" for i in range(100)])
    assert len(terms) == MAX_KEYTERMS and terms[0] == "term0"


async def test_transcribe_boosts_the_meetings_folder_vocabulary() -> None:
    service, uow, _, stt, meeting = await _setup()
    async with uow() as u:
        folder = await u.folders.create(user_id=USER, name="Compilers", icon=None)
        await u.folders.update(user_id=USER, folder_id=folder.id, name="Compilers", icon=None, vocabulary=["LR(1)"])
        await u.meetings.set_folder(user_id=USER, meeting_id=meeting.id, folder_id=folder.id)

    await service.transcribe(user_id=USER, meeting_id=meeting.id)

    assert stt.calls[0].keyterms[:2] == ["Priya Shah", "LR(1)"]

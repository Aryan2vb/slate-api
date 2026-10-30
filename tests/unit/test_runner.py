import uuid
from datetime import UTC, datetime, timedelta
from typing import cast

from app.container import Container
from app.domain.errors import InvalidInputError, ProviderError
from app.domain.models import (
    Attendee,
    ChunkStatus,
    EnhancedNotes,
    Evidence,
    Job,
    JobKind,
    JobStatus,
    MeetingStatus,
    NoteBlock,
    Section,
    SpeakerSuggestion,
    TemplateKind,
)
from app.jobs.handlers import build_handlers
from app.jobs.runner import JobRunner, backoff
from app.services.speakers import SpeakerSuggestions
from tests.fakes import FakeLlm, FakeStorage, FakeStt, InMemoryState, InMemoryUnitOfWorkFactory, fake_container

NOW = datetime.now(UTC) + timedelta(seconds=5)  # jobs enqueued by the fakes run_after real time
USER = uuid.uuid4()


async def _seed(container, *, chunks: int = 2):
    async with container.uow() as uow:
        meeting = await uow.meetings.create(
            user_id=USER,
            title="Pricing review",
            template=TemplateKind.GENERAL,
            calendar_event_id=None,
            attendees=[Attendee(name="Priya Shah", email="priya@example.com")],
        )
        for seq in range(chunks):
            path = f"{USER}/{meeting.id}/{seq}.m4a"
            chunk = await uow.chunks.add(
                meeting_id=meeting.id, seq=seq, storage_path=path, mime_type="audio/mp4", byte_size=3
            )
            await uow.chunks.mark_uploaded(meeting_id=meeting.id, chunk_id=chunk.id)
            container.storage.objects[path] = b"abc"
        await uow.jobs.enqueue(meeting_id=meeting.id, user_id=USER, kind=JobKind.TRANSCRIBE)
        await uow.commit()
    return meeting


def _state(container: Container) -> InMemoryState:
    return cast(InMemoryUnitOfWorkFactory, container.uow).state


def _runner(container, clock=lambda: NOW, handlers=None) -> JobRunner:
    return JobRunner(
        container.uow, handlers or build_handlers(container), poll_interval_s=0.01, stale_after_s=900, clock=clock
    )


def _notes() -> EnhancedNotes:
    return EnhancedNotes(
        title="Pricing review",
        tldr="Q3 prices rise five percent.",
        sections=[
            Section(
                heading="Pricing",
                blocks=[
                    NoteBlock(
                        source="ai", text="Q3 pricing goes up 5%.", evidence=[Evidence(start_ms=4000, end_ms=9000)]
                    )
                ],
            )
        ],
    )


async def test_full_chain_drives_meeting_to_ready() -> None:
    llm = FakeLlm(
        [
            SpeakerSuggestions(
                suggestions=[
                    SpeakerSuggestion(
                        label=0, name="Priya Shah", attendee_email="priya@example.com", confidence=0.95, evidence_ms=[0]
                    )
                ]
            ),
            _notes(),
        ]
    )
    container = fake_container(llm=llm)
    meeting = await _seed(container)
    runner = _runner(container)

    assert [await runner.run_once() for _ in range(4)] == [True, True, True, False]

    state = _state(container)
    assert state.meetings[meeting.id].status == MeetingStatus.READY
    assert {j.kind: j.status for j in state.jobs.values()} == {k: JobStatus.SUCCEEDED for k in JobKind}
    assert state.enhanced[meeting.id] == (_notes(), "fake-llm")
    assert state.speakers[(meeting.id, 0)].name == "Priya Shah"
    assert len(state.transcripts[meeting.id]) == 2
    assert all(c.status == ChunkStatus.DELETED for c in state.chunks.values())
    assert sorted(cast(FakeStorage, container.storage).deleted) == sorted(c.storage_path for c in state.chunks.values())
    assert "Priya Shah" in cast(FakeStt, container.stt).calls[0].keyterms
    # The enhancement prompt uses the suggested name rather than "Speaker 0".
    assert "Priya Shah: Hi, I'm Priya" in llm.prompts[1][1]


class _FailingHandler:
    def __init__(self, exc: Exception) -> None:
        self.exc = exc

    async def handle(self, job: Job) -> None:
        raise self.exc


async def test_retryable_error_requeues_with_backoff() -> None:
    container = fake_container()
    meeting = await _seed(container)
    runner = _runner(
        container, handlers={JobKind.TRANSCRIBE: _FailingHandler(ProviderError("stt", "429", retryable=True))}
    )

    assert await runner.run_once()

    job = next(iter(_state(container).jobs.values()))
    assert job.status == JobStatus.QUEUED
    assert job.run_after == NOW + timedelta(seconds=30)
    assert job.last_error and "429" in job.last_error
    assert _state(container).meetings[meeting.id].status != MeetingStatus.FAILED
    assert not await runner.run_once()  # not runnable until the backoff elapses


async def test_retryable_error_fails_permanently_after_max_attempts() -> None:
    container = fake_container()
    meeting = await _seed(container)
    clock = {"now": NOW}
    runner = _runner(
        container,
        clock=lambda: clock["now"],
        handlers={JobKind.TRANSCRIBE: _FailingHandler(ProviderError("stt", "503", retryable=True))},
    )

    for _ in range(5):
        assert await runner.run_once()
        clock["now"] += timedelta(hours=1)

    job = next(iter(_state(container).jobs.values()))
    assert (job.status, job.attempts) == (JobStatus.FAILED, 5)
    assert _state(container).meetings[meeting.id].status == MeetingStatus.FAILED


async def test_non_retryable_error_fails_meeting_immediately() -> None:
    container = fake_container()
    meeting = await _seed(container, chunks=0)  # no audio -> InvalidInputError from the real transcribe handler
    runner = _runner(container)

    assert await runner.run_once()

    job = next(iter(_state(container).jobs.values()))
    assert (job.status, job.attempts) == (JobStatus.FAILED, 1)
    assert "InvalidInputError" in (job.last_error or "")
    assert _state(container).meetings[meeting.id].status == MeetingStatus.FAILED


async def test_speaker_failure_does_not_block_enhancement() -> None:
    # The speakers call receives the wrong schema and raises; enhancement gets the second response.
    container = fake_container(llm=FakeLlm([_notes(), _notes()]))
    meeting = await _seed(container)
    runner = _runner(container)

    for _ in range(3):
        await runner.run_once()

    assert _state(container).meetings[meeting.id].status == MeetingStatus.READY


async def test_stale_running_job_is_reclaimed() -> None:
    uow = InMemoryUnitOfWorkFactory()
    container = fake_container(uow=uow, storage=FakeStorage())
    await _seed(container)
    async with uow() as u:
        await u.jobs.claim_next(now=NOW)  # a worker that died mid-job: locked, never finished

    later = NOW + timedelta(hours=1)
    runner = _runner(
        container, clock=lambda: later, handlers={JobKind.TRANSCRIBE: _FailingHandler(InvalidInputError("x"))}
    )
    assert await runner.run_once()
    assert next(iter(uow.state.jobs.values())).attempts == 2


def test_backoff_doubles_and_caps() -> None:
    assert [backoff(n).total_seconds() for n in (1, 2, 3, 6, 10)] == [30, 60, 120, 900, 900]

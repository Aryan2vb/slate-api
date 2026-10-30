"""In-memory fakes for every port. Each fake honours its port's contract (ownership scoping, idempotency), so
service tests exercise real behaviour without Postgres or vendors (Liskov: fakes are drop-in substitutes)."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Self
from uuid import UUID

from pydantic import BaseModel

from app.adapters.ratelimit.memory import InMemoryRateLimiter
from app.config import Settings
from app.container import Container
from app.domain.errors import AuthenticationError, NotFoundError
from app.domain.models import (
    Attendee,
    AudioChunk,
    AuthUser,
    ChunkStatus,
    EnhancedNotes,
    Job,
    JobKind,
    JobStatus,
    Meeting,
    MeetingStatus,
    NoteLine,
    Speaker,
    TemplateKind,
    Transcript,
    TranscriptSegment,
)
from app.ports.providers import SignedUpload, TranscriptionOptions
from app.ports.repositories import NoteLineInput


def _now() -> datetime:
    return datetime.now(UTC)


@dataclass
class InMemoryState:
    meetings: dict[UUID, Meeting] = field(default_factory=dict)
    notes: dict[UUID, list[NoteLine]] = field(default_factory=dict)
    chunks: dict[UUID, AudioChunk] = field(default_factory=dict)
    transcripts: dict[UUID, list[TranscriptSegment]] = field(default_factory=dict)
    speakers: dict[tuple[UUID, int], Speaker] = field(default_factory=dict)
    enhanced: dict[UUID, tuple[EnhancedNotes, str]] = field(default_factory=dict)
    jobs: dict[UUID, Job] = field(default_factory=dict)


class _Meetings:
    def __init__(self, s: InMemoryState) -> None:
        self.s = s

    async def create(
        self,
        *,
        user_id: UUID,
        title: str,
        template: TemplateKind,
        calendar_event_id: str | None,
        attendees: Sequence[Attendee],
    ) -> Meeting:
        now = _now()
        m = Meeting(
            id=uuid.uuid4(),
            user_id=user_id,
            title=title,
            template=template,
            calendar_event_id=calendar_event_id,
            attendees=list(attendees),
            created_at=now,
            updated_at=now,
        )
        self.s.meetings[m.id] = m
        return m

    async def get(self, *, user_id: UUID, meeting_id: UUID) -> Meeting | None:
        m = self.s.meetings.get(meeting_id)
        return m if m and m.user_id == user_id else None

    async def get_by_calendar_event(self, *, user_id: UUID, calendar_event_id: str) -> Meeting | None:
        return next(
            (m for m in self.s.meetings.values() if m.user_id == user_id and m.calendar_event_id == calendar_event_id),
            None,
        )

    async def list(self, *, user_id: UUID, limit: int, before: datetime | None) -> list[Meeting]:
        rows = [
            m for m in self.s.meetings.values() if m.user_id == user_id and (before is None or m.created_at < before)
        ]
        return sorted(rows, key=lambda m: m.created_at, reverse=True)[:limit]

    async def set_status(self, *, meeting_id: UUID, status: MeetingStatus) -> None:
        m = self.s.meetings[meeting_id]
        self.s.meetings[meeting_id] = m.model_copy(update={"status": status, "updated_at": _now()})

    async def set_languages(self, *, meeting_id: UUID, languages: Sequence[str]) -> None:
        m = self.s.meetings[meeting_id]
        self.s.meetings[meeting_id] = m.model_copy(update={"languages": list(languages), "updated_at": _now()})

    async def delete(self, *, user_id: UUID, meeting_id: UUID) -> bool:
        if await self.get(user_id=user_id, meeting_id=meeting_id) is None:
            return False
        del self.s.meetings[meeting_id]
        return True


class _Notes:
    def __init__(self, s: InMemoryState) -> None:
        self.s = s

    async def list(self, *, meeting_id: UUID) -> list[NoteLine]:
        return sorted(self.s.notes.get(meeting_id, []), key=lambda n: n.position)

    async def replace_all(self, *, meeting_id: UUID, lines: Sequence[NoteLineInput]) -> list[NoteLine]:
        now = _now()
        self.s.notes[meeting_id] = [
            NoteLine(id=x.id, meeting_id=meeting_id, position=x.position, text=x.text, t_ms=x.t_ms, updated_at=now)
            for x in lines
        ]
        return await self.list(meeting_id=meeting_id)


class _Chunks:
    def __init__(self, s: InMemoryState) -> None:
        self.s = s

    async def add(
        self, *, meeting_id: UUID, seq: int, storage_path: str, mime_type: str, byte_size: int | None
    ) -> AudioChunk:
        c = AudioChunk(
            id=uuid.uuid4(),
            meeting_id=meeting_id,
            seq=seq,
            storage_path=storage_path,
            mime_type=mime_type,
            byte_size=byte_size,
            created_at=_now(),
        )
        self.s.chunks[c.id] = c
        return c

    async def mark_uploaded(self, *, meeting_id: UUID, chunk_id: UUID) -> AudioChunk | None:
        c = self.s.chunks.get(chunk_id)
        if c is None or c.meeting_id != meeting_id:
            return None
        c = c.model_copy(update={"status": ChunkStatus.UPLOADED})
        self.s.chunks[chunk_id] = c
        return c

    async def list(self, *, meeting_id: UUID) -> list[AudioChunk]:
        return sorted((c for c in self.s.chunks.values() if c.meeting_id == meeting_id), key=lambda c: c.seq)

    async def mark_deleted(self, *, meeting_id: UUID) -> None:
        for c in await self.list(meeting_id=meeting_id):
            self.s.chunks[c.id] = c.model_copy(update={"status": ChunkStatus.DELETED})


class _Transcripts:
    def __init__(self, s: InMemoryState) -> None:
        self.s = s

    async def replace(self, *, meeting_id: UUID, segments: Sequence[TranscriptSegment]) -> None:
        self.s.transcripts[meeting_id] = list(segments)

    async def list(self, *, meeting_id: UUID) -> list[TranscriptSegment]:
        return sorted(self.s.transcripts.get(meeting_id, []), key=lambda t: t.start_ms)


class _Speakers:
    def __init__(self, s: InMemoryState) -> None:
        self.s = s

    async def list(self, *, meeting_id: UUID) -> list[Speaker]:
        return sorted((v for (m, _), v in self.s.speakers.items() if m == meeting_id), key=lambda x: x.label)

    async def upsert(self, speaker: Speaker) -> Speaker:
        self.s.speakers[(speaker.meeting_id, speaker.label)] = speaker
        return speaker


class _Enhanced:
    def __init__(self, s: InMemoryState) -> None:
        self.s = s

    async def save(self, *, meeting_id: UUID, notes: EnhancedNotes, model: str) -> None:
        self.s.enhanced[meeting_id] = (notes, model)

    async def get(self, *, meeting_id: UUID) -> EnhancedNotes | None:
        hit = self.s.enhanced.get(meeting_id)
        return hit[0] if hit else None


class _Jobs:
    def __init__(self, s: InMemoryState) -> None:
        self.s = s

    async def enqueue(self, *, meeting_id: UUID, user_id: UUID, kind: JobKind, max_attempts: int = 5) -> Job:
        now = _now()
        existing = next((j for j in self.s.jobs.values() if j.meeting_id == meeting_id and j.kind == kind), None)
        if existing and existing.status in (JobStatus.QUEUED, JobStatus.RUNNING):
            return existing
        job = Job(
            id=existing.id if existing else uuid.uuid4(),
            meeting_id=meeting_id,
            user_id=user_id,
            kind=kind,
            status=JobStatus.QUEUED,
            attempts=0,
            max_attempts=max_attempts,
            run_after=now,
            created_at=existing.created_at if existing else now,
            updated_at=now,
        )
        self.s.jobs[job.id] = job
        return job

    async def claim_next(self, *, now: datetime) -> Job | None:
        runnable = [j for j in self.s.jobs.values() if j.status == JobStatus.QUEUED and j.run_after <= now]
        if not runnable:
            return None
        job = min(runnable, key=lambda j: j.run_after)
        job = job.model_copy(update={"status": JobStatus.RUNNING, "attempts": job.attempts + 1, "locked_at": now})
        self.s.jobs[job.id] = job
        return job

    async def mark_succeeded(self, *, job_id: UUID) -> None:
        j = self.s.jobs[job_id]
        self.s.jobs[job_id] = j.model_copy(
            update={"status": JobStatus.SUCCEEDED, "locked_at": None, "last_error": None}
        )

    async def mark_failed(self, *, job_id: UUID, error: str, retry_at: datetime | None) -> None:
        j = self.s.jobs[job_id]
        upd: dict = {"last_error": error, "locked_at": None}
        upd |= {"status": JobStatus.QUEUED, "run_after": retry_at} if retry_at else {"status": JobStatus.FAILED}
        self.s.jobs[job_id] = j.model_copy(update=upd)

    async def reclaim_stale(self, *, locked_before: datetime) -> int:
        n = 0
        for j in list(self.s.jobs.values()):
            if j.status == JobStatus.RUNNING and j.locked_at and j.locked_at < locked_before:
                self.s.jobs[j.id] = j.model_copy(update={"status": JobStatus.QUEUED, "locked_at": None})
                n += 1
        return n

    async def list_for_meeting(self, *, meeting_id: UUID) -> list[Job]:
        return [j for j in self.s.jobs.values() if j.meeting_id == meeting_id]


class InMemoryUnitOfWork:
    """Shares state across units of work. Commit is a no-op: tests assert on state, not transactions."""

    def __init__(self, state: InMemoryState) -> None:
        self.meetings = _Meetings(state)
        self.notes = _Notes(state)
        self.chunks = _Chunks(state)
        self.transcripts = _Transcripts(state)
        self.speakers = _Speakers(state)
        self.enhanced = _Enhanced(state)
        self.jobs = _Jobs(state)
        self.committed = False

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def commit(self) -> None:
        self.committed = True


class InMemoryUnitOfWorkFactory:
    def __init__(self, state: InMemoryState | None = None) -> None:
        self.state = state or InMemoryState()

    def __call__(self) -> InMemoryUnitOfWork:
        return InMemoryUnitOfWork(self.state)


# --- Provider fakes -------------------------------------------------------------------------------------------


class FakeAuth:
    """Accepts tokens of the form 'user:<uuid>'."""

    async def verify(self, token: str) -> AuthUser:
        prefix, _, raw = token.partition(":")
        if prefix != "user":
            raise AuthenticationError("Invalid token")
        try:
            return AuthUser(id=UUID(raw), email=f"{raw[:8]}@example.com")
        except ValueError as e:
            raise AuthenticationError("Invalid token") from e


class FakeStorage:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.deleted: list[str] = []

    async def create_upload_url(self, path: str, *, overwrite: bool = False) -> SignedUpload:
        return SignedUpload(url=f"https://storage.test/upload/{path}", path=path, expires_in_s=900)

    async def download(self, path: str) -> bytes:
        if path not in self.objects:
            raise NotFoundError(path)
        return self.objects[path]

    async def delete(self, paths: Sequence[str]) -> None:
        for p in paths:
            self.objects.pop(p, None)
            self.deleted.append(p)


class FakeAudio:
    async def concat(self, chunks: Sequence[bytes], mime_type: str) -> tuple[bytes, str]:
        return b"".join(chunks), mime_type


class FakeStt:
    name = "fake-stt"

    def __init__(self, transcript: Transcript | None = None) -> None:
        self.transcript = transcript or Transcript(
            provider=self.name,
            segments=[
                TranscriptSegment(speaker=0, start_ms=0, end_ms=4000, text="Hi, I'm Priya. Let's review pricing."),
                TranscriptSegment(speaker=1, start_ms=4000, end_ms=9000, text="Sure, Q3 pricing goes up five percent."),
            ],
        )
        self.calls: list[TranscriptionOptions] = []

    async def transcribe(self, audio: bytes, mime_type: str, options: TranscriptionOptions) -> Transcript:
        self.calls.append(options)
        return self.transcript


class FakeLlm:
    """Returns queued responses in order; each must be an instance of the requested schema."""

    name = "fake-llm"

    def __init__(self, responses: list[BaseModel] | None = None) -> None:
        self.responses = list(responses or [])
        self.prompts: list[tuple[str, str]] = []

    async def generate[T: BaseModel](self, *, system: str, prompt: str, schema: type[T], max_tokens: int = 8000) -> T:
        self.prompts.append((system, prompt))
        response = self.responses.pop(0)
        assert isinstance(response, schema), f"FakeLlm queued {type(response).__name__}, caller wants {schema.__name__}"
        return response


def fake_container(**overrides: object) -> Container:
    parts: dict[str, object] = {
        "settings": Settings(env="test", run_worker_in_process=False),
        "uow": InMemoryUnitOfWorkFactory(),
        "auth": FakeAuth(),
        "storage": FakeStorage(),
        "stt": FakeStt(),
        "llm": FakeLlm(),
        "audio": FakeAudio(),
        "rate_limiter": InMemoryRateLimiter(),
    } | overrides
    return Container(**parts)  # type: ignore[arg-type]

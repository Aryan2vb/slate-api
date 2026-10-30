import uuid
from datetime import UTC, datetime

from app.domain.models import Job, JobKind, JobStatus, Meeting, MeetingStatus
from app.services.meetings import Stage, derive_stage


def _meeting(status: MeetingStatus) -> Meeting:
    now = datetime.now(UTC)
    return Meeting(id=uuid.uuid4(), user_id=uuid.uuid4(), title="t", status=status, created_at=now, updated_at=now)


def _job(kind: JobKind, status: JobStatus) -> Job:
    now = datetime.now(UTC)
    return Job(
        id=uuid.uuid4(),
        meeting_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        kind=kind,
        status=status,
        attempts=0,
        max_attempts=5,
        run_after=now,
        created_at=now,
        updated_at=now,
    )


def test_stage_follows_active_job() -> None:
    m = _meeting(MeetingStatus.PROCESSING)
    jobs = [_job(JobKind.TRANSCRIBE, JobStatus.SUCCEEDED), _job(JobKind.ENHANCE, JobStatus.RUNNING)]
    assert derive_stage(m, jobs) == Stage.ENHANCING


def test_failed_job_wins() -> None:
    m = _meeting(MeetingStatus.PROCESSING)
    assert derive_stage(m, [_job(JobKind.TRANSCRIBE, JobStatus.FAILED)]) == Stage.FAILED


def test_stage_from_status_when_idle() -> None:
    assert derive_stage(_meeting(MeetingStatus.READY), []) == Stage.READY
    assert derive_stage(_meeting(MeetingStatus.RECORDING), []) == Stage.RECORDING


async def test_create_is_idempotent_per_calendar_event() -> None:
    import uuid

    from app.domain.models import TemplateKind
    from app.services.meetings import MeetingService
    from tests.fakes import FakeStorage, InMemoryUnitOfWorkFactory

    service = MeetingService(InMemoryUnitOfWorkFactory(), FakeStorage())
    user = uuid.uuid4()
    args = {"user_id": user, "title": "Sync", "template": TemplateKind.GENERAL, "attendees": []}
    first = await service.create(calendar_event_id="evt-1", **args)
    again = await service.create(calendar_event_id="evt-1", **args)
    other = await service.create(calendar_event_id=None, **args)
    assert again.id == first.id
    assert other.id != first.id

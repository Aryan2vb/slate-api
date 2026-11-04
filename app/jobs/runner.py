"""Polls the jobs table and dispatches each job to its handler, with retry/backoff and stale-lock recovery.

Between polls it also runs periodic housekeeping: purging kept recordings whose retention expired."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, datetime, timedelta

import structlog

from app.container import Container
from app.domain.errors import DomainError, ProviderError
from app.domain.models import Job, JobKind, MeetingStatus
from app.jobs.handlers import JobHandler, build_handlers
from app.ports.repositories import UnitOfWorkFactory
from app.services.audio import AudioService

log = structlog.get_logger()

BASE_BACKOFF = timedelta(seconds=30)
MAX_BACKOFF = timedelta(minutes=15)
RECLAIM_EVERY = timedelta(seconds=60)
PURGE_AUDIO_EVERY = timedelta(hours=1)


def backoff(attempts: int) -> timedelta:
    return min(BASE_BACKOFF * 2 ** max(attempts - 1, 0), MAX_BACKOFF)


def is_retryable(exc: BaseException) -> bool:
    """Provider errors say for themselves. Other domain errors (not found, invalid input) never get better on retry.
    Anything unexpected (a dropped DB connection, a bug) is retried, and attempts bound the damage."""
    if isinstance(exc, ProviderError):
        return exc.retryable
    return not isinstance(exc, DomainError)


def _utcnow() -> datetime:
    return datetime.now(UTC)


class JobRunner:
    def __init__(
        self,
        uow: UnitOfWorkFactory,
        handlers: Mapping[JobKind, JobHandler],
        *,
        poll_interval_s: float,
        stale_after_s: int,
        clock: Callable[[], datetime] = _utcnow,
        purge_audio: Callable[[], Awaitable[int]] | None = None,
    ) -> None:
        self._uow = uow
        self._handlers = handlers
        self._poll_interval_s = poll_interval_s
        self._stale_after = timedelta(seconds=stale_after_s)
        self._clock = clock
        self._stopping = asyncio.Event()
        self._last_reclaim: datetime | None = None
        self._purge_audio = purge_audio
        self._last_purge: datetime | None = None

    @classmethod
    def from_container(cls, container: Container) -> JobRunner:
        s = container.settings
        return cls(
            container.uow,
            build_handlers(container),
            poll_interval_s=s.worker_poll_interval_s,
            stale_after_s=s.job_stale_after_s,
            purge_audio=AudioService(container.uow, container.storage).purge_expired,
        )

    def stop(self) -> None:
        """Graceful stop: the job in progress finishes, then run_forever returns."""
        self._stopping.set()

    async def run_forever(self) -> None:
        log.info("worker.started", poll_interval_s=self._poll_interval_s)
        while not self._stopping.is_set():
            await self.run_maintenance()
            try:
                processed = await self.run_once()
            except Exception as exc:  # the loop must survive DB outages; the next poll retries
                log.error("worker.poll_failed", error=str(exc))
                processed = False
            if not processed:
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._stopping.wait(), timeout=self._poll_interval_s)
        log.info("worker.stopped")

    async def run_maintenance(self) -> None:
        """Periodic housekeeping, due at most once per interval. A failed step is logged and retried next interval
        (every step is idempotent), and never stops the job loop."""
        if self._purge_audio is None:
            return
        now = self._clock()
        if self._last_purge is not None and now - self._last_purge < PURGE_AUDIO_EVERY:
            return
        self._last_purge = now
        try:
            await self._purge_audio()
        except Exception as exc:
            log.error("worker.audio_purge_failed", error=str(exc))

    async def run_once(self) -> bool:
        """Claims and runs at most one job. Returns whether a job was found."""
        now = self._clock()
        async with self._uow() as uow:
            if self._last_reclaim is None or now - self._last_reclaim >= RECLAIM_EVERY:
                reclaimed = await uow.jobs.reclaim_stale(locked_before=now - self._stale_after)
                self._last_reclaim = now
                if reclaimed:
                    log.warning("worker.jobs_reclaimed", count=reclaimed)
            job = await uow.jobs.claim_next(now=now)
            await uow.commit()
        if job is None:
            return False

        bound = log.bind(job_id=str(job.id), meeting_id=str(job.meeting_id), kind=job.kind.value, attempt=job.attempts)
        handler = self._handlers.get(job.kind)
        try:
            if handler is None:
                raise DomainError(f"No handler for job kind {job.kind}")
            await handler.handle(job)
        except Exception as exc:
            await self._fail(job, exc, bound)
        else:
            async with self._uow() as uow:
                await uow.jobs.mark_succeeded(job_id=job.id)
                await uow.commit()
            bound.info("job.succeeded")
        return True

    async def _fail(self, job: Job, exc: Exception, bound: structlog.typing.FilteringBoundLogger) -> None:
        error = f"{type(exc).__name__}: {exc}"[:2000]
        retry = is_retryable(exc) and job.attempts < job.max_attempts
        retry_at = self._clock() + backoff(job.attempts) if retry else None
        async with self._uow() as uow:
            await uow.jobs.mark_failed(job_id=job.id, error=error, retry_at=retry_at)
            if not retry and await uow.meetings.get(user_id=job.user_id, meeting_id=job.meeting_id):
                await uow.meetings.set_status(meeting_id=job.meeting_id, status=MeetingStatus.FAILED)
            await uow.commit()
        if retry:
            bound.warning("job.retry_scheduled", error=error, retry_at=retry_at.isoformat() if retry_at else None)
        else:
            bound.error("job.failed", error=error)


def start_worker(container: Container) -> asyncio.Task[None]:
    """Runs the worker inside the API process (free-tier hosting). Cancel the task to stop it; a job interrupted
    mid-run stays locked until the stale-lock reclaim requeues it."""
    runner = JobRunner.from_container(container)
    return asyncio.create_task(runner.run_forever(), name="slate-job-worker")


async def stop_worker(task: asyncio.Task[None]) -> None:
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task

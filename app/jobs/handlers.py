"""One handler per JobKind (Command pattern). Each runs its service, then enqueues the next stage.

The pipeline order lives here and nowhere else: transcribe -> speakers -> enhance.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Protocol

import structlog

from app.container import Container
from app.domain.models import Job, JobKind
from app.ports.repositories import UnitOfWorkFactory
from app.services.enhancement import EnhancementService
from app.services.speakers import SpeakerService
from app.services.transcription import TranscriptionService

log = structlog.get_logger()


class JobHandler(Protocol):
    async def handle(self, job: Job) -> None: ...


async def _enqueue_next(uow_factory: UnitOfWorkFactory, job: Job, kind: JobKind) -> None:
    async with uow_factory() as uow:
        await uow.jobs.enqueue(meeting_id=job.meeting_id, user_id=job.user_id, kind=kind)
        await uow.commit()


class TranscribeHandler:
    def __init__(self, service: TranscriptionService, uow: UnitOfWorkFactory) -> None:
        self._service = service
        self._uow = uow

    async def handle(self, job: Job) -> None:
        await self._service.transcribe(user_id=job.user_id, meeting_id=job.meeting_id)
        await _enqueue_next(self._uow, job, JobKind.SPEAKERS)


class SpeakersHandler:
    """Naming speakers is a nicety: if it fails, the notes are still produced with "Speaker N" labels."""

    def __init__(self, service: SpeakerService, uow: UnitOfWorkFactory) -> None:
        self._service = service
        self._uow = uow

    async def handle(self, job: Job) -> None:
        try:
            await self._service.suggest(user_id=job.user_id, meeting_id=job.meeting_id)
        except Exception as exc:
            log.warning("speakers.suggest_failed", meeting_id=str(job.meeting_id), error=str(exc))
        await _enqueue_next(self._uow, job, JobKind.ENHANCE)


class EnhanceHandler:
    def __init__(self, service: EnhancementService) -> None:
        self._service = service

    async def handle(self, job: Job) -> None:
        await self._service.enhance(user_id=job.user_id, meeting_id=job.meeting_id)


def build_handlers(container: Container) -> Mapping[JobKind, JobHandler]:
    uow = container.uow
    return {
        JobKind.TRANSCRIBE: TranscribeHandler(
            TranscriptionService(uow, container.storage, container.audio, container.stt), uow
        ),
        JobKind.SPEAKERS: SpeakersHandler(SpeakerService(uow, container.llm), uow),
        JobKind.ENHANCE: EnhanceHandler(EnhancementService(uow, container.llm)),
    }

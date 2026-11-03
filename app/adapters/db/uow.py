"""SQLAlchemy Unit of Work: one AsyncSession (one transaction) per `async with` block."""

from __future__ import annotations

from typing import Self

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.adapters.db.repositories import (
    SqlChunkRepository,
    SqlEnhancedNotesRepository,
    SqlFolderRepository,
    SqlJobRepository,
    SqlMeetingRepository,
    SqlNoteRepository,
    SqlSearchRepository,
    SqlSpeakerRepository,
    SqlTranscriptRepository,
)
from app.adapters.db.session import make_engine, make_sessionmaker


class SqlAlchemyUnitOfWork:
    folders: SqlFolderRepository
    meetings: SqlMeetingRepository
    notes: SqlNoteRepository
    chunks: SqlChunkRepository
    transcripts: SqlTranscriptRepository
    speakers: SqlSpeakerRepository
    enhanced: SqlEnhancedNotesRepository
    jobs: SqlJobRepository
    search: SqlSearchRepository

    def __init__(self, sessionmaker: async_sessionmaker[AsyncSession]) -> None:
        self._sessionmaker = sessionmaker
        self._session: AsyncSession | None = None

    async def __aenter__(self) -> Self:
        session = self._sessionmaker()
        self._session = session
        self.folders = SqlFolderRepository(session)
        self.meetings = SqlMeetingRepository(session)
        self.notes = SqlNoteRepository(session)
        self.chunks = SqlChunkRepository(session)
        self.transcripts = SqlTranscriptRepository(session)
        self.speakers = SqlSpeakerRepository(session)
        self.enhanced = SqlEnhancedNotesRepository(session)
        self.jobs = SqlJobRepository(session)
        self.search = SqlSearchRepository(session)
        return self

    async def __aexit__(self, *exc: object) -> None:
        assert self._session is not None
        try:
            # Anything not committed is discarded; after a commit this is a no-op.
            await self._session.rollback()
        finally:
            await self._session.close()
            self._session = None

    async def commit(self) -> None:
        assert self._session is not None, "UnitOfWork used outside `async with`"
        await self._session.commit()


class SqlAlchemyUnitOfWorkFactory:
    def __init__(self, engine: AsyncEngine) -> None:
        self.engine = engine
        self._sessionmaker = make_sessionmaker(engine)

    @classmethod
    def from_url(cls, url: str) -> SqlAlchemyUnitOfWorkFactory:
        return cls(make_engine(url))

    def __call__(self) -> SqlAlchemyUnitOfWork:
        return SqlAlchemyUnitOfWork(self._sessionmaker)

    async def dispose(self) -> None:
        await self.engine.dispose()

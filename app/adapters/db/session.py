"""Engine and session construction for the async Postgres adapter."""

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine


def make_engine(url: str) -> AsyncEngine:
    return create_async_engine(url, pool_pre_ping=True)


def make_sessionmaker(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    # expire_on_commit=False: domain objects are built before commit and must stay readable after it.
    return async_sessionmaker(engine, expire_on_commit=False, autoflush=False)

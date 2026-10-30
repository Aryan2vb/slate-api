"""Postgres fixtures. DB tests run only when TEST_DATABASE_URL points at a disposable database."""

import os
from collections.abc import AsyncIterator

import pytest
from sqlalchemy import text

from app.adapters.db.tables import Base
from app.adapters.db.uow import SqlAlchemyUnitOfWorkFactory

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if TEST_DATABASE_URL:
        return
    skip = pytest.mark.skip(reason="TEST_DATABASE_URL not set")
    for item in items:
        if "db" in item.keywords:
            item.add_marker(skip)


@pytest.fixture(scope="session")
async def uow_factory() -> AsyncIterator[SqlAlchemyUnitOfWorkFactory]:
    assert TEST_DATABASE_URL
    factory = SqlAlchemyUnitOfWorkFactory.from_url(TEST_DATABASE_URL)
    async with factory.engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    yield factory
    await factory.dispose()


@pytest.fixture(autouse=True)
async def _clean_tables(request: pytest.FixtureRequest) -> AsyncIterator[None]:
    yield
    if "db" not in request.keywords or not TEST_DATABASE_URL:
        return
    factory: SqlAlchemyUnitOfWorkFactory = request.getfixturevalue("uow_factory")
    tables = ", ".join(t.name for t in Base.metadata.sorted_tables)
    async with factory.engine.begin() as conn:
        await conn.execute(text(f"TRUNCATE {tables} CASCADE"))

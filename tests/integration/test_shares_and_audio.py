import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.exc import IntegrityError

from app.adapters.db.uow import SqlAlchemyUnitOfWorkFactory
from app.domain.models import Meeting, TemplateKind

pytestmark = pytest.mark.db

USER_A, USER_B = uuid.uuid4(), uuid.uuid4()


async def _meeting(uow_factory: SqlAlchemyUnitOfWorkFactory, user_id: uuid.UUID = USER_A) -> Meeting:
    async with uow_factory() as uow:
        m = await uow.meetings.create(
            user_id=user_id, title="Pricing", template=TemplateKind.GENERAL, calendar_event_id=None, attendees=[]
        )
        await uow.commit()
        return m


async def test_share_lifecycle(uow_factory: SqlAlchemyUnitOfWorkFactory) -> None:
    m = await _meeting(uow_factory)
    now = datetime.now(UTC)
    async with uow_factory() as uow:
        share = await uow.shares.create(
            meeting_id=m.id, user_id=USER_A, token="t" * 43, include_transcript=True, expires_at=now + timedelta(days=1)
        )
        await uow.commit()
    assert share.include_transcript and share.revoked_at is None

    async with uow_factory() as uow:
        assert await uow.shares.get_by_token(token="t" * 43, now=now) == share
        assert await uow.shares.get_active(user_id=USER_A, meeting_id=m.id, now=now) == share
        assert await uow.shares.get_active(user_id=USER_B, meeting_id=m.id, now=now) is None
        assert await uow.shares.get_by_token(token="t" * 43, now=now + timedelta(days=2)) is None  # expired

    async with uow_factory() as uow:  # rotation in one transaction passes the one-unrevoked-share index
        assert await uow.shares.revoke_active(user_id=USER_A, meeting_id=m.id, now=now) == 1
        await uow.shares.create(
            meeting_id=m.id, user_id=USER_A, token="u" * 43, include_transcript=False, expires_at=now + timedelta(1)
        )
        await uow.commit()
    async with uow_factory() as uow:
        assert await uow.shares.get_by_token(token="t" * 43, now=now) is None
        assert (await uow.shares.get_active(user_id=USER_A, meeting_id=m.id, now=now)).token == "u" * 43  # type: ignore[union-attr]

    with pytest.raises(IntegrityError):  # a second unrevoked share is refused by the database
        async with uow_factory() as uow:
            await uow.shares.create(
                meeting_id=m.id, user_id=USER_A, token="v" * 43, include_transcript=False, expires_at=now
            )

    async with uow_factory() as uow:  # shares go with their meeting
        await uow.meetings.delete(user_id=USER_A, meeting_id=m.id)
        await uow.commit()
    async with uow_factory() as uow:
        assert await uow.shares.get_by_token(token="u" * 43, now=now) is None


async def test_kept_audio_columns_and_expiry_scan(uow_factory: SqlAlchemyUnitOfWorkFactory) -> None:
    now = datetime.now(UTC)
    old, fresh = await _meeting(uow_factory), await _meeting(uow_factory, USER_B)
    async with uow_factory() as uow:
        await uow.meetings.set_audio(meeting_id=old.id, path="a/old.m4a", expires_at=now, duration_ms=1000)
        await uow.meetings.set_audio(
            meeting_id=fresh.id, path="b/new.m4a", expires_at=now + timedelta(days=1), duration_ms=None
        )
        await uow.commit()

    async with uow_factory() as uow:
        got = await uow.meetings.get(user_id=USER_A, meeting_id=old.id)
        assert got is not None and got.audio_path == "a/old.m4a" and got.audio_duration_ms == 1000
        assert [m.id for m in await uow.meetings.list_expired_audio(now=now, limit=10)] == [old.id]
        await uow.meetings.clear_audio(meeting_ids=[old.id])
        await uow.commit()

    async with uow_factory() as uow:
        got = await uow.meetings.get(user_id=USER_A, meeting_id=old.id)
        assert got is not None and got.audio_path is None and got.audio_expires_at is None
        assert await uow.meetings.list_expired_audio(now=now, limit=10) == []

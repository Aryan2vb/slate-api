import uuid
from uuid import UUID

import pytest

from app.adapters.db.uow import SqlAlchemyUnitOfWorkFactory
from app.domain.models import (
    EnhancedNotes,
    Evidence,
    Meeting,
    NoteBlock,
    SearchKind,
    Section,
    TemplateKind,
    TranscriptSegment,
)
from tests.integration.test_repositories import Line

pytestmark = pytest.mark.db

USER_A = uuid.uuid4()
USER_B = uuid.uuid4()


async def _meeting(
    uow_factory: SqlAlchemyUnitOfWorkFactory, title: str, folder_id: UUID | None = None, user_id: UUID = USER_A
) -> Meeting:
    async with uow_factory() as uow:
        m = await uow.meetings.create(
            user_id=user_id,
            title=title,
            template=TemplateKind.LECTURE,
            calendar_event_id=None,
            attendees=[],
            folder_id=folder_id,
        )
        await uow.commit()
    return m


async def test_folder_crud_counts_and_ownership(uow_factory: SqlAlchemyUnitOfWorkFactory) -> None:
    async with uow_factory() as uow:
        compilers = await uow.folders.create(user_id=USER_A, name="compilers", icon="code")
        algebra = await uow.folders.create(user_id=USER_A, name="Algebra", icon=None)
        await uow.folders.create(user_id=USER_B, name="Theirs", icon=None)
        await uow.commit()
    assert compilers.vocabulary == []
    await _meeting(uow_factory, "Lexing", compilers.id)
    await _meeting(uow_factory, "Parsing", compilers.id)
    await _meeting(uow_factory, "Loose")

    async with uow_factory() as uow:
        listed = await uow.folders.list(user_id=USER_A)
        assert [(s.folder.name, s.meeting_count) for s in listed] == [("Algebra", 0), ("compilers", 2)]
        assert await uow.folders.get(user_id=USER_B, folder_id=compilers.id) is None
        assert (
            await uow.folders.update(user_id=USER_B, folder_id=compilers.id, name="x", icon=None, vocabulary=[]) is None
        )
        updated = await uow.folders.update(
            user_id=USER_A, folder_id=compilers.id, name="Compilers", icon=None, vocabulary=["LR(1)", "DFA"]
        )
        assert updated is not None and updated.vocabulary == ["LR(1)", "DFA"] and updated.icon is None
        assert await uow.folders.delete(user_id=USER_B, folder_id=algebra.id) is False
        await uow.commit()

    async with uow_factory() as uow:
        filed = await uow.meetings.list(user_id=USER_A, limit=10, before=None, folder_id=compilers.id)
        assert {m.title for m in filed} == {"Lexing", "Parsing"}
        unfiled = await uow.meetings.list(user_id=USER_A, limit=10, before=None, unfiled=True)
        assert [m.title for m in unfiled] == ["Loose"]
        assert await uow.folders.delete(user_id=USER_A, folder_id=compilers.id) is True
        await uow.commit()

    async with uow_factory() as uow:  # ON DELETE SET NULL: the meetings survive, unfiled
        unfiled = await uow.meetings.list(user_id=USER_A, limit=10, before=None, unfiled=True)
        assert {m.title for m in unfiled} == {"Lexing", "Parsing", "Loose"}


async def test_set_folder_and_notes_for_folder(uow_factory: SqlAlchemyUnitOfWorkFactory) -> None:
    async with uow_factory() as uow:
        folder = await uow.folders.create(user_id=USER_A, name="Compilers", icon=None)
        await uow.commit()
    m = await _meeting(uow_factory, "Lecture 1")
    other = await _meeting(uow_factory, "Not filed")
    async with uow_factory() as uow:
        await uow.notes.replace_all(meeting_id=m.id, lines=[Line(uuid.uuid4(), 0, "LR(1) items")])
        await uow.notes.replace_all(meeting_id=other.id, lines=[Line(uuid.uuid4(), 0, "elsewhere")])
        assert await uow.meetings.set_folder(user_id=USER_B, meeting_id=m.id, folder_id=folder.id) is None
        moved = await uow.meetings.set_folder(user_id=USER_A, meeting_id=m.id, folder_id=folder.id)
        assert moved is not None and moved.folder_id == folder.id
        await uow.commit()
    async with uow_factory() as uow:
        lines = await uow.notes.list_for_folder(user_id=USER_A, folder_id=folder.id)
        assert [n.text for n in lines] == ["LR(1) items"]
        assert await uow.notes.list_for_folder(user_id=USER_B, folder_id=folder.id) == []


async def test_search_covers_notes_transcripts_and_ai_blocks(uow_factory: SqlAlchemyUnitOfWorkFactory) -> None:
    async with uow_factory() as uow:
        folder = await uow.folders.create(user_id=USER_A, name="Compilers", icon=None)
        await uow.commit()
    lecture = await _meeting(uow_factory, "Parsing lecture", folder.id)
    loose = await _meeting(uow_factory, "Loose meeting")
    theirs = await _meeting(uow_factory, "Their lecture", user_id=USER_B)
    notes = EnhancedNotes(
        title="Parsing",
        tldr="LR parsing",
        sections=[
            Section(
                heading="Parsers",
                blocks=[
                    NoteBlock(source="user", text="LR(1) vs LALR"),  # echoes a note line; not an ai_note hit
                    NoteBlock(
                        source="ai",
                        text="An LR(1) parser uses one token of lookahead.",
                        evidence=[Evidence(start_ms=4000, end_ms=9000)],
                    ),
                    NoteBlock(source="ai", text="Shift-reduce conflicts are resolved by precedence."),
                ],
            )
        ],
    )
    async with uow_factory() as uow:
        await uow.notes.replace_all(
            meeting_id=lecture.id,
            lines=[Line(uuid.uuid4(), 0, "LR(1) vs LALR", 1200), Line(uuid.uuid4(), 1, "DFA minimisation")],
        )
        await uow.notes.replace_all(meeting_id=loose.id, lines=[Line(uuid.uuid4(), 0, "DFAs again, कल का lecture")])
        await uow.notes.replace_all(meeting_id=theirs.id, lines=[Line(uuid.uuid4(), 0, "LR(1) DFA")])
        await uow.transcripts.replace(
            meeting_id=lecture.id,
            segments=[
                TranscriptSegment(speaker=0, start_ms=4000, end_ms=9000, text="So an LR(1) parser looks ahead."),
                TranscriptSegment(speaker=1, start_ms=9000, end_ms=12000, text="कल का lecture DFA पर था"),
            ],
        )
        await uow.enhanced.save(meeting_id=lecture.id, notes=notes, model="fake")
        await uow.commit()

    async with uow_factory() as uow:
        # Punctuated query: substring (ILIKE) over all three kinds, never the other user's rows.
        hits = await uow.search.search(user_id=USER_A, query="lr(1)", folder_id=None, limit=20)
        assert {(h.kind, h.text) for h in hits} == {
            (SearchKind.NOTE, "LR(1) vs LALR"),
            (SearchKind.TRANSCRIPT, "So an LR(1) parser looks ahead."),
            (SearchKind.AI_NOTE, "An LR(1) parser uses one token of lookahead."),
        }
        note = next(h for h in hits if h.kind == SearchKind.NOTE)
        assert (note.meeting_id, note.meeting_title, note.folder_id, note.start_ms) == (
            lecture.id,
            "Parsing lecture",
            folder.id,
            1200,
        )
        segment = next(h for h in hits if h.kind == SearchKind.TRANSCRIPT)
        assert (segment.start_ms, segment.end_ms, segment.speaker) == (4000, 9000, 0)
        ai = next(h for h in hits if h.kind == SearchKind.AI_NOTE)
        assert (ai.start_ms, ai.end_ms, ai.speaker) == (4000, 9000, None)

        # Word query: full text with prefix matching; the exact word ranks above the prefix-only "DFAs".
        dfa = await uow.search.search(user_id=USER_A, query="DFA", folder_id=None, limit=20)
        assert [h.text for h in dfa][-1] == "DFAs again, कल का lecture"
        assert {h.text for h in dfa} == {"DFA minimisation", "कल का lecture DFA पर था", "DFAs again, कल का lecture"}

        # Devanagari words, scoped to one folder.
        hindi = await uow.search.search(user_id=USER_A, query="कल का", folder_id=folder.id, limit=20)
        assert [(h.kind, h.meeting_id) for h in hindi] == [(SearchKind.TRANSCRIPT, lecture.id)]

        assert await uow.search.search(user_id=USER_A, query="precedence", folder_id=None, limit=1) != []
        assert await uow.search.search(user_id=USER_A, query="LALR", folder_id=None, limit=20) != []
        assert await uow.search.search(user_id=USER_A, query="50%", folder_id=None, limit=20) == []
        assert len(await uow.search.search(user_id=USER_A, query="lr(1)", folder_id=None, limit=2)) == 2

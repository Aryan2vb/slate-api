import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from app.domain.models import (
    ActionItem,
    EnhancedNotes,
    Evidence,
    MeetingStatus,
    NoteBlock,
    NoteLine,
    Section,
    TemplateKind,
    TranscriptSegment,
)
from app.services.enhancement import RESTORED_SECTION, EnhancementService, ground_notes
from tests.fakes import FakeLlm, FakeStt, InMemoryUnitOfWorkFactory

USER = uuid.uuid4()
SEGMENTS = [TranscriptSegment(speaker=0, start_ms=0, end_ms=60_000, text="We agreed to raise prices.")]


def _line(position: int, text: str, t_ms: int | None = None) -> NoteLine:
    return NoteLine(
        id=uuid.uuid4(), meeting_id=uuid.uuid4(), position=position, text=text, t_ms=t_ms, updated_at=datetime.now(UTC)
    )


def _ev(start: int, end: int) -> list[Evidence]:
    return [Evidence(start_ms=start, end_ms=end)]


def _notes(*blocks: NoteBlock, action_items: list[ActionItem] | None = None) -> EnhancedNotes:
    return EnhancedNotes(
        title="t", tldr="t", sections=[Section(heading="Main", blocks=list(blocks))], action_items=action_items or []
    )


def test_keeps_grounded_blocks_and_user_lines() -> None:
    notes = _notes(NoteBlock(source="user", text="prices up"), NoteBlock(source="ai", text="5%", evidence=_ev(0, 10)))
    assert ground_notes(notes, [_line(0, "prices up")], SEGMENTS) == notes


def test_drops_ai_blocks_without_valid_evidence() -> None:
    notes = _notes(
        NoteBlock(source="ai", text="no evidence"),
        NoteBlock(source="ai", text="out of range", evidence=_ev(50_000, 120_000)),
        NoteBlock(source="ai", text="partly valid", evidence=[*_ev(0, 5), *_ev(-1, 5)]),
    )
    result = ground_notes(notes, [], SEGMENTS)
    assert [(b.text, b.evidence) for b in result.sections[0].blocks] == [("partly valid", _ev(0, 5))]


def test_restores_user_lines_the_model_dropped_or_reworded_in_order() -> None:
    notes = _notes(NoteBlock(source="user", text="Prices go up!"))  # reworded from "prices up"
    lines = [_line(1, "ship by friday"), _line(0, "prices  up "), _line(2, "   ")]

    result = ground_notes(notes, lines, SEGMENTS)

    restored = result.sections[-1]
    assert restored.heading == RESTORED_SECTION
    assert [(b.source, b.text) for b in restored.blocks] == [("user", "prices up"), ("user", "ship by friday")]
    # The reworded "user" block was model text: with no evidence it is dropped, leaving no empty section behind.
    assert [s.heading for s in result.sections] == [RESTORED_SECTION]


def test_mislabelled_user_block_with_evidence_becomes_ai() -> None:
    notes = _notes(NoteBlock(source="user", text="invented", evidence=_ev(0, 10)))
    assert ground_notes(notes, [], SEGMENTS).sections[0].blocks[0].source == "ai"


def test_no_transcript_means_no_ai_content() -> None:
    notes = _notes(
        NoteBlock(source="ai", text="x", evidence=_ev(0, 10)),
        NoteBlock(source="user", text="mine"),
        action_items=[ActionItem(task="t", evidence=_ev(0, 10))],
    )
    result = ground_notes(notes, [_line(0, "mine")], [])
    assert [b.text for s in result.sections for b in s.blocks] == ["mine"]
    assert result.action_items[0].evidence == []


@dataclass
class _Line:
    id: uuid.UUID
    position: int
    text: str
    t_ms: int | None


async def test_service_saves_grounded_notes_and_marks_ready() -> None:
    uow = InMemoryUnitOfWorkFactory()
    async with uow() as u:
        meeting = await u.meetings.create(
            user_id=USER, title="Pricing", template=TemplateKind.SALES, calendar_event_id=None, attendees=[]
        )
        await u.notes.replace_all(meeting_id=meeting.id, lines=[_Line(uuid.uuid4(), 0, "pricing", 65_000)])
        await u.transcripts.replace(meeting_id=meeting.id, segments=FakeStt().transcript.segments)
    raw = _notes(NoteBlock(source="ai", text="ungrounded"), NoteBlock(source="ai", text="ok", evidence=_ev(0, 4000)))
    llm = FakeLlm([raw])

    saved = await EnhancementService(uow, llm).enhance(user_id=USER, meeting_id=meeting.id)

    assert [(b.source, b.text) for s in saved.sections for b in s.blocks] == [("ai", "ok"), ("user", "pricing")]
    assert uow.state.enhanced[meeting.id] == (saved, "fake-llm")
    assert uow.state.meetings[meeting.id].status == MeetingStatus.READY
    system, prompt = llm.prompts[0]
    assert "exactly as written" in system
    assert "[01:05] pricing" in prompt and "sales call" in prompt

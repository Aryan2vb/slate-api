"""Map-reduce enhancement: long transcripts are enhanced in windows that each fit one request, then merged."""

import json
import re
import uuid
from dataclasses import dataclass
from itertools import pairwise

from pydantic import BaseModel

from app.domain.models import EnhancedNotes, Evidence, NoteBlock, Section, TemplateKind, TranscriptSegment
from app.services.enhancement import RESTORED_SECTION, EnhancementService, estimate_tokens, split_windows
from app.services.prompts import ENHANCE_SYSTEM, ENHANCE_WINDOW_SYSTEM, MERGE_SYSTEM
from tests.fakes import FakeLlm, InMemoryUnitOfWorkFactory

USER = uuid.uuid4()
SEGMENT_MS = 10_000
BUDGET = 2_500  # tokens: the window system prompt alone is ~1,000, so a window holds roughly a dozen segments


def _segments(n: int) -> list[TranscriptSegment]:
    return [
        TranscriptSegment(
            speaker=i % 2,
            start_ms=i * SEGMENT_MS,
            end_ms=(i + 1) * SEGMENT_MS,
            text=f"segment {i} " + "lorem ipsum dolor sit amet " * 10,
        )
        for i in range(n)
    ]


@dataclass
class _Line:
    id: uuid.UUID
    position: int
    text: str
    t_ms: int | None


def _between(prompt: str, tag: str) -> str:
    return prompt.split(f"<{tag}>\n", 1)[1].split(f"\n</{tag}>", 1)[0]


class ScriptedLlm:
    """Answers like a well-behaved model, from what is in the prompt.

    A window call returns one "ai" block citing each transcript line it was shown, plus each user line it was given
    (reworded with the prompt's "[mm:ss]" prefix, as models do). A merge call concatenates the partials' sections and
    repeats the first user block, so grounding has something to fix.
    """

    name = "scripted-llm"

    def __init__(self, *, pad_ai_text: int = 0) -> None:
        self.calls: list[tuple[str, str]] = []
        self.pad_ai_text = pad_ai_text

    async def generate[T: BaseModel](self, *, system: str, prompt: str, schema: type[T], max_tokens: int = 8000) -> T:
        self.calls.append((system, prompt))
        notes = self._merge(prompt) if system == MERGE_SYSTEM else self._window(prompt)
        assert schema is EnhancedNotes
        return notes  # type: ignore[return-value]

    def _window(self, prompt: str) -> EnhancedNotes:
        ranges = [(int(a), int(b)) for a, b in re.findall(r"\((\d+)-(\d+)ms\)", _between(prompt, "transcript"))]
        notes = [ln for ln in _between(prompt, "notes").splitlines() if ln.startswith("[")]
        blocks = [NoteBlock(source="user", text=ln) for ln in notes]
        blocks += [
            NoteBlock(
                source="ai", text=f"point at {a}" + "x" * self.pad_ai_text, evidence=[Evidence(start_ms=a, end_ms=b)]
            )
            for a, b in ranges
        ]
        return EnhancedNotes(
            title="part", tldr="Part.", sections=[Section(heading=f"From {ranges[0][0]}", blocks=blocks)]
        )

    def _merge(self, prompt: str) -> EnhancedNotes:
        partials = [EnhancedNotes.model_validate(p) for p in json.loads(_between(prompt, "partials"))]
        sections = [s for p in partials for s in p.sections]
        user = next((b for s in sections for b in s.blocks if b.source == "user"), None)
        if user is not None:
            sections.append(Section(heading="Repeated", blocks=[user]))
        return EnhancedNotes(title="Lecture", tldr="One. Two.", sections=sections)

    @property
    def window_calls(self) -> list[str]:
        return [p for s, p in self.calls if s == ENHANCE_WINDOW_SYSTEM]

    @property
    def merge_calls(self) -> list[str]:
        return [p for s, p in self.calls if s == MERGE_SYSTEM]


async def _meeting(uow: InMemoryUnitOfWorkFactory, segments: list[TranscriptSegment], lines: list[_Line]) -> uuid.UUID:
    async with uow() as u:
        meeting = await u.meetings.create(
            user_id=USER, title="Compilers", template=TemplateKind.LECTURE, calendar_event_id=None, attendees=[]
        )
        await u.meetings.set_languages(meeting_id=meeting.id, languages=["hi", "en"])
        await u.notes.replace_all(meeting_id=meeting.id, lines=lines)
        await u.transcripts.replace(meeting_id=meeting.id, segments=segments)
    return meeting.id


def test_split_windows_keeps_segments_whole_and_overlaps_by_one() -> None:
    segments = _segments(10)
    windows = split_windows(segments, lambda w: len(w) <= 4)

    assert [[s.start_ms // SEGMENT_MS for s in w] for w in windows] == [[0, 1, 2, 3], [3, 4, 5, 6], [6, 7, 8, 9]]
    assert all(s in segments for w in windows for s in w)  # segments are passed through, never cut or rewritten


def test_split_windows_gives_an_oversized_segment_its_own_window() -> None:
    windows = split_windows(_segments(3), lambda w: False)
    assert [len(w) for w in windows] == [1, 1, 1]


async def test_short_transcript_uses_one_call() -> None:
    uow = InMemoryUnitOfWorkFactory()
    meeting_id = await _meeting(uow, _segments(3), [_Line(uuid.uuid4(), 0, "parsing", 5_000)])
    llm = FakeLlm([EnhancedNotes(title="t", tldr="t", sections=[])])

    await EnhancementService(uow, llm, max_input_tokens=BUDGET).enhance(user_id=USER, meeting_id=meeting_id)

    assert len(llm.prompts) == 1
    system, prompt = llm.prompts[0]
    assert system == ENHANCE_SYSTEM and "segment 2" in prompt


async def test_long_transcript_is_enhanced_in_windows_then_merged() -> None:
    segments = _segments(60)
    lines = [
        _Line(uuid.uuid4(), 0, "LR(1) parser", 15_000),
        _Line(uuid.uuid4(), 1, "first & follow sets", 305_000),
        _Line(uuid.uuid4(), 2, "exam: conflicts!", 590_000),
        _Line(uuid.uuid4(), 3, "typed after the end", 900_000),
    ]
    uow = InMemoryUnitOfWorkFactory()
    meeting_id = await _meeting(uow, segments, lines)
    llm = ScriptedLlm()
    service = EnhancementService(uow, llm, max_input_tokens=BUDGET)

    notes = await service.enhance(user_id=USER, meeting_id=meeting_id)

    windows = llm.window_calls
    assert len(windows) > 2 and llm.merge_calls  # N windows, then at least one merge
    assert len(llm.calls) == len(windows) + len(llm.merge_calls)
    # Every request fits the budget.
    assert all(estimate_tokens(s + p) <= BUDGET for s, p in llm.calls)
    # Windows hold whole segments, in order, covering the transcript with at most one segment of overlap.
    shown = [[int(a) for a in re.findall(r"\((\d+)-\d+ms\)", _between(p, "transcript"))] for p in windows]
    for window, prompt in zip(shown, windows, strict=True):
        for start in window:
            assert segments[start // SEGMENT_MS].text in prompt
    assert sorted({ms for w in shown for ms in w}) == [s.start_ms for s in segments]
    for prev, nxt in pairwise(shown):
        assert nxt[0] == prev[-1]  # one shared segment
    assert all(f"Part {i} of {len(windows)}" in p for i, p in enumerate(windows, start=1))
    # Language rule reaches every call; the merge never sees the transcript.
    assert all("Hindi" in p for _, p in llm.calls)
    assert all("<transcript>" not in p and "lorem ipsum" not in p for p in llm.merge_calls)

    # Grounded against the whole meeting: each user line verbatim and exactly once, none lost.
    user_texts = [b.text for s in notes.sections for b in s.blocks if b.source == "user"]
    assert sorted(user_texts) == sorted(line.text for line in lines)
    assert all(s.heading != RESTORED_SECTION for s in notes.sections)
    # Evidence from every window survives the merge intact.
    evidence = {(e.start_ms, e.end_ms) for s in notes.sections for b in s.blocks for e in b.evidence}
    assert evidence == {(s.start_ms, s.end_ms) for s in segments}


async def test_window_gets_only_nearby_user_lines() -> None:
    uow = InMemoryUnitOfWorkFactory()
    meeting_id = await _meeting(
        uow,
        _segments(60),
        [_Line(uuid.uuid4(), 0, "early line", 5_000), _Line(uuid.uuid4(), 1, "untimed line", None)],
    )
    llm = ScriptedLlm()

    await EnhancementService(uow, llm, max_input_tokens=BUDGET).enhance(user_id=USER, meeting_id=meeting_id)

    assert "early line" in llm.window_calls[0] and "early line" not in llm.window_calls[-1]
    assert all("untimed line" in p for p in llm.window_calls)


async def test_merges_hierarchically_when_partials_exceed_the_budget() -> None:
    uow = InMemoryUnitOfWorkFactory()
    meeting_id = await _meeting(uow, _segments(60), [])
    llm = ScriptedLlm(pad_ai_text=600)  # partials so large that only two fit one merge request

    notes = await EnhancementService(uow, llm, max_input_tokens=BUDGET).enhance(user_id=USER, meeting_id=meeting_id)

    # Pairs are merged, then pairs of merges, and so on: always one fewer merge than windows.
    assert len(llm.merge_calls) == len(llm.window_calls) - 1
    assert any('"title":"Lecture"' in p for p in llm.merge_calls)  # a later round merged earlier merges
    evidence = {(e.start_ms, e.end_ms) for s in notes.sections for b in s.blocks for e in b.evidence}
    assert len(evidence) == 60

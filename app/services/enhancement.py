"""Granola-style enhancement: the user's notes are the outline, the transcript fills in grounded detail.

The model's output is never trusted as-is. `ground_notes` enforces the two promises the product makes: the user's own
lines are always kept word for word, and every AI addition points at transcript evidence that exists.

Long meetings are enhanced in parts so no request has to carry the whole transcript: it is split into windows that
each fit one request, every window gets partial notes, and the partials are merged (hierarchically if needed) without
the transcript. Grounding always runs on the final result against the whole meeting.
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from uuid import UUID

import structlog

from app.config import get_settings
from app.domain.errors import NotFoundError
from app.domain.models import (
    EnhancedNotes,
    Evidence,
    Meeting,
    MeetingStatus,
    NoteBlock,
    NoteLine,
    Section,
    TranscriptSegment,
)
from app.ports.providers import LanguageModel
from app.ports.repositories import UnitOfWorkFactory
from app.services.prompts import (
    ENHANCE_SYSTEM,
    ENHANCE_WINDOW_SYSTEM,
    MERGE_SYSTEM,
    enhance_prompt,
    enhance_window_prompt,
    format_attendees,
    format_notes,
    format_transcript,
    merge_prompt,
)

log = structlog.get_logger()

RESTORED_SECTION = "Your notes"

ENHANCE_MAX_TOKENS = 12_000  # whole meeting, or the final merge
WINDOW_MAX_TOKENS = 4_000  # notes for one window are far shorter than the window's transcript
WINDOW_OVERLAP = 1  # segments shared by neighbouring windows, so a point spanning the cut is seen whole
# A line is usually typed shortly after (sometimes before) what it is about, so windows also get lines typed nearby.
NOTE_MARGIN_MS = 30_000


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _valid_evidence(evidence: Sequence[Evidence], end_bound: int | None) -> list[Evidence]:
    if end_bound is None:
        return []
    return [e for e in evidence if 0 <= e.start_ms <= e.end_ms <= end_bound]


def estimate_tokens(text: str) -> int:
    """A cheap, conservative token estimate: about 4 UTF-8 bytes a token.

    Bytes rather than characters, so scripts such as Devanagari (3 bytes a character, and more tokens a character
    than English) are not underestimated.
    """
    return len(text.encode()) // 4 + 1


def split_windows(
    segments: Sequence[TranscriptSegment],
    fits: Callable[[Sequence[TranscriptSegment]], bool],
    overlap: int = WINDOW_OVERLAP,
) -> list[list[TranscriptSegment]]:
    """Splits segments into consecutive windows of whole segments, each the longest run for which `fits` holds.

    A window always has at least one segment, so an oversized segment still makes progress. Neighbouring windows
    share up to `overlap` segments.
    """
    windows: list[list[TranscriptSegment]] = []
    start = 0
    while start < len(segments):
        end = start + 1
        while end < len(segments) and fits(segments[start : end + 1]):
            end += 1
        windows.append(list(segments[start:end]))
        if end == len(segments):
            break
        start = max(end - overlap, start + 1)
    return windows


def ground_notes(
    notes: EnhancedNotes, user_lines: Sequence[NoteLine], segments: Sequence[TranscriptSegment]
) -> EnhancedNotes:
    """Returns notes where every user line is present verbatim and every AI block cites real transcript time.

    - A "user" block's text is rebuilt from the user's original lines it contains, so anything the model added
      around them (timestamps it echoed from the prompt, rewording) never reaches the user as their own words.
    - "user" blocks that contain none of the user's lines are model text mislabelled as the user's: they are
      treated as "ai" blocks and must carry evidence like any other.
    - Evidence outside [0, end of transcript] is removed; AI blocks left without evidence are dropped.
    - Each user line is kept once: a later "user" block that repeats only lines already placed is dropped (partial
      notes for overlapping windows can each carry the same line).
    - User lines the model dropped are restored under a trailing "Your notes" section, in their original order.
    """
    end_bound = max((s.end_ms for s in segments), default=None)
    originals: dict[str, str] = {}
    for line in sorted(user_lines, key=lambda n: n.position):
        if _norm(line.text):
            originals.setdefault(_norm(line.text), line.text)
    wanted = list(originals)

    sections: list[Section] = []
    present: set[str] = set()
    dropped_ai = 0
    for section in notes.sections:
        blocks: list[NoteBlock] = []
        for block in section.blocks:
            text = _norm(block.text)
            evidence = _valid_evidence(block.evidence, end_bound)
            owned = [w for w in wanted if w in text]
            fresh = [w for w in owned if w not in present]
            if block.source == "user" and fresh:
                present.update(fresh)
                verbatim = "\n".join(originals[w] for w in fresh)
                blocks.append(block.model_copy(update={"text": verbatim, "evidence": evidence}))
            elif block.source == "user" and owned:
                dropped_ai += 1  # a repeat of the user's lines placed earlier
            elif evidence:
                blocks.append(block.model_copy(update={"source": "ai", "evidence": evidence}))
            else:
                dropped_ai += 1
        if blocks:
            sections.append(section.model_copy(update={"blocks": blocks}))

    missing = [w for w in wanted if w not in present]
    if missing:
        restored = [NoteBlock(source="user", text=originals[w]) for w in missing]
        sections.append(Section(heading=RESTORED_SECTION, blocks=restored))

    action_items = [
        item.model_copy(update={"evidence": _valid_evidence(item.evidence, end_bound)}) for item in notes.action_items
    ]
    if dropped_ai or missing:
        log.info("enhancement.grounded", dropped_ai_blocks=dropped_ai, restored_user_lines=len(missing))
    return notes.model_copy(update={"sections": sections, "action_items": action_items})


@dataclass(frozen=True)
class _Brief:
    """What every prompt for one meeting shares, besides its notes and transcript."""

    meeting: Meeting
    attendees: str
    names: dict[int, str]


class EnhancementService:
    def __init__(self, uow: UnitOfWorkFactory, llm: LanguageModel, *, max_input_tokens: int | None = None) -> None:
        self._uow = uow
        self._llm = llm
        self._budget = max_input_tokens if max_input_tokens is not None else get_settings().llm_max_input_tokens

    async def enhance(self, *, user_id: UUID, meeting_id: UUID) -> EnhancedNotes:
        async with self._uow() as uow:
            meeting = await uow.meetings.get(user_id=user_id, meeting_id=meeting_id)
            if meeting is None:
                raise NotFoundError("Meeting not found")
            lines = await uow.notes.list(meeting_id=meeting_id)
            segments = await uow.transcripts.list(meeting_id=meeting_id)
            names = {s.label: s.name for s in await uow.speakers.list(meeting_id=meeting_id) if s.name}

        brief = _Brief(meeting=meeting, attendees=format_attendees(meeting.attendees), names=names)
        prompt = enhance_prompt(
            title=meeting.title,
            template=meeting.template,
            attendees=brief.attendees,
            notes=format_notes(lines),
            transcript=format_transcript(segments, names) or "(no transcript)",
            languages=meeting.languages,
        )
        estimated = estimate_tokens(ENHANCE_SYSTEM + prompt)
        if estimated <= self._budget or len(segments) < 2:
            raw = await self._llm.generate(
                system=ENHANCE_SYSTEM, prompt=prompt, schema=EnhancedNotes, max_tokens=ENHANCE_MAX_TOKENS
            )
        else:
            raw = await self._enhance_in_parts(brief, lines, segments, estimated=estimated)
        # Grounded against the whole meeting, whichever path produced the notes.
        notes = ground_notes(raw, lines, segments)

        async with self._uow() as uow:
            await uow.enhanced.save(meeting_id=meeting_id, notes=notes, model=self._llm.name)
            await uow.meetings.set_status(meeting_id=meeting_id, status=MeetingStatus.READY)
            await uow.commit()
        log.info(
            "enhancement.completed",
            meeting_id=str(meeting_id),
            sections=len(notes.sections),
            action_items=len(notes.action_items),
        )
        return notes

    async def _enhance_in_parts(
        self,
        brief: _Brief,
        lines: Sequence[NoteLine],
        segments: Sequence[TranscriptSegment],
        *,
        estimated: int,
    ) -> EnhancedNotes:
        """Map: partial notes per transcript window. Reduce: merge them. Calls run one after another, since a
        provider's tokens-per-minute cap is what made the single request too large in the first place."""

        def window_prompt(window: Sequence[TranscriptSegment], part: int = 1, parts: int = 1) -> str:
            return enhance_window_prompt(
                part=part,
                parts=parts,
                title=brief.meeting.title,
                template=brief.meeting.template,
                attendees=brief.attendees,
                notes=format_notes(self._window_lines(lines, window, segments)),
                transcript=format_transcript(window, brief.names),
                languages=brief.meeting.languages,
            )

        def fits(window: Sequence[TranscriptSegment]) -> bool:
            # "Part 99 of 99" so the estimate never grows once the real numbers are filled in.
            return estimate_tokens(ENHANCE_WINDOW_SYSTEM + window_prompt(window, 99, 99)) <= self._budget

        windows = split_windows(segments, fits)
        log.info(
            "enhancement.chunked",
            meeting_id=str(brief.meeting.id),
            windows=len(windows),
            segments=len(segments),
            estimated_tokens=estimated,
            budget=self._budget,
        )
        partials = [
            await self._llm.generate(
                system=ENHANCE_WINDOW_SYSTEM,
                prompt=window_prompt(window, part, len(windows)),
                schema=EnhancedNotes,
                max_tokens=WINDOW_MAX_TOKENS,
            )
            for part, window in enumerate(windows, start=1)
        ]
        return await self._merge(brief, partials)

    @staticmethod
    def _window_lines(
        lines: Sequence[NoteLine], window: Sequence[TranscriptSegment], segments: Sequence[TranscriptSegment]
    ) -> list[NoteLine]:
        """The user's lines typed during (or near) the window. The first and last windows also take lines typed
        before or after the transcript; lines without a time go to every window."""
        lo = -math.inf if window[0].start_ms <= segments[0].start_ms else window[0].start_ms - NOTE_MARGIN_MS
        hi = math.inf if window[-1].end_ms >= segments[-1].end_ms else window[-1].end_ms + NOTE_MARGIN_MS
        return [n for n in lines if n.t_ms is None or lo <= n.t_ms <= hi]

    async def _merge(self, brief: _Brief, partials: list[EnhancedNotes]) -> EnhancedNotes:
        """Merges partial notes into one, in rounds: each round merges consecutive batches that fit the budget, so
        a meeting with too many parts for one merge is merged hierarchically."""
        level = 0
        while len(partials) > 1:
            level += 1
            batches = self._merge_batches(brief, partials)
            merged: list[EnhancedNotes] = []
            for batch in batches:
                if len(batch) == 1:
                    merged.append(batch[0])  # left over at the end of a round: merged in the next one
                    continue
                merged.append(
                    await self._llm.generate(
                        system=MERGE_SYSTEM,
                        prompt=self._merge_prompt(brief, batch),
                        schema=EnhancedNotes,
                        max_tokens=ENHANCE_MAX_TOKENS,
                    )
                )
            log.info(
                "enhancement.merged",
                meeting_id=str(brief.meeting.id),
                level=level,
                inputs=len(partials),
                outputs=len(merged),
            )
            partials = merged
        return partials[0]

    def _merge_batches(self, brief: _Brief, partials: Sequence[EnhancedNotes]) -> list[list[EnhancedNotes]]:
        """Consecutive batches that fit the budget. A batch takes at least two partials (even over budget), so
        every round shrinks the list and merging always ends."""
        batches: list[list[EnhancedNotes]] = []
        start = 0
        while start < len(partials):
            end = min(start + 2, len(partials))
            while end < len(partials) and self._merge_fits(brief, partials[start : end + 1]):
                end += 1
            batches.append(list(partials[start:end]))
            start = end
        return batches

    def _merge_fits(self, brief: _Brief, batch: Sequence[EnhancedNotes]) -> bool:
        return estimate_tokens(MERGE_SYSTEM + self._merge_prompt(brief, batch)) <= self._budget

    @staticmethod
    def _merge_prompt(brief: _Brief, batch: Sequence[EnhancedNotes]) -> str:
        partials = json.dumps([p.model_dump(mode="json") for p in batch], ensure_ascii=False, separators=(",", ":"))
        return merge_prompt(
            title=brief.meeting.title,
            template=brief.meeting.template,
            partials=partials,
            parts=len(batch),
            languages=brief.meeting.languages,
        )

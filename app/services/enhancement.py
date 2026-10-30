"""Granola-style enhancement: the user's notes are the outline, the transcript fills in grounded detail.

The model's output is never trusted as-is. `ground_notes` enforces the two promises the product makes: the user's own
lines are always kept word for word, and every AI addition points at transcript evidence that exists.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from uuid import UUID

import structlog

from app.domain.errors import NotFoundError
from app.domain.models import (
    EnhancedNotes,
    Evidence,
    MeetingStatus,
    NoteBlock,
    NoteLine,
    Section,
    TranscriptSegment,
)
from app.ports.providers import LanguageModel
from app.ports.repositories import UnitOfWorkFactory
from app.services.prompts import ENHANCE_SYSTEM, enhance_prompt, format_attendees, format_notes, format_transcript

log = structlog.get_logger()

RESTORED_SECTION = "Your notes"


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _valid_evidence(evidence: Sequence[Evidence], end_bound: int | None) -> list[Evidence]:
    if end_bound is None:
        return []
    return [e for e in evidence if 0 <= e.start_ms <= e.end_ms <= end_bound]


def ground_notes(
    notes: EnhancedNotes, user_lines: Sequence[NoteLine], segments: Sequence[TranscriptSegment]
) -> EnhancedNotes:
    """Returns notes where every user line is present verbatim and every AI block cites real transcript time.

    - "user" blocks that contain none of the user's lines are model text mislabelled as the user's: they are
      treated as "ai" blocks and must carry evidence like any other.
    - Evidence outside [0, end of transcript] is removed; AI blocks left without evidence are dropped.
    - User lines the model dropped are restored under a trailing "Your notes" section, in their original order.
    """
    end_bound = max((s.end_ms for s in segments), default=None)
    wanted = [_norm(line.text) for line in sorted(user_lines, key=lambda n: n.position) if _norm(line.text)]

    sections: list[Section] = []
    present: set[str] = set()
    dropped_ai = 0
    for section in notes.sections:
        blocks: list[NoteBlock] = []
        for block in section.blocks:
            text = _norm(block.text)
            evidence = _valid_evidence(block.evidence, end_bound)
            owned = [w for w in wanted if w in text]
            if block.source == "user" and owned:
                present.update(owned)
                blocks.append(block.model_copy(update={"evidence": evidence}))
            elif evidence:
                blocks.append(block.model_copy(update={"source": "ai", "evidence": evidence}))
            else:
                dropped_ai += 1
        if blocks:
            sections.append(section.model_copy(update={"blocks": blocks}))

    missing = [w for w in dict.fromkeys(wanted) if w not in present]
    if missing:
        sections.append(Section(heading=RESTORED_SECTION, blocks=[NoteBlock(source="user", text=w) for w in missing]))

    action_items = [
        item.model_copy(update={"evidence": _valid_evidence(item.evidence, end_bound)}) for item in notes.action_items
    ]
    if dropped_ai or missing:
        log.info("enhancement.grounded", dropped_ai_blocks=dropped_ai, restored_user_lines=len(missing))
    return notes.model_copy(update={"sections": sections, "action_items": action_items})


class EnhancementService:
    def __init__(self, uow: UnitOfWorkFactory, llm: LanguageModel) -> None:
        self._uow = uow
        self._llm = llm

    async def enhance(self, *, user_id: UUID, meeting_id: UUID) -> EnhancedNotes:
        async with self._uow() as uow:
            meeting = await uow.meetings.get(user_id=user_id, meeting_id=meeting_id)
            if meeting is None:
                raise NotFoundError("Meeting not found")
            lines = await uow.notes.list(meeting_id=meeting_id)
            segments = await uow.transcripts.list(meeting_id=meeting_id)
            names = {s.label: s.name for s in await uow.speakers.list(meeting_id=meeting_id) if s.name}

        raw = await self._llm.generate(
            system=ENHANCE_SYSTEM,
            prompt=enhance_prompt(
                title=meeting.title,
                template=meeting.template,
                attendees=format_attendees(meeting.attendees),
                notes=format_notes(lines),
                transcript=format_transcript(segments, names) or "(no transcript)",
            ),
            schema=EnhancedNotes,
            max_tokens=12_000,
        )
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

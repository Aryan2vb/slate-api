"""Share links and exports of one meeting's notes.

`ShareService` manages the public links (one active per meeting; creating again rotates it). `to_markdown` and
`to_html` are pure renderers of a MeetingDetail: the export and the public page show the same content. Every piece
of user or AI text reaching HTML goes through html.escape, so notes can never inject markup or script.
"""

from __future__ import annotations

import html
import re
import secrets
import unicodedata
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

import structlog

from app.domain.errors import NotFoundError
from app.domain.models import EnhancedNotes, Evidence, Meeting, Share, TranscriptSegment
from app.ports.repositories import UnitOfWorkFactory
from app.presentation import BRAND_HEADER, PAGE_CSS
from app.services.meetings import MeetingDetail, load_detail, require_meeting

log = structlog.get_logger(__name__)

TOKEN_BYTES = 32  # secrets.token_urlsafe(32) is 43 URL-safe characters
AI_MARK = "✦"


def _utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class SharedMeeting:
    share: Share
    detail: MeetingDetail


class ShareService:
    def __init__(
        self,
        uow: UnitOfWorkFactory,
        *,
        public_base_url: str,
        ttl_days: int,
        clock: Callable[[], datetime] = _utcnow,
    ) -> None:
        self._uow = uow
        self._base = public_base_url.rstrip("/")
        self._ttl = timedelta(days=ttl_days)
        self._clock = clock

    def url_for(self, share: Share) -> str:
        return f"{self._base}/s/{share.token}"

    async def create(self, *, user_id: UUID, meeting_id: UUID, include_transcript: bool) -> Share:
        """A new link for the meeting; any earlier link stops working (rotation)."""
        now = self._clock()
        async with self._uow() as uow:
            await require_meeting(uow, user_id=user_id, meeting_id=meeting_id)
            rotated = await uow.shares.revoke_active(user_id=user_id, meeting_id=meeting_id, now=now)
            share = await uow.shares.create(
                meeting_id=meeting_id,
                user_id=user_id,
                token=secrets.token_urlsafe(TOKEN_BYTES),
                include_transcript=include_transcript,
                expires_at=now + self._ttl,
            )
            await uow.commit()
        # Never log the token: it is the credential.
        log.info(
            "share.created",
            meeting_id=str(meeting_id),
            share_id=str(share.id),
            include_transcript=include_transcript,
            rotated=rotated,
        )
        return share

    async def get(self, *, user_id: UUID, meeting_id: UUID) -> Share:
        async with self._uow() as uow:
            await require_meeting(uow, user_id=user_id, meeting_id=meeting_id)
            share = await uow.shares.get_active(user_id=user_id, meeting_id=meeting_id, now=self._clock())
        if share is None:
            raise NotFoundError("Meeting is not shared")
        return share

    async def revoke(self, *, user_id: UUID, meeting_id: UUID) -> None:
        """Idempotent: revoking a meeting that is not shared is not an error."""
        async with self._uow() as uow:
            await require_meeting(uow, user_id=user_id, meeting_id=meeting_id)
            revoked = await uow.shares.revoke_active(user_id=user_id, meeting_id=meeting_id, now=self._clock())
            await uow.commit()
        log.info("share.revoked", meeting_id=str(meeting_id), revoked=revoked)

    async def open(self, token: str) -> SharedMeeting:
        """What a public link shows. NotFoundError for unknown, revoked and expired links alike."""
        async with self._uow() as uow:
            share = await uow.shares.get_by_token(token=token, now=self._clock())
            if share is None:
                raise NotFoundError("Share not found")
            detail = await load_detail(uow, user_id=share.user_id, meeting_id=share.meeting_id)
        log.info("share.viewed", meeting_id=str(share.meeting_id), share_id=str(share.id))
        return SharedMeeting(share=share, detail=detail)


# --- Rendering ------------------------------------------------------------------------------------------------


def format_ms(ms: int) -> str:
    """mm:ss, or h:mm:ss from one hour on."""
    seconds = max(ms, 0) // 1000
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes:02d}:{secs:02d}"


def _date(meeting: Meeting) -> str:
    when = meeting.started_at or meeting.created_at
    return f"{when.day} {when:%B %Y}"


def _one_line(text: str) -> str:
    return " ".join(text.split())


def _stamps(evidence: Iterable[Evidence]) -> list[str]:
    return [format_ms(e.start_ms) for e in evidence]


def _speaker_names(detail: MeetingDetail) -> dict[int, str]:
    return {s.label: s.name for s in detail.speakers if s.name}


def _speaker(names: dict[int, str], segment: TranscriptSegment) -> str:
    return names.get(segment.speaker, f"Speaker {segment.speaker}")


def _has_lists(notes: EnhancedNotes) -> bool:
    return bool(notes.action_items or notes.decisions or notes.open_questions)


def to_markdown(detail: MeetingDetail, *, include_transcript: bool = False) -> str:
    meeting, notes = detail.meeting, detail.enhanced
    out: list[str] = [f"# {_one_line(meeting.title)}", "", f"_{_date(meeting)}_", ""]
    if notes is None:
        lines = [n for n in detail.notes if n.text.strip()]
        if lines:
            out += ["## Notes", ""]
            out += [f"- {_one_line(n.text)}" for n in lines]
            out.append("")
    else:
        if notes.tldr.strip():
            out += ["## TL;DR", "", notes.tldr.strip(), ""]
        for section in notes.sections:
            out += [f"## {_one_line(section.heading)}", ""]
            for block in section.blocks:
                stamps = "".join(f" [{s}]" for s in _stamps(block.evidence))
                marker = f"{AI_MARK} " if block.source == "ai" else ""
                out.append(f"- {marker}{_one_line(block.text)}{stamps}")
            out.append("")
        if notes.action_items:
            out += ["## Action items", ""]
            for item in notes.action_items:
                extra = ", ".join(x for x in (item.owner, f"due {item.due}" if item.due else None) if x)
                stamps = "".join(f" [{s}]" for s in _stamps(item.evidence))
                out.append(f"- [ ] {_one_line(item.task)}{f' ({_one_line(extra)})' if extra else ''}{stamps}")
            out.append("")
        for heading, entries in (("Decisions", notes.decisions), ("Open questions", notes.open_questions)):
            if entries:
                out += [f"## {heading}", ""]
                out += [f"- {_one_line(e)}" for e in entries]
                out.append("")
    if include_transcript and detail.transcript:
        names = _speaker_names(detail)
        out += ["## Transcript", ""]
        for seg in detail.transcript:
            out += [f"**{_speaker(names, seg)}** [{format_ms(seg.start_ms)}] {_one_line(seg.text)}", ""]
    return "\n".join(out).rstrip("\n") + "\n"


def _e(text: str) -> str:
    return html.escape(text, quote=True)


def _stamp_html(evidence: Iterable[Evidence]) -> str:
    return "".join(f' <span class="t">{_e(s)}</span>' for s in _stamps(evidence))


def _list_html(entries: Iterable[str]) -> str:
    return "<ul>" + "".join(f"<li>{_e(x)}</li>" for x in entries) + "</ul>"


def _page(title: str, body: str, *, status: bool = False) -> str:
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><meta name="robots" content="noindex,nofollow">
<title>{_e(title)} · Slate</title><style>{PAGE_CSS}</style></head>
<body class="{"status-page" if status else "notes-page"}">{BRAND_HEADER}<main>{body}</main></body></html>"""


def to_html(detail: MeetingDetail, *, include_transcript: bool = False) -> str:
    meeting, notes = detail.meeting, detail.enhanced
    parts: list[str] = [
        '<div class="eyebrow">Shared meeting notes</div>',
        f"<h1>{_e(meeting.title)}</h1>",
        f'<p class="date">{_e(_date(meeting))} &nbsp; / &nbsp; Read-only</p>',
    ]
    if notes is None:
        lines = [n.text for n in detail.notes if n.text.strip()]
        if lines:
            parts.append("<h2>Notes</h2>" + _list_html(lines))
        else:
            parts.append('<p class="empty">No notes have been added to this meeting yet.</p>')
    else:
        if notes.tldr.strip():
            parts.append(
                '<section class="summary-card" aria-label="Meeting summary"><h2>At a glance</h2>'
                f'<p class="tldr">{_e(notes.tldr.strip())}</p></section>'
            )
        for section in notes.sections:
            items = []
            for block in section.blocks:
                if block.source == "ai":
                    mark = f'<span class="mark" aria-label="AI">{AI_MARK}</span>'
                    items.append(f'<li class="ai">{mark}{_e(block.text)}{_stamp_html(block.evidence)}</li>')
                else:
                    items.append(f"<li>{_e(block.text)}</li>")
            parts.append(f"<h2>{_e(section.heading)}</h2><ul>{''.join(items)}</ul>")
        if notes.action_items:
            items = []
            for item in notes.action_items:
                extra = ", ".join(x for x in (item.owner, f"due {item.due}" if item.due else None) if x)
                suffix = f' <span class="t">({_e(extra)})</span>' if extra else ""
                items.append(f"<li>{_e(item.task)}{suffix}{_stamp_html(item.evidence)}</li>")
            parts.append(f"<h2>Action items</h2><ul>{''.join(items)}</ul>")
        if notes.decisions:
            parts.append("<h2>Decisions</h2>" + _list_html(notes.decisions))
        if notes.open_questions:
            parts.append("<h2>Open questions</h2>" + _list_html(notes.open_questions))
    if include_transcript and detail.transcript:
        names = _speaker_names(detail)
        segments = "".join(
            f'<p class="seg"><span class="who">{_e(_speaker(names, seg))}</span> '
            f'<span class="t">{_e(format_ms(seg.start_ms))}</span><br>{_e(seg.text)}</p>'
            for seg in detail.transcript
        )
        parts.append(
            '<details class="transcript"><summary>Transcript'
            '<span class="hint">Read the conversation, with speakers and timestamps</span></summary>'
            f"{segments}</details>"
        )
    parts.append(f"<footer>{AI_MARK} marks lines written by AI from the recording.<br>Shared from Slate.</footer>")
    return _page(meeting.title, "".join(parts))


def not_found_html() -> str:
    return _page(
        "Link not available",
        '<div class="status-symbol" aria-hidden="true">↗</div>'
        '<div class="eyebrow">Shared notes</div>'
        "<h1>This link isn't available</h1>"
        '<p class="message">It may have expired, or the person who shared it turned it off.</p>'
        '<p class="hint">Ask the person who shared it for a new link.</p>',
        status=True,
    )


_UNSAFE_FILENAME = re.compile(r"[^A-Za-z0-9._-]+")


def export_filename(title: str, extension: str = "md") -> str:
    """ASCII-only, filesystem- and header-safe ("Q3 pricing: review" -> "Q3-pricing-review.md")."""
    ascii_title = unicodedata.normalize("NFKD", title).encode("ascii", "ignore").decode()
    stem = _UNSAFE_FILENAME.sub("-", ascii_title).strip("-._")[:80].rstrip("-._")
    return f"{stem or 'meeting'}.{extension}"

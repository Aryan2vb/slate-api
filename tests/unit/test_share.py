import re
import uuid
from datetime import UTC, datetime, timedelta
from typing import cast

from fastapi.testclient import TestClient

from app.config import Settings
from app.container import Container
from app.domain.models import (
    ActionItem,
    EnhancedNotes,
    Evidence,
    Meeting,
    NoteBlock,
    NoteLine,
    Section,
    Speaker,
    TranscriptSegment,
)
from app.services.meetings import MeetingDetail, Stage
from app.services.share import export_filename, format_ms, to_html, to_markdown
from tests.fakes import InMemoryState, InMemoryUnitOfWorkFactory, fake_container
from tests.unit.api_helpers import auth, client, create_meeting

ALICE, BOB = uuid.uuid4(), uuid.uuid4()
XSS = "<script>alert('x')</script>"
WHEN = datetime(2026, 11, 4, 10, 30, tzinfo=UTC)


def _notes(title: str = "Pricing review") -> EnhancedNotes:
    return EnhancedNotes(
        title=title,
        tldr="Prices rise 5% in Q3.",
        sections=[
            Section(
                heading="Pricing",
                blocks=[
                    NoteBlock(source="user", text="raise prices?"),
                    NoteBlock(
                        source="ai",
                        text="Q3 pricing goes up five percent.",
                        evidence=[Evidence(start_ms=65_000, end_ms=70_000)],
                    ),
                ],
            )
        ],
        action_items=[ActionItem(owner="Priya", task="Email the new price sheet", due="Friday")],
        decisions=["Ship the new tiers"],
        open_questions=["Grandfather old customers?"],
    )


def _detail(
    *, title: str = "Pricing review", enhanced: EnhancedNotes | None = None, notes: list[str] | None = None
) -> MeetingDetail:
    meeting = Meeting(id=uuid.uuid4(), user_id=ALICE, title=title, created_at=WHEN, updated_at=WHEN)
    lines = [
        NoteLine(id=uuid.uuid4(), meeting_id=meeting.id, position=i, text=t, updated_at=WHEN)
        for i, t in enumerate(notes or [])
    ]
    return MeetingDetail(
        meeting=meeting,
        notes=lines,
        transcript=[
            TranscriptSegment(speaker=0, start_ms=1_000, end_ms=4_000, text="Hi, I'm Priya."),
            TranscriptSegment(speaker=1, start_ms=3_725_000, end_ms=3_730_000, text="See you next week."),
        ],
        speakers=[Speaker(meeting_id=meeting.id, label=0, name="Priya")],
        enhanced=enhanced,
        jobs=[],
        stage=Stage.READY,
    )


# --- Rendering ------------------------------------------------------------------------------------------------


def test_format_ms() -> None:
    assert format_ms(0) == "00:00"
    assert format_ms(65_400) == "01:05"
    assert format_ms(3_725_000) == "1:02:05"


def test_markdown_export_has_every_part_with_evidence_timestamps() -> None:
    md = to_markdown(_detail(enhanced=_notes()))
    assert md.startswith("# Pricing review\n\n_4 November 2026_\n")
    assert "## TL;DR\n\nPrices rise 5% in Q3." in md
    assert "- raise prices?\n" in md  # the user's line, unmarked
    assert "- ✦ Q3 pricing goes up five percent. [01:05]\n" in md
    assert "- [ ] Email the new price sheet (Priya, due Friday)" in md
    assert "## Decisions\n\n- Ship the new tiers" in md
    assert "## Open questions\n\n- Grandfather old customers?" in md
    assert "Transcript" not in md


def test_markdown_transcript_only_when_asked_and_uses_speaker_names() -> None:
    md = to_markdown(_detail(enhanced=_notes()), include_transcript=True)
    assert "## Transcript" in md
    assert "**Priya** [00:01] Hi, I'm Priya." in md
    assert "**Speaker 1** [1:02:05] See you next week." in md


def test_markdown_falls_back_to_the_users_notes_before_enhancement() -> None:
    md = to_markdown(_detail(notes=["first point", "  ", "second\npoint"]))
    assert "## Notes\n\n- first point\n- second point\n" in md


def test_html_escapes_everything_from_users_and_the_model() -> None:
    notes = EnhancedNotes(
        title=XSS,
        tldr=XSS,
        sections=[Section(heading=XSS, blocks=[NoteBlock(source="user", text=XSS), NoteBlock(source="ai", text=XSS)])],
        action_items=[ActionItem(owner=XSS, task=XSS, due=XSS)],
        decisions=[XSS],
        open_questions=[XSS],
    )
    detail = _detail(title=f'"><img src=x onerror=alert(1)>{XSS}', enhanced=notes)
    page = to_html(detail, include_transcript=True)
    assert "<script" not in page.lower()
    assert "<img" not in page
    assert "&lt;script&gt;alert(&#x27;x&#x27;)&lt;/script&gt;" in page
    assert page.count("&lt;script&gt;") >= 9  # title tag, h1, tldr, heading, 2 blocks, task, owner/due, lists


def test_html_page_marks_ai_lines_and_is_not_indexed() -> None:
    page = to_html(_detail(enhanced=_notes()))
    assert '<meta name="robots" content="noindex,nofollow">' in page
    assert '<li class="ai"><span class="mark" aria-label="AI">✦</span>Q3 pricing goes up five percent.' in page
    assert "<li>raise prices?</li>" in page
    assert "Transcript" not in page
    assert "Priya" in page  # the action item owner
    assert "Hi, I&#x27;m Priya." not in page
    assert "Hi, I&#x27;m Priya." in to_html(_detail(enhanced=_notes()), include_transcript=True)


def test_export_filename_is_safe() -> None:
    assert export_filename("Q3 pricing: review / v2") == "Q3-pricing-review-v2.md"
    assert export_filename("../../etc/passwd") == "etc-passwd.md"
    assert export_filename('a"b\r\nc') == "a-b-c.md"
    assert export_filename("कंपाइलर डिज़ाइन") == "meeting.md"
    assert len(export_filename("x" * 300)) <= 83


# --- API ------------------------------------------------------------------------------------------------------


def _state(container: Container) -> InMemoryState:
    return cast(InMemoryUnitOfWorkFactory, container.uow).state


def _token(url: str) -> str:
    match = re.fullmatch(r"http://localhost:8000/s/([A-Za-z0-9_-]+)", url)
    assert match, url
    return match.group(1)


def _shared_meeting(c: TestClient, container: Container, **share: object) -> tuple[dict, dict]:
    m = create_meeting(c, ALICE, title="Pricing review")
    _state(container).enhanced[uuid.UUID(m["id"])] = (_notes(), "fake")
    r = c.post(f"/meetings/{m['id']}/share", json=share, headers=auth(ALICE))
    assert r.status_code == 201, r.text
    return m, r.json()


def test_create_share_returns_link_and_public_page_renders() -> None:
    with client() as (c, container):
        m, share = _shared_meeting(c, container)
        assert share["include_transcript"] is False
        assert len(share["token"]) >= 32 and _token(share["url"]) == share["token"]
        expires = datetime.fromisoformat(share["expires_at"])
        assert timedelta(days=29) < expires - datetime.now(UTC) <= timedelta(days=30)

        page = c.get(f"/s/{share['token']}")  # no auth
        assert page.status_code == 200
        assert page.headers["content-type"].startswith("text/html")
        assert page.headers["cache-control"] == "no-store"
        assert "noindex" in page.headers["x-robots-tag"]
        assert "script-src" not in page.headers["content-security-policy"]  # default-src 'none' forbids scripts
        assert "<h1>Pricing review</h1>" in page.text and "✦" in page.text

        assert c.get(f"/meetings/{m['id']}/share", headers=auth(ALICE)).json() == share


def test_share_without_body_defaults_to_no_transcript() -> None:
    with client() as (c, _):
        m = create_meeting(c, ALICE)
        r = c.post(f"/meetings/{m['id']}/share", headers=auth(ALICE))
        assert r.status_code == 201 and r.json()["include_transcript"] is False


def test_transcript_appears_only_when_the_share_includes_it() -> None:
    with client() as (c, container):
        m, share = _shared_meeting(c, container)
        _state(container).transcripts[uuid.UUID(m["id"])] = [
            TranscriptSegment(speaker=0, start_ms=0, end_ms=1000, text="Secret aside")
        ]
        assert "Secret aside" not in c.get(f"/s/{share['token']}").text
        with_transcript = c.post(f"/meetings/{m['id']}/share", json={"include_transcript": True}, headers=auth(ALICE))
        assert with_transcript.json()["include_transcript"] is True
        assert "Secret aside" in c.get(f"/s/{with_transcript.json()['token']}").text


def test_creating_again_rotates_the_link() -> None:
    with client() as (c, container):
        m, first = _shared_meeting(c, container)
        second = c.post(f"/meetings/{m['id']}/share", json={}, headers=auth(ALICE)).json()
        assert second["token"] != first["token"]
        assert c.get(f"/s/{first['token']}").status_code == 404
        assert c.get(f"/s/{second['token']}").status_code == 200
        assert c.get(f"/meetings/{m['id']}/share", headers=auth(ALICE)).json()["token"] == second["token"]
        assert sum(s.revoked_at is None for s in _state(container).shares.values()) == 1


def test_revoke_turns_the_link_off_and_is_idempotent() -> None:
    with client() as (c, container):
        m, share = _shared_meeting(c, container)
        assert c.delete(f"/meetings/{m['id']}/share", headers=auth(ALICE)).status_code == 204
        gone = c.get(f"/s/{share['token']}")
        assert gone.status_code == 404
        assert "This link isn" in gone.text and "noindex" in gone.text
        assert "Pricing review" not in gone.text
        assert c.get(f"/meetings/{m['id']}/share", headers=auth(ALICE)).status_code == 404
        assert c.delete(f"/meetings/{m['id']}/share", headers=auth(ALICE)).status_code == 204


def test_expired_link_is_404() -> None:
    with client() as (c, container):
        m, share = _shared_meeting(c, container)
        shares = _state(container).shares
        for sid, s in list(shares.items()):
            shares[sid] = s.model_copy(update={"expires_at": datetime.now(UTC) - timedelta(seconds=1)})
        assert c.get(f"/s/{share['token']}").status_code == 404
        assert c.get(f"/meetings/{m['id']}/share", headers=auth(ALICE)).status_code == 404


def test_unknown_and_malformed_tokens_are_404_pages() -> None:
    with client() as (c, _):
        for token in ("x" * 43, "short", "%3Cscript%3E"):
            r = c.get(f"/s/{token}")
            assert r.status_code == 404 and r.headers["content-type"].startswith("text/html")
            assert "<script" not in r.text


def test_share_routes_are_owner_only() -> None:
    with client() as (c, container):
        m, _ = _shared_meeting(c, container)
        url = f"/meetings/{m['id']}/share"
        assert c.post(url, json={}, headers=auth(BOB)).status_code == 404
        assert c.get(url, headers=auth(BOB)).status_code == 404
        assert c.delete(url, headers=auth(BOB)).status_code == 404
        assert c.get(f"/meetings/{m['id']}/export", headers=auth(BOB)).status_code == 404
        assert c.post(url, json={}).status_code == 401
        assert c.post(url, json={"include_transcript": True, "extra": 1}, headers=auth(ALICE)).status_code == 422


def test_deleting_the_meeting_kills_the_link() -> None:
    with client() as (c, container):
        m, share = _shared_meeting(c, container)
        assert c.delete(f"/meetings/{m['id']}", headers=auth(ALICE)).status_code == 204
        assert c.get(f"/s/{share['token']}").status_code == 404


def test_public_page_is_rate_limited_per_ip() -> None:
    settings = Settings(env="test", run_worker_in_process=False, rate_limit_share_views_per_hour=2)
    with client(fake_container(settings=settings)) as (c, _):
        token = "y" * 43
        assert [c.get(f"/s/{token}").status_code for _ in range(3)] == [404, 404, 429]


def test_markdown_export_download() -> None:
    with client() as (c, container):
        m = create_meeting(c, ALICE, title="Q3 pricing: review")
        _state(container).enhanced[uuid.UUID(m["id"])] = (_notes(), "fake")
        _state(container).transcripts[uuid.UUID(m["id"])] = [
            TranscriptSegment(speaker=0, start_ms=0, end_ms=1000, text="Hello there")
        ]
        r = c.get(f"/meetings/{m['id']}/export?format=md", headers=auth(ALICE))
        assert r.status_code == 200
        assert r.headers["content-type"] == "text/markdown; charset=utf-8"
        disposition = r.headers["content-disposition"]
        assert disposition.startswith("attachment; filename=\"Q3-pricing-review.md\"; filename*=UTF-8''")
        assert r.text.startswith("# Q3 pricing: review\n")
        assert "✦ Q3 pricing goes up five percent. [01:05]" in r.text
        assert "Hello there" not in r.text

        with_transcript = c.get(f"/meetings/{m['id']}/export?include_transcript=true", headers=auth(ALICE))
        assert "Hello there" in with_transcript.text
        assert c.get(f"/meetings/{m['id']}/export?format=pdf", headers=auth(ALICE)).status_code == 422

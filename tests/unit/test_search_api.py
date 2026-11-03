import asyncio
import uuid

from fastapi.testclient import TestClient

from app.config import Settings
from app.domain.models import EnhancedNotes, Evidence, NoteBlock, Section, TranscriptSegment
from app.services.search import make_snippet
from tests.fakes import InMemoryUnitOfWorkFactory, fake_container
from tests.unit.api_helpers import auth, client, create_meeting

ALICE, BOB = uuid.uuid4(), uuid.uuid4()


async def _seed(uow: InMemoryUnitOfWorkFactory, meeting_id: str) -> None:
    mid = uuid.UUID(meeting_id)
    async with uow() as u:
        await u.transcripts.replace(
            meeting_id=mid,
            segments=[
                TranscriptSegment(speaker=1, start_ms=4000, end_ms=9000, text="An LR(1) parser looks one token ahead"),
                TranscriptSegment(speaker=0, start_ms=9000, end_ms=12000, text="कल का lecture DFA पर था"),
            ],
        )
        notes = EnhancedNotes(
            title="Parsing",
            tldr="LR parsing",
            sections=[
                Section(
                    heading="Parsers",
                    blocks=[
                        NoteBlock(source="user", text="LR(1) vs LALR"),
                        NoteBlock(source="ai", text="LR(1) items", evidence=[Evidence(start_ms=4000, end_ms=9000)]),
                    ],
                )
            ],
        )
        await u.enhanced.save(meeting_id=mid, notes=notes, model="fake")


def _put_notes(c: TestClient, user: uuid.UUID, meeting_id: str, *texts: str) -> None:
    lines = [{"id": str(uuid.uuid4()), "position": i, "text": t, "t_ms": 1000 * i} for i, t in enumerate(texts)]
    assert c.put(f"/meetings/{meeting_id}/notes", json={"lines": lines}, headers=auth(user)).status_code == 200


def test_search_covers_notes_transcripts_and_ai_notes() -> None:
    with client() as (c, container):
        folder = c.post("/folders", json={"name": "Compilers"}, headers=auth(ALICE)).json()
        m = create_meeting(c, ALICE, title="Parsing lecture", folder_id=folder["id"])
        _put_notes(c, ALICE, m["id"], "LR(1) vs LALR")
        bobs = create_meeting(c, BOB)
        _put_notes(c, BOB, bobs["id"], "LR(1) secrets")
        uow = container.uow
        assert isinstance(uow, InMemoryUnitOfWorkFactory)
        asyncio.run(_seed(uow, m["id"]))

        r = c.get("/search", params={"q": "lr(1)"}, headers=auth(ALICE))
        assert r.status_code == 200, r.text
        items = r.json()["items"]
        assert {(i["kind"], i["text"]) for i in items} == {
            ("note", "LR(1) vs LALR"),
            ("transcript", "An LR(1) parser looks one token ahead"),
            ("ai_note", "LR(1) items"),
        }
        by_kind = {i["kind"]: i for i in items}
        assert by_kind["note"] == {
            "kind": "note",
            "meeting_id": m["id"],
            "meeting_title": "Parsing lecture",
            "folder_id": folder["id"],
            "text": "LR(1) vs LALR",
            "start_ms": 0,
            "end_ms": None,
            "speaker": None,
            "created_at": m["created_at"],
        }
        assert (by_kind["transcript"]["start_ms"], by_kind["transcript"]["speaker"]) == (4000, 1)
        assert (by_kind["ai_note"]["start_ms"], by_kind["ai_note"]["end_ms"]) == (4000, 9000)

        hindi = c.get("/search", params={"q": "कल का"}, headers=auth(ALICE)).json()["items"]
        assert [i["kind"] for i in hindi] == ["transcript"]
        assert len(c.get("/search", params={"q": "lr(1)", "limit": 1}, headers=auth(ALICE)).json()["items"]) == 1


def test_search_scopes_to_a_folder_and_checks_its_owner() -> None:
    with client() as (c, _):
        folder = c.post("/folders", json={"name": "Compilers"}, headers=auth(ALICE)).json()
        inside = create_meeting(c, ALICE, folder_id=folder["id"])
        outside = create_meeting(c, ALICE)
        _put_notes(c, ALICE, inside["id"], "DFA in folder")
        _put_notes(c, ALICE, outside["id"], "DFA outside")

        everywhere = c.get("/search", params={"q": "dfa"}, headers=auth(ALICE)).json()["items"]
        assert {i["text"] for i in everywhere} == {"DFA in folder", "DFA outside"}
        scoped = c.get("/search", params={"q": "dfa", "folder_id": folder["id"]}, headers=auth(ALICE)).json()
        assert [i["text"] for i in scoped["items"]] == ["DFA in folder"]
        assert c.get("/search", params={"q": "dfa", "folder_id": folder["id"]}, headers=auth(BOB)).status_code == 404
        assert c.get("/search", params={"q": "dfa"}, headers=auth(BOB)).json()["items"] == []


def test_search_validates_query_and_requires_auth() -> None:
    with client() as (c, _):
        assert c.get("/search", params={"q": "dfa"}).status_code == 401
        assert c.get("/search", headers=auth(ALICE)).status_code == 422
        assert c.get("/search", params={"q": ""}, headers=auth(ALICE)).status_code == 422
        assert c.get("/search", params={"q": "   "}, headers=auth(ALICE)).status_code == 422
        assert c.get("/search", params={"q": "x" * 201}, headers=auth(ALICE)).status_code == 422
        assert c.get("/search", params={"q": "x", "limit": 51}, headers=auth(ALICE)).status_code == 422


def test_search_is_rate_limited() -> None:
    settings = Settings(env="test", run_worker_in_process=False, rate_limit_search_per_hour=2)
    with client(fake_container(settings=settings)) as (c, _):
        for _ in range(2):
            assert c.get("/search", params={"q": "dfa"}, headers=auth(ALICE)).status_code == 200
        r = c.get("/search", params={"q": "dfa"}, headers=auth(ALICE))
        assert r.status_code == 429 and "retry-after" in r.headers


def test_snippet_centres_on_the_match() -> None:
    assert make_snippet("short  text", "text") == "short text"
    long = "intro " * 60 + "the LR(1) parser " + "outro " * 60
    snippet = make_snippet(long, "lr(1)")
    assert "LR(1)" in snippet and snippet.startswith("…") and snippet.endswith("…")
    assert len(snippet) <= 202
    assert make_snippet("word " * 100, "absent").endswith("…")

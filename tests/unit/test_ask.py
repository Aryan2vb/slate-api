import uuid
from uuid import UUID

import pytest

from app.config import Settings
from app.domain.errors import InvalidInputError, NotFoundError
from app.domain.models import EnhancedNotes, Evidence, NoteBlock, Section, TemplateKind, TranscriptSegment
from app.services.ask import AskModelAnswer, AskService, extract_terms
from app.services.enhancement import estimate_tokens
from app.services.prompts import ASK_SYSTEM
from tests.fakes import FakeLlm, InMemoryUnitOfWorkFactory, fake_container
from tests.unit.api_helpers import auth, client

ALICE, BOB = uuid.uuid4(), uuid.uuid4()


class Seeded:
    def __init__(self) -> None:
        self.uow = InMemoryUnitOfWorkFactory()
        self.ids: dict[str, UUID] = {}
        self.folder_id: UUID | None = None


async def _seed() -> Seeded:
    s = Seeded()
    async with s.uow() as u:
        folder = await u.folders.create(user_id=ALICE, name="Compilers", icon=None)
        s.folder_id = folder.id

        async def meeting(title: str, folder_id: UUID | None, user: UUID = ALICE) -> UUID:
            m = await u.meetings.create(
                user_id=user,
                title=title,
                template=TemplateKind.LECTURE,
                calendar_event_id=None,
                attendees=[],
                folder_id=folder_id,
            )
            s.ids[title] = m.id
            return m.id

        parsing = await meeting("Parsing", folder.id)
        await u.transcripts.replace(
            meeting_id=parsing,
            segments=[
                TranscriptSegment(
                    speaker=0, start_ms=60_000, end_ms=65_000, text="An LR(1) parser looks one token ahead"
                ),
                TranscriptSegment(speaker=0, start_ms=70_000, end_ms=75_000, text="The DFA recognises viable prefixes"),
            ],
        )
        await u.enhanced.save(
            meeting_id=parsing,
            notes=EnhancedNotes(
                title="Parsing",
                tldr="Bottom-up parsing with LR items.",
                sections=[
                    Section(
                        heading="Items",
                        blocks=[
                            NoteBlock(
                                source="ai",
                                text="Closure of items",
                                evidence=[Evidence(start_ms=80_000, end_ms=81_000)],
                            )
                        ],
                    )
                ],
            ),
            model="fake",
        )
        lexing = await meeting("Lexing", folder.id)
        await u.transcripts.replace(
            meeting_id=lexing,
            segments=[
                TranscriptSegment(speaker=0, start_ms=5_000, end_ms=9_000, text="Every regex becomes an NFA then a DFA")
            ],
        )
        loose = await meeting("Standup", None)
        await u.transcripts.replace(
            meeting_id=loose,
            segments=[TranscriptSegment(speaker=1, start_ms=1_000, end_ms=2_000, text="DFA dashboard ships Friday")],
        )
        bobs = await meeting("Bob secret", None, user=BOB)
        await u.transcripts.replace(
            meeting_id=bobs,
            segments=[TranscriptSegment(speaker=0, start_ms=0, end_ms=1_000, text="Bob's DFA secret")],
        )
    return s


def _answer(citations: list[int], followups: list[str] | None = None) -> AskModelAnswer:
    return AskModelAnswer(answer="A DFA is a finite automaton.", citations=citations, followups=followups or [])


def _excerpt_lines(prompt: str) -> list[str]:
    body = prompt.split("<excerpts>\n", 1)[1].split("\n</excerpts>", 1)[0]
    return [line for line in body.splitlines() if line]


def _service(s: Seeded, llm: FakeLlm, budget: int = 6000) -> AskService:
    return AskService(s.uow, llm, max_input_tokens=budget)


# --- Keyword extraction ----------------------------------------------------------------------------------------


def test_extract_terms_drops_stopwords_and_keeps_jargon() -> None:
    assert extract_terms("What is an LR(1) parser?") == ["LR(1)", "parser"]
    assert extract_terms("DFA kya hota hai?") == ["DFA"]
    assert extract_terms("sir ne (NFA) ke baare mein kya bataya tha") == ["NFA"]
    assert extract_terms("कल के lecture में DFA क्या था?") == ["DFA"]
    assert extract_terms("ऑटोमेटा थ्योरी में DFA") == ["ऑटोमेटा", "थ्योरी", "DFA"]
    assert extract_terms("what is the?") == []


def test_extract_terms_ranks_jargon_first_then_length_and_dedupes() -> None:
    terms = extract_terms("compare dfa DFA with C++ templates and FIRST/FOLLOW sets in yacc grammars", limit=3)
    assert terms == ["dfa", "C++", "FIRST/FOLLOW"]


# --- Service ---------------------------------------------------------------------------------------------------


async def test_all_scope_searches_every_meeting_of_the_user_only() -> None:
    s = await _seed()
    llm = FakeLlm([_answer([1])])

    await _service(s, llm).ask(user_id=ALICE, question="What is a DFA?")

    system, prompt = llm.prompts[0]
    assert system == ASK_SYSTEM
    lines = _excerpt_lines(prompt)
    assert len(lines) == 3 and all(line.startswith(f"[{i}] ") for i, line in enumerate(lines, 1))
    joined = "\n".join(lines)
    assert "Parsing" in joined and "Lexing" in joined and "Standup" in joined
    assert "Bob" not in prompt
    assert "What is a DFA?" in prompt and "all of the user's meetings" in prompt


async def test_folder_scope_keeps_one_course_and_checks_its_owner() -> None:
    s = await _seed()
    assert s.folder_id is not None
    llm = FakeLlm([_answer([])])

    await _service(s, llm).ask(user_id=ALICE, question="DFA?", folder_id=s.folder_id)

    joined = "\n".join(_excerpt_lines(llm.prompts[0][1]))
    assert "Standup" not in joined and "Lexing" in joined and "Parsing" in joined
    assert '"Compilers"' in llm.prompts[0][1]
    with pytest.raises(NotFoundError):
        await _service(s, FakeLlm()).ask(user_id=BOB, question="DFA?", folder_id=s.folder_id)


async def test_meeting_scope_adds_its_enhanced_notes() -> None:
    s = await _seed()
    llm = FakeLlm([_answer([1, 2])])
    parsing = s.ids["Parsing"]

    result = await _service(s, llm).ask(user_id=ALICE, question="What did we cover?", meeting_id=parsing)

    lines = _excerpt_lines(llm.prompts[0][1])
    assert "Lexing" not in "\n".join(lines)
    assert "summary): Bottom-up parsing with LR items." in lines[0]
    assert any("Items: Closure of items" in line and "at 01:20" in line for line in lines)
    assert [c.meeting_id for c in result.citations] == [parsing, parsing]
    with pytest.raises(NotFoundError):
        await _service(s, FakeLlm()).ask(user_id=BOB, question="DFA?", meeting_id=parsing)
    with pytest.raises(InvalidInputError):
        await _service(s, FakeLlm()).ask(
            user_id=ALICE, question="DFA?", meeting_id=s.ids["Standup"], folder_id=s.folder_id
        )


async def test_citations_map_indexes_back_and_drop_invalid_ones() -> None:
    s = await _seed()
    llm = FakeLlm([_answer([2, 0, 99, -1, 2, 1], followups=[" Why? ", "", "Why?", "a", "b", "c"])])

    result = await _service(s, llm).ask(user_id=ALICE, question="LR(1) parser")

    lines = _excerpt_lines(llm.prompts[0][1])
    assert len(lines) == 1  # only the Parsing transcript line matches either term
    # [2] is out of range for one excerpt; [1] maps to the one excerpt; repeats, 0, negatives and 99 dropped.
    assert len(result.citations) == 1
    c = result.citations[0]
    assert (c.meeting_id, c.meeting_title, c.start_ms) == (s.ids["Parsing"], "Parsing", 60_000)
    assert c.text == "An LR(1) parser looks one token ahead"
    assert result.followups == ["Why?", "a", "b"]


async def test_no_hits_answers_honestly_without_calling_the_llm() -> None:
    s = await _seed()
    llm = FakeLlm()  # any call would fail: no responses queued

    english = await _service(s, llm).ask(user_id=ALICE, question="What about quantum chromodynamics?")
    hindi = await _service(s, llm).ask(user_id=ALICE, question="क्वांटम क्या है?")
    hinglish = await _service(s, llm).ask(user_id=ALICE, question="quantum kya hota hai")
    only_stopwords = await _service(s, llm).ask(user_id=BOB, question="kya hua?")

    assert english.answer == "I couldn't find that in your notes." and english.citations == []
    assert hindi.answer == "मुझे यह आपके नोट्स में नहीं मिला।"
    assert hinglish.answer == "Mujhe ye aapke notes mein nahi mila."
    assert only_stopwords.citations == [] and only_stopwords.followups == []
    assert llm.prompts == []


async def test_prompt_is_trimmed_to_the_input_budget() -> None:
    s = await _seed()
    async with s.uow() as u:
        m = await u.meetings.create(
            user_id=ALICE, title="Long", template=TemplateKind.LECTURE, calendar_event_id=None, attendees=[]
        )
        await u.transcripts.replace(
            meeting_id=m.id,
            segments=[
                TranscriptSegment(
                    speaker=0, start_ms=i * 1000, end_ms=i * 1000 + 900, text=f"DFA point {i} " + "x" * 250
                )
                for i in range(40)
            ],
        )
    budget = 900
    llm = FakeLlm([_answer([1])])

    await _service(s, llm, budget=budget).ask(user_id=ALICE, question="DFA")

    system, prompt = llm.prompts[0]
    assert estimate_tokens(system + prompt) <= budget
    lines = _excerpt_lines(prompt)
    assert 0 < len(lines) < 30
    assert lines[-1].startswith(f"[{len(lines)}] ")


async def test_question_is_validated() -> None:
    s = await _seed()
    with pytest.raises(InvalidInputError):
        await _service(s, FakeLlm()).ask(user_id=ALICE, question="   ")
    with pytest.raises(InvalidInputError):
        await _service(s, FakeLlm()).ask(user_id=ALICE, question="x" * 501)


# --- HTTP ------------------------------------------------------------------------------------------------------


def test_ask_endpoint_returns_answer_citations_and_followups() -> None:
    llm = FakeLlm([_answer([1], followups=["What is an NFA?"])])
    with client(fake_container(llm=llm)) as (c, _):
        m = c.post("/meetings", json={"title": "Automata"}, headers=auth(ALICE)).json()
        lines = [{"id": str(uuid.uuid4()), "position": 0, "text": "DFA = finite automaton", "t_ms": 3000}]
        assert c.put(f"/meetings/{m['id']}/notes", json={"lines": lines}, headers=auth(ALICE)).status_code == 200

        r = c.post("/ask", json={"question": "DFA kya hai?"}, headers=auth(ALICE))

        assert r.status_code == 200, r.text
        assert r.json() == {
            "answer": "A DFA is a finite automaton.",
            "citations": [
                {"meeting_id": m["id"], "meeting_title": "Automata", "start_ms": 3000, "text": "DFA = finite automaton"}
            ],
            "followups": ["What is an NFA?"],
        }
        assert c.post("/ask", json={"question": "DFA?", "meeting_id": m["id"]}, headers=auth(BOB)).status_code == 404


def test_ask_endpoint_validates_body_and_requires_auth() -> None:
    with client() as (c, _):
        assert c.post("/ask", json={"question": "DFA"}).status_code == 401
        assert c.post("/ask", json={"question": ""}, headers=auth(ALICE)).status_code == 422
        assert c.post("/ask", json={"question": "x" * 501}, headers=auth(ALICE)).status_code == 422
        assert c.post("/ask", json={"question": "DFA", "extra": 1}, headers=auth(ALICE)).status_code == 422
        assert c.post("/ask", json={"question": "DFA", "folder_id": "nope"}, headers=auth(ALICE)).status_code == 422


def test_ask_is_rate_limited() -> None:
    settings = Settings(env="test", run_worker_in_process=False, rate_limit_ask_per_hour=2)
    with client(fake_container(settings=settings)) as (c, _):
        for _ in range(2):  # no notes: the no-hit path, so no LLM responses are needed
            assert c.post("/ask", json={"question": "DFA?"}, headers=auth(ALICE)).status_code == 200
        r = c.post("/ask", json={"question": "DFA?"}, headers=auth(ALICE))
        assert r.status_code == 429 and "retry-after" in r.headers
        assert c.post("/ask", json={"question": "DFA?"}, headers=auth(BOB)).status_code == 200


def test_default_ask_limit_is_120_an_hour() -> None:
    assert Settings(env="test").rate_limit_ask_per_hour == 120

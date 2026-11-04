"""Ask Slate: answer a question across the user's meetings from retrieved excerpts, with citations.

Retrieval is deterministic (keyword extraction + the existing search repository); the model only writes the answer
from numbered excerpts and cites them by number. Citations are rebuilt from the excerpts the service sent, so the
model can never invent meeting ids or timestamps.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from uuid import UUID

import structlog
from pydantic import BaseModel, Field

from app.domain.errors import InvalidInputError
from app.domain.models import EnhancedNotes, Meeting, SearchHit
from app.ports.providers import LanguageModel
from app.ports.repositories import UnitOfWorkFactory
from app.services.enhancement import estimate_tokens
from app.services.folders import require_folder
from app.services.meetings import require_meeting
from app.services.prompts import ASK_SYSTEM, ask_prompt, format_excerpt
from app.services.search import make_snippet

log = structlog.get_logger(__name__)

MAX_QUESTION_CHARS = 500
MAX_TERMS = 6
PER_TERM_RESULTS = 20
MAX_EXCERPTS = 30
MAX_MEETING_NOTE_EXCERPTS = 20  # enhanced-note blocks added for a single-meeting question
EXCERPT_CHARS = 300
MAX_FOLLOWUPS = 3
ASK_MAX_TOKENS = 1500

# --- Keyword extraction ----------------------------------------------------------------------------------------


def _words(text: str) -> frozenset[str]:
    return frozenset(text.split())


_ENGLISH_STOPWORDS = _words(
    """
    a about above after again against all also am an and any are aren't as at be because been before being below
    between both but by can can't could did didn't do does doesn't doing don't down during each else ever every
    few for from further get gets got had has hasn't have haven't having he her here hers herself him himself his
    how i i'm if in into is isn't it it's its itself just let's like me more most my myself no nor not now of off
    on once only or other our ours ourselves out over own please same she should so some such than that that's
    the their theirs them themselves then there there's these they this those through to too under until up upon
    us very was wasn't we were weren't what what's when where which while who whom whose why will with would you
    your yours yourself yourselves
    tell explain explained describe say said says mention mentioned mean means meant discuss discussed talk talked
    give show know remember summarize summarise summary recap again anything something thing things
    lecture lectures class classes meeting meetings session sessions note notes today yesterday last week
    """
)

# Romanised Hindi (Hinglish) and Devanagari function words: question words, pronouns, postpositions, auxiliaries.
_HINDI_STOPWORDS = _words(
    """
    kya kyaa kyu kyun kyon kaise kaisa kaisi kab kaun kon kitna kitni kitne kahan kaha kidhar kis kisne kisko
    hai hain h tha thi the ho hota hoti hote hua hui hue raha rahi rahe gaya gayi gaye
    ka ki ke ko se ne me mein mai main par pe tak liye wala wali wale vala vali vale
    aur ya ki lekin magar par phir fir to toh bhi hi na nahi nahin nhi mat haan ha ji
    ye yeh yah wo woh vo vah is us iss uss isme usme iska uska iski uski iske uske inka unka
    mujhe mera meri mere hum hame humein hamara tum tumhe tera aap aapko aapka apna apni apne
    kar karo karna karke kiya kiye ki liya lena diya dena
    bata batao bataya batana bataye bataiye samjhao samjhaya samjhaiye
    matlab kuch sab sabhi ek do baat bare baare sir maam mam bhai yaar
    क्या क्यों क्यूँ कैसे कैसा कैसी कब कौन कितना कितनी कितने कहाँ कहां किस किसने किसको
    है हैं था थी थे हो होता होती होते हुआ हुई हुए रहा रही रहे गया गई गए
    का की के को से में पर तक लिए वाला वाली वाले ने
    और या लेकिन मगर फिर तो भी ही न नहीं मत हाँ जी कि जो
    यह ये वह वो इस उस इसमें उसमें इसका उसका इसकी उसकी इसके उसके
    मुझे मेरा मेरी मेरे हम हमें हमारा तुम तुम्हें आप आपको आपका अपना अपनी अपने
    कर करो करना करके किया किए लिया दिया बता बताओ बताया बताइए समझाओ समझाया समझाइए मतलब कुछ सब एक बात बारे
    आज कल
    """
)
_HINGLISH_MARKERS = _words(
    "kya kyu kyun kaise kab kaun kitna hai hain tha thi ka ki ke mein me batao samjhao matlab nahi"
)

_STOPWORDS = _ENGLISH_STOPWORDS | _HINDI_STOPWORDS
_EDGE_PUNCT = "?!.,;:\"'`\u201c\u201d\u2018\u2019«»…¿¡।|"
_DEVANAGARI = re.compile(r"[ऀ-ॿ]")
_JARGON_HINT = re.compile(r"[A-Z0-9()+#/\-_.]")


def _clean_token(token: str) -> str:
    token = token.strip(_EDGE_PUNCT)
    # A wrapping pair of parentheses is punctuation ("(DFA)"); one inside a term is part of it ("LR(1)").
    while token.startswith("(") and token.endswith(")"):
        token = token[1:-1].strip(_EDGE_PUNCT)
    if token.count("(") > token.count(")"):
        token = token.lstrip("(") if token.startswith("(") else token
    if token.count(")") > token.count("("):
        token = token.rstrip(")")
    return token.strip(_EDGE_PUNCT)


def _is_jargon(token: str) -> bool:
    """Acronyms, terms with digits or symbols ("LR(1)", "DFA", "C++", "FIRST/FOLLOW") beat ordinary words."""
    return bool(_JARGON_HINT.search(token[1:] if token[:1].isupper() else token))


def extract_terms(question: str, limit: int = MAX_TERMS) -> list[str]:
    """A few search terms from a question: stopwords (English, Hinglish, Hindi) dropped, jargon kept intact and
    ranked first, then longer words; in question order within each group. Deterministic and offline."""
    candidates: dict[str, tuple[int, int, int, str]] = {}  # casefolded -> (jargon first, longer first, position, token)
    for position, raw in enumerate(question.split()):
        token = _clean_token(raw)
        key = token.casefold()
        if not any(ch.isalnum() for ch in token) or key in _STOPWORDS:
            continue
        if len(token) < 2 and not _DEVANAGARI.search(token):
            continue
        jargon = 0 if _is_jargon(token) else 1
        if key in candidates:  # a repeat keeps its first spelling, but counts as jargon if any spelling is ("DFA")
            first = candidates[key]
            candidates[key] = (min(first[0], jargon), *first[1:])
            continue
        candidates[key] = (jargon, -len(token), position, token)
    kept = sorted(candidates.values())[:limit]
    return [token for *_, token in sorted(kept, key=lambda c: c[2])]


def _not_found_answer(question: str) -> str:
    if _DEVANAGARI.search(question):
        return "मुझे यह आपके नोट्स में नहीं मिला।"
    words = {w.strip(_EDGE_PUNCT).casefold() for w in question.split()}
    if len(words & _HINGLISH_MARKERS) >= 1 and not words & {"what", "how", "why", "when", "who", "is", "the"}:
        return "Mujhe ye aapke notes mein nahi mila."
    return "I couldn't find that in your notes."


# --- Results ---------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Citation:
    meeting_id: UUID
    meeting_title: str
    start_ms: int | None
    text: str


@dataclass(frozen=True)
class AskAnswer:
    answer: str
    citations: list[Citation] = field(default_factory=list)
    followups: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class Excerpt:
    meeting_id: UUID
    meeting_title: str
    meeting_date: datetime
    kind: str
    start_ms: int | None
    text: str

    def render(self, index: int) -> str:
        return format_excerpt(
            index=index,
            meeting_title=self.meeting_title,
            meeting_date=self.meeting_date.date().isoformat(),
            kind=self.kind,
            start_ms=self.start_ms,
            text=self.text,
        )


class AskModelAnswer(BaseModel):
    """What the model returns: excerpt numbers only, never ids or times."""

    answer: str
    citations: list[int] = Field(default_factory=list)
    followups: list[str] = Field(default_factory=list)


# --- Retrieval -------------------------------------------------------------------------------------------------


def merge_hits(hits_per_term: Sequence[tuple[str, Sequence[SearchHit]]], limit: int = MAX_EXCERPTS) -> list[Excerpt]:
    """Dedupes hits across terms; a hit matched by more terms ranks higher, then by search rank and recency."""
    merged: dict[tuple[UUID, str, int | None, str], tuple[SearchHit, set[str], float]] = {}
    for term, hits in hits_per_term:
        for hit in hits:
            key = (hit.meeting_id, hit.kind.value, hit.start_ms, " ".join(hit.text.split()).casefold())
            first, terms, rank = merged.get(key, (hit, set(), 0.0))
            merged[key] = (first, terms | {term}, rank + hit.rank)
    ordered = sorted(merged.values(), key=lambda v: (len(v[1]), v[2], v[0].created_at), reverse=True)[:limit]
    return [
        Excerpt(
            meeting_id=hit.meeting_id,
            meeting_title=hit.meeting_title,
            meeting_date=hit.created_at,
            kind=hit.kind.value,
            start_ms=hit.start_ms,
            text=make_snippet(hit.text, sorted(terms, key=len, reverse=True)[0], EXCERPT_CHARS),
        )
        for hit, terms, _ in ordered
    ]


def meeting_note_excerpts(meeting: Meeting, notes: EnhancedNotes) -> tuple[list[Excerpt], list[Excerpt]]:
    """The meeting's tldr and its enhanced-note blocks, as excerpts (tldr, blocks)."""

    def excerpt(kind: str, text: str, start_ms: int | None) -> Excerpt:
        return Excerpt(
            meeting_id=meeting.id,
            meeting_title=meeting.title,
            meeting_date=meeting.started_at or meeting.created_at,
            kind=kind,
            start_ms=start_ms,
            text=make_snippet(text, "", EXCERPT_CHARS),
        )

    tldr = [excerpt("summary", notes.tldr, None)] if notes.tldr.strip() else []
    blocks = [
        excerpt(
            "note" if b.source == "user" else "ai_note",
            f"{section.heading}: {b.text}",
            b.evidence[0].start_ms if b.evidence else None,
        )
        for section in notes.sections
        for b in section.blocks
        if b.text.strip()
    ]
    return tldr, blocks[:MAX_MEETING_NOTE_EXCERPTS]


def _dedupe(excerpts: Sequence[Excerpt]) -> list[Excerpt]:
    seen: set[tuple[UUID, str]] = set()
    out: list[Excerpt] = []
    for e in excerpts:
        key = (e.meeting_id, " ".join(e.text.split()).casefold())
        if key not in seen:
            seen.add(key)
            out.append(e)
    return out


# --- Service ---------------------------------------------------------------------------------------------------


class AskService:
    def __init__(self, uow: UnitOfWorkFactory, llm: LanguageModel, *, max_input_tokens: int) -> None:
        self._uow = uow
        self._llm = llm
        self._budget = max_input_tokens

    async def ask(
        self, *, user_id: UUID, question: str, folder_id: UUID | None = None, meeting_id: UUID | None = None
    ) -> AskAnswer:
        question = " ".join(question.split())
        if not question:
            raise InvalidInputError("Question must not be empty")
        if len(question) > MAX_QUESTION_CHARS:
            raise InvalidInputError(f"Question may hold at most {MAX_QUESTION_CHARS} characters")
        terms = extract_terms(question)
        scope_kind = "meeting" if meeting_id else "folder" if folder_id else "all"

        async with self._uow() as uow:
            scope = "all of the user's meetings"
            meeting: Meeting | None = None
            if folder_id is not None:
                folder = await require_folder(uow, user_id=user_id, folder_id=folder_id)
                scope = f'the course folder "{folder.name}"'
            if meeting_id is not None:
                meeting = await require_meeting(uow, user_id=user_id, meeting_id=meeting_id)
                if folder_id is not None and meeting.folder_id != folder_id:
                    raise InvalidInputError("That meeting is not in that folder")
                scope = f'the meeting "{meeting.title}"'
            hits_per_term = [
                (
                    term,
                    await uow.search.search(
                        user_id=user_id, query=term, folder_id=folder_id, limit=PER_TERM_RESULTS, meeting_id=meeting_id
                    ),
                )
                for term in terms
            ]
            enhanced = await uow.enhanced.get(meeting_id=meeting.id) if meeting is not None else None

        excerpts = merge_hits(hits_per_term)
        if meeting is not None and enhanced is not None:
            tldr, blocks = meeting_note_excerpts(meeting, enhanced)
            excerpts = _dedupe([*tldr, *excerpts, *blocks])

        prompt, included = self._fit(question=question, scope=scope, excerpts=excerpts)
        if not included:
            log.info("ask.answered", scope=scope_kind, terms=len(terms), excerpts=0, citations=0, llm_called=False)
            return AskAnswer(answer=_not_found_answer(question))

        result = await self._llm.generate(
            system=ASK_SYSTEM, prompt=prompt, schema=AskModelAnswer, max_tokens=ASK_MAX_TOKENS
        )
        citations = self._citations(result.citations, included)
        followups = list(dict.fromkeys(f.strip() for f in result.followups if f.strip()))[:MAX_FOLLOWUPS]
        log.info(
            "ask.answered",
            scope=scope_kind,
            terms=len(terms),
            excerpts=len(included),
            excerpts_trimmed=len(excerpts) - len(included),
            citations=len(citations),
            citations_dropped=len(result.citations) - len(citations),
            llm_called=True,
        )
        return AskAnswer(answer=result.answer.strip(), citations=citations, followups=followups)

    def _fit(self, *, question: str, scope: str, excerpts: Sequence[Excerpt]) -> tuple[str, list[Excerpt]]:
        """The longest prefix of `excerpts` whose prompt stays within the input budget (estimated tokens)."""
        included: list[Excerpt] = []
        rendered: list[str] = []
        prompt = ask_prompt(question=question, excerpts=rendered, scope=scope)
        for excerpt in excerpts:
            line = excerpt.render(len(included) + 1)
            candidate = ask_prompt(question=question, excerpts=[*rendered, line], scope=scope)
            if estimate_tokens(ASK_SYSTEM + candidate) > self._budget:
                break
            included.append(excerpt)
            rendered.append(line)
            prompt = candidate
        return prompt, included

    @staticmethod
    def _citations(indexes: Sequence[int], included: Sequence[Excerpt]) -> list[Citation]:
        """Maps 1-based excerpt numbers back to their sources; out-of-range and repeated numbers are dropped."""
        out: list[Citation] = []
        seen: set[int] = set()
        for i in indexes:
            if 1 <= i <= len(included) and i not in seen:
                seen.add(i)
                e = included[i - 1]
                out.append(
                    Citation(meeting_id=e.meeting_id, meeting_title=e.meeting_title, start_ms=e.start_ms, text=e.text)
                )
        return out

"""Vocabulary rules shared by transcription (keyterms) and folders (course vocabulary): pure functions, no I/O."""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Iterable, Sequence

from app.domain.errors import InvalidInputError

MAX_VOCABULARY_TERMS = 100
MAX_TERM_CHARS = 100

# A token from the user's notes that looks like jargon: an acronym, mixed case, digits or joining punctuation
# ("eVTOL", "PT-01", "LR(1)", "FIRST/FOLLOW"). Plain lowercase words are too common to be worth boosting.
_NOTE_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9()+#./-]*[A-Za-z0-9)+#]")


def _is_jargon(token: str) -> bool:
    return (
        (any(c.isdigit() for c in token) and any(c.isalpha() for c in token))
        or any(c.isupper() for c in token[1:])
        or any(c in "()+#/-" for c in token)
    )


def jargon_terms(text: str) -> list[str]:
    """The jargon-looking tokens of one line, in order of appearance (duplicates kept)."""
    return [t for t in _NOTE_TOKEN.findall(text) if _is_jargon(t)]


def dedupe_terms(terms: Iterable[str]) -> list[str]:
    """Trims, drops empties and removes case-insensitive duplicates, keeping the first spelling seen."""
    seen: set[str] = set()
    out: list[str] = []
    for raw in terms:
        term = raw.strip()
        key = term.casefold()
        if term and key not in seen:
            seen.add(key)
            out.append(term)
    return out


def normalize_vocabulary(terms: Sequence[str]) -> list[str]:
    """A folder's vocabulary as stored: trimmed, deduped case-insensitively, each term and the list bounded."""
    out = dedupe_terms(" ".join(t.split()) for t in terms)  # internal whitespace collapsed too
    if too_long := next((t for t in out if len(t) > MAX_TERM_CHARS), None):
        raise InvalidInputError(f"Vocabulary terms may hold at most {MAX_TERM_CHARS} characters: {too_long[:20]}...")
    if len(out) > MAX_VOCABULARY_TERMS:
        raise InvalidInputError(f"At most {MAX_VOCABULARY_TERMS} vocabulary terms")
    return out


def suggest_terms(lines: Iterable[str], *, exclude: Iterable[str] = (), limit: int = 20) -> list[tuple[str, int]]:
    """Jargon mined from note lines, most frequent first (ties: first seen), skipping terms in `exclude`.

    Counting is case-insensitive; the spelling returned is the most common one (first seen on a tie).
    """
    excluded = {t.strip().casefold() for t in exclude}
    counts: Counter[str] = Counter()
    spellings: dict[str, Counter[str]] = {}
    for line in lines:
        for token in jargon_terms(line):
            key = token.casefold()
            if key in excluded:
                continue
            counts[key] += 1
            spellings.setdefault(key, Counter())[token] += 1
    # Counter.most_common keeps insertion order among equal counts, so ties resolve to first appearance.
    return [(spellings[key].most_common(1)[0][0], n) for key, n in counts.most_common(limit)]

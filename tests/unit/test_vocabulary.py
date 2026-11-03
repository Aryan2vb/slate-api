import pytest

from app.domain.errors import InvalidInputError
from app.domain.vocabulary import jargon_terms, normalize_vocabulary, suggest_terms


def test_normalize_trims_collapses_and_dedupes_case_insensitively() -> None:
    assert normalize_vocabulary(["  LR(1) ", "lr(1)", "", "   ", "Shift   reduce", "DFA", "dfa"]) == [
        "LR(1)",
        "Shift reduce",
        "DFA",
    ]
    assert normalize_vocabulary([]) == []


def test_normalize_bounds_terms_and_count() -> None:
    assert len(normalize_vocabulary(["x" * 100])[0]) == 100
    with pytest.raises(InvalidInputError):
        normalize_vocabulary(["x" * 101])
    assert len(normalize_vocabulary([f"t{i}" for i in range(100)])) == 100
    with pytest.raises(InvalidInputError):
        normalize_vocabulary([f"t{i}" for i in range(101)])


def test_jargon_terms_keep_acronyms_mixed_case_and_punctuated_tokens() -> None:
    assert jargon_terms("LR(1) vs LALR, see PT-01 and plain words, eVTOL") == ["LR(1)", "LALR", "PT-01", "eVTOL"]


def test_suggest_terms_ranks_by_frequency_and_excludes_known() -> None:
    lines = ["LR(1) and LALR", "LALR, lalr, DFA", "LALR DFA", "eVTOL"]
    assert suggest_terms(lines, exclude=[" dfa "]) == [("LALR", 3), ("LR(1)", 1), ("eVTOL", 1)]
    assert suggest_terms(lines, limit=1) == [("LALR", 3)]
    # The most common spelling wins.
    assert suggest_terms(["GraphQL", "GRAPHQL", "GRAPHQL"]) == [("GRAPHQL", 3)]

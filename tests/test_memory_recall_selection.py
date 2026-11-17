"""Tests for choosing which recall hits to inject."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.memory._prompting import select_recall_hits
from mindroom.token_counting.families import load_o200k

if TYPE_CHECKING:
    from mindroom.memory._shared import MemoryResult


def _hit(memory: str, score: float | None = None) -> MemoryResult:
    result: MemoryResult = {"memory": memory}
    if score is not None:
        result["score"] = score
    return result


def _text_of_tokens(tokens: int) -> str:
    text = " hello" * tokens
    assert len(load_o200k().encode_ordinary(text)) == tokens
    return text


def test_selection_drops_hits_beyond_gap() -> None:
    """Hits within the score gap of the top hit are kept; the first hit past it ends the selection."""
    hits = [_hit("a", 0.80), _hit("b", 0.78), _hit("c", 0.60), _hit("d", 0.79)]

    assert select_recall_hits(hits) == hits[:2]


def test_selection_keeps_a_hit_exactly_at_the_gap() -> None:
    """A hit whose distance equals the gap is kept despite float rounding (0.80 - 0.75 > 0.05 in floats)."""
    hits = [_hit("a", 0.80), _hit("b", 0.75)]

    assert select_recall_hits(hits) == hits


def test_selection_stops_at_max_hits() -> None:
    """No more than `max_hits` hits are kept."""
    hits = [_hit(str(index), 0.9) for index in range(8)]

    assert select_recall_hits(hits) == hits[:5]
    assert select_recall_hits(hits, max_hits=2) == hits[:2]


def test_selection_respects_token_budget() -> None:
    """The hit that would push the total past the token budget ends the selection."""
    hits = [_hit(_text_of_tokens(600), 0.9), _hit(_text_of_tokens(600), 0.9)]

    assert select_recall_hits(hits) == hits[:1]


def test_selection_keeps_nothing_when_the_first_hit_exceeds_the_budget() -> None:
    """A top hit bigger than the whole budget is not injected at all."""
    hits = [_hit(_text_of_tokens(801), 0.9), _hit("small", 0.9)]

    assert select_recall_hits(hits) == []


def test_selection_treats_a_missing_score_as_zero() -> None:
    """Results without a score count as score 0."""
    unscored = [_hit("a"), _hit("b")]

    assert select_recall_hits(unscored) == unscored
    assert select_recall_hits([_hit("a", 0.9), _hit("b")]) == [_hit("a", 0.9)]


def test_selection_empty_input_returns_empty() -> None:
    """No results select nothing."""
    assert select_recall_hits([]) == []

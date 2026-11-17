"""Tests for choosing which recall hits to inject."""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING

import tiktoken

from mindroom.memory._prompting import select_recall_hits
from mindroom.token_counting import families
from mindroom.token_counting.families import load_o200k

if TYPE_CHECKING:
    import pytest

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
    assert select_recall_hits(hits, score_gap=0.25) == hits


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


def _unloaded_encoder(monkeypatch: pytest.MonkeyPatch, get_encoding: object) -> None:
    """Forget every loaded encoder and load o200k_base through ``get_encoding`` instead of tiktoken's."""
    monkeypatch.setattr(families, "_encoders", {})
    monkeypatch.setattr(tiktoken, "get_encoding", get_encoding)


# A typical ~1,000-character entry is 250 estimated tokens; with the 2,000-character one 750 fit, and the 400-character
# one would make 850.
_ESTIMATED_HITS = (("a" * 1_000, 0.9), ("b" * 2_000, 0.9), ("c" * 400, 0.9))


def test_selection_estimates_tokens_when_the_encoder_fails_to_load(monkeypatch: pytest.MonkeyPatch) -> None:
    """A tokenizer that cannot load never fails recall: the budget estimates a quarter token per character."""

    def failing_get_encoding(name: str) -> tiktoken.Encoding:
        msg = f"cannot download {name}"
        raise OSError(msg)

    _unloaded_encoder(monkeypatch, failing_get_encoding)
    hits = [_hit(memory, score) for memory, score in _ESTIMATED_HITS]

    assert select_recall_hits(hits) == hits[:2]


def test_selection_does_not_wait_for_a_stalled_encoder(monkeypatch: pytest.MonkeyPatch) -> None:
    """A tokenizer download that hangs does not hold up recall, which estimates tokens until the encoder is ready."""
    release = threading.Event()

    def stalled_get_encoding(name: str) -> tiktoken.Encoding:
        release.wait(30)
        msg = f"{name} never arrived"
        raise OSError(msg)

    _unloaded_encoder(monkeypatch, stalled_get_encoding)
    hits = [_hit(memory, score) for memory, score in _ESTIMATED_HITS]
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            started = time.monotonic()
            selected = pool.submit(select_recall_hits, hits).result(timeout=5)
        assert time.monotonic() - started < 2
        assert selected == hits[:2]
    finally:
        release.set()

"""Tests for the shared retrieval stage: reranking candidates and choosing which to keep."""

from __future__ import annotations

import asyncio
import math
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from typing import TYPE_CHECKING

import pytest
import tiktoken

from mindroom.knowledge.retrieval import Candidate, Selection, retrieve
from mindroom.memory._file_backend import _RECALL_SELECTION, _retrieve_memories
from mindroom.token_counting import families
from mindroom.token_counting.families import load_o200k

if TYPE_CHECKING:
    from collections.abc import Sequence

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


def _select_recall_hits(
    hits: Sequence[MemoryResult],
    *,
    max_hits: int = _RECALL_SELECTION.limit,
    score_gap: float | None = _RECALL_SELECTION.hybrid_score_gap,
) -> list[MemoryResult]:
    """Select ``hits`` as the per-turn recall does, through ``retrieve`` with ``_RECALL_SELECTION``."""
    selection = replace(_RECALL_SELECTION, limit=max_hits, hybrid_score_gap=score_gap)
    return asyncio.run(_retrieve_memories("query", hits, selection))


def test_selection_drops_hits_beyond_gap() -> None:
    """Hits within the score gap of the top hit are kept; the first hit past it ends the selection."""
    hits = [_hit("a", 0.80), _hit("b", 0.78), _hit("c", 0.60), _hit("d", 0.79)]

    assert _select_recall_hits(hits) == hits[:2]
    assert _select_recall_hits(hits, score_gap=0.25) == hits


def test_selection_keeps_a_hit_exactly_at_the_gap() -> None:
    """A hit whose distance equals the gap is kept despite float rounding (0.80 - 0.75 > 0.05 in floats)."""
    hits = [_hit("a", 0.80), _hit("b", 0.75)]

    assert _select_recall_hits(hits) == hits


def test_selection_stops_at_max_hits() -> None:
    """No more than `max_hits` hits are kept."""
    hits = [_hit(str(index), 0.9) for index in range(8)]

    assert _select_recall_hits(hits) == hits[:5]
    assert _select_recall_hits(hits, max_hits=2) == hits[:2]


def test_selection_respects_token_budget() -> None:
    """The hit that would push the total past the token budget ends the selection."""
    hits = [_hit(_text_of_tokens(600), 0.9), _hit(_text_of_tokens(600), 0.9)]

    assert _select_recall_hits(hits) == hits[:1]


def test_selection_keeps_nothing_when_the_first_hit_exceeds_the_budget() -> None:
    """A top hit bigger than the whole budget is not injected at all."""
    hits = [_hit(_text_of_tokens(801), 0.9), _hit("small", 0.9)]

    assert _select_recall_hits(hits) == []


def test_selection_treats_a_missing_score_as_zero() -> None:
    """Results without a score count as score 0."""
    unscored = [_hit("a"), _hit("b")]

    assert _select_recall_hits(unscored) == unscored
    assert _select_recall_hits([_hit("a", 0.9), _hit("b")]) == [_hit("a", 0.9)]


def test_selection_empty_input_returns_empty() -> None:
    """No results select nothing."""
    assert _select_recall_hits([]) == []


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

    assert _select_recall_hits(hits) == hits[:2]


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
            selected = pool.submit(_select_recall_hits, hits).result(timeout=5)
        assert time.monotonic() - started < 2
        assert selected == hits[:2]
    finally:
        release.set()


class _Reranker:
    """Returns fixed ``scores``, after ``delay`` seconds; ``failing`` raises instead."""

    def __init__(self, scores: Sequence[float] = (), *, delay: float = 0.0, failing: bool = False) -> None:
        self.scores = list(scores)
        self.delay = delay
        self.failing = failing
        self.calls: list[tuple[str, list[str]]] = []

    async def ascore(self, query: str, documents: Sequence[str]) -> list[float]:
        self.calls.append((query, list(documents)))
        await asyncio.sleep(self.delay)
        if self.failing:
            msg = "rerank service down"
            raise RuntimeError(msg)
        return self.scores


# Hybrid scores whose 0.05 gap keeps the first two.
_GAP_CANDIDATES = (Candidate("a", "text a", 0.80), Candidate("b", "text b", 0.78), Candidate("c", "text c", 0.60))
_GAP_SELECTION = Selection(limit=5, hybrid_score_gap=0.05)


def _items(candidates: Sequence[Candidate[str]]) -> list[str]:
    return [candidate.item for candidate in candidates]


@pytest.mark.asyncio
async def test_reranker_orders_and_limits() -> None:
    """The reranker reads each candidate's text; its scores order the candidates, and the selection limits them."""
    reranker = _Reranker([0.1, 0.9, 0.5])

    kept = await retrieve("query", _GAP_CANDIDATES, Selection(limit=2), reranker=reranker)

    assert kept == [Candidate("b", "text b", 0.9), Candidate("c", "text c", 0.5)]
    assert reranker.calls == [("query", ["text a", "text b", "text c"])]


@pytest.mark.asyncio
async def test_failing_reranker_keeps_hybrid_selection() -> None:
    """A reranker that raises leaves the hybrid order and the hybrid score gap in charge."""
    kept = await retrieve("query", _GAP_CANDIDATES, _GAP_SELECTION, reranker=_Reranker(failing=True))

    assert kept == list(_GAP_CANDIDATES[:2])


@pytest.mark.asyncio
async def test_deadline_keeps_hybrid_selection() -> None:
    """A reranker slower than the deadline is abandoned for the hybrid selection, without waiting for it."""
    started = time.monotonic()

    kept = await retrieve(
        "query",
        _GAP_CANDIDATES,
        _GAP_SELECTION,
        reranker=_Reranker([0.1, 0.9, 0.5], delay=1.0),
        deadline_seconds=0.01,
    )

    assert time.monotonic() - started < 0.5
    assert kept == list(_GAP_CANDIDATES[:2])


@pytest.mark.asyncio
@pytest.mark.parametrize("scores", [[0.9, 0.5], [0.1, 0.9, 0.5, 0.3], [0.1, math.nan, 0.5], [0.1, math.inf, 0.5]])
async def test_wrong_count_or_non_finite_score_is_a_failure(scores: list[float]) -> None:
    """A wrong count, NaN or +inf is not trusted: the hybrid selection is kept."""
    kept = await retrieve("query", _GAP_CANDIDATES, _GAP_SELECTION, reranker=_Reranker(scores))

    assert kept == list(_GAP_CANDIDATES[:2])


@pytest.mark.asyncio
async def test_min_rerank_score_selects_nothing_below_threshold() -> None:
    """Reranked candidates are kept down to the threshold, however far apart; below it nothing is kept."""
    selection = replace(_GAP_SELECTION, min_rerank_score=0.8)

    kept = await retrieve("query", _GAP_CANDIDATES, selection, reranker=_Reranker([0.81, 0.5, 0.95]))
    assert _items(kept) == ["c", "a"]

    assert await retrieve("query", _GAP_CANDIDATES, selection, reranker=_Reranker([0.79, 0.5, 0.1])) == []


@pytest.mark.asyncio
async def test_unjudged_candidate_ranks_last_and_never_passes_the_threshold() -> None:
    """``-inf`` marks a candidate the reranker could not judge: the others are reranked and it comes last."""
    reranker = _Reranker([-math.inf, 0.9, -2.0])

    kept = await retrieve("query", _GAP_CANDIDATES, Selection(limit=3), reranker=reranker)
    assert _items(kept) == ["b", "c", "a"]

    threshold = Selection(limit=3, min_rerank_score=-math.inf)
    assert _items(await retrieve("query", _GAP_CANDIDATES, threshold, reranker=reranker)) == ["b", "c"]

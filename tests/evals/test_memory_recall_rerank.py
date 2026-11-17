"""Rerank arms of the memory-recall benchmark: the score cache, the A5 selection rule, the threshold sweep and the split."""

from __future__ import annotations

import json
import shutil
from dataclasses import replace
from typing import TYPE_CHECKING

import pytest

from evals.memory_recall import judge, rerank_arms, run
from evals.memory_recall.rerank_arms import select_reranked
from evals.memory_recall.rerank_cache import CachedReranker, RerankCache
from evals.memory_recall.run import split_turns
from evals.memory_recall.turns import write_turns
from mindroom.memory._prompting import select_recall_hits
from mindroom.memory._shared import MemoryResult
from tests.evals.test_memory_recall_run import (
    FIXTURE_MEMORY,
    _CountingEmbedder,
    _judge_transport,
    _turn,
    _write_config,
)
from tests.knowledge_test_support import install_knowledge_embedder

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path


class _StubReranker:
    """Scores a document 0.9 when it names Pixel, 0.6 when it names the shed, else 0.1; records every request."""

    def __init__(self) -> None:
        self.requests: list[tuple[str, list[str]]] = []

    async def ascore(self, query: str, documents: Sequence[str]) -> list[float]:
        self.requests.append((query, list(documents)))
        return [0.9 if "Pixel" in text else 0.6 if "shed" in text else 0.1 for text in documents]


def _cached(stub: _StubReranker, cache: RerankCache, *, model: str = "stub-model") -> CachedReranker:
    return CachedReranker(stub, cache, provider="stub", model=model, rubric_version="v1")


@pytest.mark.asyncio
async def test_cached_reranker_skips_second_request(tmp_path: Path) -> None:
    """A second request for scored texts reaches no provider; only new texts are sent, and the latency is kept."""
    stub = _StubReranker()
    documents = ["Pixel eats salmon", "the shed is green"]
    with RerankCache(tmp_path / "rerank.sqlite") as cache:
        reranker = _cached(stub, cache)
        first = await reranker.ascore("cat food", documents)
        again = await reranker.ascore("cat food", documents)
        assert first == again == [0.9, 0.6]
        assert len(stub.requests) == 1
        latency = reranker.latency("cat food", documents)
        assert latency is not None
        assert latency >= 0.0

        await reranker.ascore("cat food", [*documents, "a tomato bed"])
        assert stub.requests[-1] == ("cat food", ["a tomato bed"])
        # Only a call that scored every text of its request measures that request's latency.
        assert reranker.latency("cat food", [*documents, "a tomato bed"]) is None

        # Another model is another key, so it is scored afresh.
        await _cached(stub, cache, model="other-model").ascore("cat food", documents)
        assert len(stub.requests) == 3

    # The scores outlive the process.
    with RerankCache(tmp_path / "rerank.sqlite") as cache:
        assert await _cached(stub, cache).ascore("cat food", documents) == [0.9, 0.6]
    assert len(stub.requests) == 3


@pytest.mark.asyncio
async def test_cached_reranker_rejects_bad_scores(tmp_path: Path) -> None:
    """A wrong count or a non-finite score raises and caches nothing, so the turn falls back and is retried later."""

    class _Bad:
        def __init__(self, scores: list[float]) -> None:
            self.scores = scores

        async def ascore(self, query: str, documents: Sequence[str]) -> list[float]:
            _ = query, documents
            return self.scores

    with RerankCache(tmp_path / "rerank.sqlite") as cache:
        for scores in ([0.5], [0.5, float("nan")]):
            reranker = CachedReranker(_Bad(scores), cache, provider="stub", model="m", rubric_version="v1")
            with pytest.raises(ValueError, match="score"):
                await reranker.ascore("q", ["a", "b"])
        stub = _StubReranker()
        assert await _cached(stub, cache, model="m").ascore("q", ["a", "b"]) == [0.1, 0.1]
        assert len(stub.requests) == 1


def _candidates(count: int, *, text: str = "note") -> list[MemoryResult]:
    return [
        MemoryResult(id=f"c{index}", memory=f"{text} {index}", user_id="u", score=1.0 - index / 100)
        for index in range(count)
    ]


def test_a5_selection_ranks_by_reranker_score_and_keeps_hits_at_or_above_threshold() -> None:
    """Reranked hits at or above the threshold are kept best first (ties in hybrid order), at most 5 of them."""
    candidates = _candidates(8)
    scores = [0.2, 0.9, 0.95, 0.85, 0.8, 0.83, 0.83, 0.79]

    selected = select_reranked(candidates, scores, threshold=0.8)

    assert [hit["id"] for hit in selected] == ["c2", "c1", "c3", "c5", "c6"]
    assert [hit["score"] for hit in selected] == [0.95, 0.9, 0.85, 0.83, 0.83]
    assert select_reranked(candidates, scores, threshold=0.96) == []


def test_a5_selection_keeps_the_recall_token_budget() -> None:
    """The kept texts fit the shipped recall's 800 tokens: a best hit over the budget selects nothing."""
    candidates = _candidates(3)
    candidates[1] = MemoryResult(id="big", memory="word " * 900, user_id="u", score=0.5)

    assert select_reranked(candidates, [0.5, 0.9, 0.7], threshold=0.3) == []
    assert [hit["id"] for hit in select_reranked(candidates, [0.9, 0.5, 0.7], threshold=0.3)] == ["c0", "c2"]


def test_a5_selection_without_scores_is_the_shipped_selection() -> None:
    """A turn whose reranker failed gets exactly the shipped hybrid selection."""
    candidates = _candidates(8)
    assert select_reranked(candidates, None, threshold=0.8) == select_recall_hits(candidates)


def test_split_is_seeded_and_disjoint() -> None:
    """The tuning and held-out halves cover every turn once, and the same seed always splits the same way."""
    tuning, held_out = split_turns(11, seed=7)

    assert not set(tuning) & set(held_out)
    assert sorted([*tuning, *held_out]) == list(range(11))
    assert len(tuning) == 5
    assert split_turns(11, seed=7) == (tuning, held_out)
    assert split_turns(11, seed=8) != (tuning, held_out)


@pytest.mark.asyncio
async def test_threshold_sweep_reuses_scores(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Every threshold reads one reranker request per turn, and a rerun reads them all from the cache."""
    install_knowledge_embedder(monkeypatch, lambda *_args, **_kwargs: _CountingEmbedder())
    monkeypatch.setattr(judge, "_RETRY_DELAY_S", 0.0)
    monkeypatch.setenv("TEST_JUDGE_KEY", "sk-test")
    stub = _StubReranker()
    spec = rerank_arms.RERANKERS["bge-reranker-v2-m3"]
    monkeypatch.setitem(rerank_arms.RERANKERS, "bge-reranker-v2-m3", replace(spec, create=lambda _paths: stub))
    memory_dir = shutil.copytree(FIXTURE_MEMORY, tmp_path / "workspace" / "memory")
    turns = [_turn("2026-01-15", "salmon kibble Pixel"), _turn("2026-01-15", "garden shed colour")]
    write_turns(turns, tmp_path / "turns.jsonl")
    out = tmp_path / "out"
    argv = [
        *("--turns", str(tmp_path / "turns.jsonl"), "--memory-dir", str(memory_dir)),
        *("--config", str(_write_config(tmp_path)), "--out", str(out), "--key-env", "TEST_JUDGE_KEY"),
        *("--arms", "shipped,A5", "--thresholds", "0.5,0.8"),
    ]

    await run.run_cli(argv, transport=_judge_transport())
    assert len(stub.requests) == len(turns)
    first = json.loads((out / "results.json").read_text())
    await run.run_cli(argv, transport=_judge_transport())
    assert len(stub.requests) == len(turns)

    results = json.loads((out / "results.json").read_text())
    assert results == first
    assert set(results) == {"all", "tuning", "held_out"}
    assert set(results["all"]) == {"shipped", "A5@0.5", "A5@0.8"}
    assert results["all"]["A5@0.5"]["mean_tokens"] > results["all"]["A5@0.8"]["mean_tokens"] > 0
    assert results["all"]["A5@0.8"]["fallback_rate"] == 0.0
    assert results["all"]["A5@0.8"]["rerank_p95_ms"] >= results["all"]["A5@0.8"]["rerank_p50_ms"] >= 0.0
    assert results["all"]["shipped"]["rerank_p50_ms"] == 0.0
    assert results["tuning"]["shipped"]["turns"] + results["held_out"]["shipped"]["turns"] == len(turns)
    assert "Pixel" not in (out / "results.md").read_text()

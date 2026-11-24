"""Rerank arms of the memory-recall benchmark: the score cache, the A5 selection rule, the threshold sweep and the split."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from typing import TYPE_CHECKING

import httpx
import pytest

from evals.memory_recall import rerank_arms, run
from evals.memory_recall.arms import ARMS
from evals.memory_recall.rerank_arms import (
    LatencySample,
    RerankerSpec,
    measure_latency,
    rerank_turns,
    select_reranked,
)
from evals.memory_recall.rerank_cache import CachedReranker, RerankCache
from evals.memory_recall.run import latency_metrics, split_turns
from mindroom.memory._file_backend import _RECALL_SELECTION, _retrieve_memories
from mindroom.memory._shared import MemoryResult
from mindroom.token_counting import families
from tests.evals.test_memory_recall_run import (
    _cli_argv,
    _judge_transport,
    _turn,
)

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


def test_every_rerank_arm_is_the_shipped_arm_with_a_reranker() -> None:
    """A rerank arm reranks the `shipped` candidates: it differs from `shipped` only in its name and reranker."""
    rerank_arms_by_name = {name: arm for name, arm in ARMS.items() if arm.reranker is not None}
    assert list(rerank_arms_by_name) == ["A5", "A6o", "A6t", "A7o", "A7t"]
    for arm in rerank_arms_by_name.values():
        assert replace(arm, name="shipped", reranker=None) == ARMS["shipped"]


@pytest.mark.asyncio
async def test_cached_reranker_skips_second_request(tmp_path: Path) -> None:
    """A second request for scored texts reaches no provider, and only new texts are sent."""
    stub = _StubReranker()
    documents = ["Pixel eats salmon", "the shed is green"]
    with RerankCache(tmp_path / "rerank.sqlite") as cache:
        reranker = _cached(stub, cache)
        first = await reranker.ascore("cat food", documents)
        again = await reranker.ascore("cat food", documents)
        assert first == again == [0.9, 0.6]
        assert len(stub.requests) == 1

        await reranker.ascore("cat food", [*documents, "a tomato bed"])
        assert stub.requests[-1] == ("cat food", ["a tomato bed"])

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


def _candidates(count: int) -> list[MemoryResult]:
    return [
        MemoryResult(id=f"c{index}", memory=f"note {index}", user_id="u", score=1.0 - index / 100)
        for index in range(count)
    ]


@pytest.mark.asyncio
async def test_a5_selection_ranks_by_reranker_score_and_keeps_hits_at_or_above_threshold() -> None:
    """Reranked hits at or above the threshold are kept best first (ties in hybrid order), at most 5 of them."""
    candidates = _candidates(8)
    scores = [0.2, 0.9, 0.95, 0.85, 0.8, 0.83, 0.83, 0.79]

    selected = await select_reranked("q", candidates, scores, threshold=0.8)

    assert [hit["id"] for hit in selected] == ["c2", "c1", "c3", "c5", "c6"]
    assert [hit["score"] for hit in selected] == [0.95, 0.9, 0.85, 0.83, 0.83]
    assert await select_reranked("q", candidates, scores, threshold=0.96) == []


@pytest.mark.asyncio
async def test_a5_selection_keeps_the_recall_token_budget() -> None:
    """The kept texts fit the shipped recall's 800 tokens: a best hit over the budget selects nothing."""
    candidates = _candidates(3)
    candidates[1] = MemoryResult(id="big", memory="word " * 900, user_id="u", score=0.5)

    assert await select_reranked("q", candidates, [0.5, 0.9, 0.7], threshold=0.3) == []
    assert [hit["id"] for hit in await select_reranked("q", candidates, [0.9, 0.5, 0.7], threshold=0.3)] == ["c0", "c2"]


@pytest.mark.asyncio
async def test_a5_selection_without_scores_is_the_shipped_selection() -> None:
    """A turn whose reranker failed gets exactly the shipped hybrid selection."""
    candidates = _candidates(8)
    assert await select_reranked("q", candidates, None, threshold=0.8) == await _retrieve_memories(
        "q",
        candidates,
        _RECALL_SELECTION,
    )


@pytest.mark.asyncio
async def test_a5_selection_counts_exact_o200k_tokens_from_a_cold_start(monkeypatch: pytest.MonkeyPatch) -> None:
    """Before the tokenizer has loaded, selection still counts exact o200k tokens, never the character estimate.

    `"x " * 1000` is 2,000 characters, which the estimate counts as 500 tokens, but o200k encodes as about 1,000, so
    it does not fit the 800-token budget.
    """
    # No encoder has loaded, and a background load never finishes.
    monkeypatch.setattr(families, "_encoders", {})
    monkeypatch.setattr(families, "_load_in_background", lambda _name: None)
    candidates = [MemoryResult(id="c0", memory="x " * 1000, user_id="u", score=0.5)]

    assert await select_reranked("q", candidates, [0.9], threshold=0.5) == []


def test_split_is_seeded_and_disjoint() -> None:
    """The tuning and held-out halves cover every turn once, and the same seed always splits the same way."""
    tuning, held_out = split_turns(11, seed=7)

    assert not set(tuning) & set(held_out)
    assert sorted([*tuning, *held_out]) == list(range(11))
    assert len(tuning) == 5
    assert split_turns(11, seed=7) == (tuning, held_out)
    assert split_turns(11, seed=8) != (tuning, held_out)


class _TimedReranker:
    """Answers at once, after 50 ms for a query naming "slow", raises for "broken", never for "stall"; counts calls."""

    def __init__(self) -> None:
        self.calls = 0

    async def ascore(self, query: str, documents: Sequence[str]) -> list[float]:
        self.calls += 1
        if "slow" in query:
            await asyncio.sleep(0.05)
        if "stall" in query:
            await asyncio.Event().wait()
        if "broken" in query:
            msg = "provider refused"
            raise RuntimeError(msg)
        return [0.5] * len(documents)


@pytest.mark.asyncio
async def test_latency_mode_times_every_uncached_call_and_counts_fallbacks(monkeypatch: pytest.MonkeyPatch) -> None:
    """Each measured turn calls the provider itself, after one warm-up call; slow and failed calls fall back.

    A call over the deadline adds only the deadline, as live recall stops waiting then.
    """
    monkeypatch.setattr(rerank_arms, "DEADLINE_S", 0.02)
    timed = _TimedReranker()
    queries = ["fast", "fast", "slow", "broken", "unmeasured"]
    turns = [_turn("2026-01-15", query) for query in queries]
    candidates = [_candidates(2) for _query in queries]
    candidates.insert(1, [])
    turns.insert(1, _turn("2026-01-15", "no candidates"))

    sample = await measure_latency(timed, turns, candidates, count=4)

    # One warm-up call, then every measured turn, a repeated query too.
    assert timed.calls == 5
    assert sample.failure is None
    assert [call.outcome for call in sample.calls] == ["ok", "ok", "ok", "error"]
    assert [call.fallback for call in sample.calls] == [False, False, True, True]
    metrics = latency_metrics(sample)
    assert metrics["latency_turns"] == 4
    assert metrics["latency_fallback_rate"] == 0.5
    assert metrics["latency_added_p95_ms"] == pytest.approx(20.0)
    assert metrics["latency_added_p50_ms"] is not None
    assert metrics["latency_added_p50_ms"] < 20.0


@pytest.mark.asyncio
async def test_latency_mode_records_a_stalled_call_as_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    """A call that never returns is stopped at the hard cap, counts as a timeout and fallback, and the run goes on."""
    monkeypatch.setattr(rerank_arms, "DEADLINE_S", 0.005)
    monkeypatch.setattr(rerank_arms, "CALL_CAP_S", 0.02)
    queries = ["fast", "stall", "fast again"]

    sample = await measure_latency(
        _TimedReranker(),
        [_turn("2026-01-15", query) for query in queries],
        [_candidates(2) for _query in queries],
        count=3,
    )

    assert sample.failure is None
    assert [call.outcome for call in sample.calls] == ["ok", "timeout", "ok"]
    assert sample.calls[1].seconds == 0.02
    assert sample.calls[1].fallback
    assert latency_metrics(sample)["latency_fallback_rate"] == pytest.approx(1 / 3)


@pytest.mark.asyncio
async def test_stalled_warm_up_fails_the_latency_table(monkeypatch: pytest.MonkeyPatch) -> None:
    """A warm-up that never returns ends the arm's latency measurement with a clear failure, not a hang."""
    monkeypatch.setattr(rerank_arms, "CALL_CAP_S", 0.02)
    timed = _TimedReranker()

    sample = await measure_latency(
        timed,
        [_turn("2026-01-15", "stall"), _turn("2026-01-15", "fast")],
        [_candidates(2), _candidates(2)],
        count=2,
    )

    assert timed.calls == 1
    assert sample.calls == []
    assert sample.failure is not None
    assert "warm-up" in sample.failure
    metrics = latency_metrics(sample)
    assert metrics["latency_failure"] == sample.failure
    assert metrics["latency_added_p95_ms"] is None
    assert metrics["latency_fallback_rate"] is None


def test_latency_metrics_without_measurements_are_missing() -> None:
    """No measured call reports no latency and no fallback rate, never a zero."""
    assert latency_metrics(LatencySample(calls=[], failure=None)) == {
        "latency_turns": 0,
        "latency_added_p50_ms": None,
        "latency_added_p95_ms": None,
        "latency_fallback_rate": None,
        "latency_failure": None,
    }


@pytest.mark.asyncio
async def test_cost_estimate_adds_request_and_rubric_overhead(tmp_path: Path) -> None:
    """A priced provider's estimate counts the query, the candidates and the request's fixed and per-candidate overhead."""
    spec = RerankerSpec(
        provider="stub",
        model="m",
        rubric_version="v1",
        usd_per_million_input_tokens=1.0,
        create=lambda _paths: _StubReranker(),
        request_overhead_tokens=100,
        candidate_overhead_tokens=60,
    )
    candidates = [
        MemoryResult(id="c0", memory="hello", user_id="u"),
        MemoryResult(id="c1", memory="world", user_id="u"),
    ]
    with RerankCache(tmp_path / "rerank.sqlite") as cache:
        [call] = await rerank_turns([_turn("2026-01-15", "hi")], [candidates], _cached(_StubReranker(), cache), spec)

    assert call is not None
    # "hi", "hello", "world" are one o200k token each.
    assert call.usd == pytest.approx((3 + 100 + 2 * 60) / 1_000_000)


@pytest.mark.asyncio
async def test_threshold_sweep_reuses_scores(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Every threshold reads one reranker request per turn, and a rerun reads them all from the cache."""
    stub = _StubReranker()
    spec = rerank_arms.RERANKERS["bge-reranker-v2-m3"]
    monkeypatch.setitem(rerank_arms.RERANKERS, "bge-reranker-v2-m3", replace(spec, create=lambda _paths: stub))
    turns = ["salmon kibble Pixel", "garden shed colour"]
    argv, out = _cli_argv(monkeypatch, tmp_path, turns, *("--arms", "shipped,A5", "--thresholds", "0.5,0.8"))

    await run.run_cli(argv, transport=_judge_transport())
    assert len(stub.requests) == len(turns)
    first = json.loads((out / "results.json").read_text())
    await run.run_cli(argv, transport=_judge_transport())
    assert len(stub.requests) == len(turns)

    results = json.loads((out / "results.json").read_text())
    assert results == first
    assert results["judge_model"] == run.JUDGE_MODEL
    assert results["split_seed"] == 0
    assert "latency" not in results
    splits = results["splits"]
    assert set(splits) == {"all", "tuning", "held_out"}
    assert set(splits["all"]) == {"shipped", "A5@0.5", "A5@0.8"}
    assert splits["all"]["A5@0.5"]["mean_tokens"] > splits["all"]["A5@0.8"]["mean_tokens"] > 0
    # A quality run reports no latency or fallback numbers, which only latency mode measures.
    assert not any("latency" in column or "fallback" in column for column in splits["all"]["A5@0.8"])
    assert splits["all"]["A5@0.8"]["rerank_errors"] == 0
    assert splits["tuning"]["shipped"]["turns"] + splits["held_out"]["shipped"]["turns"] == len(turns)
    assert "Pixel" not in (out / "results.md").read_text()


@pytest.mark.asyncio
async def test_latency_mode_samples_only_held_out_turns(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Latency mode times uncached calls for held-out turns only, in order, and reports beside the quality rows."""
    stub = _StubReranker()
    spec = rerank_arms.RERANKERS["bge-reranker-v2-m3"]
    monkeypatch.setitem(rerank_arms.RERANKERS, "bge-reranker-v2-m3", replace(spec, create=lambda _paths: stub))
    queries = [f"garden shed colour {number}" for number in range(8)]
    argv, out = _cli_argv(
        monkeypatch,
        tmp_path,
        queries,
        *("--arms", "A5", "--thresholds", "0.5", "--latency-turns", "100"),
    )

    await run.run_cli(argv, transport=_judge_transport())

    _tuning, held_out = split_turns(len(queries), seed=0)
    # The quality pass scores every turn once; then a warm-up and one timed call per held-out turn follow.
    timed = [query for query, _documents in stub.requests[len(queries) :]]
    assert timed == [queries[held_out[0]], *(queries[index] for index in held_out)]
    latency = json.loads((out / "results.json").read_text())["latency"]
    assert set(latency) == {"A5"}
    assert latency["A5"]["latency_turns"] == len(held_out)
    assert latency["A5"]["latency_fallback_rate"] == 0.0
    assert latency["A5"]["latency_failure"] is None
    assert "latency_added_p95_ms" in (out / "results.md").read_text()


@pytest.mark.asyncio
async def test_judge_model_is_selectable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A second judge answers every request of its run and is named in the results."""
    argv, out = _cli_argv(
        monkeypatch,
        tmp_path,
        ["salmon kibble Pixel"],
        *("--arms", "shipped", "--judge-model", "google/gemini-3.8-flash"),
    )
    inner = _judge_transport()
    models: list[str] = []

    async def handle(request: httpx.Request) -> httpx.Response:
        models.append(json.loads(request.content)["model"])
        return await inner.handle_async_request(request)

    await run.run_cli(argv, transport=httpx.MockTransport(handle))

    assert models
    assert set(models) == {"google/gemini-3.8-flash"}
    assert json.loads((out / "results.json").read_text())["judge_model"] == "google/gemini-3.8-flash"

"""Judgment rerank arms A6o, A6t, A7o and A7t: built on the production judgment reranker, cached, and billed."""

from __future__ import annotations

import json
from dataclasses import replace
from functools import partial
from typing import TYPE_CHECKING, Any

import httpx
import pytest

from evals.memory_recall import rerank_arms, run
from evals.memory_recall.arms import ARMS
from evals.memory_recall.rerank_arms import RERANKERS, RerankerSpec, rerank_turns
from evals.memory_recall.rerank_cache import CachedReranker, RerankCache
from mindroom.constants import resolve_runtime_paths
from mindroom.judgment import rerank as judgment_rerank
from mindroom.judgment.client import JudgmentClient
from mindroom.memory._shared import MemoryResult
from mindroom.model_defaults import OPENAI_DECISIONS_MODEL
from tests.evals.test_memory_recall_run import (
    _cli_argv,
    _judge_transport,
    _turn,
    _write_config,
)

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

_BILLED_INPUT_TOKENS = 7_000
#: Arm name, reranker provider, the model its scores are cached under, whether it asks the gate question.
_JUDGMENT_ARMS = [
    ("A6o", "openai_decisions", OPENAI_DECISIONS_MODEL, False),
    ("A6t", "typesafe", "jev-1.13.0", False),
    ("A7o", "openai_decisions", OPENAI_DECISIONS_MODEL, True),
    ("A7t", "typesafe", "jev-1.13.0", True),
]


def _probability(question: object) -> float:
    """0.4 for the gate question; for a candidate 0.9 when its passage names Pixel, 0.6 the shed, else 0.1."""
    text = json.dumps(question)
    if "stated before help reply" in text:
        return 0.4
    return 0.9 if "Pixel" in text else 0.6 if "shed" in text else 0.1


class _Provider:
    """Both judgment endpoints on `httpx.MockTransport`; records every posted body by host, and its client's timeout."""

    def __init__(self) -> None:
        self.posted: list[tuple[str, dict[str, Any]]] = []
        self.timeouts: list[float] = []

    def respond(self, request: httpx.Request, *, timeout_seconds: float) -> httpx.Response:
        posted = json.loads(request.content)
        self.posted.append((request.url.host, posted))
        self.timeouts.append(timeout_seconds)
        usage = {"input_tokens": _BILLED_INPUT_TOKENS, "output_tokens": 0}
        if request.url.host == "api.typesafe.ai":
            answers = {name: {"type": "noul", "noul": _probability(q)} for name, q in posted["questions"].items()}
            return httpx.Response(200, json={"model": "jev-1.13.0", "answers": answers, "usage": usage})
        answers = [
            {"type": "predicate", "name": q["name"], "probability": _probability(q)} for q in posted["questions"]
        ]
        return httpx.Response(200, json={"model": OPENAI_DECISIONS_MODEL, "answers": answers, "usage": usage})

    def requests_to(self, provider: str) -> int:
        host = "api.typesafe.ai" if provider == "typesafe" else "api.openai.com"
        return sum(posted_host == host for posted_host, _body in self.posted)


@pytest.fixture
def provider(monkeypatch: pytest.MonkeyPatch) -> _Provider:
    """Route every judgment client the arms build to a mock provider; nothing reaches the network."""
    mock = _Provider()
    init = JudgmentClient.__init__

    def mocked_init(self: JudgmentClient, **kwargs: Any) -> None:  # noqa: ANN401
        respond = partial(mock.respond, timeout_seconds=kwargs["timeout_seconds"])
        init(self, **{**kwargs, "transport": httpx.MockTransport(respond)})

    monkeypatch.setattr(JudgmentClient, "__init__", mocked_init)
    return mock


@pytest.mark.asyncio
@pytest.mark.parametrize(("arm", "provider_name", "model", "gate"), _JUDGMENT_ARMS)
async def test_judgment_arm_scores_every_candidate_in_one_production_request(
    tmp_path: Path,
    provider: _Provider,
    arm: str,
    provider_name: str,
    model: str,
    gate: bool,
) -> None:
    """Each arm resolves to the production judgment reranker: one request, one predicate per candidate (+ gate)."""
    spec = RERANKERS[ARMS[arm].reranker or ""]
    assert (spec.provider, spec.model) == (provider_name, model)
    runtime_paths = resolve_runtime_paths(
        config_path=_write_config(tmp_path),
        storage_path=tmp_path / "index",
        process_env={"OPENAI_API_KEY": "sk-synthetic", "TYPESAFE_API_KEY": "ts-synthetic"},
    )
    reranker = spec.create(runtime_paths)

    scores = await reranker.ascore("what does Pixel eat", ["Pixel eats salmon", "the shed is green", "noon"])

    assert provider.requests_to(provider_name) == len(provider.posted) == 1
    ((_host, posted),) = provider.posted
    assert len(posted["questions"]) == 3 + gate
    # The gate caps every score at its own probability, 0.4.
    assert scores == pytest.approx([0.4, 0.4, 0.1] if gate else [0.9, 0.6, 0.1])
    # Latency mode builds the reranker as live recall does, which asks the same request.
    assert spec.create_live is not None
    await spec.create_live(runtime_paths).ascore(
        "what does Pixel eat",
        ["Pixel eats salmon", "the shed is green", "noon"],
    )
    assert len(provider.posted) == 2
    assert provider.posted[1][1] == posted


def test_rubric_version_follows_the_rubric_text(monkeypatch: pytest.MonkeyPatch) -> None:
    """A changed candidate rubric or gate question gives a new rubric version, so cached scores are not reused."""
    versions = {name: RERANKERS[ARMS[name].reranker or ""].rubric_version for name, *_rest in _JUDGMENT_ARMS}
    assert all(versions.values())
    assert versions["A6o"] == versions["A6t"] != versions["A7o"] == versions["A7t"]

    monkeypatch.setattr(judgment_rerank, "_GATE", replace(judgment_rerank._GATE, instructions="Another gate?"))
    assert rerank_arms._rubric_version(gate=True) != versions["A7o"]
    assert rerank_arms._rubric_version(gate=False) == versions["A6o"]

    original = judgment_rerank._candidate_question
    monkeypatch.setattr(
        judgment_rerank,
        "_candidate_question",
        lambda index, document: replace(original(index, document), when_true="Another rubric."),
    )
    assert rerank_arms._rubric_version(gate=False) != versions["A6o"]


@pytest.mark.asyncio
async def test_judgment_arms_sweep_thresholds_from_cached_scores_and_bill_their_cost(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    provider: _Provider,
) -> None:
    """One request per turn serves every threshold, a rerun asks nothing, and the cost is the billed input."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-synthetic")
    monkeypatch.setenv("TYPESAFE_API_KEY", "ts-synthetic")
    turns = ["salmon kibble Pixel", "garden shed colour"]
    argv, out = _cli_argv(monkeypatch, tmp_path, turns, *("--arms", "shipped,A6t,A7o", "--thresholds", "0.5,0.8"))

    await run.run_cli(argv, transport=_judge_transport())
    assert provider.requests_to("typesafe") == provider.requests_to("openai_decisions") == len(turns)
    first = json.loads((out / "results.json").read_text())
    await run.run_cli(argv, transport=_judge_transport())
    assert len(provider.posted) == 2 * len(turns)

    results = json.loads((out / "results.json").read_text())
    assert results == first
    rows = results["splits"]["all"]
    assert set(rows) == {"shipped", "A6t@0.5", "A6t@0.8", "A7o@0.5", "A7o@0.8"}
    assert rows["A6t@0.5"]["mean_tokens"] > rows["A6t@0.8"]["mean_tokens"] > 0
    # The gate caps every score at 0.4, so no threshold keeps anything.
    assert rows["A7o@0.5"]["mean_tokens"] == 0
    assert rows["A6t@0.8"]["usd_per_turn"] == pytest.approx(_BILLED_INPUT_TOKENS * 0.042 / 1_000_000)
    assert rows["A7o@0.8"]["usd_per_turn"] == pytest.approx(_BILLED_INPUT_TOKENS * 0.10 / 1_000_000)
    assert rows["A6t@0.8"]["usd_source"] == rows["A7o@0.8"]["usd_source"] == "billed"
    assert rows["shipped"]["usd_source"] is None
    assert "Pixel" not in (out / "results.md").read_text()


@pytest.mark.asyncio
async def test_latency_mode_times_judgment_calls_with_the_live_timeout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    provider: _Provider,
) -> None:
    """Quality calls wait up to the hard cap; timed calls time out after the production 1.5 s, as live recall does."""
    monkeypatch.setenv("TYPESAFE_API_KEY", "ts-synthetic")
    argv, out = _cli_argv(
        monkeypatch,
        tmp_path,
        ["salmon kibble Pixel", "garden shed colour"],
        *("--arms", "A6t", "--thresholds", "0.5", "--latency-turns", "5"),
    )

    await run.run_cli(argv, transport=_judge_transport())

    # Two quality calls, then a warm-up and one timed call for the one held-out turn.
    assert provider.timeouts == [rerank_arms.CALL_CAP_S] * 2 + [1.5] * 2
    latency = json.loads((out / "results.json").read_text())["latency"]["A6t"]
    assert latency["latency_turns"] == 1
    assert latency["latency_fallback_rate"] == 0.0


class _BilledStub:
    """Scores every text 0.5 and reports the input tokens its provider billed for the last call."""

    def __init__(self) -> None:
        self.last_input_tokens: int | None = None

    async def ascore(self, query: str, documents: Sequence[str]) -> list[float]:
        self.last_input_tokens = 1_000 + len(documents)
        _ = query
        return [0.5] * len(documents)


@pytest.mark.asyncio
async def test_cost_is_billed_for_whole_requests_and_estimated_otherwise(tmp_path: Path) -> None:
    """A turn whose request was sent whole costs its billed input, also from the cache; a partly cached one estimates."""
    spec = RerankerSpec(
        provider="stub",
        model="m",
        rubric_version="v1",
        usd_per_million_input_tokens=1.0,
        create=lambda _paths: _BilledStub(),
    )
    whole = [MemoryResult(id="c0", memory="hello", user_id="u"), MemoryResult(id="c1", memory="world", user_id="u")]
    # Shares the query and one text with the first turn, so only its new text is sent.
    partial = [*whole[:1], MemoryResult(id="c2", memory="again", user_id="u")]
    turns = [_turn("2026-01-15", "hi"), _turn("2026-01-15", "hi")]
    with RerankCache(tmp_path / "rerank.sqlite") as cache:
        reranker = CachedReranker(_BilledStub(), cache, provider="stub", model="m", rubric_version="v1")
        first, second = await rerank_turns(turns, [whole, partial], reranker, spec)
        assert first is not None
        assert second is not None
        assert (first.usd, first.billed) == (pytest.approx(1_002 / 1_000_000), True)
        # "hi", "hello", "again" are one o200k token each; the spec adds no overhead.
        assert (second.usd, second.billed) == (pytest.approx(3 / 1_000_000), False)
        assert run.rerank_metrics([first, second])["usd_source"] == "mixed"

        # A rerun reads the billed count back without asking the provider.
        rerun = CachedReranker(_BilledStub(), cache, provider="stub", model="m", rubric_version="v1")
        [cached] = await rerank_turns(turns[:1], [whole], rerun, spec)
    assert cached == first
    assert run.rerank_metrics([cached])["usd_source"] == "billed"

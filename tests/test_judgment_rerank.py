"""Judgment backends rerank every candidate with one batched request."""

from __future__ import annotations

import json
from functools import partial
from typing import TYPE_CHECKING, Any

import httpx
import pytest

import mindroom.knowledge.rerank as rerank_module
from mindroom.config.judgment import OpenAIDecisionsJudgmentConfig, TypeSafeJudgmentConfig
from mindroom.config.main import Config
from mindroom.credentials.manager import get_runtime_shared_credentials_manager
from mindroom.judgment.answers import JudgmentError
from mindroom.judgment.client import JudgmentClient
from mindroom.judgment.evaluator import BatchEvaluator
from mindroom.judgment.openai_decisions import OPENAI_DECISIONS
from mindroom.judgment.rerank import _JudgmentReranker, create_judgment_reranker
from mindroom.model_defaults import OPENAI_DECISIONS_MODEL
from tests.conftest import test_runtime_paths

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

_SECRET = "ghp_abcdefghijklmnopqrstuvwxyz0123456789AB"  # noqa: S105


def _answers(probabilities: dict[str, float | None]) -> Callable[[list[str]], list[dict[str, Any]]]:
    """Answer each posted question by name; ``None`` refuses it."""

    def answer(names: list[str]) -> list[dict[str, Any]]:
        return [
            {"type": "refusal", "name": name}
            if probabilities[name] is None
            else {"type": "predicate", "name": name, "probability": probabilities[name]}
            for name in names
        ]

    return answer


def _reranker(
    answer: Callable[[list[str]], list[dict[str, Any]]] | None,
    seen: list[httpx.Request],
    *,
    gate: bool = False,
) -> _JudgmentReranker:
    """A reranker on the real OpenAI Decisions wire; a ``None`` answer fails with HTTP 500."""

    def respond(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        posted = json.loads(request.content)
        if answer is None:
            return httpx.Response(500)
        names = [question["name"] for question in posted["questions"]]
        usage = {"input_tokens": 10, "output_tokens": 0}
        return httpx.Response(200, json={"answers": answer(names), "model": OPENAI_DECISIONS_MODEL, "usage": usage})

    client = JudgmentClient(api_key="synthetic", wire=OPENAI_DECISIONS, transport=httpx.MockTransport(respond))
    batch = BatchEvaluator(
        evaluate=partial(client.judge_many, owner="root:rerank", allow_network=True),
        fits=client.batch_fits,
    )
    return _JudgmentReranker(batch, gate=gate)


@pytest.mark.asyncio
async def test_one_request_for_all_candidates() -> None:
    """Every candidate is one predicate of a single request about the query."""
    seen: list[httpx.Request] = []
    reranker = _reranker(_answers({"c0": 0.9, "c1": 0.2, "c2": 0.5}), seen)
    await reranker.ascore("Where do I park?", ["Park in lot B.", "Lunch is at noon.", "Lot B opens at 8."])

    assert len(seen) == 1
    posted = json.loads(seen[0].content)
    assert posted["input"] == [{"role": "user", "content": "user: Where do I park?"}]
    questions = posted["questions"]
    assert [question["name"] for question in questions] == ["c0", "c1", "c2"]
    instructions = questions[0]["instructions"]
    assert instructions.startswith("Passage: Park in lot B.\nWould this passage help reply to the user's message?")
    assert "True when: It states a fact, preference, decision or instruction the reply should use." in instructions
    assert "False when: It is unrelated, or only shares a topic or words with the message." in instructions


@pytest.mark.asyncio
async def test_scores_follow_document_order() -> None:
    """Scores line up with the documents even when the provider answers in another order."""

    def shuffled(names: list[str]) -> list[dict[str, Any]]:
        probabilities = {"c0": 0.1, "c1": 0.7, "c2": 0.4}
        return [{"type": "predicate", "name": name, "probability": probabilities[name]} for name in reversed(names)]

    scores = await _reranker(shuffled, []).ascore("query", ["a", "b", "c"])
    assert scores == [0.1, 0.7, 0.4]


@pytest.mark.asyncio
async def test_refused_candidate_scores_zero() -> None:
    """A refused question scores 0.0 while the others keep their probabilities."""
    scores = await _reranker(_answers({"c0": 0.9, "c1": None, "c2": 0.6}), []).ascore("query", ["a", "b", "c"])
    assert scores == [0.9, 0.0, 0.6]


@pytest.mark.asyncio
async def test_secret_in_candidate_is_sent_redacted() -> None:
    """A credential in a candidate or the query never leaves; the redacted candidate is still judged."""
    seen: list[httpx.Request] = []
    reranker = _reranker(_answers({"c0": 0.7, "c1": 0.3}), seen)
    scores = await reranker.ascore(f"Is {_SECRET} still valid?", [f"The deploy token is {_SECRET}.", "Unrelated."])

    assert all(_SECRET not in request.content.decode() for request in seen)
    assert "redacted" in json.loads(seen[0].content)["questions"][0]["instructions"]
    assert scores == [0.7, 0.3]


def _straddling(offset: int) -> str:
    """A text over the clip length whose secret starts ``offset`` characters into it."""
    return "." * offset + f" {_SECRET} " + "." * (3_000 - offset)


def _secret_fragments(body: str) -> list[str]:
    """Every 8-character piece of the secret that appears in a posted body."""
    pieces = {_SECRET[start : start + 8] for start in range(len(_SECRET) - 7)}
    return sorted(piece for piece in pieces if piece in body)


# A 3,044-character text keeps [0, 1000) and [2044, 3044); offsets 990 and 2024 put the secret across each edge.
_CLIP_EDGES = [990, 2_024]


@pytest.mark.asyncio
@pytest.mark.parametrize("offset", _CLIP_EDGES)
async def test_secret_across_a_clip_edge_of_a_candidate_never_leaves(offset: int) -> None:
    """A credential cut by the clip window is redacted before clipping, and the candidate is still judged."""
    seen: list[httpx.Request] = []
    scores = await _reranker(_answers({"c0": 0.7}), seen).ascore("query", [_straddling(offset)])

    assert _secret_fragments(seen[0].content.decode()) == []
    assert scores == [0.7]


@pytest.mark.asyncio
@pytest.mark.parametrize("offset", _CLIP_EDGES)
async def test_secret_across_a_clip_edge_of_the_query_never_leaves(offset: int) -> None:
    """A credential cut by the clip window of the query is redacted before clipping."""
    seen: list[httpx.Request] = []
    scores = await _reranker(_answers({"c0": 0.7}), seen).ascore(_straddling(offset), ["a"])

    assert _secret_fragments(seen[0].content.decode()) == []
    assert scores == [0.7]


@pytest.mark.asyncio
async def test_candidates_past_the_byte_cap_score_zero() -> None:
    """Leading candidates that fit one request are judged; the rest score 0.0 and keep their order."""
    seen: list[httpx.Request] = []
    reranker = _reranker(lambda names: _answers(dict.fromkeys(names, 0.9))(names), seen)
    documents = [f"passage {index} " + "lorem ipsum dolor sit amet " * 80 for index in range(100)]
    scores = await reranker.ascore("query", documents)

    judged = len(json.loads(seen[0].content)["questions"])
    assert 0 < judged < len(documents)
    assert len(seen[0].content) <= 128_000
    assert scores == [0.9] * judged + [0.0] * (len(documents) - judged)


@pytest.mark.asyncio
async def test_backend_failure_raises() -> None:
    """A failed batch raises, so retrieval falls back to the hybrid selection."""
    with pytest.raises(JudgmentError):
        await _reranker(None, []).ascore("query", ["a", "b"])


@pytest.mark.asyncio
async def test_blank_query_raises_without_a_request() -> None:
    """A request that cannot be built for a reason other than its size raises instead of dropping candidates."""
    seen: list[httpx.Request] = []
    with pytest.raises(JudgmentError):
        await _reranker(_answers({"c0": 0.9}), seen).ascore("   ", ["a"])
    assert seen == []


@pytest.mark.asyncio
async def test_gate_caps_every_score() -> None:
    """With the gate, every candidate scores at most the gate question's probability, asked in the same request."""
    seen: list[httpx.Request] = []
    reranker = _reranker(_answers({"c0": 0.9, "c1": 0.1, "g": 0.2}), seen, gate=True)
    scores = await reranker.ascore("query", ["a", "b"])

    assert len(seen) == 1
    assert [question["name"] for question in json.loads(seen[0].content)["questions"]] == ["c0", "c1", "g"]
    assert scores == [0.2, 0.1]


@pytest.mark.asyncio
async def test_missing_credential_builds_no_reranker(tmp_path: Path) -> None:
    """Without an API key neither judgment provider builds a reranker, and a configured one fails to build."""
    paths = test_runtime_paths(tmp_path)
    assert create_judgment_reranker(TypeSafeJudgmentConfig(provider="typesafe"), paths) is None
    settings = OpenAIDecisionsJudgmentConfig(provider="openai_decisions")
    assert create_judgment_reranker(settings, paths) is None
    with pytest.raises(RuntimeError, match="openai_decisions"):
        rerank_module._create_reranker(settings, paths)


def test_judgment_provider_is_a_reranker_config(tmp_path: Path) -> None:
    """``reranker`` accepts a judgment provider, keeps its 0.8 threshold, and builds a judgment reranker."""
    config = Config(reranker={"provider": "openai_decisions"})
    assert isinstance(config.reranker, OpenAIDecisionsJudgmentConfig)
    assert config.reranker.threshold == 0.8

    paths = test_runtime_paths(tmp_path)
    get_runtime_shared_credentials_manager(paths).save_credentials("openai", {"api_key": "stored-openai-key"})
    assert isinstance(rerank_module._create_reranker(config.reranker, paths), _JudgmentReranker)

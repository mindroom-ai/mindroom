"""OpenAI Decisions judgments keep the shared rubric, limits, and fail-closed validation."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import httpx
import pytest

from mindroom.config.judgment import OpenAIDecisionsJudgmentConfig
from mindroom.config.main import Config
from mindroom.credentials import get_runtime_shared_credentials_manager
from mindroom.judgment.client import JudgmentClient
from mindroom.judgment.evaluator import create_choice_evaluator, create_judgment_evaluator
from mindroom.judgment.openai_decisions import _ENDPOINT, OPENAI_DECISIONS
from mindroom.judgment.state import ChoiceQuestion, JudgmentMessage, JudgmentQuestion, build_judgment_request
from mindroom.model_defaults import OPENAI_DECISIONS_MODEL
from tests.conftest import test_runtime_paths

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from mindroom.judgment.answers import ChoiceDecision, JudgmentResult
    from mindroom.judgment.state import JudgmentRequest

pytestmark = pytest.mark.asyncio

_QUESTION = JudgmentQuestion(
    id="simple_task",
    instructions="Can the configured cheaper model handle this task?",
    when_true="Routine text transformation with complete input.",
    when_false="Complex reasoning, missing information, or unsupported tools.",
)
_CHOICE = ChoiceQuestion(
    id="responder",
    instructions="Choose the best responder.",
    options=(("code", "Programming"), ("research", "Research"), ("no_fit", "No suitable responder")),
)
_MESSAGES = (
    JudgmentMessage("user", 'Alphabetize "pear, apple".'),
    JudgmentMessage("assistant", "apple, pear"),
    JudgmentMessage("user", "Now reverse it."),
)


def _request(
    question: JudgmentQuestion | ChoiceQuestion = _QUESTION,
    guidance: str = "Judge only the task.",
) -> JudgmentRequest:
    return build_judgment_request(question, _MESSAGES, instructions=guidance)


def _body(answer: dict[str, Any], **root: object) -> dict[str, Any]:
    return {
        "answers": [answer],
        "model": OPENAI_DECISIONS_MODEL,
        "usage": {
            "input_tokens": 52,
            "input_tokens_details": {"cache_write_tokens": 0, "cached_tokens": 0},
            "output_tokens": 0,
            "output_tokens_details": {"reasoning_tokens": 0},
            "total_tokens": 52,
        },
        **root,
    }


def _predicate(probability: object = 0.9) -> dict[str, Any]:
    return {"type": "predicate", "name": "simple_task", "probability": probability}


def _choice(**overrides: object) -> dict[str, Any]:
    return {
        "type": "choice",
        "name": "responder",
        "choice": "code",
        "probabilities": [
            {"value": "code", "probability": 0.9},
            {"value": "research", "probability": 0.06},
            {"value": "no_fit", "probability": 0.04},
        ],
        "confidence": 0.95,
        **overrides,
    }


def _client(body: dict[str, Any], seen: list[httpx.Request] | None = None) -> JudgmentClient:
    def respond(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        return httpx.Response(200, json=body)

    return JudgmentClient(api_key="synthetic-openai", wire=OPENAI_DECISIONS, transport=httpx.MockTransport(respond))


async def _judge(body: dict[str, Any]) -> JudgmentResult[bool]:
    return await _client(body).judge(_request(), owner="decisions", allow_network=True)


async def _judge_choice(body: dict[str, Any]) -> JudgmentResult[ChoiceDecision]:
    return await _client(body).judge_choice(_request(_CHOICE), owner="decisions", allow_network=True)


async def test_predicate_request_carries_the_shared_rubric_and_roles() -> None:
    """The endpoint, credential, rubric, guidance, and every message's role must reach the predicate."""
    seen: list[httpx.Request] = []
    result = await _client(_body(_predicate(0.81)), seen).judge(_request(), owner="decisions", allow_network=True)

    assert result.failure is None
    assert result.decision is True
    assert result.probability == 0.81
    assert result.model_id == OPENAI_DECISIONS_MODEL
    assert (result.input_tokens, result.output_tokens) == (52, 0)
    assert len(seen) == 1
    assert str(seen[0].url) == _ENDPOINT == "https://api.openai.com/v1/decisions"
    assert seen[0].headers["authorization"] == "Bearer synthetic-openai"
    evidence = json.loads(_request().body or b"")
    assert json.loads(seen[0].content) == {
        "model": "gpt-6-luna",
        "input": [
            {"role": "user", "content": 'user: Alphabetize "pear, apple".'},
            {"role": "user", "content": "assistant: apple, pear"},
            {"role": "user", "content": "user: Now reverse it."},
        ],
        "questions": [
            {
                "type": "predicate",
                "name": "simple_task",
                "instructions": (
                    f"{evidence['question']['instructions']}\n"
                    f"True when: {_QUESTION.when_true}\n"
                    f"False when: {_QUESTION.when_false}\n"
                    "Guidance: Judge only the task."
                ),
            },
        ],
    }


@pytest.mark.parametrize(("probability", "decision"), [(0.8, True), (0.79, False), (0.0, False), (1, True)])
async def test_predicate_probability_applies_the_threshold(probability: float, decision: bool) -> None:
    """The decision is derived locally from the probability, never generated by the provider."""
    result = await _judge(_body(_predicate(probability)))
    assert result.failure is None
    assert result.decision is decision
    assert result.probability == probability


async def test_choice_request_and_validated_distribution() -> None:
    """Choices keep their descriptions, and the accepted winner keeps its full distribution."""
    seen: list[httpx.Request] = []
    client = _client(_body(_choice()), seen)
    result = await client.judge_choice(_request(_CHOICE, guidance=""), owner="decisions", allow_network=True)

    assert result.failure is None
    assert result.decision is not None
    assert result.decision.option == "code"
    assert result.decision.confidence == 0.95
    assert dict(result.decision.probabilities) == {"code": 0.9, "research": 0.06, "no_fit": 0.04}
    assert result.probability == 0.9
    question = json.loads(seen[0].content)["questions"][0]
    assert question["type"] == "choice"
    assert question["name"] == "responder"
    assert "Guidance" not in question["instructions"]
    assert {(choice["value"], choice["description"]) for choice in question["choices"]} == set(_CHOICE.options)


@pytest.mark.parametrize(
    "answer",
    [
        _choice(confidence=0.2),
        _choice(
            choice="code",
            probabilities=[
                {"value": "code", "probability": 0.6},
                {"value": "research", "probability": 0.3},
                {"value": "no_fit", "probability": 0.1},
            ],
        ),
        _choice(
            probabilities=[
                {"value": "code", "probability": 0.45},
                {"value": "research", "probability": 0.45},
                {"value": "no_fit", "probability": 0.1},
            ],
        ),
    ],
    ids=["low_confidence", "low_probability", "tie"],
)
async def test_uncertain_choices_abstain(answer: dict[str, Any]) -> None:
    """A low-confidence, low-probability, or tied winner is a valid abstention."""
    result = await _judge_choice(_body(answer))
    assert result.failure is None
    assert result.decision is None


async def test_refusals_abstain_without_failure() -> None:
    """A provider refusal is an explicit non-answer, not a malformed response."""
    for answer in ({"type": "refusal", "name": "simple_task"}, {"type": "refusal"}):
        result = await _judge(_body(answer))
        assert result.failure is None
        assert result.decision is None
        assert result.probability is None
        assert result.input_tokens == 52
    choice = await _judge_choice(_body({"type": "refusal", "name": "responder"}))
    assert choice.failure is None
    assert choice.decision is None


@pytest.mark.parametrize("model", ["gpt-6-luna", "gpt-6-luna-2026-10-01"])
async def test_alias_and_dated_snapshot_are_accepted(model: str) -> None:
    """The alias may resolve to a dated snapshot, which the outcome logs record."""
    result = await _judge(_body(_predicate(), model=model))
    assert result.failure is None
    assert result.model_id == model


@pytest.mark.parametrize("model", ["gpt-6-sol", "gpt-6-luna-mini", "gpt-6-luna-latest", None])
async def test_other_models_are_model_drift(model: object) -> None:
    """A different model has a distinct closed failure."""
    result = await _judge(_body(_predicate(), model=model))
    assert result.failure == "model_drift"
    assert result.decision is None


async def test_unknown_envelope_metadata_is_ignored() -> None:
    """New top-level or usage metadata cannot change the answer, so it does not disable the backend."""
    body = _body(_predicate(), id="dec_123", object="decision")
    body["usage"]["future_counter"] = 1
    result = await _judge(body)
    assert result.failure is None
    assert result.decision is True


@pytest.mark.parametrize(
    "mutate",
    [
        lambda body: body.pop("usage"),
        lambda body: body.pop("answers"),
        lambda body: body.update({"usage": None}),
        lambda body: body["usage"].pop("output_tokens"),
        lambda body: body["usage"].update({"input_tokens": -1}),
        lambda body: body["usage"].update({"input_tokens": True}),
        lambda body: body.update({"answers": []}),
        lambda body: body.update({"answers": [_predicate(), _predicate()]}),
        lambda body: body.update({"answers": {"simple_task": _predicate()}}),
        lambda body: body["answers"][0].update({"name": "other_question"}),
        lambda body: body["answers"][0].update({"type": "score"}),
        lambda body: body["answers"][0].update({"extra": None}),
        lambda body: body["answers"][0].pop("probability"),
        lambda body: body["answers"][0].update({"probability": True}),
        lambda body: body["answers"][0].update({"probability": "0.9"}),
        lambda body: body["answers"][0].update({"probability": 1.1}),
        lambda body: body["answers"][0].update({"probability": -0.1}),
        lambda body: body.update({"answers": [{"type": "refusal", "name": "simple_task", "reason": "x"}]}),
    ],
)
async def test_malformed_predicates_fail_closed(mutate: Callable[[dict[str, Any]], object]) -> None:
    """Missing, extra, or mistyped answer fields must never become an accepted decision."""
    body = _body(_predicate())
    mutate(body)
    result = await _judge(body)
    assert result.failure == "invalid_response"
    assert result.decision is None


@pytest.mark.parametrize(
    "answer",
    [
        _choice(choice="unknown"),
        _choice(choice="research"),
        _choice(choice=True),
        _choice(confidence=1.1),
        _choice(type="predicate"),
        _choice(extra=None),
        _choice(probabilities={"code": 0.9, "research": 0.06, "no_fit": 0.04}),
        _choice(probabilities=[{"value": "code", "probability": 0.9}, {"value": "research", "probability": 0.1}]),
        _choice(
            probabilities=[
                {"value": "code", "probability": 0.9},
                {"value": "code", "probability": 0.06},
                {"value": "no_fit", "probability": 0.04},
            ],
        ),
        _choice(
            probabilities=[
                {"value": "code", "probability": 0.9},
                {"value": True, "probability": 0.06},
                {"value": "no_fit", "probability": 0.04},
            ],
        ),
        _choice(
            probabilities=[
                {"value": "code", "probability": 0.9},
                {"value": "research", "probability": 0.06, "label": "Research"},
                {"value": "no_fit", "probability": 0.04},
            ],
        ),
        _choice(
            probabilities=[
                {"value": "code", "probability": 0.9},
                {"value": "research", "probability": 0.5},
                {"value": "no_fit", "probability": 0.04},
            ],
        ),
    ],
)
async def test_malformed_choices_fail_closed(answer: dict[str, Any]) -> None:
    """An unrequested, inconsistent, or mistyped choice must never select a responder."""
    result = await _judge_choice(_body(answer))
    assert result.failure == "invalid_response"
    assert result.decision is None


async def test_evaluators_use_the_shared_openai_credential(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The backend authenticates with the OpenAI key MindRoom models use and is skipped without one."""
    paths = test_runtime_paths(tmp_path)
    settings = OpenAIDecisionsJudgmentConfig(provider="openai_decisions", threshold=0.7, timeout_seconds=2)
    assert create_judgment_evaluator(settings, Config(), paths, owner="agent", question_id="simple_task") is None
    assert create_choice_evaluator(settings, paths, owner="router", question_id="responder") is None

    get_runtime_shared_credentials_manager(paths).save_credentials("openai", {"api_key": "stored-openai-key"})
    calls: list[tuple[str, str]] = []
    bodies = iter((_body(_predicate(0.75)), _body(_choice())))

    async def post(self: JudgmentClient, _body: bytes) -> bytes:
        calls.append((self._api_key, self._wire.endpoint))
        return json.dumps(next(bodies)).encode()

    monkeypatch.setattr(JudgmentClient, "_post", post)
    evaluate = create_judgment_evaluator(settings, Config(), paths, owner="agent", question_id="simple_task")
    choose = create_choice_evaluator(settings, paths, owner="router", question_id="responder")
    assert evaluate is not None
    assert choose is not None

    assert (await evaluate(_request())).decision is True
    chosen = await choose(_request(_CHOICE))
    assert chosen.decision is not None
    assert chosen.decision.option == "code"
    assert calls == [("stored-openai-key", _ENDPOINT)] * 2

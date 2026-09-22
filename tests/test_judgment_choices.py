"""Choice judgments validate one bounded comparative decision across backends."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import httpx
import pytest
from agno.models.response import ModelResponse

from mindroom import model_loading
from mindroom.config.judgment import LLMJudgmentConfig
from mindroom.config.main import Config
from mindroom.judgment.client import PINNED_MODEL, SystemOneClient
from mindroom.judgment.llm import judge_choice_with_llm
from mindroom.judgment.state import ChoiceQuestion, JudgmentMessage, build_judgment_request
from tests.conftest import test_runtime_paths
from tests.participation_helpers import ParticipationModel

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.judgment.answers import ChoiceDecision, JudgmentResult
    from mindroom.judgment.state import JudgmentRequest


def _request() -> JudgmentRequest:
    return build_judgment_request(
        ChoiceQuestion(
            id="responder",
            instructions="Choose the best responder.",
            options=(("code", "Programming"), ("research", "Research"), ("no_fit", "No suitable responder")),
        ),
        (JudgmentMessage("user", "Fix this Python function."),),
        instructions="Prefer a single capable responder.",
    )


def _answer(**overrides: object) -> dict:
    return {
        "type": "choice",
        "choice": "code",
        "confidence": 0.95,
        "probabilities": {"code": 0.9, "research": 0.06, "no_fit": 0.04},
        **overrides,
    }


async def _judge(answer: dict) -> JudgmentResult[ChoiceDecision]:
    def respond(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        question = payload["questions"]["responder"]
        assert question["type"] == "choice"
        assert question["criteria"] == {
            "code": "Programming",
            "research": "Research",
            "no_fit": "No suitable responder",
        }
        return httpx.Response(
            200,
            content=json.dumps(
                {
                    "model": PINNED_MODEL,
                    "answers": {"responder": answer},
                    "usage": {"input_tokens": 42, "output_tokens": 1},
                },
            ).encode(),
        )

    return await SystemOneClient(
        api_key="synthetic",
        model=PINNED_MODEL,
        transport=httpx.MockTransport(respond),
    ).judge_choice(_request(), owner="router", allow_network=True)


@pytest.mark.asyncio
async def test_choice_retains_validated_distribution_and_usage() -> None:
    """Choice retains validated distribution and usage."""
    result = await _judge(_answer())
    assert result.failure is None
    assert result.decision is not None
    assert result.decision.option == "code"
    assert result.decision.confidence == 0.95
    assert dict(result.decision.probabilities) == {"code": 0.9, "research": 0.06, "no_fit": 0.04}
    assert result.probability == 0.9
    assert result.input_tokens == 42


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "answer",
    [
        _answer(choice="unknown"),
        _answer(choice="research"),
        _answer(confidence=True),
        _answer(confidence=1.1),
        _answer(confidence=float("nan")),
        _answer(probabilities={"code": float("inf"), "research": 0.1, "no_fit": 0.0}),
        _answer(probabilities={"code": 0.9, "research": 0.1}),
        _answer(probabilities={"code": 0.9, "research": 0.1, "no_fit": 0.5}),
        _answer(probabilities={"code": True, "research": 0.0, "no_fit": 0.0}),
        _answer(probabilities={"code": 1.1, "research": -0.1, "no_fit": 0.0}),
        _answer(extra="unexpected"),
        _answer(type="noul"),
    ],
)
async def test_malformed_choices_fail_closed(answer: dict) -> None:
    """Malformed choices fail closed."""
    result = await _judge(answer)
    assert result.failure == "invalid_response"
    assert result.decision is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "answer",
    [
        _answer(confidence=0.2),
        _answer(probabilities={"code": 0.6, "research": 0.3, "no_fit": 0.1}),
    ],
)
async def test_uncertain_choices_abstain(answer: dict) -> None:
    """Uncertain choices abstain."""
    result = await _judge(answer)
    assert result.failure is None
    assert result.decision is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("content", "expected", "failure"),
    [
        ('{"decision":"code"}', "code", None),
        ('{"decision":"no_fit"}', "no_fit", None),
        ('{"decision":null}', None, None),
        ('{"decision":true}', None, "invalid_response"),
        ('{"decision":"unknown"}', None, "invalid_response"),
        ('{"decision":"code","confidence":0.9}', None, "invalid_response"),
        ('{"decision":"code","decision":"research"}', None, "invalid_response"),
    ],
)
async def test_llm_choice_is_allowlisted_without_invented_confidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    content: str,
    expected: str | None,
    failure: str | None,
) -> None:
    """Llm choice is allowlisted without invented confidence."""
    model = ParticipationModel(ModelResponse(content=content))
    monkeypatch.setattr(model_loading, "get_model_instance", lambda *_: model)
    result = await judge_choice_with_llm(
        _request(),
        LLMJudgmentConfig(provider="llm", model="default"),
        Config(),
        test_runtime_paths(tmp_path),
        owner="router",
    )
    assert result.failure == failure
    assert (result.decision.option if result.decision else None) == expected
    if result.decision:
        assert result.decision.confidence is None
        assert result.decision.probabilities == ()
    assert model.requests[0]["tools"] == []
    assert model.requests[0]["tool_choice"] == "none"


@pytest.mark.parametrize(
    "options",
    [
        (),
        (("code", "one"), ("code", "two")),
        (("", "blank"),),
        (("code", "token=sk-secret"),),
        (("code", "x" * 16000),),
        tuple((str(i), "option") for i in range(256)),
    ],
)
def test_incomplete_choice_criteria_never_produce_wire_bytes(options: tuple[tuple[str, str], ...]) -> None:
    """Incomplete choice criteria never produce wire bytes."""
    request = build_judgment_request(
        ChoiceQuestion("responder", "Choose", options),
        (JudgmentMessage("user", "Help"),),
        instructions="",
    )
    assert not request.complete
    assert request.body is None

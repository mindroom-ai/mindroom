"""The same bounded rubric works through independent judgment backends."""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING

import httpx
import pytest
from agno.metrics import MessageMetrics
from agno.models.response import ModelResponse
from groq import AsyncGroq

from mindroom import model_loading
from mindroom.config.judgment import LLMJudgmentConfig
from mindroom.config.main import Config
from mindroom.groq_model import MindRoomGroq
from mindroom.judgment.client import PINNED_MODEL, SystemOneClient
from mindroom.judgment.llm import judge_with_llm
from mindroom.judgment.state import JudgmentMessage, JudgmentQuestion, build_judgment_request
from mindroom.provider_tool_policy import provider_tools_disabled
from tests.conftest import test_runtime_paths
from tests.participation_helpers import ParticipationModel

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.judgment.state import JudgmentRequest


def _request() -> JudgmentRequest:
    return build_judgment_request(
        JudgmentQuestion(
            id="simple_task",
            instructions="Can the configured cheaper model handle this task?",
            when_true="Routine text transformation with complete input.",
            when_false="Complex reasoning, missing information, or unsupported tools.",
        ),
        (JudgmentMessage("user", "Alphabetize these words: pear, apple."),),
        instructions="Judge only the supplied task.",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("decision", [True, False])
async def test_backends_share_rubric_context_and_normalized_decision(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    decision: bool,
) -> None:
    """A second task must work without participation-specific decoding or different evidence."""
    posted: list[dict] = []
    score = 0.9 if decision else 0.1

    def respond(request: httpx.Request) -> httpx.Response:
        posted.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "model": PINNED_MODEL,
                "answers": {"simple_task": {"type": "noul", "noul": score}},
                "usage": {"input_tokens": 20, "output_tokens": 1},
            },
        )

    judge = ParticipationModel(
        ModelResponse(
            content=json.dumps({"decision": decision}),
            response_usage=MessageMetrics(input_tokens=25, output_tokens=3),
        ),
    )
    monkeypatch.setattr(model_loading, "get_model_instance", lambda *_: judge)
    request = _request()
    llm = await judge_with_llm(
        request,
        LLMJudgmentConfig(provider="llm", model="cheap"),
        Config(),
        test_runtime_paths(tmp_path),
        owner="llm",
    )
    typesafe = await SystemOneClient(
        api_key="synthetic",
        model=PINNED_MODEL,
        transport=httpx.MockTransport(respond),
    ).judge(request, owner="typesafe", allow_network=True)

    assert llm.decision is typesafe.decision is decision
    assert llm.failure is typesafe.failure is None
    assert llm.input_tokens == 25
    assert llm.output_tokens == 3
    assert typesafe.input_tokens == 20
    assert typesafe.output_tokens == 1
    assert llm.probability is None
    assert typesafe.probability == score
    evidence = json.loads(judge.requests[0]["messages"][1].content)
    assert posted[0]["state"] == evidence["state"]
    rubric = posted[0]["questions"]["simple_task"]
    assert rubric["instructions"]["question"] == evidence["question"]["instructions"]
    assert rubric["criteria"] == evidence["question"]["criteria"]
    assert rubric["instructions"]["guidance"] == evidence["guidance"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "content",
    [
        '{"decision": 1}',
        '{"decision": "true"}',
        '{"decision": NaN}',
        '{"decision": true, "decision": false}',
        '{"decision": true, "confidence": 0.9}',
        "not JSON",
        "[true]",
        '{"decision": true} {"decision": false}',
        "\ud800",
    ],
)
async def test_llm_rejects_ambiguous_or_untyped_decisions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    content: str,
) -> None:
    """Malformed decisions must abstain rather than truthily approving a turn."""
    judge = ParticipationModel(ModelResponse(content=content))
    monkeypatch.setattr(model_loading, "get_model_instance", lambda *_: judge)
    result = await judge_with_llm(
        _request(),
        LLMJudgmentConfig(provider="llm", model="cheap"),
        Config(),
        test_runtime_paths(tmp_path),
        owner="llm",
    )
    assert result.decision is None
    assert result.probability is None
    assert result.failure == "invalid_response"


@pytest.mark.asyncio
async def test_backends_share_capacity_and_cancellation_releases_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Switching backends cannot bypass an owner's in-flight judgment limit."""
    entered = asyncio.Event()

    async def respond(_request: httpx.Request) -> httpx.Response:
        entered.set()
        await asyncio.Event().wait()
        raise AssertionError

    request = _request()
    client = SystemOneClient(api_key="synthetic", model=PINNED_MODEL, transport=httpx.MockTransport(respond))
    task = asyncio.create_task(client.judge(request, owner="shared-owner", allow_network=True))
    await entered.wait()
    judge = ParticipationModel(ModelResponse(content='{"decision": true}'))
    monkeypatch.setattr(model_loading, "get_model_instance", lambda *_: judge)
    try:
        full = await judge_with_llm(
            request,
            LLMJudgmentConfig(provider="llm", model="cheap"),
            Config(),
            test_runtime_paths(tmp_path),
            owner="shared-owner",
        )
        assert full.failure == "capacity_exhausted"
        assert judge.requests == []
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    recovered = await judge_with_llm(
        request,
        LLMJudgmentConfig(provider="llm", model="cheap"),
        Config(),
        test_runtime_paths(tmp_path),
        owner="shared-owner",
    )
    assert recovered.decision is True


@pytest.mark.asyncio
async def test_llm_native_tool_mode_is_refused_before_network(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A dedicated judge cannot execute automatic provider tools."""
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(500)

    judge = MindRoomGroq(
        id="groq/compound",
        async_client=AsyncGroq(
            api_key="synthetic",
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(respond)),
        ),
    )
    monkeypatch.setattr(model_loading, "get_model_instance", lambda *_: judge)
    try:
        result = await judge_with_llm(
            _request(),
            LLMJudgmentConfig(provider="llm", model="cheap"),
            Config(),
            test_runtime_paths(tmp_path),
            owner="llm",
        )
    finally:
        await judge.async_client.close()
    assert result.decision is None
    assert result.failure == "provider_error"
    assert requests == []
    assert not provider_tools_disabled()

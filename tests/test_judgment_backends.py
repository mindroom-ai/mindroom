"""The same bounded rubric works through independent judgment backends."""

from __future__ import annotations

import asyncio
import json
from threading import Event
from typing import TYPE_CHECKING

import httpx
import pytest
from google.oauth2.credentials import Credentials

from mindroom.config.judgment import LLMJudgmentConfig
from mindroom.config.main import Config
from mindroom.config.models import ModelConfig
from mindroom.judgment import llm as judgment_llm
from mindroom.judgment.client import JudgmentClient
from mindroom.judgment.execution import SHARED_CAPACITY
from mindroom.judgment.llm import judge_with_llm
from mindroom.judgment.state import JudgmentMessage, JudgmentQuestion, build_judgment_request
from mindroom.judgment.typesafe import _PINNED_MODEL, SYSTEM_ONE
from mindroom.llm.errors import ProviderError
from mindroom.providers.model_http_client import MODEL_HTTP_CLIENT
from mindroom.providers.tool_policy import provider_tools_disabled
from tests.conftest import test_runtime_paths
from tests.fakes.scripted_model import Await, ScriptedModel, answer, calls, tool_call, usage
from tests.gemini_helpers import gemini_decision_response

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from mindroom.judgment.state import JudgmentRequest
    from tests.fakes.scripted_model import ScriptStep


def _judge(monkeypatch: pytest.MonkeyPatch, *turn: ScriptStep) -> ScriptedModel:
    """Serve the judgment model from one scripted turn and return it, so tests read the request it received."""
    judge = ScriptedModel([list(turn)])
    monkeypatch.setattr(judgment_llm, "build_model_provider", lambda *_: judge)
    return judge


def _decision(content: str, *, input_tokens: int = 0, output_tokens: int = 0) -> list[ScriptStep]:
    return answer(content, usage(input_tokens, output_tokens))


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
                "model": _PINNED_MODEL,
                "answers": {"simple_task": {"type": "noul", "noul": score}},
                "usage": {"input_tokens": 20, "output_tokens": 1},
            },
        )

    judge = _judge(monkeypatch, *_decision(json.dumps({"decision": decision}), input_tokens=25, output_tokens=3))
    request = _request()
    llm = await judge_with_llm(
        request,
        LLMJudgmentConfig(provider="llm", model="cheap"),
        Config(),
        test_runtime_paths(tmp_path),
        owner="llm",
    )
    typesafe = await JudgmentClient(
        api_key="synthetic",
        wire=SYSTEM_ONE,
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
    evidence = json.loads(judge.requests[0].messages[1].get_content_string())
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
    _judge(monkeypatch, *_decision(content))
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
    client = JudgmentClient(api_key="synthetic", wire=SYSTEM_ONE, transport=httpx.MockTransport(respond))
    task = asyncio.create_task(client.judge(request, owner="shared-owner", allow_network=True))
    await entered.wait()
    judge = _judge(monkeypatch, *_decision('{"decision": true}'))
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


def _gemini_config(respond: Callable[[httpx.Request], httpx.Response], *, vertexai: bool) -> Config:
    """Return a config whose ``cheap`` model is Gemini 3.8 Flash answering through ``respond``."""
    extra_kwargs: dict[str, object] = {
        "client_params": {
            "http_options": {"httpx_async_client": httpx.AsyncClient(transport=httpx.MockTransport(respond))},
        },
    }
    if vertexai:
        extra_kwargs |= {
            "vertexai": True,
            "project_id": "test-project",
            "location": "us-central1",
            "credentials": Credentials(token="test-token"),  # noqa: S106 - synthetic SDK credential
        }
    model = ModelConfig(
        provider="google",
        id="gemini-3.8-flash",
        api_key=None if vertexai else "test-key",
        extra_kwargs=extra_kwargs,
    )
    return Config(models={"cheap": model})


@pytest.mark.asyncio
@pytest.mark.parametrize("vertexai", [False, True], ids=["gemini_api", "vertex_ai"])
async def test_llm_gemini_judgment_requires_json_output(tmp_path: Path, vertexai: bool) -> None:
    """Gemini can call functions without declarations, which would discard a valid judgment."""
    requests: list[dict[str, object]] = []

    def respond(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        requests.append(payload)
        return gemini_decision_response(payload, '{"decision": true}', leaked_call={"name": "skills_list", "args": {}})

    result = await judge_with_llm(
        _request(),
        LLMJudgmentConfig(provider="llm", model="cheap"),
        _gemini_config(respond, vertexai=vertexai),
        test_runtime_paths(tmp_path),
        owner="llm",
    )
    assert result.decision is True
    assert result.failure is None
    assert "tools" not in requests[0]
    assert "toolConfig" not in requests[0]
    assert requests[0]["generationConfig"] == {"responseMimeType": "application/json"}


@pytest.mark.asyncio
async def test_llm_native_tool_mode_is_refused_before_network(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A dedicated judge cannot execute automatic provider tools."""
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(500)

    monkeypatch.setattr(MODEL_HTTP_CLIENT, "send", httpx.AsyncClient(transport=httpx.MockTransport(respond)).send)
    config = Config(models={"cheap": ModelConfig(provider="groq", id="groq/compound", api_key="synthetic")})
    result = await judge_with_llm(
        _request(),
        LLMJudgmentConfig(provider="llm", model="cheap"),
        config,
        test_runtime_paths(tmp_path),
        owner="llm",
    )
    assert result.decision is None
    assert result.failure == "provider_error"
    assert requests == []
    assert not provider_tools_disabled()


@pytest.mark.asyncio
async def test_llm_decision_request_offers_no_tools_and_disables_provider_tools(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The decision is one non-streaming request with tool choice none, sent while provider tools are disabled."""
    disabled: list[bool] = []

    async def observe() -> None:
        disabled.append(provider_tools_disabled())

    judge = _judge(monkeypatch, Await(observe), *_decision('{"decision": true}', input_tokens=9, output_tokens=1))
    result = await judge_with_llm(
        _request(),
        LLMJudgmentConfig(provider="llm", model="cheap"),
        Config(),
        test_runtime_paths(tmp_path),
        owner="llm",
    )

    assert (result.decision, result.model_id, result.input_tokens, result.output_tokens) == (
        True,
        "scripted-model",
        9,
        1,
    )
    assert disabled == [True]
    assert not provider_tools_disabled()
    [request] = judge.requests
    assert (request.tools, request.tool_choice, request.stream) == ((), "none", False)
    assert [message.role for message in request.messages] == ["system", "user"]


@pytest.mark.asyncio
async def test_llm_without_reported_usage_leaves_the_counts_unknown(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A provider that reports no usage leaves the token counts unknown instead of zero."""
    _judge(monkeypatch, *_decision('{"decision": false}'))
    result = await judge_with_llm(
        _request(),
        LLMJudgmentConfig(provider="llm", model="cheap"),
        Config(),
        test_runtime_paths(tmp_path),
        owner="llm",
    )
    assert (result.decision, result.input_tokens, result.output_tokens) == (False, None, None)


async def _fail_judge() -> None:
    msg = "Provider failed"
    raise ProviderError(msg, provider="Synthetic")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("turn", "failure"),
    [
        (calls(tool_call("unsafe", "call"), content='{"decision": true}'), "invalid_response"),
        ([Await(_fail_judge)], "provider_error"),
    ],
    ids=["tool_call", "provider_error"],
)
async def test_llm_tool_calls_and_provider_errors_abstain(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    turn: list[ScriptStep],
    failure: str,
) -> None:
    """A judge that calls a tool or fails decides nothing."""
    _judge(monkeypatch, *turn)
    result = await judge_with_llm(
        _request(),
        LLMJudgmentConfig(provider="llm", model="cheap"),
        Config(),
        test_runtime_paths(tmp_path),
        owner="llm",
    )
    assert (result.decision, result.failure) == (None, failure)


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [False, True])
@pytest.mark.parametrize("loader_fails", [False, True])
async def test_abandoned_model_load_retains_capacity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    cancel: bool,
    loader_fails: bool,
) -> None:
    """Timeouts and cancellation cannot admit more work while a loader still runs."""
    entered = asyncio.Event()
    finished = asyncio.Event()
    release = Event()
    loop = asyncio.get_running_loop()
    judge = ScriptedModel([_decision('{"decision": true}')])

    def load(*_: object) -> ScriptedModel:
        loop.call_soon_threadsafe(entered.set)
        try:
            assert release.wait(timeout=5)
            if loader_fails:
                msg = "late loader failure"
                raise RuntimeError(msg)
            return judge
        finally:
            loop.call_soon_threadsafe(finished.set)

    monkeypatch.setattr(judgment_llm, "build_model_provider", load)
    monkeypatch.setattr(SHARED_CAPACITY, "_max_concurrent", 1)
    settings = LLMJudgmentConfig(provider="llm", model="cheap", timeout_seconds=0.1 if not cancel else 5)
    task = asyncio.create_task(
        judge_with_llm(_request(), settings, Config(), test_runtime_paths(tmp_path), owner="loading"),
    )
    try:
        await asyncio.wait_for(entered.wait(), timeout=2)
        if cancel:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, timeout=1)
        else:
            assert (await asyncio.wait_for(task, timeout=1)).failure == "timeout"
        client = JudgmentClient(
            api_key="synthetic",
            wire=SYSTEM_ONE,
            transport=httpx.MockTransport(lambda _: httpx.Response(500)),
        )
        for owner in ("loading", "another-owner"):
            blocked = await client.judge(_request(), owner=owner, allow_network=True)
            assert blocked.failure == "capacity_exhausted"
        assert not finished.is_set()
        assert judge.requests == []
    finally:
        release.set()
        await asyncio.wait_for(finished.wait(), timeout=2)
        if not task.done():
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

    monkeypatch.setattr(judgment_llm, "build_model_provider", lambda *_: judge)
    async with asyncio.timeout(2):
        while True:
            recovered = await judge_with_llm(
                _request(),
                settings,
                Config(),
                test_runtime_paths(tmp_path),
                owner="loading",
            )
            if recovered.failure != "capacity_exhausted":
                break
            await asyncio.sleep(0)
    assert recovered.decision is True
    assert len(judge.requests) == 1

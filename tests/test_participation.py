"""Same-model participation decisions must never execute tools or alter reply inputs."""

from __future__ import annotations

import asyncio
import json
from copy import deepcopy
from dataclasses import replace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, Mock

import httpx
from agno.agent import Agent
from agno.compression.manager import CompressionManager
from agno.metrics import MessageMetrics, RunMetrics
from agno.run.agent import RunOutput
from openai import AsyncOpenAI

from mindroom.agno_participation import participation_model
from mindroom.ai import ai_response, stream_agent_response
from mindroom.google_gemini import MindRoomGoogleGemini
from mindroom.openai_models import MindRoomOpenAIChat
from mindroom.participation import ParticipationGate
from tests.ai_user_id_helpers import _config, _prepared_prompt_result, _runtime_paths
from tests.conftest import make_turn_context
from tests.gemini_helpers import gemini_client, gemini_decision_response, gemini_response
from tests.participation_helpers import ParticipationModel

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path
    from typing import Any

import pytest
from agno.models.message import Message
from agno.models.response import ModelResponse
from agno.run.agent import RunContentEvent
from agno.tools.function import Function


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
async def test_decision_cannot_execute_tools(stream: bool) -> None:
    """Bypassing the provider-only decision boundary would execute the supplied tool."""
    executed: list[str] = []

    def operate() -> str:
        executed.append("operated")
        return "done"

    model = ParticipationModel(
        ModelResponse(
            tool_calls=[{"id": "call-1", "type": "function", "function": {"name": "operate", "arguments": "{}"}}],
        ),
    )
    gate = ParticipationGate()
    messages = [Message(role="user", content="Thanks, everyone.")]
    with participation_model(model, gate, run_id="primary"):
        if stream:
            result = [
                chunk
                async for chunk in model.aresponse_stream(
                    messages,
                    tools=[Function.from_callable(operate)],
                    run_response=RunOutput(run_id="primary"),
                )
            ]
            assert all(not chunk.content for chunk in result)
        else:
            result = await model.aresponse(
                messages,
                tools=[Function.from_callable(operate)],
                run_response=RunOutput(run_id="primary"),
            )
            assert not result.content
    assert executed == []
    assert len(model.requests) == 1
    assert gate.is_silent


@pytest.mark.asyncio
async def test_approval_reuses_exact_normal_prefix_and_only_checks_once() -> None:
    """Rebuilding tools/system or checking every continuation breaks this invariant."""
    model = ParticipationModel(
        ModelResponse(content='{"action":"respond","reason":"A factual question is unanswered."}'),
    )
    gate = ParticipationGate(instructions="Offer concise technical help.")
    messages = [
        Message(role="system", content="You are a helpful agent."),
        Message(role="user", content="Can anyone explain this?"),
    ]
    original = deepcopy(messages)
    tools = [{"type": "function", "function": {"name": "lookup", "parameters": {"type": "object", "properties": {}}}}]
    original_response = model.ainvoke
    with participation_model(model, gate, run_id="primary"):
        response = await model.aresponse(
            messages,
            tools=tools,
            tool_choice="auto",
            run_response=RunOutput(run_id="primary"),
        )
        assert response.content == "Useful answer"
        await model.aresponse(
            [Message(role="user", content="Continue")],
            tools=tools,
            tool_choice="auto",
            run_response=RunOutput(run_id="primary"),
        )
    decision, normal, _continuation = model.requests
    assert decision["messages"][:-1] == original
    assert normal["messages"] == original
    assert decision["tools"] == normal["tools"] == tools
    assert decision["tool_choice"] == "none"
    assert normal["tool_choice"] == "auto"
    assert decision["response_format"] == normal["response_format"]
    assert "Offer concise technical help." in decision["messages"][-1].content
    assert len(model.requests) == 3
    assert gate.approved
    assert gate.decided.is_set()
    assert model.ainvoke == original_response


@pytest.mark.asyncio
async def test_openai_decision_disables_function_selection_before_answering() -> None:
    """A decision containing a selected function would discard an otherwise useful answer."""
    requests: list[dict[str, Any]] = []
    executions: list[tuple[int, int]] = []

    def multiply(a: int, b: int) -> str:
        """Multiply two integers."""
        executions.append((a, b))
        return str(a * b)

    def provider(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        requests.append(payload)
        deciding = len(requests) == 1
        message: dict[str, Any] = {
            "role": "assistant",
            "content": '{"action":"respond","reason":"An unanswered math question."}' if deciding else "437",
        }
        finish_reason = "stop"
        if (deciding and payload.get("tool_choice") != "none") or len(requests) == 2:
            message["tool_calls"] = [
                {
                    "id": "call-multiply",
                    "type": "function",
                    "function": {"name": "multiply", "arguments": '{"a":23,"b":19}'},
                },
            ]
            finish_reason = "tool_calls"
            if not deciding:
                message["content"] = None
        return httpx.Response(
            200,
            json={
                "id": "completion",
                "object": "chat.completion",
                "created": 1,
                "model": "test",
                "choices": [{"index": 0, "message": message, "finish_reason": finish_reason}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as http_client:
        model = MindRoomOpenAIChat(
            id="test",
            api_key="test-key",
            async_client=AsyncOpenAI(api_key="test-key", http_client=http_client),
        )
        gate = ParticipationGate()
        with participation_model(model, gate, run_id="primary"):
            response = await model.aresponse(
                [Message(role="user", content="Use the calculator to multiply 23 by 19.")],
                tools=[Function.from_callable(multiply)],
                tool_choice="auto",
                run_response=RunOutput(run_id="primary"),
            )

    assert response.content == "437"
    assert gate.approved
    assert executions == [(23, 19)]
    assert len(requests) == 3
    assert requests[0]["tool_choice"] == "none"
    assert requests[1]["tool_choice"] == "auto"
    assert requests[0]["tools"] == requests[1]["tools"]
    assert requests[0]["messages"][:-1] == requests[1]["messages"]


@pytest.mark.asyncio
async def test_gemini_decision_requests_reason_first_json_while_keeping_declarations() -> None:
    """Gemini can call functions under mode NONE, which would silence an otherwise useful answer."""
    requests: list[dict[str, Any]] = []
    executions: list[tuple[int, int]] = []
    multiply_call = {"name": "multiply", "args": {"a": 23, "b": 19}}

    def multiply(a: int, b: int) -> str:
        """Multiply two integers."""
        executions.append((a, b))
        return str(a * b)

    def provider(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        requests.append(payload)
        if len(requests) == 1:
            return gemini_decision_response(
                payload,
                '{"reason":"An unanswered math question.","action":"respond"}',
                leaked_call=multiply_call,
            )
        if len(requests) == 2:
            return gemini_response({"functionCall": multiply_call})
        return gemini_response({"text": "437"})

    gate = ParticipationGate()
    async with gemini_client(provider, vertexai=False) as client:
        model = MindRoomGoogleGemini(id="test", client=client)
        with participation_model(model, gate, run_id="primary"):
            response = await model.aresponse(
                [Message(role="user", content="Use the calculator to multiply 23 by 19.")],
                tools=[Function.from_callable(multiply)],
                tool_choice="auto",
                run_response=RunOutput(run_id="primary"),
            )

    assert gate.approved
    assert response.content == "437"
    assert executions == [(23, 19)]
    decision, primary, _continuation = requests
    assert decision["tools"] == primary["tools"]
    assert decision["tools"][0]["functionDeclarations"][0]["name"] == "multiply"
    assert decision["toolConfig"] == {"functionCallingConfig": {"mode": "NONE"}}
    assert primary["toolConfig"] == {"functionCallingConfig": {"mode": "AUTO"}}
    output_schema = decision["generationConfig"]["responseJsonSchema"]
    assert decision["generationConfig"]["responseMimeType"] == "application/json"
    # Constrained decoding that commits to the action first biased small Gemini models toward silence.
    assert list(output_schema["properties"]) == ["reason", "action"]
    assert output_schema["properties"]["action"]["enum"] == ["respond", "stay_silent"]
    assert "responseMimeType" not in primary.get("generationConfig", {})
    assert "responseJsonSchema" not in primary.get("generationConfig", {})
    # Gemini merges the appended decision prompt into the final user turn.
    assert decision["contents"][0]["parts"][:-1] == primary["contents"][0]["parts"]


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["respond", "stay_silent"])
@pytest.mark.parametrize(
    ("prefix", "suffix"),
    [
        ("", ""),
        ("My decision: ", ""),
        ("", " That is my decision."),
        ("Before.\n", "\nAfter."),
        ("```json\n", "\n```"),
        ("Before.\n```json\n", "\n```\nAfter."),
        ("[Decision]\n", ""),
        ("", " See [context](https://example.com)."),
        ("Some {informal notation}. ", ""),
    ],
)
async def test_single_decision_allows_surrounding_text(action: str, prefix: str, suffix: str) -> None:
    """Prose and fences must not hide a single valid verdict or leak into the answer."""
    reason = 'An unanswered question about {"key": "value"} needs help.'
    content = prefix + json.dumps({"action": action, "reason": reason}) + suffix
    model = ParticipationModel(ModelResponse(content=content))
    gate = ParticipationGate()
    with participation_model(model, gate, run_id="primary"):
        result = await model.aresponse(
            [Message(role="user", content="Can anyone explain this?")],
            run_response=RunOutput(run_id="primary"),
        )
    assert gate.decision is not None
    assert gate.decision.action == action
    assert gate.decision.reason == reason
    assert (result.content or "") == ("Useful answer" if action == "respond" else "")
    assert len(model.requests) == (2 if action == "respond" else 1)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "content",
    [
        '{"action":"respond","reason":"Help."}{"action":"stay_silent","reason":"Wait."}',
        '{"action":"respond","reason":"Help."} More text. {"action":"respond","reason":"Help."}',
        'An example: {}. Decision: {"action":"respond","reason":"Help."}',
        'Before. [{"action":"respond","reason":"Help."}] After.',
        'Before. {"decision":{"action":"respond","reason":"Help."}} After.',
        '[{"action":"respond","reason":"Help."}',
        '{"decision":{"action":"respond","reason":"Help."}',
        '[1, {"action":"respond","reason":"Help."}',
        '[null, {"action":"respond","reason":"Help."}',
        'Before. {"action":"respond","reason":"Help.","extra":true} After.',
        'Before. {"action":"respond"} After.',
        'Before. {"action":"maybe","reason":"Help."} After.',
        'Before. {"action":"respond","reason":"Help." After.',
    ],
)
async def test_ambiguous_or_invalid_embedded_decisions_stay_quiet(content: str) -> None:
    """Extraction must not pick a verdict from multiple objects or bypass schema validation."""
    model = ParticipationModel(ModelResponse(content=content))
    gate = ParticipationGate()
    with participation_model(model, gate, run_id="primary"):
        result = await model.aresponse(
            [Message(role="user", content="Can anyone explain this?")],
            run_response=RunOutput(run_id="primary"),
        )
    assert not result.content
    assert gate.is_silent
    assert len(model.requests) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "content",
    [
        '{"action":"stay_silent","action":"respond","reason":"Wait."}',
        '{"action":"respond","action":"stay_silent","reason":"Wait."}',
        '{"action":"respond","action":"respond","reason":"Help."}',
        '{"action":"respond","reason":"Wait.","reason":"Help."}',
        '{"action":"stay_silent","\\u0061ction":"respond","reason":"Wait."}',
    ],
)
async def test_duplicate_decision_keys_stay_quiet(content: str) -> None:
    """Duplicate keys must fail validation regardless of order, value, or escaping."""
    model = ParticipationModel(ModelResponse(content=f"Before.\n```json\n{content}\n```\nAfter."))
    gate = ParticipationGate()
    with participation_model(model, gate, run_id="primary"):
        result = await model.aresponse(
            [Message(role="user", content="Can anyone explain this?")],
            run_response=RunOutput(run_id="primary"),
        )
    assert not result.content
    assert gate.is_silent
    assert gate.decision is not None
    assert gate.decision.reason == "decision_failed"
    assert not gate.is_declined
    assert len(model.requests) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "decision",
    [
        ModelResponse(content='{"action":"stay_silent","reason":"Humans are discussing plans."}'),
        ModelResponse(content="Sure!"),
        ModelResponse(content='{"action":"respond"}'),
        ModelResponse(content='{"action":"error","reason":"Not a model verdict."}'),
        RuntimeError("provider unavailable"),
    ],
)
async def test_invalid_failed_or_declined_decision_stays_quiet(decision: ModelResponse | Exception) -> None:
    """An untrusted or failed decision must not authorize an ambient reply."""
    model = ParticipationModel(decision)
    gate = ParticipationGate()
    with participation_model(model, gate, run_id="primary"):
        result = await model.aresponse(
            [Message(role="user", content="I agree.")],
            run_response=RunOutput(run_id="primary"),
        )
    assert not result.content
    assert len(model.requests) == 1
    assert gate.is_silent
    assert gate.is_declined == (isinstance(decision, ModelResponse) and '"stay_silent"' in (decision.content or ""))


@pytest.mark.asyncio
async def test_cancellation_is_not_converted_to_silence() -> None:
    """Stopping a deciding turn must retain normal cancellation/recovery semantics."""

    class CancelledModel(ParticipationModel):
        async def ainvoke(
            self,
            messages: list[Message],  # noqa: ARG002
            **_kwargs: object,
        ) -> ModelResponse:
            raise asyncio.CancelledError

    model = CancelledModel(ModelResponse())
    original = model.ainvoke
    with pytest.raises(asyncio.CancelledError), participation_model(model, ParticipationGate(), run_id="primary"):
        await model.aresponse([Message(role="user", content="Question")], run_response=RunOutput(run_id="primary"))
    assert model.ainvoke == original


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("action", ["respond", "stay_silent"])
@pytest.mark.parametrize("cached", [False, True])
async def test_agent_turn_applies_decision_before_model_answer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    streaming: bool,
    action: str,
    cached: bool,
) -> None:
    """Both AI adapters must gate the actual Agno run and suppress decision text."""
    model = ParticipationModel(ModelResponse(content='{"action":"' + action + '","reason":"Conversation context."}'))
    model.cache_response = cached
    monkeypatch.setattr(
        model,
        "_get_cached_model_response",
        Mock(
            return_value={
                "result": {"content": "Cached answer"},
                "streaming_responses": [{"content": "Cached answer"}],
            },
        ),
    )
    agent = Agent(model=model, name="general", telemetry=False)
    monkeypatch.setattr("mindroom.ai._prepare_agent_and_prompt", AsyncMock(return_value=_prepared_prompt_result(agent)))
    ctx = replace(
        make_turn_context("general", session_id="session-1", run_id="run-1"),
        participation=ParticipationGate(),
    )
    kwargs = {
        "prompt": "test prompt",
        "runtime_paths": _runtime_paths(tmp_path),
        "config": _config(),
        "show_tool_calls": False,
    }
    if streaming:
        chunks = [chunk async for chunk in stream_agent_response(ctx, **kwargs)]
        result = "".join(
            chunk if isinstance(chunk, str) else str(chunk.content or "")
            for chunk in chunks
            if isinstance(chunk, str | RunContentEvent)
        )
    else:
        result = await ai_response(ctx, **kwargs)
    assert result == ("Useful answer" if action == "respond" else "")
    assert len(model.requests) == (2 if action == "respond" else 1)
    assert model.cache_response is cached


@pytest.mark.asyncio
async def test_decision_usage_is_included_in_run_metrics() -> None:
    """The extra model request must not disappear from recorded token usage."""
    model = ParticipationModel(
        ModelResponse(
            content='{"action":"respond","reason":"Help requested."}',
            response_usage=MessageMetrics(input_tokens=40, output_tokens=8, cache_read_tokens=30),
        ),
    )
    run = RunOutput(run_id="primary", metrics=RunMetrics())
    with participation_model(model, ParticipationGate(), run_id="primary"):
        await model.aresponse([Message(role="user", content="Question")], run_response=run)
    assert run.metrics.input_tokens == 40
    assert run.metrics.output_tokens == 8
    assert run.metrics.cache_read_tokens == 30


@pytest.mark.asyncio
async def test_decision_uses_same_compressed_messages_as_reply(monkeypatch: pytest.MonkeyPatch) -> None:
    """Checking before tool-result compression would miss the reusable prefix."""
    manager = CompressionManager()

    async def compress(messages: list[Message], **_kwargs: object) -> None:
        messages[:] = [Message(role="user", content="Compressed conversation")]

    monkeypatch.setattr(manager, "ashould_compress", AsyncMock(return_value=True))
    monkeypatch.setattr(manager, "acompress", compress)
    model = ParticipationModel(ModelResponse(content='{"action":"respond","reason":"Open question."}'))
    with participation_model(model, ParticipationGate(), run_id="primary"):
        await model.aresponse(
            [Message(role="user", content="Long conversation")],
            compression_manager=manager,
            run_response=RunOutput(run_id="primary"),
        )
    decision, reply = model.requests
    assert decision["messages"][:-1] == reply["messages"]
    assert decision["messages"][0].content == "Compressed conversation"
    assert decision["compress_tool_results"] is True
    assert reply["compress_tool_results"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
async def test_preparation_errors_do_not_publish_before_participation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    streaming: bool,
) -> None:
    """Failed preparation cannot bypass the participation decision with an error reply."""
    monkeypatch.setattr(
        "mindroom.ai._prepare_agent_and_prompt",
        AsyncMock(side_effect=RuntimeError("Preparation failed")),
    )
    gate = ParticipationGate()
    ctx = replace(make_turn_context("general", session_id="session-1"), participation=gate)
    if streaming:
        chunks = [
            chunk
            async for chunk in stream_agent_response(
                ctx,
                prompt="Question",
                runtime_paths=_runtime_paths(tmp_path),
                config=_config(),
            )
        ]
        assert chunks == []
    else:
        assert await ai_response(ctx, prompt="Question", runtime_paths=_runtime_paths(tmp_path), config=_config()) == ""
    assert gate.is_silent


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("approved", [False, True])
async def test_compression_failure_is_quiet_until_participation_is_approved(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    streaming: bool,
    approved: bool,
) -> None:
    """Agno errors before the provider boundary cannot authorize an error reply."""
    manager = CompressionManager()
    monkeypatch.setattr(manager, "ashould_compress", AsyncMock(side_effect=RuntimeError("Compression failed")))
    model = ParticipationModel(ModelResponse())
    agent = Agent(model=model, name="general", telemetry=False, compression_manager=manager)
    monkeypatch.setattr("mindroom.ai._prepare_agent_and_prompt", AsyncMock(return_value=_prepared_prompt_result(agent)))
    gate = ParticipationGate()
    if approved:
        gate.approve_existing_response()
    ctx = replace(make_turn_context("general", session_id="session-1"), participation=gate)
    kwargs = {"prompt": "Question", "runtime_paths": _runtime_paths(tmp_path), "config": _config()}
    if streaming:
        chunks = [chunk async for chunk in stream_agent_response(ctx, **kwargs)]
        result = "".join(chunk for chunk in chunks if isinstance(chunk, str))
    else:
        result = await ai_response(ctx, **kwargs)
    if approved:
        assert "Compression failed" in result
    else:
        assert result == ""
        assert gate.is_silent
    assert model.requests == []


@pytest.mark.asyncio
async def test_stream_backed_provider_does_not_reenter_decision() -> None:
    """A provider's blocking-to-stream delegation must reach the actual provider once."""

    class StreamBackedModel(ParticipationModel):
        async def ainvoke(self, messages: list[Message], **kwargs: object) -> ModelResponse:
            chunks = [chunk async for chunk in self.ainvoke_stream(messages, **kwargs)]
            return chunks[0]

        async def ainvoke_stream(self, messages: list[Message], **kwargs: object) -> AsyncIterator[ModelResponse]:
            yield await super().ainvoke(messages, **kwargs)

    model = StreamBackedModel(ModelResponse(content='{"action":"respond","reason":"Help requested."}'))
    with participation_model(model, ParticipationGate(), run_id="primary"):
        response = await model.aresponse(
            [Message(role="user", content="Question")],
            run_response=RunOutput(run_id="primary"),
        )
    assert response.content == "Useful answer"
    assert len(model.requests) == 2

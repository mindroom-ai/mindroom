"""Same-model participation decisions must never execute tools or alter reply inputs."""

from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import replace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, Mock

from agno.agent import Agent
from agno.compression.manager import CompressionManager
from agno.metrics import MessageMetrics, RunMetrics
from agno.run.agent import RunOutput

from mindroom.ai import ai_response, stream_agent_response
from mindroom.participation import ParticipationGate, participation_model
from tests.ai_user_id_helpers import _config, _prepared_prompt_result, _runtime_paths
from tests.conftest import make_turn_context
from tests.participation_helpers import ParticipationModel

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

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
    assert decision["tool_choice"] == normal["tool_choice"] == "auto"
    assert decision["response_format"] == normal["response_format"]
    assert "Offer concise technical help." in decision["messages"][-1].content
    assert len(model.requests) == 3
    assert gate.approved
    assert gate.decided.is_set()
    assert model.ainvoke == original_response


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "decision",
    [
        ModelResponse(content='{"action":"stay_silent","reason":"Humans are discussing plans."}'),
        ModelResponse(content="Sure!"),
        ModelResponse(content='{"action":"respond"}'),
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

"""Recovery keeps the authoritative visible response while accepted jobs resume."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator  # noqa: TC003 - Agno resolves tool return annotations at runtime.
from dataclasses import replace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock

import pytest
from agno.agent import Agent
from agno.models.response import ModelResponse, ToolExecution
from agno.run.agent import RunCompletedEvent, RunContentEvent, ToolCallCompletedEvent, ToolCallStartedEvent

from mindroom.ai import ai_response, stream_agent_response
from mindroom.delivery_gateway import DeliveryGateway
from mindroom.matrix.client_delivery import DeliveredMatrixEvent
from mindroom.response_runner import _EarlyPlaceholderState
from mindroom.streaming import RESTART_INTERRUPTED_RESPONSE_NOTE, StreamingPresentation, send_streaming_response
from mindroom.tool_jobs.completion import _ReadyJobContinuation
from mindroom.tool_jobs.runtime import BackgroundOutcome, JobSpec, ToolJobRuntime, register_background_runtime
from mindroom.tool_system.events import (
    BackgroundWaitChunk,
    ToolTraceEntry,
    build_tool_trace_content,
    deserialize_tool_trace,
)
from mindroom.turn_record import RevisionSnapshotChangedError
from tests.ai_user_id_helpers import _config, _prepared_prompt_result, _runtime_paths
from tests.conftest import make_turn_context, unwrap_extracted_collaborator
from tests.delegation_helpers import DelegationModel, _call
from tests.response_runner_helpers import _bot, _plain_request, _target
from tests.test_stale_stream_cleanup import _aiter, _make_message_event, _room_get_event_response
from tests.test_subagent_runtime import _job

if TYPE_CHECKING:
    from pathlib import Path


@pytest.mark.asyncio
async def test_nested_completion_does_not_repeat_parent_text(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A tool's completion event cannot reset the owning attempt's text fallback."""

    async def tool_stream() -> AsyncIterator[RunContentEvent | RunCompletedEvent]:
        yield RunContentEvent(content="Nested text.", run_id="nested-run")
        yield RunCompletedEvent(content="Nested text.", run_id="nested-run")

    model = DelegationModel(
        id="test",
        responses=[
            ModelResponse(content="Starting. ", tool_calls=[_call("tool_stream", "call")]),
            ModelResponse(content=""),
        ],
    )
    agent = Agent(id="general", model=model, tools=[tool_stream], telemetry=False)
    config = _config()
    config.memory.backend = "none"
    assert not config.background_tool_jobs.enabled
    monkeypatch.setattr("mindroom.ai._prepare_agent_and_prompt", AsyncMock(return_value=_prepared_prompt_result(agent)))

    body = await ai_response(
        make_turn_context("general", session_id="session1"),
        prompt="Run the tool.",
        runtime_paths=_runtime_paths(tmp_path),
        config=config,
        collect_streamed_response=True,
        show_tool_calls=False,
    )

    assert body == "Starting. Nested text."


@pytest.mark.asyncio
@pytest.mark.parametrize("readable", [False, True])
@pytest.mark.parametrize("show_tools", [False, True])
async def test_recovered_job_source_preserves_latest_visible_edit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    readable: bool,
    show_tools: bool,
) -> None:
    """Recovery must not replace a long edited answer with an empty presentation."""
    bot = _bot(tmp_path)
    bot.config.background_tool_jobs.enabled = True
    bot.config.agents["general"].show_tool_calls = show_tools
    runner = unwrap_extracted_collaborator(bot._response_runner)
    request = replace(
        _plain_request(_target(thread_id="$thread")),
        existing_event_id="$response",
        existing_event_is_placeholder=True,
    )
    owner = replace(
        _job().owner,
        agent_name="general",
        transport_agent_name=None,
        requester_id=request.response_envelope.requester_id,
        session_id=request.response_envelope.target.session_id,
    )
    runtime = ToolJobRuntime(tmp_path)
    register_background_runtime(bot.runtime_paths, runtime)
    old_trace = ToolTraceEntry("tool_call_completed", "original_tool", result_preview="saved result")
    narrative = ("Long analysis already visible. " * 100).rstrip()
    body = narrative + "\n\n🔧 `original_tool` [1]"
    original = _make_message_event(
        event_id="$response",
        body="Thinking...",
        timestamp_ms=10,
        sender=bot.matrix_id.full_id,
    )
    latest = _make_message_event(
        event_id="$edit",
        body="* latest",
        timestamp_ms=20,
        sender=bot.matrix_id.full_id,
        relates_to={"rel_type": "m.replace", "event_id": "$response"},
        new_content={"msgtype": "m.text", "body": body, **(build_tool_trace_content([old_trace]) or {})},
    )
    bot.client.room_get_event.side_effect = None
    bot.client.room_get_event.return_value = _room_get_event_response(original) if readable else None
    bot.client.room_get_event_relations = MagicMock(side_effect=lambda *_args, **_kwargs: _aiter(latest))

    async def operation() -> BackgroundOutcome:
        return BackgroundOutcome("completed", "saved result")

    try:
        await runtime.start(
            JobSpec("retained", "tool", 0, adapter={"source_event_id": "$event"}),
            owner=owner,
            operation=operation,
        )
        if not readable:
            with pytest.raises(RevisionSnapshotChangedError):
                await runner._recover_tool_job_source(request)
            return
        recovered = await runner._recover_tool_job_source(request)
        assert recovered.existing_event_id == "$response"
        assert recovered.initial_presentation is not None
        assert recovered.initial_presentation.response_text == (body if show_tools else narrative)
        assert recovered.initial_presentation.tool_trace == ((old_trace,) if show_tools else ())
        assert narrative in recovered.prompt
        assert recovered.sources == request.sources
        monkeypatch.setattr(
            "mindroom.knowledge.utils.KnowledgeAccessSupport.resolve_for_agent_async",
            AsyncMock(side_effect=RuntimeError("knowledge unavailable")),
        )
        outcome = await runner._process_and_respond_streaming(recovered)
        bot.client.room_redact.assert_not_called()
        assert outcome.delivery.event_id == "$response"
        assert not recovered.existing_event_is_placeholder
    finally:
        register_background_runtime(bot.runtime_paths, None)
        await runtime.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
async def test_recovered_agent_retains_prose_and_trace_through_real_tool_execution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    streaming: bool,
) -> None:
    """New tool markers must append after the restored text and keep earlier trace slots."""
    bot = _bot(tmp_path)
    bot.config.memory.backend = "none"
    target = _target(thread_id="$thread")
    prefix = "Previous detailed answer.\n\n🔧 `original_tool` [1]\n\nMore earlier text."
    old_trace = ToolTraceEntry("tool_call_completed", "original_tool", result_preview="old result")
    context = replace(
        make_turn_context("general", session_id="recovered", requester_id="@user:localhost"),
        initial_presentation=StreamingPresentation(prefix, tool_trace=(old_trace,)),
    )
    calls = []

    def retrieve() -> str:
        calls.append("retrieve")
        return "retained value"

    model = DelegationModel(
        id="test",
        responses=[
            ModelResponse(tool_calls=[_call("retrieve", "new-call")]),
            ModelResponse(content="The recovered result."),
        ],
    )

    def create_agent(*_args: object, **kwargs: object) -> Agent:
        return Agent(id="general", model=model, tools=[retrieve], db=kwargs["history_storage"], telemetry=False)

    monkeypatch.setattr("mindroom.ai.create_agent", create_agent)
    trace = []
    deliveries = []
    if streaming:

        async def edit(
            _client: object,
            _room_id: str,
            _event_id: str,
            new_content: dict[str, object],
            new_text: str,
            *,
            retry_sync_recovery: bool = False,  # noqa: ARG001
        ) -> DeliveredMatrixEvent:
            deliveries.append(new_text)
            return DeliveredMatrixEvent(event_id="$edit", content_sent=dict(new_content))

        monkeypatch.setattr("mindroom.streaming.edit_message_result", edit)
        outcome = await send_streaming_response(
            bot.client,
            target,
            bot.config,
            bot.runtime_paths,
            stream_agent_response(context, prompt="Continue.", runtime_paths=bot.runtime_paths, config=bot.config),
            existing_event_id="$response",
            tool_trace_collector=trace,
        )
        body = outcome.visible_body_text
        assert deliveries
        assert all(text.startswith(prefix) for text in deliveries)
    else:
        body = await ai_response(
            context,
            prompt="Continue.",
            runtime_paths=bot.runtime_paths,
            config=bot.config,
            tool_trace_collector=trace,
        )
    assert body.startswith(prefix)
    assert body.endswith("The recovered result.")
    assert "`retrieve` [2]" in body
    assert [entry.tool_name for entry in trace] == ["original_tool", "retrieve"]
    assert trace[0] == old_trace
    assert trace[1].result_preview == "retained value"
    assert calls == ["retrieve"]


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("has_delta", [False, True])
@pytest.mark.parametrize("joined", [False, True])
async def test_prior_prose_does_not_hide_terminal_only_answer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    streaming: bool,
    has_delta: bool,
    joined: bool,
) -> None:
    """Recovery and job-join prose cannot suppress the next attempt's terminal answer."""
    attempts = 0

    async def events(*_args: object, **_kwargs: object) -> AsyncIterator[RunContentEvent | RunCompletedEvent]:
        nonlocal attempts
        attempts += 1
        if joined and attempts == 1:
            yield RunContentEvent(content="Earlier visible answer.")
            yield RunCompletedEvent(content="Earlier visible answer.", run_id="first", session_id="session1")
            return
        if has_delta:
            yield RunContentEvent(content="Fresh streamed answer.")
        yield RunCompletedEvent(content="Fresh terminal answer.", run_id="recovery", session_id="session1")

    agent = MagicMock()
    agent.arun = MagicMock(side_effect=events)
    monkeypatch.setattr("mindroom.ai._prepare_agent_and_prompt", AsyncMock(return_value=_prepared_prompt_result(agent)))
    ctx = make_turn_context("general", session_id="session1")
    config = _config()
    if joined:
        config.background_tool_jobs.enabled = True

        async def join(attempted: set[tuple[str, int]], **_kwargs: object) -> AsyncIterator[_ReadyJobContinuation]:
            if not attempted:
                attempted.add(("job", 0))
                yield _ReadyJobContinuation("Retrieve completed background result")

        monkeypatch.setattr("mindroom.response_turn.join_conversation_jobs", join)
    else:
        ctx = replace(ctx, initial_presentation=StreamingPresentation("Earlier visible answer."))
    paths = _runtime_paths(tmp_path)
    if streaming:
        bot = _bot(tmp_path / "matrix")

        async def edit(
            _client: object,
            _room_id: str,
            _event_id: str,
            new_content: dict[str, object],
            _new_text: str,
            *,
            retry_sync_recovery: bool = False,  # noqa: ARG001
        ) -> DeliveredMatrixEvent:
            return DeliveredMatrixEvent(event_id="$edit", content_sent=dict(new_content))

        monkeypatch.setattr("mindroom.streaming.edit_message_result", edit)
        outcome = await send_streaming_response(
            bot.client,
            _target(),
            config,
            paths,
            stream_agent_response(ctx, prompt="Continue.", runtime_paths=paths, config=config),
            existing_event_id="$response",
        )
        body = outcome.visible_body_text
    else:
        body = await ai_response(
            ctx,
            prompt="Continue.",
            runtime_paths=paths,
            config=config,
            collect_streamed_response=True,
        )
    expected = "Fresh streamed answer." if has_delta else "Fresh terminal answer."
    assert body.count("Earlier visible answer.") == 1
    assert body.strip().endswith(expected)
    if not joined:
        assert body.strip() == "Earlier visible answer.\n\n" + expected
    assert body.count("Fresh terminal answer.") == int(not has_delta)
    assert attempts == (2 if joined else 1)


@pytest.mark.asyncio
async def test_recovered_blocking_cancellation_keeps_visible_body_and_trace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cancelling a recovery appends the restart note without replacing prior content."""
    bot = _bot(tmp_path)
    bot.config.memory.backend = "none"
    runner = unwrap_extracted_collaborator(bot._response_runner)
    old_trace = ToolTraceEntry("tool_call_completed", "original_tool", result_preview="saved result")
    prefix = "Already visible analysis.\n\n🔧 `original_tool` [1]"
    request = replace(
        _plain_request(_target(thread_id="$thread")),
        existing_event_id="$response",
        initial_presentation=StreamingPresentation(prefix, tool_trace=(old_trace,)),
    )
    original = _make_message_event(
        event_id="$response",
        body=prefix,
        timestamp_ms=10,
        sender=bot.matrix_id.full_id,
        extra_content=build_tool_trace_content([old_trace]),
    )
    bot.client.room_get_event.side_effect = None
    bot.client.room_get_event.return_value = _room_get_event_response(original)
    bot.client.room_get_event_relations = MagicMock(side_effect=lambda *_args, **_kwargs: _aiter())
    monkeypatch.setattr(
        "mindroom.response_runner.ai_response",
        AsyncMock(side_effect=asyncio.CancelledError("sync_restart")),
    )
    edit = AsyncMock(return_value=True)
    monkeypatch.setattr(DeliveryGateway, "edit_text", edit)
    outcome = await runner._process_and_respond(request)
    delivered = edit.await_args.args[0]
    assert delivered.new_text == prefix + "\n\n" + RESTART_INTERRUPTED_RESPONSE_NOTE
    assert delivered.tool_trace == [old_trace]
    assert outcome.delivery.final_visible_body == delivered.new_text
    assert outcome.delivery.tool_trace == (old_trace,)


@pytest.mark.asyncio
@pytest.mark.parametrize("latest_state", ["readable", "unreadable", "error", "wrong_sender"])
@pytest.mark.parametrize("recovered", [False, True])
@pytest.mark.parametrize("cancel_source", ["sync_restart", "user_stop"])
async def test_blocking_wait_cancellation_preserves_latest_presentation(  # noqa: PLR0915
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    latest_state: str,
    recovered: bool,
    cancel_source: str,
) -> None:
    """A published wait is the cancellation baseline, including newly numbered tools."""
    bot = _bot(tmp_path)
    bot.config.memory.backend = "none"
    bot.config.background_tool_jobs.enabled = True
    runner = unwrap_extracted_collaborator(bot._response_runner)
    target = _target(thread_id="$thread")
    old_trace = ToolTraceEntry("tool_call_completed", "original_tool", result_preview="saved result")
    prefix = "Already visible analysis.\n\n🔧 `original_tool` [1]"
    request = replace(
        _plain_request(target),
        existing_event_id="$response",
        initial_presentation=StreamingPresentation(prefix, tool_trace=(old_trace,)) if recovered else None,
    )
    sender = "@other:localhost" if latest_state == "wrong_sender" else bot.matrix_id.full_id
    original = _make_message_event(
        event_id="$response",
        body=prefix,
        timestamp_ms=10,
        sender=sender,
        extra_content=build_tool_trace_content([old_trace]),
    )
    edits = []
    bot.client.room_get_event.side_effect = None
    bot.client.room_get_event.return_value = (
        None if latest_state == "unreadable" else _room_get_event_response(original)
    )
    if latest_state == "error":
        bot.client.room_get_event.side_effect = RuntimeError("latest response unavailable")
    bot.client.room_get_event_relations = MagicMock(side_effect=lambda *_args, **_kwargs: _aiter(*edits))

    async def edit(
        _client: object,
        _room_id: str,
        event_id: str,
        content: dict[str, object],
        text: str,
        *,
        retry_sync_recovery: bool = False,  # noqa: ARG001
    ) -> DeliveredMatrixEvent:
        edit_id = f"$edit-{len(edits)}"
        edits.append(
            _make_message_event(
                event_id=edit_id,
                body="* " + text,
                timestamp_ms=20 + len(edits),
                sender=sender,
                relates_to={"rel_type": "m.replace", "event_id": event_id},
                new_content=content,
            ),
        )
        return DeliveredMatrixEvent(event_id=edit_id, content_sent=dict(content))

    async def events(*_args: object, **_kwargs: object) -> AsyncIterator[object]:
        yield RunContentEvent(content="New recovery analysis.")
        yield ToolCallStartedEvent(tool=ToolExecution(tool_call_id="new-call", tool_name="retrieve"))
        yield ToolCallCompletedEvent(
            tool=ToolExecution(tool_call_id="new-call", tool_name="retrieve", result="new result"),
        )
        yield BackgroundWaitChunk("\n\nWaiting for background work.")
        raise asyncio.CancelledError(cancel_source)

    monkeypatch.setattr("mindroom.delivery_gateway.edit_message_outcome", edit)
    monkeypatch.setattr("mindroom.ai.stream_response_turn", events)
    outcomes = []

    async def respond(_target: object, _state: object) -> str | None:
        outcome = await runner._process_and_respond(request)
        outcomes.append(outcome.delivery)
        return outcome.delivery.event_id

    result = await runner._run_unowned_response(
        request,
        target=target,
        early_placeholder=_EarlyPlaceholderState(),
        locked_operation=respond,
    )
    assert result == "$response"
    assert len(outcomes) == 1
    outcome = outcomes[0]
    assert outcome.terminal_status == "cancelled"
    assert outcome.cancel_source == cancel_source
    if latest_state != "readable":
        assert len(edits) == 1, "An unreadable latest response must stay intact"
        assert outcome.final_visible_body is None
    else:
        assert len(edits) == 2
        assert outcome.final_visible_body is not None
        assert "New recovery analysis." in outcome.final_visible_body
        note = (
            RESTART_INTERRUPTED_RESPONSE_NOTE if cancel_source == "sync_restart" else "**[Response cancelled by user]**"
        )
        assert outcome.final_visible_body.endswith(note)
    wait_content = edits[0].source["content"]["m.new_content"]
    assert wait_content["body"].startswith(prefix if recovered else "New recovery analysis.")
    assert f"`retrieve` [{2 if recovered else 1}]" in wait_content["body"]
    trace = deserialize_tool_trace(wait_content.get("io.mindroom.tool_trace", {}).get("events", []))
    assert trace == ([old_trace] if recovered else []) + [
        ToolTraceEntry("tool_call_completed", "retrieve", result_preview="new result"),
    ]
    if latest_state == "readable":
        assert outcome.tool_trace == tuple(trace)
        final_content = edits[-1].source["content"]["m.new_content"]
        assert final_content["io.mindroom.tool_trace"] == wait_content["io.mindroom.tool_trace"]
    bot.client.room_redact.assert_not_called()

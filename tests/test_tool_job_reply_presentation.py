"""A reply keeps its published text and tool trace across job joins, recovery re-runs, and cancellation."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator  # noqa: TC003 - Agno resolves tool return annotations at runtime.
from dataclasses import replace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock

import pytest
from agno.agent import Agent
from agno.models.response import ModelResponse
from agno.run.agent import RunCompletedEvent, RunContentEvent

from mindroom.ai import ai_response, stream_agent_response
from mindroom.delivery_gateway import DeliveryGateway
from mindroom.matrix.client_delivery import DeliveredMatrixEvent
from mindroom.response_runner import _EarlyPlaceholderState
from mindroom.streaming import RESTART_INTERRUPTED_RESPONSE_NOTE, StreamingPresentation, send_streaming_response
from mindroom.tool_jobs.completion import HeldContinuation, _JobJoin
from mindroom.tool_jobs.instances import pin_background_tool_jobs
from mindroom.tool_jobs.runtime import BackgroundOutcome, register_background_runtime
from mindroom.tool_system.events import (
    ToolTraceEntry,
    deserialize_tool_trace,
)
from tests.ai_user_id_helpers import _config, _prepared_prompt_result, _runtime_paths
from tests.conftest import make_turn_context, unwrap_extracted_collaborator
from tests.delegation_helpers import DelegationModel, _call
from tests.response_runner_helpers import _bot, _plain_request, _target
from tests.test_response_payload_preparation import _preparation
from tests.tool_job_helpers import completed_delegation_job, start_job, tool_job_runtime

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
async def test_recovered_job_source_reruns_into_its_reply_without_repeating_accepted_work(tmp_path: Path) -> None:
    """A re-run of a source whose tool calls became jobs replaces the old reply and retrieves, never repeats, them."""
    bot = _bot(tmp_path)
    bot.config.background_tool_jobs.enabled = True
    runner = unwrap_extracted_collaborator(bot._response_runner)
    request = replace(
        _plain_request(_target(thread_id="$thread")),
        existing_event_id="$response",
        existing_event_is_placeholder=True,
    )
    preparation = _preparation(request.response_envelope.target, prompt=request.prompt)
    request = replace(
        request,
        payload_preparation=replace(
            preparation,
            dispatch=replace(preparation.dispatch, envelope=request.response_envelope),
        ),
    )
    owner = replace(
        completed_delegation_job().owner,
        agent_name="general",
        transport_agent_name=None,
        requester_id=request.response_envelope.requester_id,
        session_id=request.response_envelope.target.session_id,
    )
    runtime = await tool_job_runtime(tmp_path)
    pin_background_tool_jobs(bot.config, bot.runtime_paths)
    register_background_runtime(bot.runtime_paths, runtime)

    async def operation() -> BackgroundOutcome:
        return BackgroundOutcome("completed", "saved result")

    try:
        await start_job(
            runtime,
            "retained",
            tool_name="tool",
            depth=0,
            source_event_id="$event",
            adapter={},
            owner=owner,
            operation=operation,
        )
        recovered = await runner._recover_tool_job_source(request)
        # Recovery never reads the interrupted reply it replaces.
        bot.client.room_get_event.assert_not_called()
        prepared = await runner.deps.request_preparer.prepare(recovered)
    finally:
        await runtime.shutdown()
    assert (recovered.existing_event_id, recovered.existing_event_is_placeholder) == ("$response", True)
    assert recovered.sources == request.sources
    # The re-run answers the original request; a nonpersistent note keeps it from repeating accepted work.
    assert (prepared.prompt, recovered.model_prompt) == (request.prompt, request.model_prompt)
    (note,) = prepared.system_enrichment_items
    assert (note.persist, note.minimal_required) == (False, True)
    assert "do not repeat them" in note.text
    assert 'job_id="retained"' in note.text
    # It answers the original human message, whose origin deferred preparation keeps.
    assert prepared.response_envelope.origin == request.response_envelope.origin


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("has_delta", [False, True])
@pytest.mark.parametrize("prefix", ["", "## "])
async def test_prior_prose_does_not_hide_terminal_only_answer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    streaming: bool,
    has_delta: bool,
    prefix: str,
) -> None:
    """A job join retains distinct paragraphs for incremental and terminal-only prose."""
    attempts = 0

    async def events(*_args: object, **_kwargs: object) -> AsyncIterator[RunContentEvent | RunCompletedEvent]:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            yield RunContentEvent(content="Earlier visible answer.")
            yield RunCompletedEvent(content="Earlier visible answer.", run_id="first", session_id="session1")
            return
        if has_delta:
            yield RunContentEvent(content=prefix + "Fresh streamed answer.")
        yield RunCompletedEvent(content=prefix + "Fresh terminal answer.", run_id="recovery", session_id="session1")

    agent = MagicMock()
    agent.arun = MagicMock(side_effect=events)
    monkeypatch.setattr("mindroom.ai._prepare_agent_and_prompt", AsyncMock(return_value=_prepared_prompt_result(agent)))
    ctx = make_turn_context("general", session_id="session1")
    config = _config()
    config.background_tool_jobs.enabled = True

    async def join(attempted: set[str], **_kwargs: object) -> _JobJoin:
        if not attempted:
            attempted.add("job")
            return _JobJoin(prompt="Retrieve completed background result")
        return _JobJoin()

    monkeypatch.setattr("mindroom.response_turn.join_conversation_jobs", join)
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
    expected = prefix + ("Fresh streamed answer." if has_delta else "Fresh terminal answer.")
    assert body.count("Earlier visible answer.") == 1
    assert body.strip().endswith(expected)
    assert body.strip() == "Earlier visible answer.\n\n" + expected
    assert body.count("Fresh terminal answer.") == int(not has_delta)
    assert attempts == 2


@pytest.mark.asyncio
async def test_blocking_cancellation_without_a_wait_matches_a_disabled_reply(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Enabling background jobs changes nothing about stopping a blocking reply that never waited on a job."""
    monkeypatch.setattr(
        "mindroom.response_runner.ai_response",
        AsyncMock(side_effect=asyncio.CancelledError("user_stop")),
    )
    edit = AsyncMock(return_value=True)
    monkeypatch.setattr(DeliveryGateway, "edit_text", edit)
    notes = []
    for enabled in (False, True):
        bot = _bot(tmp_path / str(enabled))
        bot.config.memory.backend = "none"
        bot.config.background_tool_jobs.enabled = enabled
        runner = unwrap_extracted_collaborator(bot._response_runner)
        request = replace(_plain_request(_target(thread_id="$thread")), existing_event_id="$response")
        outcome = await runner._process_and_respond(request)
        delivered = edit.await_args.args[0]
        notes.append((outcome.delivery.terminal_status, delivered.new_text, delivered.tool_trace))
    assert notes[1] == notes[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_source", ["sync_restart", "user_stop"])
async def test_blocking_continuation_cancellation_preserves_the_held_presentation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    cancel_source: str,
) -> None:
    """A blocking turn continuing a held message shows nothing new, so cancelling it keeps that message's text and tools."""
    bot = _bot(tmp_path)
    bot.config.memory.backend = "none"
    bot.config.background_tool_jobs.enabled = True
    runner = unwrap_extracted_collaborator(bot._response_runner)
    target = _target(thread_id="$thread")
    trace = (ToolTraceEntry("tool_call_completed", "retrieve", result_preview="new result"),)
    request = replace(
        _plain_request(target),
        existing_event_id="$response",
        held_continuation=HeldContinuation(
            presentation=StreamingPresentation("New analysis.\n\n🔧 `retrieve` [1]", tool_trace=trace),
            attempted_job_ids=frozenset({"job"}),
            joins=0,
        ),
    )
    edits = []

    async def edit(
        _client: object,
        _room_id: str,
        event_id: str,
        content: dict[str, object],
        text: str,
        *,
        retry_sync_recovery: bool = False,  # noqa: ARG001
    ) -> DeliveredMatrixEvent:
        edits.append((event_id, content, text))
        return DeliveredMatrixEvent(event_id=f"$edit-{len(edits)}", content_sent=dict(content))

    async def events(*_args: object, **_kwargs: object) -> AsyncIterator[object]:
        raise asyncio.CancelledError(cancel_source)
        yield

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
    assert len(edits) == 1
    assert outcome.final_visible_body is not None
    assert outcome.final_visible_body.startswith("New analysis.\n\n🔧 `retrieve` [1]")
    note = RESTART_INTERRUPTED_RESPONSE_NOTE if cancel_source == "sync_restart" else "**[Response cancelled by user]**"
    assert outcome.final_visible_body.endswith(note)
    assert outcome.tool_trace == trace
    final_content = edits[-1][1]
    assert deserialize_tool_trace(final_content["io.mindroom.tool_trace"]["events"]) == list(trace)
    bot.client.room_redact.assert_not_called()

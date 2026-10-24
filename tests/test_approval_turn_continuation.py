"""Approved runs finish their task, including after a same-turn continuation rebuilds the agent."""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any

import pytest

from mindroom import agents
from mindroom.ai import ai_response
from mindroom.ai_runtime import queued_message_signal_context
from mindroom.approval.execution import (
    _collect_agent_continuation,
    _continuation_turn_context,
    _stream_continuation_turn,
)
from mindroom.approval.receipt_readers import approval_receipt_context
from mindroom.config.main import Config
from mindroom.constants import AI_RUN_METADATA_KEY, resolve_runtime_paths
from mindroom.custom_tools.calculator import CalculatorTools
from mindroom.custom_tools.sleep import SleepTools
from mindroom.event_journal import ApprovalCall, approval_arguments_digest
from mindroom.llm.errors import ProviderError
from mindroom.llm.events import RunFinished, TextDelta, ToolCompleted, ToolStarted
from mindroom.llm.provider import ToolCallReady
from mindroom.llm.runs import RunRecord, RunStatus, ToolExecution
from mindroom.message_target import MessageTarget
from mindroom.response_turn import (
    CompletedApprovalRun,
    CompletedAttempt,
    PausedAnswer,
    PausedAttempt,
    ResponsePausedForApproval,
    ResumedAttempt,
)
from mindroom.tool_system.events import (
    CollectedStreamPresentation,
    StructuredStreamChunk,
    ToolTraceEntry,
)
from mindroom.tool_system.runtime_context import LiveToolDispatchContext, ToolDispatchContext
from mindroom.tool_system.worker_routing import ToolExecutionIdentity, get_tool_execution_identity
from tests.approval_continuation_helpers import approval_continuation, carries_approval_receipt
from tests.conftest import bind_runtime_paths, make_turn_context, unwrap_extracted_collaborator
from tests.fakes.scripted_model import Await, ScriptedModel, ScriptStep, answer, calls, tool_call
from tests.response_runner_helpers import _bot

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable
    from pathlib import Path

    from mindroom.constants import RuntimePaths
    from mindroom.llm.events import RunEvent
    from mindroom.llm.provider import ModelRequest, StreamEvent
    from mindroom.response_turn import ResponseTurnContext


def _continue_call(call_id: str) -> list[ScriptStep]:
    """Return a next-turn switch to the current model, which ends the step and continues the same turn."""
    return calls(tool_call("switch_thread_model", call_id, model_name="default", when="next-turn"))


def _deferred_call(call_id: str, function_name: str, **arguments: object) -> list[ScriptStep]:
    """Return a call of a deferred function through the call_tool wrapper the provider sees."""
    return calls(tool_call("call_tool", call_id, name=function_name, arguments=arguments))


def _call(function_name: str, call_id: str, **arguments: object) -> list[ScriptStep]:
    return calls(tool_call(function_name, call_id, **arguments))


def _failing(message: str) -> list[ScriptStep]:
    """Return a turn whose provider request fails."""

    async def fail() -> None:
        raise ProviderError(message, provider="Synthetic")

    return [Await(fail)]


@dataclass
class _CheckedModel(ScriptedModel):
    """Replay the scripted turns, checking that each requested call names a tool the request offered."""

    async def stream(self, request: ModelRequest) -> AsyncIterator[StreamEvent]:
        offered = {tool.name for tool in request.tools}
        async for event in super().stream(request):
            if isinstance(event, ToolCallReady):
                assert event.call["function"]["name"] in offered, (event.call, sorted(offered))
            yield event


def _identity(session_id: str) -> ToolExecutionIdentity:
    return ToolExecutionIdentity(
        channel="matrix",
        agent_name="general",
        requester_id="@user:example.org",
        room_id="!room:example.org",
        thread_id="$thread",
        resolved_thread_id="$thread",
        session_id=session_id,
    )


async def _paused_turn(
    config: Config,
    paths: RuntimePaths,
    identity: ToolExecutionIdentity,
    prompt: str,
    *,
    show_tool_calls: bool = True,
) -> PausedAttempt:
    """Run one reply of ``general`` as the response runner does and return the pause it suspended on."""
    ctx = make_turn_context(
        "general",
        session_id=identity.session_id,
        room_id=identity.room_id,
        thread_id=identity.thread_id,
        requester_id=identity.requester_id,
    )
    with pytest.raises(ResponsePausedForApproval) as raised:
        await ai_response(
            ctx,
            prompt=prompt,
            config=config,
            runtime_paths=paths,
            execution_identity=identity,
            show_tool_calls=show_tool_calls,
            supports_native_tool_approval=True,
        )
    return raised.value.paused


@pytest.mark.asyncio
@pytest.mark.parametrize("show_tool_calls", [False, True])
async def test_deferred_call_through_call_tool_pauses_under_its_real_name(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    show_tool_calls: bool,
) -> None:
    """An approval-gated deferred function called through call_tool pauses and is shown as itself."""
    paths = resolve_runtime_paths(config_path=tmp_path / "config.yaml", storage_path=tmp_path / "storage")
    config = bind_runtime_paths(
        Config.model_validate(
            {
                "defaults": {"tools": []},
                "memory": {"backend": "none"},
                "agents": {"general": {"display_name": "General", "tools": [{"calculator": {"defer": True}}]}},
                "models": {"default": {"provider": "synthetic", "id": "synthetic"}},
                "tool_approval": {"rules": [{"match": "add", "action": "require_approval"}]},
            },
        ),
        paths,
    )
    model = _CheckedModel([_deferred_call("sum", "add", a=2, b=3)])
    monkeypatch.setattr(agents, "_build_agent_model_provider", lambda *_args, **_kwargs: model)
    with pytest.raises(ResponsePausedForApproval) as raised:
        await ai_response(
            make_turn_context("general", session_id="deferred-approval"),
            prompt="Add two and three.",
            config=config,
            runtime_paths=paths,
            show_tool_calls=show_tool_calls,
            supports_native_tool_approval=True,
        )
    paused = raised.value.paused
    assert not model.turns
    assert [(entry.tool_name, entry.tool_args) for entry in paused.tools] == [("add", {"a": 2, "b": 3})]
    assert [entry.tool_name for entry in paused.tool_trace] == ["add"]
    assert "call_tool" not in paused.response_text
    if show_tool_calls:
        assert "`add` [1]" in paused.response_text


@pytest.mark.asyncio
@pytest.mark.parametrize("show_tool_calls", [False, True])
@pytest.mark.parametrize(
    "outcome",
    [
        "finish",
        "pause",
        "pause_limit",
        "immediate_pause",
        "limit",
        "error",
        "preparation_error",
        "switch_after",
        "switch_next",
    ],
)
async def test_approved_run_continues_after_a_same_turn_continuation(  # noqa: C901, PLR0915
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    show_tool_calls: bool,
    outcome: str,
) -> None:
    """A fresh continuation attempt retains history and may pause again without replaying approved work.

    Each approval's receipt goes with the resumed run's first request only, never with a fresh attempt after it.
    """
    switch_when = {"switch_after": "after-toolcall", "switch_next": "next-turn"}.get(outcome)
    paths = resolve_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path / "storage",
        process_env={"MATRIX_HOMESERVER": "https://matrix.example.org", "MINDROOM_NAMESPACE": ""},
    )
    config = bind_runtime_paths(
        Config.model_validate(
            {
                "defaults": {"tools": []},
                "memory": {"backend": "none"},
                "agents": {
                    "general": {
                        "display_name": "General",
                        "tools": ["calculator", {"sleep": {"defer": True}}, "thread_model"],
                    },
                },
                "models": {
                    "default": {"provider": "synthetic", "id": "synthetic"},
                    "alternate": {"provider": "synthetic", "id": "synthetic"},
                },
                "tool_approval": {
                    "default": "auto_approve",
                    "rules": [
                        {"match": "add", "action": "require_approval"},
                        *(
                            [{"match": "sleep", "action": "require_approval"}]
                            if outcome in {"pause", "pause_limit", "immediate_pause"} or switch_when
                            else []
                        ),
                    ],
                },
            },
        ),
        paths,
    )
    identity = _identity("approval-session")
    turns: list[list[ScriptStep]] = [
        _call("add", "approved", a=2, b=3),
        _continue_call("continue"),
        *(
            [_continue_call(f"continue-{index}") for index in range(4)]
            if outcome == "limit"
            else [_deferred_call("sleeper", "sleep", seconds=0), answer("Finished the calculation and wait.")]
        ),
    ]
    if outcome == "error":
        turns[2:] = [_failing("Provider continuation failed")]
    if outcome == "pause_limit":
        turns[3:] = [_continue_call(f"continue-again-{index}") for index in range(4)]
    if outcome == "immediate_pause":
        turns[1:1] = [_call("add", "approved-again", a=4, b=5)]
    if switch_when:
        turns.insert(1, _call("switch_thread_model", "switcher", model_name="alternate", when=switch_when))
    model = _CheckedModel(turns)
    models: list[str] = []

    def build_model(
        _config: Config,
        _paths: RuntimePaths,
        model_name: str,
        _execution_identity: ToolExecutionIdentity | None = None,
    ) -> ScriptedModel:
        if outcome == "preparation_error" and len(model.requests) == 2:
            msg = "Model preparation failed"
            raise RuntimeError(msg)
        models.append(model_name)
        return model

    monkeypatch.setattr(agents, "_build_agent_model_provider", build_model)
    executed: list[str] = []
    original_add = CalculatorTools.add
    original_sleep = SleepTools.sleep

    def add(self: CalculatorTools, a: float, b: float) -> str:
        assert get_tool_execution_identity() == identity
        executed.append("add")
        return original_add(self, a, b)

    async def sleep(self: SleepTools, seconds: int) -> str:
        assert get_tool_execution_identity() == identity
        executed.append("sleep")
        return await original_sleep(self, seconds)

    monkeypatch.setattr(CalculatorTools, "add", add)
    monkeypatch.setattr(SleepTools, "sleep", sleep)
    prompt = "Calculate the sum, wait, and finish."
    paused = await _paused_turn(config, paths, identity, prompt, show_tool_calls=show_tool_calls)
    assert executed == []
    continuation = approval_continuation(
        approval_id="approval-example",
        run_id=paused.run_id,
        session_id=identity.session_id,
        room_id=identity.room_id,
        thread_id=identity.thread_id,
        requester_id=identity.requester_id,
        state="claimed",
        calls=(
            ApprovalCall(
                "approved",
                "add",
                "general",
                2**62,
                toolkit_name="calculator",
                arguments_digest=approval_arguments_digest({"a": 2, "b": 3}),
            ),
        ),
        request_body=prompt,
        show_tool_calls=show_tool_calls,
        continuation_count=2 if outcome == "immediate_pause" else 0,
    )
    runner = unwrap_extracted_collaborator(_bot(tmp_path / "runner")._response_runner)
    execution = replace(runner._approval_execution, config=lambda: config, runtime_paths=paths)
    context = execution.tool_runtime.build_context(
        MessageTarget(
            identity.room_id,
            identity.thread_id,
            identity.resolved_thread_id,
            "$source",
            identity.session_id,
        ),
        user_id=identity.requester_id,
        active_model_name="default",
    )
    assert context is not None
    dispatch = LiveToolDispatchContext(
        execution_identity=identity,
        runtime_context=replace(context, config=config, config_provider=None, runtime_paths=paths),
    )
    trace: list[ToolTraceEntry] = []
    run_ids: list[str] = []
    paused_answer = PausedAnswer()
    resume_starts: list[int] = []

    async def resume() -> CompletedApprovalRun | PausedAttempt:
        resume_starts.append(len(model.requests))
        with approval_receipt_context(f"Trusted approval receipt {len(resume_starts)}.", run_id=continuation.run_id):
            return await execution.continue_run(
                continuation,
                paused_answer=paused_answer,
                execution_identity=identity,
                tool_dispatch=dispatch,
                decisions={call.tool_call_id: True for call in continuation.calls},
                denial_reasons={call.tool_call_id: None for call in continuation.calls},
                tool_trace_collector=trace,
                typing_log_context={},
                run_id_callback=run_ids.append,
                progress=None,
            )

    if outcome in {"error", "preparation_error"}:
        # The fresh attempt's provider failure becomes the reply's error text, which the continuation raises.
        with pytest.raises(RuntimeError):
            await resume()
        assert executed == ["add"]
        assert len(model.turns) == (2 if outcome == "preparation_error" else 0)
        return
    result = await resume()
    if outcome == "immediate_pause":
        assert isinstance(result, PausedAttempt)
        assert result.continuation_count == 2
        assert result.run_id == paused.run_id
        assert run_ids == []
        assert executed == ["add"]
        return
    if outcome == "limit":
        assert isinstance(result, CompletedApprovalRun)
        assert "Model switching did not produce a final answer" in result.response_text
        assert "need confirmation" not in result.response_text
        assert len(run_ids) == 4
        assert executed == ["add"]
        assert not model.turns
        return
    assert len(run_ids) == (2 if switch_when else 1)
    assert run_ids[0] != paused.run_id
    if outcome in {"pause", "pause_limit"} or switch_when:
        assert isinstance(result, PausedAttempt)
        assert executed == ["add"]
        assert result.run_id != paused.run_id
        assert result.run_id == run_ids[-1]
        assert result.runtime_model_name == ("alternate" if switch_when == "after-toolcall" else "default")
        assert [tool.tool_name for tool in result.tools] == ["sleep"]
        assert result.continuation_count == (2 if switch_when else 1)
        assert "need confirmation" not in result.response_text
        continuation = replace(
            continuation,
            run_id=result.run_id,
            runtime_model_name=result.runtime_model_name,
            continuation_count=result.continuation_count,
            calls=(
                ApprovalCall(
                    "sleeper",
                    "sleep",
                    "general",
                    2**62,
                    toolkit_name="sleep",
                    arguments_digest=approval_arguments_digest({"seconds": 0}),
                ),
            ),
        )
        paused_answer = PausedAnswer(text=result.response_text, tool_trace=result.tool_trace)
        result = await resume()
    if outcome == "pause_limit":
        assert isinstance(result, CompletedApprovalRun)
        assert "Model switching did not produce a final answer" in result.response_text
        assert "need confirmation" not in result.response_text
        assert len(run_ids) == 4
        assert executed == ["add", "sleep"]
        assert not model.turns
        return
    assert isinstance(result, CompletedApprovalRun)
    assert result.metadata_content[AI_RUN_METADATA_KEY]["run_id"] == run_ids[-1]
    assert result.metadata_content[AI_RUN_METADATA_KEY]["session_id"] == identity.session_id
    assert "Finished the calculation and wait." in result.response_text
    assert "need confirmation" not in result.response_text
    assert executed == ["add", "sleep"]
    assert models[-1] == ("alternate" if switch_when == "after-toolcall" else "default")
    assert not model.turns
    last = model.requests[-1].messages
    assert any(message.role == "tool" and message.tool_call_id == "approved" for message in last)
    assert any(message.role == "tool" and message.tool_call_id == "continue" for message in last)
    expected_tools = ["add", *(["switch_thread_model"] if switch_when else []), "switch_thread_model", "sleep"]
    assert [entry.tool_name for entry in trace] == (expected_tools if show_tool_calls else [])
    receipt_requests = [index for index, request in enumerate(model.requests) if carries_approval_receipt(request)]
    assert receipt_requests == resume_starts


def _finished(
    *,
    run_id: str = "run-saved",
    status: RunStatus = RunStatus.completed,
    content: str | None = None,
    tools: list[ToolExecution] | None = None,
) -> RunFinished:
    return RunFinished(RunRecord(run_id=run_id, session_id="session", status=status, content=content, tools=tools))


async def _events(*items: RunEvent) -> AsyncIterator[RunEvent]:
    for item in items:
        yield item


@pytest.mark.asyncio
@pytest.mark.parametrize("show_tool_calls", [False, True])
@pytest.mark.parametrize("boundary", ["restored", "new"])
async def test_approval_settlement_preserves_tool_boundary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    show_tool_calls: bool,
    boundary: str,
) -> None:
    """Terminal-only approval text must cross the real driver handoff with its pending separator."""
    paths = resolve_runtime_paths(config_path=tmp_path / "config.yaml", storage_path=tmp_path / "storage")
    config = bind_runtime_paths(
        Config.model_validate(
            {
                "agents": {"general": {"display_name": "General", "tools": []}},
                "models": {"default": {"provider": "synthetic", "id": "synthetic"}},
            },
        ),
        paths,
    )
    monkeypatch.setattr(agents, "_build_agent_model_provider", lambda *_args, **_kwargs: _CheckedModel([]))
    identity = _identity("session")
    continuation = approval_continuation(
        approval_id="approval-boundary",
        run_id="run-saved",
        session_id="session",
        room_id=identity.room_id,
        thread_id=identity.thread_id,
        requester_id=identity.requester_id,
        state="claimed",
        show_tool_calls=show_tool_calls,
    )
    tool = ToolExecution(tool_call_id="call-1", tool_name="inspect", result="done")
    presentation = CollectedStreamPresentation(
        show_tool_calls=show_tool_calls,
        response_text="Before approval."
        + ("\n\n🔧 `inspect` [1] ⏳" if boundary == "restored" and show_tool_calls else ""),
        tool_trace=(
            [ToolTraceEntry(type="tool_call_started", tool_name="inspect", tool_call_id="call-1")]
            if boundary == "restored"
            else []
        ),
        track_hidden_tools=True,
    )
    events = [ToolStarted(tool.call), ToolCompleted(tool.call)] if boundary == "new" else []
    events.append(_finished(content="After approval.", tools=[tool]))

    collected = await _collect_agent_continuation(_events(*events), presentation, progress=None)
    trace: list[ToolTraceEntry] = []
    result = await _stream_continuation_turn(
        _continuation_turn_context(continuation, model_name="default", metadata=None),
        presentation,
        prompt=continuation.request_body,
        show_tool_calls=continuation.show_tool_calls,
        resumed_attempt=ResumedAttempt(CompletedAttempt(response_text=collected.terminal_content)),
        config=config,
        runtime_paths=paths,
        execution_identity=identity,
        knowledge=None,
        refresh_scheduler=None,
        tool_trace_collector=trace,
        run_id_callback=None,
        tool_dispatch=ToolDispatchContext(execution_identity=identity),
        progress=None,
    )
    assert isinstance(result, CompletedApprovalRun)
    expected = "Before approval.\n\n" + ("🔧 `inspect` [1]\n\n" if show_tool_calls else "") + "After approval."
    assert result.response_text == expected
    assert len(trace) == (1 if show_tool_calls else 0)


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [RunStatus.completed, RunStatus.error])
@pytest.mark.parametrize("wire_status", [None, "completed", "error"])
async def test_approval_settlement_uses_typed_status(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    status: RunStatus,
    wire_status: str | None,
) -> None:
    """Display metadata cannot grant or deny completion of an approved response."""
    paths = resolve_runtime_paths(config_path=tmp_path / "config.yaml", storage_path=tmp_path / "storage")
    config = Config.model_validate({"agents": {"general": {"display_name": "General", "tools": []}}})
    identity = _identity("session")
    continuation = approval_continuation(
        approval_id="approval-status",
        run_id="run-saved",
        session_id="session",
        room_id="!room:example.org",
        requester_id="@user:example.org",
        state="claimed",
    )

    async def stream(
        _ctx: ResponseTurnContext,
        *,
        on_completed: Callable[[CompletedAttempt], None],
        run_metadata_collector: dict[str, Any],
        **_kwargs: object,
    ) -> AsyncIterator[TextDelta]:
        if wire_status is not None:
            run_metadata_collector[AI_RUN_METADATA_KEY] = {"status": wire_status}
        yield TextDelta("Terminal response")
        on_completed(CompletedAttempt(status=status))

    monkeypatch.setattr("mindroom.approval.execution.stream_agent_response", stream)
    settlement = _stream_continuation_turn(
        _continuation_turn_context(continuation, model_name="default", metadata=None),
        CollectedStreamPresentation(show_tool_calls=False),
        prompt=continuation.request_body,
        show_tool_calls=continuation.show_tool_calls,
        resumed_attempt=ResumedAttempt(CompletedAttempt()),
        config=config,
        runtime_paths=paths,
        execution_identity=identity,
        knowledge=None,
        refresh_scheduler=None,
        tool_trace_collector=[],
        run_id_callback=None,
        tool_dispatch=ToolDispatchContext(execution_identity=identity),
        progress=None,
    )
    if status is RunStatus.error:
        with pytest.raises(RuntimeError, match="Terminal response"):
            await settlement
    else:
        result = await settlement
        assert isinstance(result, CompletedApprovalRun)
        assert result.response_text == "Terminal response"


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["history", "projected_child"])
async def test_old_or_child_tool_changes_keep_parent_terminal_content(source: str) -> None:
    """Only newly completed parent tools can request another parent continuation attempt."""
    tool = ToolExecution(
        tool_call_id="old-continue",
        tool_name="switch_thread_model",
        result=json.dumps(
            {"action": "switch", "model": "large", "status": "ok", "tool": "thread_model", "when": "next-turn"},
        ),
    )
    presentation = CollectedStreamPresentation(show_tool_calls=False)
    parent_tools = [tool]
    events: list[RunEvent] = []
    if source == "projected_child":
        events.append(ToolCompleted(replace(tool, tool_call_id="delegate:old-continue").call))
        parent_tools = [ToolExecution(tool_call_id="delegate", tool_name="run_subagent", result="Child finished.")]
    events.append(_finished(run_id="parent", content="Final parent answer.", tools=parent_tools))

    collected = await _collect_agent_continuation(_events(*events), presentation, progress=None)

    assert presentation.final_text() == ""
    assert collected.terminal_content == "Final parent answer."
    assert collected.tool_executions == ()


@pytest.mark.asyncio
async def test_the_calls_a_continuation_completed_are_its_runs_own_entries() -> None:
    """The continuation reports the parent's calls it completed as the run's record holds them."""
    earlier = ToolExecution(tool_call_id="earlier", tool_name="inspect", result="before")
    current = ToolExecution(tool_call_id="current", tool_name="inspect", result="after")
    finished = _finished(run_id="parent", content="Done.", tools=[earlier, current])
    events = [ToolStarted(current.call), ToolCompleted(current.call), finished]

    presentation = CollectedStreamPresentation(show_tool_calls=False)
    collected = await _collect_agent_continuation(_events(*events), presentation, progress=None)

    assert len(collected.tool_executions) == 1
    assert collected.tool_executions[0] is current


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["finish", "pause"])
async def test_approved_run_streams_progress_from_its_saved_presentation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    outcome: str,
) -> None:
    """Live progress resumes the saved reply, settles the approved tool, then follows the new work."""
    paths = resolve_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path / "storage",
        process_env={"MATRIX_HOMESERVER": "https://matrix.example.org", "MINDROOM_NAMESPACE": ""},
    )
    config = bind_runtime_paths(
        Config.model_validate(
            {
                "defaults": {"tools": []},
                "memory": {"backend": "none"},
                "agents": {"general": {"display_name": "General", "tools": ["calculator"]}},
                "models": {"default": {"provider": "synthetic", "id": "synthetic"}},
                "tool_approval": {
                    "default": "auto_approve",
                    "rules": [
                        {"match": "add", "action": "require_approval"},
                        {"match": "multiply", "action": "require_approval"},
                    ],
                },
            },
        ),
        paths,
    )
    identity = _identity("progress-session")
    model = _CheckedModel(
        [
            _call("add", "approved", a=2, b=3),
            *(
                [_call("multiply", "gated", a=5, b=2)]
                if outcome == "pause"
                else [_call("subtract", "second", a=5, b=1), answer("Both results are ready.")]
            ),
        ],
    )
    monkeypatch.setattr(agents, "_build_agent_model_provider", lambda *_args, **_kwargs: model)
    paused = await _paused_turn(config, paths, identity, "Add, then keep going.")
    saved = CollectedStreamPresentation(show_tool_calls=True, response_text="Adding first.")
    saved.start_tool(paused.tools[0].call)
    continuation = approval_continuation(
        approval_id="approval-progress",
        run_id=paused.run_id,
        session_id=identity.session_id,
        room_id=identity.room_id,
        thread_id=identity.thread_id,
        requester_id=identity.requester_id,
        state="claimed",
        calls=(
            ApprovalCall(
                "approved",
                "add",
                "general",
                2**62,
                toolkit_name="calculator",
                arguments_digest=approval_arguments_digest({"a": 2, "b": 3}),
            ),
        ),
        request_body="Add, then keep going.",
        show_tool_calls=True,
    )
    runner = unwrap_extracted_collaborator(_bot(tmp_path / "runner")._response_runner)
    execution = replace(runner._approval_execution, config=lambda: config, runtime_paths=paths)
    published: list[StructuredStreamChunk] = []

    async def progress(chunk: StructuredStreamChunk) -> None:
        published.append(chunk)

    result = await execution.continue_run(
        continuation,
        paused_answer=PausedAnswer(text=saved.response_text, tool_trace=tuple(saved.tool_trace)),
        execution_identity=identity,
        tool_dispatch=ToolDispatchContext(execution_identity=identity),
        decisions={"approved": True},
        denial_reasons={"approved": None},
        tool_trace_collector=[],
        typing_log_context={},
        progress=progress,
    )

    contents = [chunk.content for chunk in published]
    assert contents[0] == saved.response_text
    resumed = [content for content in contents if content != saved.response_text]
    assert resumed[0] == "Adding first.\n\n🔧 `add` [1]\n\n"
    assert all("🔧 `add` [1] ⏳" not in content for content in resumed)
    if outcome == "pause":
        # The gated call is announced as it is planned, so the pending marker reaches the room before the pause.
        assert isinstance(result, PausedAttempt)
        assert "🔧 `multiply` [2] ⏳" in resumed[1]
        assert len(resumed) == 2
        assert result.response_text == resumed[-1]
        return
    assert isinstance(result, CompletedApprovalRun)
    assert "🔧 `subtract` [2] ⏳" in resumed[1]
    assert contents[-1] == result.response_text
    assert result.response_text.endswith("Both results are ready.")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("before", "saved", "reply"),
    [
        pytest.param("Adding first.", None, "Adding first.", id="wrote-before-the-pause"),
        pytest.param(None, None, "I have tools to execute, but I need confirmation.", id="paused-without-text"),
        pytest.param(None, "", "Tool approval continuation completed", id="pause-showed-nothing"),
    ],
)
async def test_an_approved_run_that_answers_without_text_shows_its_earlier_text_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    before: str | None,
    saved: str | None,
    reply: str,
) -> None:
    """A run that answers its approved call without text adds nothing to the reply it continues.

    Its run still holds what it carried over from the pause: the text it wrote before it, or the paused notice, which
    the blocking pause's saved reply already shows. A saved reply that showed nothing, as a streamed pause without
    text leaves it, ends with the completion note instead of the paused notice, which no longer holds once the call
    ran.
    """
    paths = resolve_runtime_paths(config_path=tmp_path / "config.yaml", storage_path=tmp_path / "storage")
    config = bind_runtime_paths(
        Config.model_validate(
            {
                "defaults": {"tools": []},
                "memory": {"backend": "none"},
                "agents": {"general": {"display_name": "General", "tools": ["calculator"]}},
                "models": {"default": {"provider": "synthetic", "id": "synthetic"}},
                "tool_approval": {"rules": [{"match": "add", "action": "require_approval"}]},
            },
        ),
        paths,
    )
    identity = _identity("silent-session")
    model = _CheckedModel([calls(tool_call("add", "approved", a=2, b=3), content=before), answer("")])
    monkeypatch.setattr(agents, "_build_agent_model_provider", lambda *_args, **_kwargs: model)
    paused = await _paused_turn(config, paths, identity, "Add them.", show_tool_calls=False)
    continuation = approval_continuation(
        approval_id="approval-silent",
        run_id=paused.run_id,
        session_id=identity.session_id,
        room_id=identity.room_id,
        thread_id=identity.thread_id,
        requester_id=identity.requester_id,
        state="claimed",
        calls=(
            ApprovalCall(
                "approved",
                "add",
                "general",
                2**62,
                toolkit_name="calculator",
                arguments_digest=approval_arguments_digest({"a": 2, "b": 3}),
            ),
        ),
        request_body="Add them.",
        show_tool_calls=False,
    )
    runner = unwrap_extracted_collaborator(_bot(tmp_path / "runner")._response_runner)
    execution = replace(runner._approval_execution, config=lambda: config, runtime_paths=paths)

    result = await execution.continue_run(
        continuation,
        paused_answer=PausedAnswer(text=paused.response_text if saved is None else saved, tool_trace=paused.tool_trace),
        execution_identity=identity,
        tool_dispatch=ToolDispatchContext(execution_identity=identity),
        decisions={"approved": True},
        denial_reasons={"approved": None},
        tool_trace_collector=[],
        typing_log_context={},
        progress=None,
    )

    assert isinstance(result, CompletedApprovalRun)
    assert result.response_text == reply


@pytest.mark.asyncio
async def test_an_approved_run_hands_its_session_to_queued_notice_finalization(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The continuation registers the agent session it resumed, so a notice delivered during it is finalized there."""
    paths = resolve_runtime_paths(config_path=tmp_path / "config.yaml", storage_path=tmp_path / "storage")
    config = bind_runtime_paths(
        Config.model_validate(
            {
                "defaults": {"tools": []},
                "memory": {"backend": "none"},
                "agents": {"general": {"display_name": "General", "tools": ["calculator"]}},
                "models": {"default": {"provider": "synthetic", "id": "synthetic"}},
                "tool_approval": {"rules": [{"match": "add", "action": "require_approval"}]},
            },
        ),
        paths,
    )
    identity = _identity("notice-session")
    model = _CheckedModel([_call("add", "approved", a=2, b=3), answer("Five.")])
    monkeypatch.setattr(agents, "_build_agent_model_provider", lambda *_args, **_kwargs: model)
    paused = await _paused_turn(config, paths, identity, "Add them.")
    continuation = approval_continuation(
        approval_id="approval-notice",
        run_id=paused.run_id,
        session_id=identity.session_id,
        room_id=identity.room_id,
        thread_id=identity.thread_id,
        requester_id=identity.requester_id,
        state="claimed",
        calls=(
            ApprovalCall(
                "approved",
                "add",
                "general",
                2**62,
                toolkit_name="calculator",
                arguments_digest=approval_arguments_digest({"a": 2, "b": 3}),
            ),
        ),
        request_body="Add them.",
    )
    runner = unwrap_extracted_collaborator(_bot(tmp_path / "runner")._response_runner)
    execution = replace(runner._approval_execution, config=lambda: config, runtime_paths=paths)

    with queued_message_signal_context(None) as notices:
        result = await execution.continue_run(
            continuation,
            paused_answer=PausedAnswer(),
            execution_identity=identity,
            tool_dispatch=ToolDispatchContext(execution_identity=identity),
            decisions={"approved": True},
            denial_reasons={"approved": None},
            tool_trace_collector=[],
            typing_log_context={},
            progress=None,
        )

    assert isinstance(result, CompletedApprovalRun)
    assert list(notices.storage_targets) == [("general", identity.session_id, "agent")]


@pytest.mark.asyncio
async def test_fresh_attempt_pause_publishes_its_progress_before_returning_the_pause(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A fresh model attempt after approval streams its text, then hands its pause back with that presentation."""
    paths = resolve_runtime_paths(config_path=tmp_path / "config.yaml", storage_path=tmp_path / "storage")
    config = Config.model_validate({"agents": {"general": {"display_name": "General", "tools": []}}})
    identity = _identity("session")
    continuation = approval_continuation(
        approval_id="approval-fresh-pause",
        run_id="run-saved",
        session_id="session",
        room_id="!room:example.org",
        requester_id="@user:example.org",
        state="claimed",
        show_tool_calls=True,
    )
    gated = ToolExecution(tool_call_id="call-2", tool_name="publish_report", tool_args={})

    async def stream(_ctx: ResponseTurnContext, **_kwargs: object) -> AsyncIterator[object]:
        yield TextDelta(" Next, publishing.")
        yield ToolStarted(gated.call)
        raise ResponsePausedForApproval(
            PausedAttempt(session_id="session", run_id="run-fresh", tools=(gated,), toolkit_owners={}),
        )

    monkeypatch.setattr("mindroom.approval.execution.stream_agent_response", stream)
    published: list[str] = []

    async def progress(chunk: StructuredStreamChunk) -> None:
        published.append(chunk.content)

    result = await _stream_continuation_turn(
        _continuation_turn_context(continuation, model_name="default", metadata=None),
        CollectedStreamPresentation(show_tool_calls=True, response_text="Checked."),
        prompt=continuation.request_body,
        show_tool_calls=continuation.show_tool_calls,
        resumed_attempt=ResumedAttempt(CompletedAttempt()),
        config=config,
        runtime_paths=paths,
        execution_identity=identity,
        knowledge=None,
        refresh_scheduler=None,
        tool_trace_collector=[],
        run_id_callback=None,
        tool_dispatch=ToolDispatchContext(execution_identity=identity),
        progress=progress,
    )

    assert isinstance(result, PausedAttempt)
    assert result.run_id == "run-fresh"
    assert published == ["Checked. Next, publishing.", "Checked. Next, publishing.\n\n🔧 `publish_report` [1] ⏳\n\n"]
    assert result.response_text == published[-1].rstrip()

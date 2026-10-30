"""Approved runs finish their task, including after a same-turn continuation rebuilds the agent."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any

import pytest

from mindroom.ai import ai_response, stream_agent_response
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
from mindroom.event_journal import ApprovalCall, ApprovalDecision, approval_arguments_digest
from mindroom.history.interrupted_replay import is_interrupted_replay_run
from mindroom.llm.errors import ProviderError
from mindroom.llm.events import RunFinished, TextDelta, ToolCompleted, ToolStarted
from mindroom.llm.provider import ToolCallReady
from mindroom.llm.runs import RunRecord, RunStatus, ToolExecution
from mindroom.message_target import MessageTarget
from mindroom.reply_presentation import Presentation, Segment, decode_presentation, encode_presentation
from mindroom.response_runner import _paused_with_committed_presentation
from mindroom.response_turn import (
    CompletedApprovalRun,
    CompletedAttempt,
    PausedAnswer,
    PausedAttempt,
    ResponsePausedForApproval,
    TurnResume,
    TurnSinks,
)
from mindroom.session_storage.agent_storage import create_session_storage
from mindroom.tool_system.events import (
    CollectedStreamPresentation,
    StructuredStreamChunk,
    ToolTraceEntry,
)
from mindroom.tool_system.runtime_context import (
    current_execution_identity,
)
from mindroom.tool_system.worker_routing import ToolExecutionIdentity
from tests.approval_continuation_helpers import RECEIPT_CALLS, approval_continuation, carries_approval_receipt
from tests.conftest import (
    bind_runtime_paths,
    install_model_providers,
    make_turn_context,
    record_store_threads,
    unwrap_extracted_collaborator,
)
from tests.delegation_helpers import _delegate_runtime_context
from tests.fakes.scripted_model import Await, ScriptedModel, ScriptStep, answer, calls, tool_call, usage
from tests.response_runner_helpers import _bot
from tests.scripted_agent_helpers import stop_turn, stored_runs
from tests.usage_helpers import counted_total, counted_usage

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from mindroom.constants import RuntimePaths
    from mindroom.llm.events import RunEvent
    from mindroom.llm.provider import ModelRequest, StreamEvent
    from mindroom.response_turn import ResponseTurnContext
    from mindroom.tool_system.runtime_context import ToolRuntimeContext


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


def _tool_context(identity: ToolExecutionIdentity, config: Config, paths: RuntimePaths) -> ToolRuntimeContext:
    """Return the tool context of the turn ``identity`` names, as its bot builds it."""
    return _delegate_runtime_context(config, paths, execution_identity=identity, agent_name=identity.agent_name)


def _identity(session_id: str) -> ToolExecutionIdentity:
    return ToolExecutionIdentity(
        channel="matrix",
        agent_name="general",
        requester_id="@user:example.org",
        room_id="!room:example.org",
        thread_id="$thread",
        resolved_thread_id="$thread",
        session_id=session_id,
        transport_agent_name="general",
    )


async def _paused_turn(
    config: Config,
    paths: RuntimePaths,
    identity: ToolExecutionIdentity,
    prompt: str,
    *,
    show_tool_calls: bool = True,
) -> PausedAttempt:
    """Run one reply of ``general`` as the response runner does and return the pause it suspended on, as it shows."""
    ctx = replace(
        make_turn_context(
            "general",
            session_id=identity.session_id,
            room_id=identity.room_id,
            thread_id=identity.thread_id,
            requester_id=identity.requester_id,
        ),
        prompt=prompt,
        execution_identity=identity,
        show_tool_calls=show_tool_calls,
        supports_native_tool_approval=True,
    )
    with pytest.raises(ResponsePausedForApproval) as raised:
        await ai_response(ctx, paths, config)
    return _paused_with_committed_presentation(raised.value, show_tool_calls=show_tool_calls)


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
    install_model_providers(monkeypatch, {"default": model})
    with pytest.raises(ResponsePausedForApproval) as raised:
        await ai_response(
            replace(
                make_turn_context("general", session_id="deferred-approval"),
                prompt="Add two and three.",
                show_tool_calls=show_tool_calls,
                supports_native_tool_approval=True,
            ),
            paths,
            config,
        )
    paused = _paused_with_committed_presentation(raised.value, show_tool_calls=show_tool_calls)
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

    Each resumed run reads its approval's receipt, and a fresh attempt after it does not.
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
        **_kwargs: object,
    ) -> ScriptedModel:
        if outcome == "preparation_error" and len(model.requests) == 2:
            msg = "Model preparation failed"
            raise RuntimeError(msg)
        models.append(model_name)
        return model

    monkeypatch.setattr("mindroom.providers.registry.build_model_provider", build_model)
    executed: list[str] = []
    original_add = CalculatorTools.add
    original_sleep = SleepTools.sleep

    def add(self: CalculatorTools, a: float, b: float) -> str:
        assert current_execution_identity() == identity
        executed.append("add")
        return original_add(self, a, b)

    async def sleep(self: SleepTools, seconds: int) -> str:
        assert current_execution_identity() == identity
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
                decision=ApprovalDecision.APPROVED,
                toolkit_name="calculator",
                arguments_digest=approval_arguments_digest({"a": 2, "b": 3}),
            ),
        ),
        request_body=prompt,
        show_tool_calls=show_tool_calls,
        continuation_count=2 if outcome == "immediate_pause" else 0,
    )
    runner = unwrap_extracted_collaborator(_bot(tmp_path / "runner")._response_runner)
    execution = replace(runner._approvals._approval_execution, config=lambda: config, runtime_paths=paths)
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
    tool_context = replace(context, config=config, config_provider=None, runtime_paths=paths)
    trace: list[ToolTraceEntry] = []
    run_ids: list[str] = []

    def stream_noting_attempts(
        ctx: ResponseTurnContext,
        runtime_paths: RuntimePaths,
        config: Config,
        *,
        sinks: TurnSinks,
        **kwargs: Any,  # noqa: ANN401
    ) -> AsyncIterator[object]:
        attempt_sinks = replace(sinks, on_attempt_start=lambda run_id, _model_name: run_ids.append(run_id))
        return stream_agent_response(ctx, runtime_paths, config, sinks=attempt_sinks, **kwargs)

    monkeypatch.setattr("mindroom.approval.execution.stream_agent_response", stream_noting_attempts)
    paused_answer = PausedAnswer()
    resume_starts: list[int] = []

    async def resume() -> CompletedApprovalRun | PausedAttempt:
        resume_starts.append(len(model.requests))
        with approval_receipt_context(RECEIPT_CALLS, run_id=continuation.run_id):
            return await execution.continue_run(
                continuation,
                paused_answer=paused_answer,
                tool_context=tool_context,
                tool_trace_collector=trace,
                typing_log_context={},
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
                    decision=ApprovalDecision.APPROVED,
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
    install_model_providers(monkeypatch, {"default": _CheckedModel([])})
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

    collected = await _collect_agent_continuation(_events(*events), presentation, run_id="run", progress=None)
    trace: list[ToolTraceEntry] = []
    result = await _stream_continuation_turn(
        _continuation_turn_context(continuation, model_name="default", metadata=None),
        presentation,
        prompt=continuation.request_body,
        resume=TurnResume(CompletedAttempt(response_text=collected.terminal_content)),
        config=config,
        runtime_paths=paths,
        knowledge=None,
        refresh_scheduler=None,
        tool_trace_collector=trace,
        tool_context=_tool_context(identity, config, paths),
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

    async def stream(*_args: object, sinks: TurnSinks, **_kwargs: object) -> AsyncIterator[TextDelta]:
        assert sinks.run_metadata_collector is not None
        assert sinks.on_completed is not None
        if wire_status is not None:
            sinks.run_metadata_collector[AI_RUN_METADATA_KEY] = {"status": wire_status}
        yield TextDelta("Terminal response")
        sinks.on_completed(CompletedAttempt(status=status))

    monkeypatch.setattr("mindroom.approval.execution.stream_agent_response", stream)
    settlement = _stream_continuation_turn(
        _continuation_turn_context(continuation, model_name="default", metadata=None),
        CollectedStreamPresentation(show_tool_calls=False),
        prompt=continuation.request_body,
        resume=TurnResume(CompletedAttempt()),
        config=config,
        runtime_paths=paths,
        knowledge=None,
        refresh_scheduler=None,
        tool_trace_collector=[],
        tool_context=_tool_context(identity, config, paths),
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

    collected = await _collect_agent_continuation(_events(*events), presentation, run_id="run", progress=None)

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
    collected = await _collect_agent_continuation(_events(*events), presentation, run_id="run", progress=None)

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
    install_model_providers(monkeypatch, {"default": model})
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
                decision=ApprovalDecision.APPROVED,
                toolkit_name="calculator",
                arguments_digest=approval_arguments_digest({"a": 2, "b": 3}),
            ),
        ),
        request_body="Add, then keep going.",
        show_tool_calls=True,
    )
    runner = unwrap_extracted_collaborator(_bot(tmp_path / "runner")._response_runner)
    execution = replace(runner._approvals._approval_execution, config=lambda: config, runtime_paths=paths)
    published: list[StructuredStreamChunk] = []

    async def progress(chunk: StructuredStreamChunk) -> None:
        published.append(chunk)

    result = await execution.continue_run(
        continuation,
        paused_answer=PausedAnswer(text=saved.response_text, tool_trace=tuple(saved.tool_trace)),
        tool_context=_tool_context(identity, config, paths),
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
    ("before", "reply"),
    [
        pytest.param("Adding first.", "Adding first.", id="wrote-before-the-pause"),
        pytest.param(None, "Tool approval continuation completed", id="paused-without-text"),
    ],
)
async def test_an_approved_run_that_answers_without_text_shows_its_earlier_text_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    before: str | None,
    reply: str,
) -> None:
    """A run that answers its approved call without text adds nothing to the reply it continues.

    Its run still holds what it carried over from the pause: the text it wrote before it, which the pause's saved
    reply already shows, or the paused notice, which no pause shows. A run that paused without text shows nothing,
    so its reply ends with the completion note instead of the paused notice, which no longer holds once the call ran.
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
    install_model_providers(monkeypatch, {"default": model})
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
                decision=ApprovalDecision.APPROVED,
                toolkit_name="calculator",
                arguments_digest=approval_arguments_digest({"a": 2, "b": 3}),
            ),
        ),
        request_body="Add them.",
        show_tool_calls=False,
    )
    runner = unwrap_extracted_collaborator(_bot(tmp_path / "runner")._response_runner)
    execution = replace(runner._approvals._approval_execution, config=lambda: config, runtime_paths=paths)
    threads = record_store_threads(monkeypatch, "load_session", "get_run")

    result = await execution.continue_run(
        continuation,
        paused_answer=PausedAnswer(text=paused.response_text, tool_trace=paused.tool_trace),
        tool_context=_tool_context(identity, config, paths),
        tool_trace_collector=[],
        typing_log_context={},
        progress=None,
    )

    assert isinstance(result, CompletedApprovalRun)
    assert result.response_text == reply
    # The continuation first reads its session and its paused run in the database's lane.
    assert threads["load_session"][0].startswith("conversation-store"), threads
    assert threads["get_run"][0].startswith("conversation-store"), threads


@pytest.mark.asyncio
async def test_an_approved_run_s_reply_publishes_the_usage_of_the_resumed_run_and_the_attempt_after_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A resumed run that a model switch continues publishes its own usage, both legs, with its continuation's.

    That is the usage the ledger counts: the resumed run replaced its paused record, and the continuation ran apart.
    """
    paths = resolve_runtime_paths(config_path=tmp_path / "config.yaml", storage_path=tmp_path / "storage")
    config = bind_runtime_paths(
        Config.model_validate(
            {
                "defaults": {"tools": []},
                "memory": {"backend": "none"},
                "agents": {"general": {"display_name": "General", "tools": ["calculator", "thread_model"]}},
                "models": {"default": {"provider": "synthetic", "id": "synthetic"}},
                "tool_approval": {"rules": [{"match": "add", "action": "require_approval"}]},
            },
        ),
        paths,
    )
    identity = _identity("usage-session")
    switch = tool_call("switch_thread_model", "continue", model_name="default", when="next-turn")
    model = _CheckedModel(
        [
            calls(tool_call("add", "approved", a=2, b=3), metrics=usage(10, 1)),
            calls(switch, metrics=usage(20, 2)),
            answer("Done.", usage(30, 3)),
        ],
    )
    install_model_providers(monkeypatch, {"default": model})
    paused = await _paused_turn(config, paths, identity, "Add them.", show_tool_calls=False)
    continuation = approval_continuation(
        approval_id="approval-usage",
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
                decision=ApprovalDecision.APPROVED,
                toolkit_name="calculator",
                arguments_digest=approval_arguments_digest({"a": 2, "b": 3}),
            ),
        ),
        request_body="Add them.",
        show_tool_calls=False,
    )
    runner = unwrap_extracted_collaborator(_bot(tmp_path / "runner")._response_runner)
    execution = replace(runner._approvals._approval_execution, config=lambda: config, runtime_paths=paths)

    result = await execution.continue_run(
        continuation,
        paused_answer=PausedAnswer(text=paused.response_text, tool_trace=paused.tool_trace),
        tool_context=_tool_context(identity, config, paths),
        tool_trace_collector=[],
        typing_log_context={},
        progress=None,
    )

    assert isinstance(result, CompletedApprovalRun)
    assert result.response_text.endswith("Done.")
    storage = create_session_storage("general", config, paths, execution_identity=identity)
    assert counted_usage(storage, identity.session_id) == [(30, 3), (30, 3)]
    published = result.metadata_content[AI_RUN_METADATA_KEY]["usage"]
    assert (published["input_tokens"], published["output_tokens"]) == counted_total(storage, identity.session_id)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("show_tool_calls", "pause", "reply"),
    [
        pytest.param(False, "", "The sum is 5.", id="tool-calls-hidden"),
        pytest.param(True, "🔧 `add` [1] ⏳", "🔧 `add` [1]\n\nThe sum is 5.", id="tool-calls-shown"),
    ],
)
async def test_a_run_that_paused_without_text_shows_no_paused_notice_in_its_pause_or_its_reply(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    show_tool_calls: bool,
    pause: str,
    reply: str,
) -> None:
    """A blocking reply shows its pause and its approved reply without a paused notice.

    The run paused without text of its own, so its record holds no content: the approval card says what the pause
    waits for.
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
    identity = _identity("notice-free-session")
    model = _CheckedModel([calls(tool_call("add", "approved", a=2, b=3)), answer("The sum is 5.")])
    install_model_providers(monkeypatch, {"default": model})
    paused = await _paused_turn(
        config,
        paths,
        identity,
        "Add them.",
        show_tool_calls=show_tool_calls,
    )
    assert paused.response_text.strip() == pause
    [stored] = stored_runs(config, paths, identity.session_id)
    assert stored.content is None
    continuation = approval_continuation(
        approval_id="approval-notice-free",
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
                decision=ApprovalDecision.APPROVED,
                toolkit_name="calculator",
                arguments_digest=approval_arguments_digest({"a": 2, "b": 3}),
            ),
        ),
        request_body="Add them.",
        show_tool_calls=show_tool_calls,
    )
    runner = unwrap_extracted_collaborator(_bot(tmp_path / "runner")._response_runner)
    execution = replace(runner._approvals._approval_execution, config=lambda: config, runtime_paths=paths)

    result = await execution.continue_run(
        continuation,
        paused_answer=PausedAnswer(text=paused.response_text, tool_trace=paused.tool_trace),
        tool_context=_tool_context(identity, config, paths),
        tool_trace_collector=[],
        typing_log_context={},
        progress=None,
    )

    assert isinstance(result, CompletedApprovalRun)
    assert result.response_text.strip() == reply


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
    install_model_providers(monkeypatch, {"default": model})
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
                decision=ApprovalDecision.APPROVED,
                toolkit_name="calculator",
                arguments_digest=approval_arguments_digest({"a": 2, "b": 3}),
            ),
        ),
        request_body="Add them.",
    )
    runner = unwrap_extracted_collaborator(_bot(tmp_path / "runner")._response_runner)
    execution = replace(runner._approvals._approval_execution, config=lambda: config, runtime_paths=paths)

    with queued_message_signal_context(None) as notices:
        result = await execution.continue_run(
            continuation,
            paused_answer=PausedAnswer(),
            tool_context=_tool_context(identity, config, paths),
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

    async def stream(*_args: object, **_kwargs: object) -> AsyncIterator[object]:
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
        resume=TurnResume(CompletedAttempt()),
        config=config,
        runtime_paths=paths,
        knowledge=None,
        refresh_scheduler=None,
        tool_trace_collector=[],
        tool_context=_tool_context(identity, config, paths),
        progress=progress,
    )

    assert isinstance(result, PausedAttempt)
    assert result.run_id == "run-fresh"
    assert published == ["Checked. Next, publishing.", "Checked. Next, publishing.\n\n🔧 `publish_report` [1] ⏳\n\n"]
    assert result.response_text == published[-1].rstrip()


@pytest.mark.asyncio
async def test_a_call_that_failed_before_an_approval_replays_as_failed_when_the_approved_turn_is_stopped(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The reply's saved trace keeps which call failed before the pause, so a stop after the approval says so."""
    paths = resolve_runtime_paths(config_path=tmp_path / "config.yaml", storage_path=tmp_path / "storage")
    config = bind_runtime_paths(
        Config.model_validate(
            {
                "defaults": {"tools": []},
                "memory": {"backend": "none"},
                "agents": {"general": {"display_name": "General", "tools": ["calculator", "thread_model"]}},
                "models": {"default": {"provider": "synthetic", "id": "synthetic"}},
                "tool_approval": {"rules": [{"match": "add", "action": "require_approval"}]},
            },
        ),
        paths,
    )
    identity = _identity("failed-call-session")

    def subtract(self: CalculatorTools, a: float, b: float) -> str:
        del self, a, b
        msg = "subtract is unavailable"
        raise RuntimeError(msg)

    monkeypatch.setattr(CalculatorTools, "subtract", subtract)
    model = _CheckedModel(
        [
            _call("subtract", "failed", a=5, b=1),
            _call("add", "approved", a=2, b=3),
            _continue_call("continue"),
            [stop_turn()],
        ],
    )
    install_model_providers(monkeypatch, {"default": model})
    paused = await _paused_turn(config, paths, identity, "Subtract, then add.")
    # The resume reads the paused answer back from the reply's records, as the reply store keeps it.
    saved = decode_presentation(
        encode_presentation(
            Presentation(
                segments=(Segment(kind="answer", text=paused.response_text, tool_trace=paused.tool_trace),),
            ),
        ),
    )
    [segment] = saved.segments
    continuation = approval_continuation(
        approval_id="approval-failed-call",
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
                decision=ApprovalDecision.APPROVED,
                toolkit_name="calculator",
                arguments_digest=approval_arguments_digest({"a": 2, "b": 3}),
            ),
        ),
        request_body="Subtract, then add.",
        show_tool_calls=True,
    )
    runner = unwrap_extracted_collaborator(_bot(tmp_path / "runner")._response_runner)
    execution = replace(runner._approvals._approval_execution, config=lambda: config, runtime_paths=paths)

    with pytest.raises(asyncio.CancelledError):
        await execution.continue_run(
            continuation,
            paused_answer=PausedAnswer(text=segment.text, tool_trace=segment.tool_trace),
            tool_context=_tool_context(identity, config, paths),
            tool_trace_collector=[],
            typing_log_context={},
            progress=None,
        )

    [replay] = [run for run in stored_runs(config, paths, identity.session_id) if is_interrupted_replay_run(run)]
    content = replay.messages[-1].content
    assert isinstance(content, str)
    assert (
        '- The `subtract` tool failed with input preview "a=5, b=1" and error preview "subtract is unavailable".'
    ) in content

"""Approved runs finish their task after rebuilding dynamically changed tools."""

from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any

import pytest
from agno.models.response import ModelResponse, ToolExecution
from agno.run.agent import RunCompletedEvent, RunContentEvent, RunOutput, ToolCallCompletedEvent, ToolCallStartedEvent
from agno.run.base import RunStatus
from agno.tools.calculator import CalculatorTools
from agno.tools.sleep import SleepTools

from mindroom.agent_storage import create_session_storage
from mindroom.agents import create_agent
from mindroom.ai import ai_response
from mindroom.approval_execution import _collect_agent_continuation, _settle_agent_continuation
from mindroom.approval_tools import toolkit_owners_for_agents
from mindroom.config.main import Config
from mindroom.constants import AI_RUN_METADATA_KEY, resolve_runtime_paths
from mindroom.event_journal import ApprovalCall, ApprovalContinuation
from mindroom.history.session_context import close_agent_runtime_state_dbs
from mindroom.mcp.toolkit import bind_mcp_server_manager
from mindroom.message_target import MessageTarget
from mindroom.response_sources import ResponseSources
from mindroom.response_turn import (
    CompletedApprovalRun,
    CompletedAttempt,
    PausedAttempt,
    ResponsePausedForApproval,
    ResumedAttempt,
    paused_attempt_from_response,
)
from mindroom.synthetic_model import SyntheticModel
from mindroom.tool_system.events import CollectedStreamPresentation, ToolTraceEntry, serialize_tool_trace
from mindroom.tool_system.runtime_context import LiveToolDispatchContext, ToolDispatchContext
from mindroom.tool_system.worker_routing import ToolExecutionIdentity, get_tool_execution_identity
from tests.conftest import bind_runtime_paths, make_turn_context, unwrap_extracted_collaborator
from tests.response_runner_helpers import _bot

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable, Iterator, Mapping, Sequence
    from pathlib import Path

    from agno.models.message import Message

    from mindroom.constants import RuntimePaths
    from mindroom.response_turn import ResponseTurnContext


@pytest.fixture(autouse=True)
def _isolate_mcp_binding() -> Iterator[None]:
    """Built-in tool continuations must not inherit another test's MCP configuration."""
    bind_mcp_server_manager(None)
    yield
    bind_mcp_server_manager(None)


def _call(name: str, call_id: str, **arguments: object) -> ModelResponse:
    return ModelResponse(
        tool_calls=[
            {"id": call_id, "type": "function", "function": {"name": name, "arguments": json.dumps(arguments)}},
        ],
    )


@dataclass
class _ScriptedModel(SyntheticModel):
    """Replace only provider requests, checking the real schema on each call."""

    responses: list[ModelResponse | RuntimeError] = field(default_factory=list)
    requests: list[list[Message]] = field(default_factory=list)

    async def ainvoke(
        self,
        messages: list[Message],
        *,
        tools: Sequence[Mapping[str, Any]] | None = None,
        **_kwargs: object,
    ) -> ModelResponse:
        self.requests.append(deepcopy(messages))
        response = self.responses.pop(0)
        if isinstance(response, RuntimeError):
            raise response
        names = {tool["function"]["name"] for tool in tools or ()}
        for call in response.tool_calls or ():
            assert call["function"]["name"] in names
        return response

    async def ainvoke_stream(
        self,
        messages: list[Message],
        *,
        tools: Sequence[Mapping[str, Any]] | None = None,
        **kwargs: object,
    ) -> AsyncIterator[ModelResponse]:
        yield await self.ainvoke(messages, tools=tools, **kwargs)


@pytest.mark.asyncio
@pytest.mark.parametrize("show_tool_calls", [False, True])
async def test_disabled_dynamic_load_does_not_seed_discarded_tools_into_approval(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    show_tool_calls: bool,
) -> None:
    """A rebuilt ordinary call keeps its own visible numbering when background jobs are off."""
    paths = resolve_runtime_paths(config_path=tmp_path / "config.yaml", storage_path=tmp_path / "storage")
    config = bind_runtime_paths(
        Config.model_validate(
            {
                "defaults": {"tools": [], "learning": False},
                "memory": {"backend": "none"},
                "agents": {"general": {"display_name": "General", "tools": [{"calculator": {"defer": True}}]}},
                "models": {"default": {"provider": "synthetic", "id": "synthetic"}},
                "tool_approval": {"rules": [{"match": "add", "action": "require_approval"}]},
            },
        ),
        paths,
    )
    responses = [_call("load_tool", "loader", tool_name="calculator"), _call("add", "sum", a=2, b=3)]
    monkeypatch.setattr(
        "mindroom.agents._load_agent_model_instance",
        lambda *_args, **_kwargs: _ScriptedModel(id="synthetic", responses=responses),
    )
    with pytest.raises(ResponsePausedForApproval) as raised:
        await ai_response(
            make_turn_context("general", session_id="load-approval"),
            prompt="Add two and three.",
            config=config,
            runtime_paths=paths,
            show_tool_calls=show_tool_calls,
            supports_native_tool_approval=True,
        )
    paused = raised.value.paused
    assert not responses
    assert [entry.tool_name for entry in paused.tools] == ["add"]
    assert [entry.tool_name for entry in paused.tool_trace] == ["add"]
    assert "load_tool" not in paused.response_text
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
async def test_approved_run_continues_after_loading_a_tool(  # noqa: C901, PLR0915
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    show_tool_calls: bool,
    outcome: str,
) -> None:
    """A fresh schema step retains history and may pause again without replaying approved work."""
    switch_when = {"switch_after": "after-toolcall", "switch_next": "next-turn"}.get(outcome)
    paths = resolve_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path / "storage",
        process_env={"MATRIX_HOMESERVER": "https://matrix.example.org", "MINDROOM_NAMESPACE": ""},
    )
    config = bind_runtime_paths(
        Config.model_validate(
            {
                "defaults": {"tools": [], "learning": False},
                "agents": {
                    "general": {
                        "display_name": "General",
                        "tools": ["calculator", {"sleep": {"defer": True}}, *(["thread_model"] if switch_when else [])],
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
    identity = ToolExecutionIdentity(
        channel="matrix",
        agent_name="general",
        requester_id="@user:example.org",
        room_id="!room:example.org",
        thread_id="$thread",
        resolved_thread_id="$thread",
        session_id="approval-session",
    )
    responses: list[ModelResponse | RuntimeError] = [
        _call("add", "approved", a=2, b=3),
        _call("load_tool", "loader", tool_name="sleep"),
        *(
            [_call("load_tool", f"loader-{index}", tool_name="sleep") for index in range(4)]
            if outcome == "limit"
            else [_call("sleep", "sleeper", seconds=0), ModelResponse(content="Finished the calculation and wait.")]
        ),
    ]
    if outcome == "error":
        responses[2:] = [RuntimeError("Provider continuation failed")]
    if outcome == "pause_limit":
        responses[3:] = [_call("load_tool", f"reload-{index}", tool_name="sleep") for index in range(4)]
    if outcome == "immediate_pause":
        responses[1:1] = [_call("add", "approved-again", a=4, b=5)]
    if switch_when:
        responses.insert(1, _call("switch_thread_model", "switcher", model_name="alternate", when=switch_when))
    requests: list[list[Message]] = []
    models: list[str] = []

    def load_model(
        _config: Config,
        _paths: RuntimePaths,
        model_name: str,
        _execution_identity: ToolExecutionIdentity | None = None,
        **_kwargs: object,
    ) -> _ScriptedModel:
        if outcome == "preparation_error" and len(requests) == 2:
            msg = "Model preparation failed"
            raise RuntimeError(msg)
        models.append(model_name)
        return _ScriptedModel(id="synthetic", responses=responses, requests=requests)

    monkeypatch.setattr("mindroom.agents._load_agent_model_instance", load_model)
    executed: list[str] = []
    original_add = CalculatorTools.add
    original_sleep = SleepTools.sleep

    def add(self: CalculatorTools, a: float, b: float) -> str:
        assert get_tool_execution_identity() == identity
        executed.append("add")
        return original_add(self, a, b)

    def sleep(self: SleepTools, seconds: int) -> str:
        assert get_tool_execution_identity() == identity
        executed.append("sleep")
        return original_sleep(self, seconds)

    monkeypatch.setattr(CalculatorTools, "add", add)
    monkeypatch.setattr(SleepTools, "sleep", sleep)
    storage = create_session_storage("general", config, paths, identity)
    actor = create_agent(
        "general",
        config,
        paths,
        identity,
        session_id=identity.session_id,
        history_storage=storage,
        dynamic_tool_continuation=True,
        supports_native_tool_approval=True,
    )
    prompt = "Calculate the sum, load the wait tool, and finish."
    try:
        paused = await actor.arun(prompt, session_id=identity.session_id, user_id=identity.requester_id)
        assert paused.status == RunStatus.paused
        assert executed == []
        captured = paused_attempt_from_response(
            paused,
            fallback_session_id=identity.session_id,
            fallback_run_id=paused.run_id,
            toolkit_owners=toolkit_owners_for_agents([actor]),
        )
        assert captured is not None
    finally:
        close_agent_runtime_state_dbs(actor, shared_scope_storage=storage)
        storage.close()
    continuation = ApprovalContinuation(
        approval_id="approval-example",
        run_id=paused.run_id,
        session_id=identity.session_id,
        entity_kind="agent",
        entity_name="general",
        room_id=identity.room_id,
        thread_id=identity.thread_id,
        requester_id=identity.requester_id,
        response_event_id="$waiting",
        sources=ResponseSources(("$source",), ("$source",)),
        state="claimed",
        calls=(ApprovalCall("approved", "add", "general", 2**62, toolkit_name="calculator"),),
        request_body=prompt,
        show_tool_calls=show_tool_calls,
        continuation_count=2 if outcome == "immediate_pause" else 0,
    )
    runner = unwrap_extracted_collaborator(_bot(tmp_path / "runner")._response_runner)
    execution = replace(runner._approval_execution, config=lambda: config, runtime_paths=paths)
    dispatch = ToolDispatchContext(execution_identity=identity)
    if switch_when:
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

    async def resume() -> CompletedApprovalRun | PausedAttempt:
        return await execution.continue_run(
            continuation,
            execution_identity=identity,
            tool_dispatch=dispatch,
            decisions={call.tool_call_id: True for call in continuation.calls},
            denial_reasons={call.tool_call_id: None for call in continuation.calls},
            tool_trace_collector=trace,
            typing_log_context={},
            run_id_callback=run_ids.append,
        )

    if outcome in {"error", "preparation_error"}:
        with pytest.raises(RuntimeError):
            await resume()
        assert executed == ["add"]
        assert len(responses) == (2 if outcome == "preparation_error" else 0)
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
        assert "Dynamic tool calls did not produce a final answer" in result.response_text
        assert "need confirmation" not in result.response_text
        assert len(run_ids) == 4
        assert executed == ["add"]
        assert not responses
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
            response_text=result.response_text,
            response_tool_trace=serialize_tool_trace(result.tool_trace, include_internal=True),
            calls=(ApprovalCall("sleeper", "sleep", "general", 2**62, toolkit_name="sleep"),),
        )
        result = await resume()
    if outcome == "pause_limit":
        assert isinstance(result, CompletedApprovalRun)
        assert "Dynamic tool calls did not produce a final answer" in result.response_text
        assert "need confirmation" not in result.response_text
        assert len(run_ids) == 4
        assert executed == ["add", "sleep"]
        assert not responses
        return
    assert isinstance(result, CompletedApprovalRun)
    assert result.metadata_content[AI_RUN_METADATA_KEY]["run_id"] == run_ids[-1]
    assert result.metadata_content[AI_RUN_METADATA_KEY]["session_id"] == identity.session_id
    assert "Finished the calculation and wait." in result.response_text
    assert "need confirmation" not in result.response_text
    assert executed == ["add", "sleep"]
    assert models[-1] == ("alternate" if switch_when == "after-toolcall" else "default")
    assert not responses
    assert any(message.role == "tool" and message.tool_call_id == "approved" for message in requests[-1])
    assert any(message.role == "tool" and message.tool_call_id == "loader" for message in requests[-1])
    expected_tools = ["add", *(["switch_thread_model"] if switch_when else []), "load_tool", "sleep"]
    assert [entry.tool_name for entry in trace] == (expected_tools if show_tool_calls else [])


@pytest.mark.asyncio
@pytest.mark.parametrize("show_tool_calls", [False, True])
@pytest.mark.parametrize("boundary", ["restored", "new"])
async def test_approval_settlement_preserves_tool_boundary(
    tmp_path: Path,
    *,
    show_tool_calls: bool,
    boundary: str,
) -> None:
    """Terminal-only approval text must cross the real driver handoff with its pending separator."""
    paths = resolve_runtime_paths(config_path=tmp_path / "config.yaml", storage_path=tmp_path / "storage")
    config = bind_runtime_paths(
        Config.model_validate({"agents": {"general": {"display_name": "General", "tools": []}}}),
        paths,
    )
    identity = ToolExecutionIdentity(
        channel="matrix",
        agent_name="general",
        requester_id="@user:example.org",
        room_id="!room:example.org",
        thread_id="$thread",
        resolved_thread_id="$thread",
        session_id="session",
    )
    continuation = ApprovalContinuation(
        approval_id="approval-boundary",
        run_id="run-saved",
        session_id="session",
        entity_kind="agent",
        entity_name="general",
        room_id=identity.room_id,
        thread_id=identity.thread_id,
        requester_id=identity.requester_id,
        response_event_id="$waiting",
        sources=ResponseSources(("$source",), ("$source",)),
        calls=(),
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

    async def events() -> AsyncIterator[object]:
        if boundary == "new":
            yield ToolCallStartedEvent(tool=tool)
            yield ToolCallCompletedEvent(tool=tool)
        yield RunCompletedEvent(content="After approval.")
        yield RunOutput(run_id="run-saved", session_id="session", status=RunStatus.completed, tools=[tool])

    collected = await _collect_agent_continuation(events(), presentation)
    trace: list[ToolTraceEntry] = []
    result = await _settle_agent_continuation(
        continuation,
        presentation,
        resumed_attempt=ResumedAttempt(CompletedAttempt(response_text=collected.terminal_content)),
        model_name="default",
        metadata=None,
        config=config,
        runtime_paths=paths,
        execution_identity=identity,
        knowledge=None,
        refresh_scheduler=None,
        tool_trace_collector=trace,
        run_id_callback=None,
        tool_dispatch=ToolDispatchContext(execution_identity=identity),
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
    identity = ToolExecutionIdentity(
        channel="matrix",
        agent_name="general",
        requester_id="@user:example.org",
        room_id="!room:example.org",
        thread_id="$thread",
        resolved_thread_id="$thread",
        session_id="session",
    )
    continuation = ApprovalContinuation(
        approval_id="approval-status",
        run_id="run-saved",
        session_id="session",
        entity_kind="agent",
        entity_name="general",
        room_id="!room:example.org",
        thread_id="$thread",
        requester_id="@user:example.org",
        response_event_id="$waiting",
        sources=ResponseSources(("$source",), ("$source",)),
        calls=(),
        state="claimed",
    )

    async def stream(
        _ctx: ResponseTurnContext,
        *,
        on_completed: Callable[[CompletedAttempt], None],
        run_metadata_collector: dict[str, Any],
        **_kwargs: object,
    ) -> AsyncIterator[RunContentEvent]:
        if wire_status is not None:
            run_metadata_collector[AI_RUN_METADATA_KEY] = {"status": wire_status}
        yield RunContentEvent(content="Terminal response")
        on_completed(CompletedAttempt(status=status))

    monkeypatch.setattr("mindroom.approval_execution.stream_agent_response", stream)
    settlement = _settle_agent_continuation(
        continuation,
        CollectedStreamPresentation(show_tool_calls=False),
        resumed_attempt=ResumedAttempt(CompletedAttempt()),
        model_name="default",
        metadata=None,
        config=config,
        runtime_paths=paths,
        execution_identity=identity,
        knowledge=None,
        refresh_scheduler=None,
        tool_trace_collector=[],
        run_id_callback=None,
        tool_dispatch=ToolDispatchContext(execution_identity=identity),
    )
    if status is RunStatus.error:
        with pytest.raises(RuntimeError, match="Terminal response"):
            await settlement
    else:
        result = await settlement
        assert isinstance(result, CompletedApprovalRun)
        assert result.response_text == "Terminal response"


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["history", "child", "projected_child"])
async def test_old_or_child_tool_changes_keep_parent_terminal_content(source: str) -> None:
    """Only newly completed parent tools can request another parent schema step."""
    tool = ToolExecution(
        tool_call_id="old-loader",
        tool_name="load_tool",
        result=json.dumps({"tool": "dynamic_tools", "status": "loaded", "tool_name": "sleep"}),
    )
    presentation = CollectedStreamPresentation(show_tool_calls=False)

    async def events() -> AsyncIterator[object]:
        parent_tools = [tool]
        if source == "child":
            yield ToolCallCompletedEvent(run_id="child", parent_run_id="parent", tool=tool)
        elif source == "projected_child":
            projected = replace(tool, tool_call_id="delegate:old-loader")
            yield ToolCallCompletedEvent(run_id="parent", tool=projected)
            parent_tools = [ToolExecution(tool_call_id="delegate", tool_name="run_subagent", result="Child finished.")]
        yield RunCompletedEvent(run_id="parent", content="Final parent answer.")
        yield RunOutput(run_id="parent", tools=parent_tools, status=RunStatus.completed, content="Final parent answer.")

    collected = await _collect_agent_continuation(events(), presentation)

    assert presentation.final_text() == ""
    assert collected.terminal_content == "Final parent answer."
    assert collected.tool_executions == ()

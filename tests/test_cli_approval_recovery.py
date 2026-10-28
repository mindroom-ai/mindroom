"""CLI approval recovery uses the existing claim and normal response driver."""

# ruff: noqa: ANN001, ANN002, ANN003, ANN202, ARG002, PLR0915

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import replace
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from agno.run import RunContext as AgnoRunContext  # noqa: TC002 - Agno resolves tool annotations
from agno.tools.function import Function, ToolResult
from agno.tools.toolkit import Toolkit

from mindroom import agents, ai
from mindroom.agent_cli import cli_approval_recovery, response_owner
from mindroom.agent_cli.approval import CliApprovalCall
from mindroom.agent_cli.bash import minimal_bash_toolkit
from mindroom.agent_cli.checkpoint import BatchCheckpoint
from mindroom.agent_cli.delegation import approval_calls_for_cli_pause
from mindroom.agent_cli.events import emit_cli_suspension
from mindroom.agent_cli.lifetime import current_cli_lifetime
from mindroom.agent_cli.protocol import ToolCallOperation
from mindroom.agent_cli.session import CliAuthenticationError, TurnToolRegistry
from mindroom.agent_cli.shell_contract import AgentCliShellEnv, current_agent_cli_shell_env
from mindroom.agent_loop.model_loop import EMPTY_REQUEST, LoopState
from mindroom.agent_loop.spec import RunContext
from mindroom.agent_loop.tools import PlannedCall, PreparedTools
from mindroom.approval import execution as approval_execution
from mindroom.approval import tool_owners as approval_tools
from mindroom.approval.receipt_readers import approval_receipt_context
from mindroom.config.agent import AgentConfig
from mindroom.config.approval import ApprovalRuleConfig, ToolApprovalConfig
from mindroom.event_journal import ApprovalCall, ApprovalDecision, approval_arguments_digest
from mindroom.history.types import HistoryScope
from mindroom.knowledge.read_protocol import SearchHit
from mindroom.knowledge.search import AgentKnowledge, KnowledgeSource, KnowledgeSourceDescription
from mindroom.llm.events import ToolCallOrigin
from mindroom.llm.media import Audio, Image
from mindroom.llm.messages import Message
from mindroom.llm.runs import RunRecord, RunRequirement, RunStatus, ToolExecution
from mindroom.media_inputs import MediaInputs
from mindroom.response_sources import ResponseSources
from mindroom.response_turn import (
    CompletedApprovalRun,
    PausedAnswer,
    ResponsePausedForApproval,
    TurnResume,
    apply_exact_approval_decisions,
)
from mindroom.run_storage import RunStorage
from mindroom.session_storage.agent_storage import create_session_storage, create_state_storage
from mindroom.session_storage.store import persist_to
from mindroom.session_storage.types import ConversationSession
from mindroom.tool_system.agent_tool_calls import PreparedAgentToolCatalog
from mindroom.tool_system.agno_toolkits import adapt_function, adapt_toolkit
from mindroom.tool_system.events import CollectedStreamPresentation
from mindroom.tool_system.runtime_context import (
    LiveToolDispatchContext,
    build_execution_identity_from_runtime_context,
    tool_runtime_context,
)
from mindroom.tool_system.tool_function import AdaptedToolkit, ToolOutcome
from tests.approval_continuation_helpers import (
    RECEIPT_CALLS,
    RECEIPT_TEXT,
    approval_continuation,
    carries_approval_receipt,
    receipts,
)
from tests.conftest import install_model_providers, stored_session_state, unwrap_extracted_collaborator
from tests.fakes.scripted_model import ScriptedModel, answer
from tests.history_helpers import store_fold
from tests.identity_helpers import persist_entity_accounts
from tests.minimal_agent_fixtures import (  # noqa: F401 - agent_cli_api is a pytest fixture
    ScriptedProvider,
    agent_cli_api,
    cli_window,
    install_scripted_shell,
    offered_tools,
    settled_receipt,
    shell_cli_owner,
)
from tests.response_runner_helpers import _bot
from tests.test_agent_cli_authority import _runtime_context, _turn_context

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.agent_loop.build import AgentBuild
    from mindroom.session_storage.store import ConversationStore
    from mindroom.tool_system.runtime_context import ToolRuntimeContext


def _minimal_build(runtime: ToolRuntimeContext, **options) -> AgentBuild:
    """Build the helper as approval recovery rebuilds a minimal agent."""
    return agents.create_agent(
        "helper",
        runtime.config,
        runtime.runtime_paths,
        build_execution_identity_from_runtime_context(runtime),
        agent_mode="minimal",
        supports_native_tool_approval=True,
        **({"knowledge": None, "active_model_name": None} | options),
    )


def _with_toolkit(build: AgentBuild, toolkit: AdaptedToolkit) -> AgentBuild:
    """Return ``build`` also offering ``toolkit``, each of its functions authored under its name."""
    authored = replace(
        toolkit,
        functions=tuple(
            replace(function, toolkit=toolkit.name, registered_toolkit=toolkit.name) for function in toolkit.functions
        ),
    )
    return replace(build, spec=replace(build.spec, toolkits=(*build.spec.toolkits, authored)))


def _saved(store: ConversationStore, session_id: str, record: RunRecord) -> tuple[RunRecord, ConversationSession]:
    """Store ``record`` as the run an approval paused in, and return it with its session as recovery reads them."""
    store.save_run(
        ConversationSession(session_id=session_id, kind="agent", owner_id="helper"),
        record,
        record_usage=False,
    )
    session = store.load_session(session_id, "agent")
    persisted = store.get_run(record.run_id)
    assert session is not None
    assert isinstance(persisted, RunRecord)
    return persisted, session


def _helper_runtime(tmp_path: Path, **helper) -> ToolRuntimeContext:
    runtime = _runtime_context(tmp_path)
    runtime.config.agents["helper"] = AgentConfig(display_name="Helper", memory_backend="file", **helper)
    runtime.config.administrators = [runtime.requester_id]
    return runtime


@pytest.mark.asyncio
async def test_approval_tool_restore_requires_current_tool_search_assignment(tmp_path: Path) -> None:
    """A saved approval owned by the search toolkit restores only while deferred tools remain assigned."""
    runtime = _runtime_context(tmp_path)
    runtime.config.agents["helper"] = AgentConfig(
        display_name="Helper",
        tools=["shell", {"sleep": {"defer": True}}],
    )
    call = ApprovalCall("search", "tool_search", "helper", 100, toolkit_name="dynamic_tools")
    identity = build_execution_identity_from_runtime_context(runtime)
    required = await approval_tools.required_approval_tool_names(
        "helper",
        (call,),
        config=runtime.config,
        runtime_paths=runtime.runtime_paths,
        execution_identity=identity,
    )
    assert required == ("dynamic_tools",)
    runtime.config.agents["helper"].tools = ["shell"]
    with pytest.raises(RuntimeError, match="no longer permitted"):
        await approval_tools.required_approval_tool_names(
            "helper",
            (call,),
            config=runtime.config,
            runtime_paths=runtime.runtime_paths,
            execution_identity=identity,
        )


class _Index:
    """A published index whose every search is recorded and answered with one chunk."""

    def __init__(self, searches: list[str]) -> None:
        self.searches = searches

    async def asearch(self, query: str, *, limit: int) -> list[SearchHit]:
        self.searches.append(query)
        return [SearchHit(content="saved generated memory")]


def _knowledge(searches: list[str]) -> AgentKnowledge:
    description = KnowledgeSourceDescription(base_id="notes", description="Saved notes")
    return AgentKnowledge(sources=(KnowledgeSource(description=description, index=_Index(searches)),))


def _approval_execution(tmp_path: Path, runtime: ToolRuntimeContext, monkeypatch, knowledge: AgentKnowledge | None):
    runner = unwrap_extracted_collaborator(_bot(tmp_path / "runner")._response_runner)
    execution = replace(
        runner._approvals._approval_execution,
        config=lambda: runtime.config,
        runtime_paths=runtime.runtime_paths,
    )
    monkeypatch.setattr(
        execution.knowledge_access,
        "resolve_for_agent_async",
        AsyncMock(return_value=SimpleNamespace(knowledge=knowledge)),
    )
    return execution


@pytest.mark.asyncio
@pytest.mark.usefixtures("agent_cli_api")
@pytest.mark.parametrize("continuation_count", [2, 4])
async def test_recovered_model_switch_call_retains_response_lifecycle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    continuation_count: int,
) -> None:
    """Recovery preserves knowledge, cancellation registration, typing and the remaining budget.

    The follow-up turn, built afresh after the switch, sends the approval receipt the runner binds.
    """
    runtime = _helper_runtime(tmp_path, tools=["shell", "thread_model"])
    runtime = replace(runtime, orchestrator=SimpleNamespace(agent_cli_registry=TurnToolRegistry()))
    persist_entity_accounts(runtime.config, runtime.runtime_paths)
    identity = build_execution_identity_from_runtime_context(runtime)
    knowledge = _knowledge([])
    builds = []
    real_create = agents.create_agent

    def build(*args, **kwargs):
        built = real_create(*args, **kwargs)
        builds.append(built)
        return built

    monkeypatch.setattr(approval_execution, "create_agent", build)
    monkeypatch.setattr(ai, "create_agent", build)
    model = ScriptedModel([answer("Recovered with knowledge.")])
    install_model_providers(monkeypatch, {"default": model})
    resumed_counts = []

    def shell_env(_config, _runtime_paths, _agent_name, token):
        # The resumed response issues its grant with the remaining continuation budget.
        lifetime = current_cli_lifetime()
        assert lifetime is not None
        resumed_counts.append(lifetime.continuation_count)
        return AgentCliShellEnv("http://127.0.0.1:8765", token)

    monkeypatch.setattr(response_owner, "agent_cli_shell_env", shell_env)
    store = create_session_storage("helper", runtime.config, runtime.runtime_paths, identity)
    _saved(
        store,
        runtime.session_id,
        RunRecord(
            run_id="saved-run",
            session_id=runtime.session_id,
            agent_id="helper",
            status=RunStatus.cancelled,
            metadata={"agent_mode": "minimal"},
            messages=[Message(role="user", content="Keep this model and answer using knowledge")],
        ),
    )
    switch_arguments = {"model_name": "default", "when": "next-turn"}
    requirement = RunRequirement(
        ToolExecution(
            tool_call_id="switch",
            tool_name="switch_thread_model",
            tool_args=switch_arguments,
            requires_confirmation=True,
        ),
    )
    continuation = approval_continuation(
        correlation_id=runtime.correlation_id,
        run_id="saved-run",
        session_id=runtime.session_id,
        entity_name="helper",
        room_id=runtime.room_id,
        thread_id=runtime.thread_id,
        requester_id=runtime.requester_id,
        sources=ResponseSources((runtime.reply_to_event_id,), (runtime.reply_to_event_id,)),
        state="claimed",
        request_body="Keep this model and answer using knowledge",
        continuation_count=continuation_count,
        calls=(
            ApprovalCall(
                "switch",
                "switch_thread_model",
                "helper",
                100,
                toolkit_name="thread_model",
                arguments_digest=approval_arguments_digest(switch_arguments),
            ),
        ),
        cli_call={
            "kind": "agent_cli",
            "toolkit": "thread_model",
            "function": "switch_thread_model",
            "call_id": "switch",
            "parent_bash_call_id": "interrupted-bash",
            "arguments": switch_arguments,
            "requirements": [requirement.to_dict()],
            "delegation_depth": 2,
        },
    )
    execution = _approval_execution(tmp_path, runtime, monkeypatch, knowledge)
    typing_events = []

    async def room_typing(_room, enabled, _timeout):
        typing_events.append(enabled)

    monkeypatch.setattr(execution.client(), "room_typing", room_typing)
    run_ids = []
    stream_agent_response = approval_execution.stream_agent_response
    monkeypatch.setattr(
        approval_execution,
        "stream_agent_response",
        lambda *args, sinks, **kwargs: stream_agent_response(
            *args,
            sinks=replace(sinks, on_attempt_start=lambda run_id, _model_name: run_ids.append(run_id)),
            **kwargs,
        ),
    )
    with approval_receipt_context(RECEIPT_CALLS, run_id=continuation.run_id):
        result = await execution.continue_run(
            continuation,
            paused_answer=PausedAnswer(),
            tool_dispatch=LiveToolDispatchContext.from_runtime_context(runtime),
            decisions={"switch": True},
            denial_reasons={"switch": None},
            tool_trace_collector=[],
            typing_log_context={},
            progress=None,
        )
    if continuation_count == 4:
        assert "Model switching did not produce a final answer" in result.response_text
        assert model.requests == []
        assert run_ids == []
    else:
        assert "Recovered with knowledge." in result.response_text
        assert len(model.requests) == 1
        assert carries_approval_receipt(model.requests[0])
        assert len(builds) == 2
        # The fresh minimal turn is built with the knowledge the recovery resolved.
        assert [source.description for source in builds[-1].surface.knowledge_sources] == ["Saved notes"]
        assert resumed_counts == [3]
        assert len(run_ids) == 1
        assert run_ids[0] != "saved-run"
    assert all(built.surface.mode == "minimal" for built in builds)
    assert typing_events == [True, False]


def _actions(name: str, entrypoint, **flags) -> AdaptedToolkit:
    function = Function.from_callable(entrypoint, name=name)
    for flag, value in flags.items():
        setattr(function, flag, value)
    return AdaptedToolkit(name="actions", functions=(adapt_function(function),), instructions=None)


# A decision the requester took on a call, as (approved, the reason given, the note the call is denied with).
APPROVED = (True, None, None)
DENIED = (False, "Denied", "Denied")
DENIED_WITHOUT_A_REASON = (False, None, "Not approved by requester")


@pytest.mark.asyncio
@pytest.mark.parametrize(("approved", "reason", "denial"), [APPROVED, DENIED, DENIED_WITHOUT_A_REASON])
@pytest.mark.parametrize("control", [None, "after-toolcall", "next-turn", "invalid"])
async def test_restart_resolves_hidden_call_and_never_replays_parent(
    tmp_path: Path,
    approved: bool,
    reason: str | None,
    denial: str | None,
    control: str | None,
) -> None:
    """Real prepared execution keeps arguments, while normal response gets an honest notice."""
    effects = []
    control_result = json.dumps(
        {
            "tool": "thread_model",
            "action": "switch",
            "status": "error" if control == "invalid" else "ok",
            "model": "new-model",
            "when": control,
        },
    )

    def action(value: str, run_context: AgnoRunContext) -> str:
        assert run_context.metadata == {"source": "original"}
        effects.append(value)
        return control_result if control else "action result"

    name = "switch_thread_model" if control else "action"
    runtime = _helper_runtime(tmp_path, tools=["shell"])
    persist_entity_accounts(runtime.config, runtime.runtime_paths)
    build = _with_toolkit(
        _minimal_build(runtime),
        _actions(name, action, requires_confirmation=True, stop_after_tool_call=bool(control)),
    )
    store = create_state_storage("helper", tmp_path)
    persisted, session = _saved(
        store,
        "session",
        RunRecord(
            run_id="run",
            session_id="session",
            agent_id="helper",
            status=RunStatus.cancelled,
            metadata={"source": "original"},
            messages=[
                Message(
                    role="assistant",
                    tool_calls=[
                        {
                            "id": "bash-parent",
                            "type": "function",
                            "function": {"name": "bash", "arguments": '{"command":"never replay"}'},
                        },
                        {
                            "id": "bash-sibling",
                            "type": "function",
                            "function": {"name": "bash", "arguments": '{"command":"also interrupted"}'},
                        },
                    ],
                    provider_data={"signature": "kept"},
                ),
            ],
        ),
    )
    requirement = RunRequirement(
        ToolExecution(
            tool_call_id="hidden",
            tool_name=name,
            tool_args={"value": "exact\nargument"},
            requires_confirmation=True,
        ),
    )
    continuation = approval_continuation(
        run_id="run",
        session_id="session",
        entity_name="helper",
        room_id=runtime.room_id,
        requester_id=runtime.requester_id,
        response_event_id="$response",
        state="claimed",
        calls=(
            ApprovalCall(
                "hidden",
                name,
                "helper",
                100,
                decision=ApprovalDecision.APPROVED if approved else ApprovalDecision.DENIED,
                toolkit_name="actions",
                arguments_digest=approval_arguments_digest({"value": "exact\nargument"}),
            ),
        ),
        cli_call={
            "kind": "agent_cli",
            "delegation_depth": 0,
            "toolkit": "actions",
            "function": name,
            "arguments": {"value": "exact\nargument"},
            "call_id": "hidden",
            "parent_bash_call_id": "bash-parent",
            "requirements": [requirement.to_dict()],
        },
    )

    presentation = CollectedStreamPresentation(show_tool_calls=True, track_hidden_tools=True)
    presentation.start_tool(
        requirement.tool_execution.call,
        origin=ToolCallOrigin("bash-parent", "actions"),
    )
    published = []

    async def publish(chunk) -> None:
        published.append(chunk)

    async def authorize(binding) -> None:
        assert binding.key.function == name

    with tool_runtime_context(runtime):
        follow_up = await cli_approval_recovery.continue_cli_approval(
            build,
            continuation,
            CliApprovalCall.from_dict(continuation.cli_call),
            persisted,
            RunStorage(store, session, None),
            paused_answer=PausedAnswer(text=presentation.final_text(), tool_trace=tuple(presentation.tool_trace)),
            runtime_context=runtime,
            decisions={"hidden": approved},
            denial_reasons={"hidden": reason},
            tool_trace_collector=[],
            refresh_scheduler=None,
            authorize=authorize,
            progress=publish,
        )
    assert isinstance(follow_up, cli_approval_recovery.CliFollowUpTurn)
    if control and approved:
        assert follow_up.reusable_build is None
        assert follow_up.ctx.active_model_name == ("new-model" if control == "after-toolcall" else None)
        assert "SYSTEM NOTICE" in follow_up.prompt
    else:
        assert follow_up.reusable_build is build
    trace = follow_up.presentation.tool_trace
    assert len(trace) == 1
    assert trace[0].type == "tool_call_completed"
    assert trace[0].tool_call_id == "hidden"
    assert trace[0].parent_bash_call_id == "bash-parent"
    assert trace[0].toolkit_name == "actions"
    # The turn's completed tools keep where the recovered call ran as well.
    [completed] = follow_up.presentation.tool_tracker.completed_tools
    assert (completed.tool_call_id, completed.parent_bash_call_id, completed.toolkit_name) == (
        "hidden",
        "bash-parent",
        "actions",
    )
    assert "⏳" not in follow_up.presentation.final_text()
    if not approved:
        assert trace[0].result_preview == denial
    else:
        # The hidden call's own start and completion reach the continued reply live.
        assert published[-1].tool_trace[0].type == "tool_call_completed"
    assert effects == (["exact\nargument"] if approved else [])
    notice = follow_up.ctx.transient_enrichment_items[0].text
    assert "interrupted" in notice
    assert ((control_result if control else "action result") if approved else denial) in notice
    saved = store.get_run("run")
    assert saved is not None
    assert saved.metadata["mindroom_replay_state"] == "interrupted"
    assert all(not message.tool_calls for message in saved.messages)
    assert "turn stopped before completion" in saved.messages[-1].get_content_string()


@pytest.mark.asyncio
async def test_a_later_save_of_the_recovered_session_keeps_the_state_the_recovered_call_wrote(tmp_path: Path) -> None:
    """The recovered call changes the session's own state, so a later run save of that session stores it too."""

    def action(value: str, run_context: AgnoRunContext) -> str:
        assert run_context.session_state is not None
        run_context.session_state["recovered"] = value
        return "action result"

    runtime = _helper_runtime(tmp_path, tools=["shell"])
    persist_entity_accounts(runtime.config, runtime.runtime_paths)
    build = _with_toolkit(_minimal_build(runtime), _actions("action", action, requires_confirmation=True))
    store = create_state_storage("helper", tmp_path)
    persisted, session = _saved(
        store,
        "session",
        RunRecord(
            run_id="run",
            session_id="session",
            agent_id="helper",
            status=RunStatus.cancelled,
            messages=[
                Message(
                    role="assistant",
                    tool_calls=[
                        {
                            "id": "bash-parent",
                            "type": "function",
                            "function": {"name": "bash", "arguments": '{"command":"never replay"}'},
                        },
                    ],
                ),
            ],
        ),
    )
    requirement = RunRequirement(
        ToolExecution(tool_call_id="hidden", tool_name="action", tool_args={"value": "v"}, requires_confirmation=True),
    )
    continuation = approval_continuation(
        run_id="run",
        session_id="session",
        entity_name="helper",
        room_id=runtime.room_id,
        requester_id=runtime.requester_id,
        response_event_id="$response",
        state="claimed",
        calls=(
            ApprovalCall(
                "hidden",
                "action",
                "helper",
                100,
                decision=ApprovalDecision.APPROVED,
                toolkit_name="actions",
                arguments_digest=approval_arguments_digest({"value": "v"}),
            ),
        ),
        cli_call={
            "kind": "agent_cli",
            "delegation_depth": 0,
            "toolkit": "actions",
            "function": "action",
            "arguments": {"value": "v"},
            "call_id": "hidden",
            "parent_bash_call_id": "bash-parent",
            "requirements": [requirement.to_dict()],
        },
    )

    async def authorize(binding) -> None:
        assert binding.key.function == "action"

    with tool_runtime_context(runtime):
        follow_up = await cli_approval_recovery.continue_cli_approval(
            build,
            continuation,
            CliApprovalCall.from_dict(continuation.cli_call),
            persisted,
            RunStorage(store, session, None),
            paused_answer=PausedAnswer(text="", tool_trace=()),
            runtime_context=runtime,
            decisions={"hidden": True},
            denial_reasons={"hidden": None},
            tool_trace_collector=[],
            refresh_scheduler=None,
            authorize=authorize,
            progress=None,
        )
    assert isinstance(follow_up, cli_approval_recovery.CliFollowUpTurn)

    await persist_to(store, session)(RunRecord(run_id="later", session_id="session", agent_id="helper"))

    assert stored_session_state(store, "session") == {"recovered": "v"}


def _outer_bash_approval(arguments: dict[str, object]) -> RunRequirement:
    """Return the waiting approval of the outer Bash's own shell command."""
    return RunRequirement(
        ToolExecution(
            tool_call_id="bash",
            tool_name="run_shell_command",
            tool_args=arguments,
            requires_confirmation=True,
        ),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(("approved", "reason", "denial"), [APPROVED, DENIED, DENIED_WITHOUT_A_REASON])
async def test_restart_settles_outer_bash_approval_without_running_it(
    tmp_path: Path,
    approved: bool,
    reason: str | None,
    denial: str | None,
) -> None:
    """A shell approval rule on the outer Bash recovers as an honest notice, never a replay."""
    runtime = _helper_runtime(tmp_path, tools=["shell"])
    persist_entity_accounts(runtime.config, runtime.runtime_paths)
    build = _minimal_build(runtime)
    store = create_state_storage("helper", tmp_path)
    marker = tmp_path / "ran"
    arguments = {"command": f"touch {marker}"}
    requirement = _outer_bash_approval(arguments)
    persisted, session = _saved(
        store,
        "session",
        RunRecord(run_id="run", session_id="session", agent_id="helper", status=RunStatus.cancelled),
    )
    presentation = CollectedStreamPresentation(show_tool_calls=True, track_hidden_tools=True)
    presentation.start_tool(requirement.tool_execution.call, origin=ToolCallOrigin("bash", "shell"))
    continuation = approval_continuation(
        run_id="run",
        session_id="session",
        entity_name="helper",
        room_id=runtime.room_id,
        requester_id=runtime.requester_id,
        response_event_id="$response",
        state="claimed",
        calls=(
            ApprovalCall(
                "bash",
                "run_shell_command",
                "helper",
                100,
                decision=ApprovalDecision.APPROVED if approved else ApprovalDecision.DENIED,
                toolkit_name="shell",
                arguments_digest=approval_arguments_digest(arguments),
            ),
        ),
        cli_call=CliApprovalCall(
            toolkit="shell",
            function="run_shell_command",
            arguments=arguments,
            call_id="bash",
            parent_bash_call_id="bash",
            requirements=(requirement,),
            delegation_depth=0,
        ).to_dict(),
    )
    with tool_runtime_context(runtime):
        follow_up = await cli_approval_recovery.continue_cli_approval(
            build,
            continuation,
            CliApprovalCall.from_dict(continuation.cli_call),
            persisted,
            RunStorage(store, session, None),
            paused_answer=PausedAnswer(text=presentation.final_text(), tool_trace=tuple(presentation.tool_trace)),
            runtime_context=runtime,
            decisions={"bash": approved},
            denial_reasons={"bash": reason},
            tool_trace_collector=[],
            refresh_scheduler=None,
            authorize=AsyncMock(),
            progress=None,
        )
    assert isinstance(follow_up, cli_approval_recovery.CliFollowUpTurn)
    trace = follow_up.presentation.tool_trace
    assert [(entry.tool_call_id, entry.type) for entry in trace] == [("bash", "tool_call_completed")]
    assert not marker.exists()
    assert ("saved command was not resumed" if approved else denial) in follow_up.ctx.transient_enrichment_items[0].text


@pytest.mark.asyncio
async def test_a_recovered_approval_saves_no_tool_call_its_fold_already_summarized(tmp_path: Path) -> None:
    """The paused reply's trace still lists the folded shell call and its nested tool; the saved record does not."""
    runtime = _helper_runtime(tmp_path, tools=["shell"])
    persist_entity_accounts(runtime.config, runtime.runtime_paths)
    build = _minimal_build(runtime)
    store = create_state_storage("helper", tmp_path)
    folded = ToolExecution(
        tool_call_id="folded-bash",
        tool_name="run_shell_command",
        tool_args={"command": "echo folded"},
        result="folded shell output",
        is_error=False,
    )
    nested = ToolExecution(
        tool_call_id="nested-call",
        tool_name="read_file",
        tool_args={"path": "notes.md"},
        result="nested tool output",
        is_error=False,
    )
    # A fold took the earlier shell call's result out of the run's replay view, and its summary holds it.
    store_fold(store, "session", HistoryScope(kind="agent", scope_id="helper"), "run", tool_call_ids=["folded-bash"])
    arguments = {"command": "touch never"}
    requirement = _outer_bash_approval(arguments)
    persisted, session = _saved(
        store,
        "session",
        RunRecord(run_id="run", session_id="session", agent_id="helper", status=RunStatus.cancelled, tools=[folded]),
    )
    presentation = CollectedStreamPresentation(show_tool_calls=True, track_hidden_tools=True)
    for execution, parent in ((folded, None), (nested, "folded-bash")):
        origin = ToolCallOrigin(parent, "shell")
        presentation.start_tool(execution.call, origin=origin)
        presentation.complete_tool(execution.call, origin=origin)
    presentation.start_tool(requirement.tool_execution.call, origin=ToolCallOrigin("bash", "shell"))
    continuation = approval_continuation(
        run_id="run",
        session_id="session",
        entity_name="helper",
        room_id=runtime.room_id,
        requester_id=runtime.requester_id,
        response_event_id="$response",
        state="claimed",
        calls=(
            ApprovalCall(
                "bash",
                "run_shell_command",
                "helper",
                100,
                decision=ApprovalDecision.DENIED,
                toolkit_name="shell",
                arguments_digest=approval_arguments_digest(arguments),
            ),
        ),
        cli_call=CliApprovalCall(
            toolkit="shell",
            function="run_shell_command",
            arguments=arguments,
            call_id="bash",
            parent_bash_call_id="bash",
            requirements=(requirement,),
            delegation_depth=0,
        ).to_dict(),
    )

    with tool_runtime_context(runtime):
        await cli_approval_recovery.continue_cli_approval(
            build,
            continuation,
            CliApprovalCall.from_dict(continuation.cli_call),
            persisted,
            RunStorage(store, session, None),
            paused_answer=PausedAnswer(text=presentation.final_text(), tool_trace=tuple(presentation.tool_trace)),
            runtime_context=runtime,
            decisions={"bash": False},
            denial_reasons={"bash": "Denied"},
            tool_trace_collector=[],
            refresh_scheduler=None,
            authorize=AsyncMock(),
            progress=None,
        )

    stored = store.load_session("session", "agent")
    assert stored is not None
    saved = "\n".join(
        message.get_content_string() for run in stored.runs if run.run_id == "run" for message in run.messages
    )
    # The denied call is recorded, and no folded result returns beside the summary that holds it.
    assert "Denied" in saved
    assert "folded shell output" not in saved
    assert "nested tool output" not in saved


async def _checkpointed_batch(
    runtime: ToolRuntimeContext,
    store: ConversationStore,
    sibling_image: Image,
) -> tuple[RunRecord, ConversationSession]:
    """Save the interrupted batch as a live minimal turn did: a completed sibling Bash and the waiting outer one."""
    session = ConversationSession(session_id=runtime.session_id, kind="agent", owner_id="helper")
    record = RunRecord(
        run_id="saved-run",
        session_id=runtime.session_id,
        agent_id="helper",
        status=RunStatus.running,
        metadata={"agent_mode": "minimal"},
    )
    catalog = PreparedAgentToolCatalog(
        run=RunContext(
            run_id="saved-run",
            user_id=runtime.requester_id,
            session=session,
        ),
        runtime_context=runtime,
        store=store,
        middleware=(),
        max_tool_calls_per_turn=None,
    )
    checkpoint = BatchCheckpoint(catalog)
    batch = [("call_completed", "completed sibling"), ("old-bash", "never replay outer")]
    assistant = Message(
        role="assistant",
        tool_calls=[
            {"id": call_id, "type": "function", "function": {"name": "bash", "arguments": json.dumps({"command": c})}}
            for call_id, c in batch
        ],
    )
    [bash] = minimal_bash_toolkit().functions
    planned = [
        PlannedCall(
            function=bash,
            execution=ToolExecution(tool_call_id=call_id, tool_name="bash", tool_args={"command": command}),
        )
        for call_id, command in batch
    ]
    state = LoopState(
        messages=[assistant],
        offered=PreparedTools(functions={}),
        tools=(),
        run=catalog.run,
        record=record,
        request_index=1,
        loop_start_ids=frozenset(),
        request_template=EMPTY_REQUEST,
    )
    await checkpoint.capture(state, planned)
    checkpoint.complete("call_completed", ToolOutcome(content="completed sibling result", images=(sibling_image,)))
    await checkpoint.persist_approval("old-bash")
    persisted = store.get_run("saved-run")
    saved_session = store.load_session(runtime.session_id, "agent")
    assert isinstance(persisted, RunRecord)
    assert saved_session is not None
    return persisted, saved_session


@pytest.mark.asyncio
@pytest.mark.usefixtures("agent_cli_api")
@pytest.mark.parametrize(
    ("shell", "missing_shell_function", "nested", "preset"),
    [(shell, missing, None, False) for shell in (False, True) for missing in (None, "run_shell_command")]
    + [(True, None, "media", False), (True, None, "approval", False), (True, None, "suspend", False)]
    + [(True, None, None, True)],
)
async def test_minimal_recovery_keeps_mode_media_and_uses_fresh_shell_grant(  # noqa: C901 - checkpoint through real resumed provider
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    shell: bool,
    missing_shell_function: str | None,
    nested: str | None,
    preset: bool,
) -> None:
    """An approved saved call keeps its exact owner, and its old Bash stays interrupted.

    The follow-up turn reads the approval receipt the runner binds after its input and keeps it for its later
    requests. A shell that ``openclaw_compat`` brings in is approved under the preset and still runs as the agent's
    shell.
    """
    runtime = _helper_runtime(tmp_path, tools=["openclaw_compat"] if preset else ["shell"])
    registry = TurnToolRegistry()
    runtime = replace(
        runtime,
        storage_path=tmp_path / "attachments",
        orchestrator=SimpleNamespace(agent_cli_registry=registry),
    )
    persist_entity_accounts(runtime.config, runtime.runtime_paths)
    effects = []
    shell_tokens = []

    async def run_shell_command(args: str, timeout: int = 120, tail: int = 100) -> str:  # noqa: ARG001, ASYNC109
        assert args in {"exact approved shell", "later model shell"}
        effects.append(args)
        token = current_agent_cli_shell_env().token
        if token not in shell_tokens:
            shell_tokens.append(token)
        if nested:
            owner = shell_cli_owner(registry)
            queued = await owner.operation(
                window=cli_window(),
                operation=ToolCallOperation(
                    operation="tools.call",
                    call_id=uuid4(),
                    toolkit="media",
                    function="media",
                    arguments={"value": "nested media"},
                ),
            )
            async with asyncio.timeout(5):
                receipt = await settled_receipt(owner, queued["call_id"])
            if nested != "suspend":
                assert receipt["status"] == "completed"
        return "fresh shell result"

    def assert_fresh_grant_revoked() -> None:
        # The recovery's Bash ran with its own grant, which ended with the recovery.
        assert len(shell_tokens) == 1
        with pytest.raises(CliAuthenticationError):
            registry.resolve("Bearer " + shell_tokens[0], now_ns=time.time_ns())

    install_scripted_shell(monkeypatch, run_shell_command)
    sibling_image = Image(url="https://example.test/completed-sibling.png")
    image = Image(content=b"saved-image", mime_type="image/png")
    audio = Audio(content=b"saved-audio", mime_type="audio/wav")

    def media(value: str) -> ToolResult:
        from agno.media import Audio as AgnoAudio  # noqa: PLC0415 - the tool authors Agno media
        from agno.media import Image as AgnoImage  # noqa: PLC0415

        effects.append(value)
        return ToolResult(
            content="media result",
            images=[AgnoImage(content=image.content, mime_type=image.mime_type)],
            audios=[AgnoAudio(content=audio.content, mime_type=audio.mime_type)],
        )

    toolkit = Toolkit(name="media", tools=[media])
    toolkit.get_async_functions()["media"].requires_confirmation = nested in {"approval", "suspend"}
    media_toolkit = adapt_toolkit(toolkit)
    name = "run_shell_command" if shell else "media"
    namespace = "shell" if shell else "media"
    approval_toolkit = "openclaw_compat" if preset else namespace
    arguments = {"args": "exact approved shell"} if shell else {"value": "exact approved media"}
    tool = ToolExecution(tool_call_id="hidden", tool_name=name, tool_args=arguments, requires_confirmation=True)
    requirement = RunRequirement(tool)
    presentation = CollectedStreamPresentation(show_tool_calls=True, track_hidden_tools=True)
    presentation.start_tool(tool.call, origin=ToolCallOrigin("old-bash", namespace))
    store = create_session_storage(
        "helper",
        runtime.config,
        runtime.runtime_paths,
        build_execution_identity_from_runtime_context(runtime),
    )
    persisted, session = await _checkpointed_batch(runtime, store, sibling_image)
    continuation = approval_continuation(
        approval_id="approved",
        run_id="saved-run",
        session_id=runtime.session_id,
        entity_name="helper",
        room_id=runtime.room_id,
        thread_id=runtime.resolved_thread_id,
        requester_id=runtime.requester_id,
        response_event_id="$response",
        correlation_id=runtime.correlation_id,
        sources=ResponseSources((runtime.reply_to_event_id,), (runtime.reply_to_event_id,)),
        state="claimed",
        show_tool_calls=True,
        request_body="recover actual request",
        runtime_model_name="default",
        calls=(
            ApprovalCall(
                "hidden",
                name,
                "helper",
                100,
                decision=ApprovalDecision.APPROVED,
                toolkit_name=approval_toolkit,
                arguments_digest=approval_arguments_digest(arguments),
            ),
        ),
        cli_call={
            "kind": "agent_cli",
            "toolkit": approval_toolkit,
            "function": name,
            "arguments": arguments,
            "call_id": "hidden",
            "parent_bash_call_id": "old-bash",
            "requirements": [requirement.to_dict()],
            "delegation_depth": 2,
        },
    )
    approvals = []
    pauses = []
    responses = []

    async def approve(paused):
        # The batch of the Bash call waiting for approval is saved before the wait.
        stored = store.load_session(runtime.session_id, "agent")
        assert stored is not None
        assert any(
            item.get("id") == paused.cli_call["parent_bash_call_id"]
            for message in stored.runs[-1].messages
            for item in message.tool_calls or ()
        )
        approvals.append(paused.cli_call["call_id"])
        if nested == "suspend":
            pauses.append(paused)
            error = ResponsePausedForApproval(paused)
            emit_cli_suspension(error)
            raise error
        return tuple(
            apply_exact_approval_decisions(
                list(paused.requirements),
                decisions={paused.cli_call["call_id"]: True},
                denial_reasons={paused.cli_call["call_id"]: None},
            ),
        )

    runtime = replace(runtime, cli_approval_handler=approve)
    if missing_shell_function:
        runtime.config.agents["helper"].tools = [{"shell": {"exclude_tools": [missing_shell_function]}}]
    provider = ScriptedProvider(
        steps=[[("bash", {"command": "later model shell"})], "finished"] if nested == "approval" else ["finished"],
    )
    provider.install(monkeypatch)
    build = _with_toolkit(_minimal_build(runtime, history_storage=store), media_toolkit)

    def check_follow_up(follow_up: cli_approval_recovery.CliFollowUpTurn) -> None:
        assert follow_up.ctx.agent_mode == "minimal"
        assert follow_up.delegation_depth == 2
        trace = follow_up.presentation.tool_trace
        assert len(trace) == (2 if nested else 1)
        assert trace[0].type == "tool_call_completed"
        assert trace[0].parent_bash_call_id == "old-bash"
        if nested:
            assert trace[1].tool_name == "media"
            assert trace[1].type == "tool_call_completed"
            assert trace[1].toolkit_name == "media"
            assert trace[1].parent_bash_call_id == "old-bash"
        if shell:
            assert_fresh_grant_revoked()
        if not shell or nested:
            assert follow_up.media.images[-1].content == image.content
            assert follow_up.media.audio[0].content == b"saved-audio"
            assert runtime.runtime_attachment_ids

    async def authorize(binding, **_kwargs):
        assert binding.catalog.runtime_context.requester_id == runtime.requester_id

    if nested:
        monkeypatch.setattr(response_owner, "authorize_prepared_tool_call", authorize)
    published = []

    async def publish(chunk) -> None:
        published.append(chunk)

    async def recover():
        recovered = await cli_approval_recovery.continue_cli_approval(
            build,
            continuation,
            CliApprovalCall.from_dict(continuation.cli_call),
            persisted,
            RunStorage(store, session, None),
            paused_answer=PausedAnswer(text=presentation.response_text, tool_trace=tuple(presentation.tool_trace)),
            runtime_context=runtime,
            decisions={"hidden": True},
            denial_reasons={"hidden": None},
            tool_trace_collector=[],
            refresh_scheduler=None,
            authorize=authorize,
            progress=publish,
        )
        if not isinstance(recovered, cli_approval_recovery.CliFollowUpTurn):
            return recovered
        responses.append(recovered.ctx)
        check_follow_up(recovered)
        return await approval_execution._stream_continuation_turn(
            recovered.ctx,
            recovered.presentation,
            prompt=recovered.prompt,
            reusable_build=recovered.reusable_build,
            resume=TurnResume(continuation_count=recovered.initial_continuation_count),
            delegation_depth=recovered.delegation_depth,
            media=MediaInputs(images=recovered.media.images),
            config=runtime.config,
            runtime_paths=runtime.runtime_paths,
            knowledge=None,
            refresh_scheduler=None,
            tool_trace_collector=[],
            tool_dispatch=LiveToolDispatchContext.from_runtime_context(runtime),
            progress=publish,
        )

    def stored_runs() -> list[dict]:
        stored = store.load_session(runtime.session_id, "agent")
        assert stored is not None
        return [run.to_dict() for run in stored.runs]

    with (
        tool_runtime_context(runtime),
        approval_receipt_context(RECEIPT_CALLS, run_id=continuation.run_id),
    ):
        if missing_shell_function:
            saved_before = stored_runs()
            # A saved shell call loses its own toolkit first; any other call loses minimal mode's shell.
            with pytest.raises(RuntimeError, match=r"no longer has its original toolkit|`run_shell_command` shell"):
                await recover()
            assert shell_tokens == []
            assert provider.requests == []
            assert effects == []
            assert stored_runs() == saved_before
            assert continuation.state == "claimed"
            return
        result = await recover()
    if nested == "suspend":
        assert len(pauses) == 1
        assert result == pauses[0]
        # The recovered run keeps the model its approval was saved with, which a second approval saves again.
        assert result.runtime_model_name == "default"
        assert result.cli_call["parent_bash_call_id"] == "old-bash"
        assert result.cli_call["toolkit"] == "media"
        assert result.cli_call["arguments"] == {"value": "nested media"}
        assert result.cli_call["call_id"] == approvals[0]
        assert responses == []
        assert effects == ["exact approved shell"]
        assert_fresh_grant_revoked()
        saved = store.get_run("saved-run")
        assert saved is not None
        assert any(item.get("id") == "old-bash" for message in saved.messages for item in message.tool_calls or ())
        return
    assert "finished" in result.response_text
    # Like a standard recovery, the hidden call and the fresh model text stream into the continued reply.
    assert published[0].tool_trace
    assert any("finished" in chunk.content for chunk in published)
    assert len(provider.requests) == (2 if nested == "approval" else 1)
    # The follow-up run reads the receipt after its input and keeps it, unchanged, in its later request.
    first = provider.requests[0]
    assert (first.messages[-1].role, first.messages[-1].content) == ("user", RECEIPT_TEXT)
    assert all(request.messages[: len(first.messages)] == first.messages for request in provider.requests[1:])
    assert [len(receipts(request)) for request in provider.requests] == [1] * len(provider.requests)
    assert offered_tools(first) == ["bash"]
    assert sibling_image.url in str(first.messages)
    assert "completed sibling result" in str(first.messages)
    assert "interrupted" in str(first.messages)
    assert effects == (
        ["exact approved shell", "nested media", "later model shell", "nested media"]
        if nested == "approval"
        else ["exact approved shell", "nested media"]
        if nested
        else ["exact approved shell" if shell else "exact approved media"]
    )
    assert len(approvals) == (2 if nested == "approval" else 0)
    saved = store.get_run("saved-run")
    assert saved is not None
    assert saved.metadata["mindroom_replay_state"] == "interrupted"
    assert all(not message.tool_calls for message in saved.messages)
    assert "turn stopped before completion" in saved.messages[-1].get_content_string()


@pytest.mark.asyncio
@pytest.mark.parametrize("available", [True, False])
@pytest.mark.parametrize("permitted", [True, False])
async def test_generated_cli_approval_rebuilds_and_authorizes_exact_function(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    available: bool,
    permitted: bool,
) -> None:
    """Generated approvals bypass toolkit restoration only, never rebuilt binding authorization."""
    runtime = _helper_runtime(tmp_path, tools=["shell"])
    runtime = replace(
        runtime,
        tool_function_filter=lambda function: permitted or function.name != "search_knowledge_base",
    )
    persist_entity_accounts(runtime.config, runtime.runtime_paths)
    identity = build_execution_identity_from_runtime_context(runtime)
    searches: list[str] = []
    store = create_session_storage("helper", runtime.config, runtime.runtime_paths, identity)
    _saved(
        store,
        runtime.session_id,
        RunRecord(
            run_id="generated-run",
            session_id=runtime.session_id,
            agent_id="helper",
            status=RunStatus.cancelled,
            metadata={"agent_mode": "minimal"},
            messages=[Message(role="user", content="Search my notes")],
        ),
    )
    arguments = {"query": "exact saved note"}
    requirement = RunRequirement(
        ToolExecution(
            tool_call_id="search-id",
            tool_name="search_knowledge_base",
            tool_args=arguments,
            requires_confirmation=True,
        ),
    )
    continuation = approval_continuation(
        approval_id="generated-approval",
        run_id="generated-run",
        session_id=runtime.session_id,
        entity_name="helper",
        room_id=runtime.room_id,
        thread_id=runtime.thread_id,
        requester_id=runtime.requester_id,
        state="claimed",
        calls=(ApprovalCall("search-id", "search_knowledge_base", "helper", 100, toolkit_name="agent"),),
        cli_call={
            "kind": "agent_cli",
            "delegation_depth": 0,
            "toolkit": "agent",
            "function": "search_knowledge_base",
            "call_id": "search-id",
            "parent_bash_call_id": "interrupted-bash",
            "arguments": arguments,
            "requirements": [requirement.to_dict()],
        },
    )
    execution = _approval_execution(tmp_path, runtime, monkeypatch, _knowledge(searches) if available else None)
    response = AsyncMock(
        return_value=CompletedApprovalRun(response_text="Recovered generated call", metadata_content={}),
    )
    monkeypatch.setattr(approval_execution, "_stream_continuation_turn", response)

    async def recover():
        return await execution.continue_run(
            continuation,
            paused_answer=PausedAnswer(),
            tool_dispatch=LiveToolDispatchContext.from_runtime_context(runtime),
            decisions={"search-id": True},
            denial_reasons={"search-id": None},
            tool_trace_collector=[],
            typing_log_context={},
            progress=None,
        )

    if available and permitted:
        result = await recover()
        assert result.response_text == "Recovered generated call"
        assert searches == ["exact saved note"]
        response.assert_awaited_once()
    else:
        with pytest.raises((ValueError, PermissionError)):
            await recover()
        assert searches == []
        response.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.usefixtures("agent_cli_api")
@pytest.mark.parametrize(
    ("deferred", "saved_trace"),
    [(True, True), (False, True), (False, False)],
    ids=["deferred", "eager", "eager-without-saved-trace"],
)
async def test_an_approved_call_of_an_implied_toolkit_runs_after_its_bash_stopped(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    deferred: bool,
    saved_trace: bool,
) -> None:
    """A toolkit an authored tool brings in is called under its own name and approved under that tool.

    The model calls ``attachments``, which ``matrix_message`` brings in, and the approval arrives after its Bash
    stopped waiting, so the call runs in the rebuilt agent, which attaches ``matrix_message`` and offers the call as
    ``attachments`` again. The saved approval names ``matrix_message`` as approvals saved before toolkits answered
    to their own tool name did, so those resume the same way. The resumed call keeps that name in the reply's tool
    trace, also when the reply's records lost the paused trace.
    """
    runtime = _helper_runtime(tmp_path, tools=["shell", {"matrix_message": {"defer": deferred}}])
    runtime.config.tool_approval = ToolApprovalConfig(
        rules=[ApprovalRuleConfig(match="list_attachments", action="require_approval")],
    )
    registry = TurnToolRegistry()
    runtime = replace(runtime, orchestrator=SimpleNamespace(agent_cli_registry=registry))
    persist_entity_accounts(runtime.config, runtime.runtime_paths)
    identity = build_execution_identity_from_runtime_context(runtime)

    async def run_shell_command(args: str, timeout: int = 120, tail: int = 100) -> str:  # noqa: ARG001, ASYNC109
        owner = shell_cli_owner(registry)
        queued = await owner.operation(
            window=cli_window(),
            operation=ToolCallOperation(
                operation="tools.call",
                call_id=uuid4(),
                toolkit="attachments",
                function="list_attachments",
                arguments={},
            ),
        )
        await settled_receipt(owner, queued["call_id"])
        return "Bash stopped waiting"

    install_scripted_shell(monkeypatch, run_shell_command)

    async def stop_waiting(paused):
        error = ResponsePausedForApproval(paused)
        emit_cli_suspension(error)
        raise error

    runtime = replace(runtime, cli_approval_handler=stop_waiting)
    ScriptedProvider(steps=[[("bash", {"command": "list my attachments"})]]).install(monkeypatch)
    with tool_runtime_context(runtime), pytest.raises(ResponsePausedForApproval) as raised:
        await ai.ai_response(
            replace(
                _turn_context(),
                agent_mode="minimal",
                run_id=None,
                prompt="list my attachments",
                execution_identity=identity,
                supports_native_tool_approval=True,
            ),
            runtime.runtime_paths,
            runtime.config,
        )
    paused = raised.value.paused
    assert paused.cli_call is not None
    call_id = paused.cli_call["call_id"]
    # The call is shown under the toolkit the model called; its approval names the tool that brings it in.
    assert [entry.toolkit_name for entry in paused.tool_trace] == ["attachments"]
    assert paused.toolkit_owners == {("helper", "list_attachments"): "matrix_message"}
    assert (paused.cli_call["toolkit"], paused.cli_call["function"]) == ("matrix_message", "list_attachments")
    continuation = approval_continuation(
        approval_id="implied-approval",
        run_id=paused.run_id,
        session_id=runtime.session_id,
        entity_name="helper",
        room_id=runtime.room_id,
        thread_id=runtime.resolved_thread_id,
        requester_id=runtime.requester_id,
        state="claimed",
        calls=tuple(
            replace(call, decision=ApprovalDecision.APPROVED) for call in approval_calls_for_cli_pause(paused, "helper")
        ),
        cli_call=paused.cli_call,
    )
    execution = _approval_execution(tmp_path, runtime, monkeypatch, None)
    response = AsyncMock(return_value=CompletedApprovalRun(response_text="Listed", metadata_content={}))
    monkeypatch.setattr(approval_execution, "_stream_continuation_turn", response)

    with tool_runtime_context(runtime):
        result = await execution.continue_run(
            continuation,
            paused_answer=PausedAnswer(text=paused.response_text, tool_trace=paused.tool_trace if saved_trace else ()),
            tool_dispatch=LiveToolDispatchContext.from_runtime_context(runtime),
            decisions={call_id: True},
            denial_reasons={call_id: None},
            tool_trace_collector=[],
            typing_log_context={},
            progress=None,
        )

    assert result.response_text == "Listed"
    [follow_up] = response.await_args.args[0].transient_enrichment_items
    assert "Exact saved tool attachments.list_attachments decision/result:" in follow_up.text
    assert [entry.toolkit_name for entry in response.await_args.args[1].tool_trace] == ["attachments"]
    assert '"status": "ok"' in follow_up.text

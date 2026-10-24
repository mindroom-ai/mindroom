"""CLI approval recovery uses the existing claim and normal response driver."""

# ruff: noqa: ANN001, ANN002, ANN003, ANN202, ARG002, PLR0915

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import replace
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, patch
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
from mindroom.agent_cli.cli_approval_waits import CliApprovalWaits
from mindroom.agent_cli.events import emit_cli_suspension, project_cli_execution
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
from mindroom.event_journal import ApprovalCall, ApprovalDecision, approval_arguments_digest
from mindroom.history.types import HistoryScope
from mindroom.knowledge.read_protocol import SearchHit
from mindroom.knowledge.search import AgentKnowledge, KnowledgeSource, KnowledgeSourceDescription
from mindroom.llm.media import Audio, Image
from mindroom.llm.messages import Message
from mindroom.llm.runs import RunRecord, RunRequirement, RunStatus, ToolExecution
from mindroom.media_inputs import MediaInputs
from mindroom.response_sources import ResponseSources
from mindroom.response_turn import (
    CompletedApprovalRun,
    PausedAnswer,
    ResponsePausedForApproval,
    apply_exact_approval_decisions,
)
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
from mindroom.tool_system.tool_function import AdaptedToolkit, ToolValue
from tests.approval_continuation_helpers import approval_continuation, carries_approval_receipt
from tests.conftest import unwrap_extracted_collaborator
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
from tests.test_agent_cli_authority import _runtime_context

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
        **options,
    )


def _with_toolkit(build: AgentBuild, toolkit: AdaptedToolkit) -> AgentBuild:
    """Return ``build`` also offering ``toolkit``, each of its functions authored under its name."""
    return replace(
        build,
        spec=replace(build.spec, toolkits=(*build.spec.toolkits, toolkit)),
        tool_owners={**build.tool_owners, **{function.name: toolkit.name for function in toolkit.functions}},
    )


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
    execution = replace(runner._approval_execution, config=lambda: runtime.config, runtime_paths=runtime.runtime_paths)
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
    monkeypatch.setattr(agents, "_build_agent_model_provider", lambda *_args: model)
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
    with approval_receipt_context("Trusted approval receipt.", run_id=continuation.run_id):
        result = await execution.continue_run(
            continuation,
            paused_answer=PausedAnswer(),
            execution_identity=identity,
            tool_dispatch=LiveToolDispatchContext.from_runtime_context(runtime),
            decisions={"switch": True},
            denial_reasons={"switch": None},
            tool_trace_collector=[],
            typing_log_context={},
            progress=None,
            run_id_callback=run_ids.append,
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
        project_cli_execution(requirement.tool_execution, parent="bash-parent", toolkit_name="actions"),
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
            session,
            store,
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
            session,
            store,
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

    fields = store.read_session_fields("session")
    assert fields is not None
    assert fields.session_state == {"recovered": "v"}


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
    presentation.start_tool(project_cli_execution(requirement.tool_execution, parent="bash", toolkit_name="shell"))
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
            session,
            store,
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
        tool_call_error=False,
    )
    nested = ToolExecution(
        tool_call_id="nested-call",
        tool_name="read_file",
        tool_args={"path": "notes.md"},
        result="nested tool output",
        tool_call_error=False,
    )
    # The fold took the earlier shell call out of the run's record and stored it on its summary row.
    store_fold(store, "session", HistoryScope(kind="agent", scope_id="helper"), "run", tools=[folded.to_dict()])
    arguments = {"command": "touch never"}
    requirement = _outer_bash_approval(arguments)
    persisted, session = _saved(
        store,
        "session",
        RunRecord(run_id="run", session_id="session", agent_id="helper", status=RunStatus.cancelled, tools=[folded]),
    )
    presentation = CollectedStreamPresentation(show_tool_calls=True, track_hidden_tools=True)
    for execution, parent in ((folded, None), (nested, "folded-bash")):
        projected = project_cli_execution(execution, parent=parent, toolkit_name="shell")
        presentation.start_tool(projected)
        presentation.complete_tool(projected)
    presentation.start_tool(project_cli_execution(requirement.tool_execution, parent="bash", toolkit_name="shell"))
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
            session,
            store,
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
        agent_name="helper",
        run=RunContext(
            run_id="saved-run",
            session_id=runtime.session_id,
            user_id=runtime.requester_id,
            session=session,
        ),
        runtime_context=runtime,
        store=store,
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
            call_id=call_id,
            function=bash,
            arguments={"command": command},
            execution=ToolExecution(tool_call_id=call_id, tool_name="bash", tool_args={"command": command}),
        )
        for call_id, command in batch
    ]
    state = LoopState(
        messages=[assistant],
        offered=PreparedTools(functions={}, instructions=()),
        tools=(),
        run=catalog.run,
        record=record,
        request_index=1,
        loop_start_ids=frozenset(),
        request_template=EMPTY_REQUEST,
    )
    await checkpoint.capture(state, planned)
    checkpoint.complete("call_completed", ToolValue(content="completed sibling result", images=(sibling_image,)))
    await checkpoint.persist_approval("old-bash")
    persisted = store.get_run("saved-run")
    saved_session = store.load_session(runtime.session_id, "agent")
    assert isinstance(persisted, RunRecord)
    assert saved_session is not None
    return persisted, saved_session


@pytest.mark.asyncio
@pytest.mark.usefixtures("agent_cli_api")
@pytest.mark.parametrize(
    ("shell", "missing_shell_function", "nested"),
    [(shell, missing, None) for shell in (False, True) for missing in (None, "run_shell_command")]
    + [(True, None, "media"), (True, None, "approval"), (True, None, "suspend")],
)
async def test_minimal_recovery_keeps_mode_media_and_uses_fresh_shell_grant(  # noqa: C901 - checkpoint through real resumed provider
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    shell: bool,
    missing_shell_function: str | None,
    nested: str | None,
) -> None:
    """An approved saved call keeps its exact owner, and its old Bash stays interrupted.

    The follow-up turn sends the approval receipt the runner binds with its first request only.
    """
    runtime = _helper_runtime(tmp_path, tools=["shell"])
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
    arguments = {"args": "exact approved shell"} if shell else {"value": "exact approved media"}
    tool = ToolExecution(tool_call_id="hidden", tool_name=name, tool_args=arguments, requires_confirmation=True)
    requirement = RunRequirement(tool)
    presentation = CollectedStreamPresentation(show_tool_calls=True, track_hidden_tools=True)
    presentation.start_tool(project_cli_execution(tool, parent="old-bash", toolkit_name=namespace))
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
        calls=(
            ApprovalCall(
                "hidden",
                name,
                "helper",
                100,
                decision=ApprovalDecision.APPROVED,
                toolkit_name=namespace,
                arguments_digest=approval_arguments_digest(arguments),
            ),
        ),
        cli_call={
            "kind": "agent_cli",
            "toolkit": namespace,
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
            session,
            store,
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
            show_tool_calls=continuation.show_tool_calls,
            reusable_build=recovered.reusable_build,
            initial_continuation_count=recovered.initial_continuation_count,
            delegation_depth=recovered.delegation_depth,
            media=MediaInputs(images=recovered.media.images),
            config=runtime.config,
            runtime_paths=runtime.runtime_paths,
            execution_identity=build_execution_identity_from_runtime_context(runtime),
            knowledge=None,
            refresh_scheduler=None,
            tool_trace_collector=[],
            run_id_callback=None,
            tool_dispatch=LiveToolDispatchContext.from_runtime_context(runtime),
            progress=publish,
        )

    def stored_runs() -> list[dict]:
        stored = store.load_session(runtime.session_id, "agent")
        assert stored is not None
        return [run.to_dict() for run in stored.runs]

    with (
        tool_runtime_context(runtime),
        approval_receipt_context("Trusted approval receipt.", run_id=continuation.run_id),
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
    assert [carries_approval_receipt(request) for request in provider.requests] == (
        [True, False] if nested == "approval" else [True]
    )
    first = provider.requests[0]
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
            execution_identity=identity,
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


@pytest.mark.parametrize(
    ("answered", "shutting_down"),
    [(False, False), (False, True), (True, False)],
    ids=["fenced", "fenced-during-shutdown", "answered-after-fence"],
)
@pytest.mark.asyncio
async def test_a_cli_wait_a_stop_fenced_settles_through_failure_so_its_cards_expire(
    *,
    answered: bool,
    shutting_down: bool,
) -> None:
    """The cancelled note reached the room, but only failure settlement expires the approval's pending card.

    A run that answered after its fence finishes as an answered one does, so its after-response work runs once.
    """
    continuation = SimpleNamespace(
        approval_id="approval-1",
        state="failing",
        failure_reason="cancelled_by_user",
        runtime_generation=None,
        cli_call={"kind": "agent_cli"},
        room_id="!room",
        source_event_ids=("$source",),
    )
    final = SimpleNamespace(permanently_failed=False)
    responses = SimpleNamespace(
        final_delivery=AsyncMock(return_value=final),
        successful_final_delivery=AsyncMock(return_value=final if answered else None),
        finish_approval=AsyncMock(return_value=True),
        request_failure=AsyncMock(return_value=None),
        settle_failure=AsyncMock(return_value=True),
    )
    waits = CliApprovalWaits(
        store=SimpleNamespace(approval_continuation_for_source=AsyncMock(return_value=continuation)),  # type: ignore[arg-type]
        responses=responses,  # type: ignore[arg-type]
        runtime_generation="runtime-a",
        retry_sources=lambda _room_id, _sources: None,
        claim=AsyncMock(),
        advance=AsyncMock(),
    )
    progress = SimpleNamespace(failure_reason=None, delivery_outcome=None)

    with patch("mindroom.agent_cli.cli_approval_waits.current_task_is_process_shutdown", return_value=shutting_down):
        await waits._settle("$source", suspended=False, progress=progress, settle_terminal=True)  # type: ignore[arg-type]

    responses.request_failure.assert_not_awaited()
    if answered:
        responses.finish_approval.assert_awaited_once_with("approval-1")
        responses.settle_failure.assert_not_awaited()
        return
    responses.finish_approval.assert_not_awaited()
    if shutting_down:
        # Approval recovery settles it at the next start.
        responses.settle_failure.assert_not_awaited()
    else:
        responses.settle_failure.assert_awaited_once_with(continuation, "cancelled_by_user")


@pytest.mark.parametrize("permanently_failed", [False, True])
@pytest.mark.asyncio
async def test_a_cli_wait_fails_an_approval_whose_final_matrix_refused(*, permanently_failed: bool) -> None:
    """A FINAL Matrix refused for good is no answer: the wait fails the approval instead of finishing it."""
    continuation = SimpleNamespace(
        approval_id="approval-1",
        state="claimed",
        runtime_generation="runtime-a",
        cli_call={"kind": "agent_cli"},
        room_id="!room",
        source_event_ids=("$source",),
    )
    responses = SimpleNamespace(
        final_delivery=AsyncMock(return_value=SimpleNamespace(permanently_failed=permanently_failed)),
        finish_approval=AsyncMock(return_value=True),
        request_failure=AsyncMock(return_value=None),
    )
    waits = CliApprovalWaits(
        store=SimpleNamespace(approval_continuation_for_source=AsyncMock(return_value=continuation)),  # type: ignore[arg-type]
        responses=responses,  # type: ignore[arg-type]
        runtime_generation="runtime-a",
        retry_sources=lambda _room_id, _sources: None,
        claim=AsyncMock(),
        advance=AsyncMock(),
    )
    progress = SimpleNamespace(failure_reason=None, delivery_outcome=None)

    await waits._settle("$source", suspended=False, progress=progress, settle_terminal=True)  # type: ignore[arg-type]

    if permanently_failed:
        responses.finish_approval.assert_not_awaited()
        responses.request_failure.assert_awaited_once()
    else:
        responses.finish_approval.assert_awaited_once_with("approval-1")
        responses.request_failure.assert_not_awaited()

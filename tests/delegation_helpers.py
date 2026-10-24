"""Shared scripted models, requester contexts, and approval drivers for delegation integration tests.

Tests script one ``ScriptedModel`` per agent through ``ScriptedAgents``, run agent replies through ``respond`` and
team replies through ``respond_team``, and continue their pauses through ``resume`` and ``resume_team``.
``pause_parent`` stops a parent at its stored pause, for tests that drive the delegation driver themselves.
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from types import SimpleNamespace
from typing import TYPE_CHECKING, ClassVar
from unittest.mock import AsyncMock, MagicMock

from agno.tools import Toolkit

from mindroom import agents
from mindroom.agent_loop.build import prompt_for_turn
from mindroom.agent_loop.run import run_agent
from mindroom.agent_loop.spec import RunContext
from mindroom.ai import ai_response
from mindroom.approval.execution import AgentApprovalExecution
from mindroom.approval.resume import ResumeRunner
from mindroom.constants import resolve_runtime_paths
from mindroom.event_journal import ApprovalCall, approval_arguments_digest
from mindroom.history.session_context import create_scope_session_storage
from mindroom.history.storage import new_scope_session
from mindroom.history.turn_recorder import TurnRecorder
from mindroom.history.types import HistoryScope
from mindroom.knowledge.utils import KnowledgeAccessSupport
from mindroom.llm.events import RunFinished
from mindroom.llm.messages import Message as OwnedMessage
from mindroom.llm.runs import RunRecord, RunStatus
from mindroom.message_target import MessageTarget
from mindroom.providers import registry as provider_registry
from mindroom.response_turn import PausedAnswer, ResponsePausedForApproval
from mindroom.session_storage.agent_storage import create_session_storage
from mindroom.session_storage.store import persist_to
from mindroom.teams.envelope import team_response
from mindroom.teams.resume import continue_paused_team_run
from mindroom.teams.spec import TeamMode
from mindroom.tool_system.runtime_context import LiveToolDispatchContext, tool_runtime_context
from tests.ai_user_id_helpers import team_orchestrator
from tests.approval_continuation_helpers import approval_continuation
from tests.authorization_helpers import make_test_tool_runtime_context
from tests.conftest import make_conversation_reader_mock, make_relation_lookup, make_turn_context
from tests.scripted_agent_helpers import registered_toolkit

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Iterator, Mapping, Sequence
    from pathlib import Path

    import pytest

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.delegation.state import DelegationState
    from mindroom.hooks import HookRegistry
    from mindroom.llm.provider import ModelProvider
    from mindroom.llm.runs import AnyRunRecord
    from mindroom.response_turn import CompletedApprovalRun, PausedAttempt
    from mindroom.session_storage.store import ConversationStore
    from mindroom.session_storage.types import ConversationSession
    from mindroom.tool_system.runtime_context import ToolRuntimeContext
    from mindroom.tool_system.worker_routing import ToolExecutionIdentity
    from tests.fakes.scripted_model import ScriptedModel


def _runtime_paths(storage_path: Path) -> RuntimePaths:
    """Create explicit runtime paths for delegate-tool agent creation tests."""
    return resolve_runtime_paths(
        config_path=storage_path / "config.yaml",
        storage_path=storage_path,
        process_env={},
    )


def _delegate_runtime_context(
    config: Config,
    runtime_paths: RuntimePaths,
    *,
    execution_identity: ToolExecutionIdentity | None = None,
    agent_name: str = "leader",
    hook_registry: HookRegistry | None = None,
    correlation_id: str | None = None,
) -> ToolRuntimeContext:
    """Build the requester context every successful delegation requires; ``hook_registry`` holds its plugin hooks."""
    room_id = execution_identity.room_id if execution_identity is not None else "!room:example.org"
    source_thread_id = execution_identity.thread_id if execution_identity is not None else None
    resolved_thread_id = execution_identity.resolved_thread_id if execution_identity is not None else None
    session_id = execution_identity.session_id if execution_identity is not None else room_id
    requester_id = execution_identity.requester_id if execution_identity is not None else "@alice:example.org"
    context = make_test_tool_runtime_context(
        agent_name=agent_name,
        target=MessageTarget(
            room_id=room_id,
            source_thread_id=source_thread_id,
            resolved_thread_id=resolved_thread_id,
            reply_to_event_id=None,
            session_id=session_id,
        ),
        requester_id=requester_id,
        client=MagicMock(),
        config=config,
        runtime_paths=runtime_paths,
        relations=make_relation_lookup(),
        conversation_reader=make_conversation_reader_mock(),
        correlation_id=correlation_id,
    )
    return context if hook_registry is None else replace(context, hook_registry=hook_registry)


@dataclass
class ScriptedAgents:
    """One scripted model per agent, handed to ``create_agent`` for the agent its execution identity names.

    ``by_session`` serves a model by the identity's session first, for an agent that delegates to itself.
    ``team`` leads every configured team's reply; ``built_models`` names the model each agent build asked for.
    """

    models: Mapping[str, ModelProvider]
    by_session: Mapping[str, ModelProvider] = field(default_factory=dict)
    team: ModelProvider | None = None
    built_models: list[str] = field(default_factory=list)

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Serve the scripted models from the seam every configured model builds at."""
        monkeypatch.setattr(provider_registry, "build_model_provider", self._build)

    def _build(
        self,
        config: Config,
        _runtime_paths: RuntimePaths,
        model_name: str = "default",
        *,
        execution_identity: ToolExecutionIdentity | None = None,
    ) -> ModelProvider:
        # A team leader builds with the team's identity; each agent builds with its own.
        if self.team is not None and execution_identity is not None and execution_identity.agent_name in config.teams:
            return self.team
        self.built_models.append(model_name)
        if execution_identity is None:
            return self.models[model_name]
        if (session := execution_identity.session_id) is not None and session in self.by_session:
            return self.by_session[session]
        return self.models[execution_identity.agent_name]


def system_prompts(model: ScriptedModel) -> list[str]:
    """Return each request's system message text, in request order."""
    return [
        "\n".join(str(message.content) for message in request.messages if message.role == "system")
        for request in model.requests
    ]


def authored_prompts(model: ScriptedModel) -> list[str]:
    """Return each request's system message before the runtime sections that follow an authored subagent's prompt."""
    return [prompt.split("\n\n<your_role>", 1)[0] for prompt in system_prompts(model)]


def offered_tools(model: ScriptedModel) -> list[list[str]]:
    """Return the sorted names of the tools each request offered."""
    return [sorted(tool.name for tool in request.tools) for request in model.requests]


def _call(name: str, call_id: str, **arguments: object) -> dict[str, object]:
    return {"id": call_id, "type": "function", "function": {"name": name, "arguments": json.dumps(arguments)}}


def _deferred_call(call_id: str, function_name: str, **arguments: object) -> dict[str, object]:
    """Call a deferred function through the call_tool wrapper a model without native tool search sees."""
    wrapped = json.dumps({"name": function_name, "arguments": arguments})
    return {"id": call_id, "type": "function", "function": {"name": "call_tool", "arguments": wrapped}}


def _saved_approval_calls(state: DelegationState) -> tuple[ApprovalCall, ...]:
    """Supply the saved journal calls when a test drives delegation without Matrix publication."""
    calls = []
    for tool in state.pending_tools:
        call_id = str(tool["tool_call_id"])
        if state.pending_child_id is None:
            assert tool["tool_name"] in {"run_subagent", "continue_subagent"}
            assert state.pending_agent_name is not None
            invoking_agent, toolkit_name = state.pending_agent_name, "delegate"
        else:
            source = state.pending_tool_sources[call_id]
            invoking_agent, toolkit_name = source.child.child_agent_name, source.toolkit_name
        calls.append(
            ApprovalCall(
                tool_call_id=call_id,
                tool_name=str(tool["tool_name"]),
                invoking_agent=invoking_agent,
                toolkit_name=toolkit_name,
                expires_at_ns=2**62,
                arguments_digest=approval_arguments_digest(tool["tool_args"]),
            ),
        )
    return tuple(calls)


def approval_calls_for(
    paused: PausedAttempt,
    *,
    default_agent: str,
    toolkit_names: Mapping[str, str],
) -> tuple[ApprovalCall, ...]:
    """Return the approval cards' calls for one pause: each paused tool bound to its arguments and toolkit."""
    agent = paused.approval_agent_name or default_agent
    return tuple(
        ApprovalCall(
            tool_call_id=str(tool.tool_call_id),
            tool_name=str(tool.tool_name),
            invoking_agent=agent,
            toolkit_name=toolkit_names[str(tool.tool_name)],
            expires_at_ns=2**62,
            arguments_digest=approval_arguments_digest(tool.tool_args),
        )
        for tool in paused.tools
    )


async def respond(
    agent_name: str,
    prompt: str,
    *,
    config: Config,
    runtime_paths: RuntimePaths,
    identity: ToolExecutionIdentity,
    run_id: str | None = None,
    stream: bool = False,
    hook_registry: HookRegistry | None = None,
    correlation_id: str | None = None,
) -> str | PausedAttempt:
    """Run one reply of ``agent_name`` as the response runner does: its text, or the pause it suspended on.

    A ``correlation_id`` is the turn's and its tool context's, as a production turn's is.
    """
    ctx = make_turn_context(
        agent_name,
        session_id=identity.session_id,
        run_id=run_id,
        correlation_id=correlation_id or f"corr-{run_id or 'turn'}",
        room_id=identity.room_id,
        thread_id=identity.thread_id,
        requester_id=identity.requester_id,
    )
    runtime = _delegate_runtime_context(
        config,
        runtime_paths,
        execution_identity=identity,
        agent_name=agent_name,
        hook_registry=hook_registry,
        correlation_id=correlation_id,
    )
    with tool_runtime_context(runtime):
        try:
            return await ai_response(
                ctx,
                prompt=prompt,
                runtime_paths=runtime_paths,
                config=config,
                execution_identity=identity,
                supports_native_tool_approval=True,
                collect_streamed_response=stream,
            )
        except ResponsePausedForApproval as suspended:
            return suspended.paused


async def _run_in_tool_context[T](
    *,
    tool_context: ToolRuntimeContext | None,
    operation: Callable[[], Awaitable[T]],
) -> T:
    with tool_runtime_context(tool_context):
        return await operation()


def approval_executor(config: Config, runtime_paths: RuntimePaths) -> AgentApprovalExecution:
    """Return the agent approval executor a response runner builds, around a detached runtime."""
    return AgentApprovalExecution(
        config=lambda: config,
        runtime_paths=runtime_paths,
        client=AsyncMock,
        tool_runtime=MagicMock(run_in_context=_run_in_tool_context),
        knowledge_access=KnowledgeAccessSupport(
            runtime=MagicMock(config=config, orchestrator=None),
            runtime_paths=runtime_paths,
        ),
        refresh_scheduler=lambda: None,
    )


async def resume(
    execution: AgentApprovalExecution,
    paused: PausedAttempt,
    calls: Sequence[ApprovalCall],
    *,
    agent_name: str,
    identity: ToolExecutionIdentity,
    config: Config,
    runtime_paths: RuntimePaths,
    decisions: Mapping[str, bool],
    denial_reasons: Mapping[str, str | None] | None = None,
    approval_id: str = "approval",
    request_body: str = "",
    hook_registry: HookRegistry | None = None,
    correlation_id: str | None = None,
) -> CompletedApprovalRun | PausedAttempt:
    """Continue ``paused`` with ``decisions`` on ``calls`` through the production approval execution.

    A ``correlation_id`` is the continued turn's, which its continuation and tool context carry as a production
    approval's do.
    """
    continuation = approval_continuation(
        approval_id=approval_id,
        run_id=paused.run_id,
        session_id=paused.session_id,
        entity_name=agent_name,
        room_id=identity.room_id,
        thread_id=identity.thread_id,
        requester_id=identity.requester_id,
        state="claimed",
        calls=tuple(calls),
        request_body=request_body,
        runtime_model_name=paused.runtime_model_name,
        continuation_count=paused.continuation_count,
        delegation_storage_bindings=paused.delegation_storage_bindings,
        correlation_id=correlation_id,
    )
    runtime = _delegate_runtime_context(
        config,
        runtime_paths,
        execution_identity=identity,
        agent_name=agent_name,
        hook_registry=hook_registry,
        correlation_id=correlation_id,
    )
    return await execution.continue_run(
        continuation,
        paused_answer=PausedAnswer(text=paused.response_text, tool_trace=paused.tool_trace),
        execution_identity=identity,
        tool_dispatch=LiveToolDispatchContext(execution_identity=identity, runtime_context=runtime),
        decisions=dict(decisions),
        denial_reasons={call_id: (denial_reasons or {}).get(call_id) for call_id in decisions},
        tool_trace_collector=[],
        typing_log_context={},
        progress=None,
    )


def stored_run(
    entity_name: str,
    run_id: str,
    *,
    config: Config,
    runtime_paths: RuntimePaths,
    identity: ToolExecutionIdentity,
) -> AnyRunRecord:
    """Return the run ``entity_name`` stored under ``run_id``: an agent's from its store, a team's from its scope's."""
    kind = "team" if entity_name in config.teams else "agent"
    store = create_scope_session_storage(
        agent_name=entity_name,
        scope=HistoryScope(kind=kind, scope_id=entity_name),
        config=config,
        runtime_paths=runtime_paths,
        execution_identity=identity,
    )
    record = store.get_run(run_id)
    assert record is not None, f"{entity_name} stored no run {run_id}"
    return record


@dataclass(frozen=True)
class PausedParent:
    """A parent run the loop stored paused on its delegation calls, and the resume that continues it."""

    record: RunRecord
    store: ConversationStore
    session: ConversationSession
    runner: ResumeRunner


async def pause_parent(
    agent_name: str,
    prompt: str,
    *,
    config: Config,
    runtime_paths: RuntimePaths,
    identity: ToolExecutionIdentity,
    run_id: str = "parent-run",
) -> PausedParent:
    """Run one turn of ``agent_name`` on the loop alone, without the delegation driver, until it pauses."""
    store = create_session_storage(agent_name, config, runtime_paths, identity)
    build = agents.create_agent(
        agent_name,
        config,
        runtime_paths,
        identity,
        history_storage=store,
        supports_native_tool_approval=True,
    )
    session = new_scope_session(session_id=identity.session_id or "", scope_id=agent_name, is_team=False)
    request = [prompt_for_turn(build), OwnedMessage(role="user", content=prompt)]
    ctx = RunContext(run_id=run_id, session_id=session.session_id, user_id=identity.requester_id)
    record: RunRecord | None = None
    async for event in run_agent(build.spec, request, ctx=ctx, persist=persist_to(store, session)):
        if isinstance(event, RunFinished):
            assert isinstance(event.record, RunRecord)
            record = event.record
    assert record is not None
    assert record.status is RunStatus.paused, record.status
    runner = ResumeRunner(
        build=build,
        config=config,
        runtime_paths=runtime_paths,
        agent_name=agent_name,
        model_name=config.get_agent(agent_name).model,
        store=store,
        session=session,
        scope=HistoryScope(kind="agent", scope_id=agent_name),
        user_id=identity.requester_id,
    )
    return PausedParent(record, store, session, runner)


def _team_runtime(
    team_name: str,
    *,
    config: Config,
    runtime_paths: RuntimePaths,
    identity: ToolExecutionIdentity,
    correlation_id: str | None = None,
) -> tuple[ToolExecutionIdentity, ToolRuntimeContext]:
    team_identity = replace(identity, agent_name=team_name)
    runtime = _delegate_runtime_context(
        config,
        runtime_paths,
        execution_identity=team_identity,
        agent_name=team_name,
        correlation_id=correlation_id,
    )
    return team_identity, runtime


async def respond_team(
    team_name: str,
    prompt: str,
    *,
    config: Config,
    runtime_paths: RuntimePaths,
    identity: ToolExecutionIdentity,
    run_id: str | None = None,
    correlation_id: str | None = None,
) -> str | PausedAttempt:
    """Run one blocking reply of the configured team ``team_name``: its text, or the pause it suspended on.

    A ``correlation_id`` is the turn's and its tool context's, as a production turn's is.
    """
    members = config.teams[team_name].agents
    team_identity, runtime = _team_runtime(
        team_name,
        config=config,
        runtime_paths=runtime_paths,
        identity=identity,
        correlation_id=correlation_id,
    )
    orchestrator = team_orchestrator(config, runtime_paths)
    orchestrator.agent_bots = {name: SimpleNamespace(running=True) for name in members}
    ctx = make_turn_context(
        team_name,
        session_id=identity.session_id,
        run_id=run_id,
        correlation_id=correlation_id or f"corr-{run_id or 'team'}",
        room_id=identity.room_id,
        thread_id=identity.thread_id,
        requester_id=identity.requester_id,
    )
    with tool_runtime_context(runtime):
        try:
            return await team_response(
                agent_names=list(members),
                mode=TeamMode(config.teams[team_name].mode),
                message=prompt,
                orchestrator=orchestrator,
                execution_identity=team_identity,
                ctx=ctx,
                user_id=identity.requester_id,
                configured_team_name=team_name,
                turn_recorder=TurnRecorder(user_message=prompt),
            )
        except ResponsePausedForApproval as suspended:
            return suspended.paused


async def resume_team(
    team_name: str,
    paused: PausedAttempt,
    calls: Sequence[ApprovalCall],
    *,
    config: Config,
    runtime_paths: RuntimePaths,
    identity: ToolExecutionIdentity,
    decisions: Mapping[str, bool],
    correlation_id: str | None = None,
) -> CompletedApprovalRun | PausedAttempt:
    """Continue the team's ``paused`` run with ``decisions`` on ``calls`` through the production team resume.

    A ``correlation_id`` is the continued turn's, which its tool context carries as a production approval's does.
    """
    team_identity, runtime = _team_runtime(
        team_name,
        config=config,
        runtime_paths=runtime_paths,
        identity=identity,
        correlation_id=correlation_id,
    )
    with tool_runtime_context(runtime):
        return await continue_paused_team_run(
            member_names=tuple(config.teams[team_name].agents),
            mode=TeamMode(config.teams[team_name].mode),
            config=config,
            runtime_paths=runtime_paths,
            execution_identity=team_identity,
            session_id=paused.session_id,
            run_id=paused.run_id,
            user_id=identity.requester_id,
            configured_team_name=team_name,
            model_name=paused.runtime_model_name or "default",
            decisions=dict(decisions),
            denial_reasons=dict.fromkeys(decisions),
            refresh_scheduler=None,
            approval_calls=calls,
            prior_tool_trace=paused.tool_trace,
            prior_presentation_state=paused.response_presentation_state,
            progress=None,
        )


class ReportTools(Toolkit):
    """One gated function the delegation tests watch: ``write_report`` records each run as a side effect."""

    written: ClassVar[list[str]] = []

    def __init__(self) -> None:
        super().__init__(name="report", tools=[self.write_report])

    async def write_report(self) -> str:
        """Write the report."""
        type(self).written.append("written")
        return "report written"


@contextmanager
def registered_report_tool() -> Iterator[type[ReportTools]]:
    """Register the ``report`` toolkit for one test and restore the registries afterwards."""
    ReportTools.written.clear()
    with registered_toolkit("report", ReportTools):
        yield ReportTools

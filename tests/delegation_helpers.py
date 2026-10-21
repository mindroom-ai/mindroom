"""Shared scripted models, requester contexts, and approval drivers for delegation integration tests.

``DelegationModel`` is the Agno model double the suites that still drive Agno agents use; the owned path's tests
script one ``ScriptedModel`` per agent through ``ScriptedAgents`` and run replies through ``respond``.
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, ClassVar, cast
from unittest.mock import AsyncMock, MagicMock

from agno.tools import Toolkit

from mindroom import agents
from mindroom.ai import ai_response
from mindroom.approval.execution import AgentApprovalExecution
from mindroom.constants import resolve_runtime_paths
from mindroom.event_journal import ApprovalCall, approval_arguments_digest
from mindroom.knowledge.utils import KnowledgeAccessSupport
from mindroom.message_target import MessageTarget
from mindroom.response_turn import PausedAnswer, ResponsePausedForApproval
from mindroom.tool_system.declarations import ToolFileAccess
from mindroom.tool_system.metadata import TOOL_METADATA, TOOL_REGISTRY, ToolCategory
from mindroom.tool_system.registration import register_tool_with_metadata
from mindroom.tool_system.runtime_context import LiveToolDispatchContext, tool_runtime_context
from tests.approval_continuation_helpers import approval_continuation
from tests.authorization_helpers import make_test_tool_runtime_context
from tests.conftest import make_conversation_reader_mock, make_relation_lookup, make_turn_context
from tests.history_helpers import RecordingModel

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable, Iterator, Mapping, Sequence
    from pathlib import Path

    import pytest
    from agno.models.message import Message
    from agno.models.response import ModelResponse

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.delegation.state import DelegationState
    from mindroom.llm.provider import ModelProvider
    from mindroom.response_turn import CompletedApprovalRun, PausedAttempt
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
) -> ToolRuntimeContext:
    """Build the requester context every successful delegation requires."""
    room_id = execution_identity.room_id if execution_identity is not None else "!room:example.org"
    source_thread_id = execution_identity.thread_id if execution_identity is not None else None
    resolved_thread_id = execution_identity.resolved_thread_id if execution_identity is not None else None
    session_id = execution_identity.session_id if execution_identity is not None else room_id
    requester_id = execution_identity.requester_id if execution_identity is not None else "@alice:example.org"
    return make_test_tool_runtime_context(
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
    )


@dataclass
class DelegationModel(RecordingModel):
    """Deterministic Agno model for the suites that still run real Agno agents."""

    responses: list[ModelResponse] = field(default_factory=list)

    async def ainvoke(self, *_args: object, **kwargs: object) -> ModelResponse:
        """Consume one planned provider response."""
        self.seen_messages = list(cast("list[Message]", kwargs.get("messages", [])))
        return self.responses.pop(0)

    async def ainvoke_stream(self, *_args: object, **kwargs: object) -> AsyncIterator[ModelResponse]:
        """Use planned responses through Agno's real streaming machinery."""
        yield await self.ainvoke(*_args, **kwargs)


@dataclass
class ScriptedAgents:
    """One scripted model per agent, handed to ``create_agent`` for the agent its execution identity names.

    ``by_session`` serves a model by the identity's session first, for an agent that delegates to itself.
    """

    models: dict[str, ScriptedModel]
    by_session: dict[str, ScriptedModel] = field(default_factory=dict)

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Serve the scripted models from the seam ``create_agent`` builds providers at."""
        monkeypatch.setattr(agents, "_build_agent_model_provider", self._build)

    def _build(
        self,
        _config: Config,
        _runtime_paths: RuntimePaths,
        model_name: str,
        execution_identity: ToolExecutionIdentity | None,
    ) -> ModelProvider:
        if execution_identity is None:
            return self.models[model_name]
        if (session := execution_identity.session_id) is not None and session in self.by_session:
            return self.by_session[session]
        return self.models[execution_identity.agent_name]


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
) -> str | PausedAttempt:
    """Run one reply of ``agent_name`` as the response runner does: its text, or the pause it suspended on."""
    ctx = make_turn_context(
        agent_name,
        session_id=identity.session_id,
        run_id=run_id,
        correlation_id=f"corr-{run_id or 'turn'}",
        room_id=identity.room_id,
        thread_id=identity.thread_id,
        requester_id=identity.requester_id,
    )
    runtime = _delegate_runtime_context(config, runtime_paths, execution_identity=identity, agent_name=agent_name)
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
) -> CompletedApprovalRun | PausedAttempt:
    """Continue ``paused`` with ``decisions`` on ``calls`` through the production approval execution."""
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
    )
    runtime = _delegate_runtime_context(config, runtime_paths, execution_identity=identity, agent_name=agent_name)
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
    original_registry = TOOL_REGISTRY.copy()
    original_metadata = TOOL_METADATA.copy()

    @register_tool_with_metadata(
        name="report",
        file_access=ToolFileAccess.NONE,
        display_name="Report",
        description="Writes the report a delegated child was asked for.",
        category=ToolCategory.DEVELOPMENT,
    )
    def report_factory() -> type[ReportTools]:
        return ReportTools

    ReportTools.written.clear()
    try:
        yield ReportTools
    finally:
        TOOL_REGISTRY.clear()
        TOOL_REGISTRY.update(original_registry)
        TOOL_METADATA.clear()
        TOOL_METADATA.update(original_metadata)

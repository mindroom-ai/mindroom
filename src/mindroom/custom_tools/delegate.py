"""Agent delegation tools for MindRoom agents.

Allows an agent to run configured agents as fresh subagents via tool calls.
The delegated agent runs independently as a one-shot agent and returns its
response as the tool result.
"""

from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack
from contextvars import ContextVar
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING
from uuid import uuid4

from agno.tools import Toolkit

from mindroom.agent_descriptions import describe_agent
from mindroom.agent_run_context import append_knowledge_availability_enrichment
from mindroom.ai import ResponseTurnContext, ai_response
from mindroom.authorization import is_sender_allowed_for_responder
from mindroom.delegation_audit import (
    child_audit_context,
    finish_child_record,
    start_child_record,
)
from mindroom.delegation_state import DelegationChild
from mindroom.knowledge.utils import resolve_agent_knowledge_access_async
from mindroom.logging_config import get_logger
from mindroom.response_turn import ResponsePausedForApproval
from mindroom.tool_system.runtime_context import (
    ToolRuntimeContext,
    ToolRuntimeModelBinding,
    get_detached_requester_context,
    get_tool_runtime_context,
    tool_runtime_context,
)
from mindroom.tool_system.worker_routing import (
    build_tool_execution_identity,
    serialize_tool_execution_identity,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from agno.run import RunContext
    from agno.tools.function import FunctionCall

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.knowledge.refresh_scheduler import KnowledgeRefreshScheduler
    from mindroom.tool_system.worker_routing import ToolExecutionIdentity

logger = get_logger(__name__)

MAX_DELEGATION_DEPTH = 3


@dataclass(frozen=True)
class _DirectDelegationProvenance:
    """Trusted parent identifiers scoped to one executing Agno tool call."""

    run_id: str
    tool_call_id: str | None


_DIRECT_DELEGATION_PROVENANCE: ContextVar[tuple[_DirectDelegationProvenance, ...]] = ContextVar(
    "direct_delegation_provenance",
    default=(),
)


async def _capture_direct_delegation_provenance(
    *,
    fc: FunctionCall,
    run_context: RunContext,
) -> None:
    """Capture framework-owned parent identity before entering the tool body."""
    current = _DIRECT_DELEGATION_PROVENANCE.get()
    _DIRECT_DELEGATION_PROVENANCE.set(
        (*current, _DirectDelegationProvenance(run_id=run_context.run_id, tool_call_id=fc.call_id)),
    )


async def _clear_direct_delegation_provenance(
    *,
    fc: FunctionCall,
    run_context: RunContext,
) -> None:
    """Clear captured provenance from Agno's guaranteed post-call cleanup."""
    del fc, run_context
    current = _DIRECT_DELEGATION_PROVENANCE.get()
    _DIRECT_DELEGATION_PROVENANCE.set(current[:-1])


class DelegateTools(Toolkit):
    """Tools that let an agent run configured agents as fresh subagents."""

    def __init__(
        self,
        agent_name: str,
        delegate_to: list[str],
        runtime_paths: RuntimePaths,
        config: Config,
        execution_identity: ToolExecutionIdentity | None = None,
        delegation_depth: int = 0,
        refresh_scheduler: KnowledgeRefreshScheduler | None = None,
    ) -> None:
        self._agent_name = agent_name
        self._delegate_to = delegate_to
        self._runtime_paths = runtime_paths
        self._config = config
        self._execution_identity = execution_identity
        self._delegation_depth = delegation_depth
        self._refresh_scheduler = refresh_scheduler

        super().__init__(
            name="delegate",
            instructions=self._build_instructions(),
            tools=[self.run_subagent],
        )
        delegate_function = self.async_functions["run_subagent"]
        delegate_function.description = self._build_run_subagent_description()
        delegate_function.pre_hook = _capture_direct_delegation_provenance
        delegate_function.post_hook = _clear_direct_delegation_provenance

    def _build_instructions(self) -> str:
        """Build toolkit instructions listing available delegation targets."""
        lines: list[str] = []
        for target_name in self._delegate_to:
            description = describe_agent(target_name, self._config)
            lines.append(description)
        return self._config.render_prompt(
            "DELEGATE_TOOLKIT_INSTRUCTIONS_TEMPLATE",
            agent_descriptions="\n\n".join(lines),
        )

    def _build_run_subagent_description(self) -> str:
        """Build the model-facing function description with this caller's allowlist."""
        available_targets = ", ".join(self._delegate_to)
        return (
            "Run one allowed configured agent as a fresh subagent and wait for its result.\n"
            f"Allowed subagents for this caller: {available_targets}.\n"
            "Use only these agent names. Include all relevant context, constraints, and expected output in task; "
            "the child does not inherit this conversation. It keeps its configured tools, workspace, and memory.\n"
            "Selecting your own name starts a fresh copy of yourself, if listed. "
            "The caller waits; this does not create a Matrix thread. "
            "Use matrix_message for an ongoing conversation instead.\n"
            "In Matrix, approval-required child tools pause for the user's approval before continuing. "
            "Returns the child's answer and an audit reference scoped to the child agent."
        )

    async def run_subagent(self, agent_name: str, task: str) -> str:
        """Run a fresh subagent and wait for its response and audit reference.

        The runtime-generated tool description lists caller-specific allowed
        targets and model guidance.

        Args:
            agent_name: One of the allowed subagent names, including your own name if listed.
            task: Self-contained task with relevant context, constraints, and expected output.

        Returns:
            The delegated agent's response, or an error message if delegation failed.

        """
        return await self.run_delegated_task(agent_name, task)

    def authorize(self, agent_name: str, task: str) -> Config | str:  # noqa: PLR0911
        """Recheck the current caller allowlist and requester authority."""
        if not task or not task.strip():
            return "Cannot delegate an empty task. Please provide a task description."

        if agent_name not in self._delegate_to:
            available = ", ".join(self._delegate_to)
            return f"Cannot delegate to '{agent_name}'. Allowed subagents: {available}."

        runtime_context = get_tool_runtime_context()
        detached_context = get_detached_requester_context()
        if runtime_context is not None:
            active_config = runtime_context.current_config
            requester_id = runtime_context.requester_id
            authorization_room_id = runtime_context.room_id
            membership_index = runtime_context.require_agent_reply_memberships()
        elif (
            detached_context is not None
            and self._execution_identity is not None
            and self._execution_identity.channel == "openai_compat"
            and self._execution_identity.requester_id == detached_context.requester_id
            and self._runtime_paths == detached_context.runtime_paths
        ):
            active_config = detached_context.config_provider()
            requester_id = detached_context.requester_id
            authorization_room_id = None
            membership_index = detached_context.agent_reply_memberships
        else:
            return f"Cannot delegate to '{agent_name}': requester authorization is unavailable."
        if active_config is None or agent_name not in active_config.agents:
            return f"Cannot delegate to '{agent_name}': that agent is not allowed to reply to you."
        caller_config = active_config.agents.get(self._agent_name)
        caller_allows_target = caller_config is not None and agent_name in caller_config.delegate_to
        if not caller_allows_target or not is_sender_allowed_for_responder(
            requester_id,
            agent_name,
            authorization_room_id,
            active_config,
            self._runtime_paths,
            membership_index,
        ):
            reason = (
                "it is no longer an allowed target"
                if not caller_allows_target
                else "that agent is not allowed to reply to you"
            )
            return f"Cannot delegate to '{agent_name}': {reason}."

        if self._delegation_depth >= MAX_DELEGATION_DEPTH:
            return "Cannot delegate: the maximum delegation depth was reached."
        return active_config

    async def run_delegated_task(  # noqa: C901, PLR0915
        self,
        agent_name: str,
        task: str,
        *,
        session_id: str | None = None,
        run_id: str | None = None,
        active_model_name: str | None = None,
        supports_native_tool_approval: bool = False,
        run_id_callback: Callable[[str], None] | None = None,
    ) -> str:
        """Execute an authorized fresh child, optionally owned by a durable parent."""
        active_config = self.authorize(agent_name, task)
        if isinstance(active_config, str):
            return active_config
        runtime_context = get_tool_runtime_context()
        provenance_stack = _DIRECT_DELEGATION_PROVENANCE.get()
        direct_provenance = provenance_stack[-1] if provenance_stack else None
        requester_id = (
            runtime_context.requester_id
            if runtime_context is not None
            else self._execution_identity.requester_id
            if self._execution_identity is not None
            else None
        )
        record_child: DelegationChild | None = None
        try:
            session_id = session_id or f"delegate:{self._agent_name}:{agent_name}:{uuid4()}"
            execution_identity = (
                replace(self._execution_identity, agent_name=agent_name, session_id=session_id)
                if self._execution_identity is not None
                else None
            )

            knowledge_resolution = await resolve_agent_knowledge_access_async(
                agent_name,
                active_config,
                self._runtime_paths,
                refresh_scheduler=self._refresh_scheduler,
                execution_identity=execution_identity,
            )
            transient_enrichment_items = append_knowledge_availability_enrichment(
                (),
                knowledge_resolution.unavailable,
            )
            logger.info(
                "Delegating task",
                from_agent=self._agent_name,
                to_agent=agent_name,
                requester_id=requester_id,
                depth=self._delegation_depth + 1,
                task_preview=task[:100],
            )
            room_id = _resolve_delegated_room_id(
                runtime_context=runtime_context,
                execution_identity=execution_identity,
            )
            thread_id = _resolve_delegated_thread_id(
                runtime_context=runtime_context,
                execution_identity=execution_identity,
            )
            active_model_name = (
                active_model_name
                or active_config.resolve_runtime_model(
                    entity_name=agent_name,
                    room_id=room_id,
                    thread_id=thread_id,
                    runtime_paths=self._runtime_paths,
                ).model_name
            )
            run_id = run_id or uuid4().hex
            if not supports_native_tool_approval:
                child_record_identity = _record_execution_identity(
                    agent_name=agent_name,
                    session_id=session_id,
                    runtime_context=runtime_context,
                    configured_identity=self._execution_identity,
                    runtime_paths=self._runtime_paths,
                )
                child_record_identity = _require_record_identity(child_record_identity)
                record_child = DelegationChild(
                    delegation_id=uuid4().hex,
                    parent_tool_call_id=(direct_provenance.tool_call_id or "" if direct_provenance is not None else ""),
                    caller_agent_name=self._agent_name,
                    child_agent_name=agent_name,
                    task=task,
                    session_id=session_id,
                    run_id=run_id,
                    model_name=active_model_name,
                    depth=self._delegation_depth + 1,
                    execution_identity=serialize_tool_execution_identity(child_record_identity),
                )
                await start_child_record(
                    record_child,
                    parent_run_id=direct_provenance.run_id if direct_provenance is not None else None,
                    config=active_config,
                    runtime_paths=self._runtime_paths,
                    caller_execution_identity=_record_execution_identity(
                        agent_name=self._agent_name,
                        session_id=runtime_context.session_id if runtime_context is not None else None,
                        runtime_context=runtime_context,
                        configured_identity=self._execution_identity,
                        runtime_paths=self._runtime_paths,
                    ),
                )
            delegated_runtime_context = self._build_delegated_runtime_context(
                agent_name=agent_name,
                session_id=session_id,
                runtime_context=runtime_context,
                active_model_name=active_model_name,
            )
            delegated_correlation_id = (
                delegated_runtime_context.correlation_id if delegated_runtime_context is not None else None
            )
            turn_ctx = ResponseTurnContext(
                entity_label=agent_name,
                session_id=session_id,
                run_id=run_id,
                correlation_id=delegated_correlation_id or uuid4().hex,
                reply_to_event_id=None,
                room_id=room_id,
                thread_id=thread_id,
                requester_id=requester_id,
                matrix_run_metadata=None,
                active_model_name=active_model_name,
                transient_enrichment_items=tuple(transient_enrichment_items),
            )

            def note_child_run_id(active_run_id: str) -> None:
                if record_child is not None:
                    record_child.run_id = active_run_id
                if run_id_callback is not None:
                    run_id_callback(active_run_id)

            async with AsyncExitStack() as audit_stack:
                if record_child is not None:
                    await audit_stack.enter_async_context(
                        child_audit_context(
                            record_child,
                            config=active_config,
                            runtime_paths=self._runtime_paths,
                        ),
                    )
                with tool_runtime_context(delegated_runtime_context):
                    response = await ai_response(
                        turn_ctx,
                        prompt=task,
                        runtime_paths=self._runtime_paths,
                        config=active_config,
                        knowledge=knowledge_resolution.knowledge,
                        run_id_callback=note_child_run_id,
                        include_interactive_questions=False,
                        include_openai_compat_guidance=(
                            execution_identity is not None and execution_identity.channel == "openai_compat"
                        ),
                        tool_function_filter=(
                            runtime_context.tool_function_filter if runtime_context is not None else None
                        ),
                        execution_identity=execution_identity,
                        delegation_depth=self._delegation_depth + 1,
                        refresh_scheduler=self._refresh_scheduler,
                        attempt_model_runtime=ToolRuntimeModelBinding(),
                        supports_native_tool_approval=supports_native_tool_approval,
                        collect_streamed_response=True,
                    )
        except asyncio.CancelledError:
            if record_child is not None:
                record_child.status = "cancelled"
                record_child.result = "Delegation cancelled."
                await finish_child_record(
                    record_child,
                    config=active_config,
                    runtime_paths=self._runtime_paths,
                )
            raise
        except ResponsePausedForApproval:
            raise
        except Exception as e:
            logger.exception(
                "Delegation failed",
                from_agent=self._agent_name,
                to_agent=agent_name,
                error=str(e),
            )
            message = f"Delegation to '{agent_name}' failed: {e}"
            if record_child is not None:
                record_child.status = "failed"
                record_child.result = str(e)
                receipt = await finish_child_record(
                    record_child,
                    config=active_config,
                    runtime_paths=self._runtime_paths,
                )
                return _result_with_receipt(message, receipt)
            return message
        else:
            result = response or "Agent completed the task but returned no content."
            if record_child is not None:
                if record_child.status not in {"completed", "failed", "cancelled", "denied"}:
                    record_child.status = "failed"
                    record_child.result = "Delegated run ended without a retained terminal outcome."
                receipt = await finish_child_record(
                    record_child,
                    config=active_config,
                    runtime_paths=self._runtime_paths,
                )
                return _result_with_receipt(result, receipt)
            return result

    def _build_delegated_runtime_context(
        self,
        *,
        agent_name: str,
        session_id: str,
        runtime_context: ToolRuntimeContext | None,
        active_model_name: str,
    ) -> ToolRuntimeContext | None:
        """Return the child tool runtime context for one delegated run."""
        if runtime_context is None:
            return None
        return replace(
            runtime_context,
            agent_name=agent_name,
            active_model_name=active_model_name,
            target=replace(runtime_context.target, session_id=session_id),
        )


def _resolve_delegated_room_id(
    *,
    runtime_context: ToolRuntimeContext | None,
    execution_identity: ToolExecutionIdentity | None,
) -> str | None:
    """Resolve the room context that should apply to a delegated child run."""
    if runtime_context is not None:
        return runtime_context.room_id
    if execution_identity is not None:
        return execution_identity.room_id
    return None


def _resolve_delegated_thread_id(
    *,
    runtime_context: ToolRuntimeContext | None,
    execution_identity: ToolExecutionIdentity | None,
) -> str | None:
    """Resolve the thread context that should apply to a delegated child run."""
    if runtime_context is not None:
        return runtime_context.resolved_thread_id
    if execution_identity is not None:
        return execution_identity.resolved_thread_id
    return None


def _record_execution_identity(
    *,
    agent_name: str,
    session_id: str | None,
    runtime_context: ToolRuntimeContext | None,
    configured_identity: ToolExecutionIdentity | None,
    runtime_paths: RuntimePaths,
) -> ToolExecutionIdentity | None:
    """Return the exact scoped identity used only for audit workspace resolution."""
    if configured_identity is not None:
        return replace(configured_identity, agent_name=agent_name, session_id=session_id)
    if runtime_context is None:
        return None
    return build_tool_execution_identity(
        channel="matrix",
        agent_name=agent_name,
        transport_agent_name=runtime_context.transport_agent_name,
        runtime_paths=runtime_paths,
        requester_id=runtime_context.requester_id,
        room_id=runtime_context.room_id,
        thread_id=runtime_context.thread_id,
        resolved_thread_id=runtime_context.resolved_thread_id,
        session_id=session_id,
    )


def _require_record_identity(identity: ToolExecutionIdentity | None) -> ToolExecutionIdentity:
    """Require the execution identity already established by authorization."""
    if identity is None:
        msg = "Delegation audit requires a child execution identity"
        raise RuntimeError(msg)
    return identity


def _result_with_receipt(result: str, receipt: str) -> str:
    """Attach the stable child record reference to a direct tool result."""
    return f"{result}\n\n{receipt}"

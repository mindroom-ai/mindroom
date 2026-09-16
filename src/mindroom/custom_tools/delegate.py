"""Agent delegation tools for MindRoom agents.

Allows an agent to start configured subagents and continue their sessions.
Each turn runs independently and returns its response as the tool result.
"""

from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack
from contextvars import ContextVar
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from agno.tools import Toolkit

from mindroom.agent_descriptions import describe_agent
from mindroom.ai import run_delegated_child_response
from mindroom.delegation.background import delegation_child
from mindroom.delegation.lifecycle import (
    authorize_delegation,
    child_run_context,
    finish_child_turn,
    prepare_child_turn,
    reserve_child_turn,
    start_child_turn,
)
from mindroom.delegation.recovery import resolve_subagent
from mindroom.delegation.sessions import (
    SubagentSessionError,
    subagent_liveness,
)
from mindroom.logging_config import get_logger
from mindroom.response_turn import ResponsePausedForApproval
from mindroom.tool_jobs.runtime import JobAccessError, get_background_runtime
from mindroom.tool_system.runtime_context import (
    get_tool_runtime_context,
)
from mindroom.tool_system.worker_routing import (
    build_tool_execution_identity,
)

if TYPE_CHECKING:
    from agno.run import RunContext
    from agno.tools.function import FunctionCall

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.delegation.state import DelegationChild
    from mindroom.knowledge.refresh_scheduler import KnowledgeRefreshScheduler
    from mindroom.tool_system.worker_routing import ToolExecutionIdentity

logger = get_logger(__name__)


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
            tools=[
                self.run_subagent,
                self.continue_subagent,
                self.inspect_subagent,
                self.wait_subagent,
                self.resume_subagent,
                self.cancel_subagent,
            ],
        )
        delegate_function = self.async_functions["run_subagent"]
        delegate_function.description = self._build_run_subagent_description()
        for function in self.async_functions.values():
            function.pre_hook = _capture_direct_delegation_provenance
            function.post_hook = _clear_direct_delegation_provenance

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
            "Omit agent_name or pass null to select yourself; the same allowlist applies. "
            "Managed Matrix calls wait up to 10 seconds, then return a Job ID while work continues. "
            "A human follow-up releases the wait and pauses the child before its next tool. "
            "Use continue_subagent with the returned subagent_id for follow-ups in the same child session.\n"
            "In Matrix, approval-required child tools pause for the user's approval before continuing. "
            "Returns the child's answer, stable subagent ID, and an audit reference scoped to the child agent."
        )

    async def run_subagent(self, task: str, agent_name: str | None = None) -> str:
        """Run a fresh subagent and wait for its response and audit reference.

        The runtime-generated tool description lists caller-specific allowed
        targets and model guidance.

        Args:
            task: Self-contained task with relevant context, constraints, and expected output.
            agent_name: Allowed subagent name; omitted or null selects yourself, if allowed.

        Returns:
            The delegated agent's response, or an error message if delegation failed.

        """
        return await self._run_child(self._agent_name if agent_name is None else agent_name, task)

    def _caller_identity(self) -> ToolExecutionIdentity:
        """Resolve one concrete caller identity for execution, ownership, and audit."""
        context = get_tool_runtime_context()
        if self._execution_identity is not None:
            return replace(
                self._execution_identity,
                agent_name=self._agent_name,
                session_id=context.session_id if context is not None else self._execution_identity.session_id,
            )
        if context is None:
            msg = "Delegation requires a caller execution identity"
            raise RuntimeError(msg)
        return build_tool_execution_identity(
            channel="matrix",
            agent_name=self._agent_name,
            transport_agent_name=context.transport_agent_name,
            runtime_paths=self._runtime_paths,
            requester_id=context.requester_id,
            room_id=context.room_id,
            thread_id=context.thread_id,
            resolved_thread_id=context.resolved_thread_id,
            session_id=context.session_id,
        )

    async def continue_subagent(self, subagent_id: str, message: str) -> str:
        """Send a follow-up to an existing subagent and wait for its answer.

        Reuses the child's conversation history, tools, workspace, and memory.
        Use after its previous turn returns; finish pending approvals first.
        The ID belongs to this caller and conversation, including after restart.
        Current allowed-subagent and requester permissions still apply.
        Each follow-up gets a separate audit record and returns the same subagent ID.

        Args:
            subagent_id: Exact Subagent ID returned by run_subagent or continue_subagent.
            message: Follow-up instructions or question for that child.

        Returns:
            The child's answer, stable subagent ID, and this turn's audit reference, or an error.

        """
        try:
            child = await resolve_subagent(
                subagent_id,
                owner=self._caller_identity(),
                config=self._config,
                runtime_paths=self._runtime_paths,
                depth=self._delegation_depth,
            )
        except (SubagentSessionError, JobAccessError) as error:
            return str(error)
        return await self._run_child(child.child_agent_name, message, continuation=child)

    async def _control_job(self, job_id: str, operation: str) -> str:
        """Validate current delegation authority before reading or controlling one exact turn."""
        runtime = get_background_runtime(self._runtime_paths)
        if runtime is None or self._delegation_depth != 0:
            return "Background subagent controls require a managed Matrix conversation."
        owner = self._caller_identity()
        try:
            job = await runtime.lookup(job_id, owner=owner, depth=self._delegation_depth)
            authorization = authorize_delegation(
                self._agent_name,
                delegation_child(job).child_agent_name,
                delegation_child(job).task,
                config=self._config,
                runtime_paths=self._runtime_paths,
                execution_identity=owner,
                depth=self._delegation_depth,
                allowed_targets=self._delegate_to,
            )
            if isinstance(authorization, str):
                return authorization
            if operation == "resume":
                job = await runtime.resume(job_id, owner=owner, depth=self._delegation_depth)
            elif operation == "cancel":
                job = await runtime.cancel(job_id, owner=owner, depth=self._delegation_depth, await_completion=True)
        except (SubagentSessionError, JobAccessError) as error:
            return str(error)
        result = f"Job ID: {job.job_id}\nSubagent ID: {delegation_child(job).subagent_id}\nStatus: {job.status}"
        if job.result is not None:
            result += f"\n\n{job.result}"
        return result

    async def inspect_subagent(self, job_id: str) -> str:
        """Read the current status and retained result of an exact background job.

        Args:
            job_id: Exact Job ID returned by a delegation call, not the reusable Subagent ID.

        """
        return await self._control_job(job_id, "inspect")

    async def wait_subagent(self, job_id: str) -> str:
        """Reattach to an exact background job for up to 10 seconds without restarting it.

        Approval requirements transfer to this parent call and still require user approval.
        Waiting does not resume a child paused for human input; use resume_subagent first.

        Args:
            job_id: Exact Job ID returned by a delegation call.

        """
        return await self._control_job(job_id, "inspect")

    async def resume_subagent(self, job_id: str) -> str:
        """Resume a child paused for human input; never grants tool approval.

        To redirect work, cancel its current job, then use continue_subagent with new instructions.

        Args:
            job_id: Exact Job ID of the paused turn.

        """
        return await self._control_job(job_id, "resume")

    async def cancel_subagent(self, job_id: str) -> str:
        """Cancel the exact background turn before continuing its conversation with new instructions.

        Args:
            job_id: Exact Job ID of the turn to cancel.

        """
        return await self._control_job(job_id, "cancel")

    async def _run_child(
        self,
        agent_name: str,
        task: str,
        *,
        continuation: DelegationChild | None = None,
    ) -> str:
        """Run one direct child using the shared preparation and settlement owner."""
        config = authorize_delegation(
            self._agent_name,
            agent_name,
            task,
            config=self._config,
            runtime_paths=self._runtime_paths,
            execution_identity=self._execution_identity,
            depth=self._delegation_depth,
            allowed_targets=self._delegate_to,
        )
        if isinstance(config, str):
            return config
        owner = self._caller_identity()
        provenance = _DIRECT_DELEGATION_PROVENANCE.get()
        parent = provenance[-1] if provenance else None
        child = prepare_child_turn(
            self._agent_name,
            agent_name,
            task,
            owner=owner,
            config=config,
            runtime_paths=self._runtime_paths,
            depth=self._delegation_depth,
            previous=continuation,
            parent_tool_call_id=(parent.tool_call_id or "") if parent is not None else "",
        )
        liveness = AsyncExitStack()
        try:
            await liveness.enter_async_context(subagent_liveness(child, self._runtime_paths))
            try:
                await reserve_child_turn(child, owner=owner, runtime_paths=self._runtime_paths)
            except (SubagentSessionError, JobAccessError) as error:
                return str(error)
            await start_child_turn(
                child,
                parent_run_id=parent.run_id if parent is not None else None,
                config=config,
                runtime_paths=self._runtime_paths,
                caller_execution_identity=owner,
            )
            async with child_run_context(child, config=config, runtime_paths=self._runtime_paths):
                response = await run_delegated_child_response(
                    child,
                    prompt=task,
                    config=config,
                    runtime_paths=self._runtime_paths,
                    refresh_scheduler=self._refresh_scheduler,
                    supports_native_tool_approval=False,
                )
        except asyncio.CancelledError:
            await finish_child_turn(
                child,
                config=config,
                runtime_paths=self._runtime_paths,
                status="cancelled",
                reason="Delegation cancelled.",
            )
            raise
        except ResponsePausedForApproval:
            raise
        except Exception as error:
            logger.exception("Delegation failed", from_agent=self._agent_name, to_agent=agent_name, error=str(error))
            receipt = await finish_child_turn(
                child,
                config=config,
                runtime_paths=self._runtime_paths,
                status="failed",
                reason=str(error),
            )
            return _result_with_receipt(f"Delegation to '{agent_name}' failed: {error}", receipt)
        else:
            receipt = await finish_child_turn(
                child,
                config=config,
                runtime_paths=self._runtime_paths,
                status="failed",
                reason="Delegated run ended without a retained terminal outcome.",
            )
            return _result_with_receipt(response or "Agent completed the task but returned no content.", receipt)
        finally:
            await liveness.aclose()


def _result_with_receipt(result: str, receipt: str) -> str:
    """Attach the stable child record reference to a direct tool result."""
    return f"{result}\n\n{receipt}" if receipt else result

"""Agent reconstruction and execution for native approval continuations."""

from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

from agno.run.agent import RunOutput
from agno.run.base import RunStatus

from mindroom.agents import create_agent
from mindroom.history.runtime import close_agent_runtime_state_dbs
from mindroom.matrix.typing import typing_indicator
from mindroom.response_turn import PausedAttempt, paused_attempt_from_response
from mindroom.tool_system.events import format_tool_completed_event
from mindroom.tool_system.runtime_context import runtime_context_from_dispatch_context
from mindroom.tool_system.worker_routing import run_with_tool_execution_identity

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    import nio

    from mindroom.approval_continuation import ApprovalContinuation
    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.tool_system.events import ToolTraceEntry
    from mindroom.tool_system.runtime_context import ToolDispatchContext, ToolRuntimeSupport
    from mindroom.tool_system.worker_routing import ToolExecutionIdentity


@dataclass(frozen=True)
class AgentApprovalExecution:
    """Rebuild and continue one persisted paused agent run."""

    config: Callable[[], Config]
    runtime_paths: RuntimePaths
    client: Callable[[], nio.AsyncClient]
    tool_runtime: ToolRuntimeSupport

    async def continue_run(  # noqa: C901
        self,
        continuation: ApprovalContinuation,
        *,
        execution_identity: ToolExecutionIdentity,
        tool_dispatch: ToolDispatchContext,
        decisions: dict[str, bool],
        denial_reasons: dict[str, str | None],
        tool_trace_collector: list[ToolTraceEntry],
    ) -> str | PausedAttempt:
        """Apply exact decisions and continue the matching persisted Agno run."""
        config = self.config()
        if continuation.entity_name not in config.agents:
            msg = f"Agent {continuation.entity_name!r} is no longer configured"
            raise RuntimeError(msg)
        agent = await asyncio.to_thread(
            create_agent,
            continuation.entity_name,
            config,
            self.runtime_paths,
            execution_identity,
            session_id=continuation.session_id,
            active_model_name=continuation.runtime_model_name,
            dynamic_tool_continuation=True,
            supports_native_tool_approval=True,
        )
        try:
            session = await agent.aget_session(
                session_id=continuation.session_id,
                user_id=continuation.requester_id,
            )
            persisted = None if session is None else session.get_run(continuation.run_id)
            if not isinstance(persisted, RunOutput) or persisted.status != RunStatus.paused:
                msg = f"Paused run {continuation.run_id!r} is no longer available"
                raise RuntimeError(msg)
            requirements = [
                deepcopy(requirement) for requirement in persisted.requirements or () if requirement.needs_confirmation
            ]
            paused_call_ids = {
                requirement.tool_execution.tool_call_id
                for requirement in requirements
                if requirement.tool_execution is not None and requirement.tool_execution.tool_call_id is not None
            }
            if len(paused_call_ids) != len(requirements) or paused_call_ids != decisions.keys():
                msg = "Paused tools no longer match the approval continuation"
                raise RuntimeError(msg)
            for requirement in requirements:
                tool = requirement.tool_execution
                assert tool is not None
                assert tool.tool_call_id is not None
                if decisions[tool.tool_call_id]:
                    requirement.confirm()
                else:
                    requirement.reject(denial_reasons[tool.tool_call_id] or "Not approved by requester")

            async def continue_run() -> RunOutput:
                result = agent.acontinue_run(
                    run_id=continuation.run_id,
                    requirements=requirements,
                    session_id=continuation.session_id,
                    user_id=continuation.requester_id,
                    stream=False,
                )
                return await cast("Awaitable[RunOutput]", result)

            async with typing_indicator(self.client(), continuation.room_id):
                response = await self.tool_runtime.run_in_context(
                    tool_context=runtime_context_from_dispatch_context(tool_dispatch),
                    operation=lambda: run_with_tool_execution_identity(
                        tool_dispatch.execution_identity,
                        operation=continue_run,
                    ),
                )
        finally:
            close_agent_runtime_state_dbs(agent)
        paused = paused_attempt_from_response(
            response,
            fallback_session_id=continuation.session_id,
            fallback_run_id=continuation.run_id,
        )
        if paused is not None:
            return paused
        if response.status != RunStatus.completed:
            raise RuntimeError(str(response.content or "Approval continuation did not complete"))
        for tool in response.tools or ():
            _, trace_entry = format_tool_completed_event(tool)
            if trace_entry is not None:
                tool_trace_collector.append(trace_entry)
        return str(response.content or "Tool approval continuation completed")

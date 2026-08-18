"""Agent reconstruction and execution for native approval continuations."""

from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, cast

from agno.db.base import SessionType
from agno.run.agent import (
    RunCompletedEvent,
    RunContentEvent,
    RunOutput,
    ToolCallCompletedEvent,
    ToolCallStartedEvent,
)
from agno.run.base import RunStatus

from mindroom import ai_runtime
from mindroom.agent_storage import create_session_storage
from mindroom.agents import create_agent
from mindroom.ai_run_metadata import build_ai_run_metadata_content
from mindroom.approval_receipt import install_approval_receipt_hooks
from mindroom.history.runtime import close_agent_runtime_state_dbs
from mindroom.matrix.typing import typing_indicator
from mindroom.response_turn import (
    CompletedApprovalRun,
    PausedAttempt,
    apply_exact_approval_decisions,
    paused_attempt_from_response,
)
from mindroom.tool_system.events import CollectedStreamPresentation, deserialize_tool_trace
from mindroom.tool_system.runtime_context import runtime_context_from_dispatch_context
from mindroom.tool_system.worker_routing import run_with_tool_execution_identity

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable

    import nio
    from agno.agent import Agent
    from agno.run.requirement import RunRequirement

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.event_journal import ApprovalContinuation
    from mindroom.knowledge import KnowledgeAccessSupport
    from mindroom.knowledge.refresh_scheduler import KnowledgeRefreshScheduler
    from mindroom.tool_system.events import ToolTraceEntry
    from mindroom.tool_system.runtime_context import ToolDispatchContext, ToolRuntimeSupport
    from mindroom.tool_system.worker_routing import ToolExecutionIdentity


async def _collect_agent_continuation(
    events: AsyncIterator[object],
    presentation: CollectedStreamPresentation,
) -> RunOutput:
    """Collect one ordered continuation stream and return its terminal run."""
    response: RunOutput | None = None
    async for event in events:
        if isinstance(event, RunOutput):
            response = event
        elif isinstance(event, RunContentEvent):
            presentation.append_text(event.content)
        elif isinstance(event, RunCompletedEvent) and event.content is not None:
            presentation.canonical_final_body_candidate = str(event.content)
        elif isinstance(event, ToolCallStartedEvent):
            presentation.start_tool(event.tool)
        elif isinstance(event, ToolCallCompletedEvent):
            presentation.complete_tool(event.tool)
    if response is None:
        msg = "Agent continuation did not yield its final run"
        raise RuntimeError(msg)
    for tool in response.tools or ():
        if not tool.is_paused:
            presentation.complete_tool(tool)
    return response


async def _continue_persisted_agent(
    agent: Agent,
    continuation: ApprovalContinuation,
    persisted: RunOutput,
    requirements: list[RunRequirement],
) -> tuple[RunOutput, CollectedStreamPresentation]:
    """Resume a persisted agent with event streaming so presentation order is retained."""
    events = agent.acontinue_run(
        run_id=continuation.run_id,
        requirements=requirements,
        session_id=continuation.session_id,
        user_id=continuation.requester_id,
        metadata=deepcopy(persisted.metadata),
        stream=True,
        stream_events=True,
        yield_run_output=True,
    )
    presentation = CollectedStreamPresentation(
        show_tool_calls=continuation.show_tool_calls,
        response_text=continuation.response_text,
        tool_trace=deserialize_tool_trace(continuation.response_tool_trace),
        track_hidden_tools=True,
    )
    response = await _collect_agent_continuation(cast("AsyncIterator[object]", events), presentation)
    return response, presentation


@dataclass(frozen=True)
class AgentApprovalExecution:
    """Rebuild and continue one persisted paused agent run."""

    config: Callable[[], Config]
    runtime_paths: RuntimePaths
    client: Callable[[], nio.AsyncClient]
    tool_runtime: ToolRuntimeSupport
    knowledge_access: KnowledgeAccessSupport
    refresh_scheduler: Callable[[], KnowledgeRefreshScheduler | None]

    async def continue_run(
        self,
        continuation: ApprovalContinuation,
        *,
        execution_identity: ToolExecutionIdentity,
        tool_dispatch: ToolDispatchContext,
        decisions: dict[str, bool],
        denial_reasons: dict[str, str | None],
        tool_trace_collector: list[ToolTraceEntry],
    ) -> CompletedApprovalRun | PausedAttempt:
        """Apply exact decisions and continue the matching persisted Agno run."""
        config = self.config()
        if continuation.entity_name not in config.agents:
            msg = f"Agent {continuation.entity_name!r} is no longer configured"
            raise RuntimeError(msg)
        knowledge = self.knowledge_access.for_agent(
            continuation.entity_name,
            execution_identity=execution_identity,
        )
        history_storage = await asyncio.to_thread(
            create_session_storage,
            continuation.entity_name,
            config,
            self.runtime_paths,
            execution_identity,
        )
        try:
            agent = await asyncio.to_thread(
                create_agent,
                continuation.entity_name,
                config,
                self.runtime_paths,
                execution_identity,
                session_id=continuation.session_id,
                history_storage=history_storage,
                active_model_name=continuation.runtime_model_name,
                knowledge=knowledge,
                refresh_scheduler=self.refresh_scheduler(),
                dynamic_tool_continuation=True,
                supports_native_tool_approval=True,
            )
        except BaseException:
            history_storage.close()
            raise
        try:
            if agent.model is not None:
                ai_runtime.install_queued_message_notice_hook(
                    agent.model,
                    notice_text=config.get_prompt("QUEUED_MESSAGE_NOTICE_TEXT"),
                )
                install_approval_receipt_hooks(agent.model, agent.fallback_config)
            session = await agent.aget_session(
                session_id=continuation.session_id,
                user_id=continuation.requester_id,
            )
            persisted = None if session is None else session.get_run(continuation.run_id)
            if not isinstance(persisted, RunOutput) or persisted.status != RunStatus.paused:
                msg = f"Paused run {continuation.run_id!r} is no longer available"
                raise RuntimeError(msg)
            requirements = apply_exact_approval_decisions(
                [deepcopy(requirement) for requirement in persisted.requirements or ()],
                decisions=decisions,
                denial_reasons=denial_reasons,
            )

            async with typing_indicator(self.client(), continuation.room_id):
                response, presentation = await self.tool_runtime.run_in_context(
                    tool_context=runtime_context_from_dispatch_context(tool_dispatch),
                    operation=lambda: run_with_tool_execution_identity(
                        tool_dispatch.execution_identity,
                        operation=lambda: _continue_persisted_agent(
                            agent,
                            continuation,
                            persisted,
                            requirements,
                        ),
                    ),
                )
        finally:
            try:
                ai_runtime.register_queued_notice_storage(
                    storage_factory=lambda: create_session_storage(
                        continuation.entity_name,
                        config,
                        self.runtime_paths,
                        execution_identity=execution_identity,
                    ),
                    session_id=continuation.session_id,
                    session_type=SessionType.AGENT,
                    entity_name=continuation.entity_name,
                )
            finally:
                try:
                    close_agent_runtime_state_dbs(agent, shared_scope_storage=history_storage)
                finally:
                    history_storage.close()
        paused = paused_attempt_from_response(
            response,
            fallback_session_id=continuation.session_id,
            fallback_run_id=continuation.run_id,
        )
        if paused is not None:
            return replace(
                paused,
                response_text=presentation.final_text(),
                tool_trace=tuple(presentation.tool_trace),
            )
        if response.status != RunStatus.completed:
            raise RuntimeError(str(response.content or "Approval continuation did not complete"))
        if continuation.show_tool_calls:
            tool_trace_collector.extend(presentation.tool_trace)
        model_name = continuation.runtime_model_name or config.resolve_entity(continuation.entity_name).model_name
        return CompletedApprovalRun(
            response_text=presentation.final_text() or str(response.content or "Tool approval continuation completed"),
            metadata_content=build_ai_run_metadata_content(
                config=config,
                model_name=model_name,
                run_id=response.run_id,
                session_id=response.session_id or continuation.session_id,
                status=response.status,
                model=response.model,
                model_provider=response.model_provider,
                metrics=response.metrics,
                tool_count=len(response.tools or ()),
            ),
        )

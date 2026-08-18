"""Agent reconstruction and execution for native approval continuations."""

from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, cast

from agno.db.base import SessionType
from agno.run.agent import RunOutput
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
    resolve_approval_response_content,
    response_content_text,
    stable_assistant_message_ids,
)
from mindroom.tool_system.events import (
    deserialize_tool_trace,
    format_assistant_tool_transcript,
    reconcile_tool_presentation,
)
from mindroom.tool_system.runtime_context import runtime_context_from_dispatch_context
from mindroom.tool_system.worker_routing import run_with_tool_execution_identity

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Sequence

    import nio
    from agno.models.response import ToolExecution

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.event_journal import ApprovalContinuation
    from mindroom.knowledge import KnowledgeAccessSupport
    from mindroom.knowledge.refresh_scheduler import KnowledgeRefreshScheduler
    from mindroom.tool_system.events import ToolTraceEntry
    from mindroom.tool_system.runtime_context import ToolDispatchContext, ToolRuntimeSupport
    from mindroom.tool_system.worker_routing import ToolExecutionIdentity


def _approval_response_presentation(
    response: RunOutput,
    paused: PausedAttempt | None,
    *,
    continuation: ApprovalContinuation,
    requirement_tools: Sequence[ToolExecution] = (),
    prior_message_ids: set[str] | frozenset[str] = frozenset(),
    show_tool_calls: bool,
) -> tuple[str, list[ToolTraceEntry], list[ToolExecution]]:
    """Reconcile one continued agent run with its durable visible snapshot."""
    presentation_tools = list(response.tools or ())
    seen_tool_call_ids = {tool.tool_call_id for tool in presentation_tools if tool.tool_call_id is not None}
    additional_tools = [*requirement_tools, *(paused.tools if paused is not None else ())]
    for tool in additional_tools:
        if tool.tool_call_id is not None and tool.tool_call_id in seen_tool_call_ids:
            continue
        presentation_tools.append(tool)
        if tool.tool_call_id is not None:
            seen_tool_call_ids.add(tool.tool_call_id)
    pending_tool_call_ids = (
        {tool.tool_call_id for tool in paused.tools if tool.tool_call_id} if paused is not None else set()
    )
    prior_tool_trace = deserialize_tool_trace(continuation.response_tool_trace)
    skipped_message_ids = prior_message_ids if continuation.response_text else frozenset()
    current_text, current_tool_trace = format_assistant_tool_transcript(
        response.messages or (),
        presentation_tools,
        pending_tool_call_ids=pending_tool_call_ids,
        start_index=len(prior_tool_trace) + 1,
        show_tool_calls=show_tool_calls,
        skip_message_ids=skipped_message_ids,
    )
    current_text = resolve_approval_response_content(
        response,
        current_text,
        terminal_content=response_content_text(response.content),
    )
    response_text, response_tool_trace = reconcile_tool_presentation(
        prior_text=continuation.response_text,
        prior_tool_trace=prior_tool_trace,
        current_text=current_text,
        current_tool_trace=current_tool_trace,
        tools=presentation_tools,
        pending_tool_call_ids=pending_tool_call_ids,
        show_tool_calls=show_tool_calls,
    )
    return response_text, response_tool_trace, presentation_tools


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
        show_tool_calls: bool = True,
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
            prior_message_ids = stable_assistant_message_ids(persisted.messages or ())
            requirements = apply_exact_approval_decisions(
                [deepcopy(requirement) for requirement in persisted.requirements or ()],
                decisions=decisions,
                denial_reasons=denial_reasons,
            )

            async def continue_run() -> RunOutput:
                result = agent.acontinue_run(
                    run_id=continuation.run_id,
                    requirements=requirements,
                    session_id=continuation.session_id,
                    user_id=continuation.requester_id,
                    metadata=deepcopy(persisted.metadata),
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
        response_text, response_tool_trace, presentation_tools = _approval_response_presentation(
            response,
            paused,
            continuation=continuation,
            requirement_tools=[
                requirement.tool_execution for requirement in requirements if requirement.tool_execution is not None
            ],
            prior_message_ids=prior_message_ids,
            show_tool_calls=show_tool_calls,
        )
        if paused is not None:
            return replace(
                paused,
                response_text=response_text,
                tool_trace=tuple(response_tool_trace),
            )
        if response.status != RunStatus.completed:
            raise RuntimeError(str(response.content or "Approval continuation did not complete"))
        tool_trace_collector.extend(response_tool_trace)
        model_name = continuation.runtime_model_name or config.resolve_entity(continuation.entity_name).model_name
        return CompletedApprovalRun(
            response_text=response_text or str(response.content or "Tool approval continuation completed"),
            metadata_content=build_ai_run_metadata_content(
                config=config,
                model_name=model_name,
                run_id=response.run_id,
                session_id=response.session_id or continuation.session_id,
                status=response.status,
                model=response.model,
                model_provider=response.model_provider,
                metrics=response.metrics,
                tool_count=len(presentation_tools),
            ),
        )

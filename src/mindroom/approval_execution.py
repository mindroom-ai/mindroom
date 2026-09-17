"""Agent reconstruction and execution for native approval continuations."""

from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import dataclass, replace
from functools import partial
from typing import TYPE_CHECKING, Any, cast
from uuid import uuid4

from agno.db.base import SessionType
from agno.run.agent import (
    RunCompletedEvent,
    RunContentEvent,
    RunErrorEvent,
    RunOutput,
    ToolCallCompletedEvent,
    ToolCallStartedEvent,
)
from agno.run.base import RunStatus
from agno.session.agent import AgentSession

from mindroom import ai_runtime
from mindroom.agent_storage import create_session_storage
from mindroom.agents import create_agent
from mindroom.ai import collect_streamed_response_content, run_delegated_child_response, stream_agent_response
from mindroom.ai_run_metadata import build_ai_run_metadata_content
from mindroom.approval_receipt import install_approval_receipt_hooks
from mindroom.approval_tools import (
    approval_denial_context,
    required_approval_tool_names,
    toolkit_owners_for_agents,
    validate_approval_tool_owners,
)
from mindroom.delegation.execution import drive_delegation_stream, has_delegation_state
from mindroom.delegation.state import DelegationState
from mindroom.error_handling import run_error_event_text
from mindroom.helper_usage import helper_usage_context
from mindroom.history.native import restore_native_history
from mindroom.history.session_context import ScopeSessionContext, close_agent_runtime_state_dbs, close_execution_storage
from mindroom.history.types import HistoryScope
from mindroom.matrix.typing import typing_indicator
from mindroom.response_turn import (
    CompletedApprovalRun,
    CompletedAttempt,
    PausedAttempt,
    ResponsePausedForApproval,
    ResponseTurnContext,
    ResumedAttempt,
    apply_local_approval_decisions,
    paused_attempt_from_response,
)
from mindroom.tool_jobs.consumption import finalize_consumption, set_consumption_storage
from mindroom.tool_jobs.execution_scope import owned_tool_execution
from mindroom.tool_jobs.settings import background_tool_jobs_enabled
from mindroom.tool_system.events import CollectedStreamPresentation, deserialize_tool_trace
from mindroom.tool_system.runtime_context import (
    ToolRuntimeModelBinding,
    runtime_context_from_dispatch_context,
)
from mindroom.tool_system.worker_routing import run_with_tool_execution_identity

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable, Mapping

    import nio
    from agno.agent import Agent
    from agno.knowledge.knowledge import Knowledge
    from agno.models.response import ToolExecution
    from agno.run.requirement import RunRequirement

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.event_journal import ApprovalContinuation
    from mindroom.knowledge.refresh_scheduler import KnowledgeRefreshScheduler
    from mindroom.knowledge.utils import KnowledgeAccessSupport
    from mindroom.tool_system.events import ToolTraceEntry
    from mindroom.tool_system.runtime_context import ToolDispatchContext, ToolRuntimeSupport
    from mindroom.tool_system.worker_routing import ToolExecutionIdentity


@dataclass(frozen=True)
class _CollectedAgentContinuation:
    """Provider facts awaiting settlement by the shared response lifecycle."""

    response: RunOutput
    tool_executions: tuple[ToolExecution, ...]
    terminal_content: str
    has_visible_content: bool


async def _collect_agent_continuation(  # noqa: C901
    events: AsyncIterator[object],
    presentation: CollectedStreamPresentation,
) -> _CollectedAgentContinuation:
    """Collect ordered events while leaving terminal fallback text for lifecycle settlement."""
    response: RunOutput | None = None
    error_event: RunErrorEvent | None = None
    terminal_content: str | None = None
    saw_content_delta = False
    current_tools: list[ToolExecution] = []
    async for event in events:
        if isinstance(event, RunOutput):
            response = event
        elif isinstance(event, RunErrorEvent):
            # Drain the producer so Agno can finish persistence and cleanup.
            error_event = event
        elif isinstance(event, RunContentEvent):
            presentation.append_text(event.content)
            saw_content_delta = saw_content_delta or bool(event.content)
        elif isinstance(event, RunCompletedEvent) and event.content is not None:
            terminal_content = str(event.content)
        elif isinstance(event, ToolCallStartedEvent):
            presentation.start_tool(event.tool)
        elif isinstance(event, ToolCallCompletedEvent):
            presentation.complete_tool(event.tool)
            if event.tool is not None and event.parent_run_id is None:
                current_tools.append(event.tool)
    if error_event is not None and (response is None or response.status == RunStatus.error):
        raise RuntimeError(run_error_event_text(error_event))
    if response is None:
        msg = "Agent continuation did not yield its final run"
        raise RuntimeError(msg)
    # Child approval results are also projected into the root presentation.
    # Only calls owned by the parent run can change its next tool schema.
    parent_call_ids = {tool.tool_call_id for tool in response.tools or ()}
    current_tools = [tool for tool in current_tools if tool.tool_call_id in parent_call_ids]
    _reconcile_agent_tools(presentation, response)
    return _CollectedAgentContinuation(
        response=response,
        tool_executions=tuple(current_tools),
        terminal_content=terminal_content if terminal_content and not saw_content_delta else "",
        has_visible_content=saw_content_delta,
    )


def _reconcile_agent_tools(presentation: CollectedStreamPresentation, response: RunOutput) -> None:
    """Retain exact delegated approval anchors alongside the parent's own tools."""
    paused = paused_attempt_from_response(
        response,
        fallback_session_id=response.session_id,
        fallback_run_id=response.run_id,
        toolkit_owners={},
    )
    if paused is not None:
        for tool in paused.tools:
            presentation.start_tool(tool)
    completed_call_ids = {
        entry.tool_call_id for entry in presentation.tool_tracker.completed_tools if entry.tool_call_id is not None
    }
    for tool in response.tools or ():
        if tool.is_paused:
            presentation.start_tool(tool)
        elif tool.tool_call_id is None or tool.tool_call_id not in completed_call_ids:
            # Repeated terminal completions must not match older public slots by name.
            presentation.complete_tool(tool)


async def _continue_persisted_agent(
    agent: Agent,
    continuation: ApprovalContinuation,
    persisted: RunOutput,
    requirements: list[RunRequirement],
    *,
    config: Config,
    runtime_paths: RuntimePaths,
    execution_identity: ToolExecutionIdentity,
    refresh_scheduler: KnowledgeRefreshScheduler | None,
    decisions: dict[str, bool],
    denial_reasons: dict[str, str | None],
    knowledge: Knowledge | None,
    tool_trace_collector: list[ToolTraceEntry],
    run_id_callback: Callable[[str], None] | None,
    tool_dispatch: ToolDispatchContext,
) -> CompletedApprovalRun | PausedAttempt:
    """Resume a persisted agent with event streaming so presentation order is retained."""

    async def persisted_event() -> AsyncIterator[RunOutput]:
        yield persisted

    delegated = has_delegation_state(persisted)
    native_events = (
        persisted_event()
        if delegated
        else agent.acontinue_run(
            run_id=continuation.run_id,
            requirements=requirements,
            session_id=continuation.session_id,
            user_id=continuation.requester_id,
            metadata=deepcopy(persisted.metadata),
            stream=True,
            stream_events=True,
            yield_run_output=True,
        )
    )
    events = drive_delegation_stream(
        agent,
        cast("AsyncIterator[object]", native_events),
        run_child=run_delegated_child_response,
        agent_name=continuation.entity_name,
        config=config,
        runtime_paths=runtime_paths,
        execution_identity=execution_identity,
        refresh_scheduler=refresh_scheduler,
        decisions=decisions if delegated else None,
        denial_reasons=denial_reasons if delegated else None,
        approval_calls=continuation.calls if delegated else (),
    )
    presentation = CollectedStreamPresentation(
        show_tool_calls=continuation.show_tool_calls,
        response_text=continuation.response_text,
        tool_trace=deserialize_tool_trace(continuation.response_tool_trace),
        track_hidden_tools=True,
    )
    collected = await _collect_agent_continuation(events, presentation)
    response = collected.response
    paused = paused_attempt_from_response(
        response,
        fallback_session_id=continuation.session_id,
        fallback_run_id=continuation.run_id,
        toolkit_owners=toolkit_owners_for_agents([agent]),
    )
    if paused is not None:
        return replace(
            paused,
            response_text=presentation.final_text(),
            tool_trace=tuple(presentation.tool_trace),
            continuation_count=continuation.continuation_count,
            runtime_model_name=continuation.runtime_model_name,
        )
    if response.status != RunStatus.completed:
        raise RuntimeError(str(response.content or "Approval continuation did not complete"))
    model_name = continuation.runtime_model_name or config.resolve_entity(continuation.entity_name).model_name
    attempt = CompletedAttempt(
        response_text=collected.terminal_content
        or (str(response.content or "Tool approval continuation completed") if not presentation.final_text() else ""),
        replayable_text=presentation.final_text(),
        has_visible_content=collected.has_visible_content,
        session_id=response.session_id or continuation.session_id,
        run_id=response.run_id,
        attempt_run_id=response.run_id or continuation.run_id,
        runtime_model_name=model_name,
        status=response.status,
        tool_executions=collected.tool_executions,
        completed_tools=tuple(presentation.tool_trace),
        metadata_content=build_ai_run_metadata_content(
            config=config,
            model_name=model_name,
            run_id=response.run_id,
            session_id=response.session_id or continuation.session_id,
            status=response.status,
            model=response.model,
            model_provider=response.model_provider,
            metrics=response.metrics,
            context_metrics=response.metrics,
            tool_count=len(response.tools or ()),
        ),
    )
    return await _settle_agent_continuation(
        continuation,
        presentation,
        resumed_attempt=ResumedAttempt(attempt, continuation_count=continuation.continuation_count),
        model_name=model_name,
        metadata=response.metadata,
        config=config,
        runtime_paths=runtime_paths,
        execution_identity=execution_identity,
        knowledge=knowledge,
        refresh_scheduler=refresh_scheduler,
        tool_trace_collector=tool_trace_collector,
        run_id_callback=run_id_callback,
        tool_dispatch=tool_dispatch,
    )


async def _settle_agent_continuation(
    continuation: ApprovalContinuation,
    presentation: CollectedStreamPresentation,
    *,
    resumed_attempt: ResumedAttempt,
    model_name: str | None,
    metadata: dict[str, Any] | None,
    config: Config,
    runtime_paths: RuntimePaths,
    execution_identity: ToolExecutionIdentity,
    knowledge: Knowledge | None,
    refresh_scheduler: KnowledgeRefreshScheduler | None,
    tool_trace_collector: list[ToolTraceEntry],
    run_id_callback: Callable[[str], None] | None,
    tool_dispatch: ToolDispatchContext,
) -> CompletedApprovalRun | PausedAttempt:
    """Settle resumed work and any fresh attempts through the shared response driver."""
    ctx = ResponseTurnContext(
        entity_label=continuation.entity_name,
        session_id=continuation.session_id,
        run_id=continuation.run_id,
        correlation_id=continuation.correlation_id or uuid4().hex,
        reply_to_event_id=continuation.sources.logical_source_event_ids[-1],
        room_id=continuation.room_id,
        thread_id=continuation.thread_id,
        requester_id=continuation.requester_id,
        matrix_run_metadata=deepcopy(metadata),
        active_model_name=model_name,
        active_event_ids=frozenset(continuation.source_event_ids),
    )
    tool_context = runtime_context_from_dispatch_context(tool_dispatch)
    run_metadata: dict[str, Any] = {}
    completed: list[CompletedAttempt] = []
    stream = stream_agent_response(
        ctx,
        prompt=continuation.request_body,
        runtime_paths=runtime_paths,
        config=config,
        knowledge=knowledge,
        execution_identity=execution_identity,
        refresh_scheduler=refresh_scheduler,
        run_id_callback=run_id_callback,
        run_metadata_collector=run_metadata,
        show_tool_calls=continuation.show_tool_calls,
        tool_function_filter=tool_context.tool_function_filter if tool_context is not None else None,
        supports_native_tool_approval=True,
        attempt_model_runtime=ToolRuntimeModelBinding(),
        resumed_attempt=resumed_attempt,
        on_completed=completed.append,
    )
    try:
        response_text, tool_trace = await collect_streamed_response_content(
            stream,
            presentation=presentation,
        )
    except ResponsePausedForApproval as error:
        if error.presentation is None:
            raise
        return replace(
            error.paused,
            response_text=error.presentation.response_text,
            tool_trace=error.presentation.tool_trace,
        )
    if not completed or completed[-1].status != RunStatus.completed:
        raise RuntimeError(response_text or "Approval continuation did not complete")
    if continuation.show_tool_calls:
        tool_trace_collector.extend(tool_trace)
    return CompletedApprovalRun(response_text=response_text, metadata_content=run_metadata)


@dataclass(frozen=True)
class AgentApprovalExecution:
    """Rebuild and continue one persisted paused agent run."""

    config: Callable[[], Config]
    runtime_paths: RuntimePaths
    client: Callable[[], nio.AsyncClient]
    tool_runtime: ToolRuntimeSupport
    knowledge_access: KnowledgeAccessSupport
    refresh_scheduler: Callable[[], KnowledgeRefreshScheduler | None]

    @partial(
        owned_tool_execution,
        enabled=lambda self, *_args, **_kwargs: background_tool_jobs_enabled(self.config(), self.runtime_paths),
    )
    async def continue_run(
        self,
        continuation: ApprovalContinuation,
        *,
        execution_identity: ToolExecutionIdentity,
        tool_dispatch: ToolDispatchContext,
        decisions: dict[str, bool],
        denial_reasons: dict[str, str | None],
        tool_trace_collector: list[ToolTraceEntry],
        typing_log_context: Mapping[str, object],
        run_id_callback: Callable[[str], None] | None = None,
    ) -> CompletedApprovalRun | PausedAttempt:
        """Apply exact decisions and continue the matching persisted Agno run."""
        config = self.config()
        if continuation.entity_name not in config.agents:
            msg = f"Agent {continuation.entity_name!r} is no longer configured"
            raise RuntimeError(msg)
        knowledge = (
            await self.knowledge_access.resolve_for_agent_async(
                continuation.entity_name,
                execution_identity=execution_identity,
            )
        ).knowledge
        storage_factory = partial(
            create_session_storage,
            continuation.entity_name,
            config,
            self.runtime_paths,
            execution_identity,
        )
        history_storage = await asyncio.to_thread(storage_factory)
        set_consumption_storage(storage_factory)
        try:
            session = await asyncio.to_thread(
                history_storage.get_session,
                session_id=continuation.session_id,
                session_type=SessionType.AGENT,
                user_id=continuation.requester_id,
            )
            persisted = session.get_run(continuation.run_id) if isinstance(session, AgentSession) else None
            if not isinstance(persisted, RunOutput) or persisted.status != RunStatus.paused:
                msg = f"Paused run {continuation.run_id!r} is no longer available"
                raise RuntimeError(msg)  # noqa: TRY301 - the preparation guard owns storage cleanup
            delegation = DelegationState.from_metadata(persisted.metadata)
            local_calls = () if delegation.pending_child_id is not None else continuation.calls
            approved_calls = tuple(call for call in local_calls if decisions.get(call.tool_call_id))
            required_tool_names = await required_approval_tool_names(
                continuation.entity_name,
                approved_calls,
                config=config,
                runtime_paths=self.runtime_paths,
                execution_identity=execution_identity,
            )
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
                required_tool_names=required_tool_names,
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
            restore_native_history(agent.model, persisted_run=persisted, session=cast("AgentSession", session))
            requirements = apply_local_approval_decisions(
                persisted,
                decisions=decisions,
                denial_reasons=denial_reasons,
            )
            validate_approval_tool_owners([agent], approved_calls, requirements)

            scope_context = ScopeSessionContext(
                scope=HistoryScope(kind="agent", scope_id=continuation.entity_name),
                storage=history_storage,
                session=cast("AgentSession", session),
                session_id=continuation.session_id,
                storage_factory=storage_factory,
            )
            with (
                helper_usage_context(scope_context),
                approval_denial_context(
                    agent,
                    {
                        continuation.run_id: tuple(
                            call for call in local_calls if not decisions.get(call.tool_call_id)
                        ),
                    },
                ),
            ):
                async with typing_indicator(
                    self.client(),
                    continuation.room_id,
                    log_context=typing_log_context,
                ):
                    result = await self.tool_runtime.run_in_context(
                        tool_context=runtime_context_from_dispatch_context(tool_dispatch),
                        operation=lambda: run_with_tool_execution_identity(
                            tool_dispatch.execution_identity,
                            operation=lambda: _continue_persisted_agent(
                                agent,
                                continuation,
                                persisted,
                                requirements,
                                config=config,
                                runtime_paths=self.runtime_paths,
                                execution_identity=execution_identity,
                                refresh_scheduler=self.refresh_scheduler(),
                                decisions=decisions,
                                denial_reasons=denial_reasons,
                                knowledge=knowledge,
                                tool_trace_collector=tool_trace_collector,
                                run_id_callback=run_id_callback,
                                tool_dispatch=tool_dispatch,
                            ),
                        ),
                    )
        finally:
            try:
                await finalize_consumption()
            finally:
                try:
                    ai_runtime.register_queued_notice_storage(
                        storage_factory=storage_factory,
                        session_id=continuation.session_id,
                        session_type=SessionType.AGENT,
                        entity_name=continuation.entity_name,
                    )
                finally:
                    try:
                        close_agent_runtime_state_dbs(agent, shared_scope_storage=history_storage)
                    finally:
                        close_execution_storage(history_storage)
        return result

"""Agent reconstruction and execution for native approval continuations."""

from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import dataclass, replace
from functools import partial
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from mindroom import ai_runtime
from mindroom.agent_cli.approval import CliApprovalCall
from mindroom.agent_cli.cli_approval_recovery import CliFollowUpTurn, continue_cli_approval
from mindroom.agent_loop.build import tool_owners_by_agent
from mindroom.agents import create_agent
from mindroom.ai import (
    agent_run_metadata_content,
    collect_streamed_response_content,
    run_delegated_child_response,
    stream_agent_response,
)
from mindroom.approval.receipt_readers import add_approval_receipt_reader
from mindroom.approval.resume import ResumeRunner, approval_decisions
from mindroom.approval.tool_owners import (
    authorize_prepared_tool_call,
    required_approval_tool_names,
    validate_approval_tool_owners,
)
from mindroom.delegation.execution import answered_delegation_calls, drive_delegation_stream, has_delegation_state
from mindroom.delegation.state import DelegationState
from mindroom.helper_usage import helper_usage_context
from mindroom.history.session_context import ScopeSessionContext
from mindroom.history.types import HistoryScope
from mindroom.llm.events import ContentDelta, RunFinished, ToolCompleted, ToolStarted
from mindroom.llm.runs import RunRecord, RunStatus
from mindroom.matrix.typing import typing_indicator
from mindroom.response_turn import (
    CompletedApprovalRun,
    CompletedAttempt,
    PausedAnswer,
    PausedAttempt,
    ResponsePausedForApproval,
    ResponseTurnContext,
    ResumedAttempt,
    local_approval_requirements,
    paused_attempt_from_response,
)
from mindroom.session_storage.agent_storage import create_session_storage
from mindroom.tool_jobs.consumption import finalize_consumption, set_consumption_storage
from mindroom.tool_jobs.execution_scope import owned_tool_execution
from mindroom.tool_jobs.settings import background_tool_jobs_enabled
from mindroom.tool_system.context_bound_streams import closing_async_stream
from mindroom.tool_system.events import CollectedStreamPresentation
from mindroom.tool_system.runtime_context import runtime_context_from_dispatch_context
from mindroom.tool_system.worker_routing import run_with_tool_execution_identity

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable, Mapping

    import nio

    from mindroom.agent_loop.build import AgentBuild
    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.event_journal import ApprovalContinuation
    from mindroom.knowledge.refresh_scheduler import KnowledgeRefreshScheduler
    from mindroom.knowledge.search import AgentKnowledge
    from mindroom.knowledge.utils import KnowledgeAccessSupport
    from mindroom.llm.events import RunEvent
    from mindroom.llm.runs import ToolExecution
    from mindroom.media_inputs import MediaInputs
    from mindroom.session_storage.store import ConversationStore
    from mindroom.session_storage.types import ConversationSession
    from mindroom.streaming import ProgressPublisher
    from mindroom.tool_system.events import ToolTraceEntry
    from mindroom.tool_system.runtime_context import ToolDispatchContext, ToolRuntimeSupport
    from mindroom.tool_system.worker_routing import ToolExecutionIdentity


@dataclass(frozen=True)
class _CollectedAgentContinuation:
    """Provider facts awaiting settlement by the shared response lifecycle."""

    response: RunRecord
    tool_executions: tuple[ToolExecution, ...]
    terminal_content: str
    has_visible_content: bool


async def _collect_agent_continuation(
    events: AsyncIterator[RunEvent],
    presentation: CollectedStreamPresentation,
    *,
    progress: ProgressPublisher | None,
) -> _CollectedAgentContinuation:
    """Collect ordered events while leaving terminal fallback text for lifecycle settlement.

    A failure of the resumed run propagates as its exception once the stream has reported it, and a failure while
    the events are presented closes the stream before it propagates.
    """
    response: RunRecord | None = None
    saw_content_delta = False
    current_tools: list[ToolExecution] = []
    await presentation.publish(progress)
    async with closing_async_stream(events):
        async for event in events:
            if isinstance(event, RunFinished):
                assert isinstance(event.record, RunRecord)
                response = event.record
            elif isinstance(event, ContentDelta):
                presentation.append_text(event.text)
                saw_content_delta = saw_content_delta or bool(event.text)
                await presentation.publish(progress)
            elif isinstance(event, ToolStarted):
                presentation.start_tool(event.tool)
                await presentation.publish(progress)
            elif isinstance(event, ToolCompleted):
                presentation.complete_tool(event.tool)
                current_tools.append(event.tool)
                await presentation.publish(progress)
    if response is None:
        msg = "Agent continuation did not yield its final run"
        raise RuntimeError(msg)
    # Child approval results are also projected into the root presentation.
    # Only calls owned by the parent run can continue its turn.
    parent_call_ids = {tool.tool_call_id for tool in response.tools or ()}
    current_tools = [tool for tool in current_tools if tool.tool_call_id in parent_call_ids]
    _reconcile_agent_tools(presentation, response)
    # A run that completed without streaming its text hands the text over for the lifecycle to append, apart from
    # what it carried over from the pause, which the reply it continues already accounts for.
    hands_over_text = response.status is RunStatus.completed and not saw_content_delta
    return _CollectedAgentContinuation(
        response=response,
        tool_executions=tuple(current_tools),
        terminal_content=str(response.new_handover_content or "") if hands_over_text else "",
        has_visible_content=saw_content_delta,
    )


def _reconcile_agent_tools(presentation: CollectedStreamPresentation, response: RunRecord) -> None:
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
    # A delegation answered on an earlier entry no longer waits, though its stored call still names the pause.
    answered = answered_delegation_calls(response)
    for tool in response.tools or ():
        # An agent's own calls name no member.
        if tool.is_paused and (None, tool.tool_call_id) not in answered:
            presentation.start_tool(tool)
        elif tool.tool_call_id is None or tool.tool_call_id not in completed_call_ids:
            # Repeated terminal completions must not match older public slots by name.
            presentation.complete_tool(tool)


async def _persisted_events(persisted: RunRecord) -> AsyncIterator[RunEvent]:
    yield RunFinished(persisted)


async def _continue_persisted_agent(
    build: AgentBuild,
    continuation: ApprovalContinuation,
    persisted: RunRecord,
    *,
    store: ConversationStore,
    session: ConversationSession,
    paused_answer: PausedAnswer,
    config: Config,
    runtime_paths: RuntimePaths,
    execution_identity: ToolExecutionIdentity,
    refresh_scheduler: KnowledgeRefreshScheduler | None,
    decisions: dict[str, bool],
    denial_reasons: dict[str, str | None],
    knowledge: AgentKnowledge | None,
    tool_trace_collector: list[ToolTraceEntry],
    run_id_callback: Callable[[str, str], None] | None,
    tool_dispatch: ToolDispatchContext,
    progress: ProgressPublisher | None,
) -> CompletedApprovalRun | PausedAttempt:
    """Resume a persisted agent with event streaming so presentation order is retained.

    A run the delegation driver owns resumes through it with the decisions on its children's calls; any other run
    resumes with the decisions on its own calls, and the driver continues it if it pauses on a delegation.
    """
    model_name = continuation.runtime_model_name or config.resolve_entity(continuation.entity_name).model_name
    runner = ResumeRunner(
        build=build,
        config=config,
        runtime_paths=runtime_paths,
        agent_name=continuation.entity_name,
        model_name=model_name,
        store=store,
        session=session,
        scope=HistoryScope(kind="agent", scope_id=continuation.entity_name),
        user_id=continuation.requester_id,
    )
    delegated = has_delegation_state(persisted)
    native_events = (
        _persisted_events(persisted)
        if delegated
        else runner.events(persisted, approval_decisions(continuation.calls, decisions, denial_reasons))
    )
    events = drive_delegation_stream(
        native_events,
        store=store,
        session=session,
        continue_parent=runner.continue_run,
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
        response_text=paused_answer.text,
        tool_trace=list(paused_answer.tool_trace),
        track_hidden_tools=True,
    )
    collected = await _collect_agent_continuation(events, presentation, progress=progress)
    response = collected.response
    paused = paused_attempt_from_response(
        response,
        fallback_session_id=continuation.session_id,
        fallback_run_id=continuation.run_id,
        toolkit_owners=tool_owners_by_agent(build),
    )
    if paused is not None:
        return replace(
            paused,
            response_text=presentation.final_text(),
            tool_trace=tuple(presentation.tool_trace),
            continuation_count=continuation.continuation_count,
            runtime_model_name=continuation.runtime_model_name,
        )
    if response.status is not RunStatus.completed:
        raise RuntimeError(str(response.content or "Approval continuation did not complete"))
    attempt = CompletedAttempt(
        response_text=collected.terminal_content
        or ("" if presentation.final_text() else "Tool approval continuation completed"),
        replayable_text=presentation.final_text(),
        has_visible_content=collected.has_visible_content,
        session_id=response.session_id or continuation.session_id,
        run_id=response.run_id,
        attempt_run_id=response.run_id or continuation.run_id,
        runtime_model_name=model_name,
        status=response.status,
        tool_executions=collected.tool_executions,
        completed_tools=tuple(presentation.tool_trace),
        metadata_content=agent_run_metadata_content(
            config,
            response,
            runtime_paths=runtime_paths,
            model_name=model_name,
            run_id=response.run_id,
            session_id=continuation.session_id,
            status=response.status,
            # An approval resume prepares no history of its own.
            prepared_history=None,
        ),
    )
    return await _stream_continuation_turn(
        _continuation_turn_context(continuation, model_name=model_name, metadata=response.metadata),
        presentation,
        prompt=continuation.request_body,
        show_tool_calls=continuation.show_tool_calls,
        resumed_attempt=ResumedAttempt(attempt, continuation_count=continuation.continuation_count),
        config=config,
        runtime_paths=runtime_paths,
        execution_identity=execution_identity,
        knowledge=knowledge,
        refresh_scheduler=refresh_scheduler,
        tool_trace_collector=tool_trace_collector,
        run_id_callback=run_id_callback,
        tool_dispatch=tool_dispatch,
        progress=progress,
    )


def _continuation_turn_context(
    continuation: ApprovalContinuation,
    *,
    model_name: str | None,
    metadata: dict[str, Any] | None,
) -> ResponseTurnContext:
    """Identify the resumed standard turn by the original response's sources."""
    return ResponseTurnContext(
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


async def _stream_continuation_turn(
    ctx: ResponseTurnContext,
    presentation: CollectedStreamPresentation,
    *,
    prompt: str,
    show_tool_calls: bool,
    config: Config,
    runtime_paths: RuntimePaths,
    execution_identity: ToolExecutionIdentity,
    knowledge: AgentKnowledge | None,
    refresh_scheduler: KnowledgeRefreshScheduler | None,
    tool_trace_collector: list[ToolTraceEntry],
    run_id_callback: Callable[[str, str], None] | None,
    tool_dispatch: ToolDispatchContext,
    progress: ProgressPublisher | None,
    resumed_attempt: ResumedAttempt | None = None,
    reusable_build: AgentBuild | None = None,
    initial_continuation_count: int = 0,
    delegation_depth: int = 0,
    media: MediaInputs | None = None,
) -> CompletedApprovalRun | PausedAttempt:
    """Stream resumed work and any fresh attempts into the continued reply through the shared response driver.

    A recovered CLI call runs without a model request, so its follow-up, the turn with no ``resumed_attempt``, reads
    the approval receipt in its first attempt's run; a resumed run already read it.
    """
    if resumed_attempt is None:
        run_id = str(uuid4())
        ctx = replace(ctx, run_id=run_id)
        add_approval_receipt_reader(run_id)
    tool_context = runtime_context_from_dispatch_context(tool_dispatch)
    run_metadata: dict[str, Any] = {}
    completed: list[CompletedAttempt] = []
    stream = stream_agent_response(
        ctx,
        prompt=prompt,
        runtime_paths=runtime_paths,
        config=config,
        knowledge=knowledge,
        execution_identity=execution_identity,
        refresh_scheduler=refresh_scheduler,
        run_id_callback=run_id_callback,
        run_metadata_collector=run_metadata,
        show_tool_calls=show_tool_calls,
        tool_function_filter=tool_context.tool_function_filter if tool_context is not None else None,
        supports_native_tool_approval=True,
        reusable_build=reusable_build,
        resumed_attempt=resumed_attempt,
        initial_continuation_count=initial_continuation_count,
        delegation_depth=delegation_depth,
        media=media,
        on_completed=completed.append,
    )
    try:
        response_text, tool_trace = await collect_streamed_response_content(
            stream,
            presentation=presentation,
            on_update=partial(presentation.publish, progress),
        )
    except ResponsePausedForApproval as error:
        if error.presentation is None:
            raise
        return replace(
            error.paused,
            response_text=error.presentation.response_text,
            tool_trace=error.presentation.tool_trace,
        )
    if not completed or completed[-1].status is not RunStatus.completed:
        raise RuntimeError(response_text or "Approval continuation did not complete")
    if show_tool_calls:
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

    @owned_tool_execution(
        enabled=lambda self, *_args, **_kwargs: background_tool_jobs_enabled(self.config(), self.runtime_paths),
    )
    async def continue_run(
        self,
        continuation: ApprovalContinuation,
        *,
        paused_answer: PausedAnswer,
        execution_identity: ToolExecutionIdentity,
        tool_dispatch: ToolDispatchContext,
        decisions: dict[str, bool],
        denial_reasons: dict[str, str | None],
        tool_trace_collector: list[ToolTraceEntry],
        typing_log_context: Mapping[str, object],
        progress: ProgressPublisher | None,
        run_id_callback: Callable[[str, str], None] | None = None,
    ) -> CompletedApprovalRun | PausedAttempt:
        """Apply exact decisions and continue the matching persisted paused run.

        ``progress``, when given, shows the resumed presentation live in the
        reply being continued; the terminal delivery stays with the caller.
        """
        config = self.config()
        cli_call = CliApprovalCall.from_dict(continuation.cli_call) if continuation.cli_call is not None else None
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
        store = await asyncio.to_thread(storage_factory)
        set_consumption_storage(storage_factory)
        session = await asyncio.to_thread(store.load_session, continuation.session_id, "agent")
        persisted = await asyncio.to_thread(store.get_run, continuation.run_id)
        if (
            session is None
            or not isinstance(persisted, RunRecord)
            or persisted.session_id != continuation.session_id
            or (cli_call is None and persisted.status is not RunStatus.paused)
        ):
            msg = f"Paused run {continuation.run_id!r} is no longer available"
            raise RuntimeError(msg)
        delegation = DelegationState.from_metadata(persisted.metadata)
        local_calls = () if delegation.pending_child_id is not None else continuation.calls
        approved_calls = tuple(call for call in local_calls if decisions.get(call.tool_call_id))
        # Generated CLI functions have no configured toolkit to restore.
        # Their exact current binding is checked by continue_cli_approval
        # and authorize_prepared_tool_call after this agent is rebuilt.
        toolkit_calls = tuple(
            call
            for call in approved_calls
            if not (
                cli_call is not None
                and call.invoking_agent == continuation.entity_name
                and call.toolkit_name == "agent"
            )
        )
        required_tool_names = await required_approval_tool_names(
            continuation.entity_name,
            toolkit_calls,
            config=config,
            runtime_paths=self.runtime_paths,
            execution_identity=execution_identity,
        )
        build = await asyncio.to_thread(
            create_agent,
            continuation.entity_name,
            config,
            self.runtime_paths,
            execution_identity,
            history_storage=store,
            active_model_name=continuation.runtime_model_name,
            knowledge=knowledge,
            refresh_scheduler=self.refresh_scheduler(),
            supports_native_tool_approval=True,
            required_tool_names=required_tool_names,
            agent_mode=(
                "minimal"
                if cli_call is not None or (persisted.metadata or {}).get("agent_mode") == "minimal"
                else "standard"
            ),
            delegation_depth=cli_call.delegation_depth if cli_call is not None else 0,
        )
        scope_context = ScopeSessionContext(
            scope=HistoryScope(kind="agent", scope_id=continuation.entity_name),
            storage=store,
            session=session,
            session_id=continuation.session_id,
            storage_factory=storage_factory,
        )
        try:
            async with typing_indicator(
                self.client(),
                continuation.room_id,
                log_context=typing_log_context,
            ):
                with helper_usage_context(scope_context):
                    runtime_context = runtime_context_from_dispatch_context(tool_dispatch)
                    operation: Callable[[], Awaitable[CompletedApprovalRun | PausedAttempt]]
                    if cli_call is not None:
                        if runtime_context is None:
                            msg = "CLI approval recovery requires its authenticated runtime context"
                            raise RuntimeError(msg)
                        expected_worker_target = runtime_context.resolve_worker_target()

                        async def recover_cli_call() -> CompletedApprovalRun | PausedAttempt:
                            recovered = await continue_cli_approval(
                                build,
                                continuation,
                                cli_call,
                                persisted,
                                session,
                                store,
                                paused_answer=paused_answer,
                                runtime_context=runtime_context,
                                decisions=decisions,
                                denial_reasons=denial_reasons,
                                tool_trace_collector=tool_trace_collector,
                                refresh_scheduler=self.refresh_scheduler(),
                                authorize=lambda binding: authorize_prepared_tool_call(
                                    binding,
                                    expected_worker_target=expected_worker_target,
                                ),
                                progress=progress,
                            )
                            if not isinstance(recovered, CliFollowUpTurn):
                                return recovered
                            return await _stream_continuation_turn(
                                recovered.ctx,
                                recovered.presentation,
                                prompt=recovered.prompt,
                                show_tool_calls=continuation.show_tool_calls,
                                reusable_build=recovered.reusable_build,
                                initial_continuation_count=recovered.initial_continuation_count,
                                delegation_depth=recovered.delegation_depth,
                                media=recovered.media,
                                config=config,
                                runtime_paths=self.runtime_paths,
                                execution_identity=execution_identity,
                                knowledge=knowledge,
                                refresh_scheduler=self.refresh_scheduler(),
                                tool_trace_collector=tool_trace_collector,
                                run_id_callback=run_id_callback,
                                tool_dispatch=tool_dispatch,
                                progress=progress,
                            )

                        operation = recover_cli_call
                    else:
                        # The resume itself refuses decisions that do not cover the waiting calls exactly; this binds
                        # each approval to the toolkit it was granted for before the agent's tools are reached.
                        validate_approval_tool_owners(
                            tool_owners_by_agent(build),
                            approved_calls,
                            local_approval_requirements(persisted),
                        )
                        operation = partial(
                            _continue_persisted_agent,
                            build,
                            continuation,
                            persisted,
                            store=store,
                            session=session,
                            paused_answer=paused_answer,
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
                            progress=progress,
                        )
                    result = await self.tool_runtime.run_in_context(
                        tool_context=runtime_context,
                        operation=lambda: run_with_tool_execution_identity(
                            tool_dispatch.execution_identity,
                            operation=operation,
                        ),
                    )
        finally:
            try:
                await finalize_consumption()
            finally:
                ai_runtime.register_scope_notice_storage(
                    scope_context=scope_context,
                    entity_name=continuation.entity_name,
                )
        return result

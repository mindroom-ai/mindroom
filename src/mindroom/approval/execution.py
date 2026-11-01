"""Agent reconstruction and execution for native approval continuations."""

from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import dataclass, replace
from functools import partial
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from mindroom.agent_cli.approval import CliApprovalCall
from mindroom.agent_cli.cli_approval_recovery import CliFollowUpTurn, continue_cli_approval
from mindroom.agent_loop.build import tool_owners_by_agent
from mindroom.agents import create_agent
from mindroom.ai import agent_run_metadata_content, run_delegated_child_response, stream_agent_response
from mindroom.ai_run_metadata import prior_usage_of
from mindroom.approval.receipt_readers import add_approval_receipt_reader
from mindroom.approval.resume import ResumeRunner
from mindroom.approval.tool_owners import (
    authorize_prepared_tool_call,
    required_approval_tool_names,
    validate_approval_tool_owners,
)
from mindroom.delegation.execution import answered_delegation_calls, drive_delegation_stream, resume_from_approval
from mindroom.helper_usage import helper_usage_context
from mindroom.history.session_context import ScopeSessionContext
from mindroom.history.types import HistoryScope
from mindroom.llm.events import TextDelta, ToolCompleted, ToolProgress, ToolStarted
from mindroom.llm.runs import RunRecord, RunStatus
from mindroom.matrix.typing import typing_indicator
from mindroom.response_turn import (
    CompletedApprovalRun,
    CompletedAttempt,
    PausedAnswer,
    PausedAttempt,
    ResponsePausedForApproval,
    ResponseTurnContext,
    TurnResume,
    TurnSinks,
    collect_streamed_response_content,
    own_approved_calls,
    pause_without_approval_error,
    paused_attempt_from_response,
    pending_approval_requirements,
)
from mindroom.run_storage import TerminalRecord, scope_run_storage
from mindroom.session_storage.agent_storage import create_session_storage
from mindroom.session_storage.lanes import run_session_storage_operation
from mindroom.tool_jobs.consumption import finalize_consumption, set_consumption_storage
from mindroom.tool_jobs.execution_scope import owned_tool_execution
from mindroom.tool_jobs.settings import background_tool_jobs_enabled
from mindroom.tool_system.context_bound_streams import closing_async_stream
from mindroom.tool_system.events import CollectedStreamPresentation, with_stored_calls
from mindroom.tool_system.runtime_context import tool_runtime_context
from mindroom.turn_model import ModelScope, resolve_turn_model

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
    from mindroom.run_storage import RunStorage
    from mindroom.streaming import ProgressPublisher
    from mindroom.tool_system.events import ToolTraceEntry
    from mindroom.tool_system.runtime_context import ToolRuntimeContext
    from mindroom.turn_model import TurnModel


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
    run_id: str,
    progress: ProgressPublisher | None,
) -> _CollectedAgentContinuation:
    """Collect ordered events of run ``run_id`` while leaving terminal fallback text for lifecycle settlement.

    A failure of the resumed run propagates as its exception once the stream has reported it, and a failure while
    the events are presented closes the stream before it propagates.
    """
    finished = TerminalRecord(RunRecord)
    saw_content_delta = False
    completed_ids: set[str] = set()
    await presentation.publish(progress)
    async with closing_async_stream(events):
        async for event in events:
            if finished.collect(event):
                continue
            if isinstance(event, TextDelta):
                presentation.append_text(event.text)
                saw_content_delta = saw_content_delta or bool(event.text)
                await presentation.publish(progress)
            elif isinstance(event, ToolStarted):
                presentation.start_tool(event.call)
                await presentation.publish(progress)
            elif isinstance(event, ToolCompleted):
                presentation.complete_tool(event.call)
                completed_ids.add(event.call.tool_call_id)
                await presentation.publish(progress)
            elif isinstance(event, ToolProgress):
                presentation.show_tool_progress(event.tool_call_id, event.text)
                await presentation.publish(progress)
    response = finished.terminal(run_id)
    # The run's own calls this continuation completed; child approval results are projected into the root
    # presentation too, but only the parent run's calls can continue its turn.
    completed = tuple(tool for tool in response.tools or () if tool.tool_call_id in completed_ids)
    _reconcile_agent_tools(presentation, response)
    # A run that completed without streaming its text hands the text it wrote since the resume over for the lifecycle
    # to append; the reply it continues already shows what it wrote before the pause.
    hands_over_text = response.status is RunStatus.completed and not saw_content_delta
    return _CollectedAgentContinuation(
        response=response,
        tool_executions=completed,
        terminal_content=(response.new_handover_content or "") if hands_over_text else "",
        has_visible_content=saw_content_delta,
    )


def _reconcile_agent_tools(presentation: CollectedStreamPresentation, response: RunRecord) -> None:
    """Retain exact delegated approval anchors alongside the parent's own tools."""
    paused = paused_attempt_from_response(
        response,
        toolkit_owners={},
    )
    if paused is not None:
        for tool in paused.tools:
            presentation.start_tool(tool.call)
    completed_call_ids = {
        entry.tool_call_id for entry in presentation.tool_tracker.completed_tools if entry.tool_call_id is not None
    }
    # A delegation answered on an earlier entry no longer waits, though its stored call still names the pause.
    answered = answered_delegation_calls(response)
    for tool in response.tools or ():
        # An agent's own calls name no member.
        if tool.is_paused and (None, tool.tool_call_id) not in answered:
            presentation.start_tool(tool.call)
        elif tool.tool_call_id not in completed_call_ids:
            # Repeated terminal completions must not match older public slots by name.
            presentation.complete_tool(tool.call)


def _continuation_model(config: Config, runtime_paths: RuntimePaths, continuation: ApprovalContinuation) -> TurnModel:
    """Return the model a paused agent run resumes on: the one it ran, else the one its room and thread choose now."""
    return resolve_turn_model(
        config,
        runtime_paths,
        ModelScope(
            entity=continuation.entity_name,
            room_id=continuation.room_id,
            thread_id=continuation.thread_id,
            explicit=continuation.runtime_model_name,
        ),
        None,
    )


async def _continue_persisted_agent(
    build: AgentBuild,
    continuation: ApprovalContinuation,
    persisted: RunRecord,
    *,
    storage: RunStorage,
    paused_answer: PausedAnswer,
    config: Config,
    runtime_paths: RuntimePaths,
    refresh_scheduler: KnowledgeRefreshScheduler | None,
    knowledge: AgentKnowledge | None,
    tool_trace_collector: list[ToolTraceEntry],
    tool_context: ToolRuntimeContext,
    progress: ProgressPublisher | None,
    model_name: str,
) -> CompletedApprovalRun | PausedAttempt:
    """Resume a persisted agent with event streaming so presentation order is retained."""
    runner = ResumeRunner(
        build=build,
        config=config,
        runtime_paths=runtime_paths,
        model_name=model_name,
        storage=storage,
        user_id=continuation.requester_id,
    )
    native_events, driver_calls = resume_from_approval(persisted, continuation.calls, runner)
    events = drive_delegation_stream(
        native_events,
        record_type=RunRecord,
        continue_parent=runner,
        run_child=run_delegated_child_response,
        agent_name=continuation.entity_name,
        config=config,
        runtime_paths=runtime_paths,
        execution_identity=tool_context.execution_identity,
        refresh_scheduler=refresh_scheduler,
        approval_calls=driver_calls,
    )
    presentation = CollectedStreamPresentation(
        show_tool_calls=continuation.show_tool_calls,
        response_text=paused_answer.text,
        # The calls before the pause stay whole, so a stop later in the turn replays them as a finished turn would.
        tool_trace=with_stored_calls(paused_answer.tool_trace, persisted.tools or ()),
        track_hidden_tools=True,
    )
    collected = await _collect_agent_continuation(events, presentation, run_id=continuation.run_id, progress=progress)
    response = collected.response
    paused = paused_attempt_from_response(
        response,
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
        # Paused on a call no approval asks about and no delegation answers, such as a plugin's externally executed
        # call: nothing can resume it, so the resume fails naming that call, as a live turn does.
        raise pause_without_approval_error(response, entity_label="agent")
    # The usage of the turn's attempts before the paused run, which its record carries across the pause.
    prior_usage = prior_usage_of(persisted)
    attempt = CompletedAttempt(
        response_text=collected.terminal_content
        or ("" if presentation.final_text() else "Tool approval continuation completed"),
        replayable_text=presentation.final_text(),
        has_visible_content=collected.has_visible_content,
        run_id=response.run_id,
        attempt_run_id=response.run_id,
        usage=response.metrics,
        status=response.status,
        tool_executions=collected.tool_executions,
        completed_tools=tuple(presentation.tool_trace),
        build_run_metadata=partial(
            agent_run_metadata_content,
            config,
            response,
            model_name=model_name,
            run_id=response.run_id,
            session_id=continuation.session_id,
            status=response.status,
            # An approval resume prepares no history of its own.
            prepared_history=None,
            prior_usage=prior_usage,
        ),
    )
    return await _stream_continuation_turn(
        _continuation_turn_context(continuation, model_name=model_name, metadata=response.metadata),
        presentation,
        prompt=continuation.request_body,
        resume=TurnResume(attempt, continuation_count=continuation.continuation_count, prior_usage=prior_usage),
        config=config,
        runtime_paths=runtime_paths,
        knowledge=knowledge,
        refresh_scheduler=refresh_scheduler,
        tool_trace_collector=tool_trace_collector,
        tool_context=tool_context,
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
    config: Config,
    runtime_paths: RuntimePaths,
    knowledge: AgentKnowledge | None,
    refresh_scheduler: KnowledgeRefreshScheduler | None,
    tool_trace_collector: list[ToolTraceEntry],
    tool_context: ToolRuntimeContext,
    progress: ProgressPublisher | None,
    resume: TurnResume,
    reusable_build: AgentBuild | None = None,
    delegation_depth: int = 0,
    media: MediaInputs | None = None,
) -> CompletedApprovalRun | PausedAttempt:
    """Stream resumed work and any fresh attempts into the continued reply through the shared response driver.

    The reply shows tool calls when ``presentation`` does. A recovered CLI call runs without a model request, so its
    follow-up, the turn whose ``resume`` restores no attempt, reads the approval receipt in its first attempt's run; a
    resumed run already read it.
    """
    if resume.attempt is None:
        run_id = str(uuid4())
        ctx = replace(ctx, run_id=run_id)
        add_approval_receipt_reader(run_id)
    run_metadata: dict[str, Any] = {}
    completed: list[CompletedAttempt] = []
    stream = stream_agent_response(
        replace(
            ctx,
            prompt=prompt,
            media=media,
            show_tool_calls=presentation.show_tool_calls,
            execution_identity=tool_context.execution_identity,
            refresh_scheduler=refresh_scheduler,
            knowledge=knowledge,
            tool_function_filter=tool_context.tool_function_filter,
            delegation_depth=delegation_depth,
            supports_native_tool_approval=True,
            reusable_build=reusable_build,
        ),
        runtime_paths,
        config,
        sinks=TurnSinks(run_metadata_collector=run_metadata, on_completed=completed.append),
        resume=resume,
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
    if presentation.show_tool_calls:
        tool_trace_collector.extend(tool_trace)
    return CompletedApprovalRun(response_text=response_text, metadata_content=run_metadata)


@dataclass(frozen=True)
class AgentApprovalExecution:
    """Rebuild and continue one persisted paused agent run."""

    config: Callable[[], Config]
    runtime_paths: RuntimePaths
    client: Callable[[], nio.AsyncClient]
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
        tool_context: ToolRuntimeContext,
        tool_trace_collector: list[ToolTraceEntry],
        typing_log_context: Mapping[str, object],
        progress: ProgressPublisher | None,
    ) -> CompletedApprovalRun | PausedAttempt:
        """Apply exact decisions and continue the matching persisted paused run under ``tool_context``'s identity.

        ``progress``, when given, shows the resumed presentation live in the
        reply being continued; the terminal delivery stays with the caller.
        """
        config = self.config()
        execution_identity = tool_context.execution_identity
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
        store, session, persisted = await run_session_storage_operation(
            storage_factory,
            lambda opened: (opened, opened.load_session(continuation.session_id), opened.get_run(continuation.run_id)),
        )
        set_consumption_storage(storage_factory)
        if (
            session is None
            or not isinstance(persisted, RunRecord)
            or persisted.session_id != continuation.session_id
            or (cli_call is None and persisted.status is not RunStatus.paused)
        ):
            msg = f"Paused run {continuation.run_id!r} is no longer available"
            raise RuntimeError(msg)
        approved_calls = own_approved_calls(persisted, continuation.calls)
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
            tools=tool_context.plugins.tools,
            execution_identity=execution_identity,
            delegation_depth=cli_call.delegation_depth if cli_call is not None else 0,
        )
        # The build and the resumed run use one model, even if a room or thread override changes meanwhile.
        model = _continuation_model(config, self.runtime_paths, continuation)
        build = await asyncio.to_thread(
            create_agent,
            continuation.entity_name,
            config,
            self.runtime_paths,
            execution_identity,
            model=model,
            knowledge=knowledge,
            # The turn's capture, so the rebuilt tools and the tool hooks around them come from one generation.
            plugins=tool_context.plugins,
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
        storage = scope_run_storage(scope_context)
        try:
            async with typing_indicator(
                self.client(),
                continuation.room_id,
                log_context=typing_log_context,
            ):
                with helper_usage_context(scope_context):
                    operation: Callable[[], Awaitable[CompletedApprovalRun | PausedAttempt]]
                    if cli_call is not None:
                        expected_worker_target = tool_context.resolve_worker_target()

                        async def recover_cli_call() -> CompletedApprovalRun | PausedAttempt:
                            recovered = await continue_cli_approval(
                                build,
                                continuation,
                                cli_call,
                                persisted,
                                storage,
                                paused_answer=paused_answer,
                                runtime_context=tool_context,
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
                                reusable_build=recovered.reusable_build,
                                resume=TurnResume(
                                    continuation_count=recovered.initial_continuation_count,
                                    prior_usage=prior_usage_of(persisted),
                                ),
                                delegation_depth=recovered.delegation_depth,
                                media=recovered.media,
                                config=config,
                                runtime_paths=self.runtime_paths,
                                knowledge=knowledge,
                                refresh_scheduler=self.refresh_scheduler(),
                                tool_trace_collector=tool_trace_collector,
                                tool_context=tool_context,
                                progress=progress,
                            )

                        operation = recover_cli_call
                    else:
                        # The resume itself refuses decisions that do not cover the waiting calls exactly; this binds
                        # each approval to the toolkit it was granted for before the agent's tools are reached.
                        validate_approval_tool_owners(
                            tool_owners_by_agent(build),
                            approved_calls,
                            pending_approval_requirements(persisted),
                        )
                        operation = partial(
                            _continue_persisted_agent,
                            build,
                            continuation,
                            persisted,
                            storage=storage,
                            paused_answer=paused_answer,
                            config=config,
                            runtime_paths=self.runtime_paths,
                            refresh_scheduler=self.refresh_scheduler(),
                            knowledge=knowledge,
                            tool_trace_collector=tool_trace_collector,
                            tool_context=tool_context,
                            progress=progress,
                            model_name=model.model_name,
                        )
                    with tool_runtime_context(tool_context):
                        result = await operation()
        finally:
            await finalize_consumption()
        return result

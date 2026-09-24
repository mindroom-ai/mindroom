"""Adapt a claimed hidden call to native execution and the ordinary response driver."""

from __future__ import annotations

from contextlib import AsyncExitStack
from copy import deepcopy
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from agno.run import RunContext
from agno.run.agent import ToolCallCompletedEvent, ToolCallStartedEvent

from mindroom.agent_cli.delegation import advance_cli_delegation
from mindroom.agent_cli.events import project_cli_execution, stream_cli_events
from mindroom.agent_cli.lifetime import response_cli_lifetime
from mindroom.agent_cli.projection import register_cli_media
from mindroom.agno_compat_cli_checkpoint import save_cli_session
from mindroom.approval_tools import validate_approval_tool_owners
from mindroom.delegation.state import DelegationState
from mindroom.dynamic_tool_continuation import continuation_decision_from_tools
from mindroom.history.interrupted_replay import (
    build_interrupted_replay_snapshot,
    persist_interrupted_replay_snapshot,
    split_interrupted_tool_trace,
)
from mindroom.hooks import EnrichmentItem
from mindroom.media_inputs import MediaInputs
from mindroom.response_turn import (
    CompletedApprovalRun,
    PausedAttempt,
    ResponsePausedForApproval,
    ResponseTurnContext,
    apply_exact_approval_decisions,
)
from mindroom.tool_system.agent_tool_calls import (
    AgentToolCallEvent,
    PreparedAgentToolCatalog,
    execute_agent_tool_call,
)
from mindroom.tool_system.context_bound_streams import closing_async_stream
from mindroom.tool_system.events import CollectedStreamPresentation, deserialize_tool_trace
from mindroom.tool_system.tool_access import ToolKey

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from agno.models.response import ModelResponse
    from agno.run.agent import RunOutput
    from agno.session.agent import AgentSession

    from mindroom.agent_cli.approval import CliApprovalCall
    from mindroom.event_journal import ApprovalContinuation
    from mindroom.knowledge.refresh_scheduler import KnowledgeRefreshScheduler
    from mindroom.minimal_agent import MinimalAgent
    from mindroom.streaming import ProgressPublisher
    from mindroom.tool_system.agent_tool_calls import PreparedAgentToolBinding
    from mindroom.tool_system.events import ToolTraceEntry
    from mindroom.tool_system.runtime_context import ToolRuntimeContext


@dataclass(frozen=True)
class CliFollowUpTurn:
    """The fresh minimal turn that reports a recovered hidden call's decision or result."""

    ctx: ResponseTurnContext
    presentation: CollectedStreamPresentation
    prompt: str
    reusable_agent: MinimalAgent | None
    initial_continuation_count: int
    delegation_depth: int
    media: MediaInputs


async def continue_cli_approval(  # noqa: C901, PLR0912, PLR0915 - one claimed recovery attempt
    agent: MinimalAgent,
    continuation: ApprovalContinuation,
    payload: CliApprovalCall,
    persisted: RunOutput,
    session: AgentSession,
    *,
    runtime_context: ToolRuntimeContext,
    decisions: dict[str, bool],
    denial_reasons: dict[str, str | None],
    tool_trace_collector: list[ToolTraceEntry],
    refresh_scheduler: KnowledgeRefreshScheduler | None,
    authorize: Callable[[PreparedAgentToolBinding], Awaitable[None]],
    progress: ProgressPublisher | None,
) -> CompletedApprovalRun | PausedAttempt | CliFollowUpTurn:
    """Execute the exact approved hidden call; the old Bash process never resumes.

    The fresh model turn that reports the result is returned for the caller to
    stream through the same continuation driver as standard approvals.
    """
    if continuation.state != "claimed":
        msg = "CLI recovery requires its native claimed continuation"
        raise ValueError(msg)
    persisted = deepcopy(persisted)
    requirements = apply_exact_approval_decisions(
        list(payload.requirements),
        decisions=decisions,
        denial_reasons=denial_reasons,
    )
    approved = tuple(call for call in continuation.calls if decisions.get(call.tool_call_id))
    external = payload.external_requirement
    state = DelegationState.from_metadata(persisted.metadata)
    if state.pending_child_id is None and payload.toolkit != "agent":
        validate_approval_tool_owners([agent], approved, requirements)
    key = ToolKey(payload.toolkit, payload.function)
    call_id = payload.call_id
    arguments = deepcopy(payload.arguments)
    requirement = next(
        (
            item
            for item in requirements
            if item.tool_execution is not None and item.tool_execution.tool_call_id == call_id
        ),
        None,
    )
    if external is None and (
        requirement is None
        or not any(
            call.tool_call_id == call_id and call.tool_name == key.function and call.toolkit_name == key.toolkit
            for call in continuation.calls
        )
    ):
        msg = "CLI payload no longer matches its exact native approval"
        raise ValueError(msg)
    context = RunContext(
        run_id=continuation.run_id,
        session_id=continuation.session_id,
        user_id=continuation.requester_id,
        session_state=deepcopy((session.session_data or {}).get("session_state", {})),
        metadata=deepcopy(persisted.metadata),
    )
    turn = ResponseTurnContext(
        entity_label=continuation.entity_name,
        session_id=continuation.session_id,
        run_id=None,
        correlation_id=continuation.correlation_id or continuation.approval_id,
        reply_to_event_id=runtime_context.reply_to_event_id,
        room_id=continuation.room_id,
        thread_id=continuation.thread_id,
        requester_id=continuation.requester_id,
        matrix_run_metadata=deepcopy(persisted.metadata),
        agent_mode="minimal",
    )
    catalog = PreparedAgentToolCatalog(agent, context, persisted, session, runtime_context)
    resources = AsyncExitStack()
    recovery_owner = None
    presentation = CollectedStreamPresentation(
        show_tool_calls=continuation.show_tool_calls,
        response_text=continuation.response_text,
        tool_trace=deserialize_tool_trace(continuation.response_tool_trace),
        track_hidden_tools=True,
    )
    messages = persisted.messages or []
    current_start = next((index for index in range(len(messages) - 1, -1, -1) if messages[index].role == "user"), 0)
    completed_messages = [message for message in messages[current_start:] if message.role == "tool"]
    media_results: list[ModelResponse] = []

    async def prepare() -> None:
        nonlocal catalog, recovery_owner
        agent.response_context = turn
        if key.toolkit == "shell":
            lifetime = await resources.enter_async_context(response_cli_lifetime())
            lifetime.continuation_count = continuation.continuation_count
            await agent.aget_tools(persisted, context, session, user_id=continuation.requester_id)
            recovery_owner = lifetime.owner
            assert recovery_owner is not None
            catalog = recovery_owner.catalog
            recovery_owner.checkpoint.resume_saved_parent(payload.parent_bash_call_id)
        else:
            catalog = await agent.prepare_execution_catalog(
                persisted,
                context,
                session,
                user_id=continuation.requester_id,
            )

    child_toolkits = {call.tool_call_id: call.toolkit_name for call in continuation.calls}

    def on_child_event(event: object) -> None:
        if isinstance(event, ToolCallStartedEvent | ToolCallCompletedEvent) and event.tool is not None:
            child_id = str(event.tool.tool_call_id)
            if child_id not in child_toolkits:
                return
            execution = project_cli_execution(
                event.tool,
                parent=payload.parent_bash_call_id,
                toolkit_name=child_toolkits[child_id],
            )
            if isinstance(event, ToolCallCompletedEvent):
                presentation.complete_tool(execution)
            elif not any(entry.tool_call_id == child_id for entry in presentation.tool_trace):
                presentation.start_tool(execution)

    result = "Bash was interrupted and its saved command was not resumed."
    controls = []
    await presentation.publish(progress)
    try:
        if external is not None:
            await prepare()
            binding = await catalog.bind(key)
            await authorize(binding)
            outcome = await advance_cli_delegation(
                binding,
                external,
                parent_bash_call_id=payload.parent_bash_call_id,
                delegation_depth=payload.delegation_depth,
                refresh_scheduler=refresh_scheduler,
                decisions=decisions,
                denial_reasons=denial_reasons,
                approval_calls=continuation.calls,
                on_event=on_child_event,
            )
            if isinstance(outcome, PausedAttempt):
                for tool in outcome.tools:
                    if not any(entry.tool_call_id == tool.tool_call_id for entry in presentation.tool_trace):
                        presentation.start_tool(
                            project_cli_execution(
                                tool,
                                parent=payload.parent_bash_call_id,
                                toolkit_name=outcome.toolkit_owners.get(
                                    (outcome.approval_agent_name or continuation.entity_name, str(tool.tool_name)),
                                ),
                            ),
                        )
                return replace(
                    outcome,
                    response_text=presentation.final_text(),
                    tool_trace=tuple(presentation.tool_trace),
                    continuation_count=continuation.continuation_count,
                )
            presentation.complete_tool(
                project_cli_execution(
                    outcome,
                    parent=payload.parent_bash_call_id,
                    toolkit_name=key.toolkit,
                ),
            )
            result = str(outcome.result or "")
        elif not decisions[call_id] or call_id == payload.parent_bash_call_id:
            # An approval for the outer Bash itself never authorizes replaying its script.
            if not decisions[call_id]:
                result = denial_reasons[call_id] or "Not approved by requester"
            assert requirement is not None
            assert requirement.tool_execution is not None
            denied = replace(
                requirement.tool_execution,
                result=result,
                tool_call_error=True,
                requires_confirmation=False,
            )
            presentation.complete_tool(
                project_cli_execution(denied, parent=payload.parent_bash_call_id, toolkit_name=key.toolkit),
            )
        else:
            await prepare()
            binding = await catalog.bind(key)
            if key.toolkit == "shell":
                assert recovery_owner is not None

                events = recovery_owner.execute_shell(
                    binding,
                    call_id,
                    arguments,
                    requirement=requirement,
                    authorize=lambda: authorize(binding),
                    parent=payload.parent_bash_call_id,
                )
            else:
                events = execute_agent_tool_call(
                    binding,
                    call_id,
                    arguments,
                    requirement=requirement,
                    authorize=lambda: authorize(binding),
                )
            stream = stream_cli_events(events)
            async with closing_async_stream(stream):
                async for event in stream:
                    if isinstance(event, ToolCallStartedEvent | ToolCallCompletedEvent):
                        # The exact call is represented by its raw events below;
                        # native callbacks retain the nested calls' own provenance.
                        if event.tool is not None and event.tool.tool_call_id != call_id:
                            if isinstance(event, ToolCallStartedEvent):
                                presentation.start_tool(event.tool)
                            else:
                                presentation.complete_tool(event.tool)
                            await presentation.publish(progress)
                        continue
                    if not isinstance(event, AgentToolCallEvent):
                        continue
                    if event.media is not None:
                        media_results.append(event.media)
                        register_cli_media(
                            event.media,
                            context=runtime_context,
                            policy=agent.output_file_policy,
                            call_id=call_id,
                        )
                    if event.execution is not None:
                        execution = project_cli_execution(
                            event.execution,
                            parent=payload.parent_bash_call_id,
                            toolkit_name=key.toolkit,
                        )
                        if event.kind == "started":
                            presentation.start_tool(execution)
                        elif event.kind in {"completed", "failed", "continuation_required"}:
                            presentation.complete_tool(execution)
                            result = str(event.execution.result or "")
                        if event.kind == "continuation_required":
                            controls.append(event.execution)
                        await presentation.publish(progress)
            if recovery_owner is not None:
                controls = recovery_owner.control_executions
    except ResponsePausedForApproval as pause:
        return replace(pause.paused, continuation_count=continuation.continuation_count)
    finally:
        try:
            await catalog.close()
        finally:
            await resources.aclose()
    completed_trace, interrupted_trace = split_interrupted_tool_trace(persisted.tools)
    known_ids = {entry.tool_call_id for entry in (*completed_trace, *interrupted_trace)}
    for entry in presentation.tool_trace:
        if entry.tool_call_id not in known_ids:
            (completed_trace if entry.type == "tool_call_completed" else interrupted_trace).append(entry)
    snapshot = build_interrupted_replay_snapshot(
        user_message=continuation.request_body,
        user_message_is_structured=False,
        partial_text=continuation.response_text,
        completed_tools=completed_trace,
        interrupted_tools=interrupted_trace,
        run_metadata=persisted.metadata,
        response_event_id=continuation.response_event_id,
    )
    await save_cli_session(
        agent,
        session,
        context.session_state,
        lambda database, saved: persist_interrupted_replay_snapshot(
            storage=database,
            session=saved,
            session_id=continuation.session_id,
            scope_id=continuation.entity_name,
            run_id=continuation.run_id,
            snapshot=snapshot,
            is_team=False,
        ),
    )
    decision = continuation_decision_from_tools(
        controls,
        original_prompt=continuation.request_body,
        continuation_count=continuation.continuation_count,
    )
    if decision.limit_message is not None:
        presentation.append_text(decision.limit_message)
        if continuation.show_tool_calls:
            tool_trace_collector.extend(presentation.tool_trace)
        return CompletedApprovalRun(response_text=presentation.final_text(), metadata_content={})
    active_model_name = (
        decision.model_switch_name
        if decision.model_switch_when == "after-toolcall"
        else continuation.runtime_model_name
    )
    media_sources = (*completed_messages, *media_results)
    # Denied, outer-Bash and delegated outcomes changed the presentation without a live event.
    await presentation.publish(progress)
    return CliFollowUpTurn(
        ctx=replace(
            turn,
            reply_to_event_id=continuation.source_event_ids[0],
            matrix_run_metadata=deepcopy(persisted.metadata),
            active_model_name=active_model_name,
            transient_enrichment_items=(
                EnrichmentItem(
                    key="cli_approval_recovery",
                    minimal_required=True,
                    text=f"The previous Bash process was interrupted and was not resumed. Exact saved tool {key.toolkit}.{key.function} decision/result: {result}",
                ),
            ),
        ),
        presentation=presentation,
        prompt=decision.next_prompt or continuation.request_body,
        reusable_agent=None if decision.should_continue else agent,
        initial_continuation_count=continuation.continuation_count + int(decision.should_continue),
        delegation_depth=payload.delegation_depth,
        media=MediaInputs(
            images=tuple(item for source in media_sources for item in source.images or ()),
            # Messages name their audio field differently from tool responses.
            audio=tuple(item for message in completed_messages for item in message.audio or ())
            + tuple(item for media in media_results for item in media.audios or ()),
            videos=tuple(item for source in media_sources for item in source.videos or ()),
            files=tuple(item for source in media_sources for item in source.files or ()),
        ),
    )

"""Adapt a claimed hidden call to native execution and the ordinary response driver."""

from __future__ import annotations

from contextlib import AsyncExitStack
from copy import deepcopy
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from mindroom.agent_cli.delegation import advance_cli_delegation
from mindroom.agent_cli.events import stream_cli_events
from mindroom.agent_cli.lifetime import response_cli_lifetime
from mindroom.agent_cli.minimal_agent import bind_minimal_owner, minimal_context_documents, prepare_minimal_catalog
from mindroom.agent_cli.projection import register_cli_media
from mindroom.agent_loop.spec import RunContext
from mindroom.approval.tool_owners import validate_approval_tool_owners
from mindroom.delegation.state import DelegationState
from mindroom.history.interrupted_replay import (
    build_interrupted_replay_snapshot,
    persist_interrupted_replay_snapshot,
    split_interrupted_tool_trace,
)
from mindroom.hooks import EnrichmentItem
from mindroom.llm.events import ToolCallOrigin, ToolCompleted, ToolStarted
from mindroom.media_inputs import MediaInputs
from mindroom.response_turn import (
    CompletedApprovalRun,
    PausedAnswer,
    PausedAttempt,
    ResponsePausedForApproval,
    ResponseTurnContext,
    apply_exact_approval_decisions,
)
from mindroom.session_storage.store import write_with_session_state
from mindroom.tool_system.agent_tool_calls import AgentToolCallEvent, execute_agent_tool_call
from mindroom.tool_system.context_bound_streams import closing_async_stream
from mindroom.tool_system.events import CollectedStreamPresentation
from mindroom.tool_system.tool_access import ToolKey
from mindroom.turn_continuation import continuation_decision_from_tools

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from mindroom.agent_cli.approval import CliApprovalCall
    from mindroom.agent_cli.turn import LiveTurnTools
    from mindroom.agent_loop.build import AgentBuild
    from mindroom.event_journal import ApprovalContinuation
    from mindroom.knowledge.refresh_scheduler import KnowledgeRefreshScheduler
    from mindroom.llm.events import ReportedCall
    from mindroom.llm.runs import RunRecord
    from mindroom.session_storage.store import ConversationStore
    from mindroom.session_storage.types import ConversationSession
    from mindroom.streaming import ProgressPublisher
    from mindroom.tool_system.agent_tool_calls import PreparedAgentToolBinding, PreparedAgentToolCatalog
    from mindroom.tool_system.events import ToolTraceEntry
    from mindroom.tool_system.runtime_context import ToolRuntimeContext
    from mindroom.tool_system.tool_function import ToolValue


@dataclass(frozen=True)
class CliFollowUpTurn:
    """The fresh minimal turn that reports a recovered hidden call's decision or result."""

    ctx: ResponseTurnContext
    presentation: CollectedStreamPresentation
    prompt: str
    reusable_build: AgentBuild | None
    initial_continuation_count: int
    delegation_depth: int
    media: MediaInputs


async def continue_cli_approval(  # noqa: C901, PLR0912, PLR0915 - one claimed recovery attempt
    build: AgentBuild,
    continuation: ApprovalContinuation,
    payload: CliApprovalCall,
    persisted: RunRecord,
    session: ConversationSession,
    store: ConversationStore,
    *,
    paused_answer: PausedAnswer,
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
    approved = tuple(call for call in continuation.calls if decisions.get(call.tool_call_id))
    external = payload.external_requirement
    state = DelegationState.from_metadata(persisted.metadata)
    if state.pending_child_id is None and payload.toolkit != "agent":
        # Generated functions have no configured toolkit; the rebuilt binding's authorization checks them.
        owners = {(build.spec.id, name): toolkit for name, toolkit in build.tool_owners.items()}
        validate_approval_tool_owners(owners, approved, payload.requirements)
    requirements = apply_exact_approval_decisions(
        list(payload.requirements),
        decisions=decisions,
        denial_reasons=denial_reasons,
    )
    key = ToolKey(payload.toolkit, payload.function)
    # Where the resumed call ran, which its own events in the reply's tool trace keep.
    call_origin = ToolCallOrigin(payload.parent_bash_call_id, key.toolkit)
    call_id = payload.call_id
    arguments = deepcopy(payload.arguments)
    requirement = next((item for item in requirements if item.tool_execution.tool_call_id == call_id), None)
    if external is None and (
        requirement is None
        or not any(
            call.tool_call_id == call_id and call.tool_name == key.function and call.toolkit_name == key.toolkit
            for call in continuation.calls
        )
    ):
        msg = "CLI payload no longer matches its exact native approval"
        raise ValueError(msg)
    run = RunContext(
        run_id=continuation.run_id,
        session_id=continuation.session_id,
        user_id=continuation.requester_id,
        session=session,
        metadata=deepcopy(persisted.metadata),
        model_name=continuation.runtime_model_name,
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
    resources = AsyncExitStack()
    catalog: PreparedAgentToolCatalog | None = None
    recovery_owner: LiveTurnTools | None = None
    presentation = CollectedStreamPresentation(
        show_tool_calls=continuation.show_tool_calls,
        response_text=paused_answer.text,
        tool_trace=list(paused_answer.tool_trace),
        track_hidden_tools=True,
    )
    messages = persisted.messages
    current_start = next((index for index in range(len(messages) - 1, -1, -1) if messages[index].role == "user"), 0)
    completed_messages = [message for message in messages[current_start:] if message.role == "tool"]
    media_results: list[ToolValue] = []

    async def prepare() -> PreparedAgentToolCatalog:
        nonlocal catalog, recovery_owner
        catalog = await prepare_minimal_catalog(
            build,
            run=run,
            store=store,
            delegation_depth=payload.delegation_depth,
            record=persisted,
        )
        if key.toolkit == "shell":
            lifetime = await resources.enter_async_context(response_cli_lifetime())
            lifetime.continuation_count = continuation.continuation_count
            recovery_owner = bind_minimal_owner(
                build,
                catalog,
                response_context=turn,
                context_documents=minimal_context_documents(build),
                delegation_depth=payload.delegation_depth,
                refresh_scheduler=refresh_scheduler,
            )
            recovery_owner.checkpoint.resume_saved_parent(payload.parent_bash_call_id)
        return catalog

    child_toolkits = {call.tool_call_id: call.toolkit_name for call in continuation.calls}

    def on_child_event(event: object) -> None:
        if isinstance(event, ToolStarted | ToolCompleted):
            child_id = str(event.call.tool_call_id)
            if child_id not in child_toolkits:
                return
            origin = ToolCallOrigin(payload.parent_bash_call_id, child_toolkits[child_id])
            if isinstance(event, ToolCompleted):
                presentation.complete_tool(event.call, origin=origin)
            elif not any(entry.tool_call_id == child_id for entry in presentation.tool_trace):
                presentation.start_tool(event.call, origin=origin)

    result = "Bash was interrupted and its saved command was not resumed."
    controls: list[ReportedCall] = []
    await presentation.publish(progress)
    try:
        if external is not None:
            binding = await (await prepare()).bind(key)
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
                        toolkit_name = outcome.toolkit_owners.get(
                            (outcome.approval_agent_name or continuation.entity_name, str(tool.tool_name)),
                        )
                        presentation.start_tool(
                            tool.call,
                            origin=ToolCallOrigin(payload.parent_bash_call_id, toolkit_name),
                        )
                return replace(
                    outcome,
                    response_text=presentation.final_text(),
                    tool_trace=tuple(presentation.tool_trace),
                    continuation_count=continuation.continuation_count,
                )
            presentation.complete_tool(outcome.call, origin=call_origin)
            result = str(outcome.result or "")
        elif not decisions[call_id] or call_id == payload.parent_bash_call_id:
            assert requirement is not None
            # An approval for the outer Bash itself never authorizes replaying its script.
            if not decisions[call_id]:
                result = requirement.denial_note
            denied = replace(requirement.tool_execution.call, result=result, is_error=True)
            presentation.complete_tool(denied, origin=call_origin)
        else:
            binding = await (await prepare()).bind(key)
            if recovery_owner is not None:
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
                    if isinstance(event, ToolStarted | ToolCompleted):
                        # The exact call is represented by its own events below;
                        # nested calls keep their own provenance.
                        if event.call.tool_call_id != call_id:
                            if isinstance(event, ToolStarted):
                                presentation.start_tool(event.call, origin=event.origin)
                            else:
                                presentation.complete_tool(event.call, origin=event.origin)
                            await presentation.publish(progress)
                        continue
                    if not isinstance(event, AgentToolCallEvent):
                        continue
                    if event.media is not None:
                        media_results.append(event.media)
                        register_cli_media(
                            event.media,
                            context=runtime_context,
                            policy=build.surface.output_file_policy,
                            call_id=call_id,
                        )
                    if event.call is not None:
                        if event.kind == "started":
                            presentation.start_tool(event.call, origin=call_origin)
                        elif event.kind in {"completed", "failed", "continuation_required"}:
                            presentation.complete_tool(event.call, origin=call_origin)
                            result = str(event.call.result or "")
                        if event.kind == "continuation_required":
                            controls.append(event.call)
                        await presentation.publish(progress)
            if recovery_owner is not None:
                controls = recovery_owner.control_calls
    except ResponsePausedForApproval as pause:
        return replace(pause.paused, continuation_count=continuation.continuation_count)
    finally:
        try:
            if catalog is not None:
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
        partial_text=paused_answer.text,
        completed_tools=completed_trace,
        interrupted_tools=interrupted_trace,
        run_metadata=persisted.metadata,
        response_event_id=continuation.response_event_id,
    )
    # The replay run and the state the recovered call left commit together.
    await write_with_session_state(
        store,
        session,
        lambda opened, state: persist_interrupted_replay_snapshot(
            storage=opened,
            session=session,
            run_id=continuation.run_id,
            snapshot=snapshot,
            state=state,
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
        reusable_build=None if decision.should_continue else build,
        initial_continuation_count=continuation.continuation_count + int(decision.should_continue),
        delegation_depth=payload.delegation_depth,
        media=MediaInputs(
            images=(
                *(item for message in completed_messages for item in message.images or ()),
                *(item for media in media_results for item in media.images),
            ),
            audio=(
                *(item for message in completed_messages for item in message.audio or ()),
                *(item for media in media_results for item in media.audios),
            ),
            videos=(
                *(item for message in completed_messages for item in message.videos or ()),
                *(item for media in media_results for item in media.videos),
            ),
            files=(
                *(item for message in completed_messages for item in message.files or ()),
                *(item for media in media_results for item in media.files),
            ),
        ),
    )

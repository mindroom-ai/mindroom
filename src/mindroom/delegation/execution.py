"""Drive durable external delegation requirements without retaining Python waits."""

from __future__ import annotations

import asyncio
import json
from contextlib import AsyncExitStack
from contextvars import ContextVar
from copy import deepcopy
from dataclasses import dataclass, replace
from functools import partial
from typing import TYPE_CHECKING, NamedTuple, NotRequired, TypedDict, Unpack, cast
from uuid import uuid4

from mindroom.agent_loop.build import tool_owners_by_agent
from mindroom.approval.receipt import add_approval_receipt_reader
from mindroom.approval.resume import ResumeDecisions, ResumeRunner, approval_decisions, forward_resume
from mindroom.approval.tool_approval import POLICY_CONFIRMATION_APPROVAL_TYPE, tool_may_require_approval
from mindroom.approval.tool_owners import required_approval_tool_names, validate_approval_tool_owners
from mindroom.delegation.background import (
    delegation_outcome,
    delegation_result,
    start_delegation,
)
from mindroom.delegation.hooks import after_delegation, before_delegation
from mindroom.delegation.job_approvals import request_child_approvals
from mindroom.delegation.lifecycle import (
    authorize_delegation,
    child_execution_identity,
    child_run_context,
    child_tool_runtime_context,
    finish_child_turn,
    live_delegation_config,
    note_child_run_id,
    observe_child_event,
    prepare_child_turn,
    reserve_child_turn,
    settle_child_response,
    start_child_turn,
)
from mindroom.delegation.personas import (
    caller_knowledge_names,
    caller_skill_names,
    caller_toolkit_names,
    follow_up_refusal,
    resolve_persona_request,
)
from mindroom.delegation.recovery import interrupt_child, interrupt_stopped_child, read_child_run, resolve_subagent
from mindroom.delegation.sessions import (
    SubagentSessionError,
    subagent_liveness,
)
from mindroom.delegation.state import (
    DELEGATION_STATE_KEY,
    DelegationChild,
    DelegationPendingTool,
    DelegationState,
    child_call_id,
    child_card_id,
    delegation_card_id,
)
from mindroom.delegation.storage import freeze_delegation_storage
from mindroom.history.types import HistoryScope
from mindroom.llm.events import RunFinished, ToolCompleted, ToolStarted
from mindroom.llm.runs import RunRecord, RunRequirement, RunStatus, TeamRunRecord, ToolExecution
from mindroom.logging_config import get_logger
from mindroom.redaction import redact_sensitive_text
from mindroom.runtime_resolution import resolve_agent_runtime, resolve_agent_storage
from mindroom.session_storage.agent_storage import create_session_storage
from mindroom.session_storage.lanes import run_session_storage_operation
from mindroom.tool_jobs.background_after import BACKGROUND_AFTER, application_arguments, read_background_after
from mindroom.tool_jobs.consumption import retain_parent_call_claim
from mindroom.tool_jobs.control import job_owns_execution
from mindroom.tool_jobs.conversation import current_approval_wait_timeout
from mindroom.tool_jobs.runtime import (
    BackgroundOutcome,
    JobAccessError,
    format_job_handle,
    get_background_runtime,
)
from mindroom.tool_jobs.settings import toolkit_is_background_excluded
from mindroom.tool_system.call_record import record_call
from mindroom.tool_system.context_bound_streams import callback_event_stream, closing_async_stream
from mindroom.tool_system.output_files import (
    OUTPUT_PATH_ARGUMENT,
    ToolOutputFilePolicy,
    ToolOutputFileRequest,
    finalize_tool_output_file,
    normalize_output_path_argument,
    prepare_tool_output_file,
)
from mindroom.tool_system.runtime_context import current_budget_monitor, get_tool_runtime_context, tool_runtime_context
from mindroom.tool_system.worker_routing import (
    run_with_tool_execution_identity,
)
from mindroom.turn_continuation import continuation_decision_from_tools
from mindroom.workspaces import resolve_agent_workspace_from_state_path

logger = get_logger(__name__)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
    from pathlib import Path

    from mindroom.agent_modes import AgentMode
    from mindroom.approval.resume import ContinueRun, ResumeEvents
    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.delegation.records import DelegationTerminalStatus
    from mindroom.delegation.state import ChildResponseRunner, DelegationHookState, SubagentPersona
    from mindroom.event_journal import ApprovalCall
    from mindroom.knowledge.refresh_scheduler import KnowledgeRefreshScheduler
    from mindroom.llm.events import RunEvent
    from mindroom.llm.runs import AnyRunRecord
    from mindroom.session_storage.store import ConversationStore
    from mindroom.session_storage.types import ConversationSession
    from mindroom.tool_jobs.runtime import ToolJobRuntime
    from mindroom.tool_system.worker_routing import ToolExecutionIdentity


class _DelegationOptions(TypedDict):
    """Explicit dependencies shared by blocking and streaming delegation drivers."""

    agent_name: str
    run_child: ChildResponseRunner
    config: Config
    runtime_paths: RuntimePaths
    execution_identity: ToolExecutionIdentity | None
    delegation_depth: NotRequired[int]
    refresh_scheduler: NotRequired[KnowledgeRefreshScheduler | None]
    decisions: NotRequired[dict[str, bool] | None]
    denial_reasons: NotRequired[dict[str, str | None] | None]
    approval_calls: NotRequired[Sequence[ApprovalCall]]


_RUNNING_CHILD_ID: ContextVar[str | None] = ContextVar("running_delegated_child", default=None)


@dataclass(frozen=True)
class _ChildOutcome:
    """Retained outcome and executable ownership observed by its actual envelope."""

    response: RunRecord
    toolkit_owners: Mapping[tuple[str, str], str | None]


@dataclass(frozen=True)
class _DelegationTarget:
    """Validated destination and task for a fresh or continued subagent."""

    agent_name: str
    task: str
    previous_child: DelegationChild | None = None
    agent_mode: AgentMode = "standard"
    persona: SubagentPersona | None = None
    model: str | None = None


# The delegate tools run outside the loop: the driver runs the child and continues the parent with its result.
DELEGATION_TOOL_NAMES = frozenset({"run_subagent", "continue_subagent"})


def _external_requirements(response: AnyRunRecord) -> list[RunRequirement]:
    return [
        requirement
        for requirement in response.requirements or ()
        if requirement.needs_external_execution and requirement.tool_execution.tool_name in DELEGATION_TOOL_NAMES
    ]


def has_delegation_state(response: AnyRunRecord) -> bool:
    """Return whether the native delegation driver owns this run's pause."""
    return bool(_external_requirements(response) or (response.metadata or {}).get(DELEGATION_STATE_KEY))


def _delegation_call(requirement: RunRequirement) -> bool:
    tool = requirement.tool_execution
    return tool.tool_name in DELEGATION_TOOL_NAMES and bool(tool.external_execution_required)


def answered_delegation_calls(response: AnyRunRecord) -> dict[tuple[str | None, str], str]:
    """Return the answer each delegation call of a paused run already holds, by its member and call id.

    The member is the one whose run waits for the call, None for the run's own; a team's members number their calls
    independently, so a call id alone does not name a call, as the driver's own keys say.
    """
    return {
        (requirement.member_agent_id, call_id): answer
        for requirement in response.requirements or ()
        if _delegation_call(requirement)
        and (call_id := requirement.tool_execution.tool_call_id) is not None
        and (answer := requirement.external_execution_result) is not None
    }


def _awaiting_answers[R: AnyRunRecord](response: R) -> R:
    """Return ``response`` with its delegation calls waiting again, for the continuation that answers them.

    The loop answers an external call only from the results it is handed, under its exact-coverage check.
    """
    return replace(
        response,
        requirements=[
            replace(requirement, external_execution_result=None) if _delegation_call(requirement) else requirement
            for requirement in response.requirements or ()
        ],
    )


async def persist_delegation_state(
    store: ConversationStore | None,
    session: ConversationSession,
    response: AnyRunRecord,
    state: DelegationState,
) -> None:
    """Save native child ownership on its real parent run, including hidden calls.

    The run's row is rewritten in place without touching the usage ledger: the loop recorded the run's usage when
    it stored the pause, and records the continuation's when that ends. ``session`` then holds the run as stored.
    """
    response.metadata = {**(response.metadata or {}), DELEGATION_STATE_KEY: state.to_dict()}
    if store is None:
        msg = "Durable delegation requires persisted session storage"
        raise RuntimeError(msg)
    # The copy is taken before the lane is awaited, so the row lands as it was handed over, whatever the driver
    # changes on the record while the lane is busy.
    handed_over = deepcopy(response)
    await run_session_storage_operation(
        lambda: store,
        lambda opened: opened.save_run(session, handed_over, record_usage=False),
    )


async def _run_child(
    child: DelegationChild,
    *,
    run_child: ChildResponseRunner,
    config: Config,
    runtime_paths: RuntimePaths,
    caller_identity: ToolExecutionIdentity,
    refresh_scheduler: KnowledgeRefreshScheduler | None,
    decisions: dict[str, bool] | None,
    denial_reasons: dict[str, str | None] | None,
    approval_calls: Sequence[ApprovalCall],
    fresh: bool,
) -> _ChildOutcome:
    """Reserve the child and run it in its execution scope; the caller holds its liveness through settlement."""
    await reserve_child_turn(child, owner=caller_identity, runtime_paths=runtime_paths)
    token = _RUNNING_CHILD_ID.set(child.delegation_id)
    try:
        async with child_run_context(child, config=config, runtime_paths=runtime_paths) as observation:
            response = await run_with_tool_execution_identity(
                child_execution_identity(child),
                operation=lambda: _execute_child(
                    child,
                    run_child=run_child,
                    config=config,
                    runtime_paths=runtime_paths,
                    refresh_scheduler=refresh_scheduler,
                    decisions=decisions,
                    denial_reasons=denial_reasons,
                    approval_calls=approval_calls,
                    fresh=fresh,
                ),
            )
            observation.keep_outcome(response.response)
            return response
    finally:
        _RUNNING_CHILD_ID.reset(token)


async def _execute_child(
    child: DelegationChild,
    *,
    run_child: ChildResponseRunner,
    config: Config,
    runtime_paths: RuntimePaths,
    refresh_scheduler: KnowledgeRefreshScheduler | None,
    decisions: dict[str, bool] | None,
    denial_reasons: dict[str, str | None] | None,
    approval_calls: Sequence[ApprovalCall],
    fresh: bool,
) -> _ChildOutcome:
    """Start a fresh envelope or resume the exact stored child under its own scope."""
    # These imports cross the creation/envelope cycle only during execution.
    from mindroom.agents import create_agent  # noqa: PLC0415
    from mindroom.knowledge.utils import resolve_agent_knowledge_access_async  # noqa: PLC0415
    from mindroom.response_turn import local_approval_requirements  # noqa: PLC0415

    identity = child_execution_identity(child)
    context = get_tool_runtime_context()
    if context is None or context.requester_id != identity.requester_id or context.room_id != identity.room_id:
        msg = "Delegation requester context does not match its retained wait"
        raise RuntimeError(msg)
    child_context = child_tool_runtime_context(context, child)
    if fresh:
        return await _start_child_envelope(
            child,
            run_child,
            config,
            runtime_paths,
            prompt=child.task,
            refresh_scheduler=refresh_scheduler,
        )
    persisted = await read_child_run(child, config, runtime_paths)
    if persisted is None:
        msg = "Delegated run was interrupted before retaining an outcome"
        raise RuntimeError(msg)
    if persisted.status != RunStatus.paused or decisions is None:
        # A retained pause must never acquire ownership from a later reconstruction.
        return _ChildOutcome(persisted, {})
    child_state = DelegationState.from_metadata(persisted.metadata)
    local_calls = () if child_state.pending_child_id is not None else approval_calls
    approved_calls = tuple(call for call in local_calls if decisions.get(call.tool_call_id))
    required_tool_names = await required_approval_tool_names(
        child.child_agent_name,
        approved_calls,
        config=config,
        runtime_paths=runtime_paths,
        execution_identity=identity,
    )
    if decisions is not None:
        await settle_child_response(
            child,
            persisted,
            config=config,
            runtime_paths=runtime_paths,
            decisions=decisions,
            denial_reasons=denial_reasons,
        )
    knowledge = await resolve_agent_knowledge_access_async(
        child.child_agent_name,
        config,
        runtime_paths,
        refresh_scheduler=refresh_scheduler,
        execution_identity=identity,
        only=child.persona.kept_knowledge if child.persona is not None else None,
    )
    storage = await asyncio.to_thread(
        create_session_storage,
        child.child_agent_name,
        config,
        runtime_paths,
        execution_identity=identity,
    )
    build = await asyncio.to_thread(
        create_agent,
        child.child_agent_name,
        config,
        runtime_paths,
        identity,
        active_model_name=child.model_name,
        knowledge=knowledge.knowledge,
        history_storage=storage,
        refresh_scheduler=refresh_scheduler,
        delegation_depth=child.depth,
        supports_native_tool_approval=True,
        include_interactive_questions=False,
        tool_function_filter=context.tool_function_filter,
        required_tool_names=required_tool_names,
        persona=child.persona,
    )
    session = await asyncio.to_thread(storage.load_session, child.session_id, "agent")
    if session is None:
        msg = "Delegated run's session is no longer available"
        raise RuntimeError(msg)
    toolkit_owners = tool_owners_by_agent(build)
    validate_approval_tool_owners(toolkit_owners, approved_calls, local_approval_requirements(persisted))
    runner = ResumeRunner(
        build=build,
        config=config,
        runtime_paths=runtime_paths,
        agent_name=child.child_agent_name,
        model_name=child.model_name,
        store=storage,
        session=session,
        scope=HistoryScope(kind="agent", scope_id=child.child_agent_name),
        user_id=identity.requester_id,
    )
    with tool_runtime_context(child_context):
        response = await _continue_child(
            runner,
            run_child,
            child,
            persisted,
            config=config,
            runtime_paths=runtime_paths,
            identity=identity,
            refresh_scheduler=refresh_scheduler,
            decisions=decisions,
            denial_reasons=denial_reasons,
            approval_calls=approval_calls,
        )

    if response.status == RunStatus.completed:
        decision = continuation_decision_from_tools(response.tools, original_prompt=child.task, continuation_count=0)
        if decision.next_prompt is not None:
            model_name = (
                decision.model_switch_name
                if decision.model_switch_when == "after-toolcall" and decision.model_switch_name is not None
                else child.model_name
            )
            note_child_run_id(child, uuid4().hex, runtime_paths, model_name=model_name)
            return await _start_child_envelope(
                child,
                run_child,
                config,
                runtime_paths,
                prompt=decision.next_prompt,
                refresh_scheduler=refresh_scheduler,
            )
    return _ChildOutcome(response, toolkit_owners)


async def _start_child_envelope(
    child: DelegationChild,
    run_child: ChildResponseRunner,
    config: Config,
    runtime_paths: RuntimePaths,
    *,
    prompt: str,
    refresh_scheduler: KnowledgeRefreshScheduler | None,
) -> _ChildOutcome:
    """Run the normal response loop in the child's retained conversation scope."""
    from mindroom.response_turn import ResponsePausedForApproval  # noqa: PLC0415

    result = None
    toolkit_owners = {}
    try:
        result = await run_child(
            child,
            prompt=prompt,
            config=config,
            runtime_paths=runtime_paths,
            refresh_scheduler=refresh_scheduler,
            supports_native_tool_approval=True,
        )
    except ResponsePausedForApproval as suspension:
        toolkit_owners = suspension.paused.toolkit_owners
        if suspension.paused.runtime_model_name is not None:
            note_child_run_id(child, child.run_id, runtime_paths, model_name=suspension.paused.runtime_model_name)
    response = await read_child_run(child, config, runtime_paths)
    if response is None:
        msg = result or "Delegated execution did not retain its exact run outcome"
        raise RuntimeError(msg)
    return _ChildOutcome(response, toolkit_owners)


async def _observed_child_events(events: AsyncIterator[RunEvent]) -> AsyncIterator[RunEvent]:
    """Pass a child's resume through its audit and progress observers; its events are this context's own."""
    async with closing_async_stream(events):
        async for event in events:
            await observe_child_event(event)
            yield event


async def _continue_child(
    runner: ResumeRunner,
    run_child: ChildResponseRunner,
    child: DelegationChild,
    persisted: RunRecord,
    *,
    config: Config,
    runtime_paths: RuntimePaths,
    identity: ToolExecutionIdentity,
    refresh_scheduler: KnowledgeRefreshScheduler | None,
    decisions: dict[str, bool] | None,
    denial_reasons: dict[str, str | None] | None,
    approval_calls: Sequence[ApprovalCall],
) -> RunRecord:
    """Apply child decisions and drive any further nested delegations."""

    async def continue_run(paused: RunRecord, resume: ResumeDecisions, _on_event: ResumeEvents | None) -> RunRecord:
        return await forward_resume(_observed_child_events(runner.events(paused, resume)), None, record_type=RunRecord)

    if not has_delegation_state(persisted):
        if decisions is None:
            return persisted
        persisted = await continue_run(
            persisted,
            approval_decisions(approval_calls, decisions, denial_reasons or {}),
            None,
        )
        decisions = None
        denial_reasons = None
        approval_calls = ()
    return await _drive_delegations(
        persisted,
        store=runner.store,
        session=runner.session,
        continue_parent=continue_run,
        agent_name=child.child_agent_name,
        run_child=run_child,
        config=config,
        runtime_paths=runtime_paths,
        execution_identity=identity,
        delegation_depth=child.depth,
        refresh_scheduler=refresh_scheduler,
        decisions=decisions,
        denial_reasons=denial_reasons,
        approval_calls=approval_calls,
    )


def _pending_child(state: DelegationState, child: DelegationChild, outcome: _ChildOutcome) -> None:
    from mindroom.response_turn import paused_attempt_from_response  # noqa: PLC0415

    response = outcome.response
    paused = paused_attempt_from_response(
        response,
        fallback_session_id=child.session_id,
        fallback_run_id=child.run_id,
        toolkit_owners=outcome.toolkit_owners,
    )
    if paused is None:
        msg = "Delegated child paused without supported exact approval requirements"
        raise RuntimeError(msg)
    state.pending_child_id = child.delegation_id
    state.pending_agent_name = paused.approval_agent_name or child.child_agent_name
    child_state = DelegationState.from_metadata(response.metadata)
    for tool in paused.tools:
        projected = deepcopy(tool)
        projected.tool_call_id = child_card_id(child.delegation_id, str(tool.tool_call_id))
        state.pending_tools.append(projected.to_dict())
        state.pending_tool_sources[projected.tool_call_id] = deepcopy(
            child_state.pending_tool_sources.get(str(tool.tool_call_id))
            or DelegationPendingTool(
                child=child,
                tool_call_id=str(tool.tool_call_id),
                toolkit_name=paused.toolkit_owners.get((child.child_agent_name, str(tool.tool_name))),
            ),
        )
        requirement = RunRequirement(projected)
        state.pending_requirements.append(requirement.to_dict())


async def _resolved_child_tool(
    source: DelegationPendingTool,
    call_id: str,
    *,
    config: Config,
    runtime_paths: RuntimePaths,
) -> ToolExecution | None:
    """Read the exact approved attempt, even after the child starts another run."""
    response = await read_child_run(source.child, config, runtime_paths)
    if response is None:
        return None
    for tool in response.tools or ():
        if tool.tool_call_id == source.tool_call_id and not tool.is_paused:
            projected = deepcopy(tool)
            projected.tool_call_id = call_id
            return projected
    return None


async def _complete_approved_child_tools(
    pending_tools: list[dict[str, object]],
    tool_sources: Mapping[str, DelegationPendingTool],
    on_event: ResumeEvents,
    *,
    config: Config,
    runtime_paths: RuntimePaths,
) -> None:
    """Publish each resumed tool's retained result in the parent's live trace."""
    for pending_tool in pending_tools:
        call_id = str(pending_tool["tool_call_id"])
        completed_tool = await _resolved_child_tool(
            tool_sources[call_id],
            call_id,
            config=config,
            runtime_paths=runtime_paths,
        )
        if completed_tool is not None:
            on_event(ToolCompleted(completed_tool))


def _settle_pending_child_tools(
    pending_tools: list[dict[str, object]],
    on_event: ResumeEvents,
    *,
    reason: str,
) -> None:
    """Close the current approval generation's visible tools after an interruption."""
    for pending_tool in pending_tools:
        cancelled_tool = ToolExecution.from_dict(pending_tool)
        cancelled_tool.requires_confirmation = False
        cancelled_tool.external_execution_required = False
        cancelled_tool.result = reason
        cancelled_tool.tool_call_error = True
        on_event(ToolCompleted(cancelled_tool))


def _prepare_delegation_output(
    caller: str,
    config: Config,
    runtime_paths: RuntimePaths,
    identity: ToolExecutionIdentity,
    raw_path: object,
    tool_name: str = "run_subagent",
) -> ToolOutputFileRequest | dict[str, object] | None:
    """Resolve the caller's output policy without scaffolding or reconciling its workspace."""
    storage = resolve_agent_storage(caller, config, runtime_paths, execution_identity=identity)
    workspace = resolve_agent_workspace_from_state_path(
        caller,
        config,
        runtime_paths=runtime_paths,
        state_storage_path=storage.state_root,
        use_state_storage_path=storage.execution.policy.private_workspace_enabled,
    )
    if workspace is None:
        if normalize_output_path_argument(raw_path) is not None:
            return {
                "mindroom_tool_output": {"status": "error", "error": "Output redirection requires an agent workspace."},
            }
        return None
    policy = ToolOutputFilePolicy.from_runtime(
        workspace.root,
        runtime_paths,
        auto_save_threshold_bytes=config.defaults.tool_output_auto_save_threshold_bytes,
    )
    return prepare_tool_output_file(policy, tool_name=tool_name, output_path=raw_path)


def _finalize_delegation_output(result: str, request: ToolOutputFileRequest | None) -> str:
    """Apply the shared file policy once and keep the native result text-serializable."""
    formatted = finalize_tool_output_file(request, result) if request is not None else result
    return formatted if isinstance(formatted, str) else json.dumps(formatted)


def _resolve_delegation_requirement(
    requirement: RunRequirement,
    result: str,
    on_event: ResumeEvents | None,
    *,
    output_request: ToolOutputFileRequest | None = None,
) -> str:
    """Answer one external call with its finished result and close its live tool trace, including rejections.

    The answer stays on the parent's requirement, as Agno's driver stored it, so a later entry never advances the
    call again; the parent's continuation hands every answer to the loop's own decision check.
    """
    result = _finalize_delegation_output(result, output_request)
    requirement.set_external_execution_result(result)
    if on_event is not None:
        on_event(ToolCompleted(replace(requirement.tool_execution, external_execution_required=False)))
    return result


def _child_result_text(child: DelegationChild, receipt: str) -> str:
    result = child.result or "Agent completed the task but returned no content."
    if child.status != "completed":
        result = f"Delegation to '{child.child_agent_name}' {child.status}: {result}"
    return f"{result}\n\n{receipt}"


async def _background_child_outcome(
    child: DelegationChild,
    *,
    owner: ToolExecutionIdentity,
    run_child: ChildResponseRunner,
    config: Config,
    runtime_paths: RuntimePaths,
    refresh_scheduler: KnowledgeRefreshScheduler | None,
    output_request: ToolOutputFileRequest | None = None,
) -> BackgroundOutcome:
    """Keep child execution, approvals, liveness and settlement owned by its background job."""

    def finished(status: DelegationTerminalStatus, text: str) -> BackgroundOutcome:
        return delegation_outcome(status, _finalize_delegation_output(text, output_request))

    run = partial(
        _run_child,
        child,
        run_child=run_child,
        runtime_paths=runtime_paths,
        caller_identity=owner,
        refresh_scheduler=refresh_scheduler,
    )
    # Settlement stays inside this claim, so recovery cannot take a child that failed before its terminal state.
    async with subagent_liveness(child, runtime_paths):
        primary_error: Exception | None = None
        try:
            outcome = await run(config=config, decisions=None, denial_reasons=None, approval_calls=(), fresh=True)
            while outcome.response.status == RunStatus.paused:
                # The job, not a parent reply, asks for the child's approvals and resumes it with the decisions.
                decisions, denial_reasons, calls = await request_child_approvals(
                    child,
                    outcome.response,
                    outcome.toolkit_owners,
                    owner=owner,
                    config=config,
                    runtime_paths=runtime_paths,
                )
                outcome = await run(
                    # A child resumes long after it started: it rebuilds under the policy current now, as its
                    # approvals did, so a rule a reload added gates its later calls too.
                    config=live_delegation_config(config),
                    decisions=decisions,
                    denial_reasons=denial_reasons,
                    approval_calls=calls,
                    fresh=False,
                )
        except asyncio.CancelledError:
            await interrupt_stopped_child(child, config=config, runtime_paths=runtime_paths)
            raise
        except Exception as error:
            primary_error = error
            try:
                await interrupt_child(
                    child,
                    config=config,
                    runtime_paths=runtime_paths,
                    reason=redact_sensitive_text(str(error)),
                    status="failed",
                )
            except Exception as cleanup_error:
                logger.warning(
                    "Native delegation cleanup failed after execution failure",
                    delegation_id=child.delegation_id,
                    error=str(cleanup_error),
                    exc_info=True,
                )
                return finished(
                    "failed",
                    f"{error}\n\nDelegation cleanup did not complete; recovery may still be required: {cleanup_error}",
                )
        try:
            receipt = await finish_child_turn(child, config=config, runtime_paths=runtime_paths)
        except Exception as settlement_error:
            if primary_error is None:
                raise
            logger.warning(
                "Native delegation settlement failed after execution failure",
                delegation_id=child.delegation_id,
                error=str(settlement_error),
                exc_info=True,
            )
            return finished(
                "failed",
                f"{primary_error}\n\n"
                f"Delegation settlement did not complete; recovery may still be required: {settlement_error}",
            )
        status = child.status
        assert status != "running"
        assert status != "paused"
        return finished(status, _child_result_text(child, receipt))


def _forget_child(state: DelegationState, child: DelegationChild) -> None:
    state.children = [item for item in state.children if item.delegation_id != child.delegation_id]


async def _detach_from_job(
    state: DelegationState,
    child: DelegationChild,
    *,
    hook_state: DelegationHookState,
    persist: Callable[[DelegationState], Awaitable[None]],
    config: Config,
    runtime_paths: RuntimePaths,
    result: str | None,
    error: BaseException | None = None,
) -> str | None:
    """Close the parent call with ``result``; the child's job keeps owning and settling the child."""
    _forget_child(state, child)
    await after_delegation(hook_state, config=config, runtime_paths=runtime_paths, result=result, error=error)
    await persist(state)
    return result


class _ParentCall(NamedTuple):
    """The parent run and exact tool call that receive a background child's result."""

    run_id: str
    team: bool
    call_id: str


async def _await_background_child(
    background: ToolJobRuntime,
    child: DelegationChild,
    *,
    fresh: bool,
    release_liveness: Callable[[], Awaitable[None]],
    operation: Callable[[], Awaitable[BackgroundOutcome]],
    cancel: Callable[[DelegationChild], Awaitable[None]],
    owner: ToolExecutionIdentity,
    depth: int,
    background_after: float | None,
    settle: Callable[[str], Awaitable[str]],
    record_return: Callable[[object], Awaitable[None]],
    parent_call: _ParentCall,
) -> str:
    """Hand a new child to its job, or find the job a crash left it with, then wait for it as the parent's call.

    Returns what the parent's call returns: the child's result, or its job handle while the job keeps working.
    ``settle`` closes the parent call with that text and returns it as the model reads it; ``record_return``
    records it on the reply.

    The parent's liveness claim ends once the job owns the child: the job claims liveness itself while it runs the
    child, and a parent claim held across the wait would block cancel cleanup.
    """
    claim = None
    retained = False
    try:
        if fresh:
            _, claim = await start_delegation(background, child, owner=owner, operation=operation, cancel=cancel)
        await release_liveness()
        waited = await background.wait(
            child.delegation_id,
            owner=owner,
            depth=depth,
            timeout=background_after,
            claim=claim,
            # A child awaiting a human decision holds its caller's reply no longer than a gated call's own wait does.
            approval_timeout=current_approval_wait_timeout(),
        )
    except JobAccessError as error:
        await background.release_wait(child.delegation_id, claim)
        if not background.has_job(child.delegation_id):
            # No job owns the child yet, so it settles as an ordinary child failure.
            raise
        # Revoked access or a closed runtime ends this wait; the parent's call reports it.
        settled = await settle(str(error))
        await record_return(str(error))
        return settled
    except BaseException:
        await background.release_wait(child.delegation_id, claim)
        raise
    try:
        result = await delegation_result(background, waited.job) if waited.claim is not None else None
        returned = result or format_job_handle(waited.job)
        settled = await settle(returned)
        await record_return(returned)
        if waited.claim is not None:
            # The outcome counts as read once the parent run saves this call's result, as a native call's does.
            await retain_parent_call_claim(
                background,
                child.delegation_id,
                waited.claim,
                run_id=parent_call.run_id,
                team=parent_call.team,
                call_id=parent_call.call_id,
            )
            retained = True
    finally:
        if not retained:
            await background.release_wait(child.delegation_id, waited.claim)
    return settled


async def _resolve_delegation_target(
    tool: ToolExecution,
    retained: DelegationChild | None,
    *,
    caller_identity: ToolExecutionIdentity,
    config: Config,
    runtime_paths: RuntimePaths,
    depth: int,
) -> _DelegationTarget | str:
    """Resolve fresh/follow-up arguments, returning a user-facing rejection if invalid."""
    args = tool.tool_args or {}
    if tool.tool_name == "continue_subagent":
        return await _resolve_follow_up_target(
            args,
            retained,
            caller_identity=caller_identity,
            config=config,
            runtime_paths=runtime_paths,
            depth=depth,
        )
    return _resolve_fresh_target(
        args,
        retained,
        caller_identity=caller_identity,
        config=config,
        runtime_paths=runtime_paths,
        depth=depth,
    )


async def _resolve_follow_up_target(
    args: dict[str, object],
    retained: DelegationChild | None,
    *,
    caller_identity: ToolExecutionIdentity,
    config: Config,
    runtime_paths: RuntimePaths,
    depth: int,
) -> _DelegationTarget | str:
    """Resolve a follow-up to its retained child, which keeps its model, mode, and persona."""
    subagent_id, task = args.get("subagent_id"), args.get("message")
    if not isinstance(subagent_id, str) or not isinstance(task, str):
        return "Cannot continue: subagent_id and message must be strings."
    try:
        previous_child = retained or await resolve_subagent(
            subagent_id,
            owner=caller_identity,
            config=config,
            runtime_paths=runtime_paths,
            depth=depth,
        )
    except SubagentSessionError as error:
        return str(error)
    if previous_child.subagent_id != subagent_id:
        msg = "Subagent ID no longer matches its retained requirement"
        raise RuntimeError(msg)
    refusal = retained is None and follow_up_refusal(
        previous_child.persona,
        caller_identity.agent_name,
        live_delegation_config(config),
        delegation_depth=depth,
    )
    if refusal:
        return refusal
    return _DelegationTarget(previous_child.child_agent_name, task, previous_child)


def _resolve_fresh_target(
    args: dict[str, object],
    retained: DelegationChild | None,
    *,
    caller_identity: ToolExecutionIdentity,
    config: Config,
    runtime_paths: RuntimePaths,
    depth: int,
) -> _DelegationTarget | str:
    """Resolve a fresh child and any persona it authors; a started child keeps its frozen persona."""
    child_name, task, minimal = args.get("agent_name"), args.get("task"), args.get("minimal")
    model = cast("str | None", args.get("model"))
    if child_name is None:
        child_name = caller_identity.agent_name
    if minimal is not None and not isinstance(minimal, bool):
        return "Cannot delegate: minimal must be a boolean or null."
    if not isinstance(child_name, str) or not isinstance(task, str):
        return "Cannot delegate: task must be a string and agent_name must be a string or null."
    if retained is not None:
        # A resumed call never rereads a profile the worker could have changed.
        return _DelegationTarget(child_name, task, model=model)

    def caller_workspace_root() -> Path | None:
        workspace = resolve_agent_runtime(
            caller_identity.agent_name,
            config,
            runtime_paths,
            execution_identity=caller_identity,
        ).workspace
        return workspace.root if workspace is not None else None

    request = resolve_persona_request(
        caller_name=caller_identity.agent_name,
        agent_name=child_name,
        system_prompt=args.get("system_prompt"),
        tools=args.get("tools"),
        skills=args.get("skills"),
        knowledge=args.get("knowledge"),
        profile=args.get("profile"),
        model=model,
        minimal=bool(minimal),
        workspace_root=caller_workspace_root() if args.get("profile") is not None else None,
        # The copy runs one level deeper, where delegate may no longer be offered.
        available_toolkits=lambda: caller_toolkit_names(
            caller_identity.agent_name,
            live_delegation_config(config),
            delegation_depth=depth + 1,
        ),
        available_skills=lambda: caller_skill_names(
            caller_identity.agent_name,
            live_delegation_config(config),
            runtime_paths,
            caller_workspace_root(),
        ),
        available_knowledge=lambda: caller_knowledge_names(caller_identity.agent_name, live_delegation_config(config)),
        cap=context.persona if (context := get_tool_runtime_context()) is not None else None,
    )
    if isinstance(request, str):
        return request
    return _DelegationTarget(child_name, task, None, request.agent_mode, request.persona, request.model)


def _validate_child_scope(
    child: DelegationChild,
    target: _DelegationTarget,
    *,
    caller_identity: ToolExecutionIdentity,
    config: Config,
    depth: int,
) -> None:
    """Reject retained children whose storage, owner, or parent requirement changed."""
    if child.storage_bindings != freeze_delegation_storage(config, child.storage_bindings):
        msg = "Delegation storage scope changed while awaiting approval"
        raise RuntimeError(msg)
    if replace(child_execution_identity(child), thread_id=caller_identity.resolved_thread_id) != replace(
        caller_identity,
        thread_id=caller_identity.resolved_thread_id,
        agent_name=child.child_agent_name,
        session_id=child.session_id,
    ):
        msg = "Delegation child scope no longer matches its parent execution identity"
        raise RuntimeError(msg)
    if (child.caller_agent_name, child.child_agent_name, child.task, child.depth) != (
        caller_identity.agent_name,
        target.agent_name,
        target.task,
        depth + 1,
    ):
        msg = "Delegation child no longer matches its parent requirement"
        raise RuntimeError(msg)


async def advance_delegation_call(  # noqa: C901, PLR0911, PLR0912, PLR0915
    requirement: RunRequirement,
    *,
    response: AnyRunRecord,
    state: DelegationState,
    previous_state: DelegationState,
    persist: Callable[[DelegationState], Awaitable[None]],
    agent_name: str,
    run_child: ChildResponseRunner,
    config: Config,
    runtime_paths: RuntimePaths,
    execution_identity: ToolExecutionIdentity,
    delegation_depth: int = 0,
    refresh_scheduler: KnowledgeRefreshScheduler | None = None,
    decisions: dict[str, bool] | None = None,
    denial_reasons: dict[str, str | None] | None = None,
    approval_calls: Sequence[ApprovalCall] = (),
    on_event: ResumeEvents | None = None,
    background: ToolJobRuntime | None = None,
) -> str | None:
    """Advance one existing child operation; return the parent call's result, or None while it waits for approval.

    The native driver owns parent continuation. Hidden callers supply a sink
    that saves the real parent without adding a provider requirement or message.
    A managed `background` runtime owns children this call starts or continues.
    """
    if execution_identity.channel != "matrix":
        msg = "Native delegation requires a Matrix execution owner"
        raise RuntimeError(msg)
    # Bind this exact parent call and its presentation before resolving the target.
    pending_id = previous_state.pending_child_id
    prior_pending_tools = previous_state.pending_tools
    prior_tool_sources = previous_state.pending_tool_sources
    tool = requirement.tool_execution
    if not tool.tool_call_id:
        msg = "Delegation requirement has no exact tool-call identity"
        raise RuntimeError(msg)
    requirement_key = delegation_card_id(requirement.member_agent_id, tool.tool_call_id)
    if on_event is not None:
        on_event(ToolStarted(deepcopy(tool)))
    # A team member's id is its config name on the owned loop, so its delegations run as that member.
    caller = agent_name if requirement.member_agent_id is None else requirement.member_agent_id
    caller_identity = replace(execution_identity, agent_name=caller)
    resolve_result = partial(_resolve_delegation_requirement, requirement, on_event=on_event)
    retained = next((item for item in state.children if item.parent_requirement_id == requirement.id), None)
    # What the decisions answer for this call: its own card, or the calls of the child it waits on, which go to that
    # child under its own call ids.
    card_decision = None if decisions is None else decisions.get(requirement_key)
    child_decisions = None
    child_reasons = None
    child_calls: tuple[ApprovalCall, ...] = ()
    if decisions is not None and retained is not None and retained.delegation_id == pending_id:
        delegation_id = retained.delegation_id
        child_decisions = {child_call_id(delegation_id, key): value for key, value in decisions.items()}
        child_reasons = {child_call_id(delegation_id, key): value for key, value in (denial_reasons or {}).items()}
        child_calls = tuple(
            replace(call, tool_call_id=child_call_id(delegation_id, call.tool_call_id)) for call in approval_calls
        )
    if card_decision is not None or child_decisions is not None:
        # The run that made the call continues with its result, so that run reads the approval's receipt.
        add_approval_receipt_reader(requirement.member_run_id or response.run_id)
    excluded = toolkit_is_background_excluded("delegate", config, runtime_paths)
    # A child saved before a crash keeps its accepted owner when startup policy changes.
    if retained is not None:
        if background is not None and not background.has_job(retained.delegation_id):
            background = None
    elif excluded:
        # An excluded delegate toolkit runs its child natively, outside the job runtime.
        background = None
        if BACKGROUND_AFTER in (tool.tool_args or {}):
            tool.tool_call_error = True
            return resolve_result(f"{BACKGROUND_AFTER} is unavailable for the excluded delegate toolkit.")
    try:
        background_after = read_background_after(
            tool.tool_args,
            owned_execution=job_owns_execution() or delegation_depth > 0,
        )
    except ValueError as error:
        tool.tool_call_error = True
        return resolve_result(str(error))
    args = application_arguments(tool.tool_args) or {}
    target = await _resolve_delegation_target(
        tool,
        retained,
        caller_identity=caller_identity,
        config=config,
        runtime_paths=runtime_paths,
        depth=delegation_depth,
    )
    if isinstance(target, str):
        return resolve_result(target)
    model = target.model

    # Recheck current authorization and output policy, including resumed calls.
    authorization = authorize_delegation(
        caller,
        target.agent_name,
        target.task,
        config=config,
        runtime_paths=runtime_paths,
        execution_identity=caller_identity,
        depth=delegation_depth,
        model=model,
    )
    output_request = None
    if not isinstance(authorization, str):
        prepared_output = _prepare_delegation_output(
            caller,
            config,
            runtime_paths,
            caller_identity,
            args.get(OUTPUT_PATH_ARGUMENT),
            tool_name=tool.tool_name or "run_subagent",
        )
        if isinstance(prepared_output, dict):
            authorization = json.dumps(prepared_output)
        else:
            output_request = prepared_output
    if isinstance(authorization, str):
        if retained is not None:
            if background is not None:
                # The child's job keeps settling it; this parent call only stops waiting for it.
                _forget_child(state, retained)
            else:
                await interrupt_child(
                    retained,
                    config=config,
                    runtime_paths=runtime_paths,
                    reason=authorization,
                )
            if pending_id == retained.delegation_id and on_event is not None:
                _settle_pending_child_tools(prior_pending_tools, on_event, reason=authorization)
        result = resolve_result(authorization)
        if requirement.id in state.hooks:
            await after_delegation(
                state.hooks[requirement.id],
                config=config,
                runtime_paths=runtime_paths,
                result=authorization,
            )
        return result
    if output_request is not None:
        resolve_result = partial(resolve_result, output_request=output_request)

    # Approval and the before hook must settle before a child can start.
    gated = tool_may_require_approval(config, tool.tool_name or "run_subagent")
    approved_here = card_decision is True
    if gated and retained is None and not approved_here and state.gates.get(requirement_key) is not False:
        # Saved gates live in session storage that worker code can write, so only an approval
        # consumed by this continuation can start a fresh child; otherwise ask again.
        state.gates.pop(requirement_key, None)
    if gated and requirement_key not in state.gates:
        projected = deepcopy(tool)
        projected.external_execution_required = False
        projected.requires_confirmation = True
        projected.approval_type = POLICY_CONFIRMATION_APPROVAL_TYPE
        projected.tool_call_id = requirement_key
        state.pending_tools = [projected.to_dict()]
        state.pending_requirements = [RunRequirement(projected).to_dict()]
        state.pending_agent_name = caller
        await persist(state)
        return None
    if state.gates.get(requirement_key) is False:
        return resolve_result("Delegation denied by requester; child was not executed.")
    if card_decision is not None:
        # The approved gate card shows the projected call; the child starts from this separate requirement.
        call = next((call for call in approval_calls if call.tool_call_id == requirement_key), None)
        if call is None or not call.binds_arguments(tool.tool_args):
            msg = "Saved delegation approval no longer matches its pending arguments; retry the request"
            raise RuntimeError(msg)
    # Hook records live in storage worker code can write, so only a started child may reuse its stored gate.
    if retained is None or requirement.id not in state.hooks:
        state.hooks[requirement.id] = await before_delegation(
            execution_identity=caller_identity,
            arguments=args,
            tool_name=tool.tool_name or "run_subagent",
            config=config,
            runtime_paths=runtime_paths,
        )
        await persist(state)
    hook_state = state.hooks[requirement.id]
    if hook_state.blocked_result is not None:
        blocked_result = resolve_result(hook_state.blocked_result)
        await after_delegation(
            hook_state,
            config=config,
            runtime_paths=runtime_paths,
            result=blocked_result,
        )
        await persist(state)
        return blocked_result

    child = retained
    fresh = child is None
    if child is None:
        child = prepare_child_turn(
            caller,
            target.agent_name,
            target.task,
            owner=execution_identity,
            config=config,
            runtime_paths=runtime_paths,
            depth=delegation_depth,
            budget_monitor=current_budget_monitor(),
            model=model,
            agent_mode=target.agent_mode,
            previous=target.previous_child,
            parent_tool_call_id=tool.tool_call_id,
            parent_requirement_id=requirement.id,
            persona=target.persona,
        )
        state.children.append(child)
    detach = partial(
        _detach_from_job,
        state,
        child,
        hook_state=hook_state,
        persist=persist,
        config=config,
        runtime_paths=runtime_paths,
    )
    # The liveness claim spans the child's run or job admission and its settlement, so recovery never takes the
    # child mid-failure. A managed wait releases it: the job claims liveness itself while it runs the child, and a
    # parent claim held across the wait would block cancel cleanup.
    async with AsyncExitStack() as liveness:
        await liveness.enter_async_context(subagent_liveness(child, runtime_paths))
        if child.result is None or background is not None:
            # Scope violations must propagate; they are not ordinary child failures.
            _validate_child_scope(child, target, caller_identity=caller_identity, config=config, depth=delegation_depth)
            try:
                # The reply records a background child's call before its job starts, as for any managed call: the
                # child's own tools run unrecorded in its job, so a restart's account names the delegation.
                record_return = (
                    await record_call(tool.tool_name or "run_subagent", args) if background is not None else None
                )
                if fresh:
                    await start_child_turn(
                        child,
                        parent_run_id=response.run_id,
                        parent_delegation_id=_RUNNING_CHILD_ID.get(),
                        config=config,
                        runtime_paths=runtime_paths,
                        caller_execution_identity=caller_identity,
                    )
                    # Persist the child before execution, with startup covered by cleanup.
                    await persist(state)
                if background is not None:
                    assert record_return is not None
                    return await _await_background_child(
                        background,
                        child,
                        fresh=fresh,
                        release_liveness=liveness.aclose,
                        operation=partial(
                            _background_child_outcome,
                            child,
                            owner=caller_identity,
                            run_child=run_child,
                            config=authorization,
                            runtime_paths=runtime_paths,
                            refresh_scheduler=refresh_scheduler,
                            output_request=output_request,
                        ),
                        cancel=partial(interrupt_stopped_child, config=config, runtime_paths=runtime_paths),
                        owner=caller_identity,
                        depth=delegation_depth,
                        background_after=background_after,
                        parent_call=_ParentCall(
                            run_id=response.run_id,
                            team=not isinstance(response, RunRecord),
                            call_id=tool.tool_call_id,
                        ),
                        # The job already applied the output policy to the child's result.
                        settle=lambda result: detach(result=resolve_result(result, output_request=None)),
                        record_return=record_return,
                    )
                child_outcome = await _run_child(
                    child,
                    run_child=run_child,
                    config=authorization,
                    runtime_paths=runtime_paths,
                    caller_identity=caller_identity,
                    refresh_scheduler=refresh_scheduler,
                    decisions=child_decisions,
                    denial_reasons=child_reasons,
                    approval_calls=child_calls,
                    fresh=fresh,
                )
                if child_decisions is not None and on_event is not None:
                    await _complete_approved_child_tools(
                        prior_pending_tools,
                        prior_tool_sources,
                        on_event,
                        config=config,
                        runtime_paths=runtime_paths,
                    )
                if child_outcome.response.status is RunStatus.paused:
                    _pending_child(state, child, child_outcome)
                    await persist(state)
                    return None
            except asyncio.CancelledError:
                if background is not None and background.has_job(child.delegation_id):
                    await detach(
                        result=None,
                        error=asyncio.CancelledError(
                            "Delegation wait cancelled; accepted child remains owned by its job.",
                        ),
                    )
                    raise
                await interrupt_stopped_child(child, config=config, runtime_paths=runtime_paths)
                await after_delegation(
                    hook_state,
                    config=config,
                    runtime_paths=runtime_paths,
                    result=child.result,
                    error=asyncio.CancelledError("Delegation cancelled."),
                )
                await persist(state)
                raise
            except Exception as error:
                if background is not None and background.has_job(child.delegation_id):
                    await detach(result=None, error=error)
                    raise
                # The cause reaches the parent model and the child's record, so credentials in it are redacted.
                reason = redact_sensitive_text(str(error))
                await interrupt_child(
                    child,
                    config=config,
                    runtime_paths=runtime_paths,
                    reason=reason,
                    status="failed",
                )
                if child_decisions is not None and on_event is not None:
                    _settle_pending_child_tools(prior_pending_tools, on_event, reason=reason)
            await persist(state)

        # Completed and failed children both close the parent call and its after hook.
        receipt = await finish_child_turn(child, config=config, runtime_paths=runtime_paths)
    result = resolve_result(_child_result_text(child, receipt))
    await after_delegation(
        hook_state,
        config=config,
        runtime_paths=runtime_paths,
        result=result,
    )
    return result


def prepare_delegation_state(
    response: AnyRunRecord,
    *,
    agent_name: str,
    config: Config,
    decisions: dict[str, bool] | None,
    denial_reasons: dict[str, str | None] | None,
    approval_calls: Sequence[ApprovalCall] = (),
) -> tuple[DelegationState, DelegationState, ResumeDecisions]:
    """Consume exact approval decisions and return the parent's state, its pre-decision snapshot, and its own decisions.

    Decisions on the parent's own calls, asked about after its delegations, resume the parent; decisions on a
    delegation's gate are kept as gates for the call they name.
    """
    from mindroom.response_turn import apply_exact_approval_decisions  # noqa: PLC0415

    state = DelegationState.from_metadata(response.metadata)
    if not state.storage_bindings and isinstance(response, RunRecord):
        state.storage_bindings = freeze_delegation_storage(config, (agent_name,))
    previous_state = deepcopy(state)
    own = ResumeDecisions()
    if decisions is not None:
        # Validate every presented identity before touching any child or gate.
        apply_exact_approval_decisions(
            [RunRequirement.from_dict(item) for item in state.pending_requirements],
            decisions=decisions,
            denial_reasons=denial_reasons or {},
        )
        if state.pending_child_id is None:
            ordinary = [item for item in response.requirements or () if item.needs_confirmation]
            ordinary_ids = {item.tool_execution.tool_call_id for item in ordinary}
            if set(decisions) == ordinary_ids:
                own = approval_decisions(approval_calls, decisions, denial_reasons or {})
            else:
                state.gates.update(decisions)
    state.clear_pending()
    return state, previous_state, own


async def _drive_delegations[R: AnyRunRecord](
    response: R,
    *,
    store: ConversationStore | None,
    session: ConversationSession,
    continue_parent: ContinueRun[R],
    agent_name: str,
    run_child: ChildResponseRunner,
    config: Config,
    runtime_paths: RuntimePaths,
    execution_identity: ToolExecutionIdentity | None,
    delegation_depth: int = 0,
    refresh_scheduler: KnowledgeRefreshScheduler | None = None,
    decisions: dict[str, bool] | None = None,
    denial_reasons: dict[str, str | None] | None = None,
    approval_calls: Sequence[ApprovalCall] = (),
    on_event: ResumeEvents | None = None,
) -> R:
    """Advance one durable parent; sequential children preserve completed sibling results.

    ``continue_parent`` resumes the parent with the decisions on its own calls and its children's results, and
    returns its new terminal record; the loop behind it refuses anything but exact coverage of the calls the parent
    waits for. ``store`` and ``session`` are where the parent's row lives.
    """
    if not has_delegation_state(response):
        return response
    if execution_identity is None or execution_identity.channel != "matrix":
        msg = "Native delegation requires a Matrix execution owner"
        raise RuntimeError(msg)
    state, previous_state, own = prepare_delegation_state(
        response,
        agent_name=agent_name,
        config=config,
        decisions=decisions,
        denial_reasons=denial_reasons,
        approval_calls=approval_calls,
    )
    background = get_background_runtime(runtime_paths) if delegation_depth == 0 and not job_owns_execution() else None
    while response.status is RunStatus.paused:
        external = _external_requirements(response)
        # Worker code can write the stored run, and one approval binds a call ID, so a copied call must not start twice.
        call_keys = [(item.member_agent_id, item.tool_execution.tool_call_id) for item in external]
        if len(set(call_keys)) < len(call_keys):
            msg = "Paused run holds the same delegation call more than once; retry the request"
            raise RuntimeError(msg)
        # The parent's own calls this resume decided are answered by its continuation, not asked about again.
        decided = {call.tool_call_id for call in own.approved} | set(own.denied)
        ordinary = [
            requirement
            for requirement in response.requirements or ()
            if requirement.needs_confirmation and requirement.tool_execution.tool_call_id not in decided
        ]
        if (
            not external
            and not ordinary
            and any(
                not item.is_resolved() and item.tool_execution.tool_call_id not in decided
                for item in response.requirements or ()
            )
        ):
            return response
        persist = partial(persist_delegation_state, store, session, response)
        # Calls answered on an earlier entry keep their answers; only the calls still waiting advance.
        for requirement in external:
            result = await advance_delegation_call(
                requirement,
                response=response,
                state=state,
                previous_state=previous_state,
                persist=persist,
                agent_name=agent_name,
                run_child=run_child,
                config=config,
                runtime_paths=runtime_paths,
                execution_identity=execution_identity,
                delegation_depth=delegation_depth,
                refresh_scheduler=refresh_scheduler,
                decisions=decisions,
                denial_reasons=denial_reasons,
                approval_calls=approval_calls,
                on_event=on_event,
                background=background,
            )
            if result is None:
                return response
        # Ask for ordinary calls last, so the approval that covers them belongs to the continuation that runs them.
        if ordinary:
            state.pending_requirements = [requirement.to_dict() for requirement in ordinary]
            state.pending_tools = [requirement.tool_execution.to_dict() for requirement in ordinary]
            await persist(state)
            return response
        await persist(state)
        response = await continue_parent(
            _awaiting_answers(response),
            replace(own, external_results=answered_delegation_calls(response)),
            on_event,
        )
        own = ResumeDecisions()
        state.clear_pending()
        await persist_delegation_state(store, session, response, state)
    return response


async def drive_delegation_stream[R: AnyRunRecord](
    events: AsyncIterator[RunEvent],
    *,
    store: ConversationStore | None,
    session: ConversationSession,
    continue_parent: ContinueRun[R],
    **options: Unpack[_DelegationOptions],
) -> AsyncIterator[RunEvent]:
    """Pass a run's events through; when it ends paused on delegations, drive them and end with the driven record.

    The driven record is paused when a child waits for approval or the parent's own calls do, else the parent's
    continuation ended it.
    """
    record: AnyRunRecord | None = None
    async with closing_async_stream(events):
        async for event in events:
            if isinstance(event, RunFinished):
                record = event.record
            else:
                yield event
    if record is None:
        return
    if record.status is not RunStatus.paused or not has_delegation_state(record):
        yield RunFinished(record)
        return

    paused = cast("R", record)

    async def drive(on_event: Callable[[object], None]) -> R:
        return await _drive_delegations(
            paused,
            store=store,
            session=session,
            continue_parent=continue_parent,
            on_event=on_event,
            **options,
        )

    driven: AnyRunRecord = paused
    driven_stream = callback_event_stream(drive)
    async with closing_async_stream(driven_stream):
        async for event in driven_stream:
            if isinstance(event, RunRecord | TeamRunRecord):
                driven = event
            else:
                yield cast("RunEvent", event)
    yield RunFinished(driven)

"""Own child execution outcomes and publish their runtime and audit projections."""

from __future__ import annotations

from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Literal
from uuid import uuid4

from mindroom.authorization import is_sender_allowed_for_responder
from mindroom.background_tasks import run_coroutine_until_complete
from mindroom.budgets.monitor import budget_model
from mindroom.delegation.audit import (
    child_audit_context,
    child_response_usage,
    finish_child_record,
    record_child_response,
    start_child_record,
)
from mindroom.delegation.audit import observe_child_event as record_child_event
from mindroom.delegation.personas import caller_toolkit_names, self_only_refusal
from mindroom.delegation.records import DelegationRecordLimitError
from mindroom.delegation.sessions import reserve_subagent_turn, update_subagent_turn, update_subagent_turn_sync
from mindroom.delegation.state import TERMINAL_CHILD_STATUSES, DelegationChild
from mindroom.delegation.storage import freeze_delegation_storage
from mindroom.llm.events import ContentDelta, RunFinished, ToolStarted
from mindroom.llm.runs import RunRecord, RunStatus
from mindroom.requester_identity import is_access_checked_requester_id
from mindroom.tool_system.progress import cap_progress, report_tool_progress
from mindroom.tool_system.runtime_context import get_detached_requester_context, get_tool_runtime_context
from mindroom.tool_system.worker_routing import parse_tool_execution_identity_payload, serialize_tool_execution_identity

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Mapping, Sequence

    from mindroom.agent_modes import AgentMode
    from mindroom.budgets.monitor import BudgetMonitor
    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.delegation.state import DelegationTerminalStatus, SubagentPersona
    from mindroom.llm.events import RunEvent
    from mindroom.tool_system.runtime_context import ToolRuntimeContext
    from mindroom.tool_system.worker_routing import ToolExecutionIdentity

MAX_DELEGATION_DEPTH = 3

type _DelegationGrant = Literal["delegate", "dynamic_workflow"]


@dataclass
class _ChildRunObservation:
    """Exact terminal evidence collected while one child response envelope runs.

    ``response`` is the terminal record of the child's latest run: the record the run ended with when it ran in this
    context, which also carries what storage never holds, such as a failure's ``error``, else the outcome the envelope
    read back from storage. A child that is cancelled leaves no record here: the loop stores its terminal row, which
    the interruption reads, and the cancellation propagates to the driver.
    """

    child: DelegationChild
    parent: _ChildRunObservation | None = None
    response: RunRecord | None = None
    # The tail of what the child said and which tools it called, reported as its caller's progress.
    activity: str = ""

    def keep_outcome(self, record: RunRecord) -> None:
        """Keep the outcome the envelope returned, unless this context already holds the record that run ended with.

        The envelope reads a run it started back from storage, and the stored copy lacks what is never stored.
        """
        if self.response is None or self.response.run_id != record.run_id:
            self.response = record


_CHILD_RUN: ContextVar[_ChildRunObservation | None] = ContextVar("delegation_child_run", default=None)


def running_child_id() -> str | None:
    """Return the delegation id of the child turn running in this context, or None outside one."""
    observation = _CHILD_RUN.get()
    return None if observation is None else observation.child.delegation_id


def active_delegation_edges(owner: ToolExecutionIdentity) -> tuple[tuple[str, str, _DelegationGrant], ...]:
    """Return the exact active native ancestry for a retained leaf execution owner, with each edge's grant."""
    observation = _CHILD_RUN.get()
    if observation is None or child_execution_identity(observation.child) != owner:
        return ()
    edges = []
    while observation is not None:
        child = observation.child
        edges.append((child.caller_agent_name, child.child_agent_name, delegation_grant(child)))
        observation = observation.parent
    return tuple(reversed(edges))


def child_execution_identity(child: DelegationChild) -> ToolExecutionIdentity:
    """Validate the retained session and agent before resolving any child resource."""
    identity = parse_tool_execution_identity_payload(child.execution_identity, strict=True)
    if identity is None or identity.agent_name != child.child_agent_name or identity.session_id != child.session_id:
        msg = "Delegation child execution identity does not match its retained wait"
        raise RuntimeError(msg)
    return identity


def child_tool_runtime_context(context: ToolRuntimeContext, child: DelegationChild) -> ToolRuntimeContext:
    """Rebind a caller's tool context to one child turn, carrying its persona as the cap on copies it authors."""
    return replace(
        context,
        agent_name=child.child_agent_name,
        active_model_name=child.model_name,
        target=replace(context.target, session_id=child.session_id),
        persona=child.persona,
    )


async def reserve_child_turn(
    child: DelegationChild,
    *,
    owner: ToolExecutionIdentity,
    runtime_paths: RuntimePaths,
) -> None:
    """Claim a turn and adopt an exact retained attempt if its parent snapshot is stale."""
    retained = await reserve_subagent_turn(child, owner=owner, runtime_paths=runtime_paths)
    if retained is not None:
        child.run_id = retained.run_id
        child.model_name = retained.model_name


def note_child_run_id(
    child: DelegationChild,
    run_id: str,
    runtime_paths: RuntimePaths,
    *,
    model_name: str | None = None,
) -> None:
    """Publish the exact attempt and bound model before it can execute tools."""
    child.run_id = run_id
    context = get_tool_runtime_context()
    if model_name is None and context is not None:
        model_name = context.active_model_name
    if model_name is not None and model_name != child.model_name:
        # A model switch during the run is what the child's follow-ups ask for next.
        child.requested_model_name = model_name
        child.model_name = model_name
    update_subagent_turn_sync(child, runtime_paths)


async def start_child_turn(
    child: DelegationChild,
    *,
    parent_run_id: str | None,
    config: Config,
    runtime_paths: RuntimePaths,
    caller_execution_identity: ToolExecutionIdentity | None,
    parent_delegation_id: str | None = None,
) -> None:
    """Create and bind the audit record before startup cancellation can propagate."""

    async def create_record() -> None:
        locator = await start_child_record(
            child,
            parent_run_id=parent_run_id,
            config=config,
            runtime_paths=runtime_paths,
            caller_execution_identity=caller_execution_identity,
            parent_delegation_id=parent_delegation_id,
        )
        child.record_locator = locator.to_dict()

    await run_coroutine_until_complete(create_record())


async def settle_child_response(
    child: DelegationChild,
    response: RunRecord,
    *,
    config: Config,
    runtime_paths: RuntimePaths,
    decisions: Mapping[str, bool] | None = None,
    denial_reasons: Mapping[str, str | None] | None = None,
) -> None:
    """Derive operational state from one exact run, then publish its audit view.

    A finished run's result is what it hands over (``handover_content``), so a failed run's is why it failed.
    """
    if response.run_id != child.run_id or response.session_id != child.session_id:
        msg = "Delegation child response does not match its retained run"
        raise ValueError(msg)
    # TODO(simplify): A resumed child that wrote nothing hands over its carried pause notice ("I have tools to
    # execute, but I need confirmation.") after its call ran, as main did. Hand over only what the resume added
    # (``new_handover_content``) or the completion note below. The delegating parent's model would notice.
    content = response.handover_content
    if response.status == RunStatus.completed:
        child.status = "completed"
        child.result = str(content or "Agent completed the task but returned no content.")
    elif response.status == RunStatus.cancelled:
        child.status = "cancelled"
        child.result = str(content or "Delegation cancelled.")
    elif response.status in {RunStatus.error, RunStatus.regenerated}:
        child.status = "failed"
        child.result = str(content or response.status)
    else:
        child.status = "paused" if response.status == RunStatus.paused else "running"
        child.result = None
    await update_subagent_turn(child, runtime_paths)
    try:
        usage = await record_child_response(
            child,
            response,
            config=config,
            runtime_paths=runtime_paths,
            decisions=decisions,
            denial_reasons=denial_reasons,
        )
    except DelegationRecordLimitError:
        if child.status not in TERMINAL_CHILD_STATUSES:
            raise
        # A full record keeps room for its terminal event, so settle it without the remaining ordinary events.
        usage = child_response_usage(response)
    if child.status in TERMINAL_CHILD_STATUSES:
        await finish_child_record(child, config=config, runtime_paths=runtime_paths, usage=usage)


async def finish_child_turn(
    child: DelegationChild,
    *,
    config: Config,
    runtime_paths: RuntimePaths,
    status: DelegationTerminalStatus | None = None,
    reason: str | None = None,
) -> str:
    """Settle an interruption without overwriting an already retained terminal outcome."""
    if status is not None and child.status not in TERMINAL_CHILD_STATUSES:
        child.status = status
        child.result = reason
    if child.status not in TERMINAL_CHILD_STATUSES:
        msg = f"Cannot finish active delegation: {child.delegation_id}"
        raise ValueError(msg)
    await update_subagent_turn(child, runtime_paths)
    if not child.record_locator:
        return ""
    return await finish_child_record(child, config=config, runtime_paths=runtime_paths)


@asynccontextmanager
async def child_run_context(
    child: DelegationChild,
    *,
    config: Config,
    runtime_paths: RuntimePaths,
) -> AsyncIterator[_ChildRunObservation]:
    """Observe one child attempt and settle it independently of audit event capture."""
    observation = _ChildRunObservation(child, parent=_CHILD_RUN.get())
    token = _CHILD_RUN.set(observation)
    try:
        async with child_audit_context(child, config=config, runtime_paths=runtime_paths):
            yield observation
    finally:
        try:
            response = observation.response
            if response is not None and response.run_id == child.run_id:
                await settle_child_response(child, response, config=config, runtime_paths=runtime_paths)
        finally:
            _CHILD_RUN.reset(token)


def _report_child_activity(observation: _ChildRunObservation, event: ContentDelta | ToolStarted) -> None:
    text = event.text if isinstance(event, ContentDelta) else f"\nCalling {event.tool.tool_name}\n"
    if text:
        observation.activity = cap_progress(observation.activity + text)
        report_tool_progress(observation.activity)


async def observe_child_event(event: RunEvent) -> None:
    """Retain execution evidence and report the child's activity before forwarding the event to the audit adapter.

    The loop's events carry no run identity; they are the events of the child turn running in this context, whose
    terminal record names the run it ended.
    """
    observation = _CHILD_RUN.get()
    if observation is not None and isinstance(event, (ContentDelta, ToolStarted)):
        _report_child_activity(observation, event)
    if isinstance(event, RunFinished) and isinstance(event.record, RunRecord):
        observe_child_record(event.record)
    await record_child_event(event)


def observe_child_record(record: RunRecord) -> None:
    """Retain the terminal record of the child turn running in this context, as the loop stored it.

    A completed or paused record arrives with ``RunFinished``; a run that failed stores its ``ERROR`` record and
    raises instead, so the turn that caught the failure hands the record over here.
    """
    observation = _CHILD_RUN.get()
    if observation is None:
        return
    child = observation.child
    if record.run_id == child.run_id and record.session_id == child.session_id:
        observation.response = record


def delegation_grant(child: DelegationChild) -> _DelegationGrant:
    """Name the caller permission that authorizes this child: its delegate allowlist or its workflow tool."""
    return "dynamic_workflow" if child.persona is not None and child.persona.source_kind == "workflow" else "delegate"


def caller_grants_child(caller_name: str, agent_name: str, config: Config, grant: _DelegationGrant) -> bool:
    """Return whether the caller's current config still grants this child, by delegation or its workflow tool."""
    caller = config.agents.get(caller_name)
    if caller is None:
        return False
    if grant == "delegate":
        return agent_name in caller.delegate_to
    return agent_name == caller_name and "dynamic_workflow" in caller_toolkit_names(
        caller_name,
        config,
        delegation_depth=0,
    )


def _target_refusal(
    caller_name: str,
    agent_name: str,
    task: str,
    *,
    config: Config,
    allowed_targets: Sequence[str] | None,
    grant: _DelegationGrant,
) -> str | None:
    """Return why this caller cannot start this target at all, before any requester check."""
    if not task or not task.strip():
        return "Cannot delegate an empty task. Please provide a task description."
    if grant == "dynamic_workflow":
        if agent_name == caller_name:
            return None
        return self_only_refusal(agent_name)
    if allowed_targets is None:
        caller = config.agents.get(caller_name)
        allowed_targets = caller.delegate_to if caller is not None else []
    if agent_name in allowed_targets:
        return None
    return f"Cannot delegate to '{agent_name}'. Allowed subagents: {', '.join(allowed_targets)}."


def live_delegation_config(config: Config) -> Config:
    """Return the live config that authorization and execution use, not a toolkit's or response's snapshot."""
    context = get_tool_runtime_context()
    if context is not None:
        return context.current_config
    detached = get_detached_requester_context()
    live = detached.config_provider() if detached is not None else None
    return live if live is not None else config


def authorize_delegation(  # noqa: PLR0911
    caller_name: str,
    agent_name: str,
    task: str,
    *,
    config: Config,
    runtime_paths: RuntimePaths,
    execution_identity: ToolExecutionIdentity | None,
    depth: int,
    allowed_targets: Sequence[str] | None = None,
    model: str | None = None,
    grant: _DelegationGrant = "delegate",
) -> Config | str:
    """Recheck the caller's current grant and requester authority, returning the live config."""
    refusal = _target_refusal(
        caller_name,
        agent_name,
        task,
        config=config,
        allowed_targets=allowed_targets,
        grant=grant,
    )
    if refusal is not None:
        return refusal

    runtime_context = get_tool_runtime_context()
    detached_context = get_detached_requester_context()
    if runtime_context is not None:
        active_config = runtime_context.current_config
        requester_id = runtime_context.requester_id
        authorization_room_id = runtime_context.room_id
        membership_index = runtime_context.require_agent_reply_memberships()
    elif (
        detached_context is not None
        and execution_identity is not None
        and execution_identity.channel == "openai_compat"
        and execution_identity.requester_id == detached_context.requester_id
        and runtime_paths == detached_context.runtime_paths
    ):
        active_config = detached_context.config_provider()
        requester_id = detached_context.requester_id
        authorization_room_id = None
        membership_index = detached_context.agent_reply_memberships
    else:
        return f"Cannot delegate to '{agent_name}': requester authorization is unavailable."
    if active_config is None or agent_name not in active_config.agents:
        return f"Cannot delegate to '{agent_name}': that agent is not allowed to reply to you."
    caller_allows_target = caller_grants_child(caller_name, agent_name, active_config, grant)
    # A workflow participant is the caller's own copy inside the caller's tool call, so the requester
    # the caller already serves, possibly only through a team's access, keeps that access.
    if not caller_allows_target or (
        grant == "delegate"
        and not is_sender_allowed_for_responder(
            requester_id,
            agent_name,
            authorization_room_id,
            active_config,
            runtime_paths,
            membership_index,
        )
    ):
        reason = (
            "it is no longer an allowed target"
            if not caller_allows_target
            else "that agent is not allowed to reply to you"
        )
        return f"Cannot delegate to '{agent_name}': {reason}."
    if active_config.agents[agent_name].private is not None and not is_access_checked_requester_id(
        requester_id,
        active_config,
        runtime_paths,
    ):
        return f"Cannot delegate to '{agent_name}': a private agent runs only for a person or bot account."

    if depth >= MAX_DELEGATION_DEPTH:
        return "Cannot delegate: the maximum delegation depth was reached."
    if model is not None and (not isinstance(model, str) or model not in active_config.models):
        available_models = ", ".join(sorted(active_config.models))
        return f"Cannot delegate: Unknown model '{model}'. Available models: {available_models}."
    return active_config


def prepare_child_turn(
    caller_name: str,
    agent_name: str,
    task: str,
    *,
    owner: ToolExecutionIdentity,
    config: Config,
    runtime_paths: RuntimePaths,
    depth: int,
    budget_monitor: BudgetMonitor | None,
    model: str | None = None,
    agent_mode: AgentMode = "standard",
    previous: DelegationChild | None = None,
    parent_tool_call_id: str = "",
    parent_requirement_id: str = "",
    persona: SubagentPersona | None = None,
) -> DelegationChild:
    """Prepare the same scoped fresh/follow-up turn for direct and native callers.

    A follow-up keeps the mode and persona of the child it continues, and its requested model, checked again against the owner's budget.
    """
    delegation_id = uuid4().hex
    session_id = previous.session_id if previous is not None else f"delegate:{caller_name}:{agent_name}:{delegation_id}"
    identity = (
        child_execution_identity(previous)
        if previous is not None
        else replace(
            owner,
            agent_name=agent_name,
            session_id=session_id,
        )
    )
    if previous is not None:
        # LEGACY_COMPAT: Delegated children persisted without a requested model.
        # Legacy format: Parent delegation state and subagent session records stored only model_name, the model the child ran on.
        # Last legacy release: v2026.10.244; replacement: the next release also persists requested_model_name.
        # Handling: An absent requested model reads as the recorded model, so follow-ups keep the model they ran on before.
        # Coverage: tests/test_budget_enforcement.py::test_delegated_follow_up_of_a_child_saved_without_a_requested_model.
        requested_model_name = previous.requested_model_name or previous.model_name
    else:
        requested_model_name = config.resolve_runtime_model(
            entity_name=agent_name,
            active_model_name=model,
            room_id=identity.room_id,
            thread_id=identity.resolved_thread_id,
            runtime_paths=runtime_paths,
        ).model_name
    # Each turn checks the budget against the requested model, so a follow-up recovers once spend is under the cap.
    model_name = budget_model(config, runtime_paths, budget_monitor, owner.requester_id, requested_model_name)
    return DelegationChild(
        delegation_id=delegation_id,
        parent_tool_call_id=parent_tool_call_id,
        caller_agent_name=caller_name,
        child_agent_name=agent_name,
        task=task,
        session_id=session_id,
        run_id=uuid4().hex,
        model_name=model_name,
        requested_model_name=requested_model_name,
        depth=depth + 1,
        execution_identity=serialize_tool_execution_identity(identity),
        subagent_id=previous.subagent_id if previous is not None else delegation_id,
        previous_delegation_id=previous.delegation_id if previous is not None else None,
        parent_requirement_id=parent_requirement_id,
        storage_bindings=freeze_delegation_storage(config, (caller_name, agent_name)),
        agent_mode=previous.agent_mode if previous is not None else agent_mode,
        persona=previous.persona if previous is not None else persona,
    )

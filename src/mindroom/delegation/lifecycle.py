"""Own child execution outcomes and publish their runtime and audit projections."""

from __future__ import annotations

from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Literal
from uuid import uuid4

from agno.run.agent import RunCancelledEvent, RunErrorEvent, RunOutput
from agno.run.base import RunStatus

from mindroom.authorization import is_sender_allowed_for_responder
from mindroom.background_tasks import run_coroutine_until_complete
from mindroom.delegation.audit import (
    child_audit_context,
    finish_child_record,
    record_child_response,
    start_child_record,
)
from mindroom.delegation.audit import observe_child_event as record_child_event
from mindroom.delegation.sessions import reserve_subagent_turn, update_subagent_turn, update_subagent_turn_sync
from mindroom.delegation.state import DelegationChild
from mindroom.delegation.storage import freeze_delegation_storage
from mindroom.error_handling import run_error_event_text
from mindroom.tool_system.runtime_context import get_detached_requester_context, get_tool_runtime_context
from mindroom.tool_system.worker_routing import parse_tool_execution_identity_payload, serialize_tool_execution_identity

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Mapping, Sequence

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.tool_system.worker_routing import ToolExecutionIdentity

MAX_DELEGATION_DEPTH = 3

type _ChildTerminalStatus = Literal["completed", "failed", "cancelled", "denied"]
_TERMINAL = frozenset({"completed", "failed", "cancelled", "denied"})


@dataclass
class _ChildRunObservation:
    """Exact terminal evidence collected while one child response envelope runs."""

    child: DelegationChild
    response: RunOutput | None = None
    terminal: tuple[str, _ChildTerminalStatus, str] | None = None


_CHILD_RUN: ContextVar[_ChildRunObservation | None] = ContextVar("delegation_child_run", default=None)


def child_execution_identity(child: DelegationChild) -> ToolExecutionIdentity:
    """Validate the retained session and agent before resolving any child resource."""
    identity = parse_tool_execution_identity_payload(child.execution_identity, strict=True)
    if identity is None or identity.agent_name != child.child_agent_name or identity.session_id != child.session_id:
        msg = "Delegation child execution identity does not match its retained wait"
        raise RuntimeError(msg)
    return identity


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
    if model_name is not None:
        child.model_name = model_name
    elif context is not None and context.active_model_name is not None:
        child.model_name = context.active_model_name
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
    response: RunOutput,
    *,
    config: Config,
    runtime_paths: RuntimePaths,
    decisions: Mapping[str, bool] | None = None,
    denial_reasons: Mapping[str, str | None] | None = None,
    error_message: str | None = None,
) -> None:
    """Derive operational state from one exact run, then publish its audit view."""
    if response.run_id != child.run_id or response.session_id != child.session_id:
        msg = "Delegation child response does not match its retained run"
        raise ValueError(msg)
    if response.status == RunStatus.completed:
        child.status = "completed"
        child.result = str(response.content or "Agent completed the task but returned no content.")
    elif response.status == RunStatus.cancelled:
        child.status = "cancelled"
        child.result = str(response.content or "Delegation cancelled.")
    elif response.status in {RunStatus.error, RunStatus.regenerated}:
        child.status = "failed"
        child.result = error_message or str(response.content or response.status)
    else:
        child.status = "paused" if response.status == RunStatus.paused else "running"
        child.result = None
    await update_subagent_turn(child, runtime_paths)
    usage = await record_child_response(
        child,
        response,
        config=config,
        runtime_paths=runtime_paths,
        decisions=decisions,
        denial_reasons=denial_reasons,
    )
    if child.status in _TERMINAL:
        await finish_child_record(child, config=config, runtime_paths=runtime_paths, usage=usage)


async def finish_child_turn(
    child: DelegationChild,
    *,
    config: Config,
    runtime_paths: RuntimePaths,
    status: _ChildTerminalStatus | None = None,
    reason: str | None = None,
) -> str:
    """Settle an interruption without overwriting an already retained terminal outcome."""
    if status is not None and child.status not in _TERMINAL:
        child.status = status
        child.result = reason
    if child.status not in _TERMINAL:
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
    observation = _ChildRunObservation(child)
    token = _CHILD_RUN.set(observation)
    try:
        async with child_audit_context(child, config=config, runtime_paths=runtime_paths):
            yield observation
    finally:
        try:
            response = observation.response
            if response is not None and response.run_id == child.run_id:
                terminal = observation.terminal
                await settle_child_response(
                    child,
                    response,
                    config=config,
                    runtime_paths=runtime_paths,
                    error_message=(
                        terminal[2]
                        if terminal is not None and terminal[0] == response.run_id and terminal[1] == "failed"
                        else None
                    ),
                )
            elif observation.terminal is not None and observation.terminal[0] == child.run_id:
                _, status, reason = observation.terminal
                await finish_child_turn(child, config=config, runtime_paths=runtime_paths, status=status, reason=reason)
        finally:
            _CHILD_RUN.reset(token)


async def observe_child_event(event: object) -> None:
    """Retain execution evidence before forwarding the event to the audit adapter."""
    observation = _CHILD_RUN.get()
    if observation is not None and isinstance(event, (RunOutput, RunCancelledEvent, RunErrorEvent)):
        child = observation.child
        if event.run_id == child.run_id and event.session_id == child.session_id:
            if isinstance(event, RunOutput):
                # Failed outputs can retain stale text; keep the matching error event's details.
                observation.response = event
                if event.status != RunStatus.error:
                    observation.terminal = None
            else:
                observation.response = None
                if isinstance(event, RunCancelledEvent):
                    observation.terminal = (child.run_id, "cancelled", event.reason or "Delegation cancelled.")
                else:
                    observation.terminal = (
                        child.run_id,
                        "failed",
                        run_error_event_text(event),
                    )
    await record_child_event(event)


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
) -> Config | str:
    """Recheck the current caller allowlist and requester authority."""
    if allowed_targets is None:
        caller = config.agents.get(caller_name)
        allowed_targets = caller.delegate_to if caller is not None else []
    if not task or not task.strip():
        return "Cannot delegate an empty task. Please provide a task description."

    if agent_name not in allowed_targets:
        available = ", ".join(allowed_targets)
        return f"Cannot delegate to '{agent_name}'. Allowed subagents: {available}."

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
    caller_config = active_config.agents.get(caller_name)
    caller_allows_target = caller_config is not None and agent_name in caller_config.delegate_to
    if not caller_allows_target or not is_sender_allowed_for_responder(
        requester_id,
        agent_name,
        authorization_room_id,
        active_config,
        runtime_paths,
        membership_index,
    ):
        reason = (
            "it is no longer an allowed target"
            if not caller_allows_target
            else "that agent is not allowed to reply to you"
        )
        return f"Cannot delegate to '{agent_name}': {reason}."

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
    model: str | None = None,
    previous: DelegationChild | None = None,
    parent_tool_call_id: str = "",
    parent_requirement_id: str = "",
) -> DelegationChild:
    """Prepare the same scoped fresh/follow-up turn for direct and native callers."""
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
    model_name = (
        previous.model_name
        if previous is not None
        else config.resolve_runtime_model(
            entity_name=agent_name,
            active_model_name=model,
            room_id=identity.room_id,
            thread_id=identity.resolved_thread_id,
            runtime_paths=runtime_paths,
        ).model_name
    )
    return DelegationChild(
        delegation_id=delegation_id,
        parent_tool_call_id=parent_tool_call_id,
        caller_agent_name=caller_name,
        child_agent_name=agent_name,
        task=task,
        session_id=session_id,
        run_id=uuid4().hex,
        model_name=model_name,
        depth=depth + 1,
        execution_identity=serialize_tool_execution_identity(identity),
        subagent_id=previous.subagent_id if previous is not None else delegation_id,
        previous_delegation_id=previous.delegation_id if previous is not None else None,
        parent_requirement_id=parent_requirement_id,
        storage_bindings=freeze_delegation_storage(config, (caller_name, agent_name)),
    )

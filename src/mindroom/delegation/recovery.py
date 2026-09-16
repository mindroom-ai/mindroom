"""Recover retained child turns above the handle and Agno session stores."""

from __future__ import annotations

import asyncio
from copy import deepcopy
from functools import partial
from typing import TYPE_CHECKING, Literal

from agno.db.base import SessionType
from agno.run.agent import RunOutput
from agno.run.base import RunStatus
from agno.run.team import TeamRunOutput
from agno.session.agent import AgentSession

from mindroom.agent_storage import create_session_storage
from mindroom.delegation.hooks import after_delegation
from mindroom.delegation.lifecycle import child_execution_identity, finish_child_turn, settle_child_response
from mindroom.delegation.sessions import load_retained_subagent_turn, load_subagent, subagent_recovery_lock
from mindroom.delegation.state import DELEGATION_STATE_KEY, DelegationChild, DelegationState
from mindroom.delegation.storage import delegation_storage_config
from mindroom.history.session_context import create_scope_session_storage
from mindroom.history.types import HistoryScope
from mindroom.tool_system.worker_routing import parse_tool_execution_identity_payload

if TYPE_CHECKING:
    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.event_journal import ApprovalContinuation
    from mindroom.tool_system.worker_routing import ToolExecutionIdentity


async def resolve_subagent(
    subagent_id: str,
    *,
    owner: ToolExecutionIdentity,
    config: Config,
    runtime_paths: RuntimePaths,
    depth: int,
) -> DelegationChild:
    """Resolve a scoped handle and reconcile an abandoned attempt under its recovery lock."""
    load = partial(load_subagent, subagent_id, owner=owner, config=config, runtime_paths=runtime_paths, depth=depth)
    child = await load()
    if child.status == "running":
        with subagent_recovery_lock(subagent_id, runtime_paths) as acquired:
            if acquired:
                child = await load()
                if child.status == "running":
                    await _recover_subagent_turn(child, config=config, runtime_paths=runtime_paths)
    return child


async def read_child_run(
    child: DelegationChild,
    config: Config,
    paths: RuntimePaths,
) -> RunOutput | None:
    """Read one exact run from the frozen child storage and validate its requester."""
    config = delegation_storage_config(config, child.storage_bindings)

    def read() -> RunOutput | None:
        storage = create_session_storage(
            child.child_agent_name,
            config,
            paths,
            execution_identity=child_execution_identity(child),
        )
        try:
            session = storage.get_session(child.session_id, session_type=SessionType.AGENT)
            # Each turn owns its exact run even after later follow-ups reuse the session.
            runs = reversed(session.runs or ()) if isinstance(session, AgentSession) else ()
            response = next((run for run in runs if run.run_id == child.run_id), None)
            return deepcopy(response) if isinstance(response, RunOutput) else None
        finally:
            storage.close()

    response = await asyncio.to_thread(read)
    if response is not None and (
        not response.run_id
        or response.session_id != child.session_id
        or response.user_id != child_execution_identity(child).requester_id
    ):
        msg = "Delegation session contains an outcome outside its requester identity"
        raise RuntimeError(msg)
    return response


async def _recover_subagent_turn(child: DelegationChild, *, config: Config, runtime_paths: RuntimePaths) -> None:
    """Reconcile an abandoned running claim while its exclusive liveness lock is held."""
    response = await read_child_run(child, config, runtime_paths)
    if response is not None and response.status == RunStatus.paused:
        await settle_child_response(child, response, config=config, runtime_paths=runtime_paths)
        return
    await interrupt_child(
        child,
        config=config,
        runtime_paths=runtime_paths,
        reason="Subagent turn was interrupted by a restart. Send a follow-up to continue its history.",
        status="failed",
    )


async def interrupt_child(
    child: DelegationChild,
    *,
    config: Config,
    runtime_paths: RuntimePaths,
    reason: str,
    status: Literal["cancelled", "failed"] = "cancelled",
) -> None:
    """Settle retained descendants and preserve any already completed child outcome."""
    retained = await load_retained_subagent_turn(child, runtime_paths)
    if retained is not None:
        child.run_id = retained.run_id
        child.model_name = retained.model_name
    config = delegation_storage_config(config, child.storage_bindings)
    response = await read_child_run(child, config, runtime_paths)
    if response is not None and response.status == RunStatus.completed:
        await settle_child_response(child, response, config=config, runtime_paths=runtime_paths)
        return
    if response is not None:
        await _cancel_delegations(response, config=config, runtime_paths=runtime_paths, reason=reason)
        response.status = RunStatus.cancelled if status == "cancelled" else RunStatus.error
        response.content = reason
        response.requirements = []
        for tool in response.tools or ():
            if tool.is_paused:
                tool.result = reason
                tool.tool_call_error = True
            tool.requires_confirmation = False
            tool.external_execution_required = False
            tool.requires_user_input = False

        def persist() -> None:
            storage = create_session_storage(
                child.child_agent_name,
                config,
                runtime_paths,
                execution_identity=child_execution_identity(child),
            )
            try:
                storage.upsert_run(run=response, session_id=child.session_id, user_id=response.user_id)
            finally:
                storage.close()

        await asyncio.to_thread(persist)
    await finish_child_turn(child, config=config, runtime_paths=runtime_paths, status=status, reason=reason)


async def _cancel_delegations(
    response: RunOutput | TeamRunOutput,
    *,
    config: Config,
    runtime_paths: RuntimePaths,
    reason: str = "Delegation cancelled.",
) -> None:
    """Cancel retained descendants without running any of their pending tools."""
    state = DelegationState.from_metadata(response.metadata)
    for child in state.children:
        if child.status not in {"completed", "failed", "cancelled", "denied"}:
            await interrupt_child(child, config=config, runtime_paths=runtime_paths, reason=reason)
    for requirement_id, hook_state in state.hooks.items():
        child = next((item for item in state.children if item.parent_requirement_id == requirement_id), None)
        await after_delegation(
            hook_state,
            config=config,
            runtime_paths=runtime_paths,
            result=child.result if child is not None else None,
            error=asyncio.CancelledError(reason) if child is None or child.status == "cancelled" else None,
        )
    state.clear_pending()
    if (response.metadata or {}).get(DELEGATION_STATE_KEY):
        response.metadata = {**(response.metadata or {}), DELEGATION_STATE_KEY: state.to_dict()}


async def cancel_approval_delegations(
    continuation: ApprovalContinuation,
    *,
    config: Config,
    runtime_paths: RuntimePaths,
    reason: str,
) -> None:
    """Read the frozen root scope and settle its retained child waits on failure."""
    if not continuation.execution_identity:
        return
    config = delegation_storage_config(config, continuation.delegation_storage_bindings)
    identity = parse_tool_execution_identity_payload(continuation.execution_identity, strict=True)
    if identity is None:
        return
    scope = continuation.history_scope
    if scope is None:
        if continuation.entity_kind == "team":
            return
        scope = HistoryScope(kind="agent", scope_id=continuation.entity_name)
    if scope.kind == "agent" and continuation.entity_name not in config.agents:
        return
    storage = await asyncio.to_thread(
        create_scope_session_storage,
        scope=scope,
        agent_name=continuation.entity_name,
        config=config,
        runtime_paths=runtime_paths,
        execution_identity=identity,
    )
    try:
        response = await asyncio.to_thread(storage.get_run, continuation.run_id)
        if isinstance(response, (RunOutput, TeamRunOutput)) and bool(
            (response.metadata or {}).get(DELEGATION_STATE_KEY),
        ):
            await _cancel_delegations(response, config=config, runtime_paths=runtime_paths, reason=reason)
            await asyncio.to_thread(
                storage.upsert_run,
                run=response,
                session_id=continuation.session_id,
                user_id=continuation.requester_id,
            )
    finally:
        storage.close()

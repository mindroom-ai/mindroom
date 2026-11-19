"""Recover retained child turns above the handle and conversation stores."""

from __future__ import annotations

import asyncio
from functools import partial
from typing import TYPE_CHECKING, Literal

from mindroom.delegation.background import delegation_child
from mindroom.delegation.cost import SubagentCost, receiving_subagent_cost
from mindroom.delegation.hooks import after_delegation
from mindroom.delegation.lifecycle import finish_child_turn, settle_child_response
from mindroom.delegation.sessions import (
    SUBAGENT_BUSY,
    SubagentSessionError,
    load_retained_subagent_turn,
    load_subagent,
    subagent_recovery_lock,
)
from mindroom.delegation.state import (
    DELEGATION_STATE_KEY,
    RESTART_INTERRUPTION_REASON,
    TERMINAL_CHILD_STATUSES,
    DelegationChild,
    DelegationState,
    child_execution_identity,
    child_scope,
    interrupted_by_restart,
)
from mindroom.delegation.storage import delegation_storage_config
from mindroom.history.run_checkpoint import is_recovered_run, recover_interrupted_run
from mindroom.history.session_context import create_scope_session_storage
from mindroom.history.storage import new_scope_session
from mindroom.history.types import HistoryScope
from mindroom.llm.runs import RunRecord, RunStatus
from mindroom.session_storage.lanes import run_session_storage_operation
from mindroom.tool_jobs.control import job_stopped_by_shutdown
from mindroom.tool_jobs.runtime import TERMINAL_STATUSES, get_background_runtime

if TYPE_CHECKING:
    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.event_journal import ApprovalContinuation
    from mindroom.llm.runs import AnyRunRecord
    from mindroom.session_storage.store import ConversationStore
    from mindroom.tool_system.worker_routing import ToolExecutionIdentity


async def load_settled_subagent(
    subagent_id: str,
    *,
    owner: ToolExecutionIdentity,
    config: Config,
    runtime_paths: RuntimePaths,
    depth: int,
) -> DelegationChild:
    """Load a scoped handle for a follow-up, settled as `settled_subagent` settles it.

    A handle whose current turn's job has not ended yet is refused as busy.
    """
    child = await settled_subagent(subagent_id, owner=owner, config=config, runtime_paths=runtime_paths, depth=depth)
    if _job_still_runs(child, runtime_paths):
        # A settled child's job still delivers its answer, or continues the turn a restart stopped; until that job
        # ends, the subagent is its.
        raise SubagentSessionError(SUBAGENT_BUSY)
    return child


async def settled_subagent(
    subagent_id: str,
    *,
    owner: ToolExecutionIdentity,
    config: Config,
    runtime_paths: RuntimePaths,
    depth: int,
) -> DelegationChild:
    """Load a scoped handle, first settling an abandoned attempt under its recovery lock, so this writes state."""
    load = partial(load_subagent, subagent_id, owner=owner, config=config, runtime_paths=runtime_paths, depth=depth)
    child = await load()
    if child.status == "running":
        with subagent_recovery_lock(subagent_id, runtime_paths) as acquired:
            if acquired:
                child = await load()
                if child.status == "running":
                    await _recover_subagent_turn(child, config=config, runtime_paths=runtime_paths)
    return child


def _job_still_runs(child: DelegationChild, runtime_paths: RuntimePaths) -> bool:
    """Return whether a subagent job of this process holds the child's subagent and has not ended yet.

    The job is matched by the subagent, not by the turn: a job that continues after a restart names its next turn
    before the handle does.
    """
    runtime = get_background_runtime(runtime_paths)
    return runtime is not None and any(
        job.kind == "delegation"
        and job.status not in TERMINAL_STATUSES
        and delegation_child(job).subagent_id == child.subagent_id
        for job in runtime.retained_jobs()
    )


def _child_storage(child: DelegationChild, config: Config, paths: RuntimePaths) -> ConversationStore:
    """Open the child's agent-scope storage, frozen at delegation; ``config`` already carries the child's bindings."""
    return create_scope_session_storage(
        scope=child_scope(child),
        config=config,
        runtime_paths=paths,
        execution_identity=child_execution_identity(child),
    )


async def read_child_run(
    child: DelegationChild,
    config: Config,
    paths: RuntimePaths,
) -> RunRecord | None:
    """Read one exact run from the frozen child storage and validate its requester."""
    config = delegation_storage_config(config, child.storage_bindings)

    def read(storage: ConversationStore) -> RunRecord | None:
        # Each turn owns its exact run even after later follow-ups reuse the session.
        response = storage.get_run(child.run_id)
        return response if isinstance(response, RunRecord) else None

    response = await run_session_storage_operation(partial(_child_storage, child, config, paths), read)
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
        reason=RESTART_INTERRUPTION_REASON,
        status="failed",
    )


async def interrupt_stopped_child(
    child: DelegationChild,
    *,
    config: Config,
    runtime_paths: RuntimePaths,
    cancel_reason: str = "Delegation cancelled.",
) -> None:
    """Settle a child its job stopped: a shutdown or restart interrupts it as crash recovery does, else it is cancelled.

    A job cancelled while it waited to continue a turn a restart had already settled ends cancelled too: its handle
    keeps that turn as the restart left it, for a follow-up to continue.
    """
    shutdown = job_stopped_by_shutdown()
    await interrupt_child(
        child,
        config=config,
        runtime_paths=runtime_paths,
        reason=RESTART_INTERRUPTION_REASON if shutdown else cancel_reason,
        status="failed" if shutdown else "cancelled",
    )
    if not shutdown and interrupted_by_restart(child):
        child.status, child.result = "cancelled", cancel_reason


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
        # What the saved turn cost so far stands too; a settlement below adds only what it settles.
        child.cost_usd, child.subagents_cost_usd = retained.cost_usd, retained.subagents_cost_usd
        if retained.status in TERMINAL_CHILD_STATUSES:
            # A settlement saved before a crash stands; only finish projecting it.
            child.status, child.result = retained.status, retained.result
            await finish_child_turn(child, config=config, runtime_paths=runtime_paths)
            return
    config = delegation_storage_config(config, child.storage_bindings)
    response = await read_child_run(child, config, runtime_paths)
    # A run recovery already finished did not complete: only its handle is left to settle.
    recovered = response is not None and is_recovered_run(response)
    if response is not None and response.status == RunStatus.completed and not recovered:
        await settle_child_response(child, response, config=config, runtime_paths=runtime_paths)
        return
    if response is not None and not recovered:
        await _cancel_delegations(response, config=config, runtime_paths=runtime_paths, reason=reason)

        def persist(storage: ConversationStore) -> None:
            # The run exists; this rewrites its row through a stand-in session.
            stand_in = new_scope_session(session_id=child.session_id, scope=child_scope(child))
            # A run a restart or a stop cut short keeps its work as a turn the child's follow-ups replay.
            if recover_interrupted_run(storage, stand_in, response):
                return
            # Any other run keeps what the child wrote without replaying; why it stopped is its error, which is what
            # it hands over, and the usage ledger stays as it is.
            response.status = RunStatus.cancelled if status == "cancelled" else RunStatus.error
            response.error = reason
            for tool in response.tools or ():
                if tool.is_paused:
                    tool.result = reason
                    tool.is_error = True
                tool.requires_confirmation = False
                tool.external_execution_required = False
            storage.save_run(stand_in, response, record_usage=False)

        # The save runs in the parent's context; the child's run keeps its own stored cost, not the parent's.
        with receiving_subagent_cost(SubagentCost()):
            await run_session_storage_operation(partial(_child_storage, child, config, runtime_paths), persist)
    await finish_child_turn(child, config=config, runtime_paths=runtime_paths, status=status, reason=reason)


async def _cancel_delegations(
    response: AnyRunRecord,
    *,
    config: Config,
    runtime_paths: RuntimePaths,
    reason: str,
) -> None:
    """Cancel retained descendants without running any of their pending tools."""
    state = DelegationState.from_metadata(response.metadata)
    for child in state.children:
        if child.status not in TERMINAL_CHILD_STATUSES:
            await interrupt_child(child, config=config, runtime_paths=runtime_paths, reason=reason)
    for pause_id, hook_state in state.hooks.items():
        child = next((item for item in state.children if item.parent_requirement_id == pause_id), None)
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
    config = delegation_storage_config(config, continuation.delegation_storage_bindings)
    scope = continuation.history_scope
    if scope is None:
        if continuation.entity_kind == "team":
            return
        scope = HistoryScope(kind="agent", scope_id=continuation.entity_name)
    if scope.kind == "agent" and continuation.entity_name not in config.agents:
        return
    storage, response = await run_session_storage_operation(
        partial(
            create_scope_session_storage,
            scope=scope,
            config=config,
            runtime_paths=runtime_paths,
            execution_identity=continuation.execution_identity,
        ),
        lambda opened: (opened, opened.get_run(continuation.run_id)),
    )
    if response is not None and bool((response.metadata or {}).get(DELEGATION_STATE_KEY)):
        await _cancel_delegations(response, config=config, runtime_paths=runtime_paths, reason=reason)
        changes = {DELEGATION_STATE_KEY: (response.metadata or {})[DELEGATION_STATE_KEY]}
        run_id = response.run_id
        await run_session_storage_operation(
            lambda: storage,
            lambda opened: opened.patch_run_metadata(continuation.session_id, run_id, changes),
        )

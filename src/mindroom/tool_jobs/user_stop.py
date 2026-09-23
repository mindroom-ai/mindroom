"""Apply durable human Stop intent to the jobs owned by the clicked conversation reply."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from mindroom.event_journal import EventKind
from mindroom.handled_turns import TurnRecordCodec
from mindroom.tool_jobs.completion import completion_source_id
from mindroom.tool_jobs.runtime import get_background_runtime

if TYPE_CHECKING:
    from mindroom.constants import RuntimePaths
    from mindroom.event_journal import JournalEvent, PrincipalStore, TurnRecordStore
    from mindroom.tool_jobs.runtime import BackgroundJob, ToolJobRuntime
    from mindroom.turn_record import TurnRecord


async def _reply_receipt_order(store: PrincipalStore, stopped: TurnRecord, stop_receipt_order: int) -> int | None:
    """Bound the clicked response using durable sources, even before its first delivery binds."""
    target = stopped.conversation_target
    assert target is not None
    orders = []
    if stopped.response_event_id is not None:
        bound = await store.response_receipt_order_before_stop(
            room_id=target.room_id,
            response_event_id=stopped.response_event_id,
            stop_receipt_order=stop_receipt_order,
        )
        if bound is not None:
            orders.append(bound)
    # A streaming or deleted placeholder may not have a bound delivery attempt.
    for source in stopped.source_event_ids:
        event = await store.load_event(source)
        if event is not None and event.receipt_order <= stop_receipt_order:
            orders.append(event.receipt_order)
    if stopped.latest_edit_receipt_order is not None and stopped.latest_edit_receipt_order <= stop_receipt_order:
        orders.append(stopped.latest_edit_receipt_order)
    return max(orders) if orders else None


async def _original_event(runtime: ToolJobRuntime, store: PrincipalStore, source: str) -> JournalEvent | None:
    """Follow internal completion ancestry to its originating admitted turn."""
    event = await store.load_event(source)
    seen = {source}
    while event is not None and event.kind is EventKind.TOOL_JOB_COMPLETION:
        parent_id = event.source.get("job_id")
        parent_source = runtime.source_event_id(parent_id) if isinstance(parent_id, str) else None
        if parent_source is None or parent_source in seen:
            return None
        seen.add(parent_source)
        event = await store.load_event(parent_source)
    return event


async def stop_conversation_jobs(
    runtime: ToolJobRuntime,
    store: PrincipalStore,
    stopped: TurnRecord,
    *,
    stop_receipt_order: int,
) -> None:
    """Cancel outstanding work through this reply; retain manual access to saved results."""
    target = stopped.conversation_target
    if target is None or stopped.requester_id is None:
        return
    cutoff = await _reply_receipt_order(store, stopped, stop_receipt_order)
    if cutoff is None:
        return

    async def matches(job: BackgroundJob) -> bool:
        owner = job.owner
        source = job.adapter.get("source_event_id")
        if (
            owner.channel != "matrix"
            or (owner.transport_agent_name or owner.agent_name) != stopped.response_owner
            or owner.room_id != target.room_id
            or owner.resolved_thread_id != target.resolved_thread_id
            or owner.session_id != target.session_id
            or owner.requester_id != stopped.requester_id
            or not isinstance(source, str)
        ):
            return False
        event = await _original_event(runtime, store, source)
        if event is None or event.receipt_order > cutoff:
            return False
        # Consumption can precede completion of the reply or its approval continuation.
        if job.wait_acknowledged and job.status not in {"running", "cancel_requested", "awaiting_approval"}:
            completion = completion_source_id(job.job_id, job.generation)
            for owned_source in (event.event_id, completion):
                if (
                    await store.is_pending(owned_source)
                    or await store.approval_continuation_for_source(owned_source) is not None
                ):
                    return True
            return False
        return True

    await runtime.stop_jobs(receipt_order=stop_receipt_order, matches=matches)


async def response_was_stopped(
    source_event_id: str,
    store: PrincipalStore,
    runtime_paths: RuntimePaths,
    transport_agent_name: str,
) -> bool:
    """Fence original and completion responses, including their owned approvals."""
    runtime = get_background_runtime(runtime_paths)
    if runtime is None:
        return False
    if await runtime.is_source_user_stopped(source_event_id, transport_agent_name):
        return True
    if not source_event_id.startswith("tool-job:"):
        return False
    event = await store.load_event(source_event_id)
    return (
        event is not None
        and event.kind is EventKind.TOOL_JOB_COMPLETION
        and isinstance(job_id := event.source.get("job_id"), str)
        and await runtime.is_user_stopped(job_id)
    )


async def restore_user_stops(runtime: ToolJobRuntime, store: PrincipalStore, turns: TurnRecordStore) -> None:
    """Close a crash between the durable Stop intent and any individual job marker."""
    for index, anchor, encoded in await turns.load_all():
        if index != anchor:
            continue
        stopped = TurnRecordCodec._from_ledger_record(index, json.loads(encoded))
        if stopped is not None and stopped.user_stop_receipt_order is not None:
            await stop_conversation_jobs(runtime, store, stopped, stop_receipt_order=stopped.user_stop_receipt_order)

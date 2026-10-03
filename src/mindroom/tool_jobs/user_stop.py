"""Apply durable human Stop intent to the jobs owned by the clicked conversation reply."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.tool_jobs.runtime import TERMINAL_STATUSES, get_background_runtime

if TYPE_CHECKING:
    from collections.abc import Sequence

    from mindroom.constants import RuntimePaths
    from mindroom.event_journal import PrincipalStore, TurnRecordStore
    from mindroom.tool_jobs.runtime import BackgroundJob, ToolJobRuntime
    from mindroom.turn_record import TurnRecord


async def _reply_receipt_order(
    store: PrincipalStore,
    stopped: TurnRecord,
    room_id: str,
    stop_receipt_order: int,
) -> int | None:
    """Bound the clicked response using durable sources, even before its first delivery binds."""
    orders = []
    if stopped.response_event_id is not None:
        bound = await store.response_receipt_order_before_stop(
            room_id=room_id,
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
    cutoff = await _reply_receipt_order(store, stopped, target.room_id, stop_receipt_order)
    if cutoff is None:
        return

    async def matches(job: BackgroundJob) -> bool:
        owner = job.owner
        if (
            owner.channel != "matrix"
            or owner.recipient != stopped.response_owner
            or owner.room_id != target.room_id
            or owner.resolved_thread_id != target.resolved_thread_id
            or owner.session_id != target.session_id
            or owner.requester_id != stopped.requester_id
            or job.source_event_id is None
        ):
            return False
        source = await store.load_event(job.source_event_id)
        if source is None or source.receipt_order > cutoff:
            return False
        # Consumption can precede completion of the reply that read the outcome, or of its approval continuation.
        if job.consumed and job.status in TERMINAL_STATUSES:
            for owned_source in (job.source_event_id, job.consuming_source):
                if owned_source is not None and (
                    await store.is_pending(owned_source)
                    or await store.approval_continuation_for_source(owned_source) is not None
                ):
                    return True
            return False
        return True

    await runtime.stop_jobs(receipt_order=stop_receipt_order, matches=matches)


async def response_was_stopped(source_event_id: str, runtime_paths: RuntimePaths, transport_agent_name: str) -> bool:
    """Fence a stopped reply's source, including its foreground approval recovery."""
    runtime = get_background_runtime(runtime_paths)
    return runtime is not None and await runtime.is_source_user_stopped(source_event_id, transport_agent_name)


async def restore_user_stops(
    runtime: ToolJobRuntime,
    store: PrincipalStore,
    turns: TurnRecordStore,
    jobs: Sequence[BackgroundJob],
) -> None:
    """Apply Stops saved while the runtime could not receive them, reading only the turns that started these jobs."""
    applied: set[tuple[str, ...]] = set()
    for job in jobs:
        if job.source_event_id is None:
            continue
        record = await turns.load(job.source_event_id)
        if record is None or record.user_stop_receipt_order is None or record.source_event_ids in applied:
            continue
        applied.add(record.source_event_ids)
        await stop_conversation_jobs(runtime, store, record, stop_receipt_order=record.user_stop_receipt_order)

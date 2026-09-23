"""Apply durable human Stop intent to the jobs owned by the clicked conversation reply."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.event_journal import EventKind
from mindroom.tool_jobs.runtime import (
    TERMINAL_STATUSES,
    completion_event_id,
    get_background_runtime,
    parse_completion_event_id,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

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


async def _source_ancestry(runtime: ToolJobRuntime, store: PrincipalStore, source: str) -> list[JournalEvent]:
    """Load a job's admitted source, then through each internal completion the turn that started the delivered job.

    The last event is the originating human turn, unless the ancestry is broken and it is still a completion.
    """
    ancestry: list[JournalEvent] = []
    seen = {source}
    while (event := await store.load_event(source)) is not None:
        ancestry.append(event)
        if event.kind is not EventKind.TOOL_JOB_COMPLETION:
            break
        # A settled event keeps no payload, so its identity is the only durable record of the job it delivered.
        completion = parse_completion_event_id(event.event_id)
        parent_source = runtime.source_event_id(completion[0]) if completion is not None else None
        if parent_source is None or parent_source in seen:
            break
        seen.add(parent_source)
        source = parent_source
    return ancestry


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
        ancestry = await _source_ancestry(runtime, store, job.source_event_id)
        if not ancestry or ancestry[-1].kind is EventKind.TOOL_JOB_COMPLETION or ancestry[-1].receipt_order > cutoff:
            return False
        # Consumption can precede completion of the reply or its approval continuation.
        if job.consumed and job.status in TERMINAL_STATUSES:
            for owned_source in (ancestry[-1].event_id, completion_event_id(job)):
                if (
                    await store.is_pending(owned_source)
                    or await store.approval_continuation_for_source(owned_source) is not None
                ):
                    return True
            return False
        return True

    await runtime.stop_jobs(receipt_order=stop_receipt_order, matches=matches)


async def response_was_stopped(source_event_id: str, runtime_paths: RuntimePaths, transport_agent_name: str) -> bool:
    """Fence original and completion responses, including their owned approvals."""
    runtime = get_background_runtime(runtime_paths)
    if runtime is None:
        return False
    if await runtime.is_source_user_stopped(source_event_id, transport_agent_name):
        return True
    completion = parse_completion_event_id(source_event_id)
    return completion is not None and await runtime.is_user_stopped(completion[0])


async def restore_user_stops(
    runtime: ToolJobRuntime,
    store: PrincipalStore,
    turns: TurnRecordStore,
    jobs: Sequence[BackgroundJob],
) -> None:
    """Apply Stops saved while the runtime could not receive them, reading only the turns that own these jobs.

    Those are each job's source and, through completions, the turns behind it, so the cost follows the jobs.
    """
    stopped: dict[tuple[str, ...], tuple[TurnRecord, int]] = {}
    for job in jobs:
        if job.source_event_id is None:
            continue
        for event in await _source_ancestry(runtime, store, job.source_event_id):
            record = await turns.load(event.event_id)
            if record is not None and record.user_stop_receipt_order is not None:
                stopped.setdefault(record.source_event_ids, (record, record.user_stop_receipt_order))
    for record, stop_receipt_order in stopped.values():
        await stop_conversation_jobs(runtime, store, record, stop_receipt_order=stop_receipt_order)

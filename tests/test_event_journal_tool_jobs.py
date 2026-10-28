"""Background tool jobs live in the event journal, written only by the runtime generation that owns them."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

import pytest

from mindroom.event_journal import (
    JOB_ENDED_REASON,
    ApprovalCardReservation,
    BackgroundApprovalDecision,
    CallKey,
    DeliveryStage,
    ToolJobExistsError,
    ToolJobOwnershipLostError,
    tool_job_run_id,
)
from tests.approval_card_helpers import pending_card, terminal_edit

if TYPE_CHECKING:
    from collections.abc import Callable

    from mindroom.event_journal import EventJournalStore


@pytest.mark.asyncio
async def test_snapshot_and_payload_round_trip(journal_store: EventJournalStore) -> None:
    """A later snapshot keeps the payload its outcome saved; deletion forgets both."""
    jobs = journal_store.tool_jobs("first")
    await jobs.take_ownership()
    await jobs.accept("job", '{"status": "running"}')
    assert await jobs.load_payload("job") is None
    await jobs.save("job", '{"status": "completed"}', '{"value": "full result"}', ends_approvals=True)
    await jobs.save("job", '{"status": "completed", "consumed": true}', ends_approvals=True)
    assert [(job.job_id, job.job_json) for job in await jobs.load_all()] == [
        ("job", '{"status": "completed", "consumed": true}'),
    ]
    assert await jobs.load_payload("job") == '{"value": "full result"}'
    assert [job.job_id for job in await journal_store.tool_jobs("reader").load_all()] == ["job"]
    await jobs.delete("job")
    assert await jobs.load_all() == ()
    assert await jobs.load_payload("job") is None


@pytest.mark.asyncio
async def test_an_accepted_id_is_never_accepted_again(journal_store: EventJournalStore) -> None:
    """Admission refuses an ID any runtime accepted, even after a later runtime took over."""
    first = journal_store.tool_jobs("first")
    await first.take_ownership()
    await first.accept("job", "{}")
    second = journal_store.tool_jobs("second")
    await second.take_ownership()
    with pytest.raises(ToolJobExistsError):
        await second.accept("job", "{}")


@pytest.mark.asyncio
async def test_a_newer_runtime_fences_the_older_one(journal_database: Callable[[], EventJournalStore]) -> None:
    """Once another process takes over, every write of the older runtime is refused and changes nothing."""
    older = journal_database().tool_jobs("older")
    await older.take_ownership()
    await older.accept("job", '{"status": "running"}')
    newer = journal_database().tool_jobs("newer")
    await newer.take_ownership()
    for write in (
        older.require_ownership(),
        older.accept("other", "{}"),
        older.save("job", '{"status": "completed"}', '{"value": "late"}', ends_approvals=True),
        older.delete("job"),
    ):
        with pytest.raises(ToolJobOwnershipLostError):
            await write
    assert [(job.job_id, job.job_json) for job in await newer.load_all()] == [("job", '{"status": "running"}')]
    assert await newer.load_payload("job") is None
    await newer.save("job", '{"status": "interrupted"}', ends_approvals=True)
    assert [job.job_json for job in await newer.load_all()] == ['{"status": "interrupted"}']


@pytest.mark.asyncio
async def test_writes_need_ownership(journal_store: EventJournalStore) -> None:
    """A runtime that never took ownership cannot write."""
    with pytest.raises(ToolJobOwnershipLostError):
        await journal_store.tool_jobs("unowned").accept("job", "{}")


@pytest.mark.asyncio
async def test_the_save_of_a_jobs_end_denies_its_pending_cards(journal_store: EventJournalStore) -> None:
    """An ended job leaves no answerable card, without an approval runtime to deny it, and its running saves leave one."""
    router = journal_store.principal("router@shared")
    call = CallKey.detached(tool_job_run_id("job"), "call")
    delivery_id = "script-approval:tool-job:job:call"
    assert await router.approvals.reserve_detached_card(
        call=call,
        owner_kind="job",
        tool_name="write_report",
        toolkit_name="reports",
        invoking_agent="code",
        room_id="!room:test",
        thread_id="$thread",
        expires_at_ns=time.time_ns() + 60_000_000_000,
        card=ApprovalCardReservation(
            delivery_id=delivery_id,
            tool_call_id="call",
            event_type="io.mindroom.tool_approval",
            approver_user_id="@human:test",
            approvable=True,
            payload={"approval_id": delivery_id, "tool_name": "write_report", "status": "pending"},
        ),
    )
    await router.acknowledge_matrix_delivery(
        delivery_id=delivery_id,
        stage=DeliveryStage.INITIAL,
        event_id="$card",
        delivered_projections=(),
    )
    jobs = journal_store.tool_jobs("owner")
    await jobs.take_ownership()
    await jobs.accept("job", '{"status": "running"}')

    await jobs.save("job", '{"status": "awaiting_approval"}', ends_approvals=False)
    waiting = await pending_card(router.approvals, room_id="!room:test", card_event_id="$card")
    assert waiting is not None
    assert not waiting.decided
    await jobs.save("job", '{"status": "cancelled"}', ends_approvals=True)

    ended = await pending_card(router.approvals, room_id="!room:test", card_event_id="$card")
    assert ended is not None
    assert ended.decided
    assert await router.approvals.detached_decision(call) == BackgroundApprovalDecision("denied", JOB_ENDED_REASON)
    edit = await terminal_edit(router, delivery_id)
    assert (edit["status"], edit["resolution_reason"]) == ("denied", JOB_ENDED_REASON)

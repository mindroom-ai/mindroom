"""Backend-neutral durability tests for suspended tool-approval continuations."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest

import mindroom.event_journal as journal

if TYPE_CHECKING:
    from mindroom.event_journal import EventJournalStore

pytestmark = pytest.mark.asyncio


def _continuation(approval_id: str = "approval-1") -> object:
    """Return one literal continuation whose exact tool call can be recovered."""
    return journal.StoredApprovalContinuation(
        approval_id=approval_id,
        card_transaction_id=f"mindroom-approval-{approval_id}",
        room_id="!room:example.org",
        thread_id="$thread",
        response_event_id="$response",
        source_event_ids=("$source",),
        entity_kind="agent",
        entity_name="code",
        session_id="session-1",
        run_id="run-1",
        tool_call_id="call-1",
        tool_name="write_file",
        arguments={"path": "notes.txt", "contents": "hello"},
        requester_id="@alice:example.org",
        execution_identity={
            "channel": "matrix",
            "agent_name": "code",
            "requester_id": "@alice:example.org",
            "room_id": "!room:example.org",
            "thread_id": "$thread",
            "resolved_thread_id": "$thread",
            "session_id": "session-1",
            "tenant_id": None,
            "account_id": None,
            "transport_agent_name": "code",
        },
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
    )


async def test_continuation_decision_and_claim_are_first_writer_wins(
    journal_store: EventJournalStore,
) -> None:
    """Duplicate decisions and worker wake-ups must not create a second execution owner."""
    alice = journal_store.principal("agent@alice")
    continuation = _continuation()
    assert await alice.create_approval_continuation(continuation) is True

    assert await alice.resolve_approval_continuation("approval-1", "approved") is True
    assert await alice.resolve_approval_continuation("approval-1", "denied") is False
    assert await alice.claim_approval_continuation("approval-1", "worker-a") is True
    assert await alice.claim_approval_continuation("approval-1", "worker-b") is False

    stored = await alice.approval_continuation("approval-1")
    assert stored is not None
    assert stored.decision == "approved"
    assert stored.state == journal.ApprovalContinuationState.CLAIMED
    assert stored.claimant_id == "worker-a"
    assert stored.arguments == {"path": "notes.txt", "contents": "hello"}


async def test_continuation_and_handled_source_turn_commit_as_one_handoff(
    journal_store: EventJournalStore,
) -> None:
    """The source cannot become replayable independently of its durable continuation."""
    alice = journal_store.principal("agent@alice")
    continuation = _continuation()

    created = await alice.create_approval_continuation_with_turn(
        continuation,
        agent_name="code",
        index_event_ids=("$source",),
        anchor_event_id="$source",
        record_json='{"completed":true,"response_event_id":"$response"}',
    )

    assert created is True
    assert await alice.approval_continuation("approval-1") is not None
    assert await journal_store.turn_records("code").load_all() == (
        (
            "$source",
            "$source",
            '{"completed":true,"response_event_id":"$response"}',
        ),
    )

    duplicate = await alice.create_approval_continuation_with_turn(
        continuation,
        agent_name="code",
        index_event_ids=("$source",),
        anchor_event_id="$source",
        record_json='{"completed":false}',
    )
    assert duplicate is False
    assert (await journal_store.turn_records("code").load_all())[0][2] != '{"completed":false}'


async def test_completed_or_failed_continuations_cannot_be_reclaimed(
    journal_store: EventJournalStore,
) -> None:
    """A terminal continuation must never return to an executable state."""
    alice = journal_store.principal("agent@alice")
    await alice.create_approval_continuation(_continuation("completed"))
    await alice.resolve_approval_continuation("completed", "approved")
    await alice.claim_approval_continuation("completed", "worker-a")

    assert await alice.mark_approval_continuation_delivered("completed", "worker-a") is True
    assert await alice.complete_approval_continuation("completed", "worker-a") is True
    assert await alice.claim_approval_continuation("completed", "worker-b") is False

    await alice.create_approval_continuation(_continuation("failed"))
    assert await alice.fail_approval_continuation("failed", "Agent is no longer available.") is True
    assert await alice.resolve_approval_continuation("failed", "approved") is False
    failed = await alice.approval_continuation("failed")
    assert failed is not None
    assert failed.state == journal.ApprovalContinuationState.TERMINAL_FAILURE
    assert failed.failure_reason == "Agent is no longer available."


async def test_recovery_scan_returns_only_nonterminal_continuations(
    journal_store: EventJournalStore,
) -> None:
    """Startup must find waiting, ready, and uncertain claimed work without reviving terminal rows."""
    alice = journal_store.principal("agent@alice")
    for approval_id in ("waiting", "ready", "claimed", "delivered", "completed", "failed"):
        await alice.create_approval_continuation(_continuation(approval_id))
    await alice.resolve_approval_continuation("ready", "denied")
    await alice.resolve_approval_continuation("claimed", "approved")
    await alice.claim_approval_continuation("claimed", "dead-worker")
    await alice.resolve_approval_continuation("delivered", "approved")
    await alice.claim_approval_continuation("delivered", "worker-b")
    await alice.mark_approval_continuation_delivered("delivered", "worker-b")
    await alice.resolve_approval_continuation("completed", "approved")
    await alice.claim_approval_continuation("completed", "worker-a")
    await alice.mark_approval_continuation_delivered("completed", "worker-a")
    await alice.complete_approval_continuation("completed", "worker-a")
    await alice.fail_approval_continuation("failed", "terminal")

    recoverable = await alice.recoverable_approval_continuations()

    assert [item.approval_id for item in recoverable] == ["waiting", "ready", "claimed", "delivered"]

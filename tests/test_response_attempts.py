"""Durable response identity is independent of recovery payloads."""

from __future__ import annotations

from dataclasses import replace
from hashlib import sha256
from typing import TYPE_CHECKING

import pytest

from mindroom import response_sources
from mindroom.event_journal import DeliveryStage, response_attempts
from mindroom.message_target import MessageTarget
from mindroom.turn_record import TurnRecord
from tests.test_event_journal_store import ROOM, admit
from tests.test_event_journal_store import TestApprovalContinuations as _ApprovalFixtures

pytestmark = pytest.mark.asyncio

if TYPE_CHECKING:
    from mindroom.event_journal import EventJournalStore


async def test_attempt_binding_rejects_conflicts_and_preserves_frozen_delivery(
    journal_store: EventJournalStore,
) -> None:
    """A later caller cannot reassign the frozen payload to another owner or target."""
    principal = journal_store.principal("agent@alice")
    await admit(principal, "$edit")
    attempt = response_sources.ResponseAttempt(
        "agent",
        response_sources.ResponseSources(("$edit",), ("$source",), edit_receipt_order=2),
    )
    arguments = {
        "delivery_id": "$edit",
        "stage": DeliveryStage.FINAL,
        "room_id": ROOM,
        "thread_id": None,
        "payload": {"body": "first"},
        "edits_event_id": "$answer",
    }
    assert await principal.enqueue_matrix_delivery(**arguments, response_attempt=attempt)
    assert await principal.claim_matrix_delivery(delivery_id="$edit", stage=DeliveryStage.FINAL)
    with pytest.raises(ValueError, match="identity"):
        await principal.enqueue_matrix_delivery(**arguments, response_attempt=replace(attempt, entity_name="other"))
    with pytest.raises(ValueError, match="identity"):
        await principal.enqueue_matrix_delivery(**{**arguments, "edits_event_id": "$wrong"}, response_attempt=attempt)
    delivery = await principal.load_matrix_delivery(delivery_id="$edit", stage=DeliveryStage.FINAL)
    assert delivery.payload["body"] == "first"
    assert delivery.edits_event_id == "$answer"


async def test_attempted_new_send_rejects_retry_edit_target_and_remains_acknowledgeable(
    journal_store: EventJournalStore,
) -> None:
    """A frozen new send cannot acquire a retry's unrelated visible edit target."""
    principal = journal_store.principal("agent@alice")
    await admit(principal, "$source")
    attempt = response_sources.ResponseAttempt(
        "agent",
        response_sources.ResponseSources(("$source",), ("$source",)),
    )
    arguments = {
        "delivery_id": "$source",
        "stage": DeliveryStage.FINAL,
        "room_id": ROOM,
        "thread_id": None,
        "payload": {"body": "first"},
        "result": {"terminal_status": "completed"},
        "response_attempt": attempt,
    }
    transaction_id = await principal.enqueue_matrix_delivery(**arguments)
    assert transaction_id is not None
    assert await principal.claim_matrix_delivery(delivery_id="$source", stage=DeliveryStage.FINAL)
    with pytest.raises(ValueError, match="identity"):
        await principal.enqueue_matrix_delivery(**arguments, edits_event_id="$wrong")
    stored = await journal_store.backend.read(
        lambda tx: response_attempts.load_response_attempt(tx, "agent@alice", "$source"),
    )
    assert stored.response_event_id is None
    frozen = await principal.load_matrix_delivery(delivery_id="$source", stage=DeliveryStage.FINAL)
    assert frozen.edits_event_id is None
    assert frozen.transaction_id == transaction_id
    assert frozen.payload["body"] == "first"
    assert frozen.result == {"terminal_status": "completed"}
    assert await principal.enqueue_matrix_delivery(**arguments) == transaction_id
    acknowledgement = await principal.acknowledge_matrix_delivery(
        delivery_id="$source",
        stage=DeliveryStage.FINAL,
        event_id="$actual-send",
        delivered_projections=(),
    )
    assert acknowledgement.bound
    assert acknowledgement.settled_event_id == "$actual-send"
    stored = await journal_store.backend.read(
        lambda tx: response_attempts.load_response_attempt(tx, "agent@alice", "$source"),
    )
    assert stored.response_event_id == "$actual-send"


async def test_ack_binds_visible_original_instead_of_edit_event(journal_store: EventJournalStore) -> None:
    """The Matrix edit ACK names a transport event, not the visible response."""
    principal = journal_store.principal("agent@alice")
    await admit(principal, "$edit")
    attempt = response_sources.ResponseAttempt(
        "agent",
        response_sources.ResponseSources(("$edit",), ("$source",)),
    )
    await principal.enqueue_matrix_delivery(
        delivery_id="$edit",
        stage=DeliveryStage.FINAL,
        room_id=ROOM,
        thread_id=None,
        payload={"body": "answer"},
        edits_event_id="$answer",
        response_attempt=attempt,
    )
    await principal.claim_matrix_delivery(delivery_id="$edit", stage=DeliveryStage.FINAL)
    await principal.acknowledge_matrix_delivery(
        delivery_id="$edit",
        stage=DeliveryStage.FINAL,
        event_id="$edit-ack",
        delivered_projections=(),
    )
    stored = await journal_store.backend.read(
        lambda transaction: response_attempts.load_response_attempt(transaction, "agent@alice", "$edit"),
    )
    assert stored.response_event_id == "$answer"
    assert stored.logical_source_event_ids == ("$source",)


async def test_large_coalesced_identity_is_not_limited_by_index_entry_size(journal_store: EventJournalStore) -> None:
    """Realistic high-entropy source membership exceeds a PostgreSQL B-tree entry."""
    principal = journal_store.principal("agent@alice")
    await admit(principal, "$edit")
    logical = tuple("$" + sha256(str(index).encode()).hexdigest() for index in range(250))
    attempt = response_sources.ResponseAttempt("agent", response_sources.ResponseSources(("$edit",), logical))
    assert await principal.enqueue_matrix_delivery(
        delivery_id="$edit",
        stage=DeliveryStage.FINAL,
        room_id=ROOM,
        thread_id=None,
        payload={"body": "answer"},
        response_attempt=attempt,
    )
    stored = await journal_store.backend.read(
        lambda tx: response_attempts.load_response_attempt(tx, "agent@alice", "$edit"),
    )
    assert stored.logical_source_event_ids == logical


async def test_refused_duplicate_approval_does_not_register_another_attempt(journal_store: EventJournalStore) -> None:
    """Refusing an occupied approval ID must commit no orphan response identity."""
    principal = journal_store.principal("agent@alice")
    for event_id in ("$source-1", "$source-2", "$other"):
        await admit(principal, event_id)
    approval = _ApprovalFixtures.continuation()
    assert await principal.create_approval_continuation(approval) is not None
    conflicting = replace(approval, sources=response_sources.ResponseSources(("$other",), ("$other",)))
    assert await principal.create_approval_continuation(conflicting) is None
    assert (
        await journal_store.backend.read(
            lambda tx: response_attempts.load_response_attempt(
                tx,
                "agent@alice",
                "$other",
            ),
        )
        is None
    )


@pytest.mark.parametrize("newer_state", ["pending", "failed", "unacknowledged", "successful"])
@pytest.mark.parametrize("same_sources", [False, True])
async def test_failure_requires_exact_acknowledged_success(
    journal_store: EventJournalStore,
    newer_state: str,
    same_sources: bool,
) -> None:
    """A shared source or newer admission alone cannot retire an older failure."""
    principal = journal_store.principal("agent@alice")
    for event_id in ("$old", "$new"):
        await admit(principal, event_id)
    old = response_sources.ResponseAttempt(
        "agent",
        response_sources.ResponseSources(("$old",), ("$source", "$second"), edit_receipt_order=1),
    )
    newer = response_sources.ResponseAttempt(
        "agent",
        response_sources.ResponseSources(
            ("$new",),
            ("$source", "$second") if same_sources else ("$source",),
            edit_receipt_order=2,
        ),
    )
    arguments = {
        "stage": DeliveryStage.FINAL,
        "room_id": ROOM,
        "thread_id": None,
        "payload": {"body": "answer"},
        "edits_event_id": "$answer",
    }
    await principal.enqueue_matrix_delivery(**arguments, delivery_id="$old", response_attempt=old)
    if newer_state != "pending":
        await principal.enqueue_matrix_delivery(
            **arguments,
            delivery_id="$new",
            response_attempt=newer,
            result=None if newer_state == "failed" else {"body": "answer"},
        )
        await principal.claim_matrix_delivery(delivery_id="$new", stage=DeliveryStage.FINAL)
        if newer_state != "unacknowledged":
            await principal.acknowledge_matrix_delivery(
                delivery_id="$new",
                stage=DeliveryStage.FINAL,
                event_id="$new-ack",
                delivered_projections=(),
            )
    current = TurnRecord.create(
        ("$source", "$second"),
        response_owner="agent",
        response_event_id="$answer",
        conversation_target=MessageTarget(ROOM, None, None, "$source", ROOM),
    )
    stored = await journal_store.backend.read(
        lambda tx: response_attempts.load_response_attempt(tx, "agent@alice", "$old"),
    )
    disposition = await journal_store.backend.read(
        lambda tx: response_attempts.approval_failure_disposition(
            tx,
            "agent@alice",
            attempt=stored,
            current_record=current,
            failure_reason="failure",
        ),
    )
    expected = "retire" if same_sources and newer_state == "successful" else "publish"
    assert disposition.value == expected

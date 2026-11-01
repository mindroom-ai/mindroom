"""Durable admission and replay of inbound Matrix events.

Admission is the boundary that makes the no-loss guarantee real: nio is told an
event was accepted only after this transaction commits, so a crash before the
commit leaves the event for redelivery rather than losing it.

There is deliberately no durable ``running`` state. A process that dies
mid-turn must leave its event eligible for retry, and a state that says
"someone is working on this" would instead leave it stranded until a human
noticed.
"""

from __future__ import annotations

import json
import time
from collections.abc import Mapping
from dataclasses import dataclass, replace
from itertools import batched
from typing import TYPE_CHECKING, cast
from uuid import UUID

from mindroom.event_journal.history_recovery import (
    HistoryRecoveryOutcome,
    HistoryRecoveryState,
    RoomHistoryRecovery,
)
from mindroom.logging_config import get_logger

from . import (
    approval_cards,
    approval_continuations,
    interactive_questions,
    membership_hooks,
    outbox,
    reads,
    replies,
    reply_messages,
    turn_rows,
)
from .identity import agent_of_principal, decode_thread_id, encode_thread_id
from .membership_state import claim_membership_epoch
from .models import (
    TURN_BACKED_KINDS,
    AdmissionFacts,
    AdmissionResult,
    DeliveryProjectionPendingError,
    DeliveryStage,
    DepartureSource,
    EventClass,
    EventKind,
    InboundEvent,
    IngestionBatchAdmission,
    IngestionBatchIntegrityError,
    IngestionBatchSequenceError,
    IngestionBatchValidationError,
    IngestionConsumerBindingError,
    IngestionRecordAdmission,
    IngestionRecordDisposition,
    JournalEvent,
    PendingPage,
    ResponseRecoveryState,
    RoomMembershipPosition,
    SemanticConsumer,
)
from .projection import ProjectedEvent, is_redacted, project, scrub_redacted_projection
from .schema import PENDING_STATE, SETTLED_STATE

if TYPE_CHECKING:
    from mindroom.turn_record import TurnRecord

    from .backend import Row, Transaction

logger = get_logger(__name__)

_JOURNAL_COLUMNS = """
    event_id, room_id, thread_id, kind, sender,
    origin_server_ts, source_json, receipt_order, semantic_consumer
"""
_EVENT_JOURNAL_COLUMNS = """
    events.event_id AS event_id, events.room_id AS room_id,
    events.thread_id AS thread_id, events.kind AS kind, events.sender AS sender,
    events.origin_server_ts AS origin_server_ts, events.source_json AS source_json,
    events.receipt_order AS receipt_order, events.semantic_consumer AS semantic_consumer
"""
# Successful repair is hidden from callers but retained as the revision carrier,
# so a later gap cannot reuse the identity of an old in-flight walk.
_REPAIRED_RECOVERY_STATE = "repaired"
_TURN_BACKED_KIND_VALUES = tuple(sorted(kind.value for kind in TURN_BACKED_KINDS))
_TURN_BACKED_KIND_PLACEHOLDERS = ", ".join("?" for _ in _TURN_BACKED_KIND_VALUES)
_MATRIX_MEMBERSHIPS = frozenset({"ban", "invite", "join", "knock", "leave"})
# Event IDs bound into one query, well under every backend's parameter limit.
_PENDING_AMONG_BATCH = 500


def validate_ingestion_batch_admission(admission: IngestionBatchAdmission) -> None:
    """Check sequence and application effects at the journal boundary."""
    if (
        not isinstance(admission, IngestionBatchAdmission)
        or not isinstance(admission.stream_id, UUID)
        or type(admission.sequence) is not int
        or not 1 <= admission.sequence <= 2**63 - 2
    ):
        message = "Invalid batch sequence or stream"
        raise IngestionBatchValidationError(message)
    for record in admission.records:
        _validate_ingestion_record(record)


def _validate_ingestion_record(item: IngestionRecordAdmission) -> None:
    invalid = IngestionBatchValidationError("Invalid ingestion record admission")

    def require(condition: object) -> None:
        if not condition:
            raise invalid

    require(isinstance(item, IngestionRecordAdmission))
    require(type(item.disposition) is IngestionRecordDisposition)
    _validate_membership_effect(item)
    effect = item.disposition
    room_id = item.room_id
    e = item.event
    p = item.projected
    if effect is not IngestionRecordDisposition.SEMANTIC_EVENT:
        require(e is None and p is None)
        if effect is IngestionRecordDisposition.HISTORY_LOSS:
            require(type(room_id) is str and bool(room_id))
        elif effect is IngestionRecordDisposition.ROOM_LIFECYCLE:
            require(item.membership is not None)
        elif item.membership is None:
            require(room_id is None)
        return
    if item.membership is None:
        require(room_id is None)
    require(type(e) is InboundEvent)
    event = cast("InboundEvent", e)
    require(room_id is None or room_id == event.room_id)
    require(all(type(v) is str and v for v in (event.event_id, event.room_id, event.sender)))
    require(event.thread_id is None or (type(event.thread_id) is str and bool(event.thread_id)))
    require(type(event.kind) is EventKind and type(event.event_class) is EventClass)
    require(type(event.origin_server_ts) is int and isinstance(event.source, Mapping))
    if event.kind is EventKind.OPAQUE_HISTORY:
        require(event.event_class is EventClass.CONTEXT_ONLY and p is None)
    if p is None:
        return
    require(type(p) is ProjectedEvent and isinstance(p.content, Mapping))
    projected = p
    require(all(type(v) is str for v in (projected.event_id, projected.room_id, projected.sender)))
    require(type(projected.thread_id) is type(event.thread_id) and type(projected.origin_server_ts) is int)
    require(projected.event_id == event.event_id and projected.room_id == event.room_id)
    require(projected.thread_id == event.thread_id and projected.sender == event.sender)
    require(projected.origin_server_ts == event.origin_server_ts)
    relations = projected.replaces_event_id, projected.redacts_event_id
    require(all(v is None or (type(v) is str and v) for v in relations))


def _validate_membership_effect(item: IngestionRecordAdmission) -> None:
    """Validate own membership independently of the event's disposition."""
    invalid = IngestionBatchValidationError("Invalid own-membership effect")
    if item.membership is None:
        if any(
            value is not None
            for value in (
                item.source,
                item.previous_membership,
                item.previous_membership_epoch,
                item.membership_epoch,
            )
        ):
            raise invalid
        return
    if (
        type(item.source) is not DepartureSource
        or not isinstance(item.room_id, str)
        or not item.room_id
        or item.membership not in _MATRIX_MEMBERSHIPS
        or type(item.previous_membership_epoch) is not int
        or type(item.membership_epoch) is not int
        or item.previous_membership_epoch < 0
    ):
        raise invalid
    if item.previous_membership is None:
        if (
            item.source is not DepartureSource.REPORTED
            or item.previous_membership_epoch != 0
            or item.membership_epoch != 0
        ):
            raise invalid
    elif (
        item.previous_membership not in _MATRIX_MEMBERSHIPS
        # A local command can confirm an unobserved room's initial leave/0
        # position through HTTP even though it does not advance producer epoch.
        or (item.previous_membership == item.membership and item.source is not DepartureSource.LOCAL)
        or item.membership_epoch
        != item.previous_membership_epoch + int(item.previous_membership == "join" and item.membership != "join")
    ):
        raise invalid


def _require_matching_semantic_event(
    transaction: Transaction,
    principal_id: str,
    event: InboundEvent,
) -> Row:
    row = transaction.fetchone(
        """
        SELECT receipt_order, room_id, thread_id, kind, sender, origin_server_ts
        FROM journal_events
        WHERE principal_id = ? AND event_id = ?
        """,
        (principal_id, event.event_id),
    )
    if row is None:
        raise IngestionBatchIntegrityError
    envelope = tuple(row[column] for column in ("room_id", "sender", "origin_server_ts"))
    if tuple(map(type, envelope)) != (str, str, int) or envelope != (
        event.room_id,
        event.sender,
        event.origin_server_ts,
    ):
        raise IngestionBatchIntegrityError
    # An opaque history marker identifies an envelope, whose encrypted kind
    # and thread are unknown. It never owns a callback or becomes actionable.
    if EventKind.OPAQUE_HISTORY not in (row["kind"], event.kind) and (
        row["kind"] != event.kind.value or row["thread_id"] != encode_thread_id(event.thread_id)
    ):
        raise IngestionBatchIntegrityError
    return row


def _admit_suppressed_semantic_identity(
    transaction: Transaction,
    principal_id: str,
    event: InboundEvent,
) -> None:
    """Retain a settled identity for turn work fenced out of this tenure."""
    result = _admit(
        transaction,
        principal_id,
        replace(event, event_class=EventClass.CONTEXT_ONLY),
        None,
    )
    if result is AdmissionResult.DUPLICATE:
        _require_matching_semantic_event(transaction, principal_id, event)


def _apply_semantic_ingestion_disposition(
    transaction: Transaction,
    principal_id: str,
    event: InboundEvent,
    projected: ProjectedEvent | None,
) -> bool:
    """Admit or deduplicate one semantic event under the locked tenure."""
    state = _claim_membership_state(transaction, principal_id, event.room_id)
    if event.kind in TURN_BACKED_KINDS and state.departure_fenced:
        _admit_suppressed_semantic_identity(transaction, principal_id, event)
        return False
    semantic_result = _admit(transaction, principal_id, event, projected)
    if semantic_result is AdmissionResult.DUPLICATE:
        retained = _require_matching_semantic_event(transaction, principal_id, event)
        if retained["kind"] == EventKind.OPAQUE_HISTORY and projected is not None:
            _project_with_redactions(
                transaction,
                principal_id,
                projected,
                receipt_order=int(retained["receipt_order"]),
                membership_epoch=state.membership_epoch,
                settle_redacted_arrival=True,
            )
    if event.kind is EventKind.ROOM_LIFECYCLE and event.event_class is EventClass.CONTEXT_ONLY:
        content = cast("Mapping[str, object]", event.source["content"])
        if content.get("membership") == "join":
            membership_hooks.record_baseline(
                transaction,
                principal_id,
                event.room_id,
                cast("str", event.source["state_key"]),
            )
    return semantic_result is AdmissionResult.ADMITTED and event.event_class is EventClass.ACTIONABLE


def _apply_membership_effect(
    transaction: Transaction,
    principal_id: str,
    admission: IngestionRecordAdmission,
) -> None:
    """Apply the tenure change before any semantic effect on the same record."""
    room_id = cast("str", admission.room_id)
    producer = ingestion_membership_position(transaction, principal_id, room_id)
    previous_epoch = 0 if producer is None else producer.membership_epoch
    if admission.previous_membership_epoch != previous_epoch:
        raise IngestionBatchIntegrityError
    if producer is not None and (
        admission.previous_membership is None
        or ("join" if admission.previous_membership == "join" else "leave") != producer.membership
    ):
        raise IngestionBatchIntegrityError
    state = _claim_membership_state(transaction, principal_id, room_id)
    if admission.membership != "join" and admission.previous_membership == "join":
        if state.departure_fenced:
            raise IngestionBatchIntegrityError
        _advance_membership_epoch(transaction, principal_id, room_id)
    transaction.execute(
        "UPDATE room_membership SET departure_fenced = ? WHERE principal_id = ? AND room_id = ?",
        (int(admission.membership != "join"), principal_id, room_id),
    )
    transaction.execute(
        """
        INSERT INTO matrix_ingestion_membership (principal_id, room_id, membership, membership_epoch)
        VALUES (?, ?, ?, ?)
        ON CONFLICT (principal_id, room_id) DO UPDATE SET
            membership = excluded.membership, membership_epoch = excluded.membership_epoch
        """,
        (
            principal_id,
            room_id,
            "join" if admission.membership == "join" else "leave",
            admission.membership_epoch,
        ),
    )


def _apply_ingestion_disposition(
    transaction: Transaction,
    principal_id: str,
    admission: IngestionRecordAdmission,
) -> bool:
    """Apply one validated record effect, returning whether it dispatches."""
    disposition = admission.disposition
    if admission.membership is not None:
        _apply_membership_effect(transaction, principal_id, admission)
    if disposition in {IngestionRecordDisposition.COMPATIBILITY_ONLY, IngestionRecordDisposition.ROOM_LIFECYCLE}:
        return False

    if disposition is IngestionRecordDisposition.HISTORY_LOSS:
        _record_room_history_recovery(transaction, principal_id, cast("str", admission.room_id))
        return False

    return _apply_semantic_ingestion_disposition(
        transaction,
        principal_id,
        cast("InboundEvent", admission.event),
        admission.projected,
    )


def admit_ingestion_batch(
    transaction: Transaction,
    principal_id: str,
    admission: IngestionBatchAdmission,
) -> AdmissionFacts:
    """Commit semantic effects and the consumer's acceptance in one transaction."""
    validate_ingestion_batch_admission(admission)
    stream = str(admission.stream_id)
    row = transaction.fetchone(
        "UPDATE matrix_sync_consumers SET next_sequence = next_sequence + 1 "
        "WHERE principal_id = ? AND stream_id = ? AND next_sequence = ? "
        "RETURNING next_sequence",
        (principal_id, stream, admission.sequence),
    )
    if row is None:
        state = transaction.fetchone(
            "SELECT stream_id, next_sequence FROM matrix_sync_consumers WHERE principal_id = ?",
            (principal_id,),
        )
        if state is None or state["stream_id"] != stream:
            raise IngestionConsumerBindingError
        if admission.sequence != state["next_sequence"] - 1:
            raise IngestionBatchSequenceError
        return AdmissionFacts(False, False, tuple(AdmissionFacts(False, False) for _ in admission.records))
    facts = []
    for record in admission.records:
        semantic_new = _apply_ingestion_disposition(transaction, principal_id, record)
        if semantic_new and record.event is not None:
            _snapshot_interactive_source(transaction, principal_id, record.event)
        facts.append(AdmissionFacts(True, semantic_new))
    return AdmissionFacts(True, any(f.semantic_event_new for f in facts), tuple(facts))


def store_generation(transaction: Transaction, *, new_generation: str) -> str:
    """Return this database's generation, minting it on first use.

    The install's journal binding uses this identity to reject an accidental
    database replacement that would lose turn, delivery, and recovery ownership.
    ``new_generation`` is only used if no row exists; an established database
    keeps its identity across process restarts.
    """
    transaction.execute(
        """
        INSERT INTO journal_identity (singleton, generation)
        VALUES (?, ?)
        ON CONFLICT (singleton) DO NOTHING
        """,
        (True, new_generation),
    )
    row = transaction.fetchone("SELECT generation FROM journal_identity WHERE singleton = ?", (True,))
    if row is None:
        msg = "Event journal identity row is missing immediately after it was written"
        raise RuntimeError(msg)
    return str(row["generation"])


def read_generation(transaction: Transaction) -> str | None:
    """Return this database's generation, or ``None`` when it has never been opened.

    Deliberately does not mint. The caller asking this question is deciding
    whether the database belongs to this install, and a database that answers
    "none" has never been used by anything -- a fact that writing a generation
    on the way past would destroy.
    """
    row = transaction.fetchone("SELECT generation FROM journal_identity WHERE singleton = ?", (True,))
    return None if row is None else str(row["generation"])


def _room_history_recovery_from_row(room_id: str, row: Row) -> RoomHistoryRecovery:
    """Decode one durable recovery row into its transport-neutral value."""
    return RoomHistoryRecovery(
        room_id=room_id,
        state=HistoryRecoveryState(str(row["state"])),
        revision=int(row["revision"]),
        attempted_policy_rank=int(row["attempted_policy_rank"]),
    )


def room_history_recovery(
    transaction: Transaction,
    principal_id: str,
    room_id: str,
) -> RoomHistoryRecovery | None:
    """Return one room's current history-recovery obligation, if any."""
    row = transaction.fetchone(
        """
        SELECT state, revision, attempted_policy_rank FROM room_history_recovery
        WHERE principal_id = ? AND room_id = ? AND state <> ?
        """,
        (principal_id, room_id, _REPAIRED_RECOVERY_STATE),
    )
    return None if row is None else _room_history_recovery_from_row(room_id, row)


def _record_room_history_recovery(
    transaction: Transaction,
    principal_id: str,
    room_id: str,
) -> RoomHistoryRecovery | None:
    """Record one unknown gap while serialized with membership fencing; None once the room's departure is fenced."""
    if _claim_membership_state(transaction, principal_id, room_id).departure_fenced:
        return None
    row = transaction.fetchone(
        """
        INSERT INTO room_history_recovery (
            principal_id, room_id, state, revision, attempted_policy_rank
        )
        VALUES (?, ?, ?, 0, 0)
        ON CONFLICT (principal_id, room_id) DO UPDATE SET
            state = excluded.state,
            revision = room_history_recovery.revision + 1,
            attempted_policy_rank = 0
        RETURNING state, revision, attempted_policy_rank
        """,
        (principal_id, room_id, HistoryRecoveryState.REPAIRABLE.value),
    )
    if row is None:
        msg = f"Room history recovery for {room_id!r} is missing immediately after it was written"
        raise RuntimeError(msg)
    return _room_history_recovery_from_row(room_id, row)


def _claim_room_history_recovery(
    transaction: Transaction,
    principal_id: str,
    recovery: RoomHistoryRecovery,
) -> bool:
    """Lock and compare one exact recovery value before installing its answer."""
    row = transaction.fetchone(
        """
        UPDATE room_history_recovery SET state = state
        WHERE principal_id = ? AND room_id = ? AND state = ? AND revision = ?
          AND attempted_policy_rank = ?
        RETURNING room_id
        """,
        (
            principal_id,
            recovery.room_id,
            recovery.state.value,
            recovery.revision,
            recovery.attempted_policy_rank,
        ),
    )
    return row is not None


def _settle_claimed_room_history_recovery(
    transaction: Transaction,
    principal_id: str,
    recovery: RoomHistoryRecovery,
    *,
    exhausted_server: bool,
    attempted_policy_rank: int,
) -> HistoryRecoveryOutcome:
    """Commit the terminal state of a previously claimed recovery obligation."""
    if exhausted_server:
        transaction.execute(
            """
            UPDATE room_history_recovery SET state = ?
            WHERE principal_id = ? AND room_id = ?
            """,
            (_REPAIRED_RECOVERY_STATE, principal_id, recovery.room_id),
        )
        return HistoryRecoveryOutcome.REPAIRED
    transaction.execute(
        """
        UPDATE room_history_recovery SET state = ?, attempted_policy_rank = ?
        WHERE principal_id = ? AND room_id = ?
        """,
        (
            HistoryRecoveryState.TRUNCATED.value,
            attempted_policy_rank,
            principal_id,
            recovery.room_id,
        ),
    )
    return HistoryRecoveryOutcome.TRUNCATED


def install_room_history_recovery_chunk(
    transaction: Transaction,
    principal_id: str,
    recovery: RoomHistoryRecovery,
    *,
    events: tuple[ProjectedEvent, ...],
    expected_membership_epoch: int,
) -> bool:
    """Project one chunk only while its membership and exact recovery stand."""
    if not _claim_recovery(transaction, principal_id, recovery, expected_membership_epoch=expected_membership_epoch):
        return False
    _project_walked(transaction, principal_id, events, membership_epoch=expected_membership_epoch)
    return True


def settle_room_history_recovery(
    transaction: Transaction,
    principal_id: str,
    recovery: RoomHistoryRecovery,
    *,
    exhausted_server: bool,
    unreadable: bool,
    attempted_policy_rank: int,
    expected_membership_epoch: int,
) -> HistoryRecoveryOutcome:
    """Publish a recovery and settle its exact obligation in one transaction."""
    if not _claim_recovery(transaction, principal_id, recovery, expected_membership_epoch=expected_membership_epoch):
        return HistoryRecoveryOutcome.SUPERSEDED
    if exhausted_server and unreadable:
        # Repairing unmasks every marker the gap retracted, and one from before
        # the gap cannot vouch for an event this walk could not read: it may be
        # a reply in that very thread. A truncated obligation already withholds
        # their completeness.
        reads.revoke_room_hydration(transaction, principal_id, room_id=recovery.room_id)
    reads.publish_conversation_hydration(
        transaction,
        principal_id,
        room_id=recovery.room_id,
        thread_id=None,
        complete=exhausted_server and not unreadable,
        attempted_policy_rank=attempted_policy_rank,
        membership_epoch=expected_membership_epoch,
    )
    return _settle_claimed_room_history_recovery(
        transaction,
        principal_id,
        recovery,
        exhausted_server=exhausted_server,
        attempted_policy_rank=attempted_policy_rank,
    )


def install_hydration_chunk(
    transaction: Transaction,
    principal_id: str,
    *,
    room_id: str,
    events: tuple[ProjectedEvent, ...],
    expected_membership_epoch: int,
) -> bool:
    """Project one chunk only while its membership epoch is current."""
    if not claim_membership_epoch(
        transaction,
        principal_id,
        room_id=room_id,
        expected_membership_epoch=expected_membership_epoch,
    ):
        return False
    _project_walked(transaction, principal_id, events, membership_epoch=expected_membership_epoch)
    return True


def _claim_recovery(
    transaction: Transaction,
    principal_id: str,
    recovery: RoomHistoryRecovery,
    *,
    expected_membership_epoch: int,
) -> bool:
    """Lock the room's membership epoch, then the exact recovery, so a write that superseded either wins."""
    return claim_membership_epoch(
        transaction,
        principal_id,
        room_id=recovery.room_id,
        expected_membership_epoch=expected_membership_epoch,
    ) and _claim_room_history_recovery(transaction, principal_id, recovery)


def _project_walked(
    transaction: Transaction,
    principal_id: str,
    events: tuple[ProjectedEvent, ...],
    *,
    membership_epoch: int,
) -> None:
    """Project events a history walk read, which settle no redacted arrival and take no receipt order."""
    for event in events:
        _project_with_redactions(
            transaction,
            principal_id,
            event,
            receipt_order=0,
            membership_epoch=membership_epoch,
            settle_redacted_arrival=False,
        )


def current_membership_epoch(
    transaction: Transaction,
    principal_id: str,
    room_id: str,
) -> int:
    """Return the room's current membership epoch, starting at zero."""
    row = transaction.fetchone(
        "SELECT membership_epoch FROM room_membership WHERE principal_id = ? AND room_id = ?",
        (principal_id, room_id),
    )
    return 0 if row is None else int(row["membership_epoch"])


def ingestion_membership_position(
    transaction: Transaction,
    principal_id: str,
    room_id: str,
) -> RoomMembershipPosition | None:
    """Return the last admitted producer position, independent of journal tenure."""
    row = transaction.fetchone(
        "SELECT membership, membership_epoch FROM matrix_ingestion_membership WHERE principal_id = ? AND room_id = ?",
        (principal_id, room_id),
    )
    if row is None:
        return None
    membership, epoch = row["membership"], row["membership_epoch"]
    if membership not in {"join", "leave"} or type(epoch) is not int or epoch < 0:
        raise IngestionBatchIntegrityError
    return RoomMembershipPosition(membership, epoch)


def _advance_membership_epoch(
    transaction: Transaction,
    principal_id: str,
    room_id: str,
) -> None:
    """Invalidate everything derived for a room the bot has left and rejoined.

    Rejoining can expose a different slice of history than the bot saw before,
    so anything derived from the previous membership has to stop being trusted
    rather than be merged with the new view. Clearing the hydration marker
    alone would not do that: the projected messages it produced would still be
    readable, and the next hydration would merge the two memberships into one
    conversation. The projection is therefore dropped with it, and rebuilt from
    what the new membership can actually see.

    The journal rows survive on purpose. They are the proof that an event
    already produced its one turn, and that has to outlive any rejoin.

    A history-recovery obligation goes with the membership whose missing
    interval it describes. Keeping it would ask the next membership to repair a
    conversation that no longer exists.
    """
    epoch = current_membership_epoch(transaction, principal_id, room_id) + 1
    transaction.execute(
        """
        INSERT INTO room_membership (principal_id, room_id, membership_epoch)
        VALUES (?, ?, ?)
        ON CONFLICT (principal_id, room_id) DO UPDATE SET membership_epoch = excluded.membership_epoch
        """,
        (principal_id, room_id, epoch),
    )
    for table in (
        "conversation_hydration",
        "visible_messages",
        "unresolved_edits",
        "room_history_recovery",
    ):
        transaction.execute(
            f"DELETE FROM {table} WHERE principal_id = ? AND room_id = ?",  # noqa: S608 - a fixed table list
            (principal_id, room_id),
        )
    # A delivery that was never attempted was written for the conversation this
    # bot was in before it left, and nothing outside this process has seen it.
    # Sending it now would answer the previous membership inside the new one.
    # Keep its identity as a retired tombstone: a source-less stream may have
    # enqueued INITIAL just before this fence and still be running, and deleting
    # the row would let FINAL adopt the rejoined membership.
    #
    # An attempted delivery is a different object entirely, and deleting it was
    # the mistake worth naming. Its outcome is unknown: the homeserver may hold
    # it already. Dropping the row frees the turn to run again and post a second
    # answer, and re-deriving a fresh transaction for that answer guarantees the
    # duplicate rather than preventing it. Keeping the row preserves the exact
    # payload, transaction, and sending-device facts needed for recovery:
    # same-device attempts reuse the transaction; changed-device attempts
    # reconcile exact room history before their delivery-specific replay or
    # retain decision. That durable identity is the property this table exists
    # for.
    # A terminal acknowledgement may have committed just before a crash that
    # prevented its card from being retired. Retire it before either side of
    # the cross-principal relationship is fenced or deleted below.
    approval_cards.retire_completed_for_departure(
        transaction,
        principal_id,
        room_id=room_id,
    )
    approval_cards.deny_for_departed_card_owner(
        transaction,
        principal_id,
        room_id=room_id,
        reason="Approval transport left the room.",
    )
    reply_messages.depart_room(transaction, principal_id, room_id, now_ns=time.time_ns())
    transaction.execute(
        """
        UPDATE matrix_delivery_outbox AS delivery SET retired = 1
        WHERE principal_id = ? AND room_id = ? AND acknowledged_event_id IS NULL AND attempted = 0
          AND NOT EXISTS (
              SELECT 1 FROM approval_cards AS cards
              WHERE cards.principal_id = delivery.principal_id
                AND cards.delivery_id = delivery.delivery_id
                AND cards.kind = 'request'
          )
        """,
        (principal_id, room_id),
    )
    # Approval cards are authored by the router principal, while the paused
    # run belongs to the responding entity principal. Decide its calls and keep
    # the router's terminal Matrix edit debt before this fence removes it.
    approval_cards.expire_for_departed_responder(
        transaction,
        principal_id,
        room_id=room_id,
        reason="Requesting agent left the room.",
    )
    # A paused run owns exactly the source rows this fence is about to settle.
    # Delete the aggregate first so no durable continuation survives with no
    # runnable source. Its calls go with it unless a live card still shows them,
    # and the conversation it held is free from this commit.
    for departed in transaction.fetchall(
        f"""
        SELECT approval_id FROM approval_continuations
        WHERE principal_id = ?
          AND {approval_continuations.holds_source_in_room("approval_continuations")}
        """,  # noqa: S608 - a fixed SQL fragment, not input
        (principal_id, room_id),
    ):
        approval_continuations.delete(transaction, principal_id, approval_id=str(departed["approval_id"]))
    # Turn-backed work still pending from the membership that just ended can
    # never finish. Its answer would have to be enqueued, and enqueue refuses
    # any turn whose admitted epoch is not the room's current one -- correctly,
    # because that answer belongs to a conversation this bot is no longer in.
    #
    # Leaving those rows pending makes the refusal permanent rather than final:
    # the worker offers the source again on every replay, the model runs again,
    # and the enqueue refuses again, forever. Settling them here is what turns
    # "cannot be answered" into "will not be attempted". The rows themselves
    # survive, as everything above does, because they are still the proof that
    # these events already had their one turn.
    #
    # Only the turn-backed kinds and reactions already claimed by an interactive
    # response. Other reactions do not enqueue an answer and still owe their
    # hook work. A redaction in particular still owes real cleanup -- removing
    # the redacted request from durable turn and session state -- and sweeping
    # it up here would drop that work silently and let the redacted content
    # survive in later context.
    transaction.execute(
        f"""
        UPDATE journal_events
        SET state = ?, source_json = '', semantic_consumer = NULL
        WHERE principal_id = ? AND room_id = ? AND state = 'pending'
          AND (
            kind IN ({_TURN_BACKED_KIND_PLACEHOLDERS})
            OR (kind = ? AND semantic_consumer = ?)
          )
        """,  # noqa: S608 - placeholders are generated, values are still bound
        (
            SETTLED_STATE,
            principal_id,
            room_id,
            *_TURN_BACKED_KIND_VALUES,
            EventKind.REACTION.value,
            SemanticConsumer.INTERACTIVE_REACTION.value,
        ),
    )


@dataclass(frozen=True, slots=True)
class _MembershipState:
    """One room's application tenure and delivery fence."""

    membership_epoch: int
    departure_fenced: bool


def _claim_membership_state(
    transaction: Transaction,
    principal_id: str,
    room_id: str,
) -> _MembershipState:
    """Create or lock one membership row and decode its exact durable state."""
    row = transaction.fetchone(
        """
        INSERT INTO room_membership (
            principal_id, room_id, membership_epoch,
            departure_fenced
        ) VALUES (?, ?, 0, 0)
        ON CONFLICT (principal_id, room_id) DO UPDATE SET
            membership_epoch = room_membership.membership_epoch
        RETURNING membership_epoch, departure_fenced
        """,
        (principal_id, room_id),
    )
    if row is None:
        raise IngestionBatchIntegrityError
    values = (
        row["membership_epoch"],
        row["departure_fenced"],
    )
    if tuple(map(type, values)) != (int, int):
        raise IngestionBatchIntegrityError
    membership_epoch, departure_fenced = values
    if membership_epoch < 0 or departure_fenced not in (0, 1):
        raise IngestionBatchIntegrityError
    return _MembershipState(
        membership_epoch=membership_epoch,
        departure_fenced=bool(departure_fenced),
    )


def _admit(
    transaction: Transaction,
    principal_id: str,
    event: InboundEvent,
    projected: ProjectedEvent | None,
) -> AdmissionResult:
    """Insert, deduplicate, and project one event in a single transaction.

    A context-only event is projected here and never replayed, so it keeps no
    payload: it is admitted already settled, and settlement is what would
    otherwise have cleared it. Storing the source anyway would turn the journal
    into the raw-event cache this design exists to remove, at roughly half a
    kilobyte for every message the bot has ever seen.
    """
    epoch = _claim_membership_state(transaction, principal_id, event.room_id).membership_epoch
    actionable = event.event_class is EventClass.ACTIONABLE
    row = transaction.fetchone(
        """
        INSERT INTO journal_events (
            principal_id, event_id, room_id, thread_id, kind, sender,
            origin_server_ts, source_json, membership_epoch, state
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (principal_id, event_id) DO NOTHING
        RETURNING receipt_order
        """,
        (
            principal_id,
            event.event_id,
            event.room_id,
            encode_thread_id(event.thread_id),
            event.kind.value,
            event.sender,
            event.origin_server_ts,
            (
                json.dumps(dict(event.source), ensure_ascii=True, separators=(",", ":"), sort_keys=True)
                if actionable
                else ""
            ),
            epoch,
            PENDING_STATE if actionable else SETTLED_STATE,
        ),
    )
    if row is None:
        return AdmissionResult.DUPLICATE
    if projected is not None:
        _project_with_redactions(
            transaction,
            principal_id,
            projected,
            receipt_order=int(row["receipt_order"]),
            membership_epoch=epoch,
            settle_redacted_arrival=True,
        )
    return AdmissionResult.ADMITTED


def admit_snapshotting_source(
    transaction: Transaction,
    principal_id: str,
    event: InboundEvent,
    projected: ProjectedEvent | None,
) -> AdmissionResult:
    """Admit one event as ``_admit`` does, then freeze it as an interactive source once its visible target is durable.

    Batch admission snapshots only the actionable events it admits, after their disposition, so it calls ``_admit``.
    """
    result = _admit(transaction, principal_id, event, projected)
    if result is AdmissionResult.ADMITTED:
        _snapshot_interactive_source(transaction, principal_id, event)
    return result


def _snapshot_interactive_source(
    transaction: Transaction,
    principal_id: str,
    event: InboundEvent,
) -> None:
    """Freeze an interactive source only after its visible target is durable."""
    if interactive_questions.snapshot_source_candidate(
        transaction,
        principal_id,
        event,
    ) and outbox.has_attempted_unacknowledged_prompt_delivery(
        transaction,
        principal_id,
        room_id=event.room_id,
        membership_epoch=current_membership_epoch(transaction, principal_id, event.room_id),
    ):
        msg = f"Matrix delivery projection is pending in room {event.room_id!r}"
        raise DeliveryProjectionPendingError(msg)


def _project_with_redactions(
    transaction: Transaction,
    principal_id: str,
    projected: ProjectedEvent,
    *,
    receipt_order: int,
    membership_epoch: int,
    settle_redacted_arrival: bool,
) -> None:
    """Project one event and apply the redaction authority it reports, in this transaction.

    A redaction is recorded once for the principal's agent and applied to every
    login of it. An event that arrives already redacted ends the replies that
    lost their last source to it, and with ``settle_redacted_arrival`` its own
    pending row retires, as admission does for the event it just admitted.
    """
    redacted_event_id = project(
        transaction,
        principal_id,
        projected,
        receipt_order=receipt_order,
        membership_epoch=membership_epoch,
    )
    if redacted_event_id is None:
        return
    if projected.redacts_event_id is not None:
        redact_for_agent(
            transaction,
            agent_of_principal(principal_id),
            room_id=projected.room_id,
            event_id=redacted_event_id,
            receipt_order=receipt_order,
        )
        return
    if settle_redacted_arrival:
        _settle_redacted_turn_source(
            transaction,
            principal_id,
            room_id=projected.room_id,
            event_id=redacted_event_id,
        )
    replies.end_replies_of_deleted_source(
        transaction,
        principal_id,
        room_id=projected.room_id,
        event_id=redacted_event_id,
    )


def redact_for_agent(
    transaction: Transaction,
    agent_name: str,
    *,
    room_id: str,
    event_id: str,
    receipt_order: int,
) -> None:
    """Record one redaction for every Matrix login of an agent and apply its effects to each, in one transaction.

    The deletion is agent-scoped, as turn truth is: the agent's turn rows lose
    the event's plaintext, and every principal of the agent that has state in
    the room loses it from its projection, retires a pending turn row of it,
    and ends the replies that lost every source to it, exactly as the
    principal that observed the redaction does. An earlier login the agent
    returns to would otherwise still read the deleted text. Another agent's
    rows are never touched, and reads stay principal-scoped: sharing the
    deletion fact shares no content.
    """
    turn_rows.record_redaction(transaction, agent_name, room_id=room_id, event_id=event_id)
    for principal_id in _principals_of_agent(transaction, agent_name, room_id=room_id):
        scrub_redacted_projection(
            transaction,
            principal_id,
            room_id=room_id,
            event_id=event_id,
            receipt_order=receipt_order,
        )
        _settle_redacted_turn_source(transaction, principal_id, room_id=room_id, event_id=event_id)
        replies.end_replies_of_deleted_source(
            transaction,
            principal_id,
            room_id=room_id,
            event_id=event_id,
        )


def _principals_of_agent(transaction: Transaction, agent_name: str, *, room_id: str) -> tuple[str, ...]:
    """Return every Matrix login of one agent that has membership state in ``room_id``.

    A principal is stored as ``agent@mxid`` and agent names hold no ``@``, so the
    prefix names the agent exactly; it is compared here rather than with
    ``LIKE``, which SQLite matches case-insensitively.
    """
    rows = transaction.fetchall(
        "SELECT principal_id FROM room_membership WHERE room_id = ? ORDER BY principal_id/*bytes*/",
        (room_id,),
    )
    return tuple(
        principal_id for row in rows if agent_of_principal(principal_id := str(row["principal_id"])) == agent_name
    )


def _settle_redacted_turn_source(
    transaction: Transaction,
    principal_id: str,
    *,
    room_id: str,
    event_id: str,
) -> None:
    """Retire redacted turn ingress unless a durable continuation owns it.

    Approval continuations dispatch before event-kind ingress, so preserving
    one cannot replay its source through a message or media callback.
    """
    transaction.execute(
        f"""
        UPDATE journal_events
        SET state = ?, source_json = '', semantic_consumer = NULL
        WHERE principal_id = ? AND room_id = ? AND event_id = ? AND state = ?
          AND kind IN ({_TURN_BACKED_KIND_PLACEHOLDERS})
          AND NOT EXISTS (
              SELECT 1 FROM {approval_continuations.HELD_SOURCES}
              WHERE held_source.principal_id = ? AND held_source.event_id = ?
          )
        """,  # noqa: S608 - placeholders are generated, values are still bound
        (
            SETTLED_STATE,
            principal_id,
            room_id,
            event_id,
            PENDING_STATE,
            *_TURN_BACKED_KIND_VALUES,
            principal_id,
            event_id,
        ),
    )


def admitted_thread_id(
    transaction: Transaction,
    principal_id: str,
    *,
    room_id: str,
    event_id: str,
) -> tuple[bool, str | None]:
    """Return whether this event was admitted, and the thread it belongs to.

    Two facts rather than one, because ``None`` is a real answer: an event in
    no thread and an event nobody here has seen are opposite situations, and
    only the second is worth a homeserver round trip.

    The journal records the MSC3440 root from the event's own relation, which
    is what a caller resolving thread membership is asking for.
    """
    row = transaction.fetchone(
        """
        SELECT thread_id FROM journal_events
        WHERE principal_id = ? AND room_id = ? AND event_id = ?
        """,
        (principal_id, room_id, event_id),
    )
    if row is None:
        return False, None
    return True, decode_thread_id(row["thread_id"])


def admitted_membership_owner(
    transaction: Transaction,
    principal_id: str,
    event_id: str,
) -> tuple[str, int] | None:
    """Return the room and membership that admitted one event, or nothing.

    Nothing means no membership: the caller named something the journal never
    admitted -- a scheduled task, a hook-authored turn -- and there is no
    previous membership for its work to belong to.

    The row survives every fence on purpose, so this answer stays available
    for as long as the turn it authorized can still be running.
    """
    row = transaction.fetchone(
        "SELECT room_id, membership_epoch FROM journal_events WHERE principal_id = ? AND event_id = ?",
        (principal_id, event_id),
    )
    return None if row is None else (str(row["room_id"]), int(row["membership_epoch"]))


def pending(
    transaction: Transaction,
    principal_id: str,
    *,
    limit: int,
    room_id: str | None = None,
    after_receipt_order: int | None = None,
    runtime_generation: str = "unmanaged",
) -> PendingPage:
    """Return actionable events awaiting semantic work, in receipt order.

    ``after_receipt_order`` resumes the scan past rows a caller has already
    seen. Without it, a caller whose first page is entirely events it cannot
    act on yet — turns still running — could never reach the ones behind them.

    ``limit`` bounds the rows read, not the events returned. An unreadable row
    is dropped from the result but not from the backlog, so a page can come
    back shorter than its limit — or empty — with work still behind it. The
    page says which of those happened rather than leaving the caller to infer
    it from a length: ``reached_end`` is the only statement that there is
    nothing behind this page, and ``resume_after`` is where the next pass
    starts, counted in rows looked at rather than events returned. One query, one page: reading further because too
    few rows decoded is how a corrupt prefix turned a bounded read into a scan of the whole pending table, so the bound
    is on the rows the query may return and nothing here goes back for more.
    """
    rows = _pending_rows(
        transaction,
        principal_id,
        limit=limit,
        room_id=room_id,
        after_receipt_order=after_receipt_order,
        runtime_generation=runtime_generation,
    )
    events = _decode_rows(rows)
    return PendingPage(
        events,
        # The last row looked at, which is not the last event returned when the
        # tail of the page is unreadable. Taken from the events, a resume point
        # would step back onto that row on every pass and never get past it.
        resume_after=int(rows[-1]["receipt_order"]) if rows else after_receipt_order,
        # Fewer rows than the query was allowed to return means there were no
        # more to give, whatever the page's length turned out to be.
        reached_end=len(rows) < limit,
        unreadable_rows=len(rows) - len(events),
    )


def _pending_rows(
    transaction: Transaction,
    principal_id: str,
    *,
    limit: int,
    after_receipt_order: int | None,
    room_id: str | None,
    runtime_generation: str,
) -> tuple[Row, ...]:
    """Return one raw page of pending rows, in receipt order."""
    cursor_clause = "" if after_receipt_order is None else " AND receipt_order > ?"
    cursor_params: tuple[object, ...] = () if after_receipt_order is None else (after_receipt_order,)
    room_clause = "" if room_id is None else " AND events.room_id = ?"
    room_params: tuple[object, ...] = () if room_id is None else (room_id,)
    continuation_joins = f"""
        LEFT JOIN ({approval_continuations.HELD_SOURCES})
          ON held_source.principal_id = events.principal_id
         AND held_source.event_id = events.event_id
        LEFT JOIN reply_spans AS claim_span
          ON claim_span.principal_id = continuations.principal_id
         AND claim_span.span_id = continuations.claim_span_id
    """
    # A claim this instance's span holds keeps its sources until its FINAL
    # exists; one an older instance left goes to recovery.
    continuation_clause = """
          AND (
            continuations.approval_id IS NULL
            OR (
              held_source.ordinal = 0
              AND (
                continuations.state = 'failing'
                OR (
                  continuations.state = 'waiting'
                  AND continuations.runtime_generation IS NOT NULL
                  AND continuations.runtime_generation <> ?
                )
                OR (
                  continuations.state = 'ready'
                  AND (
                    continuations.claim_span_id IS NULL
                    OR claim_span.bot_generation <> ?
                    OR EXISTS (
                      SELECT 1 FROM matrix_delivery_outbox AS approval_final
                      WHERE approval_final.principal_id = events.principal_id
                        AND approval_final.delivery_id = events.event_id
                        AND approval_final.stage = 'final'
                    )
                  )
                )
              )
            )
          )
    """
    continuation_params = (runtime_generation, runtime_generation)
    return transaction.fetchall(
        f"""
        SELECT {_EVENT_JOURNAL_COLUMNS} FROM journal_events AS events
        {continuation_joins}
        WHERE events.principal_id = ? AND events.state = 'pending'
          {continuation_clause}{room_clause}{cursor_clause}
        ORDER BY events.receipt_order
        LIMIT ?
        """,  # noqa: S608 - a fixed column list and fixed clauses, not input
        (principal_id, *continuation_params, *room_params, *cursor_params, limit),
    )


def pending_thread_events_after(
    transaction: Transaction,
    principal_id: str,
    *,
    room_id: str,
    thread_id: str,
    after_origin_server_ts: int,
    excluding_event_id: str,
    limit: int,
) -> tuple[JournalEvent, ...]:
    """Return unsettled turn-backed events in one thread newer than a timestamp, oldest first.

    The set a replay guard asks about: work this bot accepted in the
    conversation it is about to answer and has not finished. Restricting it to
    pending rows is not an optimization. Settlement clears the replay payload,
    so a settled row has no body left to inspect -- and it is the wrong answer
    anyway, because an event that already settled will never produce the turn
    that would supersede an older one.

    Restricting it to ``TURN_BACKED_KINDS`` is the same argument one step
    further: pending means unfinished, not *will answer*. Thread membership is
    derived from content for every kind alike -- ``_inbound_event`` calls
    ``thread_root`` regardless of kind -- so a reaction, an approval, or an
    ``m.room.encrypted`` event this bot could not decrypt can all sit pending
    in a thread under the requester's own sender. An interactive reaction can
    produce a response, but only as a continuation of its selected question;
    it is not a newer conversation turn that supersedes older ingress.
    Counting one as such can drop the older message without replacing it.
    Only a message or a media event can become the turn that legitimately
    supersedes another.

    Strictly newer. Two events stamped in the same millisecond are not ordered
    by their timestamps, and treating either as proof that the other is stale
    would drop a message on a coin flip.
    """
    rows = transaction.fetchall(
        f"""
        SELECT {_JOURNAL_COLUMNS} FROM journal_events
        WHERE principal_id = ? AND state = 'pending'
          AND room_id = ? AND thread_id = ?
          AND kind IN ({_TURN_BACKED_KIND_PLACEHOLDERS})
          AND origin_server_ts > ? AND event_id <> ?
        ORDER BY origin_server_ts, receipt_order
        LIMIT ?
        """,  # noqa: S608 - a fixed column list and generated placeholders, not interpolated input
        (
            principal_id,
            room_id,
            encode_thread_id(thread_id),
            *_TURN_BACKED_KIND_VALUES,
            after_origin_server_ts,
            excluding_event_id,
            limit,
        ),
    )
    return _decode_rows(rows)


def _decode_rows(rows: tuple[Row, ...]) -> tuple[JournalEvent, ...]:
    """Decode pending rows, skipping any whose payload cannot be read.

    One unreadable row must not hide every other pending event behind it.
    A row that cannot be decoded stays in place rather than being settled,
    because settling it would claim work was done that never ran.
    """
    events: list[JournalEvent] = []
    for row in rows:
        try:
            events.append(_journal_event(row))
        except (TypeError, ValueError, json.JSONDecodeError):
            logger.exception("journal_event_row_unreadable", event_id=row["event_id"])
    return tuple(events)


def load(
    transaction: Transaction,
    principal_id: str,
    event_id: str,
) -> JournalEvent | None:
    """Return one admitted event regardless of its settlement state."""
    row = transaction.fetchone(
        f"SELECT {_JOURNAL_COLUMNS} FROM journal_events WHERE principal_id = ? AND event_id = ?",  # noqa: S608
        (principal_id, event_id),
    )
    return None if row is None else _journal_event(row)


def is_pending(transaction: Transaction, principal_id: str, event_id: str) -> bool:
    """Return whether one event still owes semantic work."""
    row = transaction.fetchone(
        "SELECT 1 AS present FROM journal_events WHERE principal_id = ? AND event_id = ? AND state = 'pending'",
        (principal_id, event_id),
    )
    return row is not None


def pending_among(transaction: Transaction, principal_id: str, event_ids: tuple[str, ...]) -> frozenset[str]:
    """Return which of these events still owe semantic work, in bounded batches."""
    pending: set[str] = set()
    for batch in batched(event_ids, _PENDING_AMONG_BATCH, strict=False):
        rows = transaction.fetchall(
            "SELECT event_id FROM journal_events WHERE principal_id = ? AND state = 'pending' "  # noqa: S608
            f"AND event_id IN ({', '.join('?' for _ in batch)})",
            (principal_id, *batch),
        )
        pending.update(str(row["event_id"]) for row in rows)
    return frozenset(pending)


def _source_has_redaction_handoff(
    transaction: Transaction,
    principal_id: str,
    event_id: str,
    captured: TurnRecord,
) -> bool:
    """Prove the settled source was redacted in its own room, which is the turn's redaction authority.

    Session history needs no handoff: each response removes history derived from redacted events.
    """
    source = transaction.fetchone(
        "SELECT room_id FROM journal_events WHERE principal_id = ? AND event_id = ? AND state = 'settled'",
        (principal_id, event_id),
    )
    if source is None or not is_redacted(transaction, principal_id, source["room_id"], event_id):
        return False
    return captured.conversation_target is None or captured.conversation_target.room_id == source["room_id"]


def _sources_settled_by_departure(
    transaction: Transaction,
    principal_id: str,
    event_ids: tuple[str, ...],
) -> bool:
    """Prove every exact retained source is settled under an ended membership."""
    return bool(event_ids) and all(
        transaction.fetchone(
            """
            SELECT 1 AS present FROM journal_events AS event
            JOIN room_membership AS membership
              ON membership.principal_id = event.principal_id AND membership.room_id = event.room_id
            WHERE event.principal_id = ? AND event.event_id = ? AND event.state = 'settled'
              AND event.membership_epoch < membership.membership_epoch
            """,
            (principal_id, event_id),
        )
        is not None
        for event_id in event_ids
    )


def response_recovery_state(
    transaction: Transaction,
    principal_id: str,
    turn_record: TurnRecord,
) -> ResponseRecoveryState:
    """Return one response's durable handoff: its sources, turn records, FINAL delivery, and reply."""
    source_event_ids = turn_record.source_event_ids
    turn_id = turn_record.anchor_event_id
    pending = tuple(is_pending(transaction, principal_id, event_id) for event_id in source_event_ids)
    delivery = (
        None
        if turn_id is None
        else outbox.load(
            transaction,
            principal_id,
            delivery_id=turn_id,
            stage=DeliveryStage.FINAL,
        )
    )
    agent_name = agent_of_principal(principal_id)
    return ResponseRecoveryState(
        pending_sources=pending,
        redacted_sources=tuple(
            not source_pending and _source_has_redaction_handoff(transaction, principal_id, event_id, turn_record)
            for event_id, source_pending in zip(source_event_ids, pending, strict=True)
        ),
        turn_records=tuple(
            turn_rows.get_turn_record(transaction, agent_name, event_id) for event_id in turn_record.indexed_event_ids
        ),
        final_delivery=delivery,
        sources_settled_by_departure=(
            not any(pending) and _sources_settled_by_departure(transaction, principal_id, source_event_ids)
        ),
        reply=reply_messages.for_sources(transaction, principal_id, source_event_ids),
    )


def _settle(transaction: Transaction, principal_id: str, event_id: str) -> None:
    """Mark one event's semantic work terminal and release its replay payload.

    The payload is cleared rather than the row deleted: the row is the proof
    that this event already produced its one turn, and it has to outlive the
    work it authorized.

    Settled is the whole source fact. Why ordinary work ended -- answered, or
    deliberately not answered -- was recorded for a while and never once read
    back. An interactive prompt revision is different: its consumed-by source
    remains on that immutable revision so projection repair cannot revive it.
    """
    transaction.execute(
        """
        UPDATE journal_events
        SET state = ?, source_json = '', semantic_consumer = NULL
        WHERE principal_id = ? AND event_id = ? AND state = 'pending'
        """,
        (SETTLED_STATE, principal_id, event_id),
    )
    transaction.execute(
        "DELETE FROM interactive_selections WHERE principal_id = ? AND source_event_id = ?",
        (principal_id, event_id),
    )


def settle_many(transaction: Transaction, principal_id: str, event_ids: tuple[str, ...]) -> None:
    """Settle several events that one terminal turn accounted for."""
    for event_id in event_ids:
        _settle(transaction, principal_id, event_id)


def settle_turn_sources(transaction: Transaction, principal_id: str, event_ids: tuple[str, ...]) -> tuple[str, ...]:
    """Settle a terminal turn's sources, ending the replies left waiting to replay them; return those replies."""
    settle_many(transaction, principal_id, event_ids)
    return replies.drop_replays(transaction, principal_id, event_ids, now_ns=time.time_ns())


def unsettled_event_ids(transaction: Transaction, principal_id: str) -> frozenset[str]:
    """Return every event that still owes semantic work."""
    rows = transaction.fetchall(
        "SELECT event_id FROM journal_events WHERE principal_id = ? AND state = 'pending'",
        (principal_id,),
    )
    return frozenset(row["event_id"] for row in rows)


def claim_semantic_consumer(
    transaction: Transaction,
    principal_id: str,
    event_id: str,
    consumer: SemanticConsumer,
) -> SemanticConsumer | None:
    """Record the sole consumer, or retire a stale interactive reaction.

    First claim wins, durably. A replay after a crash therefore cannot let a
    second consumer act on the same reaction.
    """
    row = transaction.fetchone(
        """
        UPDATE journal_events
        SET semantic_consumer = COALESCE(semantic_consumer, ?)
        WHERE principal_id = ? AND event_id = ? AND state = 'pending'
        RETURNING semantic_consumer, room_id, kind, membership_epoch
        """,
        (consumer.value, principal_id, event_id),
    )
    if row is None:
        msg = f"Cannot claim a consumer for settled or missing event {event_id!r}"
        raise RuntimeError(msg)
    claimed = SemanticConsumer(row["semantic_consumer"])
    if (
        claimed is SemanticConsumer.INTERACTIVE_REACTION
        and EventKind(row["kind"]) is EventKind.REACTION
        and int(row["membership_epoch"]) != current_membership_epoch(transaction, principal_id, row["room_id"])
    ):
        _settle(transaction, principal_id, event_id)
        return None
    return claimed


def _journal_event(row: Row) -> JournalEvent:
    source = json.loads(row["source_json"]) if row["source_json"] else {}
    if not isinstance(source, dict):
        msg = f"Journal event {row['event_id']!r} has a non-object source"
        raise TypeError(msg)
    return JournalEvent(
        event_id=row["event_id"],
        room_id=row["room_id"],
        thread_id=decode_thread_id(row["thread_id"]),
        kind=EventKind(row["kind"]),
        sender=row["sender"],
        origin_server_ts=int(row["origin_server_ts"]),
        source=source,
        receipt_order=int(row["receipt_order"]),
        semantic_consumer=(
            SemanticConsumer(row["semantic_consumer"]) if row["semantic_consumer"] is not None else None
        ),
    )

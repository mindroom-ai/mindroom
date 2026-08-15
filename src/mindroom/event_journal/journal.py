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
from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, cast
from uuid import UUID

from mindroom.history_recovery import (
    HistoryRecoveryOutcome,
    HistoryRecoveryState,
    RoomHistoryRecovery,
)
from mindroom.logging_config import get_logger

from .identity import decode_thread_id, encode_thread_id
from .models import (
    TURN_BACKED_KINDS,
    AdmissionFacts,
    AdmissionResult,
    DepartureObservation,
    DepartureOutcome,
    DepartureSource,
    EventClass,
    EventKind,
    InboundEvent,
    IngestionBatchAdmission,
    IngestionBatchIntegrityError,
    IngestionBatchSequenceError,
    IngestionBatchValidationError,
    IngestionConsumerBindingError,
    IngestionRecordDisposition,
    JournalEvent,
    PendingPage,
    RoomMembershipPosition,
    SemanticConsumer,
)
from .projection import ProjectedEvent, project
from .schema import PENDING_STATE, SETTLED_STATE

if TYPE_CHECKING:
    from .backend import Row, Transaction

logger = get_logger(__name__)

_JOURNAL_COLUMNS = """
    event_id, room_id, thread_id, kind, sender,
    origin_server_ts, source_json, receipt_order, semantic_consumer
"""
# Successful repair is hidden from callers but retained as the revision carrier,
# so a later gap cannot reuse the identity of an old in-flight walk.
_REPAIRED_RECOVERY_STATE = "repaired"
_MATRIX_MEMBERSHIPS = frozenset({"ban", "invite", "join", "knock", "leave"})


def validate_ingestion_batch_admission(  # noqa: PLR0915 - exact XOR grammar
    admission: object,
) -> None:
    """Reject any carrier outside the exact durable-admission grammar."""
    invalid = IngestionBatchValidationError("Invalid ingestion batch admission")
    if type(admission) is not IngestionBatchAdmission:
        raise invalid

    def require(condition: object) -> None:
        if not condition:
            raise invalid

    item = admission
    schema, sequence = item.schema_version, item.sequence
    require(type(schema) is type(sequence) is int)
    require(schema == 1 and 0 <= sequence <= 2**63 - 2)
    require(type(item.consumer_generation) is type(item.stream_id) is UUID)
    require(type(item.sha256) is bytes and len(item.sha256) == 32)
    require(type(item.record_id) is str and bool(item.record_id))
    require(type(item.disposition) is IngestionRecordDisposition)

    effect = item.disposition
    source = item.source
    room_id = item.room_id
    previous_membership = item.previous_membership
    membership = item.membership
    previous_epoch = item.previous_membership_epoch
    epoch = item.membership_epoch
    e = item.event
    p = item.projected
    if effect is IngestionRecordDisposition.COMPATIBILITY_ONLY:
        require(
            all(
                value is None
                for value in (
                    room_id,
                    source,
                    previous_membership,
                    membership,
                    previous_epoch,
                    epoch,
                    e,
                    p,
                )
            ),
        )
        return
    if effect is IngestionRecordDisposition.HISTORY_LOSS:
        require(type(room_id) is str and bool(room_id))
        require(
            all(
                value is None
                for value in (
                    source,
                    previous_membership,
                    membership,
                    previous_epoch,
                    epoch,
                    e,
                    p,
                )
            ),
        )
        return
    if effect is IngestionRecordDisposition.ROOM_LIFECYCLE:
        require(type(source) is DepartureSource)
        require(type(room_id) is str and bool(room_id))
        require(type(membership) is str and membership in _MATRIX_MEMBERSHIPS)
        require(type(previous_epoch) is type(epoch) is int)
        lifecycle_previous_epoch = cast("int", previous_epoch)
        lifecycle_epoch = cast("int", epoch)
        require(lifecycle_previous_epoch >= 0)
        if previous_membership is None:
            require(
                source is DepartureSource.REPORTED and lifecycle_previous_epoch == lifecycle_epoch == 0,
            )
        else:
            require(
                type(previous_membership) is str
                and previous_membership in _MATRIX_MEMBERSHIPS
                and previous_membership != membership,
            )
            departure = previous_membership == "join" and membership != "join"
            require(lifecycle_epoch == lifecycle_previous_epoch + int(departure))
        require(e is None and p is None)
        return

    require(effect is IngestionRecordDisposition.SEMANTIC_EVENT)
    require(
        all(
            value is None
            for value in (
                source,
                room_id,
                previous_membership,
                membership,
                previous_epoch,
                epoch,
            )
        ),
    )
    require(type(e) is InboundEvent)
    event = cast("InboundEvent", e)
    require(all(type(v) is str and v for v in (event.event_id, event.room_id, event.sender)))
    require(event.thread_id is None or (type(event.thread_id) is str and bool(event.thread_id)))
    require(type(event.kind) is EventKind and type(event.event_class) is EventClass)
    require(type(event.origin_server_ts) is int and isinstance(event.source, Mapping))
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


def _matching_semantic_event_exists(
    transaction: Transaction,
    principal_id: str,
    event: InboundEvent,
) -> bool:
    row = transaction.fetchone(
        """
        SELECT room_id, thread_id, kind, sender, origin_server_ts
        FROM journal_events
        WHERE principal_id = ? AND event_id = ?
        """,
        (principal_id, event.event_id),
    )
    if row is None:
        return False
    retained = tuple(row[column] for column in ("room_id", "thread_id", "kind", "sender", "origin_server_ts"))
    expected = (
        event.room_id,
        encode_thread_id(event.thread_id),
        event.kind.value,
        event.sender,
        event.origin_server_ts,
    )
    if tuple(map(type, retained)) != (str, str, str, str, int) or retained != expected:
        raise IngestionBatchIntegrityError
    return True


def _require_matching_semantic_event(
    transaction: Transaction,
    principal_id: str,
    event: InboundEvent,
) -> None:
    if not _matching_semantic_event_exists(transaction, principal_id, event):
        raise IngestionBatchIntegrityError


def _admit_suppressed_semantic_identity(
    transaction: Transaction,
    principal_id: str,
    event: InboundEvent,
) -> None:
    """Retain a settled identity for turn work fenced out of this tenure."""
    result = admit(
        transaction,
        principal_id,
        replace(event, event_class=EventClass.CONTEXT_ONLY),
        None,
    )
    if result is AdmissionResult.ADMITTED:
        return
    if result is AdmissionResult.DUPLICATE:
        _require_matching_semantic_event(transaction, principal_id, event)
        return
    raise IngestionBatchIntegrityError


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
    semantic_result = admit(transaction, principal_id, event, projected)
    if semantic_result is AdmissionResult.ADMITTED:
        return event.event_class is EventClass.ACTIONABLE
    if semantic_result is AdmissionResult.DUPLICATE:
        _require_matching_semantic_event(transaction, principal_id, event)
        return False
    raise IngestionBatchIntegrityError


def _apply_ingestion_disposition(
    transaction: Transaction,
    principal_id: str,
    admission: IngestionBatchAdmission,
) -> bool:
    """Apply one validated record effect, returning whether it dispatches."""
    disposition = admission.disposition
    if disposition is IngestionRecordDisposition.COMPATIBILITY_ONLY:
        return False

    if disposition is IngestionRecordDisposition.ROOM_LIFECYCLE:
        room_id = cast("str", admission.room_id)
        source = cast("DepartureSource", admission.source)
        if admission.previous_membership == "join":
            state = _claim_membership_state(transaction, principal_id, room_id)
            if source is DepartureSource.LOCAL and admission.previous_membership_epoch != state.membership_epoch:
                raise IngestionBatchIntegrityError
            _fence_departure_from_state(
                transaction,
                principal_id,
                room_id,
                source=source,
                state=state,
            )
            return False
        if admission.membership == "join":
            state = _claim_membership_state(transaction, principal_id, room_id)
            membership_epoch = cast("int", admission.membership_epoch)
            if source is DepartureSource.REPORTED and membership_epoch < state.membership_epoch:
                # A delayed source echo cannot undo a newer durable local departure.
                return False
            if membership_epoch != state.membership_epoch:
                raise IngestionBatchIntegrityError
            note_membership_restarted(transaction, principal_id, room_id)
        return False

    if disposition is IngestionRecordDisposition.HISTORY_LOSS:
        room_id = cast("str", admission.room_id)
        state = _claim_membership_state(transaction, principal_id, room_id)
        _record_room_history_recovery_locked(
            transaction,
            principal_id,
            room_id,
            state,
        )
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
    """Atomically claim and persist one authenticated ingestion record."""
    validate_ingestion_batch_admission(admission)
    generation, stream = str(admission.consumer_generation), str(admission.stream_id)
    row = transaction.fetchone("UPDATE matrix_sync_consumers SET next_sequence = next_sequence + 1 WHERE principal_id = ? AND consumer_generation = ? AND stream_id = ? AND next_sequence = ? AND next_sequence < 9223372036854775807 RETURNING consumer_generation, stream_id, next_sequence", (principal_id, generation, stream, admission.sequence))  # fmt: skip
    if row is None:
        state = transaction.fetchone("SELECT c.consumer_generation AS c_generation, c.stream_id AS c_stream, c.next_sequence AS c_next, r.schema_version AS r_schema, r.batch_sha256 AS r_sha256, r.record_id AS r_record_id FROM matrix_sync_consumers AS c LEFT JOIN matrix_ingestion_receipts AS r ON r.principal_id = c.principal_id AND r.consumer_generation = c.consumer_generation AND r.stream_id = c.stream_id AND r.sequence = ? WHERE c.principal_id = ?", (admission.sequence, principal_id))  # fmt: skip
        if state is None:
            raise IngestionConsumerBindingError
        owner = state["c_generation"], state["c_stream"]
        if tuple(map(type, owner)) != (str, str) or owner != (generation, stream):
            raise IngestionConsumerBindingError
        frontier = state["c_next"]
        if type(frontier) is not int or not 0 <= frontier <= 2**63 - 1:
            raise IngestionBatchIntegrityError
        if admission.sequence == frontier - 1:
            receipt = state["r_schema"], state["r_sha256"], state["r_record_id"]
            expected = 1, admission.sha256.hex(), admission.record_id
            if tuple(map(type, receipt)) != (int, str, str) or receipt != expected:
                raise IngestionBatchIntegrityError
            return AdmissionFacts(receipt_new=False, semantic_event_new=False)
        if admission.sequence < frontier - 1 or admission.sequence > frontier:
            raise IngestionBatchSequenceError
        raise IngestionBatchIntegrityError
    owner = row["consumer_generation"], row["stream_id"]
    if not all(type(value) is str for value in owner) or owner != (generation, stream):
        raise IngestionConsumerBindingError
    next_sequence = row["next_sequence"]
    if type(next_sequence) is not int or next_sequence != admission.sequence + 1:
        raise IngestionBatchIntegrityError
    semantic_event_new = _apply_ingestion_disposition(
        transaction,
        principal_id,
        admission,
    )
    transaction.execute("INSERT INTO matrix_ingestion_receipts (principal_id, consumer_generation, stream_id, sequence, schema_version, batch_sha256, record_id) VALUES (?, ?, ?, ?, 1, ?, ?)", (principal_id, generation, stream, admission.sequence, admission.sha256.hex(), admission.record_id))  # fmt: skip
    return AdmissionFacts(receipt_new=True, semantic_event_new=semantic_event_new)


def store_generation(transaction: Transaction, *, new_generation: str) -> str:
    """Return this database's generation, minting it on first use.

    A Matrix sync token only means something beside the store that consumed the
    events it already covers. Resume from a token saved before this database
    existed and every event between is skipped silently -- the homeserver
    considers them delivered and will not send them again, and nothing
    downstream can tell the difference between "no messages" and "the messages
    went to a database that is gone".

    So the token is saved next to a generation, and a checkpoint naming a
    different one is refused. ``new_generation`` is only used if no row exists;
    an established database keeps the value it was born with, which is what
    makes the comparison mean "same database" rather than "same process".
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
    )


def room_history_recovery(
    transaction: Transaction,
    principal_id: str,
    room_id: str,
) -> RoomHistoryRecovery | None:
    """Return one room's current history-recovery obligation, if any."""
    row = transaction.fetchone(
        """
        SELECT state, revision FROM room_history_recovery
        WHERE principal_id = ? AND room_id = ? AND state <> ?
        """,
        (principal_id, room_id, _REPAIRED_RECOVERY_STATE),
    )
    return None if row is None else _room_history_recovery_from_row(room_id, row)


def _record_room_history_recovery_locked(
    transaction: Transaction,
    principal_id: str,
    room_id: str,
    state: _DepartureState,
) -> RoomHistoryRecovery | None:
    """Record one gap after the caller has locked its membership state."""
    if state.departure_fenced:
        return None
    row = transaction.fetchone(
        """
        INSERT INTO room_history_recovery (principal_id, room_id, state, revision)
        VALUES (?, ?, ?, 0)
        ON CONFLICT (principal_id, room_id) DO UPDATE SET
            state = excluded.state,
            revision = room_history_recovery.revision + 1
        RETURNING state, revision
        """,
        (principal_id, room_id, HistoryRecoveryState.REPAIRABLE.value),
    )
    if row is None:
        msg = f"Room history recovery for {room_id!r} is missing immediately after it was written"
        raise RuntimeError(msg)
    return _room_history_recovery_from_row(room_id, row)


def record_room_history_recovery(
    transaction: Transaction,
    principal_id: str,
    room_id: str,
) -> RoomHistoryRecovery | None:
    """Record one unknown gap while serialized with membership fencing."""
    state = _claim_membership_state(transaction, principal_id, room_id)
    return _record_room_history_recovery_locked(
        transaction,
        principal_id,
        room_id,
        state,
    )


def claim_room_history_recovery(
    transaction: Transaction,
    principal_id: str,
    recovery: RoomHistoryRecovery,
) -> bool:
    """Lock and compare one exact recovery value before installing its answer."""
    row = transaction.fetchone(
        """
        UPDATE room_history_recovery SET state = state
        WHERE principal_id = ? AND room_id = ? AND state = ? AND revision = ?
        RETURNING room_id
        """,
        (
            principal_id,
            recovery.room_id,
            recovery.state.value,
            recovery.revision,
        ),
    )
    return row is not None


def settle_room_history_recovery(
    transaction: Transaction,
    principal_id: str,
    recovery: RoomHistoryRecovery,
    *,
    exhausted_server: bool,
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
        UPDATE room_history_recovery SET state = ?
        WHERE principal_id = ? AND room_id = ?
        """,
        (HistoryRecoveryState.TRUNCATED.value, principal_id, recovery.room_id),
    )
    return HistoryRecoveryOutcome.TRUNCATED


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


def membership_position(
    transaction: Transaction,
    principal_id: str,
    room_id: str,
) -> RoomMembershipPosition:
    """Return the exact durable prior position for a local join or leave."""
    row = transaction.fetchone(
        "SELECT membership_epoch, departure_fenced FROM room_membership WHERE principal_id = ? AND room_id = ?",
        (principal_id, room_id),
    )
    if row is None:
        return RoomMembershipPosition("leave", 0)
    membership_epoch = row["membership_epoch"]
    departure_fenced = row["departure_fenced"]
    if (
        type(membership_epoch) is not int
        or membership_epoch < 0
        or type(departure_fenced) is not int
        or departure_fenced not in (0, 1)
    ):
        raise IngestionBatchIntegrityError
    return RoomMembershipPosition(
        "leave" if departure_fenced else "join",
        membership_epoch,
    )


def _advance_membership_epoch(
    transaction: Transaction,
    principal_id: str,
    room_id: str,
) -> int:
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
        "redaction_tombstones",
        "room_history_recovery",
    ):
        transaction.execute(
            f"DELETE FROM {table} WHERE principal_id = ? AND room_id = ?",  # noqa: S608 - a fixed table list
            (principal_id, room_id),
        )
    # A delivery that was never attempted was written for the conversation this
    # bot was in before it left, and nothing outside this process has seen it.
    # Sending it now would answer the previous membership inside the new one.
    #
    # An attempted delivery is a different object entirely, and deleting it was
    # the mistake worth naming. Its outcome is unknown: the homeserver may hold
    # it already. Dropping the row frees the turn to run again and post a second
    # answer, and re-deriving a fresh transaction for that answer guarantees the
    # duplicate rather than preventing it. Keeping the row keeps the frozen
    # payload and the transaction that goes with it, so the only thing a retry
    # can do is present the same transaction again and collapse onto the same
    # event. That converges on exactly one visible answer whether or not the
    # first attempt landed, which is the property this table exists for.
    transaction.execute(
        """
        DELETE FROM response_outbox
        WHERE principal_id = ? AND room_id = ? AND acknowledged_event_id IS NULL AND attempted = 0
        """,
        (principal_id, room_id),
    )
    # An approval card goes the other way from an attempted delivery, and the
    # asymmetry is the same one the recovery pass already makes: a lost answer
    # is unacceptable, so the outbox risks a duplicate; a card is a question,
    # and asking it twice gets one answer to two prompts. Nothing here can
    # duplicate anything either way, because the turn that would have posted a
    # second card is settled just below rather than left to run again.
    #
    # So the only question is whether these rows are worth keeping, and they
    # are not. Every read of a card is filtered to the room's current
    # membership -- deliberately, because expiring or redelivering one would
    # edit an event in a conversation this bot has already stopped trusting,
    # answering a question nobody in the new membership asked. Kept, they are
    # rows with no reader and no other remover, still answering writes keyed on
    # the card's event rather than on the epoch. Dropped, they simply stop
    # existing at the same moment they stopped meaning anything.
    transaction.execute(
        "DELETE FROM approval_cards WHERE principal_id = ? AND room_id = ?",
        (principal_id, room_id),
    )
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
    # Only the turn-backed kinds. A redaction, a reaction, an approval reply
    # and a decryption failure do not enqueue an answer, so the epoch predicate
    # never blocks them and none of them is unanswerable. A redaction in
    # particular still owes real cleanup -- removing the redacted request from
    # durable turn and session state -- and sweeping it up here would drop that
    # work silently and let the redacted content survive in later context.
    turn_backed = tuple(sorted(kind.value for kind in TURN_BACKED_KINDS))
    kind_placeholders = ", ".join("?" for _ in turn_backed)
    transaction.execute(
        f"""
        UPDATE journal_events
        SET state = ?, source_json = '', semantic_consumer = NULL
        WHERE principal_id = ? AND room_id = ? AND state = 'pending'
          AND kind IN ({kind_placeholders})
        """,  # noqa: S608 - placeholders are generated, values are still bound
        (SETTLED_STATE, principal_id, room_id, *turn_backed),
    )
    return epoch


def _fence_departure_from_state(
    transaction: Transaction,
    principal_id: str,
    room_id: str,
    *,
    source: DepartureSource,
    state: _DepartureState,
) -> DepartureOutcome:
    """Apply one departure after its room-membership row has been locked."""
    if source is DepartureSource.REPORTED and state.owed_reports > 0:
        owed_reports = state.owed_reports - 1
        _write_departure_state(
            transaction,
            principal_id,
            room_id,
            membership_epoch=state.membership_epoch,
            departure_fenced=state.departure_fenced,
            owed_reports=owed_reports,
        )
        return DepartureOutcome(
            observation=DepartureObservation.OWED_REPORT_CONSUMED,
            membership_epoch=state.membership_epoch,
            owed_reports=owed_reports,
        )
    if state.departure_fenced:
        return DepartureOutcome(
            observation=DepartureObservation.ALREADY_FENCED,
            membership_epoch=state.membership_epoch,
            owed_reports=state.owed_reports,
        )
    membership_epoch = _advance_membership_epoch(transaction, principal_id, room_id)
    owed_reports = state.owed_reports + 1 if source is DepartureSource.LOCAL else state.owed_reports
    _write_departure_state(
        transaction,
        principal_id,
        room_id,
        membership_epoch=membership_epoch,
        departure_fenced=True,
        owed_reports=owed_reports,
    )
    return DepartureOutcome(
        observation=DepartureObservation.FENCED,
        membership_epoch=membership_epoch,
        owed_reports=owed_reports,
    )


def fence_departure(
    transaction: Transaction,
    principal_id: str,
    room_id: str,
    *,
    source: DepartureSource,
) -> DepartureOutcome:
    """Invalidate a room's derived state once per departure, however often it is seen.

    One departure reaches the bot twice: locally, the moment it leaves, and
    again in the sync response reporting the leave. Deciding which of the two
    is a repeat is the whole job, and it happens inside the same transaction as
    the invalidation so that a crash between deciding and invalidating is not a
    state this can be left in. Recording "a report is still owed" for an
    advance that never committed would cost the departure its only fence.

    The two observers are not symmetric, so their bookkeeping is not either:

    - A local departure is always followed by a sync report of it, so it leaves
      a debt behind for that report to consume. A rejoin does not clear the
      debt: the report is still owed, and when it comes it still describes the
      departure that was already fenced.
    - A sync report has no local counterpart to wait for -- most departures the
      bot did not initiate never produce one -- so it leaves no debt. It marks
      the room fenced instead, which is what suppresses the local observation
      of the same departure when the sync response gets there first.

    That asymmetry is in what each observation *records*, not in whether it may
    fence a room that is already fenced. Neither may. A sync report is not a
    once-only signal: a checkpoint that could not advance, an initial sync, a
    resumed older token all present the same leave again, and a bot cannot
    leave a room it has not rejoined. Fencing on the repeat deletes whatever
    the membership after it has built -- the hydrated conversation, the answer
    queued for it -- and settles the live message that provoked it as ignored
    with its replay payload erased. A rejoin is what re-arms the fence, and
    every path this bot takes back into a room reports one.
    """
    state = _claim_membership_state(transaction, principal_id, room_id)
    return _fence_departure_from_state(
        transaction,
        principal_id,
        room_id,
        source=source,
        state=state,
    )


def note_membership_restarted(transaction: Transaction, principal_id: str, room_id: str) -> None:
    """Record that the bot is in a room again, so its next departure fences.

    Only the fenced mark is cleared. An owed sync report survives a rejoin on
    purpose: the report describes the departure that ended the *previous*
    membership, and letting it fence the new one is exactly the deletion of a
    freshly hydrated conversation this whole mechanism exists to prevent.
    """
    transaction.execute(
        "UPDATE room_membership SET departure_fenced = 0 WHERE principal_id = ? AND room_id = ?",
        (principal_id, room_id),
    )


def retire_owed_departure_reports(transaction: Transaction, principal_id: str, room_id: str) -> None:
    """Forget reports that can no longer arrive, so a real departure still fences."""
    transaction.execute(
        "UPDATE room_membership SET owed_departure_reports = 0 WHERE principal_id = ? AND room_id = ?",
        (principal_id, room_id),
    )


def rooms_owing_departure_reports(transaction: Transaction, principal_id: str) -> frozenset[str]:
    """Return every room whose local departure is still owed a sync report."""
    rows = transaction.fetchall(
        "SELECT room_id FROM room_membership WHERE principal_id = ? AND owed_departure_reports > 0",
        (principal_id,),
    )
    return frozenset(row["room_id"] for row in rows)


@dataclass(frozen=True, slots=True)
class _DepartureState:
    """One room's departure bookkeeping as the transaction found it."""

    membership_epoch: int
    departure_fenced: bool
    owed_reports: int


def _claim_membership_state(
    transaction: Transaction,
    principal_id: str,
    room_id: str,
) -> _DepartureState:
    """Create or lock one membership row and decode its exact durable state."""
    row = transaction.fetchone(
        """
        INSERT INTO room_membership (
            principal_id, room_id, membership_epoch,
            departure_fenced, owed_departure_reports
        ) VALUES (?, ?, 0, 0, 0)
        ON CONFLICT (principal_id, room_id) DO UPDATE SET
            membership_epoch = room_membership.membership_epoch
        RETURNING membership_epoch, departure_fenced, owed_departure_reports
        """,
        (principal_id, room_id),
    )
    if row is None:
        raise IngestionBatchIntegrityError
    values = (
        row["membership_epoch"],
        row["departure_fenced"],
        row["owed_departure_reports"],
    )
    if tuple(map(type, values)) != (int, int, int):
        raise IngestionBatchIntegrityError
    membership_epoch, departure_fenced, owed_reports = values
    if membership_epoch < 0 or departure_fenced not in (0, 1) or owed_reports < 0:
        raise IngestionBatchIntegrityError
    return _DepartureState(
        membership_epoch=membership_epoch,
        departure_fenced=bool(departure_fenced),
        owed_reports=owed_reports,
    )


def _write_departure_state(
    transaction: Transaction,
    principal_id: str,
    room_id: str,
    *,
    membership_epoch: int,
    departure_fenced: bool,
    owed_reports: int,
) -> None:
    transaction.execute(
        """
        INSERT INTO room_membership (principal_id, room_id, membership_epoch, departure_fenced, owed_departure_reports)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT (principal_id, room_id) DO UPDATE SET
            departure_fenced = excluded.departure_fenced,
            owed_departure_reports = excluded.owed_departure_reports
        """,
        (principal_id, room_id, membership_epoch, int(departure_fenced), owed_reports),
    )


def admit(
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
    epoch = current_membership_epoch(transaction, principal_id, event.room_id)
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
        project(
            transaction,
            principal_id,
            projected,
            receipt_order=int(row["receipt_order"]),
            membership_epoch=epoch,
        )
    return AdmissionResult.ADMITTED


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


def admitted_membership_epoch(
    transaction: Transaction,
    principal_id: str,
    event_id: str,
) -> int | None:
    """Return the membership one event was admitted under, or nothing.

    Nothing means no membership: the caller named something the journal never
    admitted -- a scheduled task, a hook-authored turn -- and there is no
    previous membership for its work to belong to.

    The row survives every fence on purpose, so this answer stays available
    for as long as the turn it authorized can still be running.
    """
    row = transaction.fetchone(
        "SELECT membership_epoch FROM journal_events WHERE principal_id = ? AND event_id = ?",
        (principal_id, event_id),
    )
    return None if row is None else int(row["membership_epoch"])


def pending(
    transaction: Transaction,
    principal_id: str,
    *,
    limit: int,
    after_receipt_order: int | None = None,
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
    starts, counted in rows looked at rather than events returned.
    """
    return _pending_page(transaction, principal_id, limit=limit, after_receipt_order=after_receipt_order)


def _pending_page(
    transaction: Transaction,
    principal_id: str,
    *,
    limit: int,
    after_receipt_order: int | None,
    kind: EventKind | None = None,
) -> PendingPage:
    """Return whatever decoded from one page of at most ``limit`` raw rows.

    One query, one page. Reading further because too few rows decoded is how a
    corrupt prefix turned a bounded read into a scan of the whole pending
    table, so the bound is on the rows the query may return and nothing here
    goes back for more.
    """
    rows = _pending_rows(
        transaction,
        principal_id,
        limit=limit,
        after_receipt_order=after_receipt_order,
        kind=kind,
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
    kind: EventKind | None = None,
) -> tuple[Row, ...]:
    """Return one raw page of pending rows, in receipt order."""
    cursor_clause = "" if after_receipt_order is None else " AND receipt_order > ?"
    cursor_params: tuple[object, ...] = () if after_receipt_order is None else (after_receipt_order,)
    kind_clause = "" if kind is None else " AND kind = ?"
    kind_params: tuple[object, ...] = () if kind is None else (kind.value,)
    return transaction.fetchall(
        f"""
        SELECT {_JOURNAL_COLUMNS} FROM journal_events
        WHERE principal_id = ? AND state = 'pending'{kind_clause}{cursor_clause}
        ORDER BY receipt_order
        LIMIT ?
        """,  # noqa: S608 - a fixed column list and fixed clauses, not input
        (principal_id, *kind_params, *cursor_params, limit),
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
    derived from content for every kind alike -- ``inbound_event`` calls
    ``thread_root`` regardless of kind -- so a reaction, an approval, or an
    ``m.room.encrypted`` event this bot could not decrypt can all sit pending
    in a thread under the requester's own sender. None of them will produce a
    response, so counting one as a newer unanswered turn drops the older
    message and answers neither. Only a message or a media event can become
    the turn that legitimately supersedes another.

    Strictly newer. Two events stamped in the same millisecond are not ordered
    by their timestamps, and treating either as proof that the other is stale
    would drop a message on a coin flip.
    """
    kinds = tuple(sorted(kind.value for kind in TURN_BACKED_KINDS))
    kind_placeholders = ", ".join("?" for _ in kinds)
    rows = transaction.fetchall(
        f"""
        SELECT {_JOURNAL_COLUMNS} FROM journal_events
        WHERE principal_id = ? AND state = 'pending'
          AND room_id = ? AND thread_id = ?
          AND kind IN ({kind_placeholders})
          AND origin_server_ts > ? AND event_id <> ?
        ORDER BY origin_server_ts, receipt_order
        LIMIT ?
        """,  # noqa: S608 - a fixed column list and generated placeholders, not interpolated input
        (
            principal_id,
            room_id,
            encode_thread_id(thread_id),
            *kinds,
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


def settle(transaction: Transaction, principal_id: str, event_id: str) -> None:
    """Mark one event's semantic work terminal and release its replay payload.

    The payload is cleared rather than the row deleted: the row is the proof
    that this event already produced its one turn, and it has to outlive the
    work it authorized.

    Settled is the whole fact. Why the work ended -- answered, or deliberately
    not answered -- was recorded for a while and never once read back, and a
    durable column nothing consults is a claim nothing keeps honest.
    """
    transaction.execute(
        """
        UPDATE journal_events
        SET state = ?, source_json = '', semantic_consumer = NULL
        WHERE principal_id = ? AND event_id = ? AND state = 'pending'
        """,
        (SETTLED_STATE, principal_id, event_id),
    )


def settle_many(transaction: Transaction, principal_id: str, event_ids: tuple[str, ...]) -> None:
    """Settle several events that one terminal turn accounted for."""
    for event_id in event_ids:
        settle(transaction, principal_id, event_id)


def unsettled_event_ids(transaction: Transaction, principal_id: str) -> frozenset[str]:
    """Return every event that still owes semantic work."""
    rows = transaction.fetchall(
        "SELECT event_id FROM journal_events WHERE principal_id = ? AND state = 'pending'",
        (principal_id,),
    )
    return frozenset(row["event_id"] for row in rows)


def pending_of_kind(
    transaction: Transaction,
    principal_id: str,
    kind: EventKind,
    *,
    limit: int,
    after_receipt_order: int | None = None,
) -> PendingPage:
    """Return pending events of one kind, in receipt order.

    Pages the same way ``pending`` does, so a caller that needs every one of
    them can walk to the end and know when it got there -- and can tell that a
    row it walked past was one it could not read, which is the difference
    between an enumeration that is complete and one that only looks it.
    """
    return _pending_page(
        transaction,
        principal_id,
        limit=limit,
        after_receipt_order=after_receipt_order,
        kind=kind,
    )


def claim_semantic_consumer(
    transaction: Transaction,
    principal_id: str,
    event_id: str,
    consumer: SemanticConsumer,
) -> SemanticConsumer:
    """Record the sole consumer of one event, returning whoever holds it.

    First claim wins, durably. A replay after a crash therefore cannot let a
    second consumer act on the same reaction.
    """
    row = transaction.fetchone(
        """
        UPDATE journal_events
        SET semantic_consumer = COALESCE(semantic_consumer, ?)
        WHERE principal_id = ? AND event_id = ? AND state = 'pending'
        RETURNING semantic_consumer
        """,
        (consumer.value, principal_id, event_id),
    )
    if row is None:
        msg = f"Cannot claim a consumer for settled or missing event {event_id!r}"
        raise RuntimeError(msg)
    return SemanticConsumer(row["semantic_consumer"])


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

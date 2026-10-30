"""The transactions that record a Matrix delivery together with the journal, reply, and turn rows it couples to.

The outbox's own rows live in ``outbox``. What lives here writes them in the
same commit as the facts a delivery hands over -- the turn it settles, the
reply row its lifecycle rule chose, the projection an acknowledgement makes
visible -- so no crash can leave one without the other.
"""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

from mindroom import reply_lifecycle as rl

from . import journal, outbox, replies, reply_messages, reply_spans, turn_rows
from .identity import agent_of_principal
from .membership_state import claim_active_membership_epoch, claim_membership_epoch
from .models import (
    DeliveryAcknowledgement,
    DeliveryStage,
    FinalAnswerEnqueue,
    PermanentDeliveryFailure,
    ReplyRowFacts,
)
from .projection import discard_delivery_event, project
from .replies import AppliedTransition, ReplyRowEnqueue

if TYPE_CHECKING:
    from collections.abc import Mapping

    from .backend import Transaction
    from .projection import ProjectedEvent
    from .replies import PostCommitEffect, PreparedReplyRow, ReplyRowRequest


class ReplyRowRefusedError(Exception):
    """The outbox refused a reply row the lifecycle had already decided; roll both back."""


def enqueue_delivery(
    transaction: Transaction,
    principal_id: str,
    *,
    delivery_id: str,
    stage: DeliveryStage,
    event_type: str,
    room_id: str,
    thread_id: str | None,
    payload: Mapping[str, object],
    result: Mapping[str, object] | None,
    edits_event_id: str | None,
    permanent_failure_reason: str | None,
    edit_target_pending: bool = False,
    reply_id: str | None = None,
    span_id: str | None = None,
    reply_sequence: int | None = None,
    reply_row: ReplyRowFacts | None = None,
) -> str | None:
    """Record delivery intent unless the membership that authorized it has ended.

    The fence retires a room's unattempted deliveries because they answer a
    conversation the bot has left. This closes the other half of the same
    window: a turn that was still running when the fence committed would
    otherwise write its answer back in afterwards, and the fence has already
    been and gone. Because both are writes that claim the membership row, the
    two possible orderings are "enqueued, then retired" and "fenced, then
    refused". Neither leaves a sendable answer behind, while the retired row
    prevents a later stage from adopting the rejoined membership.

    An already-attempted row remains recoverable, and deliberately so. Its
    outcome is unknown -- the homeserver may be holding it -- and refusing the
    retry would strand it unacknowledged forever while leaving whatever it
    sent visible. Same-device recovery can reuse the frozen transaction ID;
    changed-device recovery first reconciles exact room history and then
    follows the delivery type's explicit replay-or-retain policy.

    """
    admitted_owner = journal.admitted_membership_owner(transaction, principal_id, delivery_id)
    attempted = outbox.is_attempted(
        transaction,
        principal_id,
        delivery_id=delivery_id,
        stage=stage,
    )
    if attempted:
        ownership = outbox.delivery_ownership(
            transaction,
            principal_id,
            delivery_id=delivery_id,
            stage=stage,
        )
        if ownership is None:
            msg = f"Attempted delivery {delivery_id!r}/{stage.value!r} has no outbox row"
            raise RuntimeError(msg)
        _stored_room_id, membership_epoch = ownership
    elif admitted_owner is None:
        active_epoch = claim_active_membership_epoch(
            transaction,
            principal_id,
            room_id=room_id,
        )
        if active_epoch is None:
            return None
        membership_epoch = active_epoch
    else:
        admitted_room_id, admitted_epoch = admitted_owner
        if admitted_room_id != room_id or not claim_membership_epoch(
            transaction,
            principal_id,
            room_id=room_id,
            expected_membership_epoch=admitted_epoch,
        ):
            return None
        membership_epoch = admitted_epoch
    return outbox.enqueue(
        transaction,
        principal_id,
        delivery_id=delivery_id,
        stage=stage,
        event_type=event_type,
        room_id=room_id,
        membership_epoch=membership_epoch,
        thread_id=thread_id,
        payload=payload,
        result=result,
        edits_event_id=edits_event_id,
        edit_target_pending=edit_target_pending,
        permanent_failure_reason=permanent_failure_reason,
        reply_id=reply_id,
        span_id=span_id,
        reply_sequence=reply_sequence,
        reply_row=reply_row,
    )


def enqueue_final_answer(
    transaction: Transaction,
    principal_id: str,
    *,
    delivery_id: str,
    event_type: str,
    room_id: str,
    thread_id: str | None,
    payload: Mapping[str, object],
    result: Mapping[str, object] | None,
    edits_event_id: str | None,
    permanent_failure_reason: str | None,
    turn_key: str | None,
) -> FinalAnswerEnqueue | None:
    """Record a FINAL answer and settle what its bound turn answers; a refusal settles nothing.

    Nothing durable would owe the answer after a refusal, so the turn stays the
    journal's.
    """
    transaction_id = enqueue_delivery(
        transaction,
        principal_id,
        delivery_id=delivery_id,
        stage=DeliveryStage.FINAL,
        event_type=event_type,
        room_id=room_id,
        thread_id=thread_id,
        payload=payload,
        result=result,
        edits_event_id=edits_event_id,
        permanent_failure_reason=permanent_failure_reason,
    )
    if transaction_id is None:
        return None
    bound = (
        None
        if turn_key is None
        else turn_rows.bind_final_delivery(
            transaction,
            agent_of_principal(principal_id),
            delivery_id=delivery_id,
            room_id=room_id,
            turn_key=turn_key,
        )
    )
    settled = tuple(dict.fromkeys(bound or (delivery_id,)))
    journal.settle_many(transaction, principal_id, settled)
    return FinalAnswerEnqueue(transaction_id=transaction_id, settled_event_ids=settled)


def turn_membership_is_current(
    transaction: Transaction,
    principal_id: str,
    *,
    turn_id: str,
    room_id: str,
) -> bool:
    """Return whether the membership that admitted a turn is still the room's."""
    owner = journal.admitted_membership_owner(transaction, principal_id, turn_id)
    if owner is None:
        owner = outbox.turn_ownership(transaction, principal_id, delivery_id=turn_id)
    if owner is None:
        # Nothing admitted or enqueued this turn, so no membership owns it yet.
        return True
    owner_room_id, owner_epoch = owner
    return owner_room_id == room_id and owner_epoch == journal.current_membership_epoch(
        transaction,
        principal_id,
        room_id,
    )


def record_permanent_failure(
    transaction: Transaction,
    principal_id: str,
    *,
    delivery_id: str,
    stage: DeliveryStage,
    reason: str,
) -> PermanentDeliveryFailure:
    """Stop retrying one refused payload, applying the rule of every reply row the refusal fails."""
    failed_rows, acknowledged = outbox.record_permanent_failure(
        transaction,
        principal_id,
        delivery_id=delivery_id,
        stage=stage,
        reason=reason,
    )
    effects: list[PostCommitEffect] = []
    for failed_id, failed_stage in failed_rows:
        delivery = outbox.load(transaction, principal_id, delivery_id=failed_id, stage=failed_stage)
        applied = None if delivery is None else replies.fail_row(transaction, principal_id, delivery)
        if applied is not None:
            effects.extend(applied.post_commit)
    return PermanentDeliveryFailure(acknowledged_event_id=acknowledged, reply_effects=tuple(effects))


def retire_delivery(
    transaction: Transaction,
    principal_id: str,
    *,
    delivery_id: str,
    stage: DeliveryStage,
    room_id: str,
    membership_epoch: int,
) -> str | None:
    """Retain an obsolete send as an identity tombstone, or return its ACK."""
    # Projection and acknowledgement take the membership row before
    # the delivery row. Retirement uses the same order, so an echo
    # either projects first and is removed below, or observes the
    # committed tombstone and is refused.
    claim_membership_epoch(
        transaction,
        principal_id,
        room_id=room_id,
        expected_membership_epoch=membership_epoch,
    )
    delivery = outbox.retire(
        transaction,
        principal_id,
        delivery_id=delivery_id,
        stage=stage,
        room_id=room_id,
        membership_epoch=membership_epoch,
    )
    if delivery is None:
        return None
    if delivery.retired and delivery.edits_event_id is not None:
        discard_delivery_event(
            transaction,
            principal_id,
            event_id=delivery.edits_event_id,
        )
    return delivery.acknowledged_event_id


def acknowledge_delivery(
    transaction: Transaction,
    principal_id: str,
    *,
    delivery_id: str,
    stage: DeliveryStage,
    event_id: str,
    delivered_projections: tuple[ProjectedEvent, ...],
    terminal_response_event_id: str | None,
) -> DeliveryAcknowledgement:
    """Record the Matrix event one claimed delivery produced, with what it projects and completes, if nothing else has."""
    ownership = outbox.claim_active_delivery_ownership(
        transaction,
        principal_id,
        delivery_id=delivery_id,
        stage=stage,
    )
    may_project = ownership is not None
    projection_epoch = 0 if ownership is None else ownership[1]
    bound = outbox.acknowledge(
        transaction,
        principal_id,
        delivery_id=delivery_id,
        stage=stage,
        event_id=event_id,
    )
    # A caller that lost the acknowledgement must not write the record
    # either. The row already names another event, and a terminal
    # record pointing somewhere else is the disagreement this whole
    # transaction exists to prevent.
    if bound and may_project and terminal_response_event_id is not None:
        turn_rows.complete_bound_delivery(
            transaction,
            agent_of_principal(principal_id),
            delivery_id=delivery_id,
            response_event_id=terminal_response_event_id,
        )
    if bound and may_project:
        for delivered_projection in delivered_projections:
            project(
                transaction,
                principal_id,
                delivered_projection,
                receipt_order=0,
                membership_epoch=projection_epoch,
            )
    elif bound and not may_project:
        discard_delivery_event(
            transaction,
            principal_id,
            event_id=event_id,
        )
    if bound:
        delivery = outbox.load(transaction, principal_id, delivery_id=delivery_id, stage=stage)
        reply_applied = (
            None
            if delivery is None
            else replies.acknowledge_row(
                transaction,
                principal_id,
                delivery,
                event_id=event_id,
                membership_current=may_project,
            )
        )
        return DeliveryAcknowledgement(
            settled_event_id=event_id,
            reply_effects=() if reply_applied is None else reply_applied.post_commit,
        )
    # Lost the row. Whatever is on it now is the answer this delivery
    # resolves to, and the caller has to be told that rather than its
    # own event id -- everything downstream records what `flush`
    # returns, so a loser reporting its own send is how the outbox and
    # the turn record end up naming different events.
    settled = transaction.fetchone(
        """
        SELECT acknowledged_event_id FROM matrix_delivery_outbox
        WHERE principal_id = ? AND delivery_id = ? AND stage = ?
        """,
        (principal_id, delivery_id, stage.value),
    )
    return DeliveryAcknowledgement(
        settled_event_id=None if settled is None else str(settled["acknowledged_event_id"]),
    )


def _decide_reply_row(
    transaction: Transaction,
    principal_id: str,
    request: ReplyRowRequest,
) -> ReplyRowEnqueue | tuple[rl.Reply, rl.Span, rl.Transition]:
    """Return the row a retried write resolves to, or the reply, span, and transition the rule decided."""
    if request.create is not None:
        claim = request.create.claim
        earlier = reply_spans.latest_for_delivery(transaction, principal_id, claim.delivery_id)
        if earlier is not None:
            # A retried acknowledgement finds the reply its first attempt created,
            # and resolves that attempt's row instead of writing another.
            created = reply_messages.lock(transaction, principal_id, earlier.reply_id)
            assert created is not None
            reused = _span_row(transaction, principal_id, created, earlier, rl.WriteStage.INITIAL)
            assert reused is not None, "an acknowledgement's reply is created with its INITIAL row"
            return replace(
                reused,
                applied=AppliedTransition(
                    transition=rl.Transition(outcome=rl.Outcome.DUPLICATE, reply=created, spans=(earlier,)),
                    post_commit=(),
                ),
            )
        transition = rl.interactive_acknowledgement(claim, shown=request.create.shown)
        assert transition.reply is not None
        reply, span = transition.reply, transition.spans[0]
    else:
        assert request.decide is not None
        locked = reply_messages.lock(transaction, principal_id, request.reply_id)
        loaded = reply_spans.load(transaction, principal_id, request.span_id)
        if locked is None or loaded is None:
            msg = f"Reply {request.reply_id} or span {request.span_id} does not exist"
            raise RuntimeError(msg)
        reply, span = locked, loaded
        earlier = _span_row(transaction, principal_id, reply, span, request.stage)
        if earlier is not None:
            return earlier
        transition = (
            rl.Transition(outcome=rl.Outcome.STALE, reply=reply)
            if replies.retired(transaction, principal_id, span, author_generation=request.author_generation)
            else request.decide(reply, span)
        )
    return reply, span, transition


# Every reply row is a room message.
_REPLY_ROW_EVENT_TYPE = "m.room.message"


def enqueue_reply_row(
    transaction: Transaction,
    principal_id: str,
    request: ReplyRowRequest,
    prepared: PreparedReplyRow,
) -> ReplyRowEnqueue:
    """Decide one reply write with its lifecycle rule and record the row it chose, in one transaction.

    The rule runs against the locked reply and its span as they are now, so a
    payload rendered before a Stop or deletion committed is refused rather
    than written (``Outcome.RECOMPUTE``). Sources the rule settles are settled
    here, replacing the turn handoff a plain ``FINAL`` carries.
    """
    decided = _decide_reply_row(transaction, principal_id, request)
    if isinstance(decided, ReplyRowEnqueue):
        return decided
    reply, span, transition = decided
    if not transition.applied or transition.row is None:
        return ReplyRowEnqueue(
            applied=replies.with_ended_span(replies.apply(transaction, principal_id, transition), span),
            settled_event_ids=replies.settled_event_ids(transaction, principal_id, transition),
        )
    row = transition.row
    stage = row.stage
    edits_event_id = None if stage is DeliveryStage.INITIAL else reply.event_id
    # A row waits for the reply's event only while an earlier row may still create it. With none left, as when
    # Matrix refused the only create for good, the row creates the event itself.
    edit_target_pending = (
        edits_event_id is None
        and stage is not DeliveryStage.INITIAL
        and replies.has_unresolved_rows(transaction, principal_id, reply.reply_id)
    )
    row_fields = {
        "reply_id": reply.reply_id,
        "span_id": span.span_id,
        "reply_sequence": row.sequence,
        "reply_row": ReplyRowFacts(placeholder_only=request.placeholder_only, new_text=prepared.new_text),
    }
    if stage is DeliveryStage.EDIT:
        delivery_id = replies.edit_delivery_id(span.delivery_id, row.sequence)
        if not claim_membership_epoch(
            transaction,
            principal_id,
            room_id=reply.room_id,
            expected_membership_epoch=reply.membership_epoch,
        ):
            raise ReplyRowRefusedError
        transaction_id = outbox.enqueue(
            transaction,
            principal_id,
            delivery_id=delivery_id,
            stage=stage,
            event_type=_REPLY_ROW_EVENT_TYPE,
            room_id=reply.room_id,
            membership_epoch=reply.membership_epoch,
            thread_id=reply.thread_id,
            payload=prepared.payload,
            result=prepared.result,
            edits_event_id=edits_event_id,
            edit_target_pending=edit_target_pending,
            permanent_failure_reason=prepared.permanent_failure_reason,
            **row_fields,
        )
    else:
        delivery_id = span.delivery_id
        transaction_id = enqueue_delivery(
            transaction,
            principal_id,
            delivery_id=delivery_id,
            stage=stage,
            event_type=_REPLY_ROW_EVENT_TYPE,
            room_id=reply.room_id,
            thread_id=reply.thread_id,
            payload=prepared.payload,
            result=prepared.result,
            edits_event_id=edits_event_id,
            permanent_failure_reason=prepared.permanent_failure_reason,
            edit_target_pending=edit_target_pending,
            **row_fields,
        )
    if transaction_id is None:
        raise ReplyRowRefusedError
    applied = replies.apply(transaction, principal_id, transition)
    if prepared.permanent_failure_reason is not None:
        # A payload refused before any send fails its row now, as a refusal from Matrix would.
        refused = outbox.load(transaction, principal_id, delivery_id=delivery_id, stage=stage)
        assert refused is not None
        failed = replies.fail_row(transaction, principal_id, refused)
        if failed is not None:
            applied = AppliedTransition(
                transition=transition,
                post_commit=(*applied.post_commit, *failed.post_commit),
            )
    return ReplyRowEnqueue(
        applied=applied,
        delivery_id=delivery_id,
        stage=row.stage,
        settled_event_ids=replies.settled_event_ids(transaction, principal_id, transition),
        sequence=row.sequence,
    )


def _span_row(
    transaction: Transaction,
    principal_id: str,
    reply: rl.Reply,
    span: rl.Span,
    stage: rl.WriteStage | None,
) -> ReplyRowEnqueue | None:
    """Return the span's INITIAL or FINAL row already recorded, which a retried write resolves to.

    A send that failed after its row was recorded leaves the row owed; writing
    it again would decide a second time against a span that has moved on. The
    stored row keeps its payload, as a re-enqueued attempted row does.
    """
    if stage not in {rl.WriteStage.INITIAL, rl.WriteStage.FINAL}:
        return None
    stored = outbox.load(transaction, principal_id, delivery_id=span.delivery_id, stage=stage)
    if stored is None or stored.reply_id != reply.reply_id:
        return None
    if stage is rl.WriteStage.FINAL and stored.span_id != span.span_id:
        return None
    return ReplyRowEnqueue(
        applied=AppliedTransition(
            transition=rl.Transition(outcome=rl.Outcome.DUPLICATE, reply=reply),
            post_commit=(),
        ),
        delivery_id=stored.delivery_id,
        stage=stage,
        sequence=stored.reply_sequence,
    )

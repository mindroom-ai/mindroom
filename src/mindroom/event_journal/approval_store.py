"""One principal's approval records: calls, cards, grants, scheduled calls, and paused runs."""

from __future__ import annotations

import time
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Literal

from mindroom import reply_lifecycle as rl

from . import (
    approval_calls,
    approval_cards,
    approval_continuations,
    approval_grants,
    replies,
    scheduled_approvals,
)
from .approval_continuations import ApprovalAdvance
from .store import ReplyRowRefusedError, enqueue_reply_row

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from mindroom.tool_approval_grants import ApprovalGrant, ApprovalGrantRevocation, ApprovalGrantScope

    from .approval_calls import BackgroundApprovalDecision, CallKey
    from .approval_cards import (
        ApprovalCardReservation,
        ApprovalCardView,
        ApprovalDecisionMetadata,
        RecordedApprovalDecision,
    )
    from .approval_continuations import ApprovalContinuation, ApprovalContinuationState
    from .backend import Backend, Transaction
    from .replies import AppliedTransition, PostCommitEffect, PreparedReplyRow, ReplyRowEnqueue, ReplyRowRequest
    from .scheduled_approvals import (
        ScheduledApprovalArmState,
        ScheduledCall,
        ScheduledCallBinding,
        ScheduledCallClaim,
        ScheduledCallOutcome,
        ScheduledCallRefusal,
    )

_DEFAULT_ROOM_CARD_LIMIT = 256


@dataclass(frozen=True, slots=True)
class ApprovalStore:
    """One principal's approval records."""

    _backend: Backend
    _principal_id: str

    async def reserve_reply_cards(
        self,
        *,
        continuation_principal_id: str,
        continuation_id: str,
        expected_generation: int,
        cards: tuple[ApprovalCardReservation, ...],
    ) -> bool:
        """Atomically reserve every undecided call's card of one paused generation and release its publication lease."""
        return await self._backend.write(
            lambda transaction: approval_cards.reserve_reply_cards(
                transaction,
                self._principal_id,
                continuation_principal_id=continuation_principal_id,
                continuation_id=continuation_id,
                expected_generation=expected_generation,
                cards=cards,
            ),
        )

    async def reserve_detached_card(
        self,
        *,
        call: CallKey,
        owner_kind: Literal["job", "script"],
        tool_name: str,
        toolkit_name: str,
        invoking_agent: str,
        room_id: str,
        thread_id: str | None,
        expires_at_ns: int,
        card: ApprovalCardReservation,
        grant_scope: ApprovalGrantScope | None = None,
    ) -> bool:
        """Atomically reserve one detached call and its card, or apply an active grant in the card's place."""
        return await self._backend.write(
            lambda transaction: approval_cards.reserve_detached_card(
                transaction,
                self._principal_id,
                call=call,
                owner_kind=owner_kind,
                tool_name=tool_name,
                toolkit_name=toolkit_name,
                invoking_agent=invoking_agent,
                room_id=room_id,
                thread_id=thread_id,
                expires_at_ns=expires_at_ns,
                card=card,
                grant_scope=grant_scope,
            ),
        )

    async def detached_decision(self, call: CallKey) -> BackgroundApprovalDecision | None:
        """Return one detached call's decision, if it has one."""
        return await self._backend.read(
            lambda transaction: approval_calls.detached_decision(transaction, self._principal_id, call),
        )

    async def resolve_approval_call(
        self,
        call: CallKey,
        *,
        requested_status: Literal["approved", "denied", "expired"],
        reason: str | None,
        metadata: ApprovalDecisionMetadata | None = None,
    ) -> RecordedApprovalDecision:
        """Decide one call, whatever owns it, and queue its card's terminal edit."""
        return await self._backend.write(
            lambda transaction: approval_calls.resolve_call(
                transaction,
                call,
                requested=requested_status,
                reason=reason,
                metadata=metadata,
            ),
        )

    async def deny_pending_detached_calls(self, *, owner_id: str, reason: str) -> int:
        """Deny every pending call of one detached run atomically, returning how many this decided."""
        return await self._backend.write(
            lambda transaction: approval_calls.deny_pending(
                transaction,
                self._principal_id,
                owner_id=owner_id,
                reason=reason,
            ),
        )

    async def prune_detached_calls(self, *, owner_id: str) -> bool:
        """Delete one detached run's decided calls after their cards have retired."""
        return await self._backend.write(
            lambda transaction: approval_calls.prune(transaction, self._principal_id, owner_id=owner_id),
        )

    async def reserve_scheduled_call_approval(
        self,
        *,
        binding: ScheduledCallBinding,
        card: ApprovalCardReservation,
        any_arguments_offered: bool,
    ) -> bool:
        """Atomically reserve one scheduling-time card and its exact-call binding."""
        return await self._backend.write(
            lambda transaction: scheduled_approvals.reserve(
                transaction,
                self._principal_id,
                binding=binding,
                card=card,
                any_arguments_offered=any_arguments_offered,
            ),
        )

    async def arm_scheduled_call_approval(
        self,
        *,
        task_id: str,
        workflow_digest: str,
        any_arguments_allowed: bool,
    ) -> ScheduledApprovalArmState:
        """Arm one approved scheduled call for its unchanged task firing on time."""
        return await self._backend.write(
            lambda transaction: scheduled_approvals.arm(
                transaction,
                self._principal_id,
                task_id=task_id,
                workflow_digest=workflow_digest,
                any_arguments_allowed=any_arguments_allowed,
                now_ns=time.time_ns(),
            ),
        )

    async def withdraw_scheduled_call_approval(self, *, task_id: str, reason: str) -> RecordedApprovalDecision:
        """Withdraw one cancelled task's approval and deny its card if still pending."""
        return await self._backend.write(
            lambda transaction: scheduled_approvals.withdraw(
                transaction,
                self._principal_id,
                task_id=task_id,
                reason=reason,
            ),
        )

    async def scheduled_call(self, *, task_id: str) -> ScheduledCall | None:
        """Read the call one scheduled task stored."""
        return await self._backend.read(
            lambda transaction: scheduled_approvals.stored_call(transaction, self._principal_id, task_id=task_id),
        )

    async def claim_scheduled_call(
        self,
        *,
        call: ScheduledCall,
        arguments_json: str,
        receipt: ApprovalCardReservation,
    ) -> ScheduledCallClaim | ScheduledCallRefusal:
        """Spend one armed scheduled approval and reserve its receipt in one commit."""
        return await self._backend.write(
            lambda transaction: scheduled_approvals.claim(
                transaction,
                self._principal_id,
                call=call,
                arguments_json=arguments_json,
                receipt=receipt,
                now_ns=time.time_ns(),
            ),
        )

    async def record_scheduled_call_outcome(self, *, task_id: str, outcome: ScheduledCallOutcome) -> None:
        """Record how one claimed scheduled call ended."""
        await self._backend.write(
            lambda transaction: scheduled_approvals.record_outcome(
                transaction,
                self._principal_id,
                task_id=task_id,
                outcome=outcome,
            ),
        )

    async def create_approval_grant(
        self,
        *,
        room_id: str,
        card_event_id: str,
        sender_id: str,
        seconds: int,
        metadata: ApprovalDecisionMetadata,
        reason: str | None = None,
        current_binding: str | None = None,
    ) -> tuple[RecordedApprovalDecision, ...]:
        """Commit the originating decision, fixed grant, and matching pending decisions."""
        return await self._backend.write(
            lambda transaction: approval_grants.create(
                transaction,
                self._principal_id,
                room_id=room_id,
                card_event_id=card_event_id,
                sender_id=sender_id,
                seconds=seconds,
                metadata=metadata,
                reason=reason,
                current_binding=current_binding,
            ),
        )

    async def approval_grant_for_card(self, *, room_id: str, card_event_id: str) -> ApprovalGrant | None:
        """Load a grant independently of its retired card."""
        return await self._backend.read(
            lambda transaction: approval_grants.for_card(
                transaction,
                self._principal_id,
                room_id=room_id,
                card_event_id=card_event_id,
            ),
        )

    async def maintain_automatic_approvals(self, *, grant_id: str | None = None) -> tuple[str, ...]:
        """Retire settled receipts, enqueue grant revocations after their approval edits, and prune old scheduled calls."""

        def maintain(transaction: Transaction) -> tuple[str, ...]:
            if grant_id is None:
                approval_grants.lock(transaction, self._principal_id)
                approval_cards.retire_receipts(transaction, self._principal_id)
            deliveries = approval_grants.maintain(transaction, self._principal_id, grant_id=grant_id)
            if grant_id is None:
                scheduled_approvals.prune(transaction, self._principal_id, time.time_ns())
            return deliveries

        return await self._backend.write(maintain)

    async def revoke_approval_grant(
        self,
        *,
        room_id: str,
        card_event_id: str,
        sender_id: str,
        grant_id: str,
    ) -> ApprovalGrantRevocation | None:
        """Record durable revocation debt for ordered acknowledgement delivery."""
        return await self._backend.write(
            lambda transaction: approval_grants.revoke(
                transaction,
                self._principal_id,
                room_id=room_id,
                card_event_id=card_event_id,
                sender_id=sender_id,
                grant_id=grant_id,
            ),
        )

    async def retire_approval_card(self, *, delivery_id: str) -> bool:
        """Retire a request card whose terminal edit Matrix acknowledged."""
        return await self._backend.write(
            lambda transaction: approval_cards.retire(transaction, self._principal_id, delivery_id=delivery_id),
        )

    async def remember_terminal_approval_alias(self, *, room_id: str, card_event_id: str, delivery_id: str) -> None:
        """Remember a transport-verified copy of an automatic receipt as another event of that receipt."""
        await self._backend.write(
            lambda transaction: approval_cards.remember_alias(
                transaction,
                self._principal_id,
                room_id=room_id,
                card_event_id=card_event_id,
                delivery_id=delivery_id,
            ),
        )

    async def approval_card(
        self,
        *,
        room_id: str,
        card_event_id: str,
    ) -> ApprovalCardView | Literal["terminal"] | None:
        """Return the card one Matrix event shows, ``"terminal"`` for a terminal event whose card is unknown."""
        return await self._backend.read(
            lambda transaction: approval_cards.card_for_event(
                transaction,
                self._principal_id,
                room_id=room_id,
                card_event_id=card_event_id,
            ),
        )

    async def pending_approval_room_ids(self) -> tuple[str, ...]:
        """Return every current room where this principal still owns a live request card."""
        return await self._backend.read(
            lambda transaction: approval_cards.pending_room_ids(transaction, self._principal_id),
        )

    async def pending_approval_cards(
        self,
        *,
        room_id: str,
        limit: int = _DEFAULT_ROOM_CARD_LIMIT,
        after: tuple[int, str] | None = None,
    ) -> tuple[ApprovalCardView, ...]:
        """Return one room's live request cards, oldest first."""
        return await self._backend.read(
            lambda transaction: approval_cards.pending_cards(
                transaction,
                self._principal_id,
                room_id=room_id,
                limit=limit,
                after=after,
            ),
        )

    async def pause_for_approval(
        self,
        hold: ApprovalContinuation | ApprovalAdvance,
        request: ReplyRowRequest,
        prepared: PreparedReplyRow,
    ) -> ReplyRowEnqueue | None:
        """Create or advance a paused run's owner and pause its reply with the pause row, in one transaction.

        ``None`` means the continuation could not take its sources or advance,
        or the outbox refused the row; a pause the reply's rule refuses (a Stop
        committed meanwhile) comes back unapplied. Either way nothing is written.
        """
        try:
            return await self._backend.write(
                lambda transaction: _pause_for_approval(transaction, self._principal_id, hold, request, prepared),
            )
        except ReplyRowRefusedError:
            return None
        except _PauseRefusedError as refused:
            return refused.enqueue

    async def pending_approvals(self) -> tuple[ApprovalContinuation, ...]:
        """Return every approval continuation this principal owns."""
        return await self._backend.read(
            lambda transaction: approval_continuations.for_principal(transaction, self._principal_id),
        )

    async def approval_continuation_for_source(
        self,
        event_id: str,
    ) -> ApprovalContinuation | None:
        """Return the paused run that owns one original source event."""
        return await self._backend.read(
            lambda transaction: approval_continuations.for_source(
                transaction,
                self._principal_id,
                event_id=event_id,
            ),
        )

    async def approval_continuation(self, approval_id: str) -> ApprovalContinuation | None:
        """Return one principal-owned paused run by its stable identity."""
        return await self._backend.read(
            lambda transaction: approval_continuations.get(
                transaction,
                self._principal_id,
                approval_id=approval_id,
            ),
        )

    async def claim_approval_resume(
        self,
        approval_id: str,
        *,
        claim: rl.ClaimRequest,
    ) -> tuple[ApprovalContinuation | None, AppliedTransition | None]:
        """Claim one ready paused run with its reply's resume span.

        Returns neither when the continuation is not ready. A reply with
        unresolved durable writes refuses the resume, and the continuation
        stays ready.
        """
        return await self._backend.write(
            lambda transaction: _claim_approval_resume(
                transaction,
                self._principal_id,
                approval_id=approval_id,
                claim=claim,
            ),
        )

    async def claim_approval_in_place(
        self,
        approval_id: str,
        *,
        runtime_generation: str,
        reply_id: str,
        span_id: str,
    ) -> tuple[ApprovalContinuation | None, AppliedTransition | None]:
        """Claim a ready continuation for the response waiting on it, and resume its reply in place.

        Only a span this bot instance still owns resumes; when the reply's
        rule refuses, the continuation stays ready and nothing executes.
        """

        def claim(transaction: Transaction) -> tuple[ApprovalContinuation | None, AppliedTransition | None]:
            current = approval_continuations.get(transaction, self._principal_id, approval_id=approval_id)
            if current is None or current.state != "ready":
                return None, None
            resumed = replies.decide_on_span(
                transaction,
                self._principal_id,
                reply_id=reply_id,
                span_id=span_id,
                author_generation=runtime_generation,
                decide=lambda reply, span: rl.resumed_in_place(
                    reply,
                    span,
                    approval_id=approval_id,
                    now_ns=time.time_ns(),
                ),
            )
            if not resumed.transition.applied:
                raise _InPlaceResumeRefusedError(resumed)
            claimed = approval_continuations.claim(
                transaction,
                self._principal_id,
                approval_id=approval_id,
                span_id=span_id,
            )
            assert claimed, "the reply resumed in place for a continuation no other span claimed"
            return approval_continuations.get(transaction, self._principal_id, approval_id=approval_id), resumed

        try:
            return await self._backend.write(claim)
        except _InPlaceResumeRefusedError as refused:
            return None, refused.applied

    async def activate_approval_continuation(
        self,
        approval_id: str,
        *,
        expected_generation: int,
    ) -> ApprovalContinuation | None:
        """Expose one generation only after every approval card is durable."""
        return await self._backend.write(
            lambda transaction: approval_continuations.activate(
                transaction,
                self._principal_id,
                approval_id=approval_id,
                expected_generation=expected_generation,
            ),
        )

    async def request_approval_failure(
        self,
        approval_id: str,
        reason: str,
        *,
        expected_state: ApprovalContinuationState,
        expected_generation: int = 0,
        expected_runtime_generation: str | None = None,
    ) -> ApprovalContinuation | None:
        """Fence one observed continuation state against any later execution."""
        return await self._backend.write(
            lambda transaction: approval_continuations.request_failure(
                transaction,
                self._principal_id,
                approval_id=approval_id,
                reason=reason,
                expected_state=expected_state,
                expected_generation=expected_generation,
                expected_runtime_generation=expected_runtime_generation,
            ),
        )

    async def release_approval_continuation(
        self,
        approval_id: str,
        *,
        expected_generation: int,
    ) -> tuple[PostCommitEffect, ...] | None:
        """Release an interrupted continuation, whose reply rule hands its sources back to replay or ends it stopped.

        Returns the work its commit left for afterwards, or ``None`` when the run may not be released.
        """
        return await self._backend.write(
            lambda transaction: _end_approval(
                transaction,
                self._principal_id,
                approval_id,
                may_end=lambda: approval_continuations.may_release(
                    transaction,
                    self._principal_id,
                    approval_id=approval_id,
                    expected_generation=expected_generation,
                ),
                end=lambda released: replies.approval_released(transaction, self._principal_id, released),
            ),
        )

    async def finish_approval_continuation(self, approval_id: str) -> tuple[PostCommitEffect, ...] | None:
        """Settle one paused run after its FINAL delivery reaches a terminal outcome, and end its reply.

        Returns the work its commit left for afterwards, or ``None`` when the run is not ready to finish.
        """
        return await self._backend.write(
            lambda transaction: _end_approval(
                transaction,
                self._principal_id,
                approval_id,
                may_end=lambda: approval_continuations.may_finish(
                    transaction,
                    self._principal_id,
                    approval_id=approval_id,
                ),
                end=lambda finishing: replies.approval_finished(
                    transaction,
                    self._principal_id,
                    finishing,
                    owner_available=True,
                ),
            ),
        )

    async def enqueue_unavailable_approval_notice(
        self,
        *,
        approval_id: str,
        room_id: str,
        thread_id: str | None,
        payload: Mapping[str, object],
    ) -> str | None:
        """Enqueue this membership's physical attempt for one logical notice."""
        return await self._backend.write(
            lambda transaction: approval_continuations.enqueue_unavailable_notice(
                transaction,
                self._principal_id,
                approval_id=approval_id,
                room_id=room_id,
                thread_id=thread_id,
                payload=payload,
            ),
        )

    async def discard_unavailable_approval_continuation(
        self,
        approval_id: str,
        *,
        notice_principal_id: str,
    ) -> bool:
        """Release sources after permanent owner loss and visible card cleanup, ending the reply the approval paused."""
        # The owner that could settle the reply is gone; the notice is what the room sees.
        # No live ledger of that owner learns it; its next start loads it.
        ended = await self._backend.write(
            lambda transaction: _end_approval(
                transaction,
                self._principal_id,
                approval_id,
                may_end=lambda: approval_continuations.may_discard_unavailable(
                    transaction,
                    self._principal_id,
                    approval_id=approval_id,
                    notice_principal_id=notice_principal_id,
                ),
                end=lambda discarded: replies.approval_finished(
                    transaction,
                    self._principal_id,
                    discarded,
                    owner_available=False,
                ),
            ),
        )
        return ended is not None

    @property
    def principal_id(self) -> str:
        """Return this view's durable principal identity."""
        return self._principal_id


def _claim_approval_resume(
    transaction: Transaction,
    principal_id: str,
    *,
    approval_id: str,
    claim: rl.ClaimRequest,
) -> tuple[ApprovalContinuation | None, AppliedTransition | None]:
    """Claim a ready continuation with its paused reply's resume span, in one transaction."""
    current = approval_continuations.get(transaction, principal_id, approval_id=approval_id)
    if current is None or current.state != "ready":
        return None, None
    # Every continuation pauses a reply: the pause that created it recorded the reply.
    applied = replies.claim(
        transaction,
        principal_id,
        replace(claim, approval_id=current.approval_id),
        existing_event_id=current.response_event_id,
    )
    if applied.transition.claimed is None:
        # The reply's earlier writes are unresolved; their resolution wakes the sources.
        return None, applied
    claimed = approval_continuations.claim(
        transaction,
        principal_id,
        approval_id=approval_id,
        span_id=applied.transition.claimed.span_id,
    )
    assert claimed, "a ready continuation is claimed in the transaction that read it"
    return approval_continuations.get(transaction, principal_id, approval_id=approval_id), applied


def _end_approval(
    transaction: Transaction,
    principal_id: str,
    approval_id: str,
    *,
    may_end: Callable[[], ApprovalContinuation | None],
    end: Callable[[ApprovalContinuation], AppliedTransition | None],
) -> tuple[PostCommitEffect, ...] | None:
    """End one continuation that may end, applying the end to the reply it paused, in one transaction.

    ``None`` means the continuation may not end now. The reply is locked before
    the continuation, and it applies the end while the run still holds it; the
    run goes after.
    """
    continuation = approval_continuations.get(transaction, principal_id, approval_id=approval_id)
    paused = None if continuation is None else replies.lock_paused_reply(transaction, principal_id, continuation)
    ending = may_end()
    if ending is None:
        return None
    applied = end(ending)
    approval_continuations.delete(transaction, principal_id, approval_id=approval_id)
    effects = () if applied is None else applied.post_commit
    # A claim that waited for the approval to end, such as an edit's regeneration, retries now.
    return effects if paused is None else (*effects, replies.ApprovalEnded(approval_id, paused.reply_id))


class _InPlaceResumeRefusedError(Exception):
    """The reply's rule refused an in-place resume; the continuation's claim rolls back."""

    def __init__(self, applied: AppliedTransition) -> None:
        super().__init__("The reply's rule refused the in-place resume")
        self.applied = applied


class _PauseRefusedError(Exception):
    """The reply's rule refused a pause; the continuation created with it rolls back."""

    def __init__(self, enqueue: ReplyRowEnqueue) -> None:
        super().__init__("The reply's rule refused the pause")
        self.enqueue = enqueue


def _pause_for_approval(
    transaction: Transaction,
    principal_id: str,
    hold: ApprovalContinuation | ApprovalAdvance,
    request: ReplyRowRequest,
    prepared: PreparedReplyRow,
) -> ReplyRowEnqueue:
    """Create or advance the continuation, then pause its reply, so neither exists without the other."""
    held = (
        hold.apply(transaction, principal_id)
        if isinstance(hold, ApprovalAdvance)
        else approval_continuations.create(transaction, principal_id, replace(hold, span_id=request.span_id))
    )
    if held is None:
        raise ReplyRowRefusedError
    enqueued = enqueue_reply_row(transaction, principal_id, request, prepared)
    if not enqueued.transition.applied:
        raise _PauseRefusedError(enqueued)
    return enqueued

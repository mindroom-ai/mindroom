"""What every owner of a paused reply's ending shares: its final-answer read and its failure preparation."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.event_journal import ApprovalFailure, DeliveryStage
from mindroom.reply_lifecycle import ApprovalFailureKind

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from mindroom.cancellation import CancelSource
    from mindroom.event_journal import ApprovalContinuation, MatrixDelivery, PrincipalStore
    from mindroom.reply_lifecycle import SpanOutcome

_CANCELLED_KINDS = {
    "user_stop": ApprovalFailureKind.USER_STOP,
    "sync_restart": ApprovalFailureKind.RESTART,
    "interrupted": ApprovalFailureKind.INTERRUPTED,
}


def cancelled_failure(cancel_source: CancelSource) -> ApprovalFailure:
    """Return the approval failure of a response cancelled from ``cancel_source``."""
    return ApprovalFailure(_CANCELLED_KINDS[cancel_source])


async def read_final(
    principal: PrincipalStore,
    continuation: ApprovalContinuation,
) -> tuple[MatrixDelivery | None, SpanOutcome | None]:
    """Read the FINAL delivery the continuation names and the outcome of the span that wrote it."""
    final = await principal.load_matrix_delivery(delivery_id=continuation.final_delivery_id, stage=DeliveryStage.FINAL)
    span = None if final is None or final.span_id is None else await principal.replies.span(final.span_id)
    return final, None if span is None else span.outcome


async def prepare_approval_failure(
    current: ApprovalContinuation,
    failure: ApprovalFailure,
    *,
    request_failure: Callable[[ApprovalFailure], Awaitable[ApprovalContinuation | None]],
    expire_cards: Callable[[str, str], Awaitable[bool]] | None,
) -> ApprovalContinuation | None:
    """Fence the observed state and settle cards before publishing a failure.

    The cards are expired with the reason of the failure the continuation holds.
    Callers reload the continuation and enforce their own frozen FINAL policy.
    A missing result means changed ownership or undelivered card debt; retry it.
    """
    if current.state != "failing":
        failing = await request_failure(failure)
        if failing is None:
            return None
        current = failing
    assert current.failure is not None, "a failing continuation holds its failure"
    if expire_cards is None or not await expire_cards(current.approval_id, current.failure.reason):
        return None
    return current

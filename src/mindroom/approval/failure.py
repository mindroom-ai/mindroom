"""Shared failure preparation before each owner's distinct terminal publication."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.event_journal import ApprovalFailure
from mindroom.reply_lifecycle import ApprovalFailureKind

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from mindroom.cancellation import CancelSource
    from mindroom.event_journal import ApprovalContinuation

_CANCELLED_KINDS = {
    "user_stop": ApprovalFailureKind.USER_STOP,
    "sync_restart": ApprovalFailureKind.RESTART,
    "interrupted": ApprovalFailureKind.INTERRUPTED,
}


def cancelled_failure(cancel_source: CancelSource) -> ApprovalFailure:
    """Return the approval failure of a response cancelled from ``cancel_source``."""
    return ApprovalFailure(_CANCELLED_KINDS[cancel_source])


def response_failure(*, cancel_source: CancelSource | None, reason: str | None, default: str) -> ApprovalFailure:
    """Return the approval failure of a response that was cancelled from ``cancel_source``, or failed with ``reason``."""
    if cancel_source is not None:
        return cancelled_failure(cancel_source)
    return ApprovalFailure(ApprovalFailureKind.ERROR, reason or default)


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

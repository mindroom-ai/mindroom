"""Shared failure preparation before each owner's distinct terminal publication."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from mindroom.event_journal import ApprovalContinuation


async def prepare_approval_failure(
    current: ApprovalContinuation,
    reason: str,
    *,
    request_failure: Callable[[str], Awaitable[ApprovalContinuation | None]],
    expire_cards: Callable[[str], Awaitable[bool]] | None,
) -> ApprovalContinuation | None:
    """Fence the observed state and settle cards before publishing a failure.

    Callers reload the continuation and enforce their own frozen FINAL policy.
    A missing result means changed ownership or undelivered card debt; retry it.
    """
    if current.state != "failing":
        failing = await request_failure(reason)
        if failing is None:
            return None
        current = failing
    if expire_cards is None or not await expire_cards(current.approval_id):
        return None
    return current

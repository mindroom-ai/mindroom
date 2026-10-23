"""Which runs read the bound approval receipt: the binding an approval continuation sets around itself.

It imports nothing of the model loop, so the work the loop starts, such as a background job, can leave it.
``mindroom.approval.receipt`` renders the receipt and projects it into a reader's first request.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Generator


@dataclass(frozen=True)
class _ApprovalReceiptContext:
    """One approval continuation's receipt and, by run id, the runs that read it and those that already did."""

    receipt_text: str
    readers: set[str]
    sent: set[str] = field(default_factory=set)


_context: ContextVar[_ApprovalReceiptContext | None] = ContextVar(
    "approval_receipt_context",
    default=None,
)


@contextmanager
def approval_receipt_context(receipt_text: str, *, run_id: str) -> Generator[None, None, None]:
    """Bind one approval's trusted receipt around its continuation, for the paused run ``run_id`` it resumes.

    That run reads the receipt, and so does every run that ``add_approval_receipt_reader`` names: one whose own
    paused calls the approval decided, one that continues with the result of a delegation call whose card or
    child's calls the approval decided, or a recovered CLI call's follow-up. Each reads it once, in its first request
    as a reader. No other run reads it, such as a run the continuation starts, a run of a background job it starts,
    or a member it continues with the result of a delegation the approval did not decide.
    """
    token = _context.set(_ApprovalReceiptContext(receipt_text=receipt_text, readers={run_id}))
    try:
        yield
    finally:
        _context.reset(token)


def add_approval_receipt_reader(run_id: str) -> None:
    """Have run ``run_id`` read the bound approval receipt; outside an approval continuation nothing is bound."""
    receipt = _context.get()
    if receipt is not None:
        receipt.readers.add(run_id)


@contextmanager
def without_approval_receipt() -> Generator[None, None, None]:
    """Run work that outlives the bound approval continuation, such as a background job it starts, outside it.

    No run in that work reads the receipt, and naming one a reader binds nothing.
    """
    token = _context.set(None)
    try:
        yield
    finally:
        _context.reset(token)


def take_unsent_receipt(run_id: str) -> str | None:
    """Return the bound receipt's text when run ``run_id`` reads it and has not yet, marking it read; else None."""
    receipt = _context.get()
    if receipt is None or run_id not in receipt.readers or run_id in receipt.sent:
        return None
    receipt.sent.add(run_id)
    return receipt.receipt_text


__all__ = [
    "add_approval_receipt_reader",
    "approval_receipt_context",
    "take_unsent_receipt",
    "without_approval_receipt",
]

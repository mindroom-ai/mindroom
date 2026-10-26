"""Which runs read the bound approval receipt, under which ids it names their calls: the binding a continuation sets.

It imports nothing of the model loop, so the work the loop starts, such as a background job, can leave it.
``mindroom.approval.receipt`` renders the receipt and projects it into a reader's first request.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable, Generator

    from mindroom.event_journal import ApprovalCall


@dataclass(frozen=True)
class _ApprovalReceiptContext:
    """One approval continuation's receipt: its decided calls and, by run id, the runs that read it and those that did.

    ``calls`` name each call by the id the runs of this scope give it; each reader keeps the calls under its own ids.
    """

    calls: tuple[ApprovalCall, ...]
    readers: dict[str, tuple[ApprovalCall, ...]]
    sent: set[str] = field(default_factory=set)


_context: ContextVar[_ApprovalReceiptContext | None] = ContextVar(
    "approval_receipt_context",
    default=None,
)


def _renamed(calls: tuple[ApprovalCall, ...], call_id: Callable[[str], str]) -> tuple[ApprovalCall, ...]:
    return tuple(replace(call, tool_call_id=call_id(call.tool_call_id)) for call in calls)


@contextmanager
def approval_receipt_context(calls: tuple[ApprovalCall, ...], *, run_id: str) -> Generator[None, None, None]:
    """Bind an approval continuation's trusted receipt around it, for the paused run ``run_id`` it resumes.

    The receipt records the decided ``calls`` of every approval in the response so far, under their approval cards'
    ids, in pause order; a pending call fails the binding before anything runs. That run reads it, and so does every
    run that ``add_approval_receipt_reader`` names: one whose own paused calls the latest approval decided, one that
    continues with the result of a delegation call whose card or child's calls it decided, or a recovered CLI call's
    follow-up. Each reads it once, in its first request as a reader. No other run reads it, such as a run the
    continuation starts, a run of a background job it starts, or a member it continues with the result of a
    delegation the latest approval did not decide.
    """
    pending = next((call for call in calls if call.decision is None), None)
    if pending is not None:
        msg = f"Cannot build trusted receipt for pending approval call {pending.tool_call_id!r}"
        raise ValueError(msg)
    token = _context.set(_ApprovalReceiptContext(calls=calls, readers={run_id: calls}))
    try:
        yield
    finally:
        _context.reset(token)


def add_approval_receipt_reader(run_id: str, call_id: Callable[[str], str] | None = None) -> None:
    """Have run ``run_id`` read the bound approval receipt; outside an approval continuation nothing is bound.

    The run reads each call under the id this scope gives it, or under the id ``call_id`` gives that one: a team
    member's run shares its team's scope, so ``call_id`` names its own delegation call by its id, not its card's.
    """
    receipt = _context.get()
    if receipt is not None:
        receipt.readers.setdefault(run_id, receipt.calls if call_id is None else _renamed(receipt.calls, call_id))


@contextmanager
def approval_receipt_call_ids(call_id: Callable[[str], str]) -> Generator[None, None, None]:
    """Have the runs of this scope, such as a delegated child's, read each bound call under the id ``call_id`` gives it.

    ``call_id`` maps the id the enclosing scope gives a call, so nested scopes compose. Who reads, and who already
    did, stays the continuation's.
    """
    receipt = _context.get()
    token = _context.set(None if receipt is None else replace(receipt, calls=_renamed(receipt.calls, call_id)))
    try:
        yield
    finally:
        _context.reset(token)


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


def take_unsent_receipt(run_id: str, call_id: Callable[[str], str]) -> tuple[ApprovalCall, ...] | None:
    """Return the bound receipt's calls when run ``run_id`` reads it and has not yet, marking it read.

    Each call goes under the id ``call_id`` gives the run's own id for it, such as the id the run's request sends.
    """
    receipt = _context.get()
    if receipt is None or run_id not in receipt.readers or run_id in receipt.sent:
        return None
    receipt.sent.add(run_id)
    return _renamed(receipt.readers[run_id], call_id)


__all__ = [
    "add_approval_receipt_reader",
    "approval_receipt_call_ids",
    "approval_receipt_context",
    "take_unsent_receipt",
    "without_approval_receipt",
]

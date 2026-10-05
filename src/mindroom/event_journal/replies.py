"""Transactional application of reply lifecycle transitions.

The pure rules in ``mindroom.reply_lifecycle`` decide; this module reads the
facts a rule needs inside the transaction that also performs main's coupled
durable step, writes what the rule decided, and runs the in-transaction
effects. Post-commit effects are returned to the caller.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from mindroom.reply_lifecycle import (
    CancelSpan,
    Effect,
    Reply,
    SettleSources,
    Span,
    TransferStop,
    Transition,
    WakeApproval,
)

from . import journal, reply_messages, reply_spans

if TYPE_CHECKING:
    from .backend import Backend, Transaction

# Effects the caller runs after the transaction commits.
type PostCommitEffect = CancelSpan | WakeApproval


@dataclass(frozen=True, slots=True)
class AppliedTransition:
    """A committed transition and the work left for after the commit."""

    transition: Transition
    post_commit: tuple[PostCommitEffect, ...]


def span_for(transaction: Transaction, principal_id: str, transition: Transition, span_id: str) -> Span:
    """Return a span as the transition left it, falling back to the stored row."""
    for span in transition.spans:
        if span.span_id == span_id:
            return span
    stored = reply_spans.load(transaction, principal_id, span_id)
    if stored is None:
        msg = f"Reply span {span_id} does not exist"
        raise RuntimeError(msg)
    return stored


def apply(transaction: Transaction, principal_id: str, transition: Transition) -> AppliedTransition:
    """Write one transition and run its in-transaction effects; return the post-commit ones."""
    reply_messages.persist(transaction, principal_id, transition)
    post_commit: list[PostCommitEffect] = []
    for effect in transition.effects:
        _run(transaction, principal_id, transition, effect, post_commit)
    return AppliedTransition(transition=transition, post_commit=tuple(post_commit))


def _run(
    transaction: Transaction,
    principal_id: str,
    transition: Transition,
    effect: Effect,
    post_commit: list[PostCommitEffect],
) -> None:
    match effect:
        case SettleSources(span_id=span_id):
            span = span_for(transaction, principal_id, transition, span_id)
            journal.settle_many(transaction, principal_id, span.sources.pending)
        case CancelSpan() | WakeApproval():
            post_commit.append(effect)
        case TransferStop():
            # The turn-record copy is written by the caller that owns the ledger write.
            pass
        case _:
            msg = f"Reply effect {effect!r} has no transactional owner yet"
            raise NotImplementedError(msg)


@dataclass(frozen=True, slots=True)
class ReplyStore:
    """Read access to one principal's reply records."""

    _backend: Backend
    _principal_id: str

    async def load(self, reply_id: str) -> Reply | None:
        """Return one reply."""
        return await self._backend.read(
            lambda transaction: reply_messages.load(transaction, self._principal_id, reply_id),
        )

    async def for_event(self, event_id: str) -> Reply | None:
        """Return the reply bound to one Matrix event."""
        return await self._backend.read(
            lambda transaction: reply_messages.for_event(transaction, self._principal_id, event_id),
        )

    async def for_sources(self, event_ids: tuple[str, ...]) -> Reply | None:
        """Return the newest reply answering any of these sources."""
        return await self._backend.read(
            lambda transaction: reply_messages.for_sources(transaction, self._principal_id, event_ids),
        )

    async def span(self, span_id: str) -> Span | None:
        """Return one span."""
        return await self._backend.read(
            lambda transaction: reply_spans.load(transaction, self._principal_id, span_id),
        )

    async def spans(self, reply_id: str) -> tuple[Span, ...]:
        """Return every span of one reply, oldest first."""
        return await self._backend.read(
            lambda transaction: reply_spans.for_reply(transaction, self._principal_id, reply_id),
        )

    async def write_generation(self, generation: str, *, now_ns: int) -> None:
        """Make one bot instance the owner of this principal's replies."""
        await self._backend.write(
            lambda transaction: reply_messages.write_generation(
                transaction,
                self._principal_id,
                generation=generation,
                now_ns=now_ns,
            ),
        )

    async def active_generation(self) -> str | None:
        """Return the bot instance that owns this principal's replies now."""
        return await self._backend.read(
            lambda transaction: reply_messages.active_generation(transaction, self._principal_id),
        )

    async def apply(self, transition: Transition) -> AppliedTransition:
        """Write one transition decided outside a transaction coupled to main's steps."""
        return await self._backend.write(lambda transaction: apply(transaction, self._principal_id, transition))

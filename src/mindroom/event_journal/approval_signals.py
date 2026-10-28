"""What committed approval writes tell this process: which replies are paused, and which calls were just decided.

Store topology: a runtime writes its journal through exactly one ``EventJournalStore``. The orchestrator opens it,
holds the database exclusively, so a second runtime against the same database refuses to start, and every bot borrows
it. Every approval write of the runtime therefore commits through the one writer of that store and is published here.
Only bots built outside the orchestrator open a store of their own, which only tests do; two stores over one database
exercise races between independent writer connections, but a write reaches only the signals of the store that
committed it.

Writers record signals on their transaction with the functions below, and the store's writer publishes them in commit
order once their batch commits, before any caller resumes, so a caller cancelled after its commit still publishes.
A listener is called on the event loop it subscribed from, through ``call_soon_threadsafe`` when that is not the
writer's, so it sees signals in commit order and never after it stopped listening.
"""

from __future__ import annotations

import asyncio
import threading
from contextlib import contextmanager
from dataclasses import dataclass, field
from functools import partial
from typing import TYPE_CHECKING

from mindroom.logging_config import get_logger

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Sequence

    from .approval_calls import CallKey
    from .backend import Transaction

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class PausedReply:
    """A reply paused for an approval, which holds the conversation it answers until the approval ends."""

    principal_id: str
    approval_id: str
    room_id: str
    thread_id: str | None


@dataclass(frozen=True, slots=True)
class DecidedCard:
    """The live request card of a call just decided, whose terminal edit is queued."""

    delivery_id: str
    card_event_id: str | None
    room_id: str


@dataclass(frozen=True, slots=True)
class _Paused:
    reply: PausedReply


@dataclass(frozen=True, slots=True)
class _Ended:
    principal_id: str
    approval_id: str


@dataclass(frozen=True, slots=True)
class _Loaded:
    replies: tuple[PausedReply, ...]


@dataclass(frozen=True, slots=True)
class _Decided:
    call: CallKey
    card: DecidedCard | None


def paused(transaction: Transaction, reply: PausedReply) -> None:
    """Record that ``reply`` is paused, by its first pause or an advance to its next one."""
    transaction.signal(_Paused(reply))


def ended(transaction: Transaction, principal_id: str, approval_id: str) -> None:
    """Record that the paused reply of ``approval_id`` is gone."""
    transaction.signal(_Ended(principal_id, approval_id))


def decided(transaction: Transaction, call: CallKey, card: DecidedCard | None) -> None:
    """Record that ``call`` was decided, and the live request card whose terminal edit the decision queued."""
    transaction.signal(_Decided(call, card))


def load(transaction: Transaction) -> None:
    """Record every principal's paused replies, which replace what the signals held at this point of commit order."""
    rows = transaction.fetchall(
        """
        SELECT continuations.principal_id, continuations.approval_id, reply.room_id, reply.thread_id
        FROM approval_continuations AS continuations
        JOIN reply_spans AS span
          ON span.principal_id = continuations.principal_id AND span.span_id = continuations.span_id
        JOIN reply_messages AS reply
          ON reply.principal_id = span.principal_id AND reply.reply_id = span.reply_id
        """,
    )
    transaction.signal(
        _Loaded(
            tuple(
                PausedReply(
                    principal_id=str(row["principal_id"]),
                    approval_id=str(row["approval_id"]),
                    room_id=str(row["room_id"]),
                    thread_id=None if row["thread_id"] is None else str(row["thread_id"]),
                )
                for row in rows
            ),
        ),
    )


@dataclass(eq=False, slots=True)
class _Listener:
    """One subscriber's loop and whether it still listens."""

    loop: asyncio.AbstractEventLoop
    active: bool = True

    def deliver(self, callback: Callable[[], None]) -> None:
        """Run ``callback`` on this listener's loop, unless it stopped listening first."""
        if self.loop is _running_loop():
            self._run(callback)
            return
        if not self.loop.is_closed():
            try:
                self.loop.call_soon_threadsafe(self._run, callback)
            except RuntimeError:
                # The loop closed between the check and the call: nobody listens there any more.
                logger.debug("approval_signal_listener_loop_closed")

    def _run(self, callback: Callable[[], None]) -> None:
        if not self.active:
            return
        try:
            callback()
        except Exception:
            # A listener's failure is its own; the writer and the other listeners go on.
            logger.exception("approval_signal_listener_failed")


@dataclass(eq=False, slots=True)
class _PausedReplyListener(_Listener):
    paused: Callable[[PausedReply], None] = field(kw_only=True)
    ended: Callable[[str], None] = field(kw_only=True)


@dataclass(eq=False, slots=True)
class _DecidedCardListener(_Listener):
    decided: Callable[[DecidedCard], None] = field(kw_only=True)


@dataclass(eq=False, slots=True)
class _Wakeup(_Listener):
    event: asyncio.Event = field(default_factory=asyncio.Event, kw_only=True)


@dataclass
class ApprovalSignals:
    """One store's paused replies and decision wake-ups, derived only from what its writes committed."""

    _paused: dict[str, dict[str, PausedReply]] = field(default_factory=dict, init=False, repr=False)
    _paused_listeners: dict[str, list[_PausedReplyListener]] = field(default_factory=dict, init=False, repr=False)
    _decided_listeners: list[_DecidedCardListener] = field(default_factory=list, init=False, repr=False)
    # One wake-up per detached call someone waits on.
    _wakeups: dict[CallKey, _Wakeup] = field(default_factory=dict, init=False, repr=False)
    # Publication runs on the writer's loop; subscribers may sit on others.
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)

    def publish(self, signals: Sequence[object]) -> None:
        """Apply one committed batch's signals in order and tell each listener what concerns it."""
        deliveries: list[tuple[_Listener, Callable[[], None]]] = []
        with self._lock:
            for signal in signals:
                deliveries.extend(self._apply(signal))
        for listener, callback in deliveries:
            listener.deliver(callback)

    def _apply(self, signal: object) -> list[tuple[_Listener, Callable[[], None]]]:
        if isinstance(signal, _Paused):
            return self._add(signal.reply)
        if isinstance(signal, _Ended):
            return self._remove(signal.principal_id, signal.approval_id)
        if isinstance(signal, _Loaded):
            current = {reply.approval_id: reply for reply in signal.replies}
            known = [reply for replies in self._paused.values() for reply in replies.values()]
            deliveries = [
                delivery
                for reply in known
                if current.get(reply.approval_id) != reply
                for delivery in self._remove(reply.principal_id, reply.approval_id)
            ]
            return deliveries + [delivery for reply in signal.replies for delivery in self._add(reply)]
        assert isinstance(signal, _Decided), f"unknown approval signal {signal!r}"
        wakeup = self._wakeups.get(signal.call)
        if wakeup is not None:
            wakeup.deliver(wakeup.event.set)
        card = signal.card
        if card is None:
            return []
        return [(listener, partial(listener.decided, card)) for listener in self._decided_listeners]

    def _add(self, reply: PausedReply) -> list[tuple[_Listener, Callable[[], None]]]:
        replies = self._paused.setdefault(reply.principal_id, {})
        if replies.get(reply.approval_id) == reply:
            return []
        replies[reply.approval_id] = reply
        return [
            (listener, partial(listener.paused, reply))
            for listener in self._paused_listeners.get(reply.principal_id, ())
        ]

    def _remove(self, principal_id: str, approval_id: str) -> list[tuple[_Listener, Callable[[], None]]]:
        replies = self._paused.get(principal_id, {})
        if replies.pop(approval_id, None) is None:
            return []
        return [
            (listener, partial(listener.ended, approval_id))
            for listener in self._paused_listeners.get(principal_id, ())
        ]

    def follow_paused_replies(
        self,
        principal_id: str,
        *,
        paused: Callable[[PausedReply], None],
        ended: Callable[[str], None],
    ) -> Callable[[], None]:
        """Report each paused reply of ``principal_id`` as it pauses and ends; return how to stop listening.

        The replies paused now are reported before this returns, in the same step that registers the listener, so
        no change falls between them.
        """
        listener = _PausedReplyListener(asyncio.get_running_loop(), paused=paused, ended=ended)
        with self._lock:
            current = tuple(self._paused.get(principal_id, {}).values())
            self._paused_listeners.setdefault(principal_id, []).append(listener)
        for reply in current:
            listener.deliver(partial(paused, reply))

        def stop() -> None:
            with self._lock:
                listener.active = False
                listeners = self._paused_listeners.get(principal_id, [])
                if listener in listeners:
                    listeners.remove(listener)

        return stop

    def follow_decided_cards(self, decided: Callable[[DecidedCard], None]) -> Callable[[], None]:
        """Report each request card whose call is decided, as the decision commits; return how to stop listening."""
        listener = _DecidedCardListener(asyncio.get_running_loop(), decided=decided)
        with self._lock:
            self._decided_listeners.append(listener)

        def stop() -> None:
            with self._lock:
                listener.active = False
                if listener in self._decided_listeners:
                    self._decided_listeners.remove(listener)

        return stop

    @contextmanager
    def decision_wakeup(self, call: CallKey) -> Iterator[asyncio.Event]:
        """Hold the wake-up a decision of ``call`` sets, until the block exits; it replaces another waiter's."""
        wakeup = _Wakeup(asyncio.get_running_loop())
        with self._lock:
            self._wakeups[call] = wakeup
        try:
            yield wakeup.event
        finally:
            with self._lock:
                # Only this waiter's own registration goes.
                if self._wakeups.get(call) is wakeup:
                    del self._wakeups[call]


def _running_loop() -> asyncio.AbstractEventLoop | None:
    """Return the loop this call is running on, if it is running on one."""
    try:
        return asyncio.get_running_loop()
    except RuntimeError:
        return None

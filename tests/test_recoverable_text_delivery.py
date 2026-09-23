"""Replies that are not model answers still belong to a turn, and to the outbox.

The delivery cutover moved model answers onto the claim-before-send outbox and
left every other visible reply -- command results, the rejection notice, the
"still resolving" notice -- on the direct path. The rationale recorded for that
was that such a send "has no identity that survives a restart". For a voice echo
that is true. For these it never was: each one carries a ``TurnRecord`` whose
anchor event ID is exactly that identity.

Two things follow from the omission, and these tests pin both.

A direct send settles nothing, so the terminal record lands in the handled-turn
ledger while the journal source is still pending -- the window that forces the
degraded replay guard to consult two records instead of one. Routing the send
through the outbox settles the sources inside the enqueue, so the answer
becoming durably owed and the turn leaving the journal are one commit.

And a direct send is recovered only by scanning the room for something that may
already be there. An outbox row is recovered by resending itself, under a
transaction ID the homeserver will collapse.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import pytest

from mindroom.event_journal import DeliveryStage
from mindroom.turn_record import TurnRecord
from mindroom.visible_response_reconciliation import (
    VisibleResponseReconciler,
    VisibleResponseReconcilerDeps,
)

if TYPE_CHECKING:
    from mindroom.delivery_gateway import SendTextRequest

pytestmark = pytest.mark.asyncio

ROOM = "!room:example.org"
SOURCE = "$command"


@dataclass
class _RecordingGateway:
    """A delivery gateway that keeps the requests it was handed."""

    requests: list[SendTextRequest] = field(default_factory=list)

    async def send_text(self, request: SendTextRequest) -> str | None:
        """Accept one send and report a fixed event."""
        self.requests.append(request)
        return "$sent"


@dataclass
class _RecordingTurnStore:
    """The slice of the turn store this delivery touches."""

    recorded: list[TurnRecord] = field(default_factory=list)

    async def record_pending_turn(self, turn_record: TurnRecord) -> TurnRecord:
        """Bind one visible response to its turn."""
        self.recorded.append(turn_record)
        return turn_record


def _reconciler(gateway: _RecordingGateway) -> VisibleResponseReconciler:
    """Return a reconciler wired to recording collaborators."""

    async def settle_ignored(_event_ids: tuple[str, ...]) -> None:
        return

    return VisibleResponseReconciler(
        deps=VisibleResponseReconcilerDeps(
            runtime=None,  # type: ignore[arg-type]
            logger=None,  # type: ignore[arg-type]
            response_sender="@agent:example.org",
            turn_store=_RecordingTurnStore(),  # type: ignore[arg-type]
            delivery_gateway=gateway,  # type: ignore[arg-type]
            settle_ignored_sources=settle_ignored,
        ),
    )


def _target() -> object:
    """Return the smallest message target this delivery reads."""

    @dataclass(frozen=True)
    class _Target:
        room_id: str = ROOM
        resolved_thread_id: str | None = None

    return _Target()


class TestAReplyWithATurnBehindItIsDurable:
    """The outbox is what makes a reply survive the crash that follows it."""

    async def test_a_turn_reply_carries_its_turn_into_the_outbox(self) -> None:
        """The turn's anchor is the identity the outbox keys on.

        Without it the send takes the direct path: nothing settles the journal
        sources in the same commit, and nothing frozen exists for recovery to
        resend.
        """
        gateway = _RecordingGateway()
        handled_turn = TurnRecord.create([SOURCE])

        await _reconciler(gateway).deliver_recoverable_text(
            handled_turn,
            target=_target(),  # type: ignore[arg-type]
            response_text="the command result",
            recovered_response_event_id=None,
        )

        assert len(gateway.requests) == 1
        assert gateway.requests[0].delivery_turn_id == handled_turn.anchor_event_id
        assert gateway.requests[0].delivery_turn_id is not None
        assert gateway.requests[0].delivery_stage is DeliveryStage.FINAL

    async def test_an_answer_recovery_already_found_is_never_sent_again(self) -> None:
        """Adoption still wins over the outbox, and must not enqueue anything.

        A row from a previous membership is gone while its answer is not, so
        the room scan runs ahead of the send. Reaching the gateway at all here
        would put a second copy of an answer already in the room.
        """
        gateway = _RecordingGateway()

        adopted = await _reconciler(gateway).deliver_recoverable_text(
            TurnRecord.create([SOURCE]),
            target=_target(),  # type: ignore[arg-type]
            response_text="the command result",
            recovered_response_event_id="$already-there",
        )

        assert adopted == "$already-there"
        assert gateway.requests == []

    async def test_a_placeholder_reply_claims_the_initial_stage(self) -> None:
        """A send a later answer edits is the turn's placeholder, not its answer.

        The interactive-selection acknowledgement is exactly this: the answer
        is generated with ``existing_event_is_placeholder=True`` against the
        event this send produced. Filing it as the answer would claim the
        turn's ``FINAL`` row, so the real answer's delivery would be refused
        and its text would never reach the room.

        The stage also decides settlement. Only an answer discharges a turn, so
        a placeholder must not settle the journal sources -- a crash before the
        model finished would otherwise leave the acknowledgement in the room
        with nothing pending to replay.
        """
        gateway = _RecordingGateway()
        handled_turn = TurnRecord.create([SOURCE])

        await _reconciler(gateway).deliver_recoverable_text(
            handled_turn,
            target=_target(),  # type: ignore[arg-type]
            response_text="Processing your response...",
            recovered_response_event_id=None,
            as_placeholder=True,
        )

        assert gateway.requests[0].delivery_turn_id == handled_turn.anchor_event_id
        assert gateway.requests[0].delivery_stage is DeliveryStage.INITIAL

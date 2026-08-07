"""Where a turn stops being the journal's work and becomes the outbox's.

Contract 2 of the event-journal cutover moves that handoff off "the turn is
terminal" and onto "the answer is durably owed to a room". The two are not the
same moment and not the same fact: a turn can be terminal with nothing durable
behind it, and an answer can be durable long before its turn ends.

The handoff point is the FINAL outbox enqueue. Before it, the journal owns the
source and a crash replays the turn. After it, the outbox owns the answer and
recovery resends the frozen payload under the same transaction ID. The tests
below pin both halves and the two ways the handoff must refuse to fire.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import nio
import pytest

from mindroom.delivery_gateway import SendTextRequest
from mindroom.dispatch_callback_outcome import TurnDispatchOutcome
from mindroom.event_journal import DeliveryStage, EventClass, EventKind, SettlementOutcome
from mindroom.handled_turns import TurnRecord
from mindroom.journal_dispatch import JournalCallbacks, JournalDispatcher
from mindroom.matrix.journal_ingress import inbound_event, projected_event
from mindroom.message_target import MessageTarget
from mindroom.pending_event_worker import PendingEventWorker
from mindroom.response_delivery import ResponseDelivery
from mindroom.turn_record import canonicalize_turn_record
from tests.test_live_message_coalescing import _make_bot

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.bot import AgentBot
    from mindroom.event_journal import JournalEvent, OutboxDelivery, PrincipalStore
    from mindroom.journal_dispatch import _MessageCallback

pytestmark = pytest.mark.asyncio

ROOM = "!room:localhost"
ALICE = "@user:localhost"
BOT = "@mindroom_test_agent:localhost"


def text_event(event_id: str, body: str = "question", *, ts: int = 1_000) -> nio.RoomMessageText:
    """Return one parsed inbound message."""
    parsed = nio.Event.parse_event(
        {
            "event_id": event_id,
            "sender": ALICE,
            "origin_server_ts": ts,
            "room_id": ROOM,
            "type": "m.room.message",
            "content": {"msgtype": "m.text", "body": body},
        },
    )
    assert isinstance(parsed, nio.RoomMessageText)
    return parsed


async def admit(store: PrincipalStore, *events: nio.Event) -> None:
    """Admit each event as pending semantic work, as live ingress would."""
    for event in events:
        await store.admit(
            inbound_event(ROOM, event, EventKind.MESSAGE, EventClass.ACTIONABLE),
            projected_event(ROOM, event, EventKind.MESSAGE, self_sender=BOT),
        )


def journal(bot: AgentBot) -> PrincipalStore:
    """Return the bot's own principal-bound store."""
    return bot._journal_store.principal(bot._journal_principal_id)


async def pending_ids(bot: AgentBot) -> list[str]:
    """Return every source this bot still owes work on, in receipt order."""
    return [event.event_id for event in await journal(bot).pending()]


def adopt(bot: AgentBot, source_event_ids: list[str]) -> None:
    """Durably adopt one turn, as ingress does before starting a response."""
    bot._turn_store.record_pending_turn(
        canonicalize_turn_record(
            TurnRecord.create(source_event_ids, completed=False),
            response_owner=bot.agent_name,
            requester_id=ALICE,
            conversation_target=MessageTarget.resolve(ROOM, None, source_event_ids[-1]),
        ),
    )


async def deliver_answer(
    bot: AgentBot,
    turn_id: str,
    *,
    body: str = "the answer",
    stage: DeliveryStage = DeliveryStage.FINAL,
    sends: list[str] | None = None,
) -> str | None:
    """Run one delivery through the production outbox path and its handoff.

    Built the way `DeliveryGateway` builds it, so the handoff under test is the
    one the gateway wires rather than one this test invented.
    """

    async def send(claimed: OutboxDelivery) -> str:
        if sends is not None:
            sends.append(claimed.transaction_id)
        return f"$sent-{claimed.transaction_id}"

    delivery = ResponseDelivery(
        store=bot._delivery_gateway.deps.outbox,
        send=send,
        on_final_enqueued=bot._delivery_gateway.deps.on_final_delivery_enqueued,
    )
    return await delivery.deliver(
        turn_id=turn_id,
        stage=stage,
        room_id=ROOM,
        thread_id=None,
        payload={"msgtype": "m.text", "body": body},
    )


def _dispatcher(
    bot: AgentBot,
    on_message: _MessageCallback,
    *,
    owner_is_live: bool = False,
) -> JournalDispatcher:
    """Return a dispatcher over this bot's journal with one watched callback.

    ``owner_is_live`` stands in for a turn that is still running. The worker
    drops a deferral whose owner has vanished, so a test that wants to observe
    a deferral surviving until the handoff has to say that its owner is still
    there -- otherwise the liveness recheck clears it first and the assertion
    passes against an empty set for the wrong reason.
    """

    async def unused(_room: nio.MatrixRoom, _event: nio.Event) -> None:
        msg = "this callback is not part of the test"
        raise AssertionError(msg)

    dispatcher = JournalDispatcher(
        store=journal(bot),
        self_sender=BOT,
        callbacks=JournalCallbacks(
            on_message=on_message,
            on_media=cast("Any", unused),
            on_reaction=cast("Any", unused),
            on_approval=cast("Any", unused),
            on_room_lifecycle=cast("Any", unused),
            on_redaction=cast("Any", unused),
            on_decryption_failure=cast("Any", unused),
            source_has_live_owner=lambda _event_id: owner_is_live,
            turn_has_live_claim=lambda _event_id: False,
        ),
        room_for_id=lambda room_id: nio.MatrixRoom(room_id, BOT),
    )
    dispatcher.release_turn_replay()
    return dispatcher


class TestTheHandoffIsTheDurableEnqueue:
    """A source leaves the journal when its answer becomes recoverable."""

    async def test_a_final_enqueue_hands_over_every_source_of_a_coalesced_turn(
        self,
        tmp_path: Path,
    ) -> None:
        """The unit handed over is the turn, not the event the outbox is keyed on.

        A batch of three messages answers with one delivery under one anchor.
        Settling only that anchor would leave its siblings pending, and they
        would replay into a turn that has already been answered.
        """
        bot = _make_bot(tmp_path)
        sources = [text_event("$one", ts=1_000), text_event("$two", ts=1_001), text_event("$caption", ts=1_002)]
        await admit(journal(bot), *sources)
        adopt(bot, ["$one", "$two", "$caption"])

        await deliver_answer(bot, "$caption")

        assert await pending_ids(bot) == []
        settled = await journal(bot).load_event("$one")
        assert settled is not None

    async def test_a_placeholder_does_not_hand_the_turn_over(self, tmp_path: Path) -> None:
        """A placeholder is not an answer, so it cannot end the journal's ownership.

        Handing over on `INITIAL` would leave a crash between the placeholder
        and the model's result with nothing pending to replay and nothing
        durable to send -- the user reading "Thinking..." forever.
        """
        bot = _make_bot(tmp_path)
        await admit(journal(bot), text_event("$cause"))
        adopt(bot, ["$cause"])

        await deliver_answer(bot, "$cause", body="Thinking...", stage=DeliveryStage.INITIAL)

        assert await pending_ids(bot) == ["$cause"]

    async def test_the_handoff_happens_before_the_send(self, tmp_path: Path) -> None:
        """Once the row exists the answer is recoverable without the model.

        Waiting for the network call to return would keep the turn replayable
        across a crash the outbox already covers, and buy a second model run
        for it.
        """
        bot = _make_bot(tmp_path)
        await admit(journal(bot), text_event("$cause"))
        adopt(bot, ["$cause"])
        pending_at_send: list[list[str]] = []

        async def send(claimed: OutboxDelivery) -> str:
            pending_at_send.append(await pending_ids(bot))
            return f"$sent-{claimed.transaction_id}"

        delivery = ResponseDelivery(
            store=bot._delivery_gateway.deps.outbox,
            send=send,
            on_final_enqueued=bot._delivery_gateway.deps.on_final_delivery_enqueued,
        )
        await delivery.deliver(
            turn_id="$cause",
            stage=DeliveryStage.FINAL,
            room_id=ROOM,
            thread_id=None,
            payload={"msgtype": "m.text", "body": "the answer"},
        )

        assert pending_at_send == [[]]

    async def test_an_enqueue_refused_for_an_ended_membership_hands_nothing_over(
        self,
        tmp_path: Path,
    ) -> None:
        """A refusal leaves no row, so it must leave the turn where it was.

        The fence refuses an answer that belongs to a membership that ended.
        Nothing durable owes it afterwards, so settling the source here would
        leave no owner at all -- the silent loss this ordering exists to
        prevent. Leaving it pending is what keeps the work attributable.
        """
        bot = _make_bot(tmp_path)
        await admit(journal(bot), text_event("$cause"))
        adopt(bot, ["$cause"])
        await journal(bot).advance_membership_epoch(ROOM)
        sends: list[str] = []

        event_id = await deliver_answer(bot, "$cause", sends=sends)

        assert event_id is None
        assert sends == []
        assert await pending_ids(bot) == ["$cause"]

    async def test_a_turn_that_owes_no_answer_still_settles_as_ignored(self, tmp_path: Path) -> None:
        """Commands, router decisions, and ignored inputs keep their own path.

        Nothing was ever owed to a room, so there is no enqueue to hand over
        on, and the intentionally-ignored settlement is the whole story.
        """
        bot = _make_bot(tmp_path)
        await admit(journal(bot), text_event("$command"))

        await bot._journal_dispatcher.settle_intentionally_ignored_turn_sources(("$command",))

        assert await pending_ids(bot) == []
        settled = await journal(bot).load_event("$command")
        assert settled is not None


class TestWhatARestartOwesAfterTheHandoff:
    """Which side of the handoff a crash lands on decides who recovers."""

    @staticmethod
    async def _replayed_sources(bot: AgentBot) -> list[str]:
        """Drain the journal as a restart does and report what it re-dispatched."""
        replayed: list[str] = []

        async def handle(event: JournalEvent) -> SettlementOutcome | None:
            replayed.append(event.event_id)
            return SettlementOutcome.SUCCEEDED

        await PendingEventWorker(store=journal(bot), handle=handle).drain_once()
        return replayed

    async def test_a_crash_after_adoption_before_the_response_task_replays_the_turn(
        self,
        tmp_path: Path,
    ) -> None:
        """Boundary four stays journal-owned, and that is the documented cost.

        The durable turn record exists but nothing durable owes an answer, so
        the source has to replay and the model has to run again. A record in
        `TurnStore` is not a promise that anything will be said.
        """
        bot = _make_bot(tmp_path)
        await admit(journal(bot), text_event("$cause"))
        adopt(bot, ["$cause"])
        # The response task is never created: this is the crash.

        assert await pending_ids(bot) == ["$cause"]
        assert await self._replayed_sources(bot) == ["$cause"]

    async def test_a_restart_after_the_handoff_replays_nothing(self, tmp_path: Path) -> None:
        """The sources are gone from the journal, so no turn is re-dispatched.

        This is what stops the model running a second time for an answer that
        is already written down. Recovery has one job left, and it is the
        outbox's.
        """
        bot = _make_bot(tmp_path)
        await admit(journal(bot), text_event("$one", ts=1_000), text_event("$two", ts=1_001))
        adopt(bot, ["$one", "$two"])
        await deliver_answer(bot, "$two")

        assert await self._replayed_sources(bot) == []

    async def test_the_answer_a_restart_owes_is_the_one_the_outbox_froze(
        self,
        tmp_path: Path,
    ) -> None:
        """A crash after the handoff and before acknowledgement is outbox work.

        The turn is gone from the journal, so nothing regenerates the answer.
        What recovery resends is the payload the claim froze, under the same
        transaction ID, which is what collapses it onto one visible message.
        """
        bot = _make_bot(tmp_path)
        await admit(journal(bot), text_event("$cause"))
        adopt(bot, ["$cause"])
        outbox = bot._delivery_gateway.deps.outbox

        async def crash(_claimed: OutboxDelivery) -> str:
            msg = "crashed after the claim committed"
            raise RuntimeError(msg)

        delivery = ResponseDelivery(
            store=outbox,
            send=crash,
            on_final_enqueued=bot._delivery_gateway.deps.on_final_delivery_enqueued,
        )
        with pytest.raises(RuntimeError, match="crashed after the claim committed"):
            await delivery.deliver(
                turn_id="$cause",
                stage=DeliveryStage.FINAL,
                room_id=ROOM,
                thread_id=None,
                payload={"msgtype": "m.text", "body": "the answer"},
            )

        assert await self._replayed_sources(bot) == []
        sends: list[str] = []

        async def send(claimed: OutboxDelivery) -> str:
            sends.append(str(claimed.payload["body"]))
            return "$sent"

        assert (await ResponseDelivery(store=outbox, send=send).recover()).recovered == 1
        assert sends == ["the answer"]

    async def test_a_replayed_turn_deduplicates_onto_the_answer_it_already_wrote(
        self,
        tmp_path: Path,
    ) -> None:
        """The handoff is idempotent: running the turn twice says one thing once.

        A crash between adoption and enqueue replays the turn, so the model
        runs again and produces its answer again. Enqueueing an already
        attempted row keeps the frozen payload and its transaction, so the
        second run collapses onto the first answer rather than adding one.
        """
        bot = _make_bot(tmp_path)
        await admit(journal(bot), text_event("$cause"))
        adopt(bot, ["$cause"])
        transactions: list[str] = []

        first = await deliver_answer(bot, "$cause", body="first answer", sends=transactions)
        # The replay re-runs the model, which produces its answer again.
        second = await deliver_answer(bot, "$cause", body="regenerated answer", sends=transactions)

        assert first == second
        assert len(set(transactions)) == 1
        row = await bot._delivery_gateway.deps.outbox.load_delivery(turn_id="$cause", stage=DeliveryStage.FINAL)
        assert row is not None
        assert row.payload["body"] == "first answer"


class TestTheJournalNoLongerAsksWhetherATurnFinished:
    """The duplicate execution authority contract 2 exists to remove."""

    async def test_a_terminal_turn_does_not_retire_a_source_the_journal_still_owes(
        self,
        tmp_path: Path,
    ) -> None:
        """Terminal is not the same fact as answered, and only one of them settles.

        The old handoff asked `TurnStore` whether a turn had finished and
        retired the source without running anything. A turn can be terminal
        with nothing durable behind it -- a stop, a suppressed response, an
        error that never reached the outbox -- so that question retires work
        whose answer was never written down. The journal now hands the event
        back to the turn engine and lets it decide, which is the only owner
        that can tell those cases apart.
        """
        bot = _make_bot(tmp_path)
        dispatched: list[str] = []

        async def on_message(_room: nio.MatrixRoom, event: nio.RoomMessageText) -> TurnDispatchOutcome:
            dispatched.append(event.event_id)
            return TurnDispatchOutcome.DEFERRED

        dispatcher = _dispatcher(bot, on_message)
        await admit(journal(bot), text_event("$cause"))
        await dispatcher.drain_once()
        assert dispatched == ["$cause"]

        bot._turn_store.record_turn(TurnRecord.create(["$cause"], response_event_id="$response"))
        await dispatcher.drain_once()

        assert bot._turn_store.is_handled("$cause")
        assert dispatched == ["$cause", "$cause"], "the journal decided on the turn engine's behalf"

    async def test_the_handoff_stops_the_worker_holding_the_event_it_gave_away(
        self,
        tmp_path: Path,
    ) -> None:
        """A handed-over event leaves the in-flight set as well as the journal.

        The worker remembers deferred events so it will not dispatch a turn
        that is still running. Nothing else clears that memory once the row is
        settled -- the scan reads pending rows and this one is gone -- so an
        entry left behind stays for the lifetime of the process, one per
        delivered turn.
        """
        bot = _make_bot(tmp_path)

        async def defer(_room: nio.MatrixRoom, _event: nio.RoomMessageText) -> TurnDispatchOutcome:
            return TurnDispatchOutcome.DEFERRED

        dispatcher = _dispatcher(bot, defer, owner_is_live=True)
        await admit(journal(bot), text_event("$cause"))
        await dispatcher.drain_once()
        assert "$cause" in dispatcher._worker._deferred, "the deferral must survive to be released"

        await dispatcher.settle_delivered_turn_sources(("$cause",))

        assert dispatcher._worker._deferred == set()


class TestTheHandoffCarriesTheWholeTurn:
    """A coalesced media batch is one turn, and leaves the journal as one."""

    async def test_a_media_batch_replays_no_source_after_its_answer_is_durable(
        self,
        tmp_path: Path,
    ) -> None:
        """The remaining half of the batch replay test the snapshot made possible.

        `TestReplayFidelity` and `TestSnapshotReplayFidelity` already pin that
        the media survives. This pins the other half: once the batch's answer
        is durable, not one of its four sources replays, so the batch executes
        exactly once.
        """
        bot = _make_bot(tmp_path)
        batch = ["$one", "$two", "$three", "$caption"]
        await admit(journal(bot), *(text_event(event_id, ts=1_000 + index) for index, event_id in enumerate(batch)))
        adopt(bot, batch)
        replayed: list[str] = []

        async def handle(event: JournalEvent) -> SettlementOutcome | None:
            replayed.append(event.event_id)
            return SettlementOutcome.SUCCEEDED

        await deliver_answer(bot, "$caption")
        await PendingEventWorker(store=journal(bot), handle=handle).drain_once()

        assert replayed == []
        assert await pending_ids(bot) == []


class TestTheGatewayWiresTheHandoff:
    """The production path, not just the collaborator underneath it."""

    async def test_a_final_answer_sent_through_the_gateway_hands_the_turn_over(
        self,
        tmp_path: Path,
    ) -> None:
        """`send_text` is where a turn with no placeholder writes its answer."""
        bot = _make_bot(tmp_path)
        await admit(journal(bot), text_event("$cause"))
        adopt(bot, ["$cause"])
        delivered = MagicMock(event_id="$sent", content_sent={"msgtype": "m.text", "body": "answer"})

        with patch("mindroom.delivery_gateway.send_message_result", AsyncMock(return_value=delivered)):
            sent = await bot._delivery_gateway.send_text(
                SendTextRequest(
                    target=MessageTarget.resolve(ROOM, None, "$cause"),
                    response_text="answer",
                    delivery_turn_id="$cause",
                ),
            )

        assert sent == "$sent"
        assert await pending_ids(bot) == []

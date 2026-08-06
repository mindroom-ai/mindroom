"""The outbox is what makes one turn produce at most one visible answer.

A turn's final answer is durable before it is attempted and carries a
transaction ID derived from the turn, so a resend after a crash collapses onto
the event the homeserver already accepted rather than posting a second answer.
"""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import pytest

from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.delivery_gateway import (
    DeliveryGateway,
    DeliveryGatewayDeps,
    DeliveryStage,
    FinalDeliveryRequest,
    ResponseIdentity,
    SendTextRequest,
)
from mindroom.hooks.context import ResponseDraft
from mindroom.message_target import MessageTarget
from mindroom.streaming import PROGRESS_PLACEHOLDER
from tests.conftest import (
    FakeOutbox,
    bind_runtime_paths,
    make_event_cache_mock,
    make_outbox_mock,
    runtime_paths_for,
    test_runtime_paths,
)

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.event_journal import EventJournalStore, OutboxView, PrincipalStore

pytestmark = pytest.mark.asyncio

_ROOM_ID = "!room:localhost"
_AGENT_USER_ID = "@agent:localhost"


@pytest.fixture
def alice(journal_store: EventJournalStore) -> PrincipalStore:
    """Return one bound principal view."""
    return journal_store.principal("agent@alice")


def _gateway(tmp_path: Path, outbox: OutboxView | None = None) -> DeliveryGateway:
    """Return a delivery gateway whose only real collaborator is the outbox."""
    config = bind_runtime_paths(
        Config(agents={"agent": AgentConfig(display_name="Agent")}),
        test_runtime_paths(tmp_path),
    )
    return DeliveryGateway(
        DeliveryGatewayDeps(
            runtime=SimpleNamespace(
                client=AsyncMock(),
                config=config,
                enable_streaming=True,
                orchestrator=None,
                event_cache=make_event_cache_mock(),
            ),
            runtime_paths=runtime_paths_for(config),
            agent_name="agent",
            logger=MagicMock(),
            redact_message_event=AsyncMock(return_value=True),
            resolver=SimpleNamespace(
                build_message_target=MagicMock(),
                deps=SimpleNamespace(
                    conversation_cache=SimpleNamespace(
                        get_latest_thread_event_id_if_needed=AsyncMock(return_value="$root"),
                        notify_outbound_message=Mock(),
                    ),
                ),
            ),
            response_hooks=MagicMock(_apply_before_response=AsyncMock(), emit_after_response=AsyncMock()),
            outbox=outbox if outbox is not None else make_outbox_mock(),
        ),
    )


class TestTurnDeliveryGoesThroughTheOutbox:
    """A send that belongs to a turn is durable before it is attempted."""

    @staticmethod
    def _hooks() -> MagicMock:
        """Return hooks that pass the draft through unchanged."""
        return MagicMock(
            _apply_before_response=AsyncMock(
                side_effect=lambda *, identity, response_text, tool_trace, extra_content: ResponseDraft(
                    response_text=response_text,
                    response_kind=identity.response_kind,
                    tool_trace=tool_trace,
                    extra_content=extra_content,
                    envelope=identity.response_envelope,
                ),
            ),
            _apply_final_response_transform=AsyncMock(side_effect=lambda *, draft, **_kwargs: draft),
            emit_after_response=AsyncMock(),
        )

    @staticmethod
    def _final_request(text: str) -> FinalDeliveryRequest:
        """Return one final delivery for the turn caused by `$cause`."""
        target = MessageTarget.resolve(_ROOM_ID, None, None, room_mode=True)
        return FinalDeliveryRequest(
            target=target,
            existing_event_id=None,
            response_text=text,
            identity=ResponseIdentity(
                response_kind="agent",
                response_envelope=SimpleNamespace(source_event_id="$cause"),  # type: ignore[arg-type]
                correlation_id="c1",
            ),
            tool_trace=None,
            extra_content=None,
        )

    async def test_a_final_answer_is_enqueued_before_it_is_sent(
        self,
        tmp_path: Path,
    ) -> None:
        """The row must exist before the network call, keyed on the causing event.

        That ordering is the whole point: a crash after Matrix accepted the
        message leaves a row recovery can find, and the turn that caused it is
        the only name for it that survives a restart.
        """
        outbox = FakeOutbox()
        gateway = _gateway(tmp_path, outbox)
        gateway.deps.response_hooks._apply_before_response = self._hooks()._apply_before_response
        delivered = SimpleNamespace(event_id="$sent", content_sent={"msgtype": "m.text", "body": "answer"})

        with patch("mindroom.delivery_gateway.send_message_result", AsyncMock(return_value=delivered)):
            outcome = await gateway.deliver_final(self._final_request("answer"))

        assert outcome.event_id == "$sent"
        assert list(outbox.rows) == [("$cause", "final")]
        assert outbox.rows["$cause", "final"].acknowledged_event_id == "$sent"

    async def test_the_same_turn_resends_under_the_same_transaction_id(
        self,
        tmp_path: Path,
    ) -> None:
        """A repeated turn must collapse onto the event the server already has.

        This is what stops a restart turning one answer into two. The ID is
        derived from the turn, so the second attempt presents the identical
        one and the homeserver discards it.
        """
        outbox = FakeOutbox()
        gateway = _gateway(tmp_path, outbox)
        gateway.deps.response_hooks._apply_before_response = self._hooks()._apply_before_response
        delivered = SimpleNamespace(event_id="$sent", content_sent={"msgtype": "m.text", "body": "answer"})
        send = AsyncMock(return_value=delivered)

        with patch("mindroom.delivery_gateway.send_message_result", send):
            await gateway.deliver_final(self._final_request("answer"))
            await gateway.deliver_final(self._final_request("answer"))

        transaction_ids = {call.kwargs["transaction_id"] for call in send.await_args_list}
        assert len(transaction_ids) == 1, "a repeated turn presented a different transaction ID"

    async def test_a_rerun_turn_does_not_send_again_and_keeps_the_first_answer(
        self,
        tmp_path: Path,
    ) -> None:
        """An acknowledged turn replays its event ID instead of sending.

        Regenerated content could never become visible anyway -- the
        homeserver would drop it as a duplicate transaction and the durable
        result and the room would disagree forever. Not sending at all is the
        same guarantee without the wasted round trip, so the second run must
        both skip the network and return the first answer's event.
        """
        outbox = FakeOutbox()
        gateway = _gateway(tmp_path, outbox)
        gateway.deps.response_hooks._apply_before_response = self._hooks()._apply_before_response
        delivered = SimpleNamespace(event_id="$sent", content_sent={"msgtype": "m.text", "body": "first"})
        send = AsyncMock(return_value=delivered)

        with patch("mindroom.delivery_gateway.send_message_result", send):
            first = await gateway.deliver_final(self._final_request("first"))
            second = await gateway.deliver_final(self._final_request("second answer entirely"))

        bodies = [call.args[2]["body"] for call in send.await_args_list]
        assert bodies == ["first"], f"a rerun turn sent again: {bodies}"
        assert first.event_id == "$sent"
        assert second.event_id == "$sent"

    async def test_a_send_with_no_turn_behind_it_stays_out_of_the_outbox(
        self,
        tmp_path: Path,
    ) -> None:
        """Voice echoes and command replies are not turns.

        Giving them a durable row would put entries in the outbox that no
        recovery pass can resolve, and two unrelated sends whose derived IDs
        collided would collapse into one visible message.
        """
        outbox = FakeOutbox()
        gateway = _gateway(tmp_path, outbox)
        gateway.deps.response_hooks._apply_before_response = self._hooks()._apply_before_response
        delivered = SimpleNamespace(event_id="$sent", content_sent={"msgtype": "m.text", "body": "a notice"})

        with patch("mindroom.delivery_gateway.send_message_result", AsyncMock(return_value=delivered)):
            event_id = await gateway.send_text(
                SendTextRequest(
                    target=MessageTarget.resolve(_ROOM_ID, None, None, room_mode=True),
                    response_text="a notice",
                ),
            )

        assert event_id == "$sent"
        assert outbox.rows == {}

    async def test_a_streaming_placeholder_is_durable_under_its_own_stage(
        self,
        tmp_path: Path,
    ) -> None:
        """A streamed answer creates its visible message once, as a placeholder.

        Everything after that is an edit of the same event, so the placeholder
        is the send a crash could turn into two answers in the room. It needs
        the same durability the blocking path has, under its own stage, so it
        does not collide with the final delivery of the same turn.
        """
        outbox = FakeOutbox()
        gateway = _gateway(tmp_path, outbox)
        delivered = SimpleNamespace(event_id="$placeholder", content_sent={"msgtype": "m.text", "body": "..."})

        with patch("mindroom.delivery_gateway.send_message_result", AsyncMock(return_value=delivered)):
            event_id = await gateway.send_text(
                SendTextRequest(
                    target=MessageTarget.resolve(_ROOM_ID, None, None, room_mode=True),
                    response_text="...",
                    delivery_turn_id="$cause",
                    delivery_stage=DeliveryStage.INITIAL,
                ),
            )

        assert event_id == "$placeholder"
        assert list(outbox.rows) == [("$cause", "initial")]

    async def test_the_placeholder_and_the_final_answer_do_not_collide(
        self,
        tmp_path: Path,
    ) -> None:
        """One turn has two durable delivery points and they are distinct.

        Sharing a stage would make the final answer look like a resend of the
        placeholder, so it would never be sent and the room would keep the
        placeholder for good.
        """
        outbox = FakeOutbox()
        gateway = _gateway(tmp_path, outbox)
        placeholder = SimpleNamespace(event_id="$placeholder", content_sent={"msgtype": "m.text", "body": "..."})
        send = AsyncMock(return_value=placeholder)

        with patch("mindroom.delivery_gateway.send_message_result", send):
            await gateway.send_text(
                SendTextRequest(
                    target=MessageTarget.resolve(_ROOM_ID, None, None, room_mode=True),
                    response_text="...",
                    delivery_turn_id="$cause",
                    delivery_stage=DeliveryStage.INITIAL,
                ),
            )
            gateway.deps.response_hooks._apply_before_response = self._hooks()._apply_before_response
            await gateway.deliver_final(self._final_request("the answer"))

        assert sorted(outbox.rows) == [("$cause", "final"), ("$cause", "initial")]
        transaction_ids = {call.kwargs["transaction_id"] for call in send.await_args_list}
        assert len(transaction_ids) == 2, "the two delivery points shared a transaction ID"

    async def test_the_final_answer_is_durable_even_when_it_is_an_edit(
        self,
        tmp_path: Path,
    ) -> None:
        """Once a placeholder exists the answer arrives as an edit of it.

        That is the normal path, not a corner: every turn that shows
        "Thinking..." reaches its answer this way. An edit sent outside the
        outbox leaves nothing to recover, so a crash between generating the
        answer and editing it in leaves the user reading the placeholder for
        good.
        """
        outbox = FakeOutbox()
        gateway = _gateway(tmp_path, outbox)
        gateway.deps.response_hooks._apply_before_response = self._hooks()._apply_before_response
        edited = SimpleNamespace(event_id="$placeholder", content_sent={"msgtype": "m.text", "body": "the answer"})

        with patch("mindroom.delivery_gateway.edit_message_result", AsyncMock(return_value=edited)) as edit:
            outcome = await gateway.deliver_final(
                replace(self._final_request("the answer"), existing_event_id="$placeholder"),
            )

        assert outcome.event_id == "$placeholder"
        assert list(outbox.rows) == [("$cause", "final")]
        assert outbox.rows["$cause", "final"].edits_event_id == "$placeholder"
        assert edit.await_args.kwargs["transaction_id"] == "tx-$cause-final"
        # The stored payload is the finished replace event, because recovery
        # sends the row verbatim and cannot rebuild an envelope.
        stored = outbox.rows["$cause", "final"].payload
        assert stored["m.relates_to"] == {"rel_type": "m.replace", "event_id": "$placeholder"}
        assert stored["m.new_content"]["body"] == "the answer"

    async def test_a_rerun_turn_does_not_edit_the_answer_in_twice(
        self,
        tmp_path: Path,
    ) -> None:
        """An acknowledged final edit replays instead of editing again.

        The mirror of the durability test: without it, "always enqueue" would
        pass while still issuing a second edit on every rerun.
        """
        outbox = FakeOutbox()
        gateway = _gateway(tmp_path, outbox)
        gateway.deps.response_hooks._apply_before_response = self._hooks()._apply_before_response
        edited = SimpleNamespace(event_id="$placeholder", content_sent={"msgtype": "m.text", "body": "the answer"})
        edit = AsyncMock(return_value=edited)

        with patch("mindroom.delivery_gateway.edit_message_result", edit):
            first = await gateway.deliver_final(
                replace(self._final_request("the answer"), existing_event_id="$placeholder"),
            )
            second = await gateway.deliver_final(
                replace(self._final_request("a different answer"), existing_event_id="$placeholder"),
            )

        assert edit.await_count == 1, "a rerun turn edited the answer in a second time"
        assert first.event_id == second.event_id == "$placeholder"

    async def test_a_placeholder_terminal_edit_does_not_settle_the_turn(
        self,
        tmp_path: Path,
    ) -> None:
        """A stream that ends still showing "Thinking..." has not answered.

        Its terminal edit carries the placeholder, and `deliver_final` is what
        delivers the real answer afterwards -- against the same turn. If the
        placeholder edit claimed that turn's final delivery, `deliver_final`
        would find its own delivery already acknowledged, send nothing, and
        leave the placeholder in the room for good.
        """
        outbox = FakeOutbox()
        gateway = _gateway(tmp_path, outbox)
        gateway.deps.response_hooks._apply_before_response = self._hooks()._apply_before_response
        edited = SimpleNamespace(event_id="$placeholder", content_sent={"msgtype": "m.text", "body": "x"})
        terminal = gateway._durable_terminal_edit(
            "$cause",
            MessageTarget.resolve(_ROOM_ID, None, None, room_mode=True),
        )
        assert terminal is not None

        direct = AsyncMock(return_value=edited)
        with (
            patch("mindroom.delivery_gateway.edit_message_result", direct),
        ):
            # The stream ends on the placeholder, so its terminal edit is not
            # this turn's answer and must not claim the turn's final delivery.
            await terminal(AsyncMock(), _ROOM_ID, "$placeholder", {"body": PROGRESS_PLACEHOLDER}, PROGRESS_PLACEHOLDER)
            assert outbox.rows == {}, "a placeholder edit claimed the turn's final delivery"

            outcome = await gateway.deliver_final(
                replace(self._final_request("the answer"), existing_event_id="$placeholder"),
            )

        assert outcome.event_id == "$placeholder"
        assert direct.await_count == 2, "the placeholder edit or the answer did not go out"
        assert list(outbox.rows) == [("$cause", "final")]

    async def test_a_real_terminal_edit_does_settle_the_turn(
        self,
        tmp_path: Path,
    ) -> None:
        """The mirror: a stream that produced an answer records it.

        Without this, gating everything out would pass the test above while
        leaving streamed answers exactly as undurable as before.
        """
        outbox = FakeOutbox()
        gateway = _gateway(tmp_path, outbox)
        edited = SimpleNamespace(event_id="$streamed", content_sent={"msgtype": "m.text", "body": "streamed"})
        terminal = gateway._durable_terminal_edit(
            "$cause",
            MessageTarget.resolve(_ROOM_ID, None, None, room_mode=True),
        )
        assert terminal is not None

        with patch("mindroom.delivery_gateway.edit_message_result", AsyncMock(return_value=edited)):
            await terminal(AsyncMock(), _ROOM_ID, "$streamed", {"body": "streamed"}, "streamed")

        assert list(outbox.rows) == [("$cause", "final")]
        assert outbox.rows["$cause", "final"].acknowledged_event_id == "$streamed"

    async def test_recovery_replays_a_final_edit_as_an_edit(
        self,
        tmp_path: Path,
    ) -> None:
        """A crash between claiming and acknowledging must not add a message.

        Recovery has no request to rebuild from; it sends the row as frozen.
        If what was frozen were the new body rather than the finished replace
        event, the recovered answer would arrive as a second ordinary message
        with the placeholder still above it -- two visible messages for one
        turn, which is the thing the outbox exists to prevent.
        """
        outbox = FakeOutbox()
        gateway = _gateway(tmp_path, outbox)
        gateway.deps.response_hooks._apply_before_response = self._hooks()._apply_before_response
        edited = SimpleNamespace(event_id="$placeholder", content_sent={"body": "the answer"})

        # A delivery that reached Matrix but whose acknowledgement was lost.
        with patch("mindroom.delivery_gateway.edit_message_result", AsyncMock(return_value=None)):
            await gateway.deliver_final(
                replace(self._final_request("the answer"), existing_event_id="$placeholder"),
            )
        assert outbox.rows["$cause", "final"].acknowledged_event_id is None

        with patch("mindroom.delivery_gateway.send_message_result", AsyncMock(return_value=edited)) as send:
            recovered = await gateway.recover_deliveries()

        assert recovered.recovered == 1
        sent = send.await_args.args[2]
        assert sent["m.relates_to"] == {"rel_type": "m.replace", "event_id": "$placeholder"}, (
            "recovery sent a new message instead of replaying the edit"
        )
        assert send.await_args.kwargs["transaction_id"] == "tx-$cause-final"

    async def test_recovery_does_not_add_a_placeholder_after_the_answer(
        self,
        tmp_path: Path,
    ) -> None:
        """A placeholder send whose outcome was lost must not resurface later.

        If the placeholder never reached Matrix, the turn goes on to send its
        answer as a message of its own. Resending the placeholder on the next
        start would then put "Thinking..." into the room after the answer it
        was supposed to precede -- a message from a turn that finished.

        The row is left unacknowledged rather than deleted: it is the only
        record that something may already exist under that transaction ID.
        """
        outbox = FakeOutbox()
        gateway = _gateway(tmp_path, outbox)
        gateway.deps.response_hooks._apply_before_response = self._hooks()._apply_before_response
        answer = SimpleNamespace(event_id="$answer", content_sent={"body": "the answer"})

        with patch("mindroom.delivery_gateway.send_message_result", AsyncMock(return_value=None)):
            await gateway.send_text(
                SendTextRequest(
                    target=MessageTarget.resolve(_ROOM_ID, None, None, room_mode=True),
                    response_text=PROGRESS_PLACEHOLDER,
                    delivery_turn_id="$cause",
                    delivery_stage=DeliveryStage.INITIAL,
                ),
            )
        with patch("mindroom.delivery_gateway.send_message_result", AsyncMock(return_value=answer)):
            await gateway.deliver_final(self._final_request("the answer"))

        assert outbox.rows["$cause", "initial"].acknowledged_event_id is None
        assert outbox.rows["$cause", "final"].acknowledged_event_id == "$answer"

        with patch("mindroom.delivery_gateway.send_message_result", AsyncMock(return_value=answer)) as send:
            recovered = await gateway.recover_deliveries()

        assert recovered.recovered == 0, "recovery resent a placeholder the answer had already overtaken"
        send.assert_not_awaited()

    async def test_an_unacknowledged_answer_also_supersedes_the_placeholder(
        self,
        tmp_path: Path,
    ) -> None:
        """A placeholder is overtaken by an answer that exists, acknowledged or not.

        Crashing between claiming the answer and recording it leaves both rows
        unacknowledged. Recovery walks them oldest first, so a rule that only
        skips the placeholder once the answer is acknowledged would send the
        placeholder *and then* the answer -- two visible messages, in that
        order, for one turn.
        """
        outbox = FakeOutbox()
        gateway = _gateway(tmp_path, outbox)
        gateway.deps.response_hooks._apply_before_response = self._hooks()._apply_before_response
        answer = SimpleNamespace(event_id="$answer", content_sent={"body": "the answer"})

        with patch("mindroom.delivery_gateway.send_message_result", AsyncMock(return_value=None)):
            await gateway.send_text(
                SendTextRequest(
                    target=MessageTarget.resolve(_ROOM_ID, None, None, room_mode=True),
                    response_text=PROGRESS_PLACEHOLDER,
                    delivery_turn_id="$cause",
                    delivery_stage=DeliveryStage.INITIAL,
                ),
            )
            # The answer is claimed and then lost on the wire, exactly as a
            # crash after claim and before acknowledgement leaves it.
            await gateway.deliver_final(self._final_request("the answer"))

        assert outbox.rows["$cause", "initial"].acknowledged_event_id is None
        assert outbox.rows["$cause", "final"].acknowledged_event_id is None

        with patch("mindroom.delivery_gateway.send_message_result", AsyncMock(return_value=answer)) as send:
            recovered = await gateway.recover_deliveries()

        assert recovered.recovered == 1
        assert [call.args[2].get("body") for call in send.await_args_list] == ["the answer"], (
            "recovery sent the placeholder alongside the answer"
        )

    async def test_a_pass_that_could_not_send_reports_the_debt_it_left(
        self,
        tmp_path: Path,
    ) -> None:
        """A recovery pass that failed is not a recovery pass that finished.

        The caller schedules the next attempt on this, so a pass reporting
        success while leaving an answer unsent would strand it until the
        process restarted.
        """
        outbox = FakeOutbox()
        gateway = _gateway(tmp_path, outbox)
        gateway.deps.response_hooks._apply_before_response = self._hooks()._apply_before_response
        answer = SimpleNamespace(event_id="$answer", content_sent={"body": "the answer"})

        with patch("mindroom.delivery_gateway.send_message_result", AsyncMock(return_value=None)):
            await gateway.deliver_final(self._final_request("the answer"))
        assert outbox.rows["$cause", "final"].acknowledged_event_id is None

        with patch("mindroom.delivery_gateway.send_message_result", AsyncMock(return_value=None)):
            failed_pass = await gateway.recover_deliveries()

        assert failed_pass.failed == 1
        assert not failed_pass.complete

        with patch("mindroom.delivery_gateway.send_message_result", AsyncMock(return_value=answer)):
            retried = await gateway.recover_deliveries()

        assert retried.recovered == 1
        assert retried.complete
        assert outbox.rows["$cause", "final"].acknowledged_event_id == "$answer"

    async def test_a_replay_reports_what_was_sent_not_what_was_regenerated(
        self,
        tmp_path: Path,
    ) -> None:
        """A rerun turn may hold different text than the room does.

        The delivery it asks for was already made, so nothing is sent. What it
        is told came back must be the message that exists, not the text it just
        produced -- otherwise every consumer of the result records the wrong
        body under the right event ID.
        """
        outbox = FakeOutbox()
        gateway = _gateway(tmp_path, outbox)
        gateway.deps.response_hooks._apply_before_response = self._hooks()._apply_before_response
        sent = SimpleNamespace(event_id="$sent", content_sent={"msgtype": "m.text", "body": "first"})

        with patch("mindroom.delivery_gateway.send_message_result", AsyncMock(return_value=sent)):
            await gateway.deliver_final(self._final_request("first"))
            replayed = await gateway.deliver_final(self._final_request("regenerated and different"))

        assert replayed.event_id == "$sent"
        # The cache is told what the room holds, which is the first answer.
        # Handing it regenerated text would record a body the event does not
        # have, under that event's ID.
        notify = gateway.deps.resolver.deps.conversation_cache.notify_outbound_message
        assert notify.call_args.args[2]["body"] == "first", (
            "a replayed delivery told the cache regenerated text was in the room"
        )

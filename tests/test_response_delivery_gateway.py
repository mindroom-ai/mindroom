"""The outbox is what makes one turn produce at most one visible answer.

A turn's final answer is durable before it is attempted and carries a
transaction ID derived from the turn, so a resend after a crash collapses onto
the event the homeserver already accepted rather than posting a second answer.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.delivery_gateway import (
    CancelledVisibleNoteRequest,
    DeliveryGateway,
    DeliveryGatewayDeps,
    DeliveryStage,
    FinalDeliveryRequest,
    ResponseIdentity,
    SendTextRequest,
    StreamingDeliveryRequest,
)
from mindroom.handled_turns import TurnRecord, _reset_handled_turn_ledger_runtime
from mindroom.hooks.context import ResponseDraft
from mindroom.message_target import MessageTarget
from mindroom.response_delivery import RecoveryOutcome, ResponseDelivery
from mindroom.streaming import PROGRESS_PLACEHOLDER
from tests.conftest import (
    FakeOutbox,
    bind_runtime_paths,
    ignore_final_delivery_handoff,
    make_outbox_mock,
    runtime_paths_for,
    test_runtime_paths,
)
from tests.test_turn_store import _store

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
    from pathlib import Path

    from mindroom.event_journal import (
        DeliveryAcknowledgement,
        EventJournalStore,
        OutboxDelivery,
        OutboxView,
        PrincipalStore,
        TerminalTurnWrite,
    )


async def _empty_stream() -> AsyncIterator[str]:
    """Return a stream with nothing in it; these tests never run one."""
    return
    yield ""


pytestmark = pytest.mark.asyncio

_ROOM_ID = "!room:localhost"
_AGENT_USER_ID = "@agent:localhost"


def _identity(source_event_id: str = "$cause") -> ResponseIdentity:
    """Return the identity of one visible response, caused by one event."""
    return ResponseIdentity(
        response_kind="agent",
        response_envelope=SimpleNamespace(source_event_id=source_event_id),  # type: ignore[arg-type]
        correlation_id="c1",
    )


@pytest.fixture
def alice(journal_store: EventJournalStore) -> PrincipalStore:
    """Return one bound principal view."""
    return journal_store.principal("agent@alice")


def _gateway(
    tmp_path: Path,
    outbox: OutboxView | None = None,
    *,
    terminal_turn_for: Callable[[str, str], TurnRecord | None] | None = None,
    terminal_turn_committed: Callable[[str, str], Awaitable[None]] | None = None,
) -> DeliveryGateway:
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
            ),
            runtime_paths=runtime_paths_for(config),
            agent_name="agent",
            logger=MagicMock(),
            redact_message_event=AsyncMock(return_value=True),
            resolver=SimpleNamespace(
                build_message_target=MagicMock(),
                deps=SimpleNamespace(
                    conversation_reader=SimpleNamespace(
                        latest_thread_event_id=AsyncMock(return_value="$root"),
                    ),
                ),
            ),
            response_hooks=MagicMock(_apply_before_response=AsyncMock(), emit_after_response=AsyncMock()),
            outbox=outbox if outbox is not None else make_outbox_mock(),
            turn_handoff=ignore_final_delivery_handoff,
            terminal_turn_for=terminal_turn_for,
            terminal_turn_committed=terminal_turn_committed,
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
            identity=_identity(),
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

        with patch("mindroom.delivery_gateway.send_message_result", AsyncMock(return_value=edited)) as edit:
            outcome = await gateway.deliver_final(
                replace(self._final_request("the answer"), existing_event_id="$placeholder"),
            )

        assert outcome.event_id == "$placeholder"
        assert list(outbox.rows) == [("$cause", "final")]
        assert outbox.rows["$cause", "final"].edits_event_id == "$placeholder"
        assert edit.await_args.kwargs["transaction_id"] == "tx-$cause-final"
        assert edit.await_args.kwargs["operation"] == "edit_message"
        # The stored payload is the finished replace event, because recovery
        # sends the row verbatim and cannot rebuild an envelope.
        stored = outbox.rows["$cause", "final"].payload
        assert stored["m.relates_to"] == {"rel_type": "m.replace", "event_id": "$placeholder"}
        assert stored["m.new_content"]["body"] == "the answer"
        # Both layers are frozen, and the outer one is the only text a client
        # that does not understand m.replace ever renders. Recovery resends
        # this row byte for byte, so an outer body still reading "Thinking..."
        # would be permanent for those clients, not a one-attempt glitch.
        assert stored["body"] == "* the answer"

    async def test_a_regenerated_answer_cannot_go_out_under_a_frozen_edit(
        self,
        tmp_path: Path,
    ) -> None:
        """A rerun after a claimed edit must send the bytes the first attempt froze.

        The row freezes on claim, transaction ID included. If the second run
        were allowed to send its own text under that same transaction, one of
        two things happens and both are wrong: the first attempt did reach
        Matrix, so the homeserver dedupes the retry and returns the *old*
        event while every local record describes the new one -- or it did not,
        and the new text becomes visible while the durable row still says the
        old. Either way the room and the outbox disagree permanently.

        Reaching this needs a first attempt that is claimed and then fails, so
        the row is frozen but unacknowledged, which is exactly the "Matrix
        accepted it but the client never found out" window.
        """
        outbox = FakeOutbox()
        gateway = _gateway(tmp_path, outbox)
        gateway.deps.response_hooks._apply_before_response = self._hooks()._apply_before_response
        request = replace(self._final_request("first answer"), existing_event_id="$placeholder")

        with patch("mindroom.delivery_gateway.send_message_result", AsyncMock(return_value=None)):
            refused = await gateway.deliver_final(request)
        assert refused.terminal_status == "error"
        assert refused.failure_reason == "delivery_failed"

        frozen = outbox.rows["$cause", "final"].payload
        assert frozen["m.new_content"]["body"] == "first answer"
        assert frozen["body"] == "* first answer"

        delivered = SimpleNamespace(event_id="$placeholder", content_sent=dict(frozen))
        with patch("mindroom.delivery_gateway.send_message_result", AsyncMock(return_value=delivered)) as resend:
            await gateway.deliver_final(
                replace(self._final_request("regenerated answer"), existing_event_id="$placeholder"),
            )

        sent = resend.await_args.args[2]
        assert sent["m.new_content"]["body"] == "first answer", (
            "the rerun sent its own text under the frozen transaction"
        )
        # The fallback layer is frozen with the rest. A client that ignores
        # m.replace reads only this, so leaving it rebuildable would let the
        # rerun's text reach exactly the readers who cannot see it corrected.
        assert sent["body"] == "* first answer", "the rerun rebuilt the fallback body from its own text"
        assert resend.await_args.kwargs["transaction_id"] == "tx-$cause-final"
        assert outbox.rows["$cause", "final"].payload["m.new_content"]["body"] == "first answer"
        assert outbox.rows["$cause", "final"].payload["body"] == "* first answer"

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

        with patch("mindroom.delivery_gateway.send_message_result", edit):
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

        # The two edits take different primitives on purpose: a placeholder
        # edit has no delivery turn and rebuilds its envelope, while the
        # answer's edit sends the row the outbox froze.
        direct = AsyncMock(return_value=edited)
        durable = AsyncMock(return_value=edited)
        with (
            patch("mindroom.delivery_gateway.edit_message_result", direct),
            patch("mindroom.delivery_gateway.send_message_result", durable),
        ):
            # The stream ends on the placeholder, so its terminal edit is not
            # this turn's answer and must not claim the turn's final delivery.
            await terminal(AsyncMock(), _ROOM_ID, "$placeholder", {"body": PROGRESS_PLACEHOLDER}, PROGRESS_PLACEHOLDER)
            assert outbox.rows == {}, "a placeholder edit claimed the turn's final delivery"

            outcome = await gateway.deliver_final(
                replace(self._final_request("the answer"), existing_event_id="$placeholder"),
            )

        assert outcome.event_id == "$placeholder"
        assert direct.await_count == 1, "the placeholder edit did not go out"
        assert durable.await_count == 1, "the answer did not go out through the outbox"
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

        with patch("mindroom.delivery_gateway.send_message_result", AsyncMock(return_value=edited)):
            await terminal(AsyncMock(), _ROOM_ID, "$streamed", {"body": "streamed"}, "streamed")

        assert list(outbox.rows) == [("$cause", "final")]
        assert outbox.rows["$cause", "final"].acknowledged_event_id == "$streamed"

    async def test_a_streamed_terminal_edit_freezes_its_fallback_body_too(
        self,
        tmp_path: Path,
    ) -> None:
        """The row a stream freezes has to be renderable by a client that ignores edits.

        A streamed answer's last revision is an ``m.replace`` of the message the
        stream has been editing all along, and the outer ``body`` is the only
        text a client without edit support shows for it. Recovery resends this
        row verbatim rather than rebuilding it, so whatever is stored here is
        final for those clients.

        The stream hands the terminal edit its formatted content and its display
        text as two separate arguments, and they are not the same string
        whenever the answer mentions someone. Distinct values here so the
        assertions say which of the two each layer is built from.
        """
        outbox = FakeOutbox()
        gateway = _gateway(tmp_path, outbox)
        edited = SimpleNamespace(event_id="$streamed", content_sent={"msgtype": "m.text", "body": "done"})
        terminal = gateway._durable_terminal_edit(
            "$cause",
            MessageTarget.resolve(_ROOM_ID, None, None, room_mode=True),
        )
        assert terminal is not None

        with patch("mindroom.delivery_gateway.send_message_result", AsyncMock(return_value=edited)):
            await terminal(
                AsyncMock(),
                _ROOM_ID,
                "$streamed",
                {"msgtype": "m.text", "body": "done, @mindroom_code:localhost"},
                "done, @code",
            )

        stored = outbox.rows["$cause", "final"].payload
        assert stored["m.relates_to"] == {"rel_type": "m.replace", "event_id": "$streamed"}
        assert stored["m.new_content"]["body"] == "done, @mindroom_code:localhost"
        assert stored["body"] == "* done, @code"

    async def test_a_streamed_answer_with_no_placeholder_to_edit_is_still_durable(
        self,
        tmp_path: Path,
    ) -> None:
        """A stream does not always have a placeholder to edit.

        A queued forced compaction suppresses it on purpose, and its own send
        can fail. The answer is then the stream's first visible event, and it
        reaches the room through the send path rather than the edit path --
        which is exactly where a durable row is easiest to forget.
        """
        outbox = FakeOutbox()
        gateway = _gateway(tmp_path, outbox)
        sent = SimpleNamespace(event_id="$streamed", content_sent={"msgtype": "m.text", "body": "streamed"})
        terminal = gateway._durable_terminal_send(
            "$cause",
            MessageTarget.resolve(_ROOM_ID, None, None, room_mode=True),
        )
        assert terminal is not None

        with patch("mindroom.delivery_gateway.send_message_result", AsyncMock(return_value=sent)) as send:
            await terminal(AsyncMock(), _ROOM_ID, {"body": "streamed"}, "streamed")

        assert list(outbox.rows) == [("$cause", "final")]
        assert outbox.rows["$cause", "final"].acknowledged_event_id == "$streamed"
        assert outbox.rows["$cause", "final"].edits_event_id is None
        assert send.await_args.kwargs["transaction_id"] == "tx-$cause-final"

    async def test_the_stream_is_given_both_terminal_paths(
        self,
        tmp_path: Path,
    ) -> None:
        """Streaming has to be handed the durable sender, not just the editor.

        The two callbacks are what make a streamed answer recoverable, and a
        stream reaches its answer through whichever one applies. Passing only
        the editor leaves the no-placeholder shape silently direct.
        """
        gateway = _gateway(tmp_path, FakeOutbox())
        request = StreamingDeliveryRequest(
            target=MessageTarget.resolve(_ROOM_ID, None, None, room_mode=True),
            response_stream=_empty_stream(),
            identity=_identity(),
        )

        with patch("mindroom.delivery_gateway.send_streaming_response", AsyncMock()) as stream:
            await gateway.deliver_stream(request)

        assert stream.await_args.kwargs["terminal_edit"] is not None
        assert stream.await_args.kwargs["terminal_send"] is not None

    async def test_a_stream_that_only_ever_said_thinking_does_not_settle_the_turn(
        self,
        tmp_path: Path,
    ) -> None:
        """A first visible event reading "Thinking..." is not an answer.

        Recording it as the turn's FINAL would settle the delivery with a
        placeholder, and `deliver_final` -- which delivers the real answer in
        exactly this case -- would find its own row acknowledged and send
        nothing at all.
        """
        outbox = FakeOutbox()
        gateway = _gateway(tmp_path, outbox)
        sent = SimpleNamespace(event_id="$placeholder", content_sent={"body": PROGRESS_PLACEHOLDER})
        terminal = gateway._durable_terminal_send(
            "$cause",
            MessageTarget.resolve(_ROOM_ID, None, None, room_mode=True),
        )
        assert terminal is not None

        with patch("mindroom.delivery_gateway.send_message_result", AsyncMock(return_value=sent)) as send:
            await terminal(AsyncMock(), _ROOM_ID, {"body": PROGRESS_PLACEHOLDER}, PROGRESS_PLACEHOLDER)

        assert outbox.rows == {}
        send.assert_awaited_once()

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

    async def test_live_final_resolves_an_attempted_placeholder_before_the_answer(
        self,
        tmp_path: Path,
    ) -> None:
        """Live FINAL resolves an unknown placeholder before reporting completion."""
        outbox = FakeOutbox()
        gateway = _gateway(tmp_path, outbox)
        hooks = self._hooks()
        gateway.deps.response_hooks._apply_before_response = hooks._apply_before_response
        placeholder = SimpleNamespace(event_id="$placeholder", content_sent={"body": PROGRESS_PLACEHOLDER})
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
        with patch(
            "mindroom.delivery_gateway.send_message_result",
            AsyncMock(side_effect=[placeholder, answer]),
        ) as send:
            outcome = await gateway.deliver_final(self._final_request("the answer"))

        assert outcome.terminal_status == "completed"
        assert outcome.event_id == "$answer"
        assert outbox.rows["$cause", "initial"].acknowledged_event_id == "$placeholder"
        assert outbox.rows["$cause", "final"].acknowledged_event_id == "$answer"
        assert [call.args[2].get("body") for call in send.await_args_list] == [
            PROGRESS_PLACEHOLDER,
            "the answer",
        ]

    async def test_cancelled_live_final_finishes_both_stages_before_propagating(
        self,
        tmp_path: Path,
    ) -> None:
        """Cancellation cannot leave FINAL for recovery after resolving INITIAL."""
        outbox = FakeOutbox()
        terminal_committed = AsyncMock()
        gateway = _gateway(
            tmp_path,
            outbox,
            terminal_turn_committed=terminal_committed,
        )
        gateway.deps.response_hooks._apply_before_response = self._hooks()._apply_before_response
        placeholder = SimpleNamespace(event_id="$placeholder", content_sent={"body": PROGRESS_PLACEHOLDER})
        answer = SimpleNamespace(event_id="$answer", content_sent={"body": "the answer"})
        initial_retry_started = asyncio.Event()
        release_initial_retry = asyncio.Event()

        with patch("mindroom.delivery_gateway.send_message_result", AsyncMock(return_value=None)):
            await gateway.send_text(
                SendTextRequest(
                    target=MessageTarget.resolve(_ROOM_ID, None, None, room_mode=True),
                    response_text=PROGRESS_PLACEHOLDER,
                    delivery_turn_id="$cause",
                    delivery_stage=DeliveryStage.INITIAL,
                ),
            )

        async def send(
            _client: object,
            _room_id: str,
            content: dict[str, object],
            **_kwargs: object,
        ) -> SimpleNamespace:
            if content.get("body") == PROGRESS_PLACEHOLDER:
                initial_retry_started.set()
                await release_initial_retry.wait()
                return placeholder
            return answer

        with patch("mindroom.delivery_gateway.send_message_result", side_effect=send):
            delivery = asyncio.create_task(gateway.deliver_final(self._final_request("the answer")))
            await asyncio.wait_for(initial_retry_started.wait(), timeout=5)
            delivery.cancel()
            release_initial_retry.set()
            with pytest.raises(asyncio.CancelledError):
                await delivery

        assert outbox.rows["$cause", "initial"].acknowledged_event_id == "$placeholder"
        assert outbox.rows["$cause", "final"].acknowledged_event_id == "$answer"
        terminal_committed.assert_awaited_once_with("$cause", "$answer")

    async def test_live_final_ignores_its_inline_initial_result_when_another_process_wins(
        self,
        tmp_path: Path,
    ) -> None:
        """A cross-process FINAL winner cannot be reported as the placeholder."""
        outbox = FakeOutbox()
        gateway = _gateway(tmp_path, outbox)
        gateway.deps.response_hooks._apply_before_response = self._hooks()._apply_before_response
        placeholder = SimpleNamespace(event_id="$placeholder", content_sent={"body": PROGRESS_PLACEHOLDER})

        with patch("mindroom.delivery_gateway.send_message_result", AsyncMock(return_value=None)):
            await gateway.send_text(
                SendTextRequest(
                    target=MessageTarget.resolve(_ROOM_ID, None, None, room_mode=True),
                    response_text=PROGRESS_PLACEHOLDER,
                    delivery_turn_id="$cause",
                    delivery_stage=DeliveryStage.INITIAL,
                ),
            )

        real_acknowledge = outbox.acknowledge_delivery

        async def acknowledge(
            *,
            turn_id: str,
            stage: DeliveryStage,
            event_id: str,
            terminal_turn: TerminalTurnWrite | None = None,
        ) -> DeliveryAcknowledgement:
            acknowledged = await real_acknowledge(
                turn_id=turn_id,
                stage=stage,
                event_id=event_id,
                terminal_turn=terminal_turn,
            )
            if stage is DeliveryStage.INITIAL:
                await real_acknowledge(
                    turn_id="$cause",
                    stage=DeliveryStage.FINAL,
                    event_id="$other-final",
                )
            return acknowledged

        outbox.acknowledge_delivery = acknowledge  # type: ignore[method-assign]
        with patch(
            "mindroom.delivery_gateway.send_message_result",
            AsyncMock(return_value=placeholder),
        ) as send:
            outcome = await gateway.deliver_final(self._final_request("the answer"))

        assert outcome.terminal_status == "completed"
        assert outcome.event_id == "$other-final"
        assert outbox.rows["$cause", "final"].acknowledged_event_id == "$other-final"
        assert send.await_count == 1
        assert send.await_args.args[2]["body"] == PROGRESS_PLACEHOLDER

    async def test_live_final_reports_the_durable_winner_when_its_send_loses_acknowledgement(
        self,
        tmp_path: Path,
    ) -> None:
        """The requested-stage callback cannot override a competing durable winner."""
        outbox = FakeOutbox()
        gateway = _gateway(tmp_path, outbox)
        gateway.deps.response_hooks._apply_before_response = self._hooks()._apply_before_response
        real_acknowledge = outbox.acknowledge_delivery

        async def acknowledge(
            *,
            turn_id: str,
            stage: DeliveryStage,
            event_id: str,
            terminal_turn: TerminalTurnWrite | None = None,
        ) -> DeliveryAcknowledgement:
            if stage is DeliveryStage.FINAL:
                await real_acknowledge(
                    turn_id=turn_id,
                    stage=stage,
                    event_id="$winner",
                    terminal_turn=terminal_turn,
                )
            return await real_acknowledge(
                turn_id=turn_id,
                stage=stage,
                event_id=event_id,
                terminal_turn=terminal_turn,
            )

        outbox.acknowledge_delivery = acknowledge  # type: ignore[method-assign]
        sent = SimpleNamespace(event_id="$loser", content_sent={"body": "the answer"})
        with patch("mindroom.delivery_gateway.send_message_result", AsyncMock(return_value=sent)):
            outcome = await gateway.deliver_final(self._final_request("the answer"))

        assert outcome.terminal_status == "completed"
        assert outcome.event_id == "$winner"
        assert outbox.rows["$cause", "final"].acknowledged_event_id == "$winner"

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


class TestAnEndedMembershipStopsTheAnswer:
    """A turn that outlived its membership must not reach the room it left."""

    @staticmethod
    def _target() -> MessageTarget:
        """Return the room-mode target these tests deliver into."""
        return MessageTarget.resolve(_ROOM_ID, None, None, room_mode=True)

    async def test_a_turn_fenced_mid_flight_produces_no_visible_answer(
        self,
        tmp_path: Path,
    ) -> None:
        """The fence deleted this conversation; the answer must not rebuild it."""
        outbox = FakeOutbox()
        outbox.ended_membership_turn_ids.add("$cause")
        gateway = _gateway(tmp_path, outbox)
        gateway.deps.response_hooks._apply_before_response = (
            TestTurnDeliveryGoesThroughTheOutbox._hooks()._apply_before_response
        )
        delivered = SimpleNamespace(event_id="$sent", content_sent={"msgtype": "m.text", "body": "answer"})

        with patch("mindroom.delivery_gateway.send_message_result", AsyncMock(return_value=delivered)) as send:
            outcome = await gateway.deliver_final(TestTurnDeliveryGoesThroughTheOutbox._final_request("answer"))

        send.assert_not_awaited()
        assert outcome.event_id is None
        assert outcome.terminal_status == "error"
        assert outbox.rows == {}

    async def test_a_fenced_turns_final_edit_never_reaches_matrix(
        self,
        tmp_path: Path,
    ) -> None:
        """A streamed answer becomes visible by editing, so that edit is refused too."""
        outbox = FakeOutbox()
        outbox.ended_membership_turn_ids.add("$cause")
        gateway = _gateway(tmp_path, outbox)
        terminal = gateway._durable_terminal_edit("$cause", self._target())
        assert terminal is not None

        with patch("mindroom.delivery_gateway.edit_message_result", AsyncMock()) as edit:
            delivered = await terminal(AsyncMock(), _ROOM_ID, "$placeholder", {"body": "answer"}, "answer")

        edit.assert_not_awaited()
        assert delivered is None
        assert outbox.rows == {}

    async def test_a_stream_is_given_the_gate_that_stops_its_progressive_edits(
        self,
        tmp_path: Path,
    ) -> None:
        """Progressive edits never reach the outbox, so nothing else would stop them."""
        outbox = FakeOutbox()
        outbox.ended_membership_turn_ids.add("$cause")
        gateway = _gateway(tmp_path, outbox)
        request = StreamingDeliveryRequest(
            target=self._target(),
            response_stream=_empty_stream(),
            identity=_identity(),
        )

        with patch("mindroom.delivery_gateway.send_streaming_response", AsyncMock()) as stream:
            await gateway.deliver_stream(request)

        gate = stream.await_args.kwargs["transport_is_current"]
        assert gate is not None
        assert not await gate()

    async def test_a_cancellation_note_stops_once_the_membership_ended(self, tmp_path: Path) -> None:
        """The note has nowhere to go: the fence deleted what it would annotate."""
        outbox = FakeOutbox()
        outbox.ended_membership_turn_ids.add("$cause")
        gateway = _gateway(tmp_path, outbox)

        with patch("mindroom.delivery_gateway.edit_message_result", AsyncMock()) as edit:
            outcome = await gateway.deliver_cancelled_visible_note(
                CancelledVisibleNoteRequest(
                    target=self._target(),
                    event_id="$visible",
                    existing_event_is_placeholder=False,
                    cancel_source="user_stop",
                    identity=TestTurnDeliveryGoesThroughTheOutbox._final_request("x").identity,
                ),
            )

        edit.assert_not_awaited()
        assert outcome.terminal_status == "cancelled"

    async def test_a_cancellation_note_under_a_live_membership_still_lands(self, tmp_path: Path) -> None:
        """The gate must only stop the case it exists for."""
        gateway = _gateway(tmp_path, FakeOutbox())
        edited = SimpleNamespace(event_id="$visible", content_sent={"body": "stopped"})

        with patch("mindroom.delivery_gateway.edit_message_result", AsyncMock(return_value=edited)) as edit:
            outcome = await gateway.deliver_cancelled_visible_note(
                CancelledVisibleNoteRequest(
                    target=self._target(),
                    event_id="$visible",
                    existing_event_is_placeholder=False,
                    cancel_source="user_stop",
                    identity=TestTurnDeliveryGoesThroughTheOutbox._final_request("x").identity,
                ),
            )

        edit.assert_awaited()
        assert outcome.delivery_kind == "edited"

    async def test_suppression_cleanup_stops_once_the_membership_ended(self, tmp_path: Path) -> None:
        """The fence already dropped everything derived from that membership."""
        outbox = FakeOutbox()
        outbox.ended_membership_turn_ids.add("$cause")
        gateway = _gateway(tmp_path, outbox)

        failure = await gateway._redact_visible_response_event(
            room_id=_ROOM_ID,
            event_id="$visible",
            identity=TestTurnDeliveryGoesThroughTheOutbox._final_request("x").identity,
            redaction_reason="suppressed",
        )

        assert failure is None
        gateway.deps.redact_message_event.assert_not_awaited()

    async def test_a_stream_under_a_live_membership_keeps_its_gate_open(self, tmp_path: Path) -> None:
        """The ordinary case must still be allowed to stream."""
        gateway = _gateway(tmp_path, FakeOutbox())
        request = StreamingDeliveryRequest(
            target=self._target(),
            response_stream=_empty_stream(),
            identity=_identity(),
        )

        with patch("mindroom.delivery_gateway.send_streaming_response", AsyncMock()) as stream:
            await gateway.deliver_stream(request)

        assert await stream.await_args.kwargs["transport_is_current"]()


class TestTheFrozenEditSpeaksOneAnswer:
    """A stored edit must read the same to every client that renders it.

    An `m.replace` carries the answer twice: inside `m.new_content`, which a
    client that understands edits renders, and in the top-level `body`, which
    is what every other client shows. They are built from two separate inputs
    -- the replacement content and `new_text` -- so nothing structural stops
    them disagreeing, and the outbox freezes whatever they were.

    A row that disagrees with itself is the same failure the projection exists
    to remove, one layer lower: two readers of one history seeing two answers.
    """

    @staticmethod
    def _target() -> MessageTarget:
        """Return the room-mode target these tests deliver into."""
        return MessageTarget.resolve(_ROOM_ID, None, None, room_mode=True)

    async def test_the_stored_edit_says_the_same_thing_to_both_kinds_of_client(
        self,
        tmp_path: Path,
    ) -> None:
        """The fallback body is the replacement body, marked as an edit.

        `* ` is the Matrix convention for an edit's fallback, so the fallback
        agreeing means the prefix and nothing else.
        """
        outbox = FakeOutbox()
        gateway = _gateway(tmp_path, outbox)
        terminal = gateway._durable_terminal_edit("$cause", self._target())
        assert terminal is not None
        answer = "the whole answer"
        delivered = SimpleNamespace(event_id="$sent", content_sent={})

        with patch("mindroom.delivery_gateway.edit_message_result", AsyncMock(return_value=delivered)):
            await terminal(AsyncMock(), _ROOM_ID, "$placeholder", {"msgtype": "m.text", "body": answer}, answer)

        stored = outbox.rows[("$cause", DeliveryStage.FINAL.value)].payload
        replacement = stored["m.new_content"]
        assert isinstance(replacement, dict)
        assert replacement["body"] == answer
        assert stored["body"] == f"* {answer}"

    async def test_a_replacement_body_the_fallback_does_not_carry_is_refused(
        self,
        tmp_path: Path,
    ) -> None:
        """The two inputs are independent, so the disagreement is constructible.

        This is the shape a caller producing its fallback from a different
        string would store: the edit-aware client reads the answer, everyone
        else reads something the agent never said. Nothing rejects it today,
        which is what this pins -- the assertion is on the stored bytes, so it
        fails the moment the two stop being derived from one text.
        """
        outbox = FakeOutbox()
        gateway = _gateway(tmp_path, outbox)
        terminal = gateway._durable_terminal_edit("$cause", self._target())
        assert terminal is not None
        delivered = SimpleNamespace(event_id="$sent", content_sent={})

        with patch("mindroom.delivery_gateway.edit_message_result", AsyncMock(return_value=delivered)):
            await terminal(
                AsyncMock(),
                _ROOM_ID,
                "$placeholder",
                {"msgtype": "m.text", "body": "what the agent said"},
                "something else entirely",
            )

        stored = outbox.rows[("$cause", DeliveryStage.FINAL.value)].payload
        replacement = stored["m.new_content"]
        assert isinstance(replacement, dict)
        # Recorded, not endorsed: today the row keeps both strings. If a later
        # change makes the envelope derive one from the other, this fails and
        # should be replaced by an assertion that the mismatch is impossible.
        assert replacement["body"] == "what the agent said"
        assert stored["body"] == "* something else entirely"


class TestTheTerminalRecordCommitsWithItsAcknowledgement:
    """A delivered answer and the record that names it are one write.

    The acknowledgement is the durable proof that a visible answer exists and
    what its event ID is, which is exactly the fact the turn record is missing.
    Written separately, a crash between them leaves a delivered answer whose
    record does not know its response event -- and an edit of that message is
    then dropped, because there is nothing recorded to edit. Nothing else
    repairs it: outbox recovery walks unacknowledged rows and steps over this
    one, and the journal has no pending source to re-enter through.
    """

    @staticmethod
    def _final_request(text: str) -> FinalDeliveryRequest:
        """Return one final delivery for the turn caused by `$cause`."""
        return TestTurnDeliveryGoesThroughTheOutbox._final_request(text)

    async def test_a_final_acknowledgement_carries_the_bound_record(
        self,
        tmp_path: Path,
    ) -> None:
        """The record travelling with the acknowledgement names the delivered event."""
        outbox = FakeOutbox()
        pending = TurnRecord.create(["$cause"], completed=False, response_owner="agent")

        def bind(turn_id: str, event_id: str) -> TurnRecord | None:
            assert turn_id == "$cause"
            return replace(pending, response_event_id=event_id, completed=True)

        gateway = _gateway(tmp_path, outbox, terminal_turn_for=bind)
        gateway.deps.response_hooks._apply_before_response = (
            TestTurnDeliveryGoesThroughTheOutbox._hooks()._apply_before_response
        )
        delivered = SimpleNamespace(event_id="$sent", content_sent={"msgtype": "m.text", "body": "answer"})

        with patch("mindroom.delivery_gateway.send_message_result", AsyncMock(return_value=delivered)):
            await gateway.deliver_final(self._final_request("answer"))

        assert len(outbox.acknowledged_terminal_turns) == 1
        turn_id, terminal_turn = outbox.acknowledged_terminal_turns[0]
        assert turn_id == "$cause"
        assert terminal_turn is not None
        assert terminal_turn.agent_name == "agent"
        assert terminal_turn.index_event_ids == ("$cause",)
        assert terminal_turn.anchor_event_id == "$cause"
        record = json.loads(terminal_turn.record_json)
        assert record["response_event_id"] == "$sent"
        assert record["completed"] is True

    async def test_a_placeholder_acknowledgement_carries_no_record(
        self,
        tmp_path: Path,
    ) -> None:
        """A placeholder's acknowledgement must not bind the turn's terminal record.

        An INITIAL row is the "Thinking..." message, and calling the turn
        finished on the strength of it would mark the source handled while the
        model is still running -- so the real answer, when it arrives, has
        nowhere to go.
        """
        outbox = FakeOutbox()
        gateway = _gateway(
            tmp_path,
            outbox,
            terminal_turn_for=lambda _turn_id, event_id: TurnRecord.create(
                ["$cause"],
                response_event_id=event_id,
            ),
        )
        delivered = SimpleNamespace(event_id="$placeholder", content_sent={"msgtype": "m.text", "body": "..."})

        with patch("mindroom.delivery_gateway.send_message_result", AsyncMock(return_value=delivered)):
            await gateway.send_text(
                SendTextRequest(
                    target=MessageTarget.resolve(_ROOM_ID, None, None, room_mode=True),
                    response_text="...",
                    delivery_turn_id="$cause",
                    delivery_stage=DeliveryStage.INITIAL,
                ),
            )

        assert outbox.acknowledged_terminal_turns == [("$cause", None)]

    async def test_nothing_is_carried_when_there_is_no_record_to_bind(
        self,
        tmp_path: Path,
    ) -> None:
        """A turn with no record, or one that already names its answer, binds nothing.

        The acknowledgement still has to happen -- the answer is in the room
        either way -- so the delivery must not be held up by having nothing to
        write beside it.
        """
        outbox = FakeOutbox()
        gateway = _gateway(tmp_path, outbox, terminal_turn_for=lambda _turn_id, _event_id: None)
        gateway.deps.response_hooks._apply_before_response = (
            TestTurnDeliveryGoesThroughTheOutbox._hooks()._apply_before_response
        )
        delivered = SimpleNamespace(event_id="$sent", content_sent={"msgtype": "m.text", "body": "answer"})

        with patch("mindroom.delivery_gateway.send_message_result", AsyncMock(return_value=delivered)):
            outcome = await gateway.deliver_final(self._final_request("answer"))

        assert outcome.event_id == "$sent"
        assert outbox.acknowledged_terminal_turns == [("$cause", None)]


class TestARacedAcknowledgementSpeaksForTheRow:
    """Two flushes can reach one FINAL row, and only one of them binds it.

    That happens whenever a delivery is retried while an earlier attempt is
    still in flight -- a recovery pass overlapping a live turn, or two
    processes sharing one principal. Both claim, both produce an event, and
    the conditional acknowledgement lets exactly one through.

    The loser then owes two things and used to get both wrong. It must report
    the event the *row* names, because everything downstream records what
    ``flush`` returns and the later terminal settlement would otherwise upsert
    the loser's event over the winner's record. And it must publish nothing to
    the in-memory ledger, because it committed nothing to publish.

    Driven through the public ``flush`` against a real store on purpose. A fake
    outbox proves nothing here: what is under test is what the production
    return value carries out of a real conditional update.
    """

    @staticmethod
    async def _enqueue(alice: PrincipalStore) -> None:
        """Record one FINAL answer as durably owed, ready to be flushed twice."""
        transaction_id = await alice.enqueue_delivery(
            turn_id="turn-1",
            stage=DeliveryStage.FINAL,
            room_id=_ROOM_ID,
            thread_id=None,
            payload={"msgtype": "m.text", "body": "answer"},
        )
        assert transaction_id is not None

    async def test_a_send_that_lost_the_row_reports_the_winners_event(
        self,
        alice: PrincipalStore,
    ) -> None:
        """Two sends, two events, one row -- and both callers must name the stored one."""
        await self._enqueue(alice)
        losing_send_started = asyncio.Event()
        finish_losing_send = asyncio.Event()

        async def losing_send(_claimed: OutboxDelivery) -> str:
            losing_send_started.set()
            await finish_losing_send.wait()
            return "$loser"

        async def winning_send(_claimed: OutboxDelivery) -> str:
            return "$winner"

        losing = ResponseDelivery(store=alice, send=losing_send, sending_device_id="DEVICE1")
        winning = ResponseDelivery(store=alice, send=winning_send, sending_device_id="DEVICE1")

        loser = asyncio.create_task(losing.flush(turn_id="turn-1", stage=DeliveryStage.FINAL))
        await losing_send_started.wait()
        assert await winning.flush(turn_id="turn-1", stage=DeliveryStage.FINAL) == "$winner"
        finish_losing_send.set()

        assert await loser == "$winner", "the losing send reported its own event upward"
        stored = await alice.load_delivery(turn_id="turn-1", stage=DeliveryStage.FINAL)
        assert stored is not None
        assert stored.acknowledged_event_id == "$winner"

    async def test_an_adopted_answer_that_lost_the_row_reports_the_winners_event(
        self,
        alice: PrincipalStore,
    ) -> None:
        """The same rule on the branch that adopts an answer instead of sending one.

        A row attempted by a device this process is no longer logged in as
        makes the frozen transaction ID stop being proof, so both flushes read
        the room rather than send. Adoption is still an acknowledgement, and
        still has exactly one winner.
        """
        await self._enqueue(alice)
        await alice.claim_delivery(turn_id="turn-1", stage=DeliveryStage.FINAL)
        await alice.record_sending_device(turn_id="turn-1", stage=DeliveryStage.FINAL, device_id="OLD-DEVICE")

        losing_lookup_started = asyncio.Event()
        finish_losing_lookup = asyncio.Event()

        async def losing_lookup(_claimed: OutboxDelivery) -> str | None:
            losing_lookup_started.set()
            await finish_losing_lookup.wait()
            return "$loser"

        async def winning_lookup(_claimed: OutboxDelivery) -> str | None:
            return "$winner"

        async def never_sends(_claimed: OutboxDelivery) -> str:
            msg = "an adopted answer is already in the room"
            raise AssertionError(msg)

        losing = ResponseDelivery(
            store=alice,
            send=never_sends,
            sending_device_id="NEW-DEVICE",
            resolve_delivered=losing_lookup,
        )
        winning = ResponseDelivery(
            store=alice,
            send=never_sends,
            sending_device_id="NEW-DEVICE",
            resolve_delivered=winning_lookup,
        )

        loser = asyncio.create_task(losing.flush(turn_id="turn-1", stage=DeliveryStage.FINAL))
        await losing_lookup_started.wait()
        assert await winning.flush(turn_id="turn-1", stage=DeliveryStage.FINAL) == "$winner"
        finish_losing_lookup.set()

        assert await loser == "$winner", "the losing adoption reported its own event upward"
        stored = await alice.load_delivery(turn_id="turn-1", stage=DeliveryStage.FINAL)
        assert stored is not None
        assert stored.acknowledged_event_id == "$winner"

    async def test_only_the_caller_that_bound_the_row_publishes_its_record(
        self,
        alice: PrincipalStore,
    ) -> None:
        """A shared event ID is what both callers get, and it says nothing about who won.

        Both flushes here send the same frozen transaction ID from the same
        device, so Matrix deduplicates and hands each of them the *same* event
        -- exactly as a real homeserver does. Reading ownership off that
        equality told the loser it had won, and it published a terminal record
        the database had refused to take from it.
        """
        await self._enqueue(alice)
        losing_send_started = asyncio.Event()
        finish_losing_send = asyncio.Event()

        async def losing_send(_claimed: OutboxDelivery) -> str:
            losing_send_started.set()
            await finish_losing_send.wait()
            return "$deduplicated"

        async def winning_send(_claimed: OutboxDelivery) -> str:
            return "$deduplicated"

        losing_publishes: list[tuple[str, str]] = []
        winning_publishes: list[tuple[str, str]] = []

        async def losing_publish(turn_id: str, event_id: str) -> None:
            losing_publishes.append((turn_id, event_id))

        async def winning_publish(turn_id: str, event_id: str) -> None:
            winning_publishes.append((turn_id, event_id))

        losing = ResponseDelivery(
            store=alice,
            send=losing_send,
            sending_device_id="DEVICE1",
            terminal_turn_committed=losing_publish,
        )
        winning = ResponseDelivery(
            store=alice,
            send=winning_send,
            sending_device_id="DEVICE1",
            terminal_turn_committed=winning_publish,
        )

        loser = asyncio.create_task(losing.flush(turn_id="turn-1", stage=DeliveryStage.FINAL))
        await losing_send_started.wait()
        assert await winning.flush(turn_id="turn-1", stage=DeliveryStage.FINAL) == "$deduplicated"
        finish_losing_send.set()
        assert await loser == "$deduplicated"

        assert winning_publishes == [("turn-1", "$deduplicated")]
        assert losing_publishes == [], "a caller that bound nothing published a record anyway"


class TestTurnDeliverySerialization:
    """The gateway shares one turn-scoped delivery order without leaking the lock."""

    @staticmethod
    async def _enqueue(alice: PrincipalStore, stage: DeliveryStage) -> None:
        transaction_id = await alice.enqueue_delivery(
            turn_id="turn-1",
            stage=stage,
            room_id=_ROOM_ID,
            thread_id=None,
            payload={"msgtype": "m.text", "body": stage.value},
        )
        assert transaction_id is not None

    async def test_gateway_delivery_instances_share_the_turn_lock(
        self,
        tmp_path: Path,
        alice: PrincipalStore,
    ) -> None:
        """Recovery and live delivery use distinct objects but cannot reorder one turn."""
        await self._enqueue(alice, DeliveryStage.INITIAL)
        gateway = _gateway(tmp_path, alice)
        initial_reached_matrix = asyncio.Event()
        accept_initial = asyncio.Event()
        accepted_stages: list[DeliveryStage] = []

        async def send(delivery: OutboxDelivery) -> str:
            if delivery.stage is DeliveryStage.INITIAL:
                initial_reached_matrix.set()
                await accept_initial.wait()
            accepted_stages.append(delivery.stage)
            return f"${delivery.stage.value}"

        recovery_delivery = gateway._response_delivery(send, handoff=None)
        live_delivery = gateway._response_delivery(send, handoff=ignore_final_delivery_handoff)
        assert recovery_delivery is not live_delivery
        recovery = asyncio.create_task(recovery_delivery.recover())
        await initial_reached_matrix.wait()
        final = asyncio.create_task(
            live_delivery.deliver(
                turn_id="turn-1",
                stage=DeliveryStage.FINAL,
                room_id=_ROOM_ID,
                thread_id=None,
                payload={"msgtype": "m.text", "body": "final"},
            ),
        )
        completed_before_initial_acceptance, _ = await asyncio.wait({final}, timeout=0.2)

        accept_initial.set()
        await recovery
        await final

        assert final not in completed_before_initial_acceptance
        assert accepted_stages == [DeliveryStage.INITIAL, DeliveryStage.FINAL]

    async def test_cancelled_initial_cannot_be_accepted_after_a_distinct_final(
        self,
        tmp_path: Path,
        alice: PrincipalStore,
    ) -> None:
        """Local cancellation cannot make a still-live Matrix request safe to overtake."""
        await self._enqueue(alice, DeliveryStage.INITIAL)
        gateway = _gateway(tmp_path, alice)
        initial_reached_matrix = asyncio.Event()
        accept_initial = asyncio.Event()
        accepted_stages: list[DeliveryStage] = []
        remote_requests: set[asyncio.Task[str]] = set()

        async def send(delivery: OutboxDelivery) -> str:
            if delivery.stage is DeliveryStage.FINAL:
                accepted_stages.append(delivery.stage)
                return "$final"

            async def remote_initial_request() -> str:
                initial_reached_matrix.set()
                await accept_initial.wait()
                accepted_stages.append(DeliveryStage.INITIAL)
                return "$initial"

            remote = asyncio.create_task(remote_initial_request())
            remote_requests.add(remote)
            return await asyncio.shield(remote)

        initial_delivery = gateway._response_delivery(send, handoff=None)
        final_delivery = gateway._response_delivery(send, handoff=None)
        initial = asyncio.create_task(initial_delivery.flush(turn_id="turn-1", stage=DeliveryStage.INITIAL))
        await initial_reached_matrix.wait()
        initial.cancel()
        await asyncio.sleep(0)

        final = asyncio.create_task(
            final_delivery.deliver(
                turn_id="turn-1",
                stage=DeliveryStage.FINAL,
                room_id=_ROOM_ID,
                thread_id=None,
                payload={"msgtype": "m.text", "body": "final"},
            ),
        )
        completed_before_initial_acceptance, _ = await asyncio.wait({final}, timeout=0.1)
        accept_initial.set()

        with pytest.raises(asyncio.CancelledError):
            await initial
        await asyncio.gather(*remote_requests)
        await final

        assert final not in completed_before_initial_acceptance
        assert accepted_stages == [DeliveryStage.INITIAL, DeliveryStage.FINAL]

    async def test_cancelled_final_publishes_its_committed_terminal_before_propagating(
        self,
        tmp_path: Path,
        alice: PrincipalStore,
    ) -> None:
        """An acknowledged FINAL cannot leave its terminal-record publication behind."""
        await self._enqueue(alice, DeliveryStage.FINAL)
        send_started = asyncio.Event()
        accept_final = asyncio.Event()
        published: list[tuple[str, str]] = []

        async def send(_delivery: OutboxDelivery) -> str:
            send_started.set()
            await accept_final.wait()
            return "$final"

        async def publish(turn_id: str, event_id: str) -> None:
            published.append((turn_id, event_id))

        gateway = _gateway(tmp_path, alice, terminal_turn_committed=publish)
        delivery = gateway._response_delivery(send, handoff=None)
        final = asyncio.create_task(delivery.flush(turn_id="turn-1", stage=DeliveryStage.FINAL))
        await send_started.wait()
        final.cancel()
        accept_final.set()

        with pytest.raises(asyncio.CancelledError):
            await final

        stored = await alice.load_delivery(turn_id="turn-1", stage=DeliveryStage.FINAL)
        assert stored is not None
        assert stored.acknowledged_event_id == "$final"
        assert published == [("turn-1", "$final")]

    async def test_cancelled_final_finishes_after_its_enqueue_commits(
        self,
    ) -> None:
        """Cancellation cannot strand a FINAL whose durable handoff already landed."""
        outbox = FakeOutbox()
        enqueue_committed = asyncio.Event()
        return_from_enqueue = asyncio.Event()
        original_enqueue = outbox.enqueue_delivery
        sent: list[DeliveryStage] = []

        async def enqueue_then_wait(
            *,
            turn_id: str,
            stage: DeliveryStage,
            room_id: str,
            thread_id: str | None,
            payload: Mapping[str, object],
            edits_event_id: str | None = None,
            settle_source_event_ids: tuple[str, ...] = (),
        ) -> str | None:
            transaction_id = await original_enqueue(
                turn_id=turn_id,
                stage=stage,
                room_id=room_id,
                thread_id=thread_id,
                payload=payload,
                edits_event_id=edits_event_id,
                settle_source_event_ids=settle_source_event_ids,
            )
            enqueue_committed.set()
            await return_from_enqueue.wait()
            return transaction_id

        async def send(delivery: OutboxDelivery) -> str:
            sent.append(delivery.stage)
            return "$final"

        delivery = ResponseDelivery(store=outbox, send=send)
        with patch.object(outbox, "enqueue_delivery", side_effect=enqueue_then_wait):
            final = asyncio.create_task(
                delivery.deliver(
                    turn_id="turn-1",
                    stage=DeliveryStage.FINAL,
                    room_id=_ROOM_ID,
                    thread_id=None,
                    payload={"msgtype": "m.text", "body": "final"},
                ),
            )
            await enqueue_committed.wait()
            final.cancel()
            return_from_enqueue.set()

            with pytest.raises(asyncio.CancelledError):
                await final

        stored = await outbox.load_delivery(turn_id="turn-1", stage=DeliveryStage.FINAL)
        assert stored is not None
        assert stored.acknowledged_event_id == "$final"
        assert sent == [DeliveryStage.FINAL]
        assert await delivery.recover() == RecoveryOutcome(recovered=0, failed=0)

    async def test_cancelled_final_finishes_after_claiming_starts(
        self,
    ) -> None:
        """Cancellation after enqueue cannot leave recovery to overwrite a stop."""
        outbox = FakeOutbox()
        claim_started = asyncio.Event()
        finish_claim = asyncio.Event()
        original_claim = outbox.claim_delivery
        sent: list[DeliveryStage] = []

        async def claim_then_wait(*, turn_id: str, stage: DeliveryStage) -> OutboxDelivery | None:
            claim_started.set()
            await finish_claim.wait()
            return await original_claim(turn_id=turn_id, stage=stage)

        async def send(delivery: OutboxDelivery) -> str:
            sent.append(delivery.stage)
            return "$final"

        delivery = ResponseDelivery(store=outbox, send=send)
        with patch.object(outbox, "claim_delivery", side_effect=claim_then_wait):
            final = asyncio.create_task(
                delivery.deliver(
                    turn_id="turn-1",
                    stage=DeliveryStage.FINAL,
                    room_id=_ROOM_ID,
                    thread_id=None,
                    payload={"msgtype": "m.text", "body": "final"},
                ),
            )
            await claim_started.wait()
            final.cancel()
            finish_claim.set()

            with pytest.raises(asyncio.CancelledError):
                await final

        stored = await outbox.load_delivery(turn_id="turn-1", stage=DeliveryStage.FINAL)
        assert stored is not None
        assert stored.acknowledged_event_id == "$final"
        assert sent == [DeliveryStage.FINAL]
        assert await delivery.recover() == RecoveryOutcome(recovered=0, failed=0)

    async def test_terminal_callback_can_reenter_the_same_turn(
        self,
        tmp_path: Path,
        alice: PrincipalStore,
    ) -> None:
        """Publishing a committed FINAL runs after releasing its visible-delivery lock."""
        await self._enqueue(alice, DeliveryStage.FINAL)
        reentered: list[str | None] = []
        reentrant_delivery: ResponseDelivery | None = None

        async def publish_committed(_turn_id: str, _event_id: str) -> None:
            assert reentrant_delivery is not None
            reentered.append(await reentrant_delivery.flush(turn_id="turn-1", stage=DeliveryStage.FINAL))

        async def send(_delivery: OutboxDelivery) -> str:
            return "$answer"

        gateway = _gateway(tmp_path, alice, terminal_turn_committed=publish_committed)
        outer_delivery = gateway._response_delivery(send, handoff=None)
        reentrant_delivery = gateway._response_delivery(send, handoff=None)

        try:
            delivered = await asyncio.wait_for(
                outer_delivery.flush(turn_id="turn-1", stage=DeliveryStage.FINAL),
                timeout=0.5,
            )
        except TimeoutError:
            pytest.fail("terminal callback deadlocked on the turn's visible-delivery lock")

        assert delivered == "$answer"
        assert reentered == ["$answer"]


class TestTheAcknowledgedRecordOutlivesAConcurrentMutation:
    """The record an acknowledgement commits has to be written, not just published.

    Every other terminal write goes through the ledger's own lock, which
    publishes to memory and enqueues the row while holding it. That pairing is
    the whole reason concurrent mutation is safe: writes reach the database in
    the order they reached memory, so whichever lands last derived from memory
    that already held the other's fact.

    The acknowledgement commits its record in the outbox's transaction instead,
    outside that lock. Telling memory afterwards and stopping there puts the
    acknowledgement outside the pairing: a mutation that derived its record
    before being told, and reaches the database after the transaction, writes
    over the row and takes the answer's event ID with it.

    A live turn survives that because it re-asserts the record durably right
    after delivery. Recovery does not: it acknowledges and returns, nothing
    reads the outbox's event back into a record, and the turn no longer names
    the message an edit would have to edit -- for good, across restarts.
    """

    @staticmethod
    async def _restarted_record(journal_store: EventJournalStore, source_event_id: str) -> TurnRecord | None:
        """Return the record as a restart reads it, from the database alone.

        The live map is dropped first on purpose. Asking the ledger that just
        wrote would answer from memory, which is the half of the state that
        was never in doubt.
        """
        _reset_handled_turn_ledger_runtime()
        restarted = await _store(journal_store, agent_name="agent")
        return restarted.get_turn_record(source_event_id)

    @pytest.mark.ledger_loads_from_disk
    async def test_a_recovered_answer_keeps_its_event_through_a_concurrent_redaction(
        self,
        tmp_path: Path,
        journal_store: EventJournalStore,
        alice: PrincipalStore,
    ) -> None:
        """Both facts are durable, whichever of the two writers reaches the row last.

        The redaction is the one that actually happens: it arrives on a lane
        task of its own, it is not turn-backed so nothing defers it behind a
        live turn, and the recovery pass runs after every sync response.

        Asserting on the database rather than on the ledger is the point. Both
        orderings leave memory holding everything and only the stored row
        short, so a test that read the ledger would pass against the loss it
        was written to catch.
        """
        turn_store = await _store(journal_store, agent_name="agent")
        await turn_store.record_pending_turn(TurnRecord.create(["$source"], completed=False))
        gateway = _gateway(
            tmp_path,
            alice,
            terminal_turn_for=turn_store.terminal_turn_record,
            terminal_turn_committed=turn_store.publish_committed_response,
        )
        transaction_id = await alice.enqueue_delivery(
            turn_id="$source",
            stage=DeliveryStage.FINAL,
            room_id=_ROOM_ID,
            thread_id=None,
            payload={"msgtype": "m.text", "body": "the answer"},
        )
        assert transaction_id is not None
        send_started = asyncio.Event()
        finish_send = asyncio.Event()

        async def send(*_args: object, **_kwargs: object) -> SimpleNamespace:
            send_started.set()
            await finish_send.wait()
            return SimpleNamespace(event_id="$answer", content_sent={"body": "the answer"})

        with patch("mindroom.delivery_gateway.send_message_result", AsyncMock(side_effect=send)):
            recovery = asyncio.create_task(gateway.recover_deliveries())
            await send_started.wait()
            # Started while the answer is on the wire, so it derives its record
            # from a memory the acknowledgement has not published into yet.
            redaction = asyncio.create_task(turn_store.mark_source_redacted("$source"))
            finish_send.set()
            outcome = await recovery
            await redaction

        assert outcome.recovered == 1
        stored = await self._restarted_record(journal_store, "$source")
        assert stored is not None
        assert stored.redacted_source_event_ids == ("$source",), "the redaction never reached the database"
        assert stored.response_event_id == "$answer", "the delivered answer lost the event it is stored under"
        assert stored.completed, "a delivered turn came back unfinished"

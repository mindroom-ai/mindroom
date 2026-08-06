"""A bot must be able to read the room it has just spoken in.

Sync stays the authoritative source for MindRoom's own messages, but a turn
that speaks and then reads -- and any turn no room event triggered, such as a
scheduled task -- runs entirely inside the window before the echo comes back.
These pin that the send path closes that window, against a real journal store
rather than a mock of one.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import pytest

from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.delivery_gateway import (
    DeliveryGateway,
    DeliveryGatewayDeps,
    EditTextRequest,
    FinalDeliveryRequest,
    ResponseIdentity,
    SendTextRequest,
)
from mindroom.hooks.context import ResponseDraft
from mindroom.matrix.outbound_projection import OutboundProjection
from mindroom.message_target import MessageTarget
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


def _gateway(
    tmp_path: Path,
    projection: OutboundProjection,
    outbox: OutboxView | None = None,
) -> DeliveryGateway:
    """Return a delivery gateway whose only real collaborator is the projection."""
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
            outbound_projection=projection,
            outbox=outbox if outbox is not None else make_outbox_mock(),
        ),
    )


async def test_a_sent_answer_is_in_the_conversation_the_send_returns(
    tmp_path: Path,
    alice: PrincipalStore,
) -> None:
    """The read that follows a send is the one this exists for."""
    gateway = _gateway(tmp_path, OutboundProjection(store=alice, sender=_AGENT_USER_ID))
    delivered = SimpleNamespace(
        event_id="$sent",
        content_sent={"msgtype": "m.text", "body": "the answer"},
    )

    with patch("mindroom.delivery_gateway.send_message_result", AsyncMock(return_value=delivered)):
        event_id = await gateway.send_text(
            SendTextRequest(
                target=MessageTarget.resolve(_ROOM_ID, None, None, room_mode=True),
                response_text="the answer",
            ),
        )

    assert event_id == "$sent"
    page = await alice.read_conversation(room_id=_ROOM_ID, thread_id=None, limit=10)
    assert [(m.logical_event_id, m.sender, m.content["body"]) for m in page.messages] == [
        ("$sent", _AGENT_USER_ID, "the answer"),
    ]


async def test_a_send_that_matrix_refused_is_not_in_the_conversation(
    tmp_path: Path,
    alice: PrincipalStore,
) -> None:
    """Seeding a message that was never accepted would invent a reply."""
    gateway = _gateway(tmp_path, OutboundProjection(store=alice, sender=_AGENT_USER_ID))

    with patch("mindroom.delivery_gateway.send_message_result", AsyncMock(return_value=None)):
        event_id = await gateway.send_text(
            SendTextRequest(
                target=MessageTarget.resolve(_ROOM_ID, None, None, room_mode=True),
                response_text="the answer",
            ),
        )

    assert event_id is None
    page = await alice.read_conversation(room_id=_ROOM_ID, thread_id=None, limit=10)
    assert page.messages == ()


async def test_a_threaded_answer_lands_in_its_own_thread(
    tmp_path: Path,
    alice: PrincipalStore,
) -> None:
    """A seed in the wrong conversation is worse than no seed at all."""
    gateway = _gateway(tmp_path, OutboundProjection(store=alice, sender=_AGENT_USER_ID))
    delivered = SimpleNamespace(
        event_id="$sent",
        content_sent={
            "msgtype": "m.text",
            "body": "the answer",
            "m.relates_to": {"rel_type": "m.thread", "event_id": "$root"},
        },
    )

    with patch("mindroom.delivery_gateway.send_message_result", AsyncMock(return_value=delivered)):
        await gateway.send_text(
            SendTextRequest(
                target=MessageTarget.resolve(_ROOM_ID, "$root", None),
                response_text="the answer",
            ),
        )

    threaded = await alice.read_conversation(room_id=_ROOM_ID, thread_id="$root", limit=10)
    unthreaded = await alice.read_conversation(room_id=_ROOM_ID, thread_id=None, limit=10)
    assert [m.logical_event_id for m in threaded.messages] == ["$sent"]
    assert unthreaded.messages == ()


async def test_a_streamed_answer_reads_as_its_final_text(
    tmp_path: Path,
    alice: PrincipalStore,
) -> None:
    """Streaming reaches the final answer by editing, so the edit must seed too.

    Without this a turn that reads straight after a streamed answer sees the
    answer's first delivery -- often a placeholder -- rather than what the
    room actually shows.
    """
    gateway = _gateway(tmp_path, OutboundProjection(store=alice, sender=_AGENT_USER_ID))
    sent = SimpleNamespace(event_id="$sent", content_sent={"msgtype": "m.text", "body": "partial"})
    edited = SimpleNamespace(
        event_id="$edit",
        content_sent={
            "msgtype": "m.text",
            "body": "* complete",
            "m.new_content": {"msgtype": "m.text", "body": "complete"},
            "m.relates_to": {"rel_type": "m.replace", "event_id": "$sent"},
        },
    )
    target = MessageTarget.resolve(_ROOM_ID, None, None, room_mode=True)

    with patch("mindroom.delivery_gateway.send_message_result", AsyncMock(return_value=sent)):
        await gateway.send_text(SendTextRequest(target=target, response_text="partial"))
    with patch("mindroom.delivery_gateway.edit_message_result", AsyncMock(return_value=edited)):
        await gateway.edit_text(EditTextRequest(target=target, event_id="$sent", new_text="complete"))

    page = await alice.read_conversation(room_id=_ROOM_ID, thread_id=None, limit=10)
    assert [(m.logical_event_id, m.content["body"]) for m in page.messages] == [("$sent", "complete")]


async def test_a_failed_seed_does_not_fail_an_accepted_send(
    tmp_path: Path,
    alice: PrincipalStore,
) -> None:
    """Matrix has already accepted the event by the time seeding runs.

    This path carries no deterministic transaction ID, so a caller that
    retried on this failure would post a second visible message. A lost seed
    costs a reader the window before the echo; a lost send costs the user a
    duplicate answer.
    """
    projection = OutboundProjection(store=alice, sender=_AGENT_USER_ID)
    gateway = _gateway(tmp_path, projection)
    delivered = SimpleNamespace(event_id="$sent", content_sent={"msgtype": "m.text", "body": "the answer"})

    with (
        patch("mindroom.delivery_gateway.send_message_result", AsyncMock(return_value=delivered)),
        patch.object(
            type(alice),
            "seed_outbound_message",
            AsyncMock(side_effect=RuntimeError("journal unavailable")),
        ),
    ):
        event_id = await gateway.send_text(
            SendTextRequest(
                target=MessageTarget.resolve(_ROOM_ID, None, None, room_mode=True),
                response_text="the answer",
            ),
        )

    assert event_id == "$sent"


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
        alice: PrincipalStore,
    ) -> None:
        """The row must exist before the network call, keyed on the causing event.

        That ordering is the whole point: a crash after Matrix accepted the
        message leaves a row recovery can find, and the turn that caused it is
        the only name for it that survives a restart.
        """
        outbox = FakeOutbox()
        gateway = _gateway(tmp_path, OutboundProjection(store=alice, sender=_AGENT_USER_ID), outbox)
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
        alice: PrincipalStore,
    ) -> None:
        """A repeated turn must collapse onto the event the server already has.

        This is what stops a restart turning one answer into two. The ID is
        derived from the turn, so the second attempt presents the identical
        one and the homeserver discards it.
        """
        outbox = FakeOutbox()
        gateway = _gateway(tmp_path, OutboundProjection(store=alice, sender=_AGENT_USER_ID), outbox)
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
        alice: PrincipalStore,
    ) -> None:
        """An acknowledged turn replays its event ID instead of sending.

        Regenerated content could never become visible anyway -- the
        homeserver would drop it as a duplicate transaction and the durable
        result and the room would disagree forever. Not sending at all is the
        same guarantee without the wasted round trip, so the second run must
        both skip the network and return the first answer's event.
        """
        outbox = FakeOutbox()
        gateway = _gateway(tmp_path, OutboundProjection(store=alice, sender=_AGENT_USER_ID), outbox)
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
        alice: PrincipalStore,
    ) -> None:
        """Voice echoes and command replies are not turns.

        Giving them a durable row would put entries in the outbox that no
        recovery pass can resolve, and two unrelated sends whose derived IDs
        collided would collapse into one visible message.
        """
        outbox = FakeOutbox()
        gateway = _gateway(tmp_path, OutboundProjection(store=alice, sender=_AGENT_USER_ID), outbox)
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

"""The outbox is what makes one turn produce at most one visible answer.

A turn's final answer is durable before it is attempted and carries a
transaction ID derived from the turn, so a resend after a crash collapses onto
the event the homeserver already accepted rather than posting a second answer.
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
    FinalDeliveryRequest,
    ResponseIdentity,
    SendTextRequest,
)
from mindroom.hooks.context import ResponseDraft
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

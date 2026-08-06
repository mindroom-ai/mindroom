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
from mindroom.delivery_gateway import DeliveryGateway, DeliveryGatewayDeps, SendTextRequest
from mindroom.matrix.outbound_projection import OutboundProjection
from mindroom.message_target import MessageTarget
from tests.conftest import (
    bind_runtime_paths,
    make_event_cache_mock,
    runtime_paths_for,
    test_runtime_paths,
)

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.event_journal import EventJournalStore, PrincipalStore

pytestmark = pytest.mark.asyncio

_ROOM_ID = "!room:localhost"
_AGENT_USER_ID = "@agent:localhost"


@pytest.fixture
def alice(journal_store: EventJournalStore) -> PrincipalStore:
    """Return one bound principal view."""
    return journal_store.principal("agent@alice")


def _gateway(tmp_path: Path, projection: OutboundProjection) -> DeliveryGateway:
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

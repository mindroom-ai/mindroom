"""Shared builders for the sub-agent activity tests: snapshots, and a fake client's room that records frames."""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock

import nio
from nio.api import Api
from nio.crypto.sessions import OutboundGroupSession

from mindroom.delegation.activity import SubagentSnapshot
from mindroom.live_frames.subagents import _SUBAGENT_EVENT_TYPE, publish
from tests.conftest import make_matrix_client_mock

if TYPE_CHECKING:
    from collections.abc import Callable

FRAME_ROOM_ID = "!room:example.org"
_ACCOUNT = "@mindroom_lead:example.org"
_HOMESERVER = "http://homeserver.test"


def make_snapshot(**overrides: Any) -> SubagentSnapshot:  # noqa: ANN401
    """Return a running sub-agent snapshot, with any field overridden."""
    fields: dict[str, Any] = {
        "delegation_id": "d1",
        "subagent_id": "d1",
        "parent_delegation_id": None,
        "caller_agent_name": "helper",
        "root_caller_agent_name": "helper",
        "agent_name": "researcher",
        "source": "delegate",
        "task": "Find the answer",
        "thread_id": None,
        "status": "running",
        "started_at": 1000.0,
        "ended_at": None,
        "items": (),
        "items_dropped": 0,
        "result_preview": None,
    }
    fields.update(overrides)
    return SubagentSnapshot(**fields)


class FrameRoom:
    """A fake client's room, recording each frame, its size as sent, and when; replies are scripted, ok by default.

    ``arrived`` is set after each frame. An encrypted room encrypts with a real Megolm session, as nio does, so frame
    sizes are the real ones.
    """

    def __init__(
        self,
        *responses: nio.RoomSendEphemeralResponse | nio.RoomSendEphemeralError,
        encrypted: bool = False,
    ) -> None:
        self.client = make_matrix_client_mock(user_id=_ACCOUNT)
        self.client.rooms = {FRAME_ROOM_ID: SimpleNamespace(encrypted=encrypted)}
        self.client.room_encrypt = AsyncMock(side_effect=self._encrypt)
        self.client.room_send_ephemeral = AsyncMock(side_effect=self._send)
        self.sent: list[dict[str, Any]] = []
        self.sent_bytes: list[int] = []
        self.sent_at: list[float] = []
        self.arrived = asyncio.Event()
        self.responses = list(responses)
        # The frame encrypted for the send in flight.
        self.encrypted: list[dict[str, Any]] = []
        self._session = OutboundGroupSession()
        self._session.mark_as_shared()

    async def _encrypt(
        self,
        room_id: str,
        event_type: str,
        content: dict[str, Any],
        **_kwargs: object,
    ) -> dict[str, str]:
        plaintext = Api.to_json({"content": content, "type": event_type, "room_id": room_id})
        self.encrypted.append(content)
        return {
            "algorithm": "m.megolm.v1.aes-sha2",
            "sender_key": "k" * 43,
            "ciphertext": self._session.encrypt(plaintext, count=False),
            "session_id": self._session.id,
            "device_id": "TESTDEVICE",
        }

    async def _send(
        self,
        room_id: str,
        event_type: str,
        content: dict[str, Any],
        *,
        tx_id: str,
    ) -> nio.RoomSendEphemeralResponse | nio.RoomSendEphemeralError:
        frame = self.encrypted.pop() if "encrypted" in content else content
        assert (room_id, event_type) == (FRAME_ROOM_ID, _SUBAGENT_EVENT_TYPE)
        assert tx_id == f"subagents.{frame['sid']}.{frame['seq']}"
        self.sent.append(frame)
        # The body nio sends: compact JSON with ASCII escapes.
        self.sent_bytes.append(len(Api.to_json(content)))
        self.sent_at.append(time.monotonic())
        self.arrived.set()
        return self.responses.pop(0) if self.responses else nio.RoomSendEphemeralResponse(room_id)

    def publish(
        self,
        snapshot: SubagentSnapshot,
        *,
        previews: bool = True,
        client: Callable[[], nio.AsyncClient | None] | None = None,
        account: str = _ACCOUNT,
    ) -> None:
        """Queue ``snapshot`` as frames from ``account``, sent through this client unless ``client`` is given."""
        publish(
            account,
            FRAME_ROOM_ID,
            snapshot,
            previews=previews,
            client=client or (lambda: self.client),
            homeserver_url=_HOMESERVER,
        )

"""What an agent's terminal stream and workspace change stream share."""

from __future__ import annotations

from unittest.mock import create_autospec

import pytest
from fastapi import WebSocket

from mindroom.api import agent_streams


@pytest.mark.asyncio
async def test_a_close_reason_is_cut_to_whole_characters_a_close_frame_holds() -> None:
    """A close frame holds at most 123 bytes of reason, so a longer one loses its tail, never half a character."""
    websocket = create_autospec(WebSocket, instance=True, spec_set=True)

    await agent_streams.close(websocket, 1013, "é" * 100)

    websocket.close.assert_awaited_once_with(1013, "é" * 61)

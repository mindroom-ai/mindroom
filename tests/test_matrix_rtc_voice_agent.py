"""Tests for the failure-safe LiveKit voice bridge teardown."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from mindroom.matrix_rtc.voice_agent import RealtimeVoiceBridge


@pytest.mark.asyncio
async def test_aclose_disconnects_room_when_session_close_fails() -> None:
    """A failing realtime session close must not leave the SFU connection open."""
    bridge = RealtimeVoiceBridge(local_identity="@bot:example.org:BOTDEV", e2ee_enabled=False)
    session = MagicMock()
    session.aclose = AsyncMock(side_effect=RuntimeError("session close failed"))
    room = MagicMock()
    room.disconnect = AsyncMock()
    bridge._session = session
    bridge._room = room

    with pytest.raises(RuntimeError, match="session close failed"):
        await bridge.aclose()

    room.disconnect.assert_awaited_once()

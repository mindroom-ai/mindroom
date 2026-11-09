"""What an agent's terminal stream and workspace change stream share."""

from __future__ import annotations

import pytest

from mindroom.api import agent_streams


@pytest.mark.asyncio
async def test_a_close_reason_is_cut_to_whole_characters_a_close_frame_holds() -> None:
    """A close frame holds at most 123 bytes of reason, so a longer one loses its tail, never half a character."""
    closed: list[tuple[int, str]] = []

    class _Socket:
        async def close(self, code: int, reason: str) -> None:
            closed.append((code, reason))

    await agent_streams.close(_Socket(), 1013, "é" * 100)  # type: ignore[arg-type]

    assert closed == [(1013, "é" * 61)]

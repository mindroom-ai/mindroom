"""Tests for shared Matrix typing indicator leases."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from mindroom.matrix import typing as typing_module
from mindroom.matrix.typing import typing_indicator


@pytest.mark.parametrize("turn_count", [10, 100])
@pytest.mark.asyncio
async def test_concurrent_typing_indicators_share_one_matrix_lease(turn_count: int) -> None:
    """Concurrent turns for one Matrix user and room should not duplicate typing traffic."""
    client = AsyncMock()
    room_id = "!room:example.org"
    all_entered = asyncio.Event()
    release_turns = asyncio.Event()
    entered_count = 0

    async def turn() -> None:
        nonlocal entered_count
        async with typing_indicator(client, room_id):
            entered_count += 1
            if entered_count == turn_count:
                all_entered.set()
            await release_turns.wait()

    tasks = [asyncio.create_task(turn()) for _ in range(turn_count)]
    await asyncio.wait_for(all_entered.wait(), timeout=1)

    assert client.room_typing.await_count == 1
    client.room_typing.assert_awaited_once_with(room_id, True, 30_000)

    release_turns.set()
    await asyncio.gather(*tasks)

    assert client.room_typing.await_count == 2
    assert client.room_typing.await_args_list[-1].args == (room_id, False, 30_000)


@pytest.mark.asyncio
async def test_cancelled_initial_typing_request_releases_shared_lease() -> None:
    """Cancellation during the initial Matrix call should not retain failed lease state."""
    client = AsyncMock()
    room_id = "!room:example.org"
    request_started = asyncio.Event()

    async def block_initial_request(_room_id: str, typing: bool, _timeout_ms: int) -> None:
        if typing:
            request_started.set()
            await asyncio.Future()

    client.room_typing.side_effect = block_initial_request

    async def run_turn() -> None:
        async with typing_indicator(client, room_id):
            pass

    task = asyncio.create_task(run_turn())
    await asyncio.wait_for(request_started.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert not typing_module._ACTIVE_TYPING

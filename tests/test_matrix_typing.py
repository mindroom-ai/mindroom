"""Tests for shared Matrix typing indicator leases."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio

from mindroom.matrix import typing as typing_module
from mindroom.matrix.typing import typing_indicator


@pytest_asyncio.fixture(autouse=True)
async def _clean_typing_states() -> None:
    """Keep failed tests from leaking refresh tasks and client references."""
    assert not typing_module._ACTIVE_TYPING
    yield
    states = tuple(typing_module._ACTIVE_TYPING.values())
    typing_module._ACTIVE_TYPING.clear()
    for state in states:
        if state.refresh_task is not None:
            state.refresh_task.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await state.refresh_task


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
    assert not typing_module._ACTIVE_TYPING


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


@pytest.mark.asyncio
async def test_new_typing_lease_waits_for_prior_stop_request() -> None:
    """A new lease must not be silenced by the previous lease's final stop."""
    client = AsyncMock()
    room_id = "!room:example.org"
    calls: list[bool] = []
    first_entered = asyncio.Event()
    release_first = asyncio.Event()
    stop_started = asyncio.Event()
    release_stop = asyncio.Event()
    second_entered = asyncio.Event()
    release_second = asyncio.Event()

    async def room_typing(_room_id: str, typing: bool, _timeout_ms: int) -> None:
        calls.append(typing)
        if not typing and not stop_started.is_set():
            stop_started.set()
            await release_stop.wait()

    client.room_typing.side_effect = room_typing

    async def first_turn() -> None:
        async with typing_indicator(client, room_id):
            first_entered.set()
            await release_first.wait()

    async def second_turn() -> None:
        async with typing_indicator(client, room_id):
            second_entered.set()
            await release_second.wait()

    first_task = asyncio.create_task(first_turn())
    await asyncio.wait_for(first_entered.wait(), timeout=1)
    release_first.set()
    await asyncio.wait_for(stop_started.wait(), timeout=1)

    second_task = asyncio.create_task(second_turn())
    await asyncio.sleep(0)
    assert not second_entered.is_set()

    release_stop.set()
    await asyncio.wait_for(second_entered.wait(), timeout=1)
    release_second.set()
    await asyncio.gather(first_task, second_task)

    assert calls == [True, False, True, False]


@pytest.mark.asyncio
async def test_shared_typing_lease_uses_longest_requested_timeout() -> None:
    """Mixed callers should explicitly converge on the longest active lease timeout."""
    client = AsyncMock()
    room_id = "!room:example.org"
    both_entered = asyncio.Event()
    release_turns = asyncio.Event()
    entered_count = 0

    async def turn(timeout_seconds: int) -> None:
        nonlocal entered_count
        async with typing_indicator(client, room_id, timeout_seconds=timeout_seconds):
            entered_count += 1
            if entered_count == 2:
                both_entered.set()
            await release_turns.wait()

    tasks = [
        asyncio.create_task(turn(10)),
        asyncio.create_task(turn(60)),
    ]
    await asyncio.wait_for(both_entered.wait(), timeout=1)

    state = next(iter(typing_module._ACTIVE_TYPING.values()))
    assert state.timeout_seconds == 60

    release_turns.set()
    await asyncio.gather(*tasks)
    assert not typing_module._ACTIVE_TYPING


@pytest.mark.asyncio
async def test_typing_start_failure_does_not_fail_response_turn() -> None:
    """Typing is best-effort and must never prevent the turn body from running."""
    client = AsyncMock()
    client.room_typing.side_effect = RuntimeError("Matrix unavailable")
    body_entered = False

    async with typing_indicator(client, "!room:example.org"):
        body_entered = True

    assert body_entered is True
    assert client.room_typing.await_count == 2
    assert not typing_module._ACTIVE_TYPING


@pytest.mark.asyncio
async def test_typing_refresh_failure_is_logged_and_consumed() -> None:
    """A dead refresh task should not fail the active turn or escape unobserved."""
    client = AsyncMock()
    refresh_failed = asyncio.Event()
    call_count = 0

    async def room_typing(_room_id: str, typing: bool, _timeout_ms: int) -> None:
        nonlocal call_count
        call_count += 1
        if typing and call_count == 2:
            refresh_failed.set()
            message = "refresh failed"
            raise RuntimeError(message)

    client.room_typing.side_effect = room_typing

    async with typing_indicator(client, "!room:example.org", timeout_seconds=0):
        await asyncio.wait_for(refresh_failed.wait(), timeout=1)

    assert call_count == 3
    assert not typing_module._ACTIVE_TYPING

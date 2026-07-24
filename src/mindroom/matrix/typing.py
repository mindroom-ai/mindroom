"""Typing indicator management for Matrix agents."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from typing import TYPE_CHECKING

import nio

from mindroom.logging_config import get_logger

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

logger = get_logger(__name__)


@dataclass
class _TypingState:
    """One shared typing lease for a Matrix user in one room."""

    references: int
    started: asyncio.Future[None]
    refresh_task: asyncio.Task[None]


_ACTIVE_TYPING: dict[tuple[nio.AsyncClient, str], _TypingState] = {}


async def _set_typing(
    client: nio.AsyncClient,
    room_id: str,
    typing: bool = True,
    timeout_seconds: int = 30,
) -> None:
    """Set typing status for a user in a room.

    Args:
        client: Matrix client instance
        room_id: Room to show typing indicator in
        typing: Whether to show or hide typing indicator
        timeout_seconds: How long the typing indicator should last (in seconds)

    """
    timeout_ms = timeout_seconds * 1000
    response = await client.room_typing(room_id, typing, timeout_ms)
    if isinstance(response, nio.RoomTypingError):
        logger.warning(
            "Failed to set typing status",
            room_id=room_id,
            typing=typing,
            error=response.message,
        )
    else:
        logger.debug("Set typing status", room_id=room_id, typing=typing)


async def _refresh_typing(
    client: nio.AsyncClient,
    room_id: str,
    *,
    timeout_seconds: int,
    started: asyncio.Future[None],
) -> None:
    """Start and refresh one shared Matrix typing indicator."""
    try:
        await _set_typing(client, room_id, True, timeout_seconds)
    except asyncio.CancelledError:
        if not started.done():
            started.cancel()
        raise
    except Exception as exc:
        if not started.done():
            started.set_exception(exc)
        raise
    if not started.done():
        started.set_result(None)
    refresh_interval = min(timeout_seconds / 2, 15)
    while True:
        await asyncio.sleep(refresh_interval)
        await _set_typing(client, room_id, True, timeout_seconds)


def _acquire_typing_state(
    client: nio.AsyncClient,
    room_id: str,
    *,
    timeout_seconds: int,
) -> tuple[tuple[nio.AsyncClient, str], _TypingState]:
    """Acquire one process-local typing lease without yielding the event loop."""
    key = (client, room_id)
    state = _ACTIVE_TYPING.get(key)
    if state is not None:
        state.references += 1
        return key, state

    started = asyncio.get_running_loop().create_future()
    refresh_task = asyncio.create_task(
        _refresh_typing(
            client,
            room_id,
            timeout_seconds=timeout_seconds,
            started=started,
        ),
    )
    state = _TypingState(references=1, started=started, refresh_task=refresh_task)
    _ACTIVE_TYPING[key] = state
    return key, state


async def _release_typing_state(
    key: tuple[nio.AsyncClient, str],
    state: _TypingState,
) -> None:
    """Release a typing lease and stop Matrix typing after the final user."""
    active_state = _ACTIVE_TYPING.get(key)
    if active_state is not state:
        return
    state.references -= 1
    if state.references > 0:
        return
    del _ACTIVE_TYPING[key]
    state.refresh_task.cancel()
    with suppress(asyncio.CancelledError, Exception):
        await state.refresh_task
    client, room_id = key
    await _set_typing(client, room_id, False)


@asynccontextmanager
async def typing_indicator(
    client: nio.AsyncClient,
    room_id: str,
    timeout_seconds: int = 30,
) -> AsyncGenerator[None, None]:
    """Context manager for showing typing indicator while processing.

    Usage:
        async with typing_indicator(client, room_id):
            # Do work here - typing indicator shown
            response = await generate_response()
        # Typing indicator automatically stopped

    Args:
        client: Matrix client instance
        room_id: Room to show typing indicator in
        timeout_seconds: How long each typing notification lasts

    """
    key, state = _acquire_typing_state(client, room_id, timeout_seconds=timeout_seconds)
    try:
        await asyncio.shield(state.started)
        yield
    finally:
        await _release_typing_state(key, state)

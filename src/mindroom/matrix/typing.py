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
    timeout_seconds: int
    started: asyncio.Future[None]
    refresh_task: asyncio.Task[None] | None = None
    stopping: asyncio.Future[None] | None = None


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
    state: _TypingState,
) -> None:
    """Start and refresh one shared Matrix typing indicator."""
    try:
        await _set_typing(client, room_id, True, state.timeout_seconds)
    except asyncio.CancelledError:
        if not state.started.done():
            state.started.cancel()
        raise
    except Exception:
        logger.warning("Failed to start typing indicator", room_id=room_id, exc_info=True)
        if not state.started.done():
            state.started.set_result(None)
        return
    if not state.started.done():
        state.started.set_result(None)
    while True:
        refresh_interval = min(state.timeout_seconds / 2, 15)
        await asyncio.sleep(refresh_interval)
        try:
            await _set_typing(client, room_id, True, state.timeout_seconds)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("Failed to refresh typing indicator", room_id=room_id, exc_info=True)
            return


async def _acquire_typing_state(
    client: nio.AsyncClient,
    room_id: str,
    *,
    timeout_seconds: int,
) -> tuple[tuple[nio.AsyncClient, str], _TypingState]:
    """Acquire one process-local typing lease after any prior stop completes."""
    key = (client, room_id)
    while (state := _ACTIVE_TYPING.get(key)) is not None:
        if state.stopping is not None:
            await asyncio.shield(state.stopping)
            continue
        state.references += 1
        state.timeout_seconds = max(state.timeout_seconds, timeout_seconds)
        return key, state

    started = asyncio.get_running_loop().create_future()
    state = _TypingState(
        references=1,
        timeout_seconds=timeout_seconds,
        started=started,
    )
    state.refresh_task = asyncio.create_task(
        _refresh_typing(
            client,
            room_id,
            state=state,
        ),
    )
    _ACTIVE_TYPING[key] = state
    return key, state


async def _release_typing_state(
    key: tuple[nio.AsyncClient, str],
    state: _TypingState,
) -> None:
    """Release a typing lease and stop Matrix typing after the final user."""
    state.references -= 1
    if state.references > 0:
        return
    state.stopping = asyncio.get_running_loop().create_future()
    refresh_task = state.refresh_task
    assert refresh_task is not None
    refresh_task.cancel()
    with suppress(asyncio.CancelledError):
        await refresh_task
    client, room_id = key
    try:
        try:
            await _set_typing(client, room_id, False)
        except Exception:
            logger.warning("Failed to stop typing indicator", room_id=room_id, exc_info=True)
    finally:
        if _ACTIVE_TYPING.get(key) is state:
            del _ACTIVE_TYPING[key]
        state.stopping.set_result(None)


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
    key, state = await _acquire_typing_state(client, room_id, timeout_seconds=timeout_seconds)
    try:
        await asyncio.shield(state.started)
        yield
    finally:
        await _release_typing_state(key, state)

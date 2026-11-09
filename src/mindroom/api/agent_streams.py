"""What an agent's terminal stream and workspace change stream share: rechecking the caller, running parts, closing."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from typing import TYPE_CHECKING

from fastapi import HTTPException, WebSocket, WebSocketDisconnect, status

from mindroom.plugin_http import stream_room_reader

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Coroutine

    from mindroom.plugin_http import AgentWorkspace, ClientUser

# As often as the Computer's stream rechecks its viewer, and each check as long.
RECHECK_SECONDS = 15.0


async def rechecked(
    websocket: WebSocket,
    user: ClientUser,
    room_id: str,
    agent_user_id: str,
) -> AsyncIterator[AgentWorkspace]:
    """Yield the agent as ``user`` may use it in the room now, every ``RECHECK_SECONDS``; a slow check answers 503."""
    while True:
        await asyncio.sleep(RECHECK_SECONDS)
        try:
            async with asyncio.timeout(RECHECK_SECONDS):
                current = await (await stream_room_reader(websocket, user, room_id)).agent(agent_user_id)
        except TimeoutError:
            raise HTTPException(503, "Agent access is unavailable; try again.") from None
        yield current


async def until_first(*parts: Coroutine[object, object, None]) -> None:
    """Run ``parts`` until one returns or raises, raising what it raised, then cancel the others."""
    tasks = [asyncio.create_task(part) for part in parts]
    try:
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            task.result()
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def close(websocket: WebSocket, code: int, reason: str) -> None:
    """Close the stream unless the client is gone already, with as much of ``reason`` as a close frame holds."""
    with suppress(RuntimeError, WebSocketDisconnect):
        await websocket.close(code, reason.encode()[:123].decode(errors="ignore"))


async def close_answering(websocket: WebSocket, status_code: int, detail: str) -> None:
    """Close the stream for an HTTP answer: 1008 for 403 and 404, which trying again won't change, else 1013."""
    refused = status_code in (status.HTTP_403_FORBIDDEN, status.HTTP_404_NOT_FOUND)
    await close(websocket, status.WS_1008_POLICY_VIOLATION if refused else status.WS_1013_TRY_AGAIN_LATER, detail)

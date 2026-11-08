"""The example plugin's routes, which serve members of the room they name."""

from typing import Annotated

from fastapi import APIRouter, Depends, WebSocket

from mindroom.plugin_http import (
    ClientUser,
    RoomMessenger,
    RoomReader,
    client_stream,
    require_room_member,
    require_room_messenger,
    require_room_reader,
)

router = APIRouter()


@router.get("/rooms/{room_id}/hello")
async def hello(user: Annotated[ClientUser, Depends(require_room_member)]) -> dict[str, str]:
    """Greet the signed-in member of the room."""
    return {"hello": user.user_id}


@router.get("/rooms/{room_id}/about")
async def about(room: Annotated[RoomReader, Depends(require_room_reader)]) -> dict[str, object]:
    """Tell the member whether the room is direct, whether they are its admin, its history visibility and its agents."""
    power = await room.power()
    history = await room.history()
    return {
        "direct": await room.is_direct(),
        "admin": power.is_admin(room.user.user_id),
        "history": history.visibility,
        "agents": [agent.name for agent in await room.agents()],
    }


@router.post("/rooms/{room_id}/notice")
async def notice(messenger: Annotated[RoomMessenger, Depends(require_room_messenger)]) -> dict[str, str]:
    """Post a notice to the room as the router."""
    return {"event_id": await messenger.notice(f"{messenger.user.user_id} says hello")}


@router.post("/rooms/{room_id}/relay")
async def relay(mention: str, messenger: Annotated[RoomMessenger, Depends(require_room_messenger)]) -> dict[str, str]:
    """Relay a greeting to the agent or team ``mention`` names, as the router for the caller."""
    return {"event_id": await messenger.relay("hello", mentions=[mention])}


@router.websocket("/rooms/{room_id}/stream")
async def stream(websocket: WebSocket, room_id: str) -> None:
    """Greet the signed-in member over a websocket, then echo what they send while they stay signed in."""
    async with client_stream(websocket, room_id) as user:
        await websocket.send_text(f"hello {user.user_id}")
        async for text in websocket.iter_text():
            await websocket.send_text(text)

"""The example plugin's routes, which greet a member of the room they name and tell whether it is direct."""

from typing import Annotated

from fastapi import APIRouter, Depends

from mindroom.plugin_http import ClientUser, RoomReader, require_room_member, require_room_reader

router = APIRouter()


@router.get("/rooms/{room_id}/hello")
async def hello(user: Annotated[ClientUser, Depends(require_room_member)]) -> dict[str, str]:
    """Greet the signed-in member of the room."""
    return {"hello": user.user_id}


@router.get("/rooms/{room_id}/direct")
async def direct(room: Annotated[RoomReader, Depends(require_room_reader)]) -> dict[str, bool]:
    """Tell the signed-in member whether MindRoom's agents treat the room as a direct message."""
    return {"direct": await room.is_direct()}

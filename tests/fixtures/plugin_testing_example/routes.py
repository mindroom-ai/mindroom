"""The example plugin's route, which greets a member of the room it names."""

from typing import Annotated

from fastapi import APIRouter, Depends

from mindroom.plugin_http import ClientUser, require_room_member

router = APIRouter()


@router.get("/rooms/{room_id}/hello")
async def hello(user: Annotated[ClientUser, Depends(require_room_member)]) -> dict[str, str]:
    """Greet the signed-in member of the room."""
    return {"hello": user.user_id}

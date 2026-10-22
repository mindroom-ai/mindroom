"""Each entity's MindRoom server, published as room state so MindRoom Chat can open it without a typed URL."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import nio

from mindroom.logging_config import get_logger
from mindroom.matrix.event_types import MINDROOM_SERVER_EVENT_TYPE
from mindroom.matrix_openid import openid_audience
from mindroom.orchestration.computer_runtime import worker_computer_provider

if TYPE_CHECKING:
    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths

logger = get_logger(__name__)

# nio retries a homeserver it cannot reach without limit, and the join that triggers a write waits for it.
_PUBLISH_TIMEOUT_SECONDS = 10


def server_state_content(
    entity_name: str,
    config: Config,
    runtime_paths: RuntimePaths,
    *,
    api_enabled: bool,
) -> dict[str, object]:
    """Return the origin Chat must open, which is the OpenID audience, and whether the entity has a worker computer.

    Without a usable origin, or with the API off (`mindroom run --no-api`), the content is empty, which also clears
    what an earlier process published.
    """
    origin = openid_audience(runtime_paths)
    if origin is None or not api_enabled:
        return {}
    return {"api_origin": origin, "computer": worker_computer_provider(entity_name, config, runtime_paths) is not None}


async def publish_server_state(client: nio.AsyncClient, room_id: str, content: dict[str, object]) -> None:
    """Write the entity's server state in one room unless the room already holds it.

    Room state is not encrypted, so every room member and the homeserver can read the origin.
    A failure only leaves Chat without the address, so it is logged and never stops or long delays the join.
    """
    user_id = client.user_id
    try:
        async with asyncio.timeout(_PUBLISH_TIMEOUT_SECONDS):
            current = await client.room_get_state_event(room_id, MINDROOM_SERVER_EVENT_TYPE, user_id)
            published = current.content if isinstance(current, nio.RoomGetStateEventResponse) else None
            absent = isinstance(current, nio.RoomGetStateEventError) and current.status_code == "M_NOT_FOUND"
            # Only a confirmed state skips the write; after a failed read the write still clears a stale address.
            if published == content or (absent and not content):
                return
            response = await client.room_put_state(room_id, MINDROOM_SERVER_EVENT_TYPE, content, state_key=user_id)
    except Exception:
        logger.warning("Publishing the MindRoom server state failed", room_id=room_id, exc_info=True)
        return
    if isinstance(response, nio.RoomPutStateResponse):
        return
    if isinstance(response, nio.RoomPutStateError) and response.status_code == "M_FORBIDDEN":
        # Rooms MindRoom does not manage, such as rooms people create and invite agents to, keep the default state
        # power level for this type, so an entity without state rights is refused on every join and restart.
        logger.debug("MindRoom server state is not permitted in this room", room_id=room_id)
        return
    logger.warning("MindRoom server state was refused", room_id=room_id, response=str(response))

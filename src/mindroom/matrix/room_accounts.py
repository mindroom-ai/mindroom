"""Publish the router's room state that tells clients which MindRoom accounts and router are in a room."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING
from weakref import WeakValueDictionary

import nio

from mindroom.entity_resolution import prepared_entity_names_by_user_id
from mindroom.logging_config import get_logger
from mindroom.matrix.client_room_admin import get_room_members
from mindroom.matrix.event_types import MINDROOM_ACCOUNTS_EVENT_TYPE, ROUTER_BINDING_EVENT_TYPE

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths

logger = get_logger(__name__)
_LOCKS: WeakValueDictionary[tuple[Path, str], asyncio.Lock] = WeakValueDictionary()


async def reconcile_room_accounts(
    client: nio.AsyncClient,
    config: Config,
    runtime_paths: RuntimePaths,
    room_id: str,
) -> bool:
    """Write the router's list of the room's joined MindRoom accounts when it changed and return whether it is current.

    Failures leave it to the next trigger.

    The state key is the router's user ID, which Matrix lets only the router write.
    """
    try:
        # The deadline covers the lock wait too, so a sync frame queued behind other triggers stalls at most 10 s.
        async with asyncio.timeout(10):
            # Serialize each read and write so a slower trigger cannot replace a newer list with an older one.
            async with _LOCKS.setdefault((runtime_paths.storage_root, room_id), asyncio.Lock()):
                members = await get_room_members(client, room_id)
                if members is None:
                    return False
                accounts = members & prepared_entity_names_by_user_id(config, runtime_paths).keys()
                content = {"accounts": sorted(accounts)}
                current = await client.room_get_state_event(room_id, MINDROOM_ACCOUNTS_EVENT_TYPE, client.user_id)
                if isinstance(current, nio.RoomGetStateEventResponse) and current.content == content:
                    return True
                response = await client.room_put_state(
                    room_id,
                    MINDROOM_ACCOUNTS_EVENT_TYPE,
                    content,
                    state_key=client.user_id,
                )
                if not isinstance(response, nio.RoomPutStateResponse):
                    # A room whose power levels keep the router from sending this state refuses it on every trigger.
                    forbidden = isinstance(response, nio.RoomPutStateError) and response.status_code == "M_FORBIDDEN"
                    log = logger.debug if forbidden else logger.warning
                    log("room_accounts_publish_failed", room_id=room_id, error=str(response))
                    return False
                return True
    except Exception:
        logger.warning("room_accounts_publish_failed", room_id=room_id, exc_info=True)
        return False


async def reconcile_room_router_binding(
    client: nio.AsyncClient,
    config: Config,  # noqa: ARG001 - shares the router room-state reconciler signature
    runtime_paths: RuntimePaths,  # noqa: ARG001
    room_id: str,
) -> bool:
    """Name this router as the room's router where no valid binding exists, and return whether nothing is left to do.

    A binding naming another account stays, so two privileged routers cannot take turns overwriting it; a stale one
    only leaves voice echoes as standalone rows until a room admin clears it. A room whose power levels refuse the write
    is done too, since retrying cannot help. Other failures leave it to the next trigger.
    """
    try:
        async with asyncio.timeout(10):
            current = await client.room_get_state_event(room_id, ROUTER_BINDING_EVENT_TYPE, state_key="")
            if isinstance(current, nio.RoomGetStateEventResponse):
                bound = current.content.get("user_id")
                if isinstance(bound, str) and bound.startswith("@") and ":" in bound:
                    if bound != client.user_id:
                        logger.debug("room_router_binding_held_by_other", room_id=room_id, router_user_id=bound)
                    return True
            elif not (isinstance(current, nio.RoomGetStateEventError) and current.status_code == "M_NOT_FOUND"):
                # An unreadable binding may name another router, so only absent or malformed state is claimed.
                logger.warning("room_router_binding_publish_failed", room_id=room_id, error=str(current))
                return False
            response = await client.room_put_state(
                room_id,
                ROUTER_BINDING_EVENT_TYPE,
                {"user_id": client.user_id},
                state_key="",
            )
            if not isinstance(response, nio.RoomPutStateResponse):
                forbidden = isinstance(response, nio.RoomPutStateError) and response.status_code == "M_FORBIDDEN"
                log = logger.debug if forbidden else logger.warning
                log("room_router_binding_publish_failed", room_id=room_id, error=str(response))
                return forbidden
            return True
    except Exception:
        logger.warning("room_router_binding_publish_failed", room_id=room_id, exc_info=True)
        return False

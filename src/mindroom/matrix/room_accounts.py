"""Publish the MindRoom accounts joined to a room as router state, so clients recognize agents by any user name."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING
from weakref import WeakValueDictionary

import nio

from mindroom.entity_resolution import prepared_entity_names_by_user_id
from mindroom.logging_config import get_logger
from mindroom.matrix.client_room_admin import get_room_members, may_send_state
from mindroom.matrix.event_types import MINDROOM_ACCOUNTS_EVENT_TYPE

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
                if not await may_send_state(client, room_id, MINDROOM_ACCOUNTS_EVENT_TYPE):
                    logger.debug("room_accounts_publish_skipped", room_id=room_id, reason="insufficient_power_level")
                    return False
                response = await client.room_put_state(
                    room_id,
                    MINDROOM_ACCOUNTS_EVENT_TYPE,
                    content,
                    state_key=client.user_id,
                )
                if not isinstance(response, nio.RoomPutStateResponse):
                    logger.warning("room_accounts_publish_failed", room_id=room_id, error=str(response))
                    return False
                return True
    except Exception:
        logger.warning("room_accounts_publish_failed", room_id=room_id, exc_info=True)
        return False

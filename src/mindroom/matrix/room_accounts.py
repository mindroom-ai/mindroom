"""Publish the room state that tells clients which MindRoom accounts are in a room and which one stewards it."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

import nio

from mindroom.entity_resolution import prepared_entity_names_by_user_id
from mindroom.logging_config import get_logger
from mindroom.matrix.client_room_admin import get_room_members
from mindroom.matrix.event_types import MINDROOM_ACCOUNTS_EVENT_TYPE, ROUTER_BINDING_EVENT_TYPE

if TYPE_CHECKING:
    from collections.abc import Callable

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths

logger = get_logger(__name__)


async def reconcile_room_accounts(
    client: nio.AsyncClient,
    config: Config,
    runtime_paths: RuntimePaths,
    room_id: str,
    *,
    may_write: Callable[[], bool] | None = None,
    failure_level: Callable[[bool], int] = lambda _: logging.WARNING,
) -> bool:
    """Write the client's list of the room's joined MindRoom accounts when it changed and return whether it is current.

    A write refused for lack of power does not need a retry until the room changes.

    The state key is the writing account's user ID, the room's steward, which Matrix lets only that account write.
    """
    phase = "joined_members"
    try:
        async with asyncio.timeout(10):
            members = await get_room_members(client, room_id, failure_level=failure_level)
            if members is None:
                return False
            accounts = members & prepared_entity_names_by_user_id(config, runtime_paths).keys()
            content = {"accounts": sorted(accounts)}
            phase = "state_get"
            current = await client.room_get_state_event(room_id, MINDROOM_ACCOUNTS_EVENT_TYPE, client.user_id)
            if isinstance(current, nio.RoomGetStateEventResponse) and current.content == content:
                return True
            if may_write is not None and not may_write():
                return True
            phase = "state_put"
            response = await client.room_put_state(
                room_id,
                MINDROOM_ACCOUNTS_EVENT_TYPE,
                content,
                state_key=client.user_id,
            )
            if not isinstance(response, nio.RoomPutStateResponse):
                # A room whose power levels keep this account from sending this state refuses it on every trigger.
                forbidden = isinstance(response, nio.RoomPutStateError) and response.status_code == "M_FORBIDDEN"
                level = logging.DEBUG if forbidden else failure_level(False)
                logger.log(level, "room_accounts_publish_failed", room_id=room_id, error=str(response))
                return forbidden
            return True
    except Exception as exc:
        timed_out = isinstance(exc, TimeoutError)
        detail = {"error": "TimeoutError", "phase": phase} if timed_out else {"exc_info": True}
        logger.log(failure_level(timed_out), "room_accounts_publish_failed", room_id=room_id, **detail)
        return False


async def reconcile_room_router_binding(
    client: nio.AsyncClient,
    room_id: str,
    *,
    may_write: Callable[[], bool] | None = None,
    failure_level: Callable[[bool], int] = lambda _: logging.WARNING,
) -> bool:
    """Name this account, the room's steward, in the `io.mindroom.router` binding where no valid one exists.

    Returns whether nothing is left to do. The binding keeps the name today's clients read: the privileged MindRoom
    account whose room state they trust. A binding naming another account stays, so two privileged stewards cannot
    take turns overwriting it; a stale one only leaves voice echoes as standalone rows until a room admin clears it.
    A room that forbids the read or write is done too. The room-state worker retries transient failures.
    """
    phase = "state_get"
    try:
        async with asyncio.timeout(10):
            current = await client.room_get_state_event(room_id, ROUTER_BINDING_EVENT_TYPE, state_key="")
            if isinstance(current, nio.RoomGetStateEventResponse):
                bound = current.content.get("user_id")
                localpart, _, server = bound[1:].partition(":") if isinstance(bound, str) else ("", "", "")
                if isinstance(bound, str) and bound.startswith("@") and localpart and server:
                    if bound != client.user_id:
                        logger.debug("room_router_binding_held_by_other", room_id=room_id, router_user_id=bound)
                    return True
            elif not (isinstance(current, nio.RoomGetStateEventError) and current.status_code == "M_NOT_FOUND"):
                # An unreadable binding may name another router, so only absent or malformed state is claimed.
                forbidden = isinstance(current, nio.RoomGetStateEventError) and current.status_code == "M_FORBIDDEN"
                level = logging.DEBUG if forbidden else failure_level(False)
                logger.log(level, "room_router_binding_publish_failed", room_id=room_id, error=str(current))
                return forbidden
            if may_write is not None and not may_write():
                return True
            phase = "state_put"
            response = await client.room_put_state(
                room_id,
                ROUTER_BINDING_EVENT_TYPE,
                {"user_id": client.user_id},
                state_key="",
            )
            if not isinstance(response, nio.RoomPutStateResponse):
                forbidden = isinstance(response, nio.RoomPutStateError) and response.status_code == "M_FORBIDDEN"
                level = logging.DEBUG if forbidden else failure_level(False)
                logger.log(level, "room_router_binding_publish_failed", room_id=room_id, error=str(response))
                return forbidden
            return True
    except Exception as exc:
        timed_out = isinstance(exc, TimeoutError)
        detail = {"error": "TimeoutError", "phase": phase} if timed_out else {"exc_info": True}
        logger.log(failure_level(timed_out), "room_router_binding_publish_failed", room_id=room_id, **detail)
        return False

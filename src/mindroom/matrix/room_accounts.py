"""Publish the room state that tells clients which MindRoom accounts are in a room and which one stewards it."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

import nio

from mindroom.entity_resolution import prepared_entity_names_by_user_id
from mindroom.logging_config import get_logger
from mindroom.matrix.event_types import MINDROOM_ACCOUNTS_EVENT_TYPE, ROUTER_BINDING_EVENT_TYPE
from mindroom.matrix.joined_members import read_joined_members

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
    """Compare the joined MindRoom roster under the writer's own Matrix ID and repair it if needed."""
    return await _reconcile(client, room_id, may_write, failure_level, (config, runtime_paths))


async def reconcile_room_router_binding(
    client: nio.AsyncClient,
    room_id: str,
    *,
    may_write: Callable[[], bool] | None = None,
    failure_level: Callable[[bool], int] = lambda _: logging.WARNING,
) -> bool:
    """Name the privileged account clients trust when the binding is absent or malformed."""
    return await _reconcile(client, room_id, may_write, failure_level, None)


async def _reconcile(  # noqa: C901, PLR0911 - roster and binding share the same bounded compare and write
    client: nio.AsyncClient,
    room_id: str,
    may_write: Callable[[], bool] | None,
    failure_level: Callable[[bool], int],
    roster: tuple[Config, RuntimePaths] | None,
) -> bool:
    """Compare before writing; return False only when the worker should retry."""
    if roster is None:
        kind, key, event = ROUTER_BINDING_EVENT_TYPE, "", "room_router_binding_publish_failed"
    else:
        kind, key, event = MINDROOM_ACCOUNTS_EVENT_TYPE, client.user_id, "room_accounts_publish_failed"

    def failed(error: object, diagnostic: str = event) -> bool:
        forbidden = isinstance(error, nio.ErrorResponse) and error.status_code == "M_FORBIDDEN"
        logger.log(logging.DEBUG if forbidden else failure_level(False), diagnostic, room_id=room_id, error=str(error))
        return forbidden

    phase = "joined_members"
    try:
        async with asyncio.timeout(10):
            content: dict[str, object] = {"user_id": client.user_id}
            if roster is not None:
                members = await read_joined_members(client, room_id)
                if not isinstance(members, nio.JoinedMembersResponse):
                    return failed(members, "matrix_room_members_fetch_failed")
                names = prepared_entity_names_by_user_id(*roster)
                accounts = {member.user_id for member in members.members if member.user_id in names}
                content = {"accounts": sorted(accounts)}
            phase = "state_get"
            current = await client.room_get_state_event(room_id, kind, state_key=key)
            if isinstance(current, nio.RoomGetStateEventResponse):
                if roster is None:
                    bound = current.content.get("user_id")
                    localpart, _, server = bound[1:].partition(":") if isinstance(bound, str) else ("", "", "")
                    if isinstance(bound, str) and bound.startswith("@") and localpart and server:
                        if bound != client.user_id:
                            logger.debug("room_router_binding_held_by_other", room_id=room_id, router_user_id=bound)
                        return True
                elif current.content == content:
                    return True
            elif not (isinstance(current, nio.RoomGetStateEventError) and current.status_code == "M_NOT_FOUND"):
                return failed(current)
            if may_write is not None and not may_write():
                return False
            phase = "state_put"
            response = await client.room_put_state(room_id, kind, content, state_key=key)
            return isinstance(response, nio.RoomPutStateResponse) or failed(response)
    except Exception as exc:
        timed_out = isinstance(exc, TimeoutError)
        detail = {"error": "TimeoutError", "phase": phase} if timed_out else {"exc_info": True}
        logger.log(failure_level(timed_out), event, room_id=room_id, **detail)
        return False

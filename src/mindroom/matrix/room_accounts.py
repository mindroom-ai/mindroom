"""Publish the room state that tells clients which MindRoom accounts are in a room and which one stewards it."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING
from weakref import WeakValueDictionary

import nio

from mindroom.entity_resolution import prepared_entity_names_by_user_id
from mindroom.logging_config import get_logger
from mindroom.matrix.event_types import MINDROOM_ACCOUNTS_EVENT_TYPE, ROUTER_BINDING_EVENT_TYPE
from mindroom.matrix.joined_members import read_joined_members

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping
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
    *,
    get_alternate_clients: Callable[[], Mapping[str, nio.AsyncClient]] = dict,
    failure_level: Callable[[bool], int] = lambda _: logging.WARNING,
) -> bool:
    """Refresh the room roster using its bound account when eligible, otherwise the steward.

    The state key is the writer's own user ID.
    A forbidden write needs no retry until the room changes.
    """
    phase = "room_lock"

    async def _write_roster(writer: nio.AsyncClient) -> bool | None:
        nonlocal phase
        event_type = MINDROOM_ACCOUNTS_EVENT_TYPE
        phase = "joined_members"
        try:
            response = await read_joined_members(writer, room_id)
        except nio.LocalProtocolError as exc:
            response = exc
        if isinstance(response, nio.JoinedMembersResponse):
            members = {member.user_id for member in response.members}
            content = {"accounts": sorted(members & prepared_entity_names_by_user_id(config, runtime_paths).keys())}
            phase = "state_get"
            current = await writer.room_get_state_event(room_id, event_type, writer.user_id)
            if isinstance(current, nio.RoomGetStateEventResponse) and current.content == content:
                return True
            if writer is not client and get_alternate_clients().get(writer.user_id) is not writer:
                logger.debug("room_accounts_writer_no_longer_eligible", room_id=room_id, user_id=writer.user_id)
                return None
            phase = "state_put"
            response = await writer.room_put_state(room_id, event_type, content, state_key=writer.user_id)
            if isinstance(response, nio.RoomPutStateResponse):
                return True
        forbidden = isinstance(response, nio.ErrorResponse) and response.status_code == "M_FORBIDDEN"
        members_read = phase == "joined_members"
        level = logging.DEBUG if forbidden and not (members_read and writer is client) else failure_level(False)
        diagnostic = "matrix_room_members_fetch_failed" if members_read else "room_accounts_publish_failed"
        logger.log(level, diagnostic, room_id=room_id, error=str(response))
        return None if forbidden else False

    try:
        # Share one deadline across lock, binding, and the first writer; renew only for steward fallback.
        lock = _LOCKS.setdefault((runtime_paths.storage_root, room_id), asyncio.Lock())
        async with asyncio.timeout(10) as deadline, lock:
            lookup = bool(get_alternate_clients())
            phase = "binding_get" if lookup else phase
            reply = (
                await client.room_get_state_event(room_id, ROUTER_BINDING_EVENT_TYPE, state_key="") if lookup else None
            )
            skip = isinstance(reply, nio.RoomGetStateEventError) and reply.status_code in {"M_NOT_FOUND", "M_FORBIDDEN"}
            if lookup and not isinstance(reply, nio.RoomGetStateEventResponse) and not skip:
                logger.log(failure_level(False), "room_accounts_publish_failed", room_id=room_id, error=str(reply))
                return False
            binding = reply.content if isinstance(reply, nio.RoomGetStateEventResponse) else None
            bound = binding.get("user_id") if isinstance(binding, dict) else None
            alternate = get_alternate_clients().get(bound) if isinstance(bound, str) else None
            result = await _write_roster(alternate if alternate is not None else client)
            if alternate is None or result is not None:
                return result is not False
            deadline.reschedule(asyncio.get_running_loop().time() + 10)
            return (await _write_roster(client)) is not False
    except Exception as exc:
        timed_out = isinstance(exc, TimeoutError)
        detail = {"error": "TimeoutError", "phase": phase} if timed_out else {"exc_info": True}
        logger.log(failure_level(timed_out), "room_accounts_publish_failed", room_id=room_id, **detail)
        return False


async def reconcile_room_router_binding(
    client: nio.AsyncClient,
    room_id: str,
    *,
    may_claim: Callable[[], bool] | None = None,
    failure_level: Callable[[bool], int] = lambda _: logging.WARNING,
) -> bool:
    """Name this account, the room's steward, in the `io.mindroom.router` binding where no valid one exists.

    Returns whether nothing is left to do. The binding keeps the name today's clients read: the privileged MindRoom
    account whose room state they trust. A binding naming another account stays, so two privileged stewards cannot
    take turns overwriting it. A stale roster can leave voice echoes standalone if the bound bot cannot refresh it.
    A room that forbids the read or write is done too. The steward retries transient failures in the background.
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
            if may_claim is not None and not may_claim():
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

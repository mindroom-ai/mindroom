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


async def reconcile_room_accounts(  # noqa: C901 - one lock covers both writers' comparisons and writes
    client: nio.AsyncClient,
    config: Config,
    runtime_paths: RuntimePaths,
    room_id: str,
    *,
    get_alternate_clients: Callable[[], Mapping[str, nio.AsyncClient]] = dict,
    failure_level: Callable[[bool], int] = lambda _: logging.WARNING,
) -> bool:
    """Refresh the room roster using its bound account when eligible, otherwise the steward."""
    phase = "room_lock"
    try:
        # Bound lock waits too; the lock prevents an older comparison from overwriting a newer roster.
        async with asyncio.timeout(10), _LOCKS.setdefault((runtime_paths.storage_root, room_id), asyncio.Lock()):
            writers = [client]
            if get_alternate_clients():
                phase = "binding_get"
                reply = await client.room_get_state_event(room_id, ROUTER_BINDING_EVENT_TYPE, state_key="")
                if isinstance(reply, nio.RoomGetStateEventResponse):
                    bound = reply.content.get("user_id") if isinstance(reply.content, dict) else None
                    if isinstance(bound, str) and (alternate := get_alternate_clients().get(bound)) is not None:
                        writers.insert(0, alternate)
                elif not (
                    isinstance(reply, nio.RoomGetStateEventError)
                    and reply.status_code in {"M_NOT_FOUND", "M_FORBIDDEN"}
                ):
                    logger.log(failure_level(False), "room_accounts_publish_failed", room_id=room_id, error=str(reply))
                    return False
            event_type = MINDROOM_ACCOUNTS_EVENT_TYPE
            for writer in writers:
                phase = "joined_members"
                diagnostic = "matrix_room_members_fetch_failed"
                try:
                    response = await read_joined_members(writer, room_id)
                except nio.LocalProtocolError as exc:
                    response = exc
                if isinstance(response, nio.JoinedMembersResponse):
                    members = {member.user_id for member in response.members}
                    accounts = members & prepared_entity_names_by_user_id(config, runtime_paths).keys()
                    content = {"accounts": sorted(accounts)}
                    phase = "state_get"
                    current = await writer.room_get_state_event(room_id, event_type, writer.user_id)
                    if isinstance(current, nio.RoomGetStateEventResponse) and current.content == content:
                        return True
                    if writer is not client and get_alternate_clients().get(writer.user_id) is not writer:
                        continue
                    phase = "state_put"
                    diagnostic = "room_accounts_publish_failed"
                    response = await writer.room_put_state(room_id, event_type, content, state_key=writer.user_id)
                    if isinstance(response, nio.RoomPutStateResponse):
                        return True
                forbidden = isinstance(response, nio.ErrorResponse) and response.status_code == "M_FORBIDDEN"
                level = logging.DEBUG if forbidden else failure_level(False)
                logger.log(level, diagnostic, room_id=room_id, error=str(response))
                if not forbidden:
                    return False
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

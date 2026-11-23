"""Publish the room state that tells clients which MindRoom accounts are in a room and which one stewards it."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING
from weakref import WeakValueDictionary

import nio

from mindroom.entity_resolution import prepared_entity_names_by_user_id
from mindroom.logging_config import get_logger
from mindroom.matrix.client_room_admin import get_room_members
from mindroom.matrix.event_types import MINDROOM_ACCOUNTS_EVENT_TYPE, ROUTER_BINDING_EVENT_TYPE
from mindroom.matrix.state_failures import StateFailure, state_failure

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
    A refused roster read or write ends the attempt; a failed member read retries.
    """
    phase = "room_lock"
    event_type = MINDROOM_ACCOUNTS_EVENT_TYPE
    try:
        lock = _LOCKS.setdefault((runtime_paths.storage_root, room_id), asyncio.Lock())
        async with asyncio.timeout(10), lock:
            lookup = bool(get_alternate_clients())
            phase = "binding_get" if lookup else phase
            reply = (
                await client.room_get_state_event(room_id, ROUTER_BINDING_EVENT_TYPE, state_key="") if lookup else None
            )
            skip = isinstance(reply, nio.RoomGetStateEventError) and state_failure(reply) in {
                StateFailure.MISSING,
                StateFailure.REFUSED,
            }
            if lookup and not isinstance(reply, nio.RoomGetStateEventResponse) and not skip:
                logger.log(failure_level(False), "room_accounts_publish_failed", room_id=room_id, error=str(reply))
                return False
            binding = reply.content if isinstance(reply, nio.RoomGetStateEventResponse) else None
            bound = binding.get("user_id") if isinstance(binding, dict) else None
            writer = (get_alternate_clients().get(bound) if isinstance(bound, str) else None) or client
            phase = "joined_members"
            members = await get_room_members(writer, room_id, failure_level=failure_level)
            if members is None:
                return False
            content = {"accounts": sorted(members & prepared_entity_names_by_user_id(config, runtime_paths).keys())}
            phase = "state_get"
            response = await writer.room_get_state_event(room_id, event_type, state_key=writer.user_id)
            not_found = (
                isinstance(response, nio.RoomGetStateEventError) and state_failure(response) is StateFailure.MISSING
            )
            if isinstance(response, nio.RoomGetStateEventResponse) or not_found:
                if writer is not client and get_alternate_clients().get(writer.user_id) is not writer:
                    logger.debug("room_accounts_writer_no_longer_eligible", room_id=room_id, user_id=writer.user_id)
                    return False
                if not (isinstance(response, nio.RoomGetStateEventResponse) and response.content == content):
                    phase = "state_put"
                    response = await writer.room_put_state(room_id, event_type, content, state_key=writer.user_id)
                if isinstance(response, (nio.RoomGetStateEventResponse, nio.RoomPutStateResponse)):
                    return True
            forbidden = isinstance(response, nio.ErrorResponse) and state_failure(response) is StateFailure.REFUSED
            level = logging.DEBUG if forbidden else failure_level(False)
            logger.log(level, "room_accounts_publish_failed", room_id=room_id, error=str(response))
            return forbidden
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
            elif not (
                isinstance(current, nio.RoomGetStateEventError) and state_failure(current) is StateFailure.MISSING
            ):
                # An unreadable binding may name another router, so only absent or malformed state is claimed.
                forbidden = (
                    isinstance(current, nio.RoomGetStateEventError) and state_failure(current) is StateFailure.REFUSED
                )
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
                forbidden = (
                    isinstance(response, nio.RoomPutStateError) and state_failure(response) is StateFailure.REFUSED
                )
                level = logging.DEBUG if forbidden else failure_level(False)
                logger.log(level, "room_router_binding_publish_failed", room_id=room_id, error=str(response))
                return forbidden
            return True
    except Exception as exc:
        timed_out = isinstance(exc, TimeoutError)
        detail = {"error": "TimeoutError", "phase": phase} if timed_out else {"exc_info": True}
        logger.log(failure_level(timed_out), "room_router_binding_publish_failed", room_id=room_id, **detail)
        return False

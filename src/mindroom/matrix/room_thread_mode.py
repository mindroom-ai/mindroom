"""Reconcile the thread-mode state clients use to choose a room view."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING
from weakref import WeakValueDictionary

import nio

from mindroom.authorization import configured_responder_entities_for_room, get_available_responders_in_room
from mindroom.entity_resolution import prepared_entity_names_by_user_id
from mindroom.logging_config import get_logger
from mindroom.matrix.room_membership import ensure_room_membership_synced
from mindroom.matrix.state_failures import StateFailure, state_failure
from mindroom.room_settings import THREAD_MODE, refresh_room_setting, resolve_room_setting

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from pathlib import Path

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths

logger = get_logger(__name__)
_THREAD_MODE_EVENT_TYPE = "io.mindroom.thread_mode"
_LOCKS: WeakValueDictionary[tuple[Path, str], asyncio.Lock] = WeakValueDictionary()


async def reconcile_room_thread_mode(  # noqa: C901, PLR0911 - one outcome per comparison step
    client: nio.AsyncClient,
    config: Config,
    runtime_paths: RuntimePaths,
    room_id: str,
    *,
    put_state: Callable[..., Awaitable[nio.RoomPutStateResponse | nio.RoomPutStateError | None]] | None = None,
    failure_level: Callable[[bool], int] = lambda _: logging.WARNING,
) -> bool | None:
    """Compare one state event with reply-path responders and return whether it is current.

    The room's steward reads and writes with its own client; a bot that answers a command reads with its client
    and writes through ``put_state``, the steward's.
    The room's `thread_mode` setting is read here, under the room's lock, so the mode published is never an older
    one than the room holds; a setting that changes while its mode is published is published next.
    None says the room refused the write for lack of power.
    The steward retries transient failures in the background.
    """
    phase = "room_lock"
    try:
        identities = prepared_entity_names_by_user_id(config, runtime_paths)
        # Bound lock waits too; the lock serializes the comparison and write.
        async with asyncio.timeout(10), _LOCKS.setdefault((runtime_paths.storage_root, room_id), asyncio.Lock()):
            room = client.rooms[room_id]
            responders = configured_responder_entities_for_room(room, config, runtime_paths)
            if responders is None:
                phase = "joined_members"
                if not await ensure_room_membership_synced(
                    client,
                    room,
                    sender_id=client.user_id,
                    failure_level=failure_level,
                ):
                    logger.debug("room_thread_mode_membership_unavailable", room_id=room_id)
                    return False
                responders = get_available_responders_in_room(room, config, runtime_paths)
            phase = "responder_membership"
            joined = [
                identities[responder.full_id]
                for responder in responders
                if await _joined(client, room_id, responder.full_id)
            ]
            if not joined:
                return True
            write = put_state or client.room_put_state
            while True:
                phase = "room_setting"
                read = await refresh_room_setting(
                    THREAD_MODE,
                    client,
                    runtime_paths,
                    room_id,
                    force=True,
                    put_state=write,
                )
                if not read.ok:
                    level = failure_level(False)
                    logger.log(level, "room_thread_mode_reconcile_failed", room_id=room_id, error="setting unread")
                    return False
                mode: str | None = read.value
                if mode is None:
                    # Each responder's mode is its configured one: the runtime's copy may be a late change's by now.
                    modes = {config.get_entity_thread_mode(name, runtime_paths, room_id, read=read) for name in joined}
                    mode = "mixed" if len(modes) > 1 else next(iter(modes))
                content = {"mode": mode}
                phase = "state_get"
                current = await client.room_get_state_event(room_id, _THREAD_MODE_EVENT_TYPE, state_key="")
                if (
                    isinstance(current, nio.RoomGetStateEventError)
                    and state_failure(current) is not StateFailure.MISSING
                ):
                    level = failure_level(False)
                    logger.log(level, "room_thread_mode_reconcile_failed", room_id=room_id, error=str(current))
                    return False
                if not isinstance(current, nio.RoomGetStateEventResponse) or current.content != content:
                    phase = "state_put"
                    response = await write(room_id, _THREAD_MODE_EVENT_TYPE, content, state_key="")
                    if not isinstance(response, nio.RoomPutStateResponse):
                        # A room without a steward, or whose power levels keep it from sending this state, refuses it
                        # on every trigger.
                        refused = response is None or (
                            isinstance(response, nio.RoomPutStateError)
                            and state_failure(response) is StateFailure.REFUSED
                        )
                        level = logging.DEBUG if refused else failure_level(False)
                        logger.log(level, "room_thread_mode_reconcile_failed", room_id=room_id, error=str(response))
                        return None if refused else False
                # A change to the setting that arrived while this one was published is published as well.
                if resolve_room_setting(THREAD_MODE, runtime_paths, room_id) == read.value:
                    return True
    except Exception as exc:
        timed_out = isinstance(exc, TimeoutError)
        detail = {"error": "TimeoutError", "phase": phase} if timed_out else {"exc_info": True}
        logger.log(failure_level(timed_out), "room_thread_mode_reconcile_failed", room_id=room_id, **detail)
        return False


async def _joined(client: nio.AsyncClient, room_id: str, user_id: str) -> bool:
    membership = await client.room_get_state_event(room_id, "m.room.member", user_id)
    if isinstance(membership, nio.RoomGetStateEventError) and state_failure(membership) is not StateFailure.MISSING:
        raise RuntimeError(str(membership))
    return isinstance(membership, nio.RoomGetStateEventResponse) and membership.content.get("membership") == "join"

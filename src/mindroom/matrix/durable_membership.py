"""Local membership commands ordered after durable producer admission."""

import asyncio
import json
from collections.abc import Awaitable, Callable
from uuid import UUID, uuid5

from nio.durable import DurableSync
from nio.durable.transport import HttpError

from mindroom.event_journal.models import RoomMembershipPosition
from mindroom.logging_config import get_logger

logger = get_logger(__name__)
_OPERATION_NAMESPACE = UUID("0bd4e975-c3e9-5b10-8d46-68fdda1adc07")


def _require_command(room_id: str, target_membership: str) -> None:
    if type(room_id) is not str or not room_id:
        message = "room_id must be a nonempty str"
        raise TypeError(message)
    if type(target_membership) is not str or target_membership not in {"join", "leave"}:
        message = "target_membership must be exactly 'join' or 'leave'"
        raise TypeError(message)


async def _try_change_membership(
    session: DurableSync,
    *,
    account_id: str,
    room_id: str,
    position: RoomMembershipPosition,
    target_membership: str,
) -> tuple[bool, HttpError | None]:
    operation_name = json.dumps(
        [account_id, room_id, position.membership_epoch, target_membership],
        ensure_ascii=True,
        separators=(",", ":"),
    )
    try:
        applied = await session.change_membership(
            operation_id=uuid5(_OPERATION_NAMESPACE, operation_name),
            room_id=room_id,
            previous_membership=position.membership,
            previous_epoch=position.membership_epoch,
            current_membership=target_membership,
        )
    except HttpError as exc:
        return False, exc
    return applied, None


async def change_local_membership(
    session: DurableSync,
    *,
    account_id: str,
    room_id: str,
    target_membership: str,
    read_position: Callable[[str], Awaitable[RoomMembershipPosition | None]],
    admission_progress: asyncio.Event,
    subscribe_awaited_rooms: Callable[[], Awaitable[None]],
    is_authorized: Callable[[], bool] | None = None,
) -> bool:
    """Wait for retained source work and retry commands whose position changed.

    The caller serializes local commands while the ingestion pump keeps running.
    A producer can commit membership before the application admits its batch,
    including across restart, so an admitted position alone cannot certify a
    no-op or supply the precondition for a new command.

    A join, including one the producer already reports, returns True only once
    nio can send to the room, and False if the room's membership ends first:
    nio refuses plaintext sends into a joined room until a sync delivers the
    room's complete state.

    Both waits need a sync to report a room, so each first calls
    ``subscribe_awaited_rooms``: a Sliding source reports a room only through
    its room list or a subscription, and the room a join resets, or one whose
    join a restart left unobserved, may be in neither.
    """
    _require_command(room_id, target_membership)
    while True:
        await subscribe_awaited_rooms()
        await session.wait_for_membership_idle()
        admission_progress.clear()
        admitted = await read_position(room_id)
        if admission_progress.is_set():
            # Admission may finish while the database read is in flight.
            continue
        if await session.next_batch() is not None:
            await admission_progress.wait()
            continue
        # The caller's policy can change while the command waits for its
        # account lock or durable admission. Check immediately before handing
        # ownership to nio, including retries and already-satisfied commands.
        if is_authorized is not None and not is_authorized():
            return False
        if admitted is not None and admitted.membership == target_membership:
            break
        # Unobserved nio state starts at leave/0 but does not prove that an
        # HTTP leave succeeded, so the first command still contacts Matrix.
        position = admitted or RoomMembershipPosition("leave", 0)
        applied, http_error = await _try_change_membership(
            session,
            account_id=account_id,
            room_id=room_id,
            position=position,
            target_membership=target_membership,
        )
        if applied:
            break
        # nio can finish captured input while taking command ownership. Retry
        # only when admission moved or still owes that producer work; an HTTP
        # failure at the same settled position remains a failure for the caller.
        if await read_position(room_id) == admitted and await session.next_batch() is None:
            if http_error is not None:
                logger.warning(
                    "matrix_membership_change_failed",
                    room_id=room_id,
                    target_membership=target_membership,
                    http_status=http_error.status,
                    errcode=http_error.errcode,
                )
            return False
    if target_membership == "leave":
        return True
    await subscribe_awaited_rooms()
    return await session.wait_for_room_ready(room_id)

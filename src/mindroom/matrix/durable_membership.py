"""Local membership commands ordered after durable producer admission."""

import asyncio
import json
from collections.abc import Awaitable, Callable
from uuid import UUID, uuid5

from nio.durable import DurableSync

from mindroom.event_journal.models import RoomMembershipPosition

_OPERATION_NAMESPACE = UUID("0bd4e975-c3e9-5b10-8d46-68fdda1adc07")


async def change_local_membership(
    session: DurableSync,
    *,
    account_id: str,
    room_id: str,
    target_membership: str,
    read_position: Callable[[str], Awaitable[RoomMembershipPosition | None]],
    admission_progress: asyncio.Event,
    is_authorized: Callable[[], bool] | None = None,
) -> bool:
    """Wait for retained source work and retry commands whose position changed.

    The caller serializes local commands while the ingestion pump keeps running.
    A producer can commit membership before the application admits its batch,
    including across restart, so an admitted position alone cannot certify a
    no-op or supply the precondition for a new command.
    """
    if type(room_id) is not str or not room_id:
        message = "room_id must be a nonempty str"
        raise TypeError(message)
    if type(target_membership) is not str or target_membership not in {"join", "leave"}:
        message = "target_membership must be exactly 'join' or 'leave'"
        raise TypeError(message)
    while True:
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
            return True
        # Unobserved nio state starts at leave/0 but does not prove that an
        # HTTP leave succeeded, so the first command still contacts Matrix.
        position = admitted or RoomMembershipPosition("leave", 0)
        operation_name = json.dumps(
            [account_id, room_id, position.membership_epoch, target_membership],
            ensure_ascii=True,
            separators=(",", ":"),
        )
        if await session.change_membership(
            operation_id=uuid5(_OPERATION_NAMESPACE, operation_name),
            room_id=room_id,
            previous_membership=position.membership,
            previous_epoch=position.membership_epoch,
            current_membership=target_membership,
        ):
            return True
        # nio can finish captured input while taking command ownership. Retry
        # only when admission moved or still owes that producer work; an HTTP
        # failure at the same settled position remains a failure for the caller.
        if await read_position(room_id) == admitted and await session.next_batch() is None:
            return False

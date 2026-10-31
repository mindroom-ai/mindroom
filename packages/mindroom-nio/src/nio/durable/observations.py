"""Best-effort observations retained only for one fresh response."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from ..events import EphemeralEvent, PresenceEvent

if TYPE_CHECKING:
    from ..client.async_client import AsyncClient

logger = logging.getLogger(__name__)
TransientSections = list[tuple[str | None, object]]


async def deliver_transients(client: AsyncClient, sections: TransientSections) -> None:
    malformed = 0
    callback_errors: dict[str, int] = {}
    timed_out = False
    try:
        async with asyncio.timeout(1):
            for room_id, section in sections:
                if not isinstance(section, dict) or not isinstance(
                    section.get("events", []), list
                ):
                    malformed += 1
                    continue
                for raw in section.get("events", []):
                    try:
                        event = (
                            PresenceEvent.from_dict(raw)
                            if room_id is None
                            else EphemeralEvent.parse_event(raw)
                        )
                        if not isinstance(event, (PresenceEvent, EphemeralEvent)):
                            malformed += 1
                            continue
                    except Exception:
                        malformed += 1
                        continue
                    try:
                        if isinstance(event, PresenceEvent):
                            client._project_presence(event)
                            await client._on_presence(event)
                        else:
                            assert room_id is not None
                            room = client.rooms.get(room_id)
                            if room is not None:
                                room.handle_ephemeral_event(event)
                                await client._on_ephemeral(event, room)
                    except Exception as error:
                        name = type(error).__name__
                        callback_errors[name] = callback_errors.get(name, 0) + 1
                    await asyncio.sleep(0)
    except TimeoutError:
        timed_out = True
    if malformed or callback_errors or timed_out:
        logger.warning(
            "Discarded transient observations: malformed=%s callback_errors=%s timeout=%s",
            malformed,
            callback_errors,
            timed_out,
        )

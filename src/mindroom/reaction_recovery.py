"""Durable semantic-consumer receipts for recovered reactions."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from mindroom.commands import config_confirmation
from mindroom.constants import ROUTER_AGENT_NAME
from mindroom.dispatch_recovery_context import dispatch_recovery_active

if TYPE_CHECKING:
    from collections.abc import Callable

    import nio


async def recovered_reaction_was_consumed(
    *,
    entity_name: str,
    is_durably_handled: Callable[[str], bool],
    client: nio.AsyncClient,
    room_id: str,
    event: nio.ReactionEvent,
) -> bool:
    """Prevent durable callback replay from choosing a second semantic consumer."""
    if not dispatch_recovery_active():
        return False
    if await asyncio.to_thread(is_durably_handled, event.event_id):
        return True
    return entity_name == ROUTER_AGENT_NAME and await config_confirmation.has_visible_confirmation_response(
        client,
        room_id,
        event,
    )

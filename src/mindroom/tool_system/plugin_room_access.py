"""Room resolution and access helpers exported on ``mindroom.plugin_api``."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from mindroom.tool_system.runtime_context import ToolRuntimeContext

_SHARED_ROOM_CHECK_CONCURRENCY = 6


async def resolve_accessible_room_id(
    context: ToolRuntimeContext,
    room_id: str | None = None,
    *,
    contact_room_ok: bool = False,
) -> tuple[str | None, str | None]:
    """Resolve ``room_id`` (defaulting to the current room) and verify requester access.

    Returns ``(resolved_room_id, None)`` on success, or ``(None, error_message)`` when
    the target room is invalid, unknown, or not authorized for the requester.
    """
    from mindroom.custom_tools.attachment_helpers import (  # noqa: PLC0415
        resolve_requested_room_id,
        room_access_allowed,
    )

    resolved_room_id, room_error = await resolve_requested_room_id(context, room_id)
    if room_error is not None or resolved_room_id is None:
        return None, room_error or "Unknown room."
    if not await room_access_allowed(context, resolved_room_id, contact_room_ok=contact_room_ok):
        return None, "Not authorized to access the target room."
    return resolved_room_id, None


async def shared_room_ids(
    context: ToolRuntimeContext,
    *,
    contact_room_ok: bool = False,
) -> list[str]:
    """Return every joined room the requester is currently authorized to access, preserving room order."""
    room_ids = list(context.client.rooms)
    if not room_ids:
        return []

    from aiohttp import ClientError  # noqa: PLC0415

    from mindroom.custom_tools.attachment_helpers import room_access_allowed  # noqa: PLC0415

    checks = asyncio.Semaphore(_SHARED_ROOM_CHECK_CONCURRENCY)

    async def is_shared(room_id: str) -> bool:
        async with checks:
            try:
                return await room_access_allowed(
                    context,
                    room_id,
                    contact_room_ok=contact_room_ok,
                    require_membership=True,
                )
            except (ClientError, TimeoutError):
                return False

    allowed = await asyncio.gather(*(is_shared(room_id) for room_id in room_ids))
    return [room_id for room_id, ok in zip(room_ids, allowed, strict=True) if ok]


__all__ = ["resolve_accessible_room_id", "shared_room_ids"]

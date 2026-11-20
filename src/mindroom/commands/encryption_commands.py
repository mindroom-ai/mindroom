"""Chat-based Matrix encryption handling for the `!encrypt` and `!e2ee` commands."""

from __future__ import annotations

from typing import TYPE_CHECKING

from aiohttp import ClientError

from mindroom.matrix.client_room_admin import (
    room_admin_power_user,
    room_encryption_enable_failure,
    room_encryption_enabled,
)
from mindroom.matrix.cross_signing import cross_signing_status_line
from mindroom.matrix.decrypt_failure import e2ee_stats
from mindroom.steward_duties import moderator_rights_message

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    import nio

_ENCRYPT_USAGE = "Usage: `/encrypt` to review, then `/encrypt confirm` to enable."
_CONFIRM_ARGUMENTS = frozenset({"confirm", "yes"})
# The homeserver's reason is free text; a fallback can be a whole response repr.
_FAILURE_REASON_MAX_CHARS = 200


async def _confirm_encrypt(
    client: nio.AsyncClient,
    room_id: str,
    requester_user_id: str,
    sender_user_id: str,
    put_state: Callable[..., Awaitable[nio.RoomPutStateResponse | nio.RoomPutStateError | None]] | None,
    steward_may_write_state: Callable[[str, str], bool] | None,
) -> str:
    """Enable encryption for a room admin through ``put_state``, the room steward's, or explain why not."""
    admin_user_id = await room_admin_power_user(client, room_id, (requester_user_id, sender_user_id))
    if admin_user_id is None:
        return "❌ Room admin only."
    try:
        failure = await room_encryption_enable_failure(client, room_id, put_state=put_state)
    except (ClientError, OSError):
        # The request did not get through, which a retry can fix.
        return "❌ Failed to enable encryption. Run the command again to retry."
    if failure is None:
        return "🔐 End-to-end encryption is now enabled for this room. This cannot be undone."
    # A server without encryption answers M_FORBIDDEN too, so the steward's own power tells the two refusals apart.
    lacks_power = (
        failure.startswith("M_FORBIDDEN")
        and steward_may_write_state is not None
        and not steward_may_write_state(room_id, "m.room.encryption")
    )
    if not failure or lacks_power:
        return moderator_rights_message("enable encryption")
    return f"❌ Failed to enable encryption: {failure[:_FAILURE_REASON_MAX_CHARS]}"


async def handle_encrypt_command(
    args_text: str,
    *,
    client: nio.AsyncClient,
    room_id: str,
    requester_user_id: str,
    sender_user_id: str,
    put_state: Callable[..., Awaitable[nio.RoomPutStateResponse | nio.RoomPutStateError | None]] | None = None,
    steward_may_write_state: Callable[[str, str], bool] | None = None,
) -> str:
    """Review or enable Matrix end-to-end encryption for the current room."""
    encrypted = await room_encryption_enabled(client, room_id)
    if encrypted is None:
        return "❌ Could not read this room's encryption state. Try again shortly."
    if encrypted:
        return "🔐 This room is already end-to-end encrypted."

    requested = args_text.strip().lower()
    if not requested:
        return (
            "🔐 This room is **not** end-to-end encrypted.\n\n"
            "⚠️ Enabling encryption is **irreversible**: it cannot be turned off again for this room, "
            "and people joining later cannot read messages sent before they joined.\n\n"
            "To proceed, type `/encrypt confirm` (room admin only)."
        )
    if requested not in _CONFIRM_ARGUMENTS:
        return f"❌ Unknown argument `{args_text.strip()}`.\n\n{_ENCRYPT_USAGE}"
    return await _confirm_encrypt(
        client,
        room_id,
        requester_user_id,
        sender_user_id,
        put_state,
        steward_may_write_state,
    )


async def handle_e2ee_command(
    *,
    client: nio.AsyncClient,
    room_id: str,
) -> str:
    """Report encryption diagnostics for the current room and responding bot."""
    encrypted = await room_encryption_enabled(client, room_id)
    stats = e2ee_stats()
    room_failures = stats.decrypt_failures_by_room.get(room_id, 0)

    room_state = {True: "encrypted", False: "not encrypted", None: "unknown (state unreadable)"}[encrypted]

    store_state = "present" if client.olm is not None else "unavailable"

    lines = [
        "**E2EE diagnostics**",
        f"- Room: {room_state}",
        f"- Responding bot: `{client.user_id or 'unknown'}` (device `{client.device_id or 'unknown'}`)",
        f"- Encryption store: {store_state}",
        f"- Cross-signing: {cross_signing_status_line(client)}",
        f"- Undecryptable events in this room since startup: {room_failures}",
        (
            f"- Process totals since startup: {stats.decrypt_failures} undecryptable, "
            f"{stats.key_requests_sent} key requests sent, {stats.notices_sent} notices posted"
        ),
    ]
    return "\n".join(lines)


__all__ = ["handle_e2ee_command", "handle_encrypt_command"]

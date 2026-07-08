"""Chat-based Matrix encryption handling for the `!encrypt` and `!e2ee` commands."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.matrix.client_room_admin import (
    ensure_room_encryption_enabled,
    room_admin_power_user,
    room_encryption_enabled,
)
from mindroom.matrix.decrypt_failure import e2ee_stats

if TYPE_CHECKING:
    import nio

_ENCRYPT_USAGE = "Usage: `!encrypt` to review, then `!encrypt confirm` to enable."
_CONFIRM_ARGUMENTS = frozenset({"confirm", "yes"})


async def _confirm_encrypt(
    client: nio.AsyncClient,
    room_id: str,
    requester_user_id: str,
    sender_user_id: str,
) -> str:
    """Enable encryption for a room admin, or explain why not."""
    admin_user_id = await room_admin_power_user(client, room_id, (requester_user_id, sender_user_id))
    if admin_user_id is None:
        return "❌ Room admin only."
    if await ensure_room_encryption_enabled(client, room_id):
        return "🔐 End-to-end encryption is now enabled for this room. This cannot be undone."
    return (
        "❌ Failed to enable encryption. "
        "I may lack permission to change room state here; ask a room admin to enable it from their client."
    )


async def handle_encrypt_command(
    args_text: str,
    *,
    client: nio.AsyncClient,
    room_id: str,
    requester_user_id: str,
    sender_user_id: str,
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
            "To proceed, type `!encrypt confirm` (room admin only)."
        )
    if requested not in _CONFIRM_ARGUMENTS:
        return f"❌ Unknown argument `{args_text.strip()}`.\n\n{_ENCRYPT_USAGE}"
    return await _confirm_encrypt(client, room_id, requester_user_id, sender_user_id)


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
        "- Cross-signing: not yet supported (bot devices show as unverified)",
        f"- Undecryptable events in this room since startup: {room_failures}",
        (
            f"- Process totals since startup: {stats.decrypt_failures} undecryptable, "
            f"{stats.key_requests_sent} key requests sent, {stats.notices_sent} notices posted"
        ),
    ]
    return "\n".join(lines)


__all__ = ["handle_e2ee_command", "handle_encrypt_command"]

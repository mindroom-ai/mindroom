"""Chat-based room thread mode override handling for the `!thread_mode` command."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from mindroom.matrix.client_room_admin import user_has_room_admin_power
from mindroom.room_thread_modes import (
    RoomThreadMode,
    clear_room_thread_mode_override,
    get_room_thread_mode_override,
    set_room_thread_mode_override,
)

if TYPE_CHECKING:
    import nio

    from mindroom.constants import RuntimePaths

_LIST_ARGUMENTS = frozenset({"list", "show", "status"})
_RESET_ARGUMENTS = frozenset({"reset", "clear"})
_VALID_MODES: frozenset[str] = frozenset({"thread", "room"})
_USAGE = "Usage: `!thread_mode [room|thread|reset|show]`"


def _show_room_thread_mode(runtime_paths: RuntimePaths, room_id: str) -> str:
    override = get_room_thread_mode_override(runtime_paths, room_id)
    if override.mode is None:
        return (
            "No room thread mode override is set. "
            "Agents use their configured `thread_mode` and `room_thread_modes` values.\n\n"
            f"{_USAGE}"
        )
    return (
        f"This room uses the `{override.mode}` thread mode override.\n\n"
        "Use `!thread_mode reset` to restore configured modes."
    )


async def handle_thread_mode_command(
    args_text: str,
    *,
    client: nio.AsyncClient,
    runtime_paths: RuntimePaths,
    room_id: str,
    requester_user_id: str,
    sender_user_id: str,
) -> str:
    """Show, set, or clear the room-level thread mode override."""
    requested = args_text.strip().lower()
    if not requested or requested in _LIST_ARGUMENTS:
        return _show_room_thread_mode(runtime_paths, room_id)

    if requested not in _VALID_MODES and requested not in _RESET_ARGUMENTS:
        return f"❌ Unknown thread mode `{args_text.strip()}`.\n\n{_USAGE}"

    requester_candidates = {requester_user_id, sender_user_id}
    if not await user_has_room_admin_power(client, room_id, requester_candidates):
        return "❌ Room admin only."

    if requested in _RESET_ARGUMENTS:
        if clear_room_thread_mode_override(runtime_paths, room_id):
            return "✅ Room thread mode override removed. Agents use their configured modes again."
        return "This room has no thread mode override."

    mode = cast("RoomThreadMode", requested)
    set_room_thread_mode_override(
        runtime_paths,
        room_id=room_id,
        mode=mode,
        set_by=requester_user_id,
    )
    return (
        f"✅ This room now uses `{mode}` thread mode for future agent replies.\n\n"
        "Use `!thread_mode reset` to restore configured modes."
    )

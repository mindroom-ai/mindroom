"""Chat-based room thread mode override handling for the `!thread_mode` command."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast, get_args

from mindroom.matrix.client_room_admin import room_admin_power_user
from mindroom.room_modes import change_room_thread_mode
from mindroom.room_thread_modes import RoomThreadMode, resolve_room_thread_mode_override

if TYPE_CHECKING:
    import nio

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths

_LIST_ARGUMENTS = frozenset({"list", "show", "status"})
_RESET_ARGUMENTS = frozenset({"reset", "clear"})
_VALID_MODES: frozenset[str] = frozenset(get_args(RoomThreadMode))
_USAGE = "Usage: `!thread_mode [room|thread|reset|show]`"


def _show_room_thread_mode(runtime_paths: RuntimePaths, room_id: str) -> str:
    mode = resolve_room_thread_mode_override(runtime_paths, room_id)
    if mode is None:
        return (
            "No room thread mode override is set. "
            "Agents use their configured `thread_mode` and `room_thread_modes` values.\n\n"
            f"{_USAGE}"
        )
    return f"This room uses the `{mode}` thread mode override.\n\nUse `!thread_mode reset` to restore configured modes."


async def handle_thread_mode_command(
    args_text: str,
    *,
    client: nio.AsyncClient,
    config: Config,
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

    if await room_admin_power_user(client, room_id, (requester_user_id, sender_user_id)) is None:
        return "❌ Room admin only."

    if requested in _RESET_ARGUMENTS:
        removed = resolve_room_thread_mode_override(runtime_paths, room_id) is not None
        current = await change_room_thread_mode(client, config, runtime_paths, room_id, None)
        if not removed:
            # A rerun after a failed publication finds no override; it still warns while publishing fails.
            reply = "This room has no thread mode override."
            if current:
                return reply
        else:
            reply = "Room thread mode override removed. Agents use their configured modes again."
    else:
        mode = cast("RoomThreadMode", requested)
        current = await change_room_thread_mode(client, config, runtime_paths, room_id, mode)
        reply = (
            f"This room now uses `{mode}` thread mode for future agent replies.\n\n"
            "Use `!thread_mode reset` to restore configured modes."
        )
    if current:
        return f"✅ {reply}"
    return (
        f"⚠️ {reply}\n\nClients may still show the old thread mode in this room because publishing the new one failed. "
        "Run the command again to retry."
    )

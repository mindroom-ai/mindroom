"""Chat-based room reply mode override handling for the `!reply_mode` command."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from mindroom.matrix.client_room_admin import room_admin_power_user
from mindroom.room_reply_modes import (
    ROOM_REPLY_MODES,
    refresh_room_reply_mode,
    resolve_room_reply_mode_override,
    write_room_reply_mode,
)

if TYPE_CHECKING:
    import nio

    from mindroom.config.agent import RoomReplyMode
    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths

_LIST_ARGUMENTS = frozenset({"list", "show", "status"})
_RESET_ARGUMENTS = frozenset({"reset", "clear"})
_USAGE = "Usage: `/reply_mode [auto|mentions|reset|show]`"
_WRITE_FAILED = "❌ The room did not take the change; check that MindRoom's router may change the room's settings."
_MODE_EFFECTS: dict[RoomReplyMode, str] = {
    "auto": "The router or the room's only agent or team may answer a message that mentions no one.",
    "mentions": (
        "Agents and teams answer messages that mention them, and follow-ups in threads they already replied in "
        "or are still answering."
    ),
}


def _show_room_reply_mode(config: Config, runtime_paths: RuntimePaths, room_id: str) -> str:
    mode = resolve_room_reply_mode_override(runtime_paths, room_id)
    if mode is None:
        configured = config.get_room_reply_mode(runtime_paths, room_id)
        return (
            f"No room reply mode override is set. This room uses its configured `{configured}` reply mode.\n\n"
            f"{_MODE_EFFECTS[configured]}\n\n{_USAGE}"
        )
    return (
        f"This room uses the `{mode}` reply mode override.\n\n{_MODE_EFFECTS[mode]}\n\n"
        "Use `/reply_mode reset` to restore the configured mode."
    )


async def handle_reply_mode_command(  # noqa: PLR0911 - one answer per outcome
    args_text: str,
    *,
    client: nio.AsyncClient,
    config: Config,
    runtime_paths: RuntimePaths,
    room_id: str,
    requester_user_id: str,
    sender_user_id: str,
) -> str:
    """Show, set, or clear the room-level reply mode override, the room's `reply_mode` setting."""
    requested = args_text.strip().lower()
    await refresh_room_reply_mode(client, runtime_paths, room_id)
    if not requested or requested in _LIST_ARGUMENTS:
        return _show_room_reply_mode(config, runtime_paths, room_id)

    if requested not in ROOM_REPLY_MODES and requested not in _RESET_ARGUMENTS:
        return f"❌ Unknown reply mode `{args_text.strip()}`.\n\n{_USAGE}"

    if await room_admin_power_user(client, room_id, (requester_user_id, sender_user_id)) is None:
        return "❌ Room admin only."

    if requested in _RESET_ARGUMENTS:
        if resolve_room_reply_mode_override(runtime_paths, room_id) is None:
            return "This room has no reply mode override."
        if not await write_room_reply_mode(client, runtime_paths, room_id, None):
            return _WRITE_FAILED
        configured = config.get_room_reply_mode(runtime_paths, room_id)
        return f"✅ Room reply mode override removed. This room uses its configured `{configured}` reply mode again."

    mode = cast("RoomReplyMode", requested)
    if not await write_room_reply_mode(client, runtime_paths, room_id, mode):
        return _WRITE_FAILED
    return (
        f"✅ This room now uses `{mode}` reply mode.\n\n{_MODE_EFFECTS[mode]}\n\n"
        "Use `/reply_mode reset` to restore the configured mode."
    )

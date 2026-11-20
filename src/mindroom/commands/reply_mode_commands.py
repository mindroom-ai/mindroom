"""Chat-based room reply mode override handling for the `!reply_mode` command."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from mindroom.matrix.client_room_admin import room_admin_power_user
from mindroom.room_reply_modes import ROOM_REPLY_MODES, ReplyModeRead, refresh_room_reply_mode, write_room_reply_mode
from mindroom.steward_duties import moderator_rights_message

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    import nio

    from mindroom.config.agent import RoomReplyMode
    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths

_LIST_ARGUMENTS = frozenset({"list", "show", "status"})
_RESET_ARGUMENTS = frozenset({"reset", "clear"})
_USAGE = "Usage: `/reply_mode [auto|mentions|reset|show]`"
_WRITE_FAILED = "❌ The room did not take the change. Run the command again to retry."
_MODE_EFFECTS: dict[RoomReplyMode, str] = {
    "auto": "The agent or team that fits best, or the room's only one, may answer a message that mentions no one.",
    "mentions": (
        "Agents and teams answer messages that mention them, and follow-ups in threads they already replied in "
        "or are still answering."
    ),
}


def _write_failure(taken: bool | None) -> str:
    return moderator_rights_message("change the reply mode") if taken is None else _WRITE_FAILED


def _show_room_reply_mode(config: Config, runtime_paths: RuntimePaths, room_id: str, read: ReplyModeRead) -> str:
    mode = read.mode
    if mode is None:
        configured = config.get_room_reply_mode(runtime_paths, room_id, read)
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
    put_state: Callable[..., Awaitable[nio.RoomPutStateResponse | nio.RoomPutStateError | None]],
) -> str:
    """Show, set, or clear the room's `reply_mode` setting, written through ``put_state``, the room steward's."""
    requested = args_text.strip().lower()
    # A command reads the room's state itself, so it never trusts a copy that missed a change.
    read = await refresh_room_reply_mode(client, runtime_paths, room_id, force=True, put_state=put_state)
    if not requested or requested in _LIST_ARGUMENTS:
        if not read.ok:
            return "❌ Could not read this room's reply mode setting. Try again."
        return _show_room_reply_mode(config, runtime_paths, room_id, read)

    if requested not in ROOM_REPLY_MODES and requested not in _RESET_ARGUMENTS:
        return f"❌ Unknown reply mode `{args_text.strip()}`.\n\n{_USAGE}"

    if await room_admin_power_user(client, room_id, (requester_user_id, sender_user_id)) is None:
        return "❌ Room admin only."

    if requested in _RESET_ARGUMENTS:
        if read.ok and read.mode is None:
            return "This room has no reply mode override."
        if not (taken := await write_room_reply_mode(client, runtime_paths, room_id, None, put_state=put_state)):
            return _write_failure(taken)
        configured = config.get_room_reply_mode(runtime_paths, room_id, ReplyModeRead(ok=True, mode=None))
        return f"✅ Room reply mode override removed. This room uses its configured `{configured}` reply mode again."

    mode = cast("RoomReplyMode", requested)
    if not (taken := await write_room_reply_mode(client, runtime_paths, room_id, mode, put_state=put_state)):
        return _write_failure(taken)
    return (
        f"✅ This room now uses `{mode}` reply mode.\n\n{_MODE_EFFECTS[mode]}\n\n"
        "Use `/reply_mode reset` to restore the configured mode."
    )

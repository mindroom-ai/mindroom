"""Chat-based room thread mode override handling for the `!thread_mode` command."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from mindroom.matrix.client_room_admin import room_admin_power_user
from mindroom.matrix.room_thread_mode import reconcile_room_thread_mode
from mindroom.room_settings import (
    ROOM_THREAD_MODES,
    THREAD_MODE,
    refresh_room_setting,
    request_thread_mode_republish,
    write_room_setting,
)
from mindroom.steward_duties import moderator_rights_message, setting_write_failure_message

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    import nio

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.room_settings import RoomThreadMode

_LIST_ARGUMENTS = frozenset({"list", "show", "status"})
_RESET_ARGUMENTS = frozenset({"reset", "clear"})
_USAGE = "Usage: `/thread_mode [room|thread|reset|show]`"


def _show_room_thread_mode(mode: RoomThreadMode | None) -> str:
    if mode is None:
        return (
            "No room thread mode override is set. "
            "Agents use their configured `thread_mode` and `room_thread_modes` values.\n\n"
            f"{_USAGE}"
        )
    return f"This room uses the `{mode}` thread mode override.\n\nUse `/thread_mode reset` to restore configured modes."


def _with_publication(reply: str, published: bool | None, *, changed: bool) -> str:
    """Add to a reply whether the room's steward could publish the thread mode clients show."""
    if published:
        return f"✅ {reply}" if changed else reply
    if published is None:
        return f"⚠️ {reply}\n\n{moderator_rights_message("show clients this room's thread mode")}"
    return (
        f"⚠️ {reply}\n\nClients may still show the old thread mode in this room because publishing the new one failed. "
        "Run the command again to retry."
    )


async def handle_thread_mode_command(
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
    """Show, set, or clear the room's `thread_mode` setting, written through ``put_state``, the room steward's."""
    requested = args_text.strip().lower()
    # A command reads the room's state itself, so it never trusts a copy that missed a change.
    read = await refresh_room_setting(THREAD_MODE, client, runtime_paths, room_id, force=True, put_state=put_state)
    if not requested or requested in _LIST_ARGUMENTS:
        if not read.ok:
            return "❌ Could not read this room's thread mode setting. Try again."
        return _show_room_thread_mode(read.value)

    if requested not in ROOM_THREAD_MODES and requested not in _RESET_ARGUMENTS:
        return f"❌ Unknown thread mode `{args_text.strip()}`.\n\n{_USAGE}"

    if await room_admin_power_user(client, room_id, (requester_user_id, sender_user_id)) is None:
        return "❌ Room admin only."

    mode = None if requested in _RESET_ARGUMENTS else cast("RoomThreadMode", requested)
    # A reset that finds no override writes nothing, and still publishes a mode an earlier run could not.
    removed = not read.ok or read.value is not None
    if mode is not None or removed:
        taken = await write_room_setting(THREAD_MODE, client, runtime_paths, room_id, mode, put_state=put_state)
        if not taken:
            return setting_write_failure_message(taken, "change the thread mode")
    # The room took the setting, which agents follow from here on whether or not clients can be shown the mode.
    published = await reconcile_room_thread_mode(client, config, runtime_paths, room_id, put_state=put_state)
    if published is False:
        # Nothing else republishes a mode whose change this bot's sync does not show: the room's steward retries.
        request_thread_mode_republish(runtime_paths, room_id)
    if mode is not None:
        reply = (
            f"This room now uses `{mode}` thread mode for future agent replies.\n\n"
            "Use `/thread_mode reset` to restore configured modes."
        )
    elif removed:
        reply = "Room thread mode override removed. Agents use their configured modes again."
    else:
        reply = "This room has no thread mode override."
    return _with_publication(reply, published, changed=mode is not None or removed)

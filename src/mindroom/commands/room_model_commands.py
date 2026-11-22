"""Chat-based room model override handling for the `!room_model` command."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.commands.model_commands import available_models_text
from mindroom.matrix.client_room_admin import room_admin_power_user
from mindroom.room_settings import MODEL, classify_room_model, refresh_room_setting, write_room_setting
from mindroom.steward_duties import setting_write_failure_message

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    import nio

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths

_RESET_ARGUMENTS = frozenset({"reset", "clear"})
_LIST_ARGUMENTS = frozenset({"list", "show", "status"})


def _show_room_model(config: Config, setting: str | None) -> str:
    state = classify_room_model(setting, config.models)
    if state.active is not None:
        model = config.models[state.active]
        current = f"This room uses the `{state.active}` override ({model.provider} {model.id})."
    elif state.stale is not None:
        current = f"Stored room model override `{state.stale}` is unavailable and ignored."
    else:
        current = (
            "No room model override is set; configured room or entity models define the room default. "
            "Thread model overrides still take precedence."
        )
    return (
        f"{current}\n\n**Available models:**\n{available_models_text(config)}\n\n"
        "Use `/room_model <name>` or `/room_model <name>@<level>` to switch this room, "
        "or `/room_model reset` to remove the override."
    )


async def handle_room_model_command(  # noqa: PLR0911 - one answer per outcome
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
    """Show, set, or clear the room's `model` setting, written through ``put_state``, the room steward's."""
    requested = args_text.strip()
    # A command reads the room's state itself, so it never trusts a copy that missed a change.
    read = await refresh_room_setting(MODEL, client, runtime_paths, room_id, force=True, put_state=put_state)
    if requested not in config.models:
        if not requested or requested.lower() in _LIST_ARGUMENTS:
            if not read.ok:
                return "❌ Could not read this room's model setting. Try again."
            return _show_room_model(config, read.value)
        if requested.lower() not in _RESET_ARGUMENTS:
            return f"❌ Unknown model `{requested}`. Available models:\n{available_models_text(config)}"

    if await room_admin_power_user(client, room_id, (requester_user_id, sender_user_id)) is None:
        return "❌ Room admin only."

    model_name = requested if requested in config.models else None
    if model_name is None and read.ok and read.value is None:
        return "This room has no model override."
    taken = await write_room_setting(MODEL, client, runtime_paths, room_id, model_name, put_state=put_state)
    if not taken:
        return setting_write_failure_message(taken, "change the room model")
    if model_name is None:
        return (
            "✅ Room model override removed. Room default returns to configured room or entity models. "
            "Thread model overrides still take precedence."
        )
    model = config.models[model_name]
    return (
        f"✅ This room now uses `{model_name}` ({model.provider} {model.id}) as its model default.\n"
        "Thread model overrides still take precedence."
    )

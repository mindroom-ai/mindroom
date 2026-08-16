"""Chat-based room model override handling for the `!room_model` command."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.matrix.client_room_admin import room_admin_power_user
from mindroom.room_model_overrides import (
    clear_room_model_override,
    resolve_room_model_override,
    set_room_model_override,
)

if TYPE_CHECKING:
    import nio

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths

_RESET_ARGUMENTS = frozenset({"reset", "clear"})
_LIST_ARGUMENTS = frozenset({"list", "show", "status"})


def _available_models_text(config: Config) -> str:
    return "\n".join(f"- `{name}` ({model.provider} {model.id})" for name, model in config.models.items())


def _show_room_model(config: Config, runtime_paths: RuntimePaths, room_id: str) -> str:
    state = resolve_room_model_override(runtime_paths, room_id, configured_models=config.models)
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
        f"{current}\n\n**Available models:**\n{_available_models_text(config)}\n\n"
        "Use `!room_model <name>` to switch this room, or `!room_model reset` to remove the override."
    )


async def handle_room_model_command(
    args_text: str,
    *,
    client: nio.AsyncClient,
    config: Config,
    runtime_paths: RuntimePaths,
    room_id: str,
    requester_user_id: str,
    sender_user_id: str,
) -> str:
    """Set or clear one room's runtime model override."""
    requested = args_text.strip()
    if requested not in config.models:
        if not requested or requested.lower() in _LIST_ARGUMENTS:
            return _show_room_model(config, runtime_paths, room_id)
        if requested.lower() not in _RESET_ARGUMENTS:
            return f"❌ Unknown model `{requested}`. Available models:\n{_available_models_text(config)}"

    admin_user_id = await room_admin_power_user(client, room_id, (requester_user_id, sender_user_id))
    if admin_user_id is None:
        return "❌ Room admin only."

    if requested not in config.models:
        if clear_room_model_override(runtime_paths, room_id):
            return (
                "✅ Room model override removed. Room default returns to configured room or entity models. "
                "Thread model overrides still take precedence."
            )
        return "This room has no model override."

    set_room_model_override(
        runtime_paths,
        room_id=room_id,
        model_name=requested,
        set_by=admin_user_id,
    )
    model = config.models[requested]
    return (
        f"✅ This room now uses `{requested}` ({model.provider} {model.id}) as its model default.\n"
        "Thread model overrides still take precedence."
    )

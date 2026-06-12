"""Chat-based per-thread model override handling for the `!model` command."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.thread_models import (
    clear_thread_model_override,
    resolve_thread_model_override,
    set_thread_model_override,
)

if TYPE_CHECKING:
    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths

_RESET_ARGUMENTS = frozenset({"reset", "clear"})
_LIST_ARGUMENTS = frozenset({"list", "show"})
_THREAD_REQUIRED_MESSAGE = (
    "❌ `!model` overrides only work inside a thread. Start a thread (or reply in one) and run it there."
)


def _available_models_text(config: Config) -> str:
    return "\n".join(f"- `{name}` ({model.provider} {model.id})" for name, model in config.models.items())


def _show_thread_model(config: Config, runtime_paths: RuntimePaths, thread_id: str | None) -> str:
    override = resolve_thread_model_override(runtime_paths, thread_id, configured_models=config.models).active
    if override is not None:
        model = config.models[override]
        current = f"This thread uses the `{override}` override ({model.provider} {model.id})."
    else:
        current = "No thread model override is set; agents use their configured models."
    return (
        f"{current}\n\n**Available models:**\n{_available_models_text(config)}\n\n"
        "Use `!model <name>` inside a thread to switch it, or `!model reset` to remove the override."
    )


def handle_model_command(  # noqa: PLR0911
    args_text: str,
    *,
    config: Config,
    runtime_paths: RuntimePaths,
    room_id: str,
    thread_id: str | None,
    requester_user_id: str,
) -> str:
    """Show, set, or clear the model override for one Matrix thread."""
    requested = args_text.strip()
    # Configured model names win over list/reset aliases, so a model named
    # "list", "reset", or "default" stays reachable by its exact name.
    if requested not in config.models:
        if not requested or requested.lower() in _LIST_ARGUMENTS:
            return _show_thread_model(config, runtime_paths, thread_id)
        if thread_id is None:
            return _THREAD_REQUIRED_MESSAGE
        if requested.lower() in _RESET_ARGUMENTS:
            if clear_thread_model_override(runtime_paths, thread_id):
                return "✅ Thread model override removed. Agents use their configured models again."
            return "This thread has no model override."
        return f"❌ Unknown model `{requested}`. Available models:\n{_available_models_text(config)}"
    if thread_id is None:
        return _THREAD_REQUIRED_MESSAGE
    set_thread_model_override(
        runtime_paths,
        thread_id=thread_id,
        model_name=requested,
        room_id=room_id,
        set_by=requester_user_id,
    )
    model = config.models[requested]
    return (
        f"✅ This thread now uses `{requested}` ({model.provider} {model.id}) for all agents and teams.\n"
        "Use `!model reset` to restore the configured models."
    )

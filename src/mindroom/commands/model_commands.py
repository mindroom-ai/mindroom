"""Chat-based per-thread model override handling for the `!model` command."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.authorization import addressable_responder_names
from mindroom.model_selection import ModelSelectionRequest, model_selection_result, parse_model_selection
from mindroom.model_selection_scope import ModelPickerScope, validate_model_picker_scope
from mindroom.thread_models import (
    clear_thread_model_override,
    resolve_thread_model_override,
    set_thread_model_override,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

    import nio

    from mindroom.agent_reply_membership import AgentReplyMembershipIndex
    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths

_RESET_ARGUMENTS = frozenset({"reset", "clear"})
_LIST_ARGUMENTS = frozenset({"list", "show"})
_THREAD_REQUIRED_MESSAGE = (
    "❌ `/model` overrides only work inside a thread. Start a thread (or reply in one) and run it there."
)


def _apply_model_selection(
    request: ModelSelectionRequest,
    *,
    config: Config,
    runtime_paths: RuntimePaths,
    membership_index: AgentReplyMembershipIndex,
    room_id: str,
    thread_id: str,
    requester_user_id: str,
) -> tuple[str, str | None, bool]:
    """Persist one explicit operation synchronously; return text, any error, and whether it wrote or removed an override."""
    entity_names = addressable_responder_names(requester_user_id, room_id, config, runtime_paths, membership_index)
    if request.operation == "reset":
        text, removed = _clear_thread_model(config, runtime_paths, thread_id, entity_names)
        return text, None, removed
    if request.model not in config.models:
        error = f"Unknown model `{request.model}`. Refresh the model picker."
        return f"❌ {error}", error, False
    text = _set_thread_model(
        request.model,
        config=config,
        runtime_paths=runtime_paths,
        entity_names=entity_names,
        thread_id=thread_id,
    )
    return text, None, True


def _clear_thread_model(
    config: Config,
    runtime_paths: RuntimePaths,
    thread_id: str,
    entity_names: tuple[str, ...],
) -> tuple[str, bool]:
    """Remove the override of the entities the requester may address; describe it, and say whether one was removed."""
    if clear_thread_model_override(runtime_paths, thread_id, entity_names=entity_names, config=config):
        return (
            "✅ Thread model override removed for the agents and teams you may address; "
            "room-level model selection applies to them again."
        ), True
    return "This thread has no model override for the agents and teams you may address.", False


def _set_thread_model(
    model_name: str,
    *,
    config: Config,
    runtime_paths: RuntimePaths,
    entity_names: tuple[str, ...],
    thread_id: str,
) -> str:
    """Persist the override for the entities the requester may address and describe it for either command path."""
    set_thread_model_override(
        runtime_paths,
        thread_id=thread_id,
        model_name=model_name,
        entity_names=entity_names,
        config=config,
    )
    model = config.models[model_name]
    return (
        f"✅ This thread now uses `{model_name}` ({model.provider} {model.id}) for the agents and teams you may address.\n"
        "Use `/model reset` to restore room-level model selection."
    )


async def handle_structured_model_command(
    content: Mapping[str, object],
    *,
    client: nio.AsyncClient,
    config: Config,
    runtime_paths: RuntimePaths,
    membership_index: AgentReplyMembershipIndex,
    room_id: str,
    thread_id: str | None,
    requester_user_id: str,
    command_event_id: str,
) -> tuple[str, dict | None]:
    """Validate the structured command and actual scope before synchronous mutation."""
    try:
        request = parse_model_selection(content)
    except ValueError as exc:
        return f"❌ {exc}", None
    assert request is not None
    if request.runtime_user_id != client.user_id or request.runtime_device_id != client.device_id:
        return "", None
    scope = (
        await validate_model_picker_scope(
            client=client,
            config=config,
            runtime_paths=runtime_paths,
            membership_index=membership_index,
            room_id=room_id,
            requester_user_id=requester_user_id,
            thread_id=thread_id,
        )
        if thread_id is not None
        else None
    )
    if not isinstance(scope, ModelPickerScope) or thread_id is None:
        error = "This thread is unavailable for model selection. Refresh the model picker."
        text = f"❌ {error}"
    else:
        # The picker's result acknowledges the operation, also a reset that had nothing to remove.
        text, error, _ = _apply_model_selection(
            request,
            config=config,
            runtime_paths=runtime_paths,
            membership_index=membership_index,
            room_id=room_id,
            thread_id=thread_id,
            requester_user_id=requester_user_id,
        )
    return text, model_selection_result(
        request,
        command_event_id=command_event_id,
        room_id=room_id,
        thread_id=thread_id,
        error=error,
    )


def available_models_text(config: Config) -> str:
    """List the configured models with their provider, ID, and reasoning levels."""
    return "\n".join(
        f"- `{name}` ({model.provider} {model.id}{f'; {model.reasoning.describe()}' if model.reasoning else ''})"
        for name, model in config.models.items()
    )


def _show_thread_model(config: Config, runtime_paths: RuntimePaths, thread_id: str | None) -> str:
    overrides = resolve_thread_model_override(runtime_paths, thread_id, config=config).active
    entities_by_model: dict[str, list[str]] = {}
    for entity_name, model_name in sorted(overrides.items()):
        entities_by_model.setdefault(model_name, []).append(f"`{entity_name}`")
    if entities_by_model:
        current = (
            "This thread overrides the model of these entities; every other entity uses room-level model selection:"
        )
        for model_name, entity_names in entities_by_model.items():
            model = config.models[model_name]
            current += f"\n- `{model_name}` ({model.provider} {model.id}) for {', '.join(entity_names)}"
    else:
        current = "No thread model override is set; room-level model selection applies."
    return (
        f"{current}\n\n**Available models:**\n{available_models_text(config)}\n\n"
        "Use `/model <name>` or `/model <name>@<level>` inside a thread to switch it, "
        "or `/model reset` to remove the override."
    )


def handle_model_command(
    args_text: str,
    *,
    client: nio.AsyncClient,
    config: Config,
    runtime_paths: RuntimePaths,
    membership_index: AgentReplyMembershipIndex,
    room_id: str,
    thread_id: str | None,
    requester_user_id: str,
    command_event_id: str,
) -> tuple[str, dict | None]:
    """Show, set, or clear the model override for one Matrix thread.

    A set, or a clear that removed an override, returns the picker's result too, tied to its command event, so a
    client can pair the reply with the command it answers; a clear with nothing to remove stays an ordinary reply.
    The result names only what the command named: the room reads it, and the models the thread's agents inherit
    are told only to a requester who may address them.
    """
    requested = args_text.strip()
    # Configured model names win over list/reset aliases, so a model named
    # "list", "reset", or "default" stays reachable by its exact name.
    named_model = requested in config.models
    if not named_model and (not requested or requested.lower() in _LIST_ARGUMENTS):
        return _show_thread_model(config, runtime_paths, thread_id), None
    if thread_id is None:
        return _THREAD_REQUIRED_MESSAGE, None
    if not named_model and requested.lower() not in _RESET_ARGUMENTS:
        return f"❌ Unknown model `{requested}`. Available models:\n{available_models_text(config)}", None
    request = ModelSelectionRequest(
        client.user_id,
        client.device_id or "",
        "set" if named_model else "reset",
        requested if named_model else None,
    )
    text, _, changed = _apply_model_selection(
        request,
        config=config,
        runtime_paths=runtime_paths,
        membership_index=membership_index,
        room_id=room_id,
        thread_id=thread_id,
        requester_user_id=requester_user_id,
    )
    if not changed or not client.device_id:
        return text, None
    return text, model_selection_result(
        request,
        command_event_id=command_event_id,
        room_id=room_id,
        thread_id=thread_id,
    )

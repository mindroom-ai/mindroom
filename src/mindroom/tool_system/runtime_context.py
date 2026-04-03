"""Shared runtime context for tool calls."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import TYPE_CHECKING
from uuid import uuid4

from mindroom.hooks import (
    CustomEventContext,
    HookRegistry,
    build_hook_room_state_putter,
    build_hook_room_state_querier,
    emit,
)
from mindroom.hooks.types import validate_event_name, validate_plugin_name
from mindroom.logging_config import get_logger

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    import nio

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.hooks.sender import HookMessageSender
    from mindroom.hooks.types import HookRoomStatePutter, HookRoomStateQuerier
    from mindroom.tool_system.worker_routing import ToolExecutionIdentity


@dataclass(frozen=True)
class ToolRuntimeContext:
    """Shared runtime metadata available to all tools."""

    agent_name: str
    room_id: str
    thread_id: str | None
    resolved_thread_id: str | None
    requester_id: str
    client: nio.AsyncClient
    config: Config
    runtime_paths: RuntimePaths
    active_model_name: str | None = None
    session_id: str | None = None
    room: nio.MatrixRoom | None = None
    reply_to_event_id: str | None = None
    storage_path: Path | None = None
    attachment_ids: tuple[str, ...] = field(default_factory=tuple)
    runtime_attachment_ids: list[str] = field(default_factory=list)
    hook_registry: HookRegistry = field(default_factory=HookRegistry.empty)
    correlation_id: str | None = None
    hook_message_sender: HookMessageSender | None = None
    room_state_querier: HookRoomStateQuerier | None = None
    room_state_putter: HookRoomStatePutter | None = None
    message_received_depth: int = 0


@dataclass(frozen=True)
class ToolRuntimeHookBindings:
    """Resolved hook-facing bindings derived from one tool runtime context."""

    message_sender: HookMessageSender | None
    room_state_querier: HookRoomStateQuerier | None
    room_state_putter: HookRoomStatePutter | None
    message_received_depth: int


_TOOL_RUNTIME_CONTEXT: ContextVar[ToolRuntimeContext | None] = ContextVar(
    "tool_runtime_context",
    default=None,
)


def get_tool_runtime_context() -> ToolRuntimeContext | None:
    """Get the current shared tool runtime context."""
    return _TOOL_RUNTIME_CONTEXT.get()


def resolve_tool_runtime_hook_bindings(context: ToolRuntimeContext) -> ToolRuntimeHookBindings:
    """Return the canonical hook-facing bindings for one tool runtime context."""
    return ToolRuntimeHookBindings(
        message_sender=context.hook_message_sender,
        room_state_querier=context.room_state_querier or build_hook_room_state_querier(context.client),
        room_state_putter=context.room_state_putter or build_hook_room_state_putter(context.client),
        message_received_depth=context.message_received_depth,
    )


def resolve_current_session_id(
    *,
    execution_identity: ToolExecutionIdentity | None = None,
    runtime_context: ToolRuntimeContext | None = None,
) -> str | None:
    """Resolve the current session ID from explicit execution/runtime state."""
    if execution_identity is not None and execution_identity.session_id is not None:
        return execution_identity.session_id

    resolved_runtime_context = runtime_context if runtime_context is not None else get_tool_runtime_context()
    if resolved_runtime_context is not None and resolved_runtime_context.session_id is not None:
        return resolved_runtime_context.session_id

    return None


def attachment_id_available_in_tool_runtime_context(
    context: ToolRuntimeContext,
    attachment_id: str,
) -> bool:
    """Return whether an attachment ID is currently available in context."""
    normalized_attachment_id = attachment_id.strip()
    if not normalized_attachment_id:
        return False
    return (
        normalized_attachment_id in context.attachment_ids or normalized_attachment_id in context.runtime_attachment_ids
    )


def list_tool_runtime_attachment_ids(context: ToolRuntimeContext) -> list[str]:
    """Return all attachment IDs currently available in runtime context order."""
    attachment_ids: list[str] = []
    for attachment_id in (*context.attachment_ids, *context.runtime_attachment_ids):
        if attachment_id and attachment_id not in attachment_ids:
            attachment_ids.append(attachment_id)
    return attachment_ids


def append_tool_runtime_attachment_id(attachment_id: str) -> ToolRuntimeContext | None:
    """Append an attachment ID to the current tool context, preserving order."""
    context = get_tool_runtime_context()
    if context is None:
        return None

    normalized_attachment_id = attachment_id.strip()
    if not normalized_attachment_id:
        return context
    if attachment_id_available_in_tool_runtime_context(context, normalized_attachment_id):
        return context

    context.runtime_attachment_ids.append(normalized_attachment_id)
    return context


def get_plugin_state_root(
    plugin_name: str,
    *,
    runtime_paths: RuntimePaths | None = None,
) -> Path:
    """Return the canonical plugin state root used by hooks and plugin tools."""
    normalized_plugin_name = validate_plugin_name(plugin_name)

    context = get_tool_runtime_context()
    resolved_runtime_paths = runtime_paths or (context.runtime_paths if context is not None else None)
    if resolved_runtime_paths is None:
        msg = "runtime_paths are required when no tool runtime context is active"
        raise RuntimeError(msg)

    plugin_root = resolved_runtime_paths.storage_root / "plugins" / normalized_plugin_name
    plugin_root.mkdir(parents=True, exist_ok=True)
    return plugin_root


async def emit_custom_event(
    plugin_name: str,
    event_name: str,
    payload: dict[str, object],
) -> None:
    """Emit a namespaced custom hook event from tool code on the primary process."""
    validate_event_name(event_name)
    context = get_tool_runtime_context()
    if context is None:
        msg = "emit_custom_event() requires an active tool runtime context"
        raise RuntimeError(msg)
    if not context.hook_registry.has_hooks(event_name):
        return

    correlation_id = context.correlation_id or f"{event_name}:{uuid4().hex}"
    bindings = resolve_tool_runtime_hook_bindings(context)
    hook_context = CustomEventContext(
        event_name=event_name,
        plugin_name="",
        settings={},
        config=context.config,
        runtime_paths=context.runtime_paths,
        logger=get_logger("mindroom.hooks.tools").bind(event_name=event_name),
        correlation_id=correlation_id,
        message_sender=bindings.message_sender,
        room_state_querier=bindings.room_state_querier,
        room_state_putter=bindings.room_state_putter,
        payload=payload,
        source_plugin=plugin_name,
        room_id=context.room_id,
        thread_id=context.resolved_thread_id or context.thread_id,
        sender_id=context.requester_id,
        message_received_depth=bindings.message_received_depth,
    )
    await emit(context.hook_registry, event_name, hook_context)


@contextmanager
def tool_runtime_context(context: ToolRuntimeContext | None) -> Iterator[None]:
    """Set shared tool runtime context for the current async execution scope."""
    token = _TOOL_RUNTIME_CONTEXT.set(context)
    try:
        yield
    finally:
        _TOOL_RUNTIME_CONTEXT.reset(token)

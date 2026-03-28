"""Shared runtime context for tool calls."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import TYPE_CHECKING
from uuid import uuid4

from mindroom.hooks import CustomEventContext, HookRegistry, emit
from mindroom.hooks.types import validate_event_name
from mindroom.logging_config import get_logger

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    import nio

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.hooks.sender import HookMessageSender


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
    room: nio.MatrixRoom | None = None
    reply_to_event_id: str | None = None
    storage_path: Path | None = None
    attachment_ids: tuple[str, ...] = field(default_factory=tuple)
    runtime_attachment_ids: list[str] = field(default_factory=list)
    hook_registry: HookRegistry = field(default_factory=HookRegistry.empty)
    correlation_id: str | None = None
    hook_message_sender: HookMessageSender | None = None


_TOOL_RUNTIME_CONTEXT: ContextVar[ToolRuntimeContext | None] = ContextVar(
    "tool_runtime_context",
    default=None,
)


def get_tool_runtime_context() -> ToolRuntimeContext | None:
    """Get the current shared tool runtime context."""
    return _TOOL_RUNTIME_CONTEXT.get()


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
    normalized_plugin_name = plugin_name.strip()
    if not normalized_plugin_name:
        msg = "Plugin name must not be empty"
        raise ValueError(msg)

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
    hook_context = CustomEventContext(
        event_name=event_name,
        plugin_name="",
        settings={},
        config=context.config,
        runtime_paths=context.runtime_paths,
        logger=get_logger("mindroom.hooks.tools").bind(event_name=event_name),
        correlation_id=correlation_id,
        message_sender=context.hook_message_sender,
        payload=payload,
        source_plugin=plugin_name,
        room_id=context.room_id,
        thread_id=context.resolved_thread_id or context.thread_id,
        sender_id=context.requester_id,
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

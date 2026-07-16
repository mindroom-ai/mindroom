"""Shared live-context validation for external-trigger tools."""

from __future__ import annotations

from mindroom.tool_system.runtime_context import ToolRuntimeContext, get_tool_runtime_context


class ExternalTriggerContextError(RuntimeError):
    """Raised when an external-trigger tool lacks its required live context."""


def require_external_trigger_owner_context(tool_name: str) -> ToolRuntimeContext:
    """Return the primary-runtime context for one human-owned trigger tool call."""
    context = get_tool_runtime_context()
    if context is None:
        msg = f"{tool_name} requires live Matrix tool context."
        raise ExternalTriggerContextError(msg)
    if context.runtime_paths.control_state_root is None:
        msg = f"{tool_name} requires primary control state."
        raise ExternalTriggerContextError(msg)
    if not context.requester_id or context.requester_id == context.client.user_id:
        msg = "External trigger owner must be a human Matrix requester."
        raise ExternalTriggerContextError(msg)
    return context

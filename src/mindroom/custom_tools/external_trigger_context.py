"""Shared live-context validation for external-trigger tools."""

from __future__ import annotations

from dataclasses import replace

from mindroom.external_triggers.store import require_human_owner
from mindroom.tool_system.runtime_context import ToolRuntimeContext, get_tool_runtime_context


class ExternalTriggerContextError(RuntimeError):
    """Raised when an external-trigger tool lacks its required live context."""


def require_external_trigger_owner_context(tool_name: str) -> ToolRuntimeContext:
    """Return the live-config context of one trigger tool call whose requester may own triggers."""
    context = get_tool_runtime_context()
    if context is None:
        msg = f"{tool_name} requires live Matrix tool context."
        raise ExternalTriggerContextError(msg)
    context = replace(context, config=context.current_config, config_provider=None)
    require_human_owner(context.requester_id, context.config, context.runtime_paths)
    return context

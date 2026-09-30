"""Public catalog facade for tool metadata and registry access."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.tool_system.bootstrap import ensure_tool_registry_loaded
from mindroom.tool_system.declarations import (
    ConfigField,
    SetupType,
    ToolAuthoredOverrideValidator,
    ToolCategory,
    ToolFileAccess,
    ToolManagedInitArg,
    ToolMetadata,
    ToolStatus,
    ToolValidationInfo,
)
from mindroom.tool_system.metadata import (
    TOOL_METADATA,
    ToolConfigOverrideError,
    ToolInitOverrideError,
    ToolMetadataValidationError,
    apply_authored_overrides,
    authored_tool_overrides_to_runtime,
    clear_resolved_tool_state_cache,
    deserialize_tool_validation_snapshot,
    export_tools_metadata,
    get_tool_by_name,
    normalize_authored_tool_overrides,
    resolved_tool_metadata_for_runtime,
    resolved_tool_validation_snapshot_for_runtime,
    safe_tool_init_override_fields,
    sanitize_tool_init_overrides,
    serialize_tool_validation_snapshot,
    unresolved_plugin_tool_sources_for_runtime,
    validate_authored_tool_entry_overrides,
)
from mindroom.tool_system.sandbox_proxy import tool_builds_in_primary

if TYPE_CHECKING:
    from mindroom.constants import RuntimePaths

__all__ = [
    "TOOL_METADATA",
    "ConfigField",
    "SetupType",
    "ToolAuthoredOverrideValidator",
    "ToolCategory",
    "ToolConfigOverrideError",
    "ToolFileAccess",
    "ToolInitOverrideError",
    "ToolManagedInitArg",
    "ToolMetadata",
    "ToolMetadataValidationError",
    "ToolStatus",
    "ToolValidationInfo",
    "agent_tool_builds_in_primary",
    "apply_authored_overrides",
    "authored_tool_overrides_to_runtime",
    "clear_resolved_tool_state_cache",
    "deserialize_tool_validation_snapshot",
    "ensure_tool_registry_loaded",
    "export_tools_metadata",
    "get_tool_by_name",
    "normalize_authored_tool_overrides",
    "resolved_tool_metadata_for_runtime",
    "resolved_tool_validation_snapshot_for_runtime",
    "safe_tool_init_override_fields",
    "sanitize_tool_init_overrides",
    "serialize_tool_validation_snapshot",
    "unresolved_plugin_tool_sources_for_runtime",
    "validate_authored_tool_entry_overrides",
]


def agent_tool_builds_in_primary(
    tool_name: str,
    *,
    runtime_paths: RuntimePaths,
    worker_tools: list[str] | None,
) -> bool:
    """Return whether an agent with these worker tools has the primary process build this tool itself."""
    ensure_tool_registry_loaded(runtime_paths)
    return tool_builds_in_primary(tool_name, runtime_paths=runtime_paths, worker_tools_override=worker_tools)

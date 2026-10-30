"""Public catalog facade for tool metadata and registry access."""

from __future__ import annotations

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
    ToolConfigOverrideError,
    ToolInitOverrideError,
    apply_authored_overrides,
    deserialize_tool_validation_snapshot,
    export_tools_metadata,
    get_tool_by_name,
    load_toolkit_class,
    safe_tool_init_override_fields,
    sanitize_tool_init_overrides,
    serialize_tool_validation_snapshot,
    validate_authored_tool_entry_overrides,
)
from mindroom.tool_system.registry import ToolMetadataValidationError, builtin_tool_registry

__all__ = [
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
    "apply_authored_overrides",
    "builtin_tool_registry",
    "deserialize_tool_validation_snapshot",
    "export_tools_metadata",
    "get_tool_by_name",
    "load_toolkit_class",
    "safe_tool_init_override_fields",
    "sanitize_tool_init_overrides",
    "serialize_tool_validation_snapshot",
    "validate_authored_tool_entry_overrides",
]

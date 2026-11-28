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
from mindroom.tool_system.metadata import get_tool_by_name
from mindroom.tool_system.registry import ToolMetadataValidationError, builtin_tool_registry
from mindroom.tool_system.settings import (
    ToolConfigOverrideError,
    ToolInitOverrideError,
    deserialize_tool_validation_snapshot,
    export_tools_metadata,
    load_toolkit_class,
    safe_tool_init_override_fields,
    sanitize_tool_init_overrides,
    serialize_tool_validation_snapshot,
    validate_authored_tool_entry_overrides,
)

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

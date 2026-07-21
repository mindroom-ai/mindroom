"""Leaf declarations shared by tool implementations and the runtime catalog."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from collections.abc import Callable


class ToolAuthoredOverrideValidator(str, Enum):
    """Explicit authored-override validation modes for a tool."""

    DEFAULT = "default"
    MCP = "mcp"


class ToolCategory(str, Enum):
    """Tool categories for organization."""

    EMAIL = "email"
    ENTERTAINMENT = "entertainment"
    SOCIAL = "social"
    DEVELOPMENT = "development"
    RESEARCH = "research"
    INFORMATION = "information"
    PRODUCTIVITY = "productivity"
    COMMUNICATION = "communication"
    INTEGRATIONS = "integrations"
    SMART_HOME = "smart_home"


class ToolStatus(str, Enum):
    """Tool availability status."""

    AVAILABLE = "available"
    REQUIRES_CONFIG = "requires_config"


class SetupType(str, Enum):
    """Tool setup type."""

    NONE = "none"
    API_KEY = "api_key"
    OAUTH = "oauth"
    SPECIAL = "special"


class ToolExecutionTarget(str, Enum):
    """Default runtime location for one tool."""

    PRIMARY = "primary"
    WORKER = "worker"


class ToolManagedInitArg(str, Enum):
    """Explicit MindRoom-managed constructor inputs."""

    RUNTIME_PATHS = "runtime_paths"
    CREDENTIALS_MANAGER = "credentials_manager"
    WORKER_TARGET = "worker_target"
    TOOL_OUTPUT_WORKSPACE_ROOT = "tool_output_workspace_root"
    WORKER_TOOLS_OVERRIDE = "worker_tools_override"
    CURRENT_ROOM_ID = "current_room_id"


@dataclass
class ConfigField:
    """Definition of a configuration field."""

    name: str
    label: str
    type: Literal["boolean", "number", "password", "text", "url", "select", "string[]"] = "text"
    required: bool = True
    default: Any = None
    placeholder: str | None = None
    description: str | None = None
    options: list[dict[str, str]] | None = None
    validation: dict[str, Any] | None = None
    authored_override: bool = True
    requester_owned: bool = False


@dataclass(frozen=True)
class ToolValidationInfo:
    """Validation-only metadata for authored tool references."""

    name: str
    config_fields: tuple[ConfigField, ...] = ()
    agent_override_fields: tuple[ConfigField, ...] = ()
    authored_override_validator: ToolAuthoredOverrideValidator = ToolAuthoredOverrideValidator.DEFAULT
    supports_toolkit_filters: bool = False
    requires_room_context: bool = False
    runtime_loadable: bool = True
    unavailable_due_to_plugin_load_error: bool = False


@dataclass
class ToolMetadata:
    """Complete metadata for a tool."""

    name: str
    display_name: str
    description: str
    category: ToolCategory
    status: ToolStatus = ToolStatus.AVAILABLE
    setup_type: SetupType = SetupType.NONE
    default_execution_target: ToolExecutionTarget = ToolExecutionTarget.PRIMARY
    consumes_workspace_paths: bool = False
    requires_room_context: bool = False
    icon: str | None = None
    icon_color: str | None = None
    config_fields: list[ConfigField] | None = None
    agent_override_fields: list[ConfigField] | None = None
    authored_override_validator: ToolAuthoredOverrideValidator = ToolAuthoredOverrideValidator.DEFAULT
    dependencies: list[str] | None = None
    auth_provider: str | None = None
    docs_url: str | None = None
    helper_text: str | None = None
    function_names: tuple[str, ...] = ()
    managed_init_args: tuple[ToolManagedInitArg, ...] = ()
    supports_toolkit_filters: bool = False
    factory: Callable[[], type] | None = None

"""Leaf declarations shared by tool implementations and the runtime catalog."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Any, Literal, Protocol, runtime_checkable
from weakref import ref

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping


MATRIX_ROOM_RUNTIME_APPROVAL_TYPE = "mindroom_matrix_room_runtime"
MATRIX_ROOM_RUNTIME_TOOL_NAMES = ("invite_router",)

_SCHEMA_SOURCE_ATTRIBUTE = "__mindroom_tool_schema_source__"


@dataclass(frozen=True, slots=True)
class _ToolSchemaSource:
    """A wrapper-owned declaration; copied decorator attributes confer no identity."""

    wrapper: ref[Callable[..., object]]
    source: Callable[..., object]


def tool_schema_source(entrypoint: Callable[..., object]) -> Callable[..., object]:
    """Resolve only schema identity explicitly preserved by a MindRoom wrapper."""
    declaration = getattr(entrypoint, _SCHEMA_SOURCE_ATTRIBUTE, None)
    if isinstance(declaration, _ToolSchemaSource) and declaration.wrapper() is entrypoint:
        return declaration.source
    return entrypoint


def declare_tool_schema_source(wrapper: Callable[..., object], source: Callable[..., object]) -> None:
    """Declare the source definition used by an owned tool wrapper."""
    setattr(wrapper, _SCHEMA_SOURCE_ATTRIBUTE, _ToolSchemaSource(ref(wrapper), tool_schema_source(source)))


@runtime_checkable
class SupportsPrimaryCallPlacement(Protocol):
    """Toolkit-owned placement for calls that need the live primary runtime."""

    def runs_on_primary(self, function_name: str, arguments: Mapping[str, object]) -> bool:
        """Return whether one bound call must keep its original primary entrypoint."""
        ...


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
    RUNTIME_CONFIG = "runtime_config"
    TOOL_OUTPUT_WORKSPACE_ROOT = "tool_output_workspace_root"
    WORKER_TOOLS_OVERRIDE = "worker_tools_override"
    CURRENT_ROOM_ID = "current_room_id"
    AGENT_NAME = "agent_name"


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


@dataclass(frozen=True)
class ToolValidationInfo:
    """Validation-only metadata for authored tool references."""

    name: str
    config_fields: tuple[ConfigField, ...] = ()
    agent_override_fields: tuple[ConfigField, ...] = ()
    authored_override_validator: ToolAuthoredOverrideValidator = ToolAuthoredOverrideValidator.DEFAULT
    supports_toolkit_filters: bool = False
    requires_room_context: bool = False
    requires_primary_runtime: bool = False
    runtime_loadable: bool = True
    unavailable_due_to_plugin_load_error: bool = False


@dataclass
class ToolMetadata:
    """Complete metadata for a tool.

    ``requires_room_context`` marks toolkits that need the live Matrix room
    runtime, including its client, requester, and conversation context.
    ``requires_primary_runtime`` prevents worker routing even when an authored
    ``worker_tools`` override selects the tool. It is independent from the
    overridable ``default_execution_target``.
    """

    name: str
    display_name: str
    description: str
    category: ToolCategory
    status: ToolStatus = ToolStatus.AVAILABLE
    setup_type: SetupType = SetupType.NONE
    default_execution_target: ToolExecutionTarget = ToolExecutionTarget.PRIMARY
    requires_primary_runtime: bool = False
    consumes_workspace_paths: bool = False
    requires_room_context: bool = False
    icon: str | None = None
    icon_color: str | None = None
    config_fields: list[ConfigField] | None = None
    agent_override_fields: list[ConfigField] | None = None
    authored_override_validator: ToolAuthoredOverrideValidator = ToolAuthoredOverrideValidator.DEFAULT
    dependencies: list[str] | None = None
    auth_provider: str | None = None
    oauth_fallback_fields: tuple[str, ...] = ()
    docs_url: str | None = None
    helper_text: str | None = None
    function_names: tuple[str, ...] = ()
    # SDK functions that accept, but never use, an injected Agent or Team.
    worker_inert_agent_functions: tuple[str, ...] = ()
    managed_init_args: tuple[ToolManagedInitArg, ...] = ()
    supports_toolkit_filters: bool = False
    factory: Callable[[], type] | None = None

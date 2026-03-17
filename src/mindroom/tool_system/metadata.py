"""Tool metadata and enhanced registration system."""

from __future__ import annotations

import functools
import importlib
import os
from dataclasses import asdict, dataclass
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, cast

from loguru import logger

from mindroom.credentials import get_runtime_credentials_manager, load_scoped_credentials
from mindroom.tool_system.dependencies import auto_install_tool_extra, check_deps_installed
from mindroom.tool_system.plugins import load_plugins
from mindroom.tool_system.sandbox_proxy import maybe_wrap_toolkit_for_sandbox_proxy
from mindroom.tool_system.worker_routing import (
    WorkerScope,
    requires_shared_only_integration_scope,
    unsupported_shared_only_integration_message,
    worker_scope_allows_shared_only_integrations,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from agno.tools import Toolkit

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.credentials import CredentialsManager
    from mindroom.tool_system.worker_routing import ToolExecutionIdentity

# Registry mapping tool names to their factory functions
_TOOL_REGISTRY: dict[str, Callable[[], type[Toolkit]]] = {}
_SAFE_TOOL_INIT_OVERRIDE_FIELDS = frozenset({"base_dir"})


class ToolInitOverrideError(ValueError):
    """Raised when a caller supplies unsupported tool init overrides."""


def _sanitize_safe_tool_init_override_value(
    tool_name: str,
    field_name: str,
    value: object,
) -> object:
    """Validate one safe tool init override value."""
    if field_name == "base_dir":
        if value is None or isinstance(value, str):
            return value
        if isinstance(value, os.PathLike):
            return os.fspath(value)
        msg = f"Unsupported value for tool init override '{tool_name}.{field_name}': expected a string path or null."
        raise ToolInitOverrideError(msg)

    return value


def sanitize_tool_init_overrides(
    tool_name: str,
    tool_init_overrides: dict[str, object] | None,
) -> dict[str, object] | None:
    """Validate and retain only the explicitly safe runtime tool init overrides."""
    if not tool_init_overrides:
        return None

    metadata = TOOL_METADATA[tool_name]
    allowed_fields = {
        field.name for field in metadata.config_fields or [] if field.name in _SAFE_TOOL_INIT_OVERRIDE_FIELDS
    }
    unexpected_fields = sorted(set(tool_init_overrides) - allowed_fields)
    if unexpected_fields:
        allowed = ", ".join(sorted(allowed_fields)) or "none"
        unexpected = ", ".join(unexpected_fields)
        msg = f"Unsupported tool init override(s) for '{tool_name}': {unexpected}. Allowed overrides: {allowed}."
        raise ToolInitOverrideError(msg)

    return {
        name: _sanitize_safe_tool_init_override_value(tool_name, name, tool_init_overrides[name])
        for name in tool_init_overrides
    }


def _build_tool_config_init_kwargs(
    metadata: ToolMetadata,
    *,
    credentials: dict[str, object],
    tool_init_overrides: dict[str, object] | None,
    runtime_overrides: dict[str, object] | None,
) -> dict[str, object]:
    """Collect safe config-field kwargs for one tool constructor."""
    if not metadata.config_fields:
        return {}

    config_field_names = {field.name for field in metadata.config_fields}
    init_kwargs = {field.name: credentials[field.name] for field in metadata.config_fields if field.name in credentials}
    if tool_init_overrides:
        init_kwargs.update(
            {
                field.name: tool_init_overrides[field.name]
                for field in metadata.config_fields
                if field.name in tool_init_overrides
            },
        )
    if runtime_overrides:
        init_kwargs.update(
            {field_name: value for field_name, value in runtime_overrides.items() if field_name in config_field_names},
        )
    if "base_dir" in init_kwargs and isinstance(init_kwargs["base_dir"], str):
        init_kwargs["base_dir"] = Path(init_kwargs["base_dir"])
    return init_kwargs


def _build_managed_tool_init_kwargs(
    metadata: ToolMetadata,
    *,
    runtime_paths: RuntimePaths,
    credentials_manager: CredentialsManager | None,
    worker_scope: WorkerScope | None,
    routing_agent_name: str | None,
    execution_identity: ToolExecutionIdentity | None,
) -> dict[str, object]:
    """Build declared MindRoom-managed constructor kwargs for one tool."""
    init_kwargs: dict[str, object] = {}
    for init_arg in metadata.managed_init_args:
        if init_arg == ToolManagedInitArg.RUNTIME_PATHS:
            init_kwargs[init_arg.value] = runtime_paths
        elif init_arg == ToolManagedInitArg.CREDENTIALS_MANAGER:
            init_kwargs[init_arg.value] = credentials_manager
        elif init_arg == ToolManagedInitArg.WORKER_SCOPE:
            init_kwargs[init_arg.value] = worker_scope
        elif init_arg == ToolManagedInitArg.ROUTING_AGENT_NAME:
            init_kwargs[init_arg.value] = routing_agent_name
        elif init_arg == ToolManagedInitArg.EXECUTION_IDENTITY:
            init_kwargs[init_arg.value] = execution_identity
    return init_kwargs


def _resolve_tool_credentials_manager(
    metadata: ToolMetadata,
    runtime_paths: RuntimePaths,
    credentials_manager: CredentialsManager | None,
) -> CredentialsManager | None:
    """Return the explicit runtime credential manager for tools that persist config."""
    if credentials_manager is not None:
        return credentials_manager

    if metadata.config_fields or ToolManagedInitArg.CREDENTIALS_MANAGER in metadata.managed_init_args:
        return get_runtime_credentials_manager(runtime_paths)
    return None


def _build_tool_instance(
    tool_name: str,
    runtime_paths: RuntimePaths,
    *,
    disable_sandbox_proxy: bool = False,
    credential_overrides: dict[str, object] | None = None,
    credentials_manager: CredentialsManager | None = None,
    tool_init_overrides: dict[str, object] | None = None,
    worker_tools_override: list[str] | None = None,
    runtime_overrides: dict[str, object] | None = None,
    shared_storage_root_path: Path | None = None,
    worker_scope: WorkerScope | None = None,
    routing_agent_name: str | None = None,
    routing_agent_is_private: bool | None = None,
    execution_identity: ToolExecutionIdentity | None,
) -> Toolkit:
    """Instantiate a tool from the registry, applying credentials and sandbox proxy."""
    if requires_shared_only_integration_scope(tool_name) and not worker_scope_allows_shared_only_integrations(
        worker_scope,
    ):
        msg = unsupported_shared_only_integration_message(
            tool_name,
            worker_scope,
            agent_name=routing_agent_name,
            subject="Tool",
        )
        raise ValueError(msg)

    metadata = TOOL_METADATA[tool_name]
    tool_class = _TOOL_REGISTRY[tool_name]()
    resolved_credentials_manager = _resolve_tool_credentials_manager(
        metadata,
        runtime_paths,
        credentials_manager,
    )
    credentials = (
        load_scoped_credentials(
            tool_name,
            worker_scope=worker_scope,
            routing_agent_name=routing_agent_name,
            credentials_manager=resolved_credentials_manager,
            execution_identity=execution_identity,
        )
        if resolved_credentials_manager is not None
        else {}
    ) or {}
    if credential_overrides:
        credentials = {**credentials, **credential_overrides}
    safe_tool_init_overrides = sanitize_tool_init_overrides(tool_name, tool_init_overrides)
    init_kwargs = _build_tool_config_init_kwargs(
        metadata,
        credentials=credentials,
        tool_init_overrides=safe_tool_init_overrides,
        runtime_overrides=runtime_overrides,
    )
    init_kwargs.update(
        _build_managed_tool_init_kwargs(
            metadata,
            runtime_paths=runtime_paths,
            credentials_manager=resolved_credentials_manager,
            worker_scope=worker_scope,
            routing_agent_name=routing_agent_name,
            execution_identity=execution_identity,
        ),
    )

    toolkit = cast("Any", tool_class)(**init_kwargs)
    if disable_sandbox_proxy:
        return toolkit
    return maybe_wrap_toolkit_for_sandbox_proxy(
        tool_name,
        toolkit,
        runtime_paths=runtime_paths,
        credentials_manager=resolved_credentials_manager,
        tool_init_overrides=safe_tool_init_overrides,
        worker_tools_override=worker_tools_override,
        worker_scope=worker_scope,
        routing_agent_name=routing_agent_name,
        routing_agent_is_private=routing_agent_is_private,
        shared_storage_root_path=shared_storage_root_path,
        execution_identity=execution_identity,
    )


def get_tool_by_name(
    tool_name: str,
    runtime_paths: RuntimePaths,
    *,
    disable_sandbox_proxy: bool = False,
    credential_overrides: dict[str, object] | None = None,
    credentials_manager: CredentialsManager | None = None,
    tool_init_overrides: dict[str, object] | None = None,
    worker_tools_override: list[str] | None = None,
    runtime_overrides: dict[str, object] | None = None,
    shared_storage_root_path: Path | None = None,
    worker_scope: WorkerScope | None = None,
    routing_agent_name: str | None = None,
    routing_agent_is_private: bool | None = None,
    execution_identity: ToolExecutionIdentity | None,
) -> Toolkit:
    """Get a tool instance by its registered name."""
    if tool_name not in _TOOL_REGISTRY:
        available = ", ".join(sorted(_TOOL_REGISTRY.keys()))
        msg = f"Unknown tool: {tool_name}. Available tools: {available}"
        raise ValueError(msg)

    build = functools.partial(
        _build_tool_instance,
        tool_name,
        runtime_paths,
        disable_sandbox_proxy=disable_sandbox_proxy,
        credential_overrides=credential_overrides,
        credentials_manager=credentials_manager,
        tool_init_overrides=tool_init_overrides,
        worker_tools_override=worker_tools_override,
        runtime_overrides=runtime_overrides,
        shared_storage_root_path=shared_storage_root_path,
        worker_scope=worker_scope,
        routing_agent_name=routing_agent_name,
        routing_agent_is_private=routing_agent_is_private,
        execution_identity=execution_identity,
    )

    # Pre-check dependencies using find_spec (no side effects) before importing
    metadata = TOOL_METADATA.get(tool_name)
    deps = metadata.dependencies if metadata and metadata.dependencies else []
    if deps and not check_deps_installed(deps):
        if not auto_install_tool_extra(tool_name, runtime_paths):
            missing = ", ".join(deps)
            logger.warning(f"Missing dependencies for tool '{tool_name}': {missing}")
            logger.warning(f"Make sure the required dependencies are installed for {tool_name}")
            msg = f"Missing dependencies for tool '{tool_name}': {missing}"
            raise ImportError(msg)
        logger.info(f"Auto-installed optional dependencies for tool '{tool_name}'")
        importlib.invalidate_caches()

    try:
        return build()
    except ImportError as first_error:
        # Safety net: deps may not be exhaustively listed in metadata
        if not auto_install_tool_extra(tool_name, runtime_paths):
            logger.warning(f"Could not import tool '{tool_name}': {first_error}")
            logger.warning(f"Make sure the required dependencies are installed for {tool_name}")
            raise

        logger.info(f"Auto-installing optional dependencies for tool '{tool_name}'")
        importlib.invalidate_caches()

        try:
            return build()
        except ImportError as second_error:
            logger.warning(f"Auto-install did not resolve dependencies for '{tool_name}': {second_error}")
            raise second_error from first_error


class ToolCategory(str, Enum):
    """Tool categories for organization."""

    EMAIL = "email"
    SHOPPING = "shopping"
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

    NONE = "none"  # No setup required
    API_KEY = "api_key"  # Requires API key
    OAUTH = "oauth"  # OAuth flow
    SPECIAL = "special"  # Special setup (e.g., for Google)


class ToolExecutionTarget(str, Enum):
    """Default runtime location for one tool."""

    PRIMARY = "primary"
    WORKER = "worker"


class ToolManagedInitArg(str, Enum):
    """Explicit MindRoom-managed constructor inputs."""

    RUNTIME_PATHS = "runtime_paths"
    CREDENTIALS_MANAGER = "credentials_manager"
    WORKER_SCOPE = "worker_scope"
    ROUTING_AGENT_NAME = "routing_agent_name"
    EXECUTION_IDENTITY = "execution_identity"


@dataclass
class ConfigField:
    """Definition of a configuration field."""

    name: str  # Environment variable name (e.g., "SMTP_HOST")
    label: str  # Display label (e.g., "SMTP Host")
    type: Literal["boolean", "number", "password", "text", "url", "select"] = "text"
    required: bool = True
    default: Any = None
    placeholder: str | None = None
    description: str | None = None
    options: list[dict[str, str]] | None = None  # For select type
    validation: dict[str, Any] | None = None  # min, max, pattern, etc.


@dataclass
class ToolMetadata:
    """Complete metadata for a tool."""

    name: str  # Internal tool name (e.g., "gmail")
    display_name: str  # Display name (e.g., "Gmail")
    description: str  # Description for UI
    category: ToolCategory
    status: ToolStatus = ToolStatus.AVAILABLE
    setup_type: SetupType = SetupType.NONE
    default_execution_target: ToolExecutionTarget = ToolExecutionTarget.PRIMARY
    icon: str | None = None  # Icon identifier for frontend
    icon_color: str | None = None  # Tailwind color class like "text-blue-500"
    config_fields: list[ConfigField] | None = None  # Detailed field definitions
    dependencies: list[str] | None = None  # Required pip packages
    auth_provider: str | None = None  # Name of integration that provides auth (e.g., "google")
    docs_url: str | None = None  # Documentation URL
    helper_text: str | None = None  # Additional help text for setup
    managed_init_args: tuple[ToolManagedInitArg, ...] = ()  # Explicit MindRoom-managed constructor kwargs
    factory: Callable | None = None  # Factory function to create tool instance


# Global registry for tool metadata
TOOL_METADATA: dict[str, ToolMetadata] = {}


def register_tool_with_metadata(
    *,
    name: str,
    display_name: str,
    description: str,
    category: ToolCategory,
    status: ToolStatus = ToolStatus.AVAILABLE,
    setup_type: SetupType = SetupType.NONE,
    default_execution_target: ToolExecutionTarget = ToolExecutionTarget.PRIMARY,
    icon: str | None = None,
    icon_color: str | None = None,
    config_fields: list[ConfigField] | None = None,
    dependencies: list[str] | None = None,
    auth_provider: str | None = None,
    docs_url: str | None = None,
    helper_text: str | None = None,
    managed_init_args: tuple[ToolManagedInitArg, ...] = (),
) -> Callable[[Callable[[], type]], Callable[[], type]]:
    """Decorator to register a tool with metadata.

    This decorator stores comprehensive metadata about tools that can be used
    by the frontend and other components.

    Args:
        name: Tool identifier used in registry
        display_name: Human-readable name for UI
        description: Brief description of what the tool does
        category: Tool category for organization
        status: Availability status of the tool
        setup_type: Type of setup required
        default_execution_target: Default runtime location for the tool
        icon: Icon identifier for frontend
        icon_color: CSS color class for the icon
        config_fields: List of configuration fields
        dependencies: Required Python packages
        auth_provider: Name of integration that provides authentication
        docs_url: Link to documentation
        helper_text: Additional setup instructions
        managed_init_args: Explicit MindRoom-managed constructor kwargs

    Returns:
        Decorator function

    """

    def decorator(func: Callable) -> Callable:
        # Create metadata object
        metadata = ToolMetadata(
            name=name,
            display_name=display_name,
            description=description,
            category=category,
            status=status,
            setup_type=setup_type,
            default_execution_target=default_execution_target,
            icon=icon,
            icon_color=icon_color,
            config_fields=config_fields,
            dependencies=dependencies,
            auth_provider=auth_provider,
            docs_url=docs_url,
            helper_text=helper_text,
            managed_init_args=managed_init_args,
            factory=func,
        )

        # Store in metadata registry
        TOOL_METADATA[name] = metadata

        # Also register in TOOL_REGISTRY for actual tool loading
        _TOOL_REGISTRY[name] = func

        return func

    return decorator


def ensure_tool_registry_loaded(
    runtime_paths: RuntimePaths,
    config: Config | None = None,
) -> None:
    """Ensure core and plugin tools are registered in the metadata registry."""
    import mindroom.tools  # noqa: F401, PLC0415  # import here to avoid tools_metadata cycle

    if config is None:
        return

    load_plugins(config, runtime_paths)


def default_worker_routed_tools(tool_names: list[str]) -> list[str]:
    """Return the tool names that default to worker execution."""
    selected_tools: list[str] = []
    for tool_name in tool_names:
        metadata = TOOL_METADATA.get(tool_name)
        if metadata is not None and metadata.default_execution_target == ToolExecutionTarget.WORKER:
            selected_tools.append(tool_name)
    return selected_tools


def export_tools_metadata() -> list[dict[str, Any]]:
    """Export tool metadata as JSON-serializable dictionaries."""
    tools: list[dict[str, Any]] = []

    for metadata in TOOL_METADATA.values():
        tool_dict = asdict(metadata)
        tool_dict["category"] = metadata.category.value
        tool_dict["status"] = metadata.status.value
        tool_dict["setup_type"] = metadata.setup_type.value
        tool_dict["default_execution_target"] = metadata.default_execution_target.value
        tool_dict.pop("managed_init_args", None)
        tool_dict.pop("factory", None)
        tools.append(tool_dict)

    tools.sort(key=lambda tool: (tool["category"], tool["name"]))
    return tools

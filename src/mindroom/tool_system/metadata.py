"""Tool metadata and enhanced registration system."""

from __future__ import annotations

import functools
import importlib
import os
import sys
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, cast

from loguru import logger

from mindroom.credentials import get_runtime_credentials_manager, load_scoped_credentials
from mindroom.tool_system import plugins as plugin_module
from mindroom.tool_system.dependencies import auto_install_tool_extra, check_deps_installed
from mindroom.tool_system.plugins import load_plugins
from mindroom.tool_system.sandbox_proxy import maybe_wrap_toolkit_for_sandbox_proxy
from mindroom.tool_system.worker_routing import (
    ResolvedWorkerTarget,
    supports_tool_name_for_worker_scope,
    unsupported_shared_only_integration_message,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from agno.tools import Toolkit

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.credentials import CredentialsManager
# Registry mapping tool names to their factory functions
_TOOL_REGISTRY: dict[str, Callable[[], type[Toolkit]]] = {}
_SAFE_TOOL_INIT_OVERRIDE_FIELDS = frozenset({"base_dir", "shell_path_prepend"})
_TEXT_CONFIG_FIELD_TYPES = frozenset({"password", "select", "text", "url"})
AUTHORED_OVERRIDE_INHERIT = "__MINDROOM_INHERIT__"
_PLUGIN_MODULE_PREFIX = "mindroom_plugin_"


class ToolInitOverrideError(ValueError):
    """Raised when a caller supplies unsupported tool init overrides."""


class ToolConfigOverrideError(ValueError):
    """Raised when authored tool config overrides are invalid."""


@dataclass(frozen=True)
class _ToolRegistrySnapshot:
    registry: dict[str, Callable[[], type[Toolkit]]]
    metadata: dict[str, ToolMetadata]
    tool_module_cache: dict[Path, float]
    module_import_cache: dict[Path, plugin_module._ModuleCacheEntry]
    plugin_module_names: frozenset[str]


def is_authored_override_inherit(value: object) -> bool:
    """Return whether an authored override value clears an inherited higher-level override."""
    return value == AUTHORED_OVERRIDE_INHERIT


def apply_authored_overrides(
    base: dict[str, object],
    overrides: dict[str, object] | None,
) -> dict[str, object]:
    """Apply one authored override layer onto an existing authored-override mapping."""
    resolved = dict(base)
    if not overrides:
        return resolved

    for field_name, value in overrides.items():
        if is_authored_override_inherit(value):
            resolved.pop(field_name, None)
        else:
            resolved[field_name] = value
    return resolved


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

    if field_name == "shell_path_prepend":
        if value is None or isinstance(value, str):
            return value
        msg = (
            f"Unsupported value for tool init override '{tool_name}.{field_name}': "
            "expected a comma or newline-separated string path list or null."
        )
        raise ToolInitOverrideError(msg)

    return value


def _override_path(
    tool_name: str,
    field_name: str,
    *,
    config_path_prefix: str | None,
) -> str:
    if config_path_prefix:
        return f"{config_path_prefix}.{tool_name}.{field_name}"
    return f"{tool_name}.{field_name}"


def _agent_override_field(tool_name: str, field_name: str) -> ConfigField | None:
    """Return one tool's agent override field metadata when it exists."""
    metadata = TOOL_METADATA.get(tool_name)
    if metadata is None or not metadata.agent_override_fields:
        return None
    return next((candidate for candidate in metadata.agent_override_fields if candidate.name == field_name), None)


def _validate_text_authored_override_value(
    tool_name: str,
    field: ConfigField,
    value: object,
    *,
    full_path: str,
) -> object:
    """Validate one authored override for a text-like config field."""
    agent_override_field = _agent_override_field(tool_name, field.name)
    if agent_override_field is not None and agent_override_field.type == "string[]":
        try:
            normalized = _normalize_string_array_override(value)
        except TypeError as exc:
            msg = f"{full_path}: {exc}."
            raise ToolConfigOverrideError(msg) from exc
        if normalized is None:
            return None
        return ", ".join(normalized)

    if not isinstance(value, str):
        msg = f"{full_path}: expected a string or null."
        raise ToolConfigOverrideError(msg)
    return value


def _validate_authored_override_value(
    tool_name: str,
    field: ConfigField,
    value: object,
    *,
    full_path: str,
) -> object:
    """Validate one authored override value against its declared config field type."""
    if is_authored_override_inherit(value):
        return value

    if value is None:
        if field.required:
            msg = f"{full_path}: null is not allowed for required fields."
            raise ToolConfigOverrideError(msg)
        return None

    if field.type in _TEXT_CONFIG_FIELD_TYPES:
        return _validate_text_authored_override_value(tool_name, field, value, full_path=full_path)

    if field.type == "boolean":
        if not isinstance(value, bool):
            msg = f"{full_path}: expected a boolean or null."
            raise ToolConfigOverrideError(msg)
        return value

    if field.type == "number":
        if isinstance(value, bool) or not isinstance(value, int | float):
            msg = f"{full_path}: expected a number or null."
            raise ToolConfigOverrideError(msg)
        return value

    return value


def validate_authored_overrides(
    tool_name: str,
    overrides: dict[str, object] | None,
    *,
    config_path_prefix: str | None = None,
) -> dict[str, object]:
    """Validate authored YAML overrides against one tool's declared config fields."""
    if not overrides:
        return {}

    metadata = TOOL_METADATA.get(tool_name)
    if metadata is None:
        msg = f"Unknown tool '{tool_name}'."
        raise ToolConfigOverrideError(msg)

    fields_by_name = {field.name: field for field in metadata.config_fields or []}
    unexpected_fields = sorted(set(overrides) - set(fields_by_name))
    if unexpected_fields:
        unexpected = ", ".join(unexpected_fields)
        allowed = ", ".join(sorted(fields_by_name)) or "none"
        path = _override_path(tool_name, unexpected_fields[0], config_path_prefix=config_path_prefix)
        msg = f"{path}: unknown authored override field(s): {unexpected}. Allowed fields: {allowed}."
        raise ToolConfigOverrideError(msg)

    validated: dict[str, object] = {}
    for field_name, value in overrides.items():
        field = fields_by_name[field_name]
        full_path = _override_path(tool_name, field_name, config_path_prefix=config_path_prefix)
        if field.type == "password":
            msg = f"{full_path}: authored overrides are not allowed for password fields."
            raise ToolConfigOverrideError(msg)
        if not field.authored_override:
            msg = f"{full_path}: authored overrides are not allowed for this field."
            raise ToolConfigOverrideError(msg)
        validated[field_name] = _validate_authored_override_value(
            tool_name,
            field,
            value,
            full_path=full_path,
        )
    return validated


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
    tool_config_overrides: dict[str, object] | None,
    tool_init_overrides: dict[str, object] | None,
    runtime_overrides: dict[str, object] | None,
) -> dict[str, object]:
    """Collect safe config-field kwargs for one tool constructor."""
    if not metadata.config_fields:
        return {}

    config_field_names = {field.name for field in metadata.config_fields}
    init_kwargs = {field.name: credentials[field.name] for field in metadata.config_fields if field.name in credentials}
    if tool_config_overrides:
        for field in metadata.config_fields:
            if field.name not in tool_config_overrides:
                continue
            override_value = tool_config_overrides[field.name]
            if is_authored_override_inherit(override_value):
                continue
            init_kwargs[field.name] = override_value
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
    worker_target: ResolvedWorkerTarget | None,
) -> dict[str, object]:
    """Build declared MindRoom-managed constructor kwargs for one tool."""
    init_kwargs: dict[str, object] = {}
    for init_arg in metadata.managed_init_args:
        if init_arg == ToolManagedInitArg.RUNTIME_PATHS:
            init_kwargs[init_arg.value] = runtime_paths
        elif init_arg == ToolManagedInitArg.CREDENTIALS_MANAGER:
            init_kwargs[init_arg.value] = credentials_manager
        elif init_arg == ToolManagedInitArg.WORKER_TARGET:
            init_kwargs[init_arg.value] = worker_target
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
    tool_config_overrides: dict[str, object] | None = None,
    tool_init_overrides: dict[str, object] | None = None,
    worker_tools_override: list[str] | None = None,
    runtime_overrides: dict[str, object] | None = None,
    shared_storage_root_path: Path | None = None,
    worker_target: ResolvedWorkerTarget | None,
) -> Toolkit:
    """Instantiate a tool from the registry, applying credentials and sandbox proxy."""
    worker_scope = worker_target.worker_scope if worker_target is not None else None
    routing_agent_name = worker_target.routing_agent_name if worker_target is not None else None
    if not supports_tool_name_for_worker_scope(tool_name, worker_scope):
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
            credentials_manager=resolved_credentials_manager,
            worker_target=worker_target,
        )
        if resolved_credentials_manager is not None
        else {}
    ) or {}
    if credential_overrides:
        credentials = {**credentials, **credential_overrides}
    validated_tool_config_overrides = validate_authored_overrides(tool_name, tool_config_overrides)
    safe_tool_init_overrides = sanitize_tool_init_overrides(tool_name, tool_init_overrides)
    init_kwargs = _build_tool_config_init_kwargs(
        metadata,
        credentials=credentials,
        tool_config_overrides=validated_tool_config_overrides,
        tool_init_overrides=safe_tool_init_overrides,
        runtime_overrides=runtime_overrides,
    )
    extra_env_passthrough = init_kwargs.get("extra_env_passthrough")
    proxy_tool_init_overrides = dict(safe_tool_init_overrides or {})
    shell_path_prepend = init_kwargs.get("shell_path_prepend")
    if tool_name == "shell" and isinstance(shell_path_prepend, str):
        proxy_tool_init_overrides["shell_path_prepend"] = shell_path_prepend
    init_kwargs.update(
        _build_managed_tool_init_kwargs(
            metadata,
            runtime_paths=runtime_paths,
            credentials_manager=resolved_credentials_manager,
            worker_target=worker_target,
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
        tool_init_overrides=proxy_tool_init_overrides or None,
        tool_config_overrides=validated_tool_config_overrides,
        runtime_overrides=runtime_overrides,
        extra_env_passthrough=extra_env_passthrough if isinstance(extra_env_passthrough, str) else None,
        worker_tools_override=worker_tools_override,
        shared_storage_root_path=shared_storage_root_path,
        worker_target=worker_target,
    )


def get_tool_by_name(
    tool_name: str,
    runtime_paths: RuntimePaths,
    *,
    disable_sandbox_proxy: bool = False,
    credential_overrides: dict[str, object] | None = None,
    credentials_manager: CredentialsManager | None = None,
    tool_config_overrides: dict[str, object] | None = None,
    tool_init_overrides: dict[str, object] | None = None,
    worker_tools_override: list[str] | None = None,
    runtime_overrides: dict[str, object] | None = None,
    shared_storage_root_path: Path | None = None,
    worker_target: ResolvedWorkerTarget | None,
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
        tool_config_overrides=tool_config_overrides,
        tool_init_overrides=tool_init_overrides,
        worker_tools_override=worker_tools_override,
        runtime_overrides=runtime_overrides,
        shared_storage_root_path=shared_storage_root_path,
        worker_target=worker_target,
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
    WORKER_TARGET = "worker_target"


@dataclass
class ConfigField:
    """Definition of a configuration field."""

    name: str  # Environment variable name (e.g., "SMTP_HOST")
    label: str  # Display label (e.g., "SMTP Host")
    type: Literal["boolean", "number", "password", "text", "url", "select", "string[]"] = "text"
    required: bool = True
    default: Any = None
    placeholder: str | None = None
    description: str | None = None
    options: list[dict[str, str]] | None = None  # For select type
    validation: dict[str, Any] | None = None  # min, max, pattern, etc.
    authored_override: bool = True


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
    agent_override_fields: list[ConfigField] | None = None  # Safe per-agent override field definitions
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
    agent_override_fields: list[ConfigField] | None = None,
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
        agent_override_fields: Safe per-agent override fields serialized via config.yaml
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
            agent_override_fields=agent_override_fields,
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

    load_plugins(config, runtime_paths, set_skill_roots=False)


def _capture_tool_registry_snapshot() -> _ToolRegistrySnapshot:
    """Capture the mutable tool/plugin registry state for transactional restoration."""
    return _ToolRegistrySnapshot(
        registry=_TOOL_REGISTRY.copy(),
        metadata=TOOL_METADATA.copy(),
        tool_module_cache=plugin_module._TOOL_MODULE_CACHE.copy(),
        module_import_cache=plugin_module._MODULE_IMPORT_CACHE.copy(),
        plugin_module_names=frozenset(
            module_name for module_name in sys.modules if module_name.startswith(_PLUGIN_MODULE_PREFIX)
        ),
    )


def _restore_tool_registry_snapshot(snapshot: _ToolRegistrySnapshot) -> None:
    """Restore one previously captured tool/plugin registry snapshot."""
    _TOOL_REGISTRY.clear()
    _TOOL_REGISTRY.update(snapshot.registry)
    TOOL_METADATA.clear()
    TOOL_METADATA.update(snapshot.metadata)
    plugin_module._TOOL_MODULE_CACHE.clear()
    plugin_module._TOOL_MODULE_CACHE.update(snapshot.tool_module_cache)
    plugin_module._MODULE_IMPORT_CACHE.clear()
    plugin_module._MODULE_IMPORT_CACHE.update(snapshot.module_import_cache)
    for module_name in tuple(sys.modules):
        if module_name.startswith(_PLUGIN_MODULE_PREFIX) and module_name not in snapshot.plugin_module_names:
            sys.modules.pop(module_name, None)


@contextmanager
def loaded_tool_registry_for_validation(
    runtime_paths: RuntimePaths,
    config: Config,
) -> Iterator[None]:
    """Temporarily load plugin tools for validation without leaking global registry state."""
    snapshot = _capture_tool_registry_snapshot()
    try:
        ensure_tool_registry_loaded(runtime_paths, config)
        yield
    finally:
        _restore_tool_registry_snapshot(snapshot)


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


def _normalize_string_array_override(value: object) -> list[str] | None:
    """Normalize a string-array authored override from a list or legacy text value."""
    if value is None:
        return None
    if isinstance(value, str):
        values = [part.strip() for part in value.replace("\n", ",").split(",") if part.strip()]
        return values or None
    if not isinstance(value, list):
        msg = "expected a list of strings or a comma/newline-separated string"
        raise TypeError(msg)
    normalized: list[str] = []
    for entry in value:
        if not isinstance(entry, str):
            msg = "expected a list of strings"
            raise TypeError(msg)
        stripped = entry.strip()
        if stripped:
            normalized.append(stripped)
    return normalized or None


def _normalize_agent_override_field_value(field: ConfigField, value: object) -> object | None:
    """Normalize one authored agent override value according to its declared schema."""
    if field.type == "string[]":
        return _normalize_string_array_override(value)
    if field.type == "boolean":
        if value is None or isinstance(value, bool):
            return value
        msg = "expected a boolean or null"
        raise ValueError(msg)
    if field.type == "number":
        if value is None or (isinstance(value, (int, float)) and not isinstance(value, bool)):
            return value
        msg = "expected a number or null"
        raise ValueError(msg)
    if field.type in {"password", "select", "text", "url"}:
        if value is None:
            return None
        if isinstance(value, str):
            stripped = value.strip()
            return stripped or None
        msg = "expected a string or null"
        raise ValueError(msg)
    return value


def normalize_authored_tool_overrides(tool_name: str, overrides: dict[str, object] | None) -> dict[str, object]:
    """Validate and normalize one tool's authored per-agent overrides."""
    if not overrides:
        return {}

    metadata = TOOL_METADATA.get(tool_name)
    if metadata is None:
        msg = f"Unknown tool '{tool_name}' cannot declare per-agent overrides."
        raise ValueError(msg)

    field_map = {field.name: field for field in metadata.agent_override_fields or []}
    if not field_map:
        msg = f"Tool '{tool_name}' does not support per-agent overrides."
        raise ValueError(msg)

    unexpected_fields = sorted(set(overrides) - set(field_map))
    if unexpected_fields:
        allowed = ", ".join(sorted(field_map)) or "none"
        unexpected = ", ".join(unexpected_fields)
        msg = f"Unsupported per-agent override(s) for '{tool_name}': {unexpected}. Allowed overrides: {allowed}."
        raise ValueError(msg)

    normalized: dict[str, object] = {}
    for field_name, raw_value in overrides.items():
        field = field_map[field_name]
        try:
            normalized_value = _normalize_agent_override_field_value(field, raw_value)
        except (TypeError, ValueError) as exc:
            msg = f"Invalid per-agent override for '{tool_name}.{field_name}': {exc}"
            raise ValueError(msg) from exc
        if normalized_value is not None:
            normalized[field_name] = normalized_value
    return normalized


def authored_tool_overrides_to_runtime(tool_name: str, overrides: dict[str, object] | None) -> dict[str, object] | None:
    """Convert normalized authored per-agent overrides into runtime kwargs."""
    normalized = normalize_authored_tool_overrides(tool_name, overrides)
    if not normalized:
        return None

    metadata = TOOL_METADATA[tool_name]
    field_map = {field.name: field for field in metadata.agent_override_fields or []}
    runtime_overrides: dict[str, object] = {}
    for field_name, value in normalized.items():
        field = field_map[field_name]
        if field.type == "string[]":
            runtime_overrides[field_name] = ", ".join(cast("list[str]", value))
        else:
            runtime_overrides[field_name] = value
    return runtime_overrides or None

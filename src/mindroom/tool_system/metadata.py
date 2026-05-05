"""Tool metadata and enhanced registration system."""

from __future__ import annotations

import functools
import importlib
import math
import os
import sys
from dataclasses import asdict, dataclass
from enum import Enum
from importlib import util as importlib_util
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, cast

import mindroom.tool_system.plugin_imports as plugin_module
from mindroom.credentials import get_runtime_credentials_manager, load_scoped_credentials
from mindroom.logging_config import get_logger
from mindroom.tool_system.dependencies import auto_install_tool_extra, check_deps_installed
from mindroom.tool_system.output_files import ToolOutputFilePolicy, wrap_toolkit_for_output_files
from mindroom.tool_system.registry_state import (
    _BUILTIN_TOOL_METADATA,
    _BUILTIN_TOOL_REGISTRY,
    _PLUGIN_MODULE_PREFIX,
    _PLUGIN_REGISTRATION_SCOPE,
    _TOOL_REGISTRY,
    TOOL_METADATA,
    ToolMetadataValidationError,
    _register_plugin_tool_metadata,
    _resolved_tool_state,
    _scoped_plugin_registration_owner,
    _scoped_plugin_registration_store,
    register_builtin_tool_metadata,
)
from mindroom.tool_system.sandbox_proxy import maybe_wrap_toolkit_for_sandbox_proxy
from mindroom.tool_system.worker_routing import (
    ResolvedWorkerTarget,
    supports_tool_name_for_worker_scope,
    unsupported_shared_only_integration_message,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping
    from types import ModuleType

    from agno.tools import Toolkit

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.credentials import CredentialsManager

logger = get_logger(__name__)

_SAFE_TOOL_INIT_OVERRIDE_FIELDS = frozenset({"base_dir", "shell_path_prepend"})
_TEXT_CONFIG_FIELD_TYPES = frozenset({"password", "select", "text", "url"})
_AUTHORED_OVERRIDE_INHERIT = "__MINDROOM_INHERIT__"
_VALIDATION_PLUGIN_MODULE_SUFFIX = "__validation__"
_OMIT_TOOL_CONFIG_ARG = object()


class ToolInitOverrideError(ValueError):
    """Raised when a caller supplies unsupported tool init overrides."""


class ToolConfigOverrideError(ValueError):
    """Raised when authored tool config overrides are invalid."""


class ToolAuthoredOverrideValidator(str, Enum):
    """Explicit authored-override validation modes for a tool."""

    DEFAULT = "default"
    MCP = "mcp"


def _is_authored_override_inherit(value: object) -> bool:
    """Return whether an authored override value clears an inherited higher-level override."""
    return value == _AUTHORED_OVERRIDE_INHERIT


def apply_authored_overrides(
    base: dict[str, object],
    overrides: dict[str, object] | None,
) -> dict[str, object]:
    """Apply one authored override layer onto an existing authored-override mapping."""
    resolved = dict(base)
    if not overrides:
        return resolved

    for field_name, value in overrides.items():
        if _is_authored_override_inherit(value):
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


def _agent_override_field(
    tool_name: str,
    field_name: str,
    *,
    tool_metadata: Mapping[str, ToolMetadata | ToolValidationInfo] | None = None,
) -> ConfigField | None:
    """Return one tool's agent override field metadata when it exists."""
    metadata_by_name = TOOL_METADATA if tool_metadata is None else tool_metadata
    metadata = metadata_by_name.get(tool_name)
    if metadata is None or not metadata.agent_override_fields:
        return None
    return next((candidate for candidate in metadata.agent_override_fields if candidate.name == field_name), None)


def _validate_text_authored_override_value(
    tool_name: str,
    field: ConfigField,
    value: object,
    *,
    full_path: str,
    tool_metadata: Mapping[str, ToolMetadata | ToolValidationInfo] | None = None,
) -> object:
    """Validate one authored override for a text-like config field."""
    agent_override_field = _agent_override_field(tool_name, field.name, tool_metadata=tool_metadata)
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
    tool_metadata: Mapping[str, ToolMetadata | ToolValidationInfo] | None = None,
) -> object:
    """Validate one authored override value against its declared config field type."""
    if _is_authored_override_inherit(value):
        return value

    if value is None:
        if field.required:
            msg = f"{full_path}: null is not allowed for required fields."
            raise ToolConfigOverrideError(msg)
        return None

    if field.type in _TEXT_CONFIG_FIELD_TYPES:
        return _validate_text_authored_override_value(
            tool_name,
            field,
            value,
            full_path=full_path,
            tool_metadata=tool_metadata,
        )

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


def _validate_authored_overrides(
    tool_name: str,
    overrides: dict[str, object] | None,
    *,
    config_path_prefix: str | None = None,
    tool_metadata: Mapping[str, ToolMetadata | ToolValidationInfo] | None = None,
) -> dict[str, object]:
    """Validate authored YAML overrides against one tool's declared config fields."""
    if not overrides:
        return {}

    metadata_by_name = TOOL_METADATA if tool_metadata is None else tool_metadata
    metadata = metadata_by_name.get(tool_name)
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
            tool_metadata=tool_metadata,
        )
    return validated


def _run_authored_override_validator(
    tool_name: str,
    overrides: dict[str, object],
    *,
    validator: ToolAuthoredOverrideValidator,
) -> None:
    """Run one tool-specific authored-override validator against normalized overrides."""
    if not overrides or validator == ToolAuthoredOverrideValidator.DEFAULT:
        return
    if validator == ToolAuthoredOverrideValidator.MCP:
        from mindroom.mcp.registry import validate_mcp_agent_overrides  # noqa: PLC0415

        validate_mcp_agent_overrides(tool_name, overrides)
        return
    msg = f"Unsupported authored override validator '{validator}'."
    raise ValueError(msg)


def validate_authored_tool_entry_overrides(
    tool_name: str,
    overrides: dict[str, object] | None,
    *,
    config_path_prefix: str | None = None,
    tool_metadata: Mapping[str, ToolMetadata | ToolValidationInfo] | None = None,
) -> dict[str, object]:
    """Validate authored overrides, including any tool-specific validation mode."""
    validated_overrides = _validate_authored_overrides(
        tool_name,
        overrides,
        config_path_prefix=config_path_prefix,
        tool_metadata=tool_metadata,
    )
    metadata_by_name = TOOL_METADATA if tool_metadata is None else tool_metadata
    metadata = metadata_by_name.get(tool_name)
    if metadata is None:
        return validated_overrides
    try:
        _run_authored_override_validator(
            tool_name,
            validated_overrides,
            validator=metadata.authored_override_validator,
        )
    except ValueError as exc:
        raise ToolConfigOverrideError(str(exc)) from exc
    return validated_overrides


def sanitize_tool_init_overrides(
    tool_name: str,
    tool_init_overrides: dict[str, object] | None,
    *,
    tool_metadata: Mapping[str, ToolMetadata] | None = None,
) -> dict[str, object] | None:
    """Validate and retain only the explicitly safe runtime tool init overrides."""
    if not tool_init_overrides:
        return None

    metadata_by_name = TOOL_METADATA if tool_metadata is None else tool_metadata
    metadata = metadata_by_name.get(tool_name)
    if metadata is None:
        msg = f"Unknown tool '{tool_name}'."
        raise ToolInitOverrideError(msg)
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


def _coerce_number_tool_config_value(tool_name: str, field_name: str, value: object) -> int | float | object:
    """Normalize a persisted dashboard number field before passing it to a tool constructor."""
    if value is None:
        return _OMIT_TOOL_CONFIG_ARG
    if isinstance(value, bool):
        msg = f"Stored config value for '{tool_name}.{field_name}' must be a number."
        raise ToolConfigOverrideError(msg)
    if isinstance(value, int | float) and math.isfinite(value):
        return value
    if isinstance(value, int | float):
        msg = f"Stored config value for '{tool_name}.{field_name}' must be a finite number."
        raise ToolConfigOverrideError(msg)
    if isinstance(value, str):
        raw_value = value.strip()
        if not raw_value:
            return _OMIT_TOOL_CONFIG_ARG
        try:
            parsed = float(raw_value)
        except ValueError as exc:
            msg = f"Stored config value for '{tool_name}.{field_name}' must be a number."
            raise ToolConfigOverrideError(msg) from exc
        if not math.isfinite(parsed):
            msg = f"Stored config value for '{tool_name}.{field_name}' must be a finite number."
            raise ToolConfigOverrideError(msg)
        return int(parsed) if parsed.is_integer() else parsed
    msg = f"Stored config value for '{tool_name}.{field_name}' must be a number."
    raise ToolConfigOverrideError(msg)


def _coerce_runtime_tool_config_value(tool_name: str, field: ConfigField, value: object) -> object:
    if field.type == "number":
        return _coerce_number_tool_config_value(tool_name, field.name, value)
    return value


def _set_tool_config_init_kwarg(
    init_kwargs: dict[str, object],
    *,
    tool_name: str,
    field: ConfigField,
    value: object,
) -> None:
    coerced_value = _coerce_runtime_tool_config_value(tool_name, field, value)
    if coerced_value is not _OMIT_TOOL_CONFIG_ARG:
        init_kwargs[field.name] = coerced_value


def _apply_tool_config_init_values(
    init_kwargs: dict[str, object],
    *,
    tool_name: str,
    fields: tuple[ConfigField, ...],
    values: dict[str, object] | None,
    skip_inherited: bool = False,
) -> None:
    if not values:
        return
    for field in fields:
        if field.name not in values:
            continue
        value = values[field.name]
        if skip_inherited and _is_authored_override_inherit(value):
            continue
        _set_tool_config_init_kwarg(
            init_kwargs,
            tool_name=tool_name,
            field=field,
            value=value,
        )


def _build_tool_config_init_kwargs(
    tool_name: str,
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

    init_kwargs: dict[str, object] = {}
    fields = tuple(metadata.config_fields)
    _apply_tool_config_init_values(init_kwargs, tool_name=tool_name, fields=fields, values=credentials)
    _apply_tool_config_init_values(
        init_kwargs,
        tool_name=tool_name,
        fields=fields,
        values=tool_config_overrides,
        skip_inherited=True,
    )
    _apply_tool_config_init_values(init_kwargs, tool_name=tool_name, fields=fields, values=tool_init_overrides)
    _apply_tool_config_init_values(init_kwargs, tool_name=tool_name, fields=fields, values=runtime_overrides)
    if "base_dir" in init_kwargs and isinstance(init_kwargs["base_dir"], str):
        init_kwargs["base_dir"] = Path(init_kwargs["base_dir"])
    return init_kwargs


def _build_managed_tool_init_kwargs(
    metadata: ToolMetadata,
    *,
    runtime_paths: RuntimePaths,
    credentials_manager: CredentialsManager | None,
    worker_target: ResolvedWorkerTarget | None,
    tool_output_workspace_root: Path | None,
    worker_tools_override: list[str] | None,
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
        elif init_arg == ToolManagedInitArg.TOOL_OUTPUT_WORKSPACE_ROOT:
            init_kwargs[init_arg.value] = tool_output_workspace_root
        elif init_arg == ToolManagedInitArg.WORKER_TOOLS_OVERRIDE:
            init_kwargs[init_arg.value] = worker_tools_override
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
    allowed_shared_services: frozenset[str] | None = None,
    tool_output_workspace_root: Path | None = None,
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
        raise ToolMetadataValidationError(msg)

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
            allowed_shared_services=allowed_shared_services,
        )
        if resolved_credentials_manager is not None
        else {}
    ) or {}
    if credential_overrides:
        credentials = {**credentials, **credential_overrides}
    validated_tool_config_overrides = validate_authored_tool_entry_overrides(tool_name, tool_config_overrides)
    safe_tool_init_overrides = sanitize_tool_init_overrides(tool_name, tool_init_overrides)
    init_kwargs = _build_tool_config_init_kwargs(
        tool_name,
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
            tool_output_workspace_root=tool_output_workspace_root,
            worker_tools_override=worker_tools_override,
        ),
    )

    toolkit = cast("Any", tool_class)(**init_kwargs)
    output_file_policy = (
        ToolOutputFilePolicy.from_runtime(tool_output_workspace_root, runtime_paths)
        if tool_output_workspace_root is not None
        else None
    )
    wrap_toolkit_for_output_files(toolkit, output_file_policy)
    if disable_sandbox_proxy:
        return toolkit
    return maybe_wrap_toolkit_for_sandbox_proxy(
        tool_name,
        toolkit,
        runtime_paths=runtime_paths,
        credentials_manager=resolved_credentials_manager,
        tool_init_overrides=proxy_tool_init_overrides or None,
        tool_config_overrides=validated_tool_config_overrides,
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
    allowed_shared_services: frozenset[str] | None = None,
    tool_output_workspace_root: Path | None = None,
    worker_target: ResolvedWorkerTarget | None,
) -> Toolkit:
    """Get a tool instance by its registered name."""
    if tool_name not in _TOOL_REGISTRY:
        available = ", ".join(sorted(_TOOL_REGISTRY.keys()))
        msg = f"Unknown tool: {tool_name}. Available tools: {available}"
        raise ToolMetadataValidationError(msg)

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
        allowed_shared_services=allowed_shared_services,
        tool_output_workspace_root=tool_output_workspace_root,
        worker_target=worker_target,
    )

    # Pre-check dependencies using find_spec (no side effects) before importing
    metadata = TOOL_METADATA.get(tool_name)
    deps = metadata.dependencies if metadata and metadata.dependencies else []
    if deps and not check_deps_installed(deps):
        if not auto_install_tool_extra(tool_name, runtime_paths):
            missing = ", ".join(deps)
            logger.warning("tool_dependencies_missing", tool_name=tool_name, missing_dependencies=missing)
            logger.warning("tool_dependency_install_required", tool_name=tool_name)
            msg = f"Missing dependencies for tool '{tool_name}': {missing}"
            raise ImportError(msg)
        logger.info("tool_optional_dependencies_auto_installed", tool_name=tool_name)
        importlib.invalidate_caches()

    try:
        return build()
    except ImportError as first_error:
        # Safety net: deps may not be exhaustively listed in metadata
        if not auto_install_tool_extra(tool_name, runtime_paths):
            logger.warning("tool_import_failed", tool_name=tool_name, error=str(first_error))
            logger.warning("tool_dependency_install_required", tool_name=tool_name)
            raise

        logger.info("auto_installing_tool_optional_dependencies", tool_name=tool_name)
        importlib.invalidate_caches()

        try:
            return build()
        except ImportError as second_error:
            logger.warning("tool_auto_install_failed", tool_name=tool_name, error=str(second_error))
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
    TOOL_OUTPUT_WORKSPACE_ROOT = "tool_output_workspace_root"
    WORKER_TOOLS_OVERRIDE = "worker_tools_override"


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


@dataclass(frozen=True)
class ToolValidationInfo:
    """Validation-only metadata for authored tool references."""

    name: str
    config_fields: tuple[ConfigField, ...] = ()
    agent_override_fields: tuple[ConfigField, ...] = ()
    authored_override_validator: ToolAuthoredOverrideValidator = ToolAuthoredOverrideValidator.DEFAULT
    runtime_loadable: bool = True


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
    authored_override_validator: ToolAuthoredOverrideValidator = ToolAuthoredOverrideValidator.DEFAULT
    dependencies: list[str] | None = None  # Required pip packages
    auth_provider: str | None = None  # Name of integration that provides auth (e.g., "google")
    docs_url: str | None = None  # Documentation URL
    helper_text: str | None = None  # Additional help text for setup
    function_names: tuple[str, ...] = ()  # Optional explicit callable names for dispatch/error matching
    managed_init_args: tuple[ToolManagedInitArg, ...] = ()  # Explicit MindRoom-managed constructor kwargs
    factory: Callable | None = None  # Factory function to create tool instance


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
    authored_override_validator: ToolAuthoredOverrideValidator = ToolAuthoredOverrideValidator.DEFAULT,
    dependencies: list[str] | None = None,
    auth_provider: str | None = None,
    docs_url: str | None = None,
    helper_text: str | None = None,
    function_names: tuple[str, ...] = (),
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
        authored_override_validator: Explicit authored-override validation mode for the tool
        dependencies: Required Python packages
        auth_provider: Name of integration that provides authentication
        docs_url: Link to documentation
        helper_text: Additional setup instructions
        function_names: Optional explicit callable names exposed by the toolkit
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
            authored_override_validator=authored_override_validator,
            dependencies=dependencies,
            auth_provider=auth_provider,
            docs_url=docs_url,
            helper_text=helper_text,
            function_names=function_names,
            managed_init_args=managed_init_args,
            factory=func,
        )

        validation_owner_module_name = getattr(
            _PLUGIN_REGISTRATION_SCOPE,
            "owner_module_name",
            None,
        )
        if validation_owner_module_name is not None:
            _register_plugin_tool_metadata(validation_owner_module_name, metadata)
            return func

        if func.__module__.startswith(_PLUGIN_MODULE_PREFIX):
            _register_plugin_tool_metadata(func.__module__, metadata)
            return func

        register_builtin_tool_metadata(metadata)

        return func

    return decorator


@functools.lru_cache(maxsize=8192)
def _resolved_module_file(module_file: str) -> Path | None:
    """Return the resolved on-disk path for one module file, cached across calls."""
    try:
        return Path(module_file).resolve()
    except OSError:
        return None


def _module_origin_within_root(module: ModuleType, root: Path) -> bool:
    """Return whether one loaded module originates from within one plugin root."""
    module_file = getattr(module, "__file__", None)
    if not isinstance(module_file, str):
        return False
    resolved = _resolved_module_file(module_file)
    return resolved is not None and resolved.is_relative_to(root)


def _execute_validation_plugin_module(
    plugin_name: str,
    plugin_root: Path,
    module_path: Path,
    registrations_by_module: dict[str, dict[str, ToolMetadata]],
) -> str:
    """Execute one plugin module into a temporary validation import context."""
    runtime_module_name = plugin_module._module_name(plugin_name, plugin_root, module_path)
    validation_module_name = f"{runtime_module_name}{_VALIDATION_PLUGIN_MODULE_SUFFIX}{id(registrations_by_module)}"
    previous_packages = plugin_module._snapshot_plugin_package_chain(plugin_name, plugin_root, module_path)
    plugin_module._install_plugin_package_chain(plugin_name, plugin_root, module_path)
    spec = importlib_util.spec_from_file_location(validation_module_name, module_path)
    if spec is None or spec.loader is None:
        plugin_module._restore_plugin_package_chain(previous_packages)
        msg = f"Failed to load plugin validation module: {module_path}"
        raise ToolMetadataValidationError(msg)

    previous_module = sys.modules.get(validation_module_name)
    loaded_modules = sys.modules.copy()
    previous_modules_within_root = {
        module_name: loaded_module
        for module_name, loaded_module in loaded_modules.items()
        if _module_origin_within_root(loaded_module, plugin_root)
    }
    module = importlib_util.module_from_spec(spec)
    sys.modules[validation_module_name] = module
    try:
        with (
            _scoped_plugin_registration_store(registrations_by_module),
            _scoped_plugin_registration_owner(validation_module_name),
        ):
            spec.loader.exec_module(module)
    except Exception as exc:
        msg = f"Plugin validation module execution failed for {module_path}: {exc}"
        raise ToolMetadataValidationError(msg) from exc
    finally:
        for loaded_module_name, loaded_module in sys.modules.copy().items():
            if loaded_module_name not in previous_modules_within_root and _module_origin_within_root(
                loaded_module,
                plugin_root,
            ):
                sys.modules.pop(loaded_module_name, None)
        for loaded_module_name, loaded_module in previous_modules_within_root.items():
            sys.modules[loaded_module_name] = loaded_module
        plugin_module._restore_plugin_package_chain(previous_packages)
        if previous_module is None:
            sys.modules.pop(validation_module_name, None)
        else:
            sys.modules[validation_module_name] = previous_module

    return validation_module_name


def _resolved_tool_state_for_runtime(
    runtime_paths: RuntimePaths,
    config: Config,
    *,
    tolerate_plugin_load_errors: bool = False,
) -> tuple[dict[str, Callable[[], type[Toolkit]]], dict[str, ToolMetadata]]:
    """Return registry and metadata visible for one runtime config without mutating global state."""
    import mindroom.tools  # noqa: F401, PLC0415
    from mindroom.mcp.registry import resolved_mcp_tool_state  # noqa: PLC0415

    plugin_entries = config.plugins
    if not plugin_entries:
        builtin_registry = _BUILTIN_TOOL_REGISTRY.copy()
        builtin_metadata = _BUILTIN_TOOL_METADATA.copy()
        mcp_registry, mcp_metadata = resolved_mcp_tool_state(config)
        _merge_mcp_tool_state(
            builtin_registry,
            builtin_metadata,
            mcp_registry,
            mcp_metadata,
        )
        return builtin_registry, builtin_metadata

    plugin_bases = plugin_module._collect_plugin_bases(
        plugin_entries,
        runtime_paths,
        skip_broken_plugins=tolerate_plugin_load_errors,
    )

    plugin_module._reject_duplicate_plugin_manifest_names(plugin_bases)

    validation_registrations: dict[str, dict[str, ToolMetadata]] = {}
    active_plugins: list[tuple[str, str]] = []
    for plugin_base, plugin_entry, _ in plugin_bases:
        candidate_registrations: dict[str, dict[str, ToolMetadata]] = {}
        candidate_active_plugins: list[tuple[str, str]] = []
        try:
            if plugin_base.tools_module_path is None:
                if plugin_base.hooks_module_path is not None:
                    _execute_validation_plugin_module(
                        plugin_base.name,
                        plugin_base.root,
                        plugin_base.hooks_module_path,
                        candidate_registrations,
                    )
            else:
                candidate_active_plugins.append(
                    (
                        plugin_base.name,
                        _execute_validation_plugin_module(
                            plugin_base.name,
                            plugin_base.root,
                            plugin_base.tools_module_path,
                            candidate_registrations,
                        ),
                    ),
                )
                if (
                    plugin_base.hooks_module_path is not None
                    and plugin_base.hooks_module_path != plugin_base.tools_module_path
                ):
                    _execute_validation_plugin_module(
                        plugin_base.name,
                        plugin_base.root,
                        plugin_base.hooks_module_path,
                        candidate_registrations,
                    )
        except Exception as exc:
            if not tolerate_plugin_load_errors:
                raise
            plugin_module._log_skipped_plugin_entry(plugin_entry.path, plugin_base.root, exc)
            continue

        validation_registrations.update(candidate_registrations)
        active_plugins.extend(candidate_active_plugins)

    desired_registry, desired_metadata = _resolved_tool_state(active_plugins, validation_registrations)
    mcp_registry, mcp_metadata = resolved_mcp_tool_state(config)
    _merge_mcp_tool_state(
        desired_registry,
        desired_metadata,
        mcp_registry,
        mcp_metadata,
    )
    return desired_registry, desired_metadata


def _merge_mcp_tool_state(
    registry: dict[str, Callable[[], type[Toolkit]]],
    metadata: dict[str, ToolMetadata],
    mcp_registry: dict[str, Callable[[], type[Toolkit]]],
    mcp_metadata: dict[str, ToolMetadata],
) -> None:
    """Merge MCP tool state into one resolved runtime registry after collision checks."""
    collisions = sorted({*mcp_registry, *mcp_metadata} & {*registry, *metadata})
    if collisions:
        msg = f"MCP tool '{collisions[0]}' conflicts with an existing registered tool"
        raise ToolMetadataValidationError(msg)
    registry.update(mcp_registry)
    metadata.update(mcp_metadata)


def resolved_tool_metadata_for_runtime(
    runtime_paths: RuntimePaths,
    config: Config,
    *,
    tolerate_plugin_load_errors: bool = False,
) -> dict[str, ToolMetadata]:
    """Return tool metadata visible for one runtime config without mutating global state."""
    _, desired_metadata = _resolved_tool_state_for_runtime(
        runtime_paths,
        config,
        tolerate_plugin_load_errors=tolerate_plugin_load_errors,
    )
    return desired_metadata


def _tool_validation_snapshot_from_state(
    tool_registry: Mapping[str, Callable[[], type[Toolkit]]],
    tool_metadata: Mapping[str, ToolMetadata],
) -> dict[str, ToolValidationInfo]:
    """Project runtime tool state into a validation-only snapshot."""
    return {
        tool_name: ToolValidationInfo(
            name=tool_name,
            config_fields=tuple(metadata.config_fields or ()),
            agent_override_fields=tuple(metadata.agent_override_fields or ()),
            authored_override_validator=metadata.authored_override_validator,
            runtime_loadable=tool_name in tool_registry,
        )
        for tool_name, metadata in tool_metadata.items()
    }


def resolved_tool_validation_snapshot_for_runtime(
    runtime_paths: RuntimePaths,
    config: Config,
    *,
    tolerate_plugin_load_errors: bool = False,
) -> dict[str, ToolValidationInfo]:
    """Return validation-only tool state visible for one runtime config."""
    tool_registry, desired_metadata = _resolved_tool_state_for_runtime(
        runtime_paths,
        config,
        tolerate_plugin_load_errors=tolerate_plugin_load_errors,
    )
    return _tool_validation_snapshot_from_state(tool_registry, desired_metadata)


def serialize_tool_validation_snapshot(
    tool_validation_snapshot: Mapping[str, ToolValidationInfo],
) -> dict[str, dict[str, object]]:
    """Export one validation snapshot as a JSON-serializable object."""
    return {
        tool_name: {
            "config_fields": [asdict(field) for field in info.config_fields],
            "agent_override_fields": [asdict(field) for field in info.agent_override_fields],
            "authored_override_validator": info.authored_override_validator.value,
            "runtime_loadable": info.runtime_loadable,
        }
        for tool_name, info in sorted(tool_validation_snapshot.items())
    }


def _deserialize_tool_validation_fields(raw_fields: object, *, field_name: str) -> tuple[ConfigField, ...]:
    """Deserialize one serialized config-field list from a validation snapshot."""
    if raw_fields is None:
        return ()
    if not isinstance(raw_fields, list):
        msg = f"{field_name} must be a list of config field objects."
        raise TypeError(msg)
    fields: list[ConfigField] = []
    for index, raw_field in enumerate(raw_fields):
        if not isinstance(raw_field, dict):
            msg = f"{field_name}[{index}] must be an object."
            raise TypeError(msg)
        fields.append(ConfigField(**cast("dict[str, Any]", raw_field)))
    return tuple(fields)


def deserialize_tool_validation_snapshot(payload: object) -> dict[str, ToolValidationInfo]:
    """Deserialize one JSON payload into validation-only tool metadata."""
    if not isinstance(payload, dict):
        msg = "Tool validation snapshot must be a JSON object keyed by tool name."
        raise TypeError(msg)

    snapshot: dict[str, ToolValidationInfo] = {}
    for raw_tool_name, raw_info in payload.items():
        if not isinstance(raw_tool_name, str):
            msg = "Tool validation snapshot must be a JSON object keyed by tool name."
            raise TypeError(msg)
        if not isinstance(raw_info, dict):
            msg = f"Tool validation snapshot entry for '{raw_tool_name}' must be an object."
            raise TypeError(msg)
        tool_name = raw_tool_name
        raw_info_mapping = cast("dict[str, object]", raw_info)
        raw_validator = raw_info_mapping.get(
            "authored_override_validator",
            ToolAuthoredOverrideValidator.DEFAULT.value,
        )
        try:
            authored_override_validator = ToolAuthoredOverrideValidator(raw_validator)
        except ValueError as exc:
            msg = (
                f"Tool validation snapshot entry for '{tool_name}' has unsupported "
                f"authored_override_validator '{raw_validator}'."
            )
            raise TypeError(msg) from exc
        raw_runtime_loadable = raw_info_mapping.get("runtime_loadable", True)
        if not isinstance(raw_runtime_loadable, bool):
            msg = f"Tool validation snapshot entry for '{tool_name}' must set runtime_loadable to a boolean."
            raise TypeError(msg)
        snapshot[tool_name] = ToolValidationInfo(
            name=tool_name,
            config_fields=_deserialize_tool_validation_fields(
                raw_info_mapping.get("config_fields", []),
                field_name=f"{tool_name}.config_fields",
            ),
            agent_override_fields=_deserialize_tool_validation_fields(
                raw_info_mapping.get("agent_override_fields", []),
                field_name=f"{tool_name}.agent_override_fields",
            ),
            authored_override_validator=authored_override_validator,
            runtime_loadable=raw_runtime_loadable,
        )
    return snapshot


def default_worker_routed_tools(tool_names: list[str]) -> list[str]:
    """Return the tool names that default to worker execution."""
    selected_tools: list[str] = []
    for tool_name in tool_names:
        metadata = TOOL_METADATA.get(tool_name)
        if metadata is not None and metadata.default_execution_target == ToolExecutionTarget.WORKER:
            selected_tools.append(tool_name)
    return selected_tools


def export_tools_metadata(tool_metadata: dict[str, ToolMetadata] | None = None) -> list[dict[str, Any]]:
    """Export tool metadata as JSON-serializable dictionaries."""
    tools: list[dict[str, Any]] = []
    metadata_by_name = TOOL_METADATA if tool_metadata is None else tool_metadata

    for metadata in metadata_by_name.values():
        tool_dict = asdict(metadata)
        tool_dict["category"] = metadata.category.value
        tool_dict["status"] = metadata.status.value
        tool_dict["setup_type"] = metadata.setup_type.value
        tool_dict["default_execution_target"] = metadata.default_execution_target.value
        tool_dict.pop("authored_override_validator", None)
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
    _run_authored_override_validator(
        tool_name,
        normalized,
        validator=metadata.authored_override_validator,
    )
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

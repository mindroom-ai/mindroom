"""Runtime tool catalog resolution, validation, and instance construction."""

from __future__ import annotations

import ast
import functools
import itertools
import math
import os
import sys
import threading
import weakref
from dataclasses import asdict, dataclass
from dataclasses import field as dataclass_field
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import mindroom.tool_system.plugin_imports as plugin_module
from mindroom.constants import DEFAULT_TOOL_OUTPUT_AUTO_SAVE_THRESHOLD_BYTES
from mindroom.credentials import get_runtime_credentials_manager, load_scoped_credentials
from mindroom.logging_config import get_logger
from mindroom.tool_system.declarations import (
    ConfigField,
    ToolAuthoredOverrideValidator,
    ToolCategory,
    ToolExecutionTarget,
    ToolManagedInitArg,
    ToolMetadata,
    ToolValidationInfo,
)
from mindroom.tool_system.dependencies import auto_install_optional_extra_for_import_retry, ensure_tool_deps
from mindroom.tool_system.registry_state import (
    BUILTIN_TOOL_METADATA,
    BUILTIN_TOOL_REGISTRY,
    TOOL_METADATA,
    TOOL_REGISTRY,
    ToolMetadataValidationError,
    resolved_tool_state,
    scoped_plugin_registration_owner,
    scoped_plugin_registration_store,
)
from mindroom.tool_system.sandbox_proxy import maybe_wrap_toolkit_for_sandbox_proxy
from mindroom.tool_system.worker_routing import (
    ResolvedWorkerTarget,
    supports_tool_name_for_worker_scope,
    unsupported_shared_only_integration_message,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Mapping
    from types import ModuleType

    from agno.tools import Toolkit

    from mindroom.config.main import Config, RuntimeConfig
    from mindroom.config.plugin import PluginEntryConfig
    from mindroom.constants import RuntimePaths
    from mindroom.credentials import CredentialsManager
    from mindroom.hooks import HookCallback, HookRegistry

logger = get_logger(__name__)

_SAFE_TOOL_INIT_OVERRIDE_FIELDS = frozenset({"base_dir", "shell_path_prepend"})
_TEXT_CONFIG_FIELD_TYPES = frozenset({"password", "select", "text", "url"})
_AUTHORED_OVERRIDE_INHERIT = "__MINDROOM_INHERIT__"
_VALIDATION_PLUGIN_MODULE_SUFFIX = "__validation__"
_VALIDATION_PLUGIN_GENERATIONS = itertools.count()
_OMIT_TOOL_CONFIG_ARG = object()


class ToolInitOverrideError(ValueError):
    """Raised when a caller supplies unsupported tool init overrides."""


class ToolConfigOverrideError(ValueError):
    """Raised when authored tool config overrides are invalid."""


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
    allowed_fields = safe_tool_init_override_fields(tool_name, tool_metadata=metadata_by_name)
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


def safe_tool_init_override_fields(
    tool_name: str,
    *,
    tool_metadata: Mapping[str, ToolMetadata] | None = None,
) -> frozenset[str]:
    """Return the config fields a tool exposes as safe runtime init overrides."""
    metadata_by_name = TOOL_METADATA if tool_metadata is None else tool_metadata
    metadata = metadata_by_name.get(tool_name)
    if metadata is None:
        return frozenset()
    return frozenset(
        field.name for field in metadata.config_fields or [] if field.name in _SAFE_TOOL_INIT_OVERRIDE_FIELDS
    )


def coerce_optional_finite_number(value: object) -> int | float | None:
    """Normalize an optional finite number from runtime config text or JSON values."""
    if value is None:
        return None
    if isinstance(value, bool):
        raise TypeError
    if isinstance(value, int | float):
        if math.isfinite(value):
            return value
        raise OverflowError
    if isinstance(value, str):
        raw_value = value.strip()
        if not raw_value:
            return None
        parsed = float(raw_value)
        if not math.isfinite(parsed):
            raise OverflowError
        return int(parsed) if parsed.is_integer() else parsed
    raise TypeError


def _coerce_number_tool_config_value(tool_name: str, field_name: str, value: object) -> int | float | object:
    """Normalize a persisted dashboard number field before passing it to a tool constructor."""
    try:
        coerced = coerce_optional_finite_number(value)
    except OverflowError as exc:
        msg = f"Stored config value for '{tool_name}.{field_name}' must be a finite number."
        raise ToolConfigOverrideError(msg) from exc
    except (TypeError, ValueError) as exc:
        msg = f"Stored config value for '{tool_name}.{field_name}' must be a number."
        raise ToolConfigOverrideError(msg) from exc
    if coerced is None:
        return _OMIT_TOOL_CONFIG_ARG
    return coerced


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
    tool_factory: Callable[[], type[Toolkit]],
    tool_metadata: Mapping[str, ToolMetadata],
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
    tool_output_auto_save_threshold_bytes: int,
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

    # Imported on first tool construction so importing the registry stays free
    # of the agno runtime import chain (#1436).
    from mindroom.tool_system.output_files import (  # noqa: PLC0415
        ToolOutputFilePolicy,
        wrap_toolkit_for_output_files,
    )

    metadata = tool_metadata[tool_name]
    tool_class = tool_factory()
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
    validated_tool_config_overrides = validate_authored_tool_entry_overrides(
        tool_name,
        tool_config_overrides,
        tool_metadata=tool_metadata,
    )
    safe_tool_init_overrides = sanitize_tool_init_overrides(
        tool_name,
        tool_init_overrides,
        tool_metadata=tool_metadata,
    )
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
        ToolOutputFilePolicy.from_runtime(
            tool_output_workspace_root,
            runtime_paths,
            auto_save_threshold_bytes=tool_output_auto_save_threshold_bytes,
        )
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
    runtime_config: RuntimeConfig | None = None,
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
    tool_output_auto_save_threshold_bytes: int = DEFAULT_TOOL_OUTPUT_AUTO_SAVE_THRESHOLD_BYTES,
    worker_target: ResolvedWorkerTarget | None,
) -> Toolkit:
    """Get a tool instance by its registered name."""
    if runtime_config is None:
        tool_registry = TOOL_REGISTRY
        tool_metadata = TOOL_METADATA
    else:
        resolved_state = _resolved_tool_state_for_runtime(
            runtime_paths,
            runtime_config,
        )
        tool_registry = resolved_state.tool_registry
        tool_metadata = resolved_state.tool_metadata
    tool_factory = tool_registry.get(tool_name)
    if tool_factory is None:
        available = ", ".join(sorted(tool_registry))
        msg = f"Unknown tool: {tool_name}. Available tools: {available}"
        raise ToolMetadataValidationError(msg)

    build = functools.partial(
        _build_tool_instance,
        tool_name,
        runtime_paths,
        tool_factory=tool_factory,
        tool_metadata=tool_metadata,
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
        tool_output_auto_save_threshold_bytes=tool_output_auto_save_threshold_bytes,
        worker_target=worker_target,
    )

    # Pre-check dependencies using find_spec (no side effects) before importing
    metadata = tool_metadata.get(tool_name)
    deps = metadata.dependencies if metadata and metadata.dependencies else []
    if deps:
        missing = ", ".join(deps)
        try:
            installed = ensure_tool_deps(
                deps,
                tool_name,
                runtime_paths,
                missing_message=f"Missing dependencies for tool '{tool_name}': {missing}",
            )
        except ImportError:
            logger.warning("tool_dependencies_missing", tool_name=tool_name, missing_dependencies=missing)
            logger.warning("tool_dependency_install_required", tool_name=tool_name)
            raise
        if installed:
            logger.info("tool_optional_dependencies_auto_installed", tool_name=tool_name)

    try:
        return build()
    except ImportError as first_error:
        if not auto_install_optional_extra_for_import_retry(tool_name, runtime_paths):
            logger.warning("tool_import_failed", tool_name=tool_name, error=str(first_error))
            logger.warning("tool_dependency_install_required", tool_name=tool_name)
            raise

        logger.info("auto_installing_tool_optional_dependencies", tool_name=tool_name)
        try:
            return build()
        except ImportError as second_error:
            logger.warning("tool_auto_install_failed", tool_name=tool_name, error=str(second_error))
            raise second_error from first_error


def _remove_owned_plugin_modules(modules: tuple[tuple[str, ModuleType], ...]) -> None:
    for package_name, package in modules:
        if "." in package_name or sys.modules.get(package_name) is not package:
            continue
        for module_name in tuple(sys.modules):
            if module_name == package_name or module_name.startswith(f"{package_name}."):
                sys.modules.pop(module_name, None)


@dataclass(frozen=True)
class _OwnedPluginModules:
    """Keep one validation package importable until its resolved state is released."""

    modules: tuple[tuple[str, ModuleType], ...]

    def __post_init__(self) -> None:
        weakref.finalize(self, _remove_owned_plugin_modules, self.modules)


@dataclass(frozen=True)
class _ResolvedToolState:
    """Runtime-visible tool state plus validation-only skipped-plugin metadata."""

    tool_registry: dict[str, Callable[[], type[Toolkit]]]
    tool_metadata: dict[str, ToolMetadata]
    unavailable_tool_metadata: dict[str, ToolMetadata]
    hook_registry: HookRegistry
    owned_plugin_modules: _OwnedPluginModules | None = None


@dataclass(frozen=True)
class _ResolvedPluginHooks:
    """Minimal plugin shape needed to compile validation-time hooks."""

    name: str
    entry_config: PluginEntryConfig
    plugin_order: int
    discovered_hooks: tuple[HookCallback, ...]


@dataclass(frozen=True)
class ResolvedToolRuntimeState:
    """Resolved validation and runtime tool state carried across config construction."""

    runtime_paths: RuntimePaths
    validation_snapshot: Mapping[str, ToolValidationInfo]
    _state: _ResolvedToolState = dataclass_field(repr=False)


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


def _finish_validation_plugin_import(
    *,
    plugin_root: Path,
    validation_package_root: str,
    previous_modules_within_root: Mapping[str, ModuleType],
    previous_packages: Mapping[str, ModuleType | None],
    keep_validation_modules: bool,
) -> tuple[tuple[str, ModuleType], ...]:
    current_modules = sys.modules.copy()
    owned_plugin_modules = tuple(
        sorted(
            (
                (module_name, module)
                for module_name, module in current_modules.items()
                if module_name == validation_package_root or module_name.startswith(f"{validation_package_root}.")
            ),
            key=lambda item: item[0],
        ),
    )
    owned_modules_by_name = dict(owned_plugin_modules)
    for module_name, module in current_modules.items():
        if keep_validation_modules and owned_modules_by_name.get(module_name) is module:
            continue
        if module_name not in previous_modules_within_root and _module_origin_within_root(module, plugin_root):
            sys.modules.pop(module_name, None)
    sys.modules.update(previous_modules_within_root)
    if keep_validation_modules:
        return owned_plugin_modules
    plugin_module._restore_plugin_package_chain(dict(previous_packages))
    for module_name, _module in owned_plugin_modules:
        sys.modules.pop(module_name, None)
    return ()


def _execute_validation_plugin_module(
    plugin_name: str,
    plugin_root: Path,
    module_path: Path,
    registrations_by_module: dict[str, dict[str, ToolMetadata]],
    *,
    validation_plugin_name: str | None = None,
) -> tuple[str, tuple[HookCallback, ...], tuple[tuple[str, ModuleType], ...]]:
    """Execute one plugin module into a temporary validation import context."""
    validation_plugin_name = validation_plugin_name or (
        f"{plugin_name}{_VALIDATION_PLUGIN_MODULE_SUFFIX}{next(_VALIDATION_PLUGIN_GENERATIONS)}"
    )
    validation_module_name = plugin_module._module_name(validation_plugin_name, plugin_root, module_path)
    validation_package_root = validation_module_name.split(".", 1)[0]
    loaded_modules = sys.modules.copy()
    prepared_module = plugin_module._prepare_module(
        validation_plugin_name,
        plugin_root,
        module_path,
        validation_module_name,
    )
    if prepared_module is None:
        msg = f"Failed to load plugin validation module: {module_path}"
        raise ToolMetadataValidationError(msg)
    module, loader, previous_packages = prepared_module

    previous_modules_within_root = {
        module_name: loaded_module
        for module_name, loaded_module in loaded_modules.items()
        if _module_origin_within_root(loaded_module, plugin_root)
    }
    tasks_before_import = plugin_module._running_tasks()
    execution_error: BaseException | None = None
    created_task_count = 0
    discovered_hooks: tuple[HookCallback, ...] = ()
    owned_plugin_modules: tuple[tuple[str, ModuleType], ...] = ()
    try:
        with (
            scoped_plugin_registration_store(registrations_by_module),
            scoped_plugin_registration_owner(validation_module_name),
        ):
            loader.exec_module(module)
            from mindroom.hooks import iter_module_hooks  # noqa: PLC0415

            discovered_hooks = tuple(iter_module_hooks(module))
    except BaseException as exc:
        execution_error = exc
    finally:
        created_task_count = plugin_module._cancel_tasks_created_since(tasks_before_import)
        keep_validation_modules = execution_error is None and created_task_count == 0
        owned_plugin_modules = _finish_validation_plugin_import(
            plugin_root=plugin_root,
            validation_package_root=validation_package_root,
            previous_modules_within_root=previous_modules_within_root,
            previous_packages=previous_packages,
            keep_validation_modules=keep_validation_modules,
        )

    if execution_error is not None:
        if not isinstance(execution_error, Exception):
            raise execution_error
        msg = f"Plugin validation module execution failed for {module_path}: {execution_error}"
        raise ToolMetadataValidationError(msg) from execution_error
    if created_task_count:
        msg = f"Plugin validation module created async tasks during import: {module_path}"
        raise ToolMetadataValidationError(msg)
    return validation_module_name, discovered_hooks, owned_plugin_modules


_RESOLVED_TOOL_STATE_LOCK = threading.RLock()
_RESOLVED_TOOL_STATE_CACHE: dict[tuple[int, bool], tuple[RuntimePaths, _ResolvedToolState]] = {}
_BOUND_RUNTIME_TOOL_STATE_CACHE: dict[int, tuple[RuntimePaths, _ResolvedToolState]] = {}


def clear_resolved_tool_state_cache() -> None:
    """Drop recomputable authored-config state after live plugin registry changes."""
    with _RESOLVED_TOOL_STATE_LOCK:
        _RESOLVED_TOOL_STATE_CACHE.clear()


def bind_resolved_tool_state_cache(
    resolved_state: ResolvedToolRuntimeState,
    target_config: Config,
) -> None:
    """Bind carried tool state to the runtime config built from its validation snapshot."""
    target_key = id(target_config)
    with _RESOLVED_TOOL_STATE_LOCK:
        if target_key not in _BOUND_RUNTIME_TOOL_STATE_CACHE:
            weakref.finalize(target_config, _evict_bound_runtime_tool_state, target_key)
        _BOUND_RUNTIME_TOOL_STATE_CACHE[target_key] = (resolved_state.runtime_paths, resolved_state._state)


def _evict_bound_runtime_tool_state(cache_key: int) -> None:
    with _RESOLVED_TOOL_STATE_LOCK:
        _BOUND_RUNTIME_TOOL_STATE_CACHE.pop(cache_key, None)


def _evict_resolved_tool_state(cache_key: tuple[int, bool]) -> None:
    with _RESOLVED_TOOL_STATE_LOCK:
        _RESOLVED_TOOL_STATE_CACHE.pop(cache_key, None)


def _resolved_tool_state_for_runtime(
    runtime_paths: RuntimePaths,
    config: Config,
    *,
    tolerate_plugin_load_errors: bool = False,
) -> _ResolvedToolState:
    """Return registry and metadata visible for one runtime config without mutating global state.

    The state is computed once per config object and reused by every consumer
    (authored-entry validation, the tools API, self-config tools, worker
    snapshots): resolving it executes plugin modules and walks the filesystem,
    so recomputing per consumer stalls the event loop (#1260). The lock also
    serializes plugin validation, which temporarily rewires ``sys.modules``.
    """
    cache_key = (id(config), tolerate_plugin_load_errors)
    with _RESOLVED_TOOL_STATE_LOCK:
        bound = _BOUND_RUNTIME_TOOL_STATE_CACHE.get(id(config))
        if bound is not None and bound[0] == runtime_paths:
            return bound[1]
        cached = _RESOLVED_TOOL_STATE_CACHE.get(cache_key)
        if cached is not None and cached[0] == runtime_paths:
            return cached[1]
        resolved_state = _compute_resolved_tool_state_for_runtime(
            runtime_paths,
            config,
            tolerate_plugin_load_errors=tolerate_plugin_load_errors,
        )
        if cache_key not in _RESOLVED_TOOL_STATE_CACHE:
            weakref.finalize(config, _evict_resolved_tool_state, cache_key)
        _RESOLVED_TOOL_STATE_CACHE[cache_key] = (runtime_paths, resolved_state)
        return resolved_state


def _resolve_validation_plugin_modules(
    plugin_base: plugin_module._PluginBase,
    candidate_registrations: dict[str, dict[str, ToolMetadata]],
    candidate_active_plugins: list[tuple[str, str]],
    candidate_owned_modules: dict[str, ModuleType],
) -> tuple[HookCallback, ...]:
    """Resolve one plugin's validation modules and clean partial imports on failure."""
    candidate_hooks: tuple[HookCallback, ...] = ()
    validation_plugin_name = (
        f"{plugin_base.name}{_VALIDATION_PLUGIN_MODULE_SUFFIX}{next(_VALIDATION_PLUGIN_GENERATIONS)}"
    )
    try:
        if plugin_base.tools_module_path is None:
            if plugin_base.hooks_module_path is not None:
                _, candidate_hooks, hook_modules = _execute_validation_plugin_module(
                    plugin_base.name,
                    plugin_base.root,
                    plugin_base.hooks_module_path,
                    candidate_registrations,
                    validation_plugin_name=validation_plugin_name,
                )
                candidate_owned_modules.update(hook_modules)
        else:
            tool_module_name, tool_module_hooks, tool_modules = _execute_validation_plugin_module(
                plugin_base.name,
                plugin_base.root,
                plugin_base.tools_module_path,
                candidate_registrations,
                validation_plugin_name=validation_plugin_name,
            )
            candidate_owned_modules.update(tool_modules)
            candidate_active_plugins.append((plugin_base.name, tool_module_name))
            if (
                plugin_base.hooks_module_path is not None
                and plugin_base.hooks_module_path != plugin_base.tools_module_path
            ):
                _, candidate_hooks, hook_modules = _execute_validation_plugin_module(
                    plugin_base.name,
                    plugin_base.root,
                    plugin_base.hooks_module_path,
                    candidate_registrations,
                    validation_plugin_name=validation_plugin_name,
                )
                candidate_owned_modules.update(hook_modules)
            else:
                candidate_hooks = tool_module_hooks
    except BaseException:
        _remove_owned_plugin_modules(tuple(sorted(candidate_owned_modules.items())))
        raise
    return candidate_hooks


def _compute_resolved_tool_state_for_runtime(
    runtime_paths: RuntimePaths,
    config: Config,
    *,
    tolerate_plugin_load_errors: bool = False,
) -> _ResolvedToolState:
    """Resolve registry and metadata for one runtime config by executing its plugins."""
    import mindroom.tools  # noqa: F401, PLC0415
    from mindroom.hooks import HookRegistry  # noqa: PLC0415
    from mindroom.mcp.registry import resolved_mcp_tool_state  # noqa: PLC0415

    plugin_entries = config.plugins
    if not plugin_entries:
        builtin_registry = BUILTIN_TOOL_REGISTRY.copy()
        builtin_metadata = BUILTIN_TOOL_METADATA.copy()
        mcp_registry, mcp_metadata = resolved_mcp_tool_state(config)
        _merge_mcp_tool_state(
            builtin_registry,
            builtin_metadata,
            mcp_registry,
            mcp_metadata,
        )
        return _ResolvedToolState(builtin_registry, builtin_metadata, {}, HookRegistry.empty())

    plugin_bases = plugin_module._collect_plugin_bases(
        plugin_entries,
        runtime_paths,
        skip_broken_plugins=tolerate_plugin_load_errors,
    )

    plugin_module._reject_duplicate_plugin_manifest_names(plugin_bases)

    validation_registrations: dict[str, dict[str, ToolMetadata]] = {}
    unavailable_tool_metadata: dict[str, ToolMetadata] = {}
    active_plugins: list[tuple[str, str]] = []
    hook_plugins: list[_ResolvedPluginHooks] = []
    owned_plugin_modules: dict[str, ModuleType] = {}
    for plugin_base, plugin_entry, plugin_order in plugin_bases:
        candidate_registrations: dict[str, dict[str, ToolMetadata]] = {}
        candidate_active_plugins: list[tuple[str, str]] = []
        candidate_owned_modules: dict[str, ModuleType] = {}
        try:
            candidate_hooks = _resolve_validation_plugin_modules(
                plugin_base,
                candidate_registrations,
                candidate_active_plugins,
                candidate_owned_modules,
            )
        except BaseException as exc:
            if not isinstance(exc, Exception):
                _remove_owned_plugin_modules(tuple(sorted(owned_plugin_modules.items())))
                raise
            if not tolerate_plugin_load_errors:
                _remove_owned_plugin_modules(tuple(sorted(owned_plugin_modules.items())))
                raise
            plugin_module._log_skipped_plugin_entry(plugin_entry.path, plugin_base.root, exc)
            unavailable_tool_metadata.update(
                _unavailable_tool_metadata_from_failed_plugin(plugin_base, candidate_registrations),
            )
            continue

        validation_registrations.update(candidate_registrations)
        active_plugins.extend(candidate_active_plugins)
        owned_plugin_modules.update(candidate_owned_modules)
        hook_plugins.append(
            _ResolvedPluginHooks(
                name=plugin_base.name,
                entry_config=plugin_entry,
                plugin_order=plugin_order,
                discovered_hooks=candidate_hooks,
            ),
        )

    try:
        desired_registry, desired_metadata = resolved_tool_state(active_plugins, validation_registrations)
        mcp_registry, mcp_metadata = resolved_mcp_tool_state(config)
        _merge_mcp_tool_state(
            desired_registry,
            desired_metadata,
            mcp_registry,
            mcp_metadata,
        )
        unavailable_tool_metadata = _unavailable_tool_metadata_without_resolved_tools(
            unavailable_tool_metadata,
            desired_registry,
            desired_metadata,
        )
        hook_registry = HookRegistry.from_plugins(hook_plugins)
        module_owner = _OwnedPluginModules(tuple(sorted(owned_plugin_modules.items())))
    except BaseException:
        _remove_owned_plugin_modules(tuple(sorted(owned_plugin_modules.items())))
        raise
    return _ResolvedToolState(
        desired_registry,
        desired_metadata,
        unavailable_tool_metadata,
        hook_registry,
        module_owner,
    )


def _unavailable_tool_metadata_without_resolved_tools(
    unavailable_tool_metadata: Mapping[str, ToolMetadata],
    tool_registry: Mapping[str, Callable[[], type[Toolkit]]],
    tool_metadata: Mapping[str, ToolMetadata],
) -> dict[str, ToolMetadata]:
    """Drop skipped-plugin declarations that collide with available runtime tools."""
    resolved_tool_names = {*tool_registry, *tool_metadata}
    return {
        tool_name: metadata
        for tool_name, metadata in unavailable_tool_metadata.items()
        if tool_name not in resolved_tool_names
    }


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
    resolved_state = _resolved_tool_state_for_runtime(
        runtime_paths,
        config,
        tolerate_plugin_load_errors=tolerate_plugin_load_errors,
    )
    return resolved_state.tool_metadata


def resolved_hook_registry_for_runtime(
    runtime_paths: RuntimePaths,
    config: RuntimeConfig,
) -> HookRegistry:
    """Return the immutable hook registry carried by one runtime config."""
    resolved_state = _resolved_tool_state_for_runtime(
        runtime_paths,
        config,
    )
    return resolved_state.hook_registry


def refresh_mcp_tool_state_for_runtime(
    runtime_paths: RuntimePaths,
    config: RuntimeConfig,
) -> None:
    """Refresh MCP entries without rediscovering the config's committed plugins."""
    from mindroom.mcp.registry import resolved_mcp_tool_state  # noqa: PLC0415

    current_state = _resolved_tool_state_for_runtime(
        runtime_paths,
        config,
    )
    mcp_registry, _ = resolved_mcp_tool_state(config)
    mcp_tool_names = set(mcp_registry)
    refreshed_state = resolved_tool_runtime_state_from_registry(
        runtime_paths,
        config,
        {
            tool_name: factory
            for tool_name, factory in current_state.tool_registry.items()
            if tool_name not in mcp_tool_names
        },
        {
            tool_name: metadata
            for tool_name, metadata in current_state.tool_metadata.items()
            if tool_name not in mcp_tool_names
        },
        hook_registry=current_state.hook_registry,
        unavailable_tool_names=config.unavailable_plugin_tool_names,
        owned_plugin_modules=current_state.owned_plugin_modules,
    )
    bind_resolved_tool_state_cache(refreshed_state, config)


def _tool_validation_snapshot_from_state(
    tool_registry: Mapping[str, Callable[[], type[Toolkit]]],
    tool_metadata: Mapping[str, ToolMetadata],
    *,
    unavailable_plugin_tool_names: frozenset[str] = frozenset(),
) -> dict[str, ToolValidationInfo]:
    """Project runtime tool state into a validation-only snapshot."""
    return {
        tool_name: ToolValidationInfo(
            name=tool_name,
            config_fields=tuple(metadata.config_fields or ()),
            agent_override_fields=tuple(metadata.agent_override_fields or ()),
            authored_override_validator=metadata.authored_override_validator,
            requires_room_context=metadata.requires_room_context,
            runtime_loadable=tool_name in tool_registry,
            unavailable_due_to_plugin_load_error=tool_name in unavailable_plugin_tool_names,
        )
        for tool_name, metadata in tool_metadata.items()
    }


def _declared_tool_metadata_from_broken_plugin_source(module_path: Path) -> dict[str, ToolMetadata]:
    """Return literal tool declarations from a plugin module that failed before registration."""
    try:
        module_tree = ast.parse(module_path.read_text(encoding="utf-8"), filename=str(module_path))
    except (OSError, SyntaxError, UnicodeError):
        return {}

    metadata_by_name: dict[str, ToolMetadata] = {}
    for node in ast.walk(module_tree):
        if not isinstance(node, ast.Call):
            continue
        if not _is_register_tool_with_metadata_call(node.func):
            continue
        tool_name = _literal_string_keyword(node, "name")
        if tool_name is None:
            continue
        metadata_by_name[tool_name] = ToolMetadata(
            name=tool_name,
            display_name=_literal_string_keyword(node, "display_name") or tool_name,
            description=_literal_string_keyword(node, "description") or "Unavailable plugin tool",
            category=ToolCategory.INTEGRATIONS,
        )
    return metadata_by_name


def _is_register_tool_with_metadata_call(node: ast.expr) -> bool:
    if isinstance(node, ast.Name):
        return node.id == "register_tool_with_metadata"
    if isinstance(node, ast.Attribute):
        return node.attr == "register_tool_with_metadata"
    return False


def _literal_string_keyword(node: ast.Call, keyword_name: str) -> str | None:
    for keyword in node.keywords:
        if keyword.arg != keyword_name:
            continue
        if isinstance(keyword.value, ast.Constant) and isinstance(keyword.value.value, str):
            return keyword.value.value
    return None


def _unavailable_tool_metadata_from_failed_plugin(
    plugin_base: plugin_module._PluginBase,
    candidate_registrations: Mapping[str, dict[str, ToolMetadata]],
) -> dict[str, ToolMetadata]:
    """Return tools known to belong to one plugin that failed in tolerant startup mode."""
    metadata_by_name: dict[str, ToolMetadata] = {}
    for module_path in (plugin_base.tools_module_path, plugin_base.hooks_module_path):
        if module_path is not None:
            metadata_by_name.update(_declared_tool_metadata_from_broken_plugin_source(module_path))
    metadata_by_name.update(
        {
            tool_name: metadata
            for registrations in candidate_registrations.values()
            for tool_name, metadata in registrations.items()
        },
    )
    return metadata_by_name


def resolved_tool_runtime_state_for_runtime(
    runtime_paths: RuntimePaths,
    config: Config,
    *,
    tolerate_plugin_load_errors: bool = False,
) -> ResolvedToolRuntimeState:
    """Resolve the validation snapshot and runtime catalog in one carried value."""
    resolved_state = _resolved_tool_state_for_runtime(
        runtime_paths,
        config,
        tolerate_plugin_load_errors=tolerate_plugin_load_errors,
    )
    return _resolved_tool_runtime_state_snapshot(
        runtime_paths,
        resolved_state,
    )


def resolved_tool_runtime_state_from_registry(
    runtime_paths: RuntimePaths,
    config: Config,
    tool_registry: Mapping[str, Callable[[], type[Toolkit]]],
    tool_metadata: Mapping[str, ToolMetadata],
    *,
    hook_registry: HookRegistry | None = None,
    unavailable_tool_names: Iterable[str] = (),
    owned_plugin_modules: Mapping[str, ModuleType] | _OwnedPluginModules | None = None,
) -> ResolvedToolRuntimeState:
    """Build carried runtime state from one already-staged plugin registry."""
    from mindroom.hooks import HookRegistry  # noqa: PLC0415
    from mindroom.mcp.registry import resolved_mcp_tool_state  # noqa: PLC0415

    registry = dict(tool_registry)
    metadata = dict(tool_metadata)
    mcp_registry, mcp_metadata = resolved_mcp_tool_state(config)
    _merge_mcp_tool_state(registry, metadata, mcp_registry, mcp_metadata)
    unavailable_metadata = {
        tool_name: ToolMetadata(
            name=tool_name,
            display_name=tool_name,
            description="Unavailable plugin tool",
            category=ToolCategory.INTEGRATIONS,
        )
        for tool_name in unavailable_tool_names
        if tool_name not in metadata
    }
    module_owner = (
        _OwnedPluginModules(tuple(sorted(owned_plugin_modules.items())))
        if owned_plugin_modules is not None and not isinstance(owned_plugin_modules, _OwnedPluginModules)
        else owned_plugin_modules
    )
    return _resolved_tool_runtime_state_snapshot(
        runtime_paths,
        _ResolvedToolState(
            registry,
            metadata,
            unavailable_metadata,
            hook_registry or HookRegistry.empty(),
            module_owner,
        ),
    )


def _resolved_tool_runtime_state_snapshot(
    runtime_paths: RuntimePaths,
    resolved_state: _ResolvedToolState,
) -> ResolvedToolRuntimeState:
    """Project one resolved catalog into its validation and cache-binding value."""
    validation_metadata = {
        **resolved_state.tool_metadata,
        **resolved_state.unavailable_tool_metadata,
    }
    return ResolvedToolRuntimeState(
        runtime_paths=runtime_paths,
        validation_snapshot=_tool_validation_snapshot_from_state(
            resolved_state.tool_registry,
            validation_metadata,
            unavailable_plugin_tool_names=frozenset(resolved_state.unavailable_tool_metadata),
        ),
        _state=resolved_state,
    )


def resolved_tool_validation_snapshot_for_runtime(
    runtime_paths: RuntimePaths,
    config: Config,
    *,
    tolerate_plugin_load_errors: bool = False,
) -> dict[str, ToolValidationInfo]:
    """Return validation-only tool state visible for one runtime config."""
    resolved_state = resolved_tool_runtime_state_for_runtime(
        runtime_paths,
        config,
        tolerate_plugin_load_errors=tolerate_plugin_load_errors,
    )
    return dict(resolved_state.validation_snapshot)


def serialize_tool_validation_snapshot(
    tool_validation_snapshot: Mapping[str, ToolValidationInfo],
) -> dict[str, dict[str, object]]:
    """Export one validation snapshot as a JSON-serializable object."""
    return {
        tool_name: {
            "config_fields": [asdict(field) for field in info.config_fields],
            "agent_override_fields": [asdict(field) for field in info.agent_override_fields],
            "authored_override_validator": info.authored_override_validator.value,
            "requires_room_context": info.requires_room_context,
            "runtime_loadable": info.runtime_loadable,
            "unavailable_due_to_plugin_load_error": info.unavailable_due_to_plugin_load_error,
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
        raw_unavailable_due_to_plugin_load_error = raw_info_mapping.get("unavailable_due_to_plugin_load_error", False)
        if not isinstance(raw_unavailable_due_to_plugin_load_error, bool):
            msg = (
                f"Tool validation snapshot entry for '{tool_name}' must set "
                "unavailable_due_to_plugin_load_error to a boolean."
            )
            raise TypeError(msg)
        raw_requires_room_context = raw_info_mapping.get("requires_room_context", False)
        if not isinstance(raw_requires_room_context, bool):
            msg = f"Tool validation snapshot entry for '{tool_name}' must set requires_room_context to a boolean."
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
            requires_room_context=raw_requires_room_context,
            runtime_loadable=raw_runtime_loadable,
            unavailable_due_to_plugin_load_error=raw_unavailable_due_to_plugin_load_error,
        )
    return snapshot


def default_worker_routed_tools(
    tool_names: list[str],
    *,
    tool_metadata: Mapping[str, ToolMetadata] | None = None,
) -> list[str]:
    """Return the tool names that default to worker execution."""
    metadata_by_name = TOOL_METADATA if tool_metadata is None else tool_metadata
    selected_tools: list[str] = []
    for tool_name in tool_names:
        metadata = metadata_by_name.get(tool_name)
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


def normalize_authored_tool_overrides(
    tool_name: str,
    overrides: dict[str, object] | None,
    *,
    tool_metadata: Mapping[str, ToolMetadata | ToolValidationInfo] | None = None,
) -> dict[str, object]:
    """Validate and normalize one tool's authored per-agent overrides."""
    if not overrides:
        return {}

    metadata_by_name = TOOL_METADATA if tool_metadata is None else tool_metadata
    metadata = metadata_by_name.get(tool_name)
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


def authored_tool_overrides_to_runtime(
    tool_name: str,
    overrides: dict[str, object] | None,
    *,
    tool_metadata: Mapping[str, ToolMetadata | ToolValidationInfo] | None = None,
) -> dict[str, object] | None:
    """Convert normalized authored per-agent overrides into runtime kwargs."""
    normalized = normalize_authored_tool_overrides(tool_name, overrides, tool_metadata=tool_metadata)
    if not normalized:
        return None

    metadata_by_name = TOOL_METADATA if tool_metadata is None else tool_metadata
    metadata = metadata_by_name[tool_name]
    field_map = {field.name: field for field in metadata.agent_override_fields or []}
    runtime_overrides: dict[str, object] = {}
    for field_name, value in normalized.items():
        field = field_map[field_name]
        if field.type == "string[]":
            runtime_overrides[field_name] = ", ".join(cast("list[str]", value))
        else:
            runtime_overrides[field_name] = value
    return runtime_overrides or None

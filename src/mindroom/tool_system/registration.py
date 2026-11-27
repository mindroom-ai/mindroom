"""Leaf registration surface for tool implementations."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.tool_system.declarations import (
    ApprovalAlias,
    ConfigField,
    SetupType,
    ToolAuthoredOverrideValidator,
    ToolCategory,
    ToolExecutionTarget,
    ToolFileAccess,
    ToolManagedInitArg,
    ToolMetadata,
    ToolStatus,
)
from mindroom.tool_system.registry import register_tool

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence


def register_tool_with_metadata(
    *,
    name: str,
    display_name: str,
    description: str,
    category: ToolCategory,
    status: ToolStatus = ToolStatus.AVAILABLE,
    setup_type: SetupType = SetupType.NONE,
    default_execution_target: ToolExecutionTarget = ToolExecutionTarget.PRIMARY,
    requires_primary_runtime: bool = False,
    consumes_workspace_paths: bool = False,
    file_access: ToolFileAccess,
    executes_code: bool = False,
    requires_room_context: bool = False,
    requires_conversation: bool = False,
    icon: str | None = None,
    icon_color: str | None = None,
    config_fields: list[ConfigField] | None = None,
    agent_override_fields: list[ConfigField] | None = None,
    authored_override_validator: ToolAuthoredOverrideValidator = ToolAuthoredOverrideValidator.DEFAULT,
    dependencies: list[str] | None = None,
    auth_provider: str | None = None,
    oauth_fallback_fields: tuple[str, ...] = (),
    docs_url: str | None = None,
    helper_text: str | None = None,
    function_names: tuple[str, ...] = (),
    workspace_writers: tuple[str, ...] = (),
    approval_aliases: Mapping[str, ApprovalAlias] | None = None,
    action_scoped_grants: Sequence[str] = (),
    retired_functions: Mapping[str, str] | None = None,
    worker_inert_agent_functions: tuple[str, ...] = (),
    managed_init_args: tuple[ToolManagedInitArg, ...] = (),
    supports_toolkit_filters: bool = True,
) -> Callable[[Callable[[], type]], Callable[[], type]]:
    """Register a tool factory and its declarative metadata."""
    del worker_inert_agent_functions  # Retained as a no-op parameter for plugin API v1.x compatibility.
    if isinstance(action_scoped_grants, str):
        msg = "action_scoped_grants must be a sequence of function names, not a string"
        raise TypeError(msg)
    if oauth_fallback_fields:
        if setup_type != SetupType.OAUTH:
            msg = "OAuth fallback fields require setup_type=oauth"
            raise ValueError(msg)
        config_field_names = {field.name for field in config_fields or ()}
        missing_fields = sorted(set(oauth_fallback_fields) - config_field_names)
        if missing_fields:
            msg = f"OAuth fallback fields must reference config fields: {', '.join(missing_fields)}"
            raise ValueError(msg)

    def decorator(factory: Callable[[], type]) -> Callable[[], type]:
        metadata = ToolMetadata(
            name=name,
            display_name=display_name,
            description=description,
            category=category,
            status=status,
            setup_type=setup_type,
            default_execution_target=default_execution_target,
            requires_primary_runtime=requires_primary_runtime,
            consumes_workspace_paths=consumes_workspace_paths,
            file_access=file_access,
            executes_code=executes_code,
            requires_room_context=requires_room_context,
            requires_conversation=requires_conversation,
            icon=icon,
            icon_color=icon_color,
            config_fields=config_fields,
            agent_override_fields=agent_override_fields,
            authored_override_validator=authored_override_validator,
            dependencies=dependencies,
            auth_provider=auth_provider,
            oauth_fallback_fields=oauth_fallback_fields,
            docs_url=docs_url,
            helper_text=helper_text,
            function_names=function_names,
            workspace_writers=workspace_writers,
            approval_aliases=dict(approval_aliases or {}),
            action_scoped_grants=tuple(action_scoped_grants),
            retired_functions=dict(retired_functions or {}),
            managed_init_args=managed_init_args,
            supports_toolkit_filters=supports_toolkit_filters,
            factory=factory,
        )
        register_tool(metadata)
        return factory

    return decorator


register_builtin_tool_metadata = register_tool


__all__ = ["register_builtin_tool_metadata", "register_tool_with_metadata"]

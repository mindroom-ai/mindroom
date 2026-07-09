"""Leaf registration surface for tool implementations."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.tool_system.declarations import (
    ConfigField,
    SetupType,
    ToolAuthoredOverrideValidator,
    ToolCategory,
    ToolExecutionTarget,
    ToolManagedInitArg,
    ToolMetadata,
    ToolStatus,
)
from mindroom.tool_system.registry_state import (
    PLUGIN_MODULE_PREFIX,
    PLUGIN_REGISTRATION_SCOPE,
    register_builtin_tool_metadata,
    register_plugin_tool_metadata,
)

if TYPE_CHECKING:
    from collections.abc import Callable


def register_tool_with_metadata(
    *,
    name: str,
    display_name: str,
    description: str,
    category: ToolCategory,
    status: ToolStatus = ToolStatus.AVAILABLE,
    setup_type: SetupType = SetupType.NONE,
    default_execution_target: ToolExecutionTarget = ToolExecutionTarget.PRIMARY,
    consumes_workspace_paths: bool = False,
    requires_room_context: bool = False,
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
    """Register a tool factory and its declarative metadata."""

    def decorator(factory: Callable[[], type]) -> Callable[[], type]:
        metadata = ToolMetadata(
            name=name,
            display_name=display_name,
            description=description,
            category=category,
            status=status,
            setup_type=setup_type,
            default_execution_target=default_execution_target,
            consumes_workspace_paths=consumes_workspace_paths,
            requires_room_context=requires_room_context,
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
            factory=factory,
        )

        validation_owner_module_name = getattr(
            PLUGIN_REGISTRATION_SCOPE,
            "owner_module_name",
            None,
        )
        if validation_owner_module_name is not None:
            register_plugin_tool_metadata(validation_owner_module_name, metadata)
        elif factory.__module__.startswith(PLUGIN_MODULE_PREFIX):
            register_plugin_tool_metadata(factory.__module__, metadata)
        else:
            register_builtin_tool_metadata(metadata)
        return factory

    return decorator


__all__ = ["register_builtin_tool_metadata", "register_tool_with_metadata"]

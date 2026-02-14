"""Python tools configuration."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.tools_metadata import (
    ConfigField,
    SandboxRuntimeConfig,
    SetupType,
    ToolCategory,
    ToolStatus,
    register_tool_with_metadata,
)

if TYPE_CHECKING:
    from agno.tools.python import PythonTools


@register_tool_with_metadata(
    name="python",
    display_name="Python Tools",
    description="Execute Python code, manage files, and install packages",
    category=ToolCategory.DEVELOPMENT,
    status=ToolStatus.AVAILABLE,
    setup_type=SetupType.NONE,
    icon="SiPython",
    icon_color="text-blue-500",
    config_fields=[
        ConfigField(
            name="base_dir",
            label="Base Dir",
            type="text",
            required=False,
            default=None,
        ),
        ConfigField(
            name="safe_globals",
            label="Safe Globals",
            type="text",
            required=False,
            default=None,
        ),
        ConfigField(
            name="safe_locals",
            label="Safe Locals",
            type="text",
            required=False,
            default=None,
        ),
        ConfigField(
            name="restrict_to_base_dir",
            label="Restrict To Base Dir",
            type="boolean",
            required=False,
            default=True,
        ),
    ],
    dependencies=["agno"],
    docs_url="https://docs.agno.com/tools/toolkits/local/python",
    sandbox_runtime=SandboxRuntimeConfig(
        bind_workspace_to_base_dir=True,
        force_restrict_to_base_dir=True,
    ),
)
def python_tools() -> type[PythonTools]:
    """Return Python tools for code execution and file management."""
    from agno.tools.python import PythonTools

    return PythonTools

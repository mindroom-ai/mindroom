"""Python tools configuration."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.tools_metadata import (
    ConfigField,
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
    icon="FaPython",
    icon_color="text-blue-500",
    config_fields=[
        # Base configuration
        ConfigField(
            name="base_dir",
            label="Base Directory",
            type="text",
            required=False,
            placeholder="/path/to/working/directory",
            description="Base directory for file operations (defaults to current working directory)",
        ),
        # Execution environment
        ConfigField(
            name="safe_globals",
            label="Safe Globals",
            type="text",
            required=False,
            description="Custom global scope for code execution (advanced users only)",
        ),
        ConfigField(
            name="safe_locals",
            label="Safe Locals",
            type="text",
            required=False,
            description="Custom local scope for code execution (advanced users only)",
        ),
        # Core functionality
        ConfigField(
            name="save_and_run",
            label="Save and Run Files",
            type="boolean",
            required=False,
            default=True,
            description="Enable saving Python code to files and executing them",
        ),
        ConfigField(
            name="run_code",
            label="Run Code",
            type="boolean",
            required=False,
            default=False,
            description="Enable direct execution of Python code in memory",
        ),
        # File operations
        ConfigField(
            name="read_files",
            label="Read Files",
            type="boolean",
            required=False,
            default=False,
            description="Enable reading file contents",
        ),
        ConfigField(
            name="list_files",
            label="List Files",
            type="boolean",
            required=False,
            default=False,
            description="Enable listing files in the base directory",
        ),
        ConfigField(
            name="run_files",
            label="Run Files",
            type="boolean",
            required=False,
            default=False,
            description="Enable running existing Python files",
        ),
        # Package management
        ConfigField(
            name="pip_install",
            label="Pip Install",
            type="boolean",
            required=False,
            default=False,
            description="Enable installing packages using pip",
        ),
        ConfigField(
            name="uv_pip_install",
            label="UV Pip Install",
            type="boolean",
            required=False,
            default=False,
            description="Enable installing packages using uv pip",
        ),
    ],
    dependencies=["agno"],
    docs_url="https://docs.agno.com/tools/toolkits/local/python",
)
def python_tools() -> type[PythonTools]:
    """Return Python tools for code execution and file management."""
    from agno.tools.python import PythonTools

    return PythonTools

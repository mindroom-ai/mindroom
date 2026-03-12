"""Python tools configuration."""

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING, Any

from mindroom.tool_system.dependencies import install_command_for_current_python
from mindroom.tool_system.metadata import (
    ConfigField,
    SetupType,
    ToolCategory,
    ToolExecutionTarget,
    ToolStatus,
    register_tool_with_metadata,
)

if TYPE_CHECKING:
    from agno.tools.python import PythonTools


def _install_package_with_current_python(package_name: str) -> None:
    """Install one package into the current interpreter environment."""
    subprocess.check_call([*install_command_for_current_python(), package_name])


def _python_tools_runtime() -> tuple[Any, Any, Any, Any]:
    """Load Agno's Python tool runtime pieces lazily."""
    from agno.tools.python import PythonTools, log_debug, logger, warn

    return PythonTools, warn, log_debug, logger


@register_tool_with_metadata(
    name="python",
    display_name="Python Tools",
    description="Execute Python code, manage files, and install packages",
    category=ToolCategory.DEVELOPMENT,
    status=ToolStatus.AVAILABLE,
    setup_type=SetupType.NONE,
    default_execution_target=ToolExecutionTarget.WORKER,
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
)
def python_tools() -> type[PythonTools]:
    """Return Python tools for code execution and file management."""
    python_tools_class, warn, log_debug, logger = _python_tools_runtime()

    class MindRoomPythonTools(python_tools_class):
        """MindRoom wrapper around Agno's Python tool implementation."""

        def uv_pip_install_package(self, package_name: str) -> str:
            """Install a package into the current interpreter environment."""
            try:
                warn()
                log_debug(f"Installing package {package_name}")
                _install_package_with_current_python(package_name)
            except Exception as exc:
                logger.exception(f"Error installing package {package_name}")
                return f"Error installing package {package_name}: {exc}"
            return f"successfully installed package {package_name}"

    return MindRoomPythonTools

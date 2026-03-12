"""Python tools configuration."""

from __future__ import annotations

import shutil
import subprocess
import sys
from typing import TYPE_CHECKING

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
    uv_path = shutil.which("uv")
    if uv_path is not None:
        subprocess.check_call([uv_path, "pip", "install", "--python", sys.executable, package_name])
        return
    subprocess.check_call([sys.executable, "-m", "pip", "install", package_name])


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
    from agno.tools.python import PythonTools

    class MindRoomPythonTools(PythonTools):
        """MindRoom wrapper around Agno's Python tool implementation."""

        def uv_pip_install_package(self, package_name: str) -> str:
            """Install a package into the current interpreter environment."""
            try:
                _install_package_with_current_python(package_name)
            except Exception as exc:
                return f"Error installing package {package_name}: {exc}"
            return f"successfully installed package {package_name}"

    return MindRoomPythonTools

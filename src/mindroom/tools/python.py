"""Python tools configuration."""

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING, Any, Protocol

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
    from collections.abc import Callable
    from pathlib import Path

    from agno.tools.python import PythonTools


class _ExceptionLogger(Protocol):
    def exception(self, message: str) -> None: ...


def _install_package_with_current_python(package_name: str) -> None:
    """Install one package into the current interpreter environment."""
    subprocess.check_call([*install_command_for_current_python(), package_name])


def _install_package_with_status(
    package_name: str,
    *,
    warn: Callable[[], None],
    log_debug: Callable[[str], None],
    logger: _ExceptionLogger,
) -> str:
    """Install a package and format the tool response."""
    try:
        warn()
        log_debug(f"Installing package {package_name}")
        _install_package_with_current_python(package_name)
    except Exception as exc:
        logger.exception(f"Error installing package {package_name}")
        return f"Error installing package {package_name}: {exc}"
    return f"successfully installed package {package_name}"


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

        def __init__(
            self,
            base_dir: Path | None = None,
            safe_globals: dict[str, object] | None = None,
            safe_locals: dict[str, object] | None = None,
            restrict_to_base_dir: bool = True,
            **kwargs: object,
        ) -> None:
            super().__init__(
                base_dir=base_dir,
                safe_globals=safe_globals,
                safe_locals=safe_locals,
                restrict_to_base_dir=restrict_to_base_dir,
                **kwargs,
            )

        def pip_install_package(self, package_name: str) -> str:
            """Install a package into the current interpreter environment."""
            return _install_package_with_status(package_name, warn=warn, log_debug=log_debug, logger=logger)

        def uv_pip_install_package(self, package_name: str) -> str:
            """Backward-compatible alias for the shared installer path."""
            return _install_package_with_status(package_name, warn=warn, log_debug=log_debug, logger=logger)

    return MindRoomPythonTools

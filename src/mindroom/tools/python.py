"""Python tools configuration."""

from __future__ import annotations

import os
import subprocess
import threading
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any, Protocol

from mindroom.constants import RuntimePaths, runtime_env_values
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
    from collections.abc import Callable, Generator
    from pathlib import Path

    from agno.tools.python import PythonTools


_PYTHON_TOOL_RUNTIME_ENV_LOCK = threading.Lock()


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


@contextmanager
def _python_runtime_env_overlay(runtime_paths: RuntimePaths) -> Generator[None, None, None]:
    """Expose the committed runtime env during one in-process Python tool call."""
    env_values = dict(runtime_env_values(runtime_paths))
    with _PYTHON_TOOL_RUNTIME_ENV_LOCK:
        previous_env = {name: os.environ.get(name) for name in env_values}
        os.environ.update(env_values)
        try:
            yield
        finally:
            for name, previous_value in previous_env.items():
                if previous_value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = previous_value


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
            *,
            runtime_paths: RuntimePaths,
            **kwargs: object,
        ) -> None:
            super().__init__(
                base_dir=base_dir,
                safe_globals=safe_globals,
                safe_locals=safe_locals,
                restrict_to_base_dir=restrict_to_base_dir,
                **kwargs,
            )
            self._runtime_paths = runtime_paths

        def pip_install_package(self, package_name: str) -> str:
            """Install a package into the current interpreter environment."""
            return _install_package_with_status(package_name, warn=warn, log_debug=log_debug, logger=logger)

        def uv_pip_install_package(self, package_name: str) -> str:
            """Backward-compatible alias for the shared installer path."""
            return _install_package_with_status(package_name, warn=warn, log_debug=log_debug, logger=logger)

        def run_python_code(self, code: str, variable_to_return: str | None = None) -> str:
            """Execute Python code under the committed runtime env."""
            with _python_runtime_env_overlay(self._runtime_paths):
                return super().run_python_code(code, variable_to_return)

        def save_to_file_and_run(
            self,
            file_name: str,
            code: str,
            variable_to_return: str | None = None,
            overwrite: bool = True,
        ) -> str:
            """Execute file-backed Python code under the committed runtime env."""
            with _python_runtime_env_overlay(self._runtime_paths):
                return super().save_to_file_and_run(file_name, code, variable_to_return, overwrite)

        def run_python_file_return_variable(
            self,
            file_name: str,
            variable_to_return: str | None = None,
        ) -> str:
            """Run an existing Python file under the committed runtime env."""
            with _python_runtime_env_overlay(self._runtime_paths):
                return super().run_python_file_return_variable(file_name, variable_to_return)

    return MindRoomPythonTools

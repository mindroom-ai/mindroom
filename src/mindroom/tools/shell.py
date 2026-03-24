"""Shell tool configuration."""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING

from mindroom.constants import RuntimePaths, shell_execution_runtime_env_values
from mindroom.tool_system.metadata import (
    ConfigField,
    SetupType,
    ToolCategory,
    ToolExecutionTarget,
    ToolManagedInitArg,
    ToolStatus,
    register_tool_with_metadata,
)

if TYPE_CHECKING:
    from agno.tools.shell import ShellTools


_LOCAL_SHELL_PASSTHROUGH_ENV_KEYS = frozenset(
    {
        "CURL_CA_BUNDLE",
        "HOME",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "NO_PROXY",
        "PATH",
        "PYTHONPATH",
        "REQUESTS_CA_BUNDLE",
        "SSL_CERT_DIR",
        "SSL_CERT_FILE",
        "SHELL",
        "TERM",
        "TMPDIR",
        "USER",
        "VIRTUAL_ENV",
        "http_proxy",
        "https_proxy",
        "no_proxy",
    },
)
_NIXOS_SUDO_WRAPPER_DIR = "/run/wrappers/bin"


def _shell_subprocess_path(current_path: str | None) -> str | None:
    """Return the PATH value for shell subprocesses."""
    if not Path(_NIXOS_SUDO_WRAPPER_DIR).is_dir():
        return current_path

    if current_path is None or current_path == "":
        return _NIXOS_SUDO_WRAPPER_DIR

    path_entries = [p for p in current_path.split(os.pathsep) if p != _NIXOS_SUDO_WRAPPER_DIR]
    return os.pathsep.join([_NIXOS_SUDO_WRAPPER_DIR, *path_entries])


def _shell_subprocess_env(runtime_env: dict[str, str]) -> dict[str, str]:
    """Build the env passed to shell subprocesses."""
    env = {key: value for key, value in os.environ.items() if key in _LOCAL_SHELL_PASSTHROUGH_ENV_KEYS}
    env.update(runtime_env)

    path_value = _shell_subprocess_path(env.get("PATH"))
    if path_value is None:
        env.pop("PATH", None)
    else:
        env["PATH"] = path_value
    return env


@register_tool_with_metadata(
    name="shell",
    display_name="Shell Commands",
    description="Execute shell commands and scripts",
    category=ToolCategory.DEVELOPMENT,
    status=ToolStatus.AVAILABLE,
    setup_type=SetupType.NONE,
    default_execution_target=ToolExecutionTarget.WORKER,
    icon="Terminal",
    icon_color="text-green-500",
    config_fields=[
        ConfigField(
            name="base_dir",
            label="Base Dir",
            type="text",
            required=False,
            default=None,
        ),
        ConfigField(
            name="enable_run_shell_command",
            label="Enable Run Shell Command",
            type="boolean",
            required=False,
            default=True,
        ),
        ConfigField(
            name="all",
            label="All",
            type="boolean",
            required=False,
            default=False,
        ),
        ConfigField(
            name="extra_env_passthrough",
            label="Extra Env Passthrough",
            type="text",
            required=False,
            default=None,
            placeholder="WHISPER_URL, TTS_URL, CALDAV_*",
            description=(
                "Comma or newline-separated env var names or glob patterns to expose to shell "
                "execution in addition to the committed runtime env."
            ),
        ),
    ],
    managed_init_args=(ToolManagedInitArg.RUNTIME_PATHS,),
    dependencies=[],
    docs_url="https://docs.agno.com/tools/toolkits/local/shell",
)
def shell_tools() -> type[ShellTools]:
    """Return shell tools for command execution."""
    from agno.tools.shell import ShellTools

    class MindRoomShellTools(ShellTools):
        """MindRoom wrapper that runs shell commands with explicit runtime env passthrough."""

        def __init__(
            self,
            base_dir: Path | str | None = None,
            enable_run_shell_command: bool = True,
            all: bool = False,  # noqa: A002
            extra_env_passthrough: str | None = None,
            *,
            runtime_paths: RuntimePaths,
            **kwargs: object,
        ) -> None:
            super().__init__(
                base_dir=base_dir,
                enable_run_shell_command=enable_run_shell_command,
                all=all,
                **kwargs,
            )
            self._runtime_env = dict(
                shell_execution_runtime_env_values(
                    runtime_paths,
                    extra_env_passthrough=extra_env_passthrough,
                    process_env=runtime_paths.process_env,
                ),
            )

        def run_shell_command(self, args: list[str], tail: int = 100) -> str:
            import subprocess

            try:
                result = subprocess.run(
                    args,
                    capture_output=True,
                    text=True,
                    cwd=str(self.base_dir) if self.base_dir else None,
                    env=_shell_subprocess_env(self._runtime_env),
                    check=False,
                )
                if result.returncode != 0:
                    return f"Error: {result.stderr}"
                return "\n".join(result.stdout.split("\n")[-tail:])
            except Exception as exc:
                return f"Error: {exc}"

    return MindRoomShellTools

"""Minimal-mode Bash for agents whose shell already runs in the primary process.

Such an agent is fully trusted (see docs/architecture/security-posture.md), so
its Bash runs here like its ordinary shell and its CLI calls back over the local
API address. The response grant still scopes which tools the CLI can reach.
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import uuid4

from mindroom.agent_cli.shell_invocation import build_agent_cli_shell, invoke_agent_cli_shell
from mindroom.runtime_state import get_api_server_address
from mindroom.tool_system.sandbox_proxy import sandbox_proxy_enabled_for_tool
from mindroom.tools.shell import AgentCliShellBinding, retire_local_shell_namespace

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from agno.tools.toolkit import Toolkit

    from mindroom.agent_cli.session import CliGrant, TurnToolBridge
    from mindroom.agent_cli.worker_protocol import CliShellSettings
    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths

__all__ = ["LocalCliShell", "local_cli_shell_problems", "open_local_cli_shell", "shell_runs_in_primary"]


def shell_runs_in_primary(config: Config, runtime_paths: RuntimePaths, agent_name: str) -> bool:
    """Return whether this agent's shell commands execute in the primary process."""
    return not sandbox_proxy_enabled_for_tool(
        "shell",
        runtime_paths=runtime_paths,
        worker_tools_override=config.get_agent_worker_tools(agent_name),
    )


def _cli_executable() -> Path:
    """Return the `mindroom-agent` installed beside the running MindRoom interpreter."""
    return Path(sys.executable).parent / "mindroom-agent"


def local_cli_shell_problems() -> list[str]:
    """Return what keeps a local minimal shell from reaching this MindRoom, each phrased as its fix."""
    problems = []
    if get_api_server_address() is None:
        problems.append("Run MindRoom with its API server (without `--no-api`), because the CLI calls back through it.")
    if shutil.which("mindroom-agent", path=str(_cli_executable().parent)) is None:
        problems.append("Install MindRoom with its `mindroom-agent` command beside the running Python interpreter.")
    return problems


@dataclass(frozen=True)
class _LocalHandle:
    worker_id: str


@dataclass
class LocalCliShell:
    """One response's Bash in the primary, with a private grant file and its own handle namespace."""

    runtime_paths: RuntimePaths
    handle: _LocalHandle = field(default_factory=lambda: _LocalHandle(f"local:{uuid4().hex}"))
    _bridge: TurnToolBridge | None = field(default=None, init=False, repr=False)
    _private_dir: Path | None = field(default=None, init=False, repr=False)
    _namespace: str | None = field(default=None, init=False, repr=False)
    _toolkit: Toolkit | None = field(default=None, init=False, repr=False)

    async def install_grant(self, bridge: TurnToolBridge, grant: CliGrant, *, shell: CliShellSettings) -> None:
        """Write the grant where only this user can read it and bind a shell to this response."""
        if self._bridge is not None:
            msg = "Local CLI shell cannot accept another grant"
            raise RuntimeError(msg)
        if problems := local_cli_shell_problems():
            raise RuntimeError(" ".join(problems))
        api_address = get_api_server_address()
        assert api_address is not None
        self._bridge = bridge
        try:
            self._private_dir = Path(tempfile.mkdtemp(prefix="mindroom-agent-cli-"))
            token_path = self._private_dir / "capability"
            fd = os.open(token_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, "w") as stream:
                stream.write(grant.raw_token)
            owner = bridge.owner
            self._namespace = f"agent-cli:{self.handle.worker_id}:{owner.turn_id}:{owner.generation}"
            binding = AgentCliShellBinding(
                socket_path=None,
                namespace=self._namespace,
                handle=None,
                api_url=api_address.base_url,
                token_path=str(token_path),
            )
            # Expose only `mindroom-agent`, not MindRoom's interpreter and dependency commands;
            # configured prefixes still win, like in the agent's ordinary shell.
            bin_dir = self._private_dir / "bin"
            bin_dir.mkdir()
            (bin_dir / "mindroom-agent").symlink_to(_cli_executable())
            path_prepend = ",".join(part for part in (shell.shell_path_prepend, str(bin_dir)) if part)
            self._toolkit = build_agent_cli_shell(
                shell.model_copy(update={"shell_path_prepend": path_prepend}),
                runtime_paths=self.runtime_paths,
                binding=binding,
            )
        except BaseException:
            self.close()
            raise

    async def invoke_shell(self, function_name: str, arguments: dict[str, object]) -> object:
        """Run one canonical shell function for this response."""
        if self._toolkit is None:
            msg = "Local CLI shell is not active"
            raise RuntimeError(msg)
        return await invoke_agent_cli_shell(self._toolkit, function_name, arguments)

    def close(self) -> None:
        """Revoke the grant, stop this response's background commands, and remove the grant file."""
        self._toolkit = None
        if self._bridge is not None:
            self._bridge.revoke()
        if self._namespace is not None:
            # Nothing can reach these handles after the response, like a retired worker's.
            retire_local_shell_namespace(self._namespace)
            self._namespace = None
        if self._private_dir is not None:
            shutil.rmtree(self._private_dir, ignore_errors=True)
            self._private_dir = None


@asynccontextmanager
async def open_local_cli_shell(runtime_paths: RuntimePaths) -> AsyncIterator[LocalCliShell]:
    """Own one local minimal shell for a whole response, including its continuations."""
    shell = LocalCliShell(runtime_paths)
    try:
        yield shell
    finally:
        shell.close()

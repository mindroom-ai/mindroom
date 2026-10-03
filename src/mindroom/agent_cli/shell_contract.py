"""What minimal Bash uses of the agent's own shell: its canonical functions and the response's CLI environment."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Literal, get_args

if TYPE_CHECKING:
    from collections.abc import Iterator

_ShellOperationName = Literal["run_shell_command", "check_shell_command", "kill_shell_command"]
SHELL_OPERATION_NAMES: tuple[_ShellOperationName, ...] = get_args(_ShellOperationName)
AGENT_CLI_URL_ENV = "MINDROOM_AGENT_CLI_URL"
AGENT_CLI_TOKEN_ENV = "MINDROOM_AGENT_CLI_TOKEN"  # noqa: S105 - environment variable name
AGENT_CLI_WINDOW_ENV = "MINDROOM_AGENT_CLI_WINDOW"
# `mindroom-agent` sends its command's window back with each request.
AGENT_CLI_WINDOW_HEADER = "X-MindRoom-Agent-CLI-Window"


@dataclass(frozen=True, slots=True)
class AgentCliShellEnv:
    """How the agent's shell reaches this response's CLI routes.

    The grant only works during its response, so it travels with each command like
    any other shell environment value, wherever the agent's shell runs.
    """

    api_url: str
    token: str = field(repr=False)
    # Prepended to PATH where `mindroom-agent` is not already installed on it.
    bin_dir: str | None = None
    # The shell command this environment is exported to, so its CLI calls belong to it.
    window: str | None = None

    def env(self) -> dict[str, str]:
        """Return the variables `mindroom-agent` reads; only an environment bound to a command's window is exported."""
        assert self.window is not None, "export the CLI environment through bound_agent_cli_shell_env"
        return {AGENT_CLI_URL_ENV: self.api_url, AGENT_CLI_TOKEN_ENV: self.token, AGENT_CLI_WINDOW_ENV: self.window}


_CURRENT: ContextVar[AgentCliShellEnv | None] = ContextVar("agent_cli_shell_env", default=None)


@contextmanager
def bound_agent_cli_shell_env(shell_env: AgentCliShellEnv, *, window: str) -> Iterator[None]:
    """Export the response's CLI environment, naming the shell command's ``window``, to commands started here."""
    token = _CURRENT.set(replace(shell_env, window=window))
    try:
        yield
    finally:
        _CURRENT.reset(token)


def current_agent_cli_shell_env() -> AgentCliShellEnv | None:
    """Return the CLI environment of the shell command being started, if any."""
    return _CURRENT.get()

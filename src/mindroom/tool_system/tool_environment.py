"""The environment one tool call gives the shell commands it runs, beyond the shell tool's own.

Plugins' ``tool:environment`` hooks contribute once per call inside the tool-call policy, and the response's CLI grant
goes over them, so a command run by the local shell and one sent to a worker receive the same variables.
Each call keeps its environment in a context variable, so calls in other replies never see it and nothing edits the
process environment.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from mindroom.agent_cli.shell_contract import current_agent_cli_shell_env
from mindroom.constants import shell_extra_env_values

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping


@dataclass(frozen=True, slots=True)
class ToolEnvironment:
    """Changes to the environment of one tool call's shell commands.

    ``values`` maps a variable to its value, or to None to keep it from the command. ``pass_through`` holds names or
    glob patterns of MindRoom's own variables the command also receives, as the shell tool's ``extra_env_passthrough``
    setting does. ``path_prepend`` holds directories the command finds first on ``PATH``, before the shell tool's
    ``shell_path_prepend`` and a worker's own entries.
    """

    values: Mapping[str, str | None] = field(default_factory=dict)
    pass_through: tuple[str, ...] = ()
    path_prepend: tuple[str, ...] = ()

    def then(self, later: ToolEnvironment) -> ToolEnvironment:
        """Return this environment with ``later``'s changes applied over it."""
        return ToolEnvironment(
            values={**self.values, **later.values},
            pass_through=(*self.pass_through, *later.pass_through),
            path_prepend=(*later.path_prepend, *self.path_prepend),
        )

    @property
    def removed(self) -> frozenset[str]:
        """Return the variables kept from the command, even where its base environment would give them."""
        return frozenset(name for name, value in self.values.items() if value is None)

    def applied(self, env: Mapping[str, str], process_env: Mapping[str, str]) -> dict[str, str]:
        """Return ``env`` with the passed-through variables of ``process_env`` added, then the set and removed ones."""
        passed = shell_extra_env_values(extra_env_passthrough=",".join(self.pass_through), process_env=process_env)
        merged = {**env, **passed, **self.values}
        return {name: value for name, value in merged.items() if value is not None}


_COLLECTED: ContextVar[ToolEnvironment | None] = ContextVar("mindroom_tool_environment", default=None)


@contextmanager
def collected_tool_environment(environment: ToolEnvironment) -> Iterator[None]:
    """Give the shell commands of the tool call run here the environment its hooks collected."""
    token = _COLLECTED.set(environment)
    try:
        yield
    finally:
        _COLLECTED.reset(token)


def current_tool_environment() -> ToolEnvironment:
    """Return the environment of the tool call being run: its hooks' changes, with the response's CLI grant over them."""
    environment = _COLLECTED.get() or ToolEnvironment()
    grant = current_agent_cli_shell_env()
    if grant is None:
        return environment
    # The grant's launcher directory comes first, so an older `mindroom-agent` on the agent's configured PATH cannot
    # shadow this response's CLI; it stays out of the comma-separated setting, which would split a path with a comma.
    path_prepend = () if grant.bin_dir is None else (grant.bin_dir,)
    return environment.then(ToolEnvironment(values=grant.env(), path_prepend=path_prepend))

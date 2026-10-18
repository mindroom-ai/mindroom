"""One provider facade; canonical shell policy and execution belong to the turn owner."""

from __future__ import annotations

from typing import TYPE_CHECKING

from agno.tools.function import FunctionCall, ToolResult  # noqa: TC002 - Agno injected annotations
from agno.tools.toolkit import Toolkit

from mindroom.agno_compat_prepared_tools import BashPresentationFunction
from mindroom.tool_system.tool_access import ToolKey

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from agno.tools.function import Function


__all__ = ["MinimalBashTools"]


class MinimalBashTools(Toolkit):
    """Project only effective shell functions into one provider-visible function.

    The required execution owner retains the canonical prepared FunctionCall,
    approval state, hooks, operation mutex and dispatch-window lifetimes. There
    is intentionally no raw-worker or ordinary-proxy fallback here.
    """

    def __init__(
        self,
        *,
        execute: Callable[[ToolKey, dict[str, object], FunctionCall], Awaitable[str | ToolResult]] | None = None,
        on_prepare: Callable[[Function], None] | None = None,
    ) -> None:
        self._execute = execute
        super().__init__(name="minimal_bash", tools=[])
        function = BashPresentationFunction.from_callable(self.bash)
        assert isinstance(function, BashPresentationFunction)
        function.bind_preparation(on_prepare)
        self.async_functions["bash"] = function
        self.functions["bash"] = function

    async def bash(
        self,
        command: str | None = None,
        timeout: int | None = None,  # noqa: ASYNC109
        tail: int = 100,
        fc: FunctionCall | None = None,
    ) -> str | ToolResult:
        """Run a Bash command and return its output.

        Start servers and other long work detached with ``nohup cmd > out.log 2>&1 &`` and read the log in later
        commands.

        Args:
            command: The Bash command to run.
            timeout: Seconds after which the command and every process it started are stopped, returning the output
                so far; null stops it after 120 seconds, or never when it runs as part of a background job.
            tail: The number of output lines to return.
            fc: The provider's call, which the SDK supplies and the model never sees.

        """
        if not isinstance(command, str) or not command.strip():
            msg = "Bash requires a command"
            raise ValueError(msg)
        arguments: dict[str, object] = {"args": command, "tail": tail}
        if timeout is not None:
            arguments["timeout"] = timeout
        if self._execute is None or fc is None:
            msg = "Bash requires an active response owner"
            raise RuntimeError(msg)
        return await self._execute(ToolKey("shell", "run_shell_command"), arguments, fc)

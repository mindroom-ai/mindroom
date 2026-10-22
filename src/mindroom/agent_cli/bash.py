"""One provider facade; canonical shell policy and execution belong to the turn owner."""

from __future__ import annotations

from typing import TYPE_CHECKING

from agno.tools.function import Function
from agno.tools.toolkit import Toolkit

from mindroom.tool_system.invocation import current_tool_call
from mindroom.tool_system.tool_access import ToolKey
from mindroom.tool_system.tool_function import ToolValue  # noqa: TC001 - Agno resolves tool annotations

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable


__all__ = ["MinimalBashTools"]


class MinimalBashTools(Toolkit):
    """Project only effective shell functions into one provider-visible function.

    The required execution owner retains the canonical prepared call, approval
    state, hooks, operation mutex and dispatch-window lifetimes. There is
    intentionally no raw-worker or ordinary-proxy fallback here.
    """

    def __init__(
        self,
        *,
        execute: Callable[[ToolKey, dict[str, object], str], Awaitable[str | ToolValue]] | None = None,
    ) -> None:
        self._execute = execute
        super().__init__(name="minimal_bash", tools=[])
        # Derived once from the signature, as Agno derives a callable's schema, which the provider sees.
        function = Function.from_callable(self.bash)
        self.async_functions["bash"] = function
        self.functions["bash"] = function

    async def bash(
        self,
        command: str | None = None,
        timeout: int | None = None,  # noqa: ASYNC109
        tail: int = 100,
    ) -> str | ToolValue:
        """Run a Bash command and return its output.

        Start servers and other long work detached with ``nohup cmd > out.log 2>&1 &`` and read the log in later
        commands.

        Args:
            command: The Bash command to run.
            timeout: Seconds after which the command and every process it started are stopped, returning the output
                so far; null stops it after 120 seconds, or never when it runs as part of a background job.
            tail: The number of output lines to return.

        """
        if not isinstance(command, str) or not command.strip():
            msg = "Bash requires a command"
            raise ValueError(msg)
        arguments: dict[str, object] = {"args": command, "tail": tail}
        if timeout is not None:
            arguments["timeout"] = timeout
        call = current_tool_call()
        call_id = call.context.call_id if call is not None else None
        if self._execute is None or not call_id:
            msg = "Bash requires an active response owner"
            raise RuntimeError(msg)
        return await self._execute(ToolKey("shell", "run_shell_command"), arguments, call_id)

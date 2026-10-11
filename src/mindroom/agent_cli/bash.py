"""One provider facade; canonical shell policy and execution belong to the turn owner."""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from agno.tools.function import FunctionCall, ToolResult  # noqa: TC002 - Agno injected annotations
from agno.tools.toolkit import Toolkit

from mindroom.agno_compat_prepared_tools import BashPresentationFunction
from mindroom.shell_execution import DEFAULT_RUN_TIMEOUT_SECONDS
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
        operation: Literal["run", "poll", "stop"] = "run",
        command: str | None = None,
        handle: str | None = None,
        timeout: int = DEFAULT_RUN_TIMEOUT_SECONDS,  # noqa: ASYNC109
        tail: int = 100,
        fc: FunctionCall | None = None,
    ) -> str | ToolResult:
        """Run Bash, poll its background handle, or stop an owned command."""
        arguments: dict[str, object]
        if operation == "run":
            if not isinstance(command, str) or not command.strip() or handle is not None:
                msg = "run requires command and forbids handle"
                raise ValueError(msg)
            name = "run_shell_command"
            arguments = {"args": command, "timeout": timeout, "tail": tail}
        elif operation in {"poll", "stop"}:
            if not isinstance(handle, str) or not handle or command is not None:
                msg = "poll/stop require handle and forbid command"
                raise ValueError(msg)
            name = "check_shell_command" if operation == "poll" else "kill_shell_command"
            arguments = {"handle": handle}
            if operation == "stop":
                arguments["force"] = False
        else:
            msg = "Unsupported Bash operation"
            raise ValueError(msg)
        if self._execute is None or fc is None:
            msg = "Bash requires an active response owner"
            raise RuntimeError(msg)
        return await self._execute(ToolKey("shell", name), arguments, fc)

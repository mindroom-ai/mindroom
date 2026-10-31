"""One provider facade; canonical shell policy and execution belong to the turn owner."""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

from mindroom.agent_cli.shell_contract import SHELL_FUNCTION
from mindroom.tool_system.agno_toolkits import adapt_callable
from mindroom.tool_system.invocation import current_tool_call
from mindroom.tool_system.tool_access import ToolKey
from mindroom.tool_system.tool_function import AdaptedToolkit, ToolValue  # The schema reads it from module globals.
from mindroom.tool_system.tool_schema import closed_parameters

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable


__all__ = ["minimal_bash_toolkit"]


def minimal_bash_toolkit(
    execute: Callable[[ToolKey, dict[str, object], str], Awaitable[str | ToolValue]] | None = None,
) -> AdaptedToolkit:
    """Project only effective shell functions into one provider-visible function.

    The required execution owner retains the canonical prepared call, approval
    state, hooks, operation mutex and dispatch-window lifetimes. There is
    intentionally no raw-worker or ordinary-proxy fallback here.
    """

    async def bash(
        command: str,
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
        if not command.strip():
            msg = "Bash requires a command"
            raise ValueError(msg)
        arguments: dict[str, object] = {"args": command, "tail": tail}
        if timeout is not None:
            arguments["timeout"] = timeout
        call = current_tool_call()
        call_id = call.context.call_id if call is not None else None
        if execute is None or not call_id:
            msg = "Bash requires an active response owner"
            raise RuntimeError(msg)
        return await execute(ToolKey("shell", SHELL_FUNCTION), arguments, call_id)

    # TODO(simplify): the schema derived from the signature and docstring is closed to other properties as Agno closed
    # a schema it derived ahead of processing.
    function = adapt_callable(bash)
    function = replace(function, parameters=closed_parameters(bash, function.parameters))
    return AdaptedToolkit(name="minimal_bash", functions=(function,), instructions=None)

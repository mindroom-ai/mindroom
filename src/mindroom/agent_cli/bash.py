"""One provider facade; canonical shell policy and execution belong to the turn owner."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.tool_system.agno_toolkits import Function, adapt_function
from mindroom.tool_system.invocation import current_tool_call
from mindroom.tool_system.tool_access import ToolKey
from mindroom.tool_system.tool_function import AdaptedToolkit, ToolValue  # Agno reads ToolValue from module globals.

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

    # TODO(simplify): `command` is optional in the schema, yet every call without one fails. Make it a required
    # string. Models would see the changed tool schema.
    async def bash(
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
        if execute is None or not call_id:
            msg = "Bash requires an active response owner"
            raise RuntimeError(msg)
        return await execute(ToolKey("shell", "run_shell_command"), arguments, call_id)

    # The provider sees the schema Agno derives from the signature and docstring.
    function = adapt_function(Function.from_callable(bash))
    return AdaptedToolkit(name="minimal_bash", functions=(function,), instructions=None)

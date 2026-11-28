"""One provider facade; canonical shell policy and execution belong to the turn owner."""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

from mindroom.agent_cli.shell_contract import SHELL_FUNCTION
from mindroom.tool_system.invocation import current_tool_call
from mindroom.tool_system.tool_access import ToolKey
from mindroom.tool_system.tool_function import AdaptedToolkit, ToolValue  # The schema reads it from module globals.
from mindroom.tool_system.tool_schema import closed_parameters
from mindroom.tool_system.toolkits import adapt_callable

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable


__all__ = ["minimal_bash_toolkit"]


def _bash_description(*, sandboxed: bool = False) -> str:
    cli_clause = "" if sandboxed else ", and cannot make\nnew ``mindroom-agent`` CLI calls after handoff"
    return (
        "Run a Bash command and return its output.\n"
        "By default the call waits at most 120 seconds in the foreground. A command still running after 120 seconds\n"
        "(with ``timeout`` omitted or > 120) moves to the background and returns its PID, live log path, and\n"
        f"wait/stop commands; it stops automatically after ``timeout`` (if set) or when the turn ends{cli_clause}. "
        "Start servers or work meant to outlive the turn detached with\n"
        "``nohup cmd > out.log 2>&1 &`` and read the log in later commands. Wait for something inside one command,\n"
        "such as ``until <check>; do sleep 10; done`` with a ``timeout``, not with repeated ``sleep`` calls: every\n"
        "call is a model turn."
    )


def minimal_bash_toolkit(
    execute: Callable[[ToolKey, dict[str, object], str], Awaitable[str | ToolValue]] | None = None,
    *,
    sandboxed: bool = False,
) -> AdaptedToolkit:
    """Project the agent's shell function into one provider-visible Bash function.

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

        Args:
            command: The Bash command to run.
            timeout: Seconds after which the command and every process it started are stopped. Values <= 120 stop
                the command inline and return the output so far; null or > 120 moves it to the background after 120
                seconds and stops it after ``timeout`` (if set) or when the turn ends.
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

    function = adapt_callable(bash)
    function = replace(
        function,
        description=_bash_description(sandboxed=sandboxed),
        parameters=closed_parameters(bash, function.parameters),
    )
    return AdaptedToolkit(name="minimal_bash", functions=(function,), instructions=None)

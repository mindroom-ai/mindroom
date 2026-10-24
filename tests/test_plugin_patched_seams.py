"""Private seams that public plugins patch keep their names, keyword arguments, and call-time lookup.

mindroom-ai/agent-vault-bridge-plugin replaces these functions at import time to scope each tool call's
environment, so removing or renaming one, or calling it through a captured reference, breaks that plugin.
"""

from __future__ import annotations

import inspect

import pytest

from mindroom.tool_system import sandbox_proxy, tool_hooks
from mindroom.tool_system.call_record import recording_tool_calls
from mindroom.tools import shell


def _names(function: object) -> set[str]:
    return set(inspect.signature(function).parameters)


def test_patched_tool_call_seams_keep_the_arguments_plugins_pass() -> None:
    """The plugin forwards these keyword arguments to the functions it replaces."""
    assert {"func", "args", "tool_name", "agent_name"} <= _names(tool_hooks._call_tool)
    assert {"tool_name", "function_name", "kwargs", "execution_env", "extra_env_passthrough"} <= _names(
        sandbox_proxy._call_proxy_sync,
    )
    assert next(iter(inspect.signature(shell._shell_subprocess_env).parameters)) == "runtime_env"


class _Recorder:
    """Record started and finished calls the way a reply's tool-call record does."""

    def __init__(self) -> None:
        self.finished_results: list[object] = []

    async def started(self, tool_name: str, args: object) -> str:
        return f"{tool_name}-1"

    async def finished(self, call_id: str, tool_name: str, args: object, result: object) -> None:
        self.finished_results.append(result)


@pytest.mark.asyncio
@pytest.mark.parametrize("recorded", [False, True], ids=["unrecorded", "recorded"])
async def test_a_patched_tool_call_seam_wraps_every_recorded_call(
    monkeypatch: pytest.MonkeyPatch,
    *,
    recorded: bool,
) -> None:
    """The hook flow looks the tool-call seam up through the module, so a plugin's replacement runs.

    A reply's tool calls run with a recorder bound, so both branches must go through the seam.
    """
    seen: list[str] = []
    original = tool_hooks._call_tool

    async def wrapped(func: object, args: dict[str, object], *, tool_name: str, agent_name: str | None) -> object:
        seen.append(tool_name)
        return await original(func, args, tool_name=tool_name, agent_name=agent_name)

    monkeypatch.setattr(tool_hooks, "_call_tool", wrapped)

    async def lookup(query: str) -> str:
        return f"found {query}"

    recorder = _Recorder() if recorded else None
    with recording_tool_calls(recorder):
        result = await tool_hooks._call_recorded_tool(lookup, {"query": "x"}, tool_name="lookup", agent_name="code")

    assert result == "found x"
    assert seen == ["lookup"]
    if recorder is not None:
        assert recorder.finished_results == ["found x"]

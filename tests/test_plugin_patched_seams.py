"""Private seams that public plugins patch keep their names, keyword arguments, and call-time lookup.

mindroom-ai/agent-vault-bridge-plugin replaces these functions at import time to scope each tool call's
environment, so removing or renaming one, or calling it through a captured reference, breaks that plugin.
"""

from __future__ import annotations

import inspect

import pytest

from mindroom.tool_system import sandbox_proxy, tool_hooks
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


@pytest.mark.asyncio
async def test_a_patched_tool_call_seam_wraps_every_recorded_call(monkeypatch: pytest.MonkeyPatch) -> None:
    """The hook flow looks the tool-call seam up through the module, so a plugin's replacement runs."""
    seen: list[str] = []
    original = tool_hooks._call_tool

    async def wrapped(func: object, args: dict[str, object], *, tool_name: str, agent_name: str | None) -> object:
        seen.append(tool_name)
        return await original(func, args, tool_name=tool_name, agent_name=agent_name)

    monkeypatch.setattr(tool_hooks, "_call_tool", wrapped)

    async def lookup(query: str) -> str:
        return f"found {query}"

    result = await tool_hooks._call_recorded_tool(lookup, {"query": "x"}, tool_name="lookup", agent_name="code")

    assert result == "found x"
    assert seen == ["lookup"]

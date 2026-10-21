"""The tool-call policy runs as the loop's middleware around every call, with the call visible to the tool."""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest
from agno.tools import Toolkit

from mindroom.config.plugin import PluginEntryConfig
from mindroom.hooks import (
    EVENT_TOOL_AFTER_CALL,
    EVENT_TOOL_BEFORE_CALL,
    HookRegistry,
    ToolAfterCallContext,
    ToolBeforeCallContext,
    hook,
)
from mindroom.tool_system.agno_toolkits import adapt_toolkit
from mindroom.tool_system.call_record import recording_tool_calls
from mindroom.tool_system.invocation import ToolCallContext, current_tool_call, execute_tool_call
from mindroom.tool_system.tool_hooks import build_tool_hook_middleware

if TYPE_CHECKING:
    from collections.abc import Mapping


def _context() -> ToolCallContext:
    return ToolCallContext(run_id="run-1", session_id="session-1", call_id="call-1")


def _plugin(name: str, callbacks: list[object]) -> SimpleNamespace:
    return SimpleNamespace(
        name=name,
        discovered_hooks=tuple(callbacks),
        discovered_automations=(),
        entry_config=PluginEntryConfig(path=f"./plugins/{name}", settings={}),
        plugin_order=0,
    )


class _Probes(Toolkit):
    def __init__(self) -> None:
        super().__init__(name="probes", tools=[self.echo, self.whoami])
        self.calls: list[str] = []

    async def echo(self, text: str) -> str:
        """Return the text.

        Args:
            text: What to echo.

        """
        self.calls.append(text)
        return text.upper()

    async def whoami(self) -> str:
        """Return the id of the tool call this runs in."""
        call = current_tool_call()
        return call.context.call_id or "" if call is not None else "no call"


@pytest.mark.asyncio
async def test_before_hooks_block_a_call_and_after_hooks_see_results_and_blocks() -> None:
    """A declined call never reaches the tool; the after-call hook sees both the block and a real result."""
    seen: list[tuple[str, object, bool]] = []

    @hook(EVENT_TOOL_BEFORE_CALL)
    async def block_secret(ctx: ToolBeforeCallContext) -> None:
        if ctx.arguments.get("text") == "secret":
            ctx.decline("no secrets")

    @hook(EVENT_TOOL_AFTER_CALL)
    async def observe(ctx: ToolAfterCallContext) -> None:
        seen.append((ctx.tool_name, ctx.result, ctx.blocked))

    probes = _Probes()
    echo = next(function for function in adapt_toolkit(probes).functions if function.name == "echo")
    middleware = [
        build_tool_hook_middleware(HookRegistry.from_plugins([_plugin("policy", [block_secret, observe])]), "code"),
    ]

    allowed = await execute_tool_call(echo, {"text": "hi"}, _context(), middleware=middleware)
    blocked = await execute_tool_call(echo, {"text": "secret"}, _context(), middleware=middleware)

    assert allowed.content == "HI"
    assert probes.calls == ["hi"]
    assert "[TOOL CALL DECLINED]" in str(blocked.content)
    assert "no secrets" in str(blocked.content)
    assert [(name, blocked_flag) for name, _, blocked_flag in seen] == [("echo", False), ("echo", True)]
    assert seen[0][1] == "HI"


@pytest.mark.asyncio
async def test_every_call_is_recorded_before_it_runs_and_after_it_finishes() -> None:
    """The durable call recorder learns of a call before the tool runs and of its result afterwards."""
    events: list[tuple[str, object]] = []

    class _Recorder:
        async def started(self, tool_name: str, args: Mapping[str, object]) -> str:
            events.append(("started", (tool_name, dict(args))))
            return tool_name

        async def finished(self, call_id: str, tool_name: str, args: Mapping[str, object], result: object) -> None:
            del tool_name, args
            events.append(("finished", (call_id, result)))

    probes = _Probes()
    echo = next(function for function in adapt_toolkit(probes).functions if function.name == "echo")
    middleware = [build_tool_hook_middleware(HookRegistry.empty(), "code")]

    with recording_tool_calls(_Recorder()):
        outcome = await execute_tool_call(echo, {"text": "hi"}, _context(), middleware=middleware)

    assert outcome.content == "HI"
    assert events == [("started", ("echo", {"text": "hi"})), ("finished", ("echo", "HI"))]


@pytest.mark.asyncio
async def test_the_running_call_is_visible_to_the_tool_body_through_the_middleware() -> None:
    """``current_tool_call`` names the call being executed, even below the policy middleware."""
    probes = _Probes()
    whoami = next(function for function in adapt_toolkit(probes).functions if function.name == "whoami")
    middleware = [build_tool_hook_middleware(HookRegistry.empty(), "code")]

    outcome = await execute_tool_call(whoami, {}, _context(), middleware=middleware)

    assert outcome.content == "call-1"
    assert current_tool_call() is None

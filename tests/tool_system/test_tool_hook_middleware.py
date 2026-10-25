"""The tool-call policy runs as the loop's middleware around every call, with the call visible to the tool."""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import pytest
from agno.tools import Toolkit

from mindroom.agent_loop.tools import PlannedCall, dispatch_tool_calls
from mindroom.config.plugin import PluginEntryConfig
from mindroom.hooks import (
    EVENT_TOOL_AFTER_CALL,
    EVENT_TOOL_BEFORE_CALL,
    HookRegistry,
    ToolAfterCallContext,
    ToolBeforeCallContext,
    hook,
)
from mindroom.llm.messages import Message
from mindroom.llm.provider import ModelRequest, complete
from mindroom.tool_system.agno_toolkits import adapt_toolkit
from mindroom.tool_system.call_record import recording_tool_calls
from mindroom.tool_system.invocation import ToolCallContext, current_tool_call, execute_tool_call
from mindroom.tool_system.tool_hooks import build_tool_hook_middleware
from tests.agent_loop.helpers import context
from tests.providers.family_support import CLAUDE_DEPRECATION_FILTER, FAMILIES
from tests.providers.golden_support import Wire, hide_provider_environment

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path


def _context() -> ToolCallContext:
    return ToolCallContext(run_id="run-1", session_id="session-1", call_id="call-1", user_id=None)


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
        build_tool_hook_middleware(
            HookRegistry.from_plugins([_plugin("policy", [block_secret, observe])]),
            "code",
            dispatch_context=None,
            config=None,
            runtime_paths=None,
        ),
    ]

    allowed = await execute_tool_call(echo, {"text": "hi"}, _context(), middleware=middleware)
    blocked = await execute_tool_call(echo, {"text": "secret"}, _context(), middleware=middleware)

    assert allowed.content == "HI"
    assert probes.calls == ["hi"]
    assert "[TOOL CALL DECLINED]" in str(blocked.content)
    assert "no secrets" in str(blocked.content)
    assert [(name, blocked_flag) for name, _, blocked_flag in seen] == [("echo", False), ("echo", True)]
    assert seen[0][1] == "HI"


_DECLINED = (
    "[TOOL CALL DECLINED]\n"
    "Tool: echo\n"
    "Reason: no secrets\n\n"
    "Adjust your approach — try a different tool or different arguments."
)


def _sent_result(family: str, body: dict[str, Any]) -> dict[str, Any]:
    """Return the one tool result a request sends, as the wire encodes it."""
    if family == "anthropic":
        [block] = [
            block
            for message in body["messages"]
            for block in message["content"]
            if isinstance(block, dict) and block["type"] == "tool_result"
        ]
        # The prompt-cache marker the request adds to the newest result is not part of the result.
        return {key: value for key, value in block.items() if key != "cache_control"}
    [part] = [part for content in body["contents"] for part in content["parts"] if "functionResponse" in part]
    return part["functionResponse"]


@pytest.mark.asyncio
@pytest.mark.filterwarnings(CLAUDE_DEPRECATION_FILTER)
@pytest.mark.parametrize(
    ("family", "expected"),
    [
        ("anthropic", {"type": "tool_result", "tool_use_id": "call-1", "content": _DECLINED, "is_error": True}),
        ("gemini", {"id": "call-1", "name": "echo", "response": {"error": _DECLINED}}),
    ],
)
async def test_a_declined_call_reaches_claude_and_gemini_as_a_failed_call(
    family: str,
    expected: dict[str, Any],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The loop answers a call a before-call hook declined as failed, so Claude and Gemini read the decline as one."""
    hide_provider_environment(monkeypatch)

    @hook(EVENT_TOOL_BEFORE_CALL)
    async def block_secret(ctx: ToolBeforeCallContext) -> None:
        ctx.decline("no secrets")

    probes = _Probes()
    echo = next(function for function in adapt_toolkit(probes).functions if function.name == "echo")
    middleware = [
        build_tool_hook_middleware(
            HookRegistry.from_plugins([_plugin("policy", [block_secret])]),
            "code",
            dispatch_context=None,
            config=None,
            runtime_paths=None,
        ),
    ]
    planned = PlannedCall(function=echo, execution=echo.planned_execution("call-1", {"text": "secret"}))

    [answer] = await dispatch_tool_calls([planned], run=context(), middleware=middleware)
    wire = Wire(FAMILIES[family].reply())
    call = {"id": "call-1", "type": "function", "function": {"name": "echo", "arguments": '{"text": "secret"}'}}
    turn = [Message(role="user", content="Echo the secret."), Message(role="assistant", tool_calls=[call]), answer]
    await complete(FAMILIES[family].adapter(tmp_path, wire), ModelRequest(messages=turn, tools=[echo.definition()]))

    assert probes.calls == []
    assert _sent_result(family, wire.body()) == expected


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
    middleware = [
        build_tool_hook_middleware(
            HookRegistry.empty(),
            "code",
            dispatch_context=None,
            config=None,
            runtime_paths=None,
        ),
    ]

    with recording_tool_calls(_Recorder()):
        outcome = await execute_tool_call(echo, {"text": "hi"}, _context(), middleware=middleware)

    assert outcome.content == "HI"
    assert events == [("started", ("echo", {"text": "hi"})), ("finished", ("echo", "HI"))]


@pytest.mark.asyncio
async def test_the_running_call_is_visible_to_the_tool_body_through_the_middleware() -> None:
    """``current_tool_call`` names the call being executed, even below the policy middleware."""
    probes = _Probes()
    whoami = next(function for function in adapt_toolkit(probes).functions if function.name == "whoami")
    middleware = [
        build_tool_hook_middleware(
            HookRegistry.empty(),
            "code",
            dispatch_context=None,
            config=None,
            runtime_paths=None,
        ),
    ]

    outcome = await execute_tool_call(whoami, {}, _context(), middleware=middleware)

    assert outcome.content == "call-1"
    assert current_tool_call() is None

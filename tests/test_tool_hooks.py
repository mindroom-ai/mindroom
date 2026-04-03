"""Tests for tool call interception hooks."""

from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace
from typing import TYPE_CHECKING, ClassVar, Literal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from agno.models.ollama import Ollama
from agno.tools import Toolkit
from agno.tools.function import Function, FunctionCall

from mindroom.agents import create_agent
from mindroom.bot import AgentBot
from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.config.models import ModelConfig
from mindroom.config.plugin import PluginEntryConfig
from mindroom.hooks import (
    BUILTIN_EVENT_NAMES,
    EVENT_MESSAGE_RECEIVED,
    EVENT_TOOL_AFTER_CALL,
    EVENT_TOOL_BEFORE_CALL,
    CustomEventContext,
    HookRegistry,
    ToolAfterCallContext,
    ToolBeforeCallContext,
    emit_gate,
    hook,
)
from mindroom.hooks.execution import reset_hook_execution_state
from mindroom.hooks.types import RESERVED_EVENT_NAMESPACES, default_timeout_ms_for_event, validate_event_name
from mindroom.matrix.users import AgentMatrixUser
from mindroom.tool_system.metadata import _TOOL_REGISTRY, TOOL_METADATA, ToolCategory, register_tool_with_metadata
from mindroom.tool_system.runtime_context import ToolRuntimeContext, emit_custom_event, tool_runtime_context
from mindroom.tool_system.tool_hooks import build_tool_hook_bridge, prepend_tool_hook_bridge
from mindroom.tool_system.worker_routing import ToolExecutionIdentity, tool_execution_identity
from tests.conftest import bind_runtime_paths, runtime_paths_for, test_runtime_paths

if TYPE_CHECKING:
    from collections.abc import Generator
    from pathlib import Path

type SyncBridgeEvent = (
    tuple[Literal["before"], str, dict[str, str], str]
    | tuple[Literal["tool"], str]
    | tuple[Literal["after"], str, bool, str]
)


class NonDeepcopyableResult:
    """Mutable result object that deliberately breaks deepcopy()."""

    def __init__(self, payload: dict[str, str]) -> None:
        self.payload = payload

    def __deepcopy__(self, memo: dict[int, object]) -> object:
        """Force the snapshot path away from deepcopy()."""
        del memo
        msg = "result deepcopy disabled"
        raise TypeError(msg)

    def __repr__(self) -> str:
        """Keep the lossy fallback stable for assertions."""
        return f"NonDeepcopyableResult(payload={self.payload!r})"


class NonDeepcopyableToolError(ValueError):
    """Mutable exception object that deliberately breaks deepcopy()."""

    def __init__(self, message: str, details: dict[str, str]) -> None:
        super().__init__(message)
        self.details = details

    def __deepcopy__(self, memo: dict[int, object]) -> object:
        """Force the snapshot path away from deepcopy()."""
        del memo
        msg = "error deepcopy disabled"
        raise TypeError(msg)


def _config(
    tmp_path: Path,
    *,
    tools: list[str] | None = None,
    plugins: list[object] | None = None,
) -> Config:
    runtime_paths = test_runtime_paths(tmp_path)
    return bind_runtime_paths(
        Config(
            agents={
                "code": AgentConfig(
                    display_name="Code",
                    role="Help with coding.",
                    tools=tools or [],
                    rooms=["!room:localhost"],
                ),
            },
            models={"default": ModelConfig(provider="openai", id="test-model")},
            plugins=plugins or [],
        ),
        runtime_paths,
    )


def _plugin(
    name: str,
    callbacks: list[object],
    *,
    plugin_order: int = 0,
    settings: dict[str, object] | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        name=name,
        discovered_hooks=tuple(callbacks),
        entry_config=PluginEntryConfig(path=f"./plugins/{name}", settings=settings or {}),
        plugin_order=plugin_order,
    )


def _before_context(
    tmp_path: Path,
    *,
    agent_name: str = "code",
    room_id: str | None = "!room:localhost",
) -> ToolBeforeCallContext:
    config = _config(tmp_path)
    return ToolBeforeCallContext(
        tool_name="read_file",
        arguments={"path": "notes.txt"},
        agent_name=agent_name,
        room_id=room_id,
        thread_id="$thread",
        requester_id="@user:localhost",
        session_id="session-1",
        config=config,
        runtime_paths=runtime_paths_for(config),
        correlation_id="corr-tool",
    )


def _tool_runtime_context(
    tmp_path: Path,
    *,
    agent_name: str = "code",
    hook_message_sender: object | None = None,
) -> ToolRuntimeContext:
    config = _config(tmp_path)
    return ToolRuntimeContext(
        agent_name=agent_name,
        room_id="!room:localhost",
        thread_id="$thread",
        resolved_thread_id="$resolved-thread",
        requester_id="@user:localhost",
        client=AsyncMock(),
        config=config,
        runtime_paths=runtime_paths_for(config),
        correlation_id="corr-runtime",
        hook_message_sender=hook_message_sender,
    )


def _execution_identity() -> ToolExecutionIdentity:
    return ToolExecutionIdentity(
        channel="matrix",
        agent_name="code",
        requester_id="@user:localhost",
        room_id="!room:localhost",
        thread_id="$thread",
        resolved_thread_id="$resolved-thread",
        session_id="session-1",
    )


def _agent_bot(tmp_path: Path, *, config: Config, agent_name: str = "code") -> AgentBot:
    bot = AgentBot(
        agent_user=AgentMatrixUser(
            agent_name=agent_name,
            user_id=f"@mindroom_{agent_name}:localhost",
            display_name=agent_name.title(),
            password="test-password",  # noqa: S106
        ),
        storage_path=tmp_path,
        config=config,
        runtime_paths=runtime_paths_for(config),
        rooms=["!room:localhost"],
    )
    bot.client = MagicMock(rooms={})
    return bot


def _first_function(toolkit: Toolkit) -> Function:
    functions = [*toolkit.functions.values(), *toolkit.async_functions.values()]
    assert functions
    return functions[0]


@pytest.fixture(autouse=True)
def reset_execution_state() -> Generator[None, None, None]:
    """Keep global hook execution state isolated per test."""
    reset_hook_execution_state()
    yield
    reset_hook_execution_state()


def test_tool_events_are_registered_with_expected_timeouts() -> None:
    """Tool hook events should be valid built-ins with the expected defaults."""
    assert EVENT_TOOL_BEFORE_CALL in BUILTIN_EVENT_NAMES
    assert EVENT_TOOL_AFTER_CALL in BUILTIN_EVENT_NAMES
    assert validate_event_name(EVENT_TOOL_BEFORE_CALL) == EVENT_TOOL_BEFORE_CALL
    assert validate_event_name(EVENT_TOOL_AFTER_CALL) == EVENT_TOOL_AFTER_CALL
    assert "tool" in RESERVED_EVENT_NAMESPACES
    assert default_timeout_ms_for_event(EVENT_MESSAGE_RECEIVED) == 15000
    assert default_timeout_ms_for_event(EVENT_TOOL_BEFORE_CALL) == 200
    assert default_timeout_ms_for_event(EVENT_TOOL_AFTER_CALL) == 300


def test_tool_before_call_context_decline_helper() -> None:
    """ToolBeforeCallContext.decline() should set the mutable gate fields."""
    context = ToolBeforeCallContext(
        tool_name="read_file",
        arguments={"path": "secret.txt"},
        agent_name="code",
        room_id="!room:localhost",
        thread_id="$thread",
        requester_id="@user:localhost",
        session_id="session-1",
    )

    context.decline("blocked")

    assert context.declined is True
    assert context.decline_reason == "blocked"


@pytest.mark.asyncio
async def test_emit_gate_respects_priority_scope_and_first_decline(tmp_path: Path) -> None:
    """emit_gate() should run in priority order, apply scopes, and stop on the first decline."""
    seen: list[str] = []

    @hook(EVENT_TOOL_BEFORE_CALL, name="wrong-room", priority=5, rooms=["!elsewhere:localhost"])
    async def wrong_room(ctx: ToolBeforeCallContext) -> None:
        del ctx
        seen.append("wrong-room")

    @hook(EVENT_TOOL_BEFORE_CALL, priority=10, agents=["code"], rooms=["!room:localhost"])
    async def blocker(ctx: ToolBeforeCallContext) -> None:
        seen.append("blocker")
        ctx.decline("policy blocked the tool")

    @hook(EVENT_TOOL_BEFORE_CALL, priority=20)
    async def later(ctx: ToolBeforeCallContext) -> None:
        del ctx
        seen.append("later")

    registry = HookRegistry.from_plugins([_plugin("tool-policy", [later, wrong_room, blocker])])
    context = _before_context(tmp_path)

    await emit_gate(registry, EVENT_TOOL_BEFORE_CALL, context)

    assert seen == ["blocker"]
    assert context.declined is True
    assert context.decline_reason == "policy blocked the tool"


@pytest.mark.asyncio
async def test_emit_gate_isolates_arguments_between_hooks(tmp_path: Path) -> None:
    """Each gate hook should see the original arguments, even after earlier hook mutation."""
    seen: list[tuple[str, bool, list[str]]] = []

    @hook(EVENT_TOOL_BEFORE_CALL, priority=10)
    async def tamper(ctx: ToolBeforeCallContext) -> None:
        ctx.arguments["path"] = "notes.txt"
        ctx.arguments["options"]["allowed"] = True
        ctx.arguments["tags"].append("tampered")
        seen.append(
            (str(ctx.arguments["path"]), bool(ctx.arguments["options"]["allowed"]), list(ctx.arguments["tags"])),
        )

    @hook(EVENT_TOOL_BEFORE_CALL, priority=20)
    async def policy(ctx: ToolBeforeCallContext) -> None:
        seen.append(
            (str(ctx.arguments["path"]), bool(ctx.arguments["options"]["allowed"]), list(ctx.arguments["tags"])),
        )
        if "secret" in str(ctx.arguments["path"]):
            ctx.decline("secret paths stay blocked")

    registry = HookRegistry.from_plugins([_plugin("tool-policy", [tamper, policy])])
    context = _before_context(tmp_path)
    context.arguments = {
        "path": "secret.txt",
        "options": {"allowed": False},
        "tags": ["original"],
    }

    await emit_gate(registry, EVENT_TOOL_BEFORE_CALL, context)

    assert seen == [
        ("notes.txt", True, ["original", "tampered"]),
        ("secret.txt", False, ["original"]),
    ]
    assert context.arguments == {
        "path": "secret.txt",
        "options": {"allowed": False},
        "tags": ["original"],
    }
    assert context.declined is True
    assert context.decline_reason == "secret paths stay blocked"


@pytest.mark.asyncio
async def test_emit_gate_fails_open_on_errors_and_timeouts(tmp_path: Path) -> None:
    """emit_gate() should ignore hook failures and continue to later hooks."""
    seen: list[str] = []

    @hook(EVENT_TOOL_BEFORE_CALL, priority=10)
    async def broken(ctx: ToolBeforeCallContext) -> None:
        seen.append("broken")
        ctx.decline("do not keep this decline")
        msg = "boom"
        raise RuntimeError(msg)

    @hook(EVENT_TOOL_BEFORE_CALL, priority=20, timeout_ms=10)
    async def slow(ctx: ToolBeforeCallContext) -> None:
        del ctx
        seen.append("slow")
        await asyncio.sleep(0.05)

    @hook(EVENT_TOOL_BEFORE_CALL, priority=30)
    async def allowed(ctx: ToolBeforeCallContext) -> None:
        del ctx
        seen.append("allowed")

    registry = HookRegistry.from_plugins([_plugin("tool-policy", [broken, slow, allowed])])
    context = _before_context(tmp_path)

    await emit_gate(registry, EVENT_TOOL_BEFORE_CALL, context)

    assert seen == ["broken", "slow", "allowed"]
    assert context.declined is False
    assert context.decline_reason == ""


def test_build_tool_hook_bridge_returns_none_without_tool_hooks() -> None:
    """The hot path should stay a no-op when no tool hooks are registered."""
    assert build_tool_hook_bridge(HookRegistry.empty(), agent_name="code") is None


def test_sync_function_call_execute_runs_tool_hooks(tmp_path: Path) -> None:
    """Sync FunctionCall.execute() should still fire the bridge hooks."""
    seen: list[SyncBridgeEvent] = []

    @hook(EVENT_TOOL_BEFORE_CALL)
    async def before(ctx: ToolBeforeCallContext) -> None:
        seen.append(("before", ctx.tool_name, dict(ctx.arguments), ctx.thread_id))

    @hook(EVENT_TOOL_AFTER_CALL)
    async def after(ctx: ToolAfterCallContext) -> None:
        seen.append(("after", ctx.result, ctx.blocked, ctx.thread_id))

    registry = HookRegistry.from_plugins([_plugin("tool-policy", [before, after])])
    bridge = build_tool_hook_bridge(
        registry,
        agent_name="code",
        execution_identity=_execution_identity(),
    )
    assert bridge is not None

    class DemoToolkit(Toolkit):
        def __init__(self) -> None:
            super().__init__(name="demo", tools=[self.echo])

        def echo(self, text: str) -> str:
            seen.append(("tool", text))
            return text.upper()

    toolkit = DemoToolkit()
    function = _first_function(toolkit)
    prepend_tool_hook_bridge(toolkit, bridge)

    with (
        patch("agno.tools.function.log_warning") as mock_log_warning,
        tool_runtime_context(_tool_runtime_context(tmp_path)),
        tool_execution_identity(_execution_identity()),
    ):
        result = FunctionCall(function=function, arguments={"text": "hi"}, call_id="call-1").execute()

    assert result.status == "success"
    assert result.result == "HI"
    mock_log_warning.assert_not_called()
    assert seen == [
        ("before", "echo", {"text": "hi"}, "$resolved-thread"),
        ("tool", "hi"),
        ("after", "HI", False, "$resolved-thread"),
    ]


@pytest.mark.asyncio
async def test_sync_tool_function_call_aexecute_runs_tool_hooks(tmp_path: Path) -> None:
    """Async execution should still run the bridge for sync tool entrypoints."""
    seen: list[SyncBridgeEvent] = []

    @hook(EVENT_TOOL_BEFORE_CALL)
    async def before(ctx: ToolBeforeCallContext) -> None:
        seen.append(("before", ctx.tool_name, dict(ctx.arguments), ctx.thread_id))

    @hook(EVENT_TOOL_AFTER_CALL)
    async def after(ctx: ToolAfterCallContext) -> None:
        seen.append(("after", ctx.result, ctx.blocked, ctx.thread_id))

    registry = HookRegistry.from_plugins([_plugin("tool-policy", [before, after])])
    bridge = build_tool_hook_bridge(
        registry,
        agent_name="code",
        execution_identity=_execution_identity(),
    )
    assert bridge is not None

    class DemoToolkit(Toolkit):
        def __init__(self) -> None:
            super().__init__(name="demo", tools=[self.echo])

        def echo(self, text: str) -> str:
            seen.append(("tool", text))
            return text.upper()

    toolkit = DemoToolkit()
    function = _first_function(toolkit)
    prepend_tool_hook_bridge(toolkit, bridge)

    with tool_runtime_context(_tool_runtime_context(tmp_path)), tool_execution_identity(_execution_identity()):
        result = await FunctionCall(function=function, arguments={"text": "hi"}, call_id="call-1").aexecute()

    assert result.status == "success"
    assert result.result == "HI"
    assert seen == [
        ("before", "echo", {"text": "hi"}, "$resolved-thread"),
        ("tool", "hi"),
        ("after", "HI", False, "$resolved-thread"),
    ]


@pytest.mark.asyncio
async def test_tool_hook_bridge_allows_call_and_populates_contexts(tmp_path: Path) -> None:
    """The bridge should pass through allowed calls and emit before/after contexts."""
    before_seen: list[tuple[str, dict[str, object], str, str | None, str | None, str | None, str | None]] = []
    after_seen: list[
        tuple[object | None, BaseException | None, bool, str | None, str | None, str | None, str | None]
    ] = []

    @hook(EVENT_TOOL_BEFORE_CALL)
    async def before(ctx: ToolBeforeCallContext) -> None:
        before_seen.append(
            (
                ctx.tool_name,
                dict(ctx.arguments),
                ctx.agent_name,
                ctx.room_id,
                ctx.thread_id,
                ctx.requester_id,
                ctx.session_id,
            ),
        )

    @hook(EVENT_TOOL_AFTER_CALL)
    async def after(ctx: ToolAfterCallContext) -> None:
        after_seen.append(
            (
                ctx.result,
                ctx.error,
                ctx.blocked,
                ctx.room_id,
                ctx.thread_id,
                ctx.requester_id,
                ctx.session_id,
            ),
        )

    registry = HookRegistry.from_plugins([_plugin("tool-policy", [before, after])])
    bridge = build_tool_hook_bridge(
        registry,
        agent_name="code",
        execution_identity=_execution_identity(),
    )
    assert bridge is not None

    async def next_func(**kwargs: object) -> dict[str, object]:
        return {"echo": kwargs["path"]}

    with tool_runtime_context(_tool_runtime_context(tmp_path)), tool_execution_identity(_execution_identity()):
        result = await bridge("read_file", next_func, {"path": "notes.txt"})

    assert result == {"echo": "notes.txt"}
    assert before_seen == [
        (
            "read_file",
            {"path": "notes.txt"},
            "code",
            "!room:localhost",
            "$resolved-thread",
            "@user:localhost",
            "session-1",
        ),
    ]
    assert after_seen == [
        (
            {"echo": "notes.txt"},
            None,
            False,
            "!room:localhost",
            "$resolved-thread",
            "@user:localhost",
            "session-1",
        ),
    ]


@pytest.mark.asyncio
async def test_tool_after_call_hooks_cannot_mutate_returned_result(tmp_path: Path) -> None:
    """After-call hooks should observe results without mutating the returned value."""

    @hook(EVENT_TOOL_AFTER_CALL)
    async def mutate(ctx: ToolAfterCallContext) -> None:
        assert isinstance(ctx.result, dict)
        ctx.result["mutated"] = True

    registry = HookRegistry.from_plugins([_plugin("tool-policy", [mutate])])
    bridge = build_tool_hook_bridge(
        registry,
        agent_name="code",
        execution_identity=_execution_identity(),
    )
    assert bridge is not None

    async def next_func(**kwargs: object) -> dict[str, object]:
        return {"echo": kwargs["path"]}

    with tool_runtime_context(_tool_runtime_context(tmp_path)), tool_execution_identity(_execution_identity()):
        result = await bridge("read_file", next_func, {"path": "notes.txt"})

    assert result == {"echo": "notes.txt"}


@pytest.mark.asyncio
async def test_tool_after_call_hooks_cannot_mutate_non_deepcopyable_result(tmp_path: Path) -> None:
    """After-call hooks should still get an isolated snapshot when deepcopy() fails."""
    seen: list[object | None] = []

    @hook(EVENT_TOOL_AFTER_CALL)
    async def mutate(ctx: ToolAfterCallContext) -> None:
        seen.append(ctx.result)
        assert ctx.result == "NonDeepcopyableResult(payload={'echo': 'notes.txt'})"

    registry = HookRegistry.from_plugins([_plugin("tool-policy", [mutate])])
    bridge = build_tool_hook_bridge(
        registry,
        agent_name="code",
        execution_identity=_execution_identity(),
    )
    assert bridge is not None

    async def next_func(**kwargs: object) -> NonDeepcopyableResult:
        return NonDeepcopyableResult({"echo": str(kwargs["path"])})

    with tool_runtime_context(_tool_runtime_context(tmp_path)), tool_execution_identity(_execution_identity()):
        result = await bridge("read_file", next_func, {"path": "notes.txt"})

    assert isinstance(result, NonDeepcopyableResult)
    assert result.payload == {"echo": "notes.txt"}
    assert seen == ["NonDeepcopyableResult(payload={'echo': 'notes.txt'})"]


@pytest.mark.asyncio
async def test_tool_hook_context_send_message_uses_bound_sender(tmp_path: Path) -> None:
    """Tool hook contexts should route send_message() through the active hook sender."""
    sent: list[tuple[str, str, str | None, str, dict[str, object] | None, bool]] = []

    async def hook_message_sender(
        room_id: str,
        body: str,
        thread_id: str | None,
        source_hook: str,
        extra_content: dict[str, object] | None,
        *,
        trigger_dispatch: bool = False,
    ) -> str | None:
        sent.append((room_id, body, thread_id, source_hook, extra_content, trigger_dispatch))
        return f"${body}-event"

    @hook(EVENT_TOOL_BEFORE_CALL)
    async def before(ctx: ToolBeforeCallContext) -> None:
        event_id = await ctx.send_message(
            "!room:localhost",
            "before",
            thread_id="$before-thread",
            extra_content={"phase": "before"},
        )
        assert event_id == "$before-event"

    @hook(EVENT_TOOL_AFTER_CALL)
    async def after(ctx: ToolAfterCallContext) -> None:
        event_id = await ctx.send_message(
            "!room:localhost",
            "after",
            thread_id="$after-thread",
            extra_content={"phase": "after"},
        )
        assert event_id == "$after-event"

    registry = HookRegistry.from_plugins([_plugin("tool-policy", [before, after])])
    bridge = build_tool_hook_bridge(
        registry,
        agent_name="code",
        execution_identity=_execution_identity(),
    )
    assert bridge is not None

    async def next_func(**kwargs: object) -> dict[str, object]:
        return {"echo": kwargs["path"]}

    with (
        tool_runtime_context(_tool_runtime_context(tmp_path, hook_message_sender=hook_message_sender)),
        tool_execution_identity(_execution_identity()),
    ):
        result = await bridge("read_file", next_func, {"path": "notes.txt"})

    assert result == {"echo": "notes.txt"}
    assert sent == [
        (
            "!room:localhost",
            "before",
            "$before-thread",
            "tool-policy:tool:before_call",
            {
                "phase": "before",
                "com.mindroom.original_sender": "@user:localhost",
            },
            False,
        ),
        (
            "!room:localhost",
            "after",
            "$after-thread",
            "tool-policy:tool:after_call",
            {
                "phase": "after",
                "com.mindroom.original_sender": "@user:localhost",
            },
            False,
        ),
    ]


@pytest.mark.asyncio
async def test_tool_hook_context_room_state_helpers_use_runtime_client(tmp_path: Path) -> None:
    """Tool hook contexts should expose live room-state helpers from the active runtime client."""
    sent: list[tuple[str, str, str | None, str, dict[str, object] | None, bool]] = []

    async def hook_message_sender(
        room_id: str,
        body: str,
        thread_id: str | None,
        source_hook: str,
        extra_content: dict[str, object] | None,
        *,
        trigger_dispatch: bool = False,
    ) -> str | None:
        sent.append((room_id, body, thread_id, source_hook, extra_content, trigger_dispatch))
        return "$dispatch-event"

    @hook(EVENT_TOOL_BEFORE_CALL)
    async def before(ctx: ToolBeforeCallContext) -> None:
        query_result = await ctx.query_room_state("!room:localhost", "m.room.name", "")
        put_result = await ctx.put_room_state(
            "!room:localhost",
            "com.mindroom.thread.tags",
            "$resolved-thread",
            {"tags": {"queued": True}},
        )
        event_id = await ctx.send_message("!room:localhost", "dispatch", trigger_dispatch=True)
        assert query_result == {"name": "Lobby"}
        assert put_result is True
        assert event_id == "$dispatch-event"

    registry = HookRegistry.from_plugins([_plugin("tool-policy", [before])])
    bridge = build_tool_hook_bridge(
        registry,
        agent_name="code",
        execution_identity=_execution_identity(),
    )
    assert bridge is not None

    async def next_func(**kwargs: object) -> dict[str, object]:
        return {"echo": kwargs["path"]}

    runtime_context = _tool_runtime_context(tmp_path, hook_message_sender=hook_message_sender)
    runtime_context.client.room_get_state_event.return_value = SimpleNamespace(content={"name": "Lobby"})
    runtime_context.client.room_put_state.return_value = object()

    with tool_runtime_context(runtime_context), tool_execution_identity(_execution_identity()):
        result = await bridge("read_file", next_func, {"path": "notes.txt"})

    assert result == {"echo": "notes.txt"}
    runtime_context.client.room_get_state_event.assert_awaited_once_with("!room:localhost", "m.room.name", "")
    runtime_context.client.room_put_state.assert_awaited_once_with(
        "!room:localhost",
        "com.mindroom.thread.tags",
        {"tags": {"queued": True}},
        state_key="$resolved-thread",
    )
    assert sent == [
        (
            "!room:localhost",
            "dispatch",
            None,
            "tool-policy:tool:before_call",
            {
                "com.mindroom.original_sender": "@user:localhost",
            },
            True,
        ),
    ]


@pytest.mark.asyncio
async def test_sync_tool_aexecute_send_message_uses_request_loop(tmp_path: Path) -> None:
    """Sync-tool hooks should keep send_message() on the active request loop under aexecute()."""
    request_thread = threading.get_ident()
    request_loop = asyncio.get_running_loop()
    seen: list[tuple[str, int, int] | tuple[str, str]] = []

    async def hook_message_sender(
        room_id: str,
        body: str,
        thread_id: str | None,
        source_hook: str,
        extra_content: dict[str, object] | None,
        *,
        trigger_dispatch: bool = False,
    ) -> str | None:
        del room_id, body, thread_id, source_hook, extra_content, trigger_dispatch
        current_loop = asyncio.get_running_loop()
        current_thread = threading.get_ident()
        seen.append(("sender", current_thread, id(current_loop)))
        assert current_thread == request_thread
        assert current_loop is request_loop
        return "$ok"

    @hook(EVENT_TOOL_BEFORE_CALL)
    async def before(ctx: ToolBeforeCallContext) -> None:
        current_loop = asyncio.get_running_loop()
        current_thread = threading.get_ident()
        seen.append(("hook", current_thread, id(current_loop)))
        assert current_thread == request_thread
        assert current_loop is request_loop
        event_id = await ctx.send_message("!room:localhost", "before")
        seen.append(("event_id", event_id or ""))

    registry = HookRegistry.from_plugins([_plugin("tool-policy", [before])])
    bridge = build_tool_hook_bridge(
        registry,
        agent_name="code",
        execution_identity=_execution_identity(),
    )
    assert bridge is not None

    class DemoToolkit(Toolkit):
        def __init__(self) -> None:
            super().__init__(name="demo", tools=[self.echo])

        def echo(self, text: str) -> str:
            current_loop = asyncio.get_running_loop()
            current_thread = threading.get_ident()
            seen.append(("tool", current_thread, id(current_loop)))
            assert current_thread == request_thread
            assert current_loop is request_loop
            return text.upper()

    toolkit = DemoToolkit()
    function = _first_function(toolkit)
    prepend_tool_hook_bridge(toolkit, bridge)

    with (
        tool_runtime_context(_tool_runtime_context(tmp_path, hook_message_sender=hook_message_sender)),
        tool_execution_identity(_execution_identity()),
    ):
        result = await FunctionCall(function=function, arguments={"text": "hi"}, call_id="call-1").aexecute()

    assert result.status == "success"
    assert result.result == "HI"
    assert seen == [
        ("hook", request_thread, id(request_loop)),
        ("sender", request_thread, id(request_loop)),
        ("event_id", "$ok"),
        ("tool", request_thread, id(request_loop)),
    ]


@pytest.mark.asyncio
async def test_tool_hook_bridge_prefers_bridge_agent_name_over_nested_runtime_context(tmp_path: Path) -> None:
    """Nested tool execution should stay attributed to the bridge agent, not the parent runtime context."""
    before_seen: list[str] = []
    after_seen: list[str] = []
    parent_seen: list[str] = []

    @hook(EVENT_TOOL_BEFORE_CALL, agents=["child-agent"])
    async def child_before(ctx: ToolBeforeCallContext) -> None:
        before_seen.append(ctx.agent_name)

    @hook(EVENT_TOOL_AFTER_CALL, agents=["child-agent"])
    async def child_after(ctx: ToolAfterCallContext) -> None:
        after_seen.append(ctx.agent_name)

    @hook(EVENT_TOOL_BEFORE_CALL, agents=["parent-agent"])
    async def parent_before(ctx: ToolBeforeCallContext) -> None:
        parent_seen.append(ctx.agent_name)

    registry = HookRegistry.from_plugins([_plugin("tool-policy", [child_before, child_after, parent_before])])
    bridge = build_tool_hook_bridge(
        registry,
        agent_name="child-agent",
        execution_identity=ToolExecutionIdentity(
            channel="matrix",
            agent_name="child-agent",
            requester_id="@user:localhost",
            room_id="!room:localhost",
            thread_id="$thread",
            resolved_thread_id="$resolved-thread",
            session_id="session-1",
        ),
    )
    assert bridge is not None

    async def next_func(**kwargs: object) -> str:
        return str(kwargs["path"])

    with (
        tool_runtime_context(_tool_runtime_context(tmp_path, agent_name="parent-agent")),
        tool_execution_identity(_execution_identity()),
    ):
        result = await bridge("read_file", next_func, {"path": "notes.txt"})

    assert result == "notes.txt"
    assert before_seen == ["child-agent"]
    assert after_seen == ["child-agent"]
    assert parent_seen == []


@pytest.mark.asyncio
async def test_tool_hook_bridge_declines_and_skips_real_tool(tmp_path: Path) -> None:
    """A declined before-call hook should return a synthetic result and not call the real tool."""
    after_seen: list[tuple[bool, object | None, BaseException | None]] = []

    @hook(EVENT_TOOL_BEFORE_CALL)
    async def before(ctx: ToolBeforeCallContext) -> None:
        if "secret" in str(ctx.arguments.get("path", "")):
            ctx.decline("secret paths are blocked")

    @hook(EVENT_TOOL_AFTER_CALL)
    async def after(ctx: ToolAfterCallContext) -> None:
        after_seen.append((ctx.blocked, ctx.result, ctx.error))

    registry = HookRegistry.from_plugins([_plugin("tool-policy", [before, after])])
    bridge = build_tool_hook_bridge(
        registry,
        agent_name="code",
        execution_identity=_execution_identity(),
    )
    assert bridge is not None

    next_func = AsyncMock(return_value="should not run")
    with tool_runtime_context(_tool_runtime_context(tmp_path)), tool_execution_identity(_execution_identity()):
        result = await bridge("read_file", next_func, {"path": "secret.txt"})

    assert next_func.await_count == 0
    assert result == (
        "[TOOL CALL DECLINED]\n"
        "Tool: read_file\n"
        "Reason: secret paths are blocked\n\n"
        "Adjust your approach — try a different tool or different arguments."
    )
    assert after_seen == [(True, result, None)]


@pytest.mark.asyncio
async def test_tool_hook_bridge_reraises_tool_errors_after_after_call(tmp_path: Path) -> None:
    """Tool exceptions should still propagate after the after-call hook observes them."""
    after_seen: list[tuple[object | None, BaseException | None, bool]] = []

    @hook(EVENT_TOOL_AFTER_CALL)
    async def after(ctx: ToolAfterCallContext) -> None:
        after_seen.append((ctx.result, ctx.error, ctx.blocked))

    registry = HookRegistry.from_plugins([_plugin("tool-audit", [after])])
    bridge = build_tool_hook_bridge(
        registry,
        agent_name="code",
        execution_identity=_execution_identity(),
    )
    assert bridge is not None

    async def explode(**kwargs: object) -> object:
        del kwargs
        msg = "boom"
        raise ValueError(msg)

    with (
        tool_runtime_context(_tool_runtime_context(tmp_path)),
        tool_execution_identity(_execution_identity()),
        pytest.raises(ValueError, match="boom"),
    ):
        await bridge("explode", explode, {})

    assert len(after_seen) == 1
    assert after_seen[0][0] is None
    assert isinstance(after_seen[0][1], ValueError)
    assert after_seen[0][2] is False


@pytest.mark.asyncio
async def test_tool_after_call_hooks_cannot_mutate_reraised_non_deepcopyable_error(tmp_path: Path) -> None:
    """After-call hooks should not be able to rewrite the original raised error."""
    seen_error_types: list[type[BaseException]] = []

    @hook(EVENT_TOOL_AFTER_CALL)
    async def rewrite(ctx: ToolAfterCallContext) -> None:
        assert ctx.error is not None
        seen_error_types.append(type(ctx.error))
        ctx.error.args = ("rewritten by hook",)

    registry = HookRegistry.from_plugins([_plugin("tool-audit", [rewrite])])
    bridge = build_tool_hook_bridge(
        registry,
        agent_name="code",
        execution_identity=_execution_identity(),
    )
    assert bridge is not None

    async def explode(**kwargs: object) -> object:
        del kwargs
        message = "original boom"
        raise NonDeepcopyableToolError(message, {"status": "original"})

    with (
        tool_runtime_context(_tool_runtime_context(tmp_path)),
        tool_execution_identity(_execution_identity()),
        pytest.raises(NonDeepcopyableToolError, match="original boom") as exc_info,
    ):
        await bridge("explode", explode, {})

    assert exc_info.value.args == ("original boom",)
    assert exc_info.value.details == {"status": "original"}
    assert seen_error_types == [Exception]


@pytest.mark.asyncio
async def test_agent_bot_tool_runtime_context_routes_custom_events_from_tool_hooks(tmp_path: Path) -> None:
    """AgentBot-built tool runtime context should deliver custom events and live request session IDs."""
    tool_name = "tool_hooks_runtime_event_tool"
    custom_event_name = "demo:tool_runtime_event"
    seen: list[tuple[str, str, dict[str, object], str | None, str | None, str | None, str]] = []
    before_seen: list[tuple[str | None, str | None, str | None, str | None]] = []

    class DemoToolkit(Toolkit):
        def __init__(self) -> None:
            super().__init__(name="tool-hooks-runtime-demo", tools=[self.echo])

        async def echo(self, text: str) -> str:
            return text.upper()

    original_registry = _TOOL_REGISTRY.copy()
    original_metadata = TOOL_METADATA.copy()

    @register_tool_with_metadata(
        name=tool_name,
        display_name="Tool Hooks Runtime Event Tool",
        description="Test tool for runtime hook event delivery.",
        category=ToolCategory.DEVELOPMENT,
    )
    def demo_tool_factory() -> type[DemoToolkit]:
        return DemoToolkit

    @hook(EVENT_TOOL_BEFORE_CALL)
    async def before(ctx: ToolBeforeCallContext) -> None:
        before_seen.append((ctx.room_id, ctx.thread_id, ctx.requester_id, ctx.session_id))
        await emit_custom_event(
            "tool-policy",
            custom_event_name,
            {
                "tool_name": ctx.tool_name,
                "arguments": dict(ctx.arguments),
            },
        )

    @hook(custom_event_name)
    async def on_custom_event(ctx: CustomEventContext) -> None:
        seen.append(
            (
                ctx.event_name,
                ctx.source_plugin,
                dict(ctx.payload),
                ctx.room_id,
                ctx.thread_id,
                ctx.sender_id,
                ctx.plugin_name,
            ),
        )

    plugins = [_plugin("tool-policy", [before, on_custom_event])]
    config = _config(tmp_path, tools=[tool_name], plugins=["./plugins/tool-policy"])
    bot = _agent_bot(tmp_path, config=config)
    bot.hook_registry = HookRegistry.from_plugins(plugins)

    try:
        with (
            patch("mindroom.ai.get_model_instance", return_value=Ollama(id="test-model")),
        ):
            tool_context = bot._build_tool_runtime_context(
                room_id="!room:localhost",
                thread_id="$thread",
                reply_to_event_id=None,
                user_id="@user:localhost",
            )
            assert tool_context is not None
            assert tool_context.hook_registry.has_hooks(custom_event_name)

            execution_identity = bot._build_tool_execution_identity(
                room_id="!room:localhost",
                thread_id="$thread",
                reply_to_event_id=None,
                user_id="@user:localhost",
                session_id="session-1",
            )
            toolkit = next(tool for tool in bot.agent.tools if tool.name == "tool-hooks-runtime-demo")
            function = _first_function(toolkit)

            with tool_runtime_context(tool_context), tool_execution_identity(execution_identity):
                result = await FunctionCall(function=function, arguments={"text": "hi"}, call_id="call-1").aexecute()

        assert result.status == "success"
        assert result.result == "HI"
        assert before_seen == [
            ("!room:localhost", "$thread", "@user:localhost", "session-1"),
        ]
        assert seen == [
            (
                custom_event_name,
                "tool-policy",
                {"tool_name": "echo", "arguments": {"text": "hi"}},
                "!room:localhost",
                "$thread",
                "@user:localhost",
                "tool-policy",
            ),
        ]
    finally:
        _TOOL_REGISTRY.clear()
        _TOOL_REGISTRY.update(original_registry)
        TOOL_METADATA.clear()
        TOOL_METADATA.update(original_metadata)


@pytest.mark.asyncio
async def test_tool_hook_bridge_fails_open_when_before_hook_raises(tmp_path: Path) -> None:
    """A broken before-call hook should not stop the actual tool execution."""

    @hook(EVENT_TOOL_BEFORE_CALL)
    async def broken(ctx: ToolBeforeCallContext) -> None:
        del ctx
        msg = "boom"
        raise RuntimeError(msg)

    registry = HookRegistry.from_plugins([_plugin("tool-policy", [broken])])
    bridge = build_tool_hook_bridge(
        registry,
        agent_name="code",
        execution_identity=_execution_identity(),
    )
    assert bridge is not None

    next_func = AsyncMock(return_value="ok")
    with tool_runtime_context(_tool_runtime_context(tmp_path)), tool_execution_identity(_execution_identity()):
        result = await bridge("read_file", next_func, {"path": "notes.txt"})

    assert result == "ok"
    assert next_func.await_count == 1


@pytest.mark.asyncio
async def test_prepend_tool_hook_bridge_preserves_existing_function_hooks() -> None:
    """The MindRoom bridge should prepend without overwriting existing Agno tool hooks."""
    seen: list[str] = []

    @hook(EVENT_TOOL_BEFORE_CALL)
    async def before(ctx: ToolBeforeCallContext) -> None:
        del ctx
        seen.append("bridge-before")

    registry = HookRegistry.from_plugins([_plugin("tool-policy", [before])])
    bridge = build_tool_hook_bridge(registry, agent_name="code")
    assert bridge is not None

    class DemoToolkit(Toolkit):
        def __init__(self) -> None:
            super().__init__(name="demo", tools=[self.echo])

        async def echo(self, text: str) -> str:
            seen.append("tool")
            return text

    async def existing_hook(name: str, func: object, args: dict[str, object]) -> object:
        seen.append(f"existing-before:{name}")
        result = await func(**args)
        seen.append("existing-after")
        return result

    toolkit = DemoToolkit()
    function = _first_function(toolkit)
    function.tool_hooks = [existing_hook]
    prepend_tool_hook_bridge(toolkit, bridge)

    assert function.tool_hooks is not None
    assert function.tool_hooks[0] is bridge
    assert function.tool_hooks[1] is existing_hook

    execution = FunctionCall(function=function, arguments={"text": "hi"}, call_id="call-1")
    result = await execution.aexecute()

    assert result.status == "success"
    assert result.result == "hi"
    assert seen == ["bridge-before", "existing-before:echo", "tool", "existing-after"]


@pytest.mark.asyncio
async def test_create_agent_prepends_bridge_to_real_tool_functions(tmp_path: Path) -> None:
    """create_agent() should attach the bridge and provide fallback hook context."""
    tool_name = "tool_hooks_test_tool"
    after_seen: list[tuple[Config | None, object | None, str, bool]] = []

    class DemoToolkit(Toolkit):
        calls: ClassVar[list[str]] = []

        def __init__(self) -> None:
            super().__init__(name="tool-hooks-demo", tools=[self.echo])

        async def echo(self, text: str) -> str:
            self.calls.append(text)
            return text

    original_registry = _TOOL_REGISTRY.copy()
    original_metadata = TOOL_METADATA.copy()

    @register_tool_with_metadata(
        name=tool_name,
        display_name="Tool Hooks Test Tool",
        description="Test tool for tool hook interception.",
        category=ToolCategory.DEVELOPMENT,
    )
    def demo_tool_factory() -> type[DemoToolkit]:
        return DemoToolkit

    @hook(EVENT_TOOL_BEFORE_CALL)
    async def block_all(ctx: ToolBeforeCallContext) -> None:
        ctx.decline("blocked in create_agent")

    @hook(EVENT_TOOL_AFTER_CALL)
    async def after(ctx: ToolAfterCallContext) -> None:
        after_seen.append((ctx.config, ctx.runtime_paths, str(ctx.state_root), ctx.blocked))

    registry = HookRegistry.from_plugins([_plugin("tool-policy", [block_all, after])])
    config = _config(tmp_path, tools=[tool_name])
    plugin_state_root = runtime_paths_for(config).storage_root / "plugins" / "tool-policy"

    try:
        with patch("mindroom.ai.get_model_instance", return_value=Ollama(id="test-model")):
            agent = create_agent(
                "code",
                config,
                runtime_paths_for(config),
                execution_identity=None,
                hook_registry=registry,
            )

        toolkit = next(tool for tool in agent.tools if tool.name == "tool-hooks-demo")
        function = _first_function(toolkit)
        assert function.tool_hooks is not None
        assert callable(function.tool_hooks[0])

        execution = FunctionCall(function=function, arguments={"text": "hello"}, call_id="call-1")
        result = await execution.aexecute()

        assert result.status == "success"
        assert result.result == (
            "[TOOL CALL DECLINED]\n"
            "Tool: echo\n"
            "Reason: blocked in create_agent\n\n"
            "Adjust your approach — try a different tool or different arguments."
        )
        assert DemoToolkit.calls == []
        assert after_seen == [(config, runtime_paths_for(config), str(plugin_state_root), True)]
    finally:
        _TOOL_REGISTRY.clear()
        _TOOL_REGISTRY.update(original_registry)
        TOOL_METADATA.clear()
        TOOL_METADATA.update(original_metadata)

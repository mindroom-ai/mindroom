"""Tests for tool call interception hooks."""

from __future__ import annotations

import asyncio
import json
import threading
import time
from dataclasses import replace
from types import SimpleNamespace
from typing import TYPE_CHECKING, ClassVar, Literal
from unittest.mock import AsyncMock, MagicMock, patch

import nio
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
from mindroom.constants import HOOK_MESSAGE_RECEIVED_DEPTH_KEY
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
from mindroom.message_target import MessageTarget
from mindroom.orchestrator import MultiAgentOrchestrator
from mindroom.tool_approval import get_approval_store, initialize_approval_store, shutdown_approval_store
from mindroom.tool_system.metadata import _TOOL_REGISTRY, TOOL_METADATA, ToolCategory, register_tool_with_metadata
from mindroom.tool_system.runtime_context import (
    ToolDispatchContext,
    ToolRuntimeContext,
    emit_custom_event,
    get_plugin_state_root,
    tool_runtime_context,
)
from mindroom.tool_system.tool_hooks import build_tool_hook_bridge, prepend_tool_hook_bridge
from mindroom.tool_system.worker_routing import ToolExecutionIdentity, tool_execution_identity
from tests.conftest import (
    bind_runtime_paths,
    make_conversation_cache_mock,
    make_event_cache_mock,
    runtime_paths_for,
    test_runtime_paths,
)

if TYPE_CHECKING:
    from collections.abc import Generator
    from pathlib import Path

    from mindroom.constants import RuntimePaths

type SyncBridgeEvent = (
    tuple[Literal["before"], str, dict[str, str], str]
    | tuple[Literal["tool"], str]
    | tuple[Literal["after"], str, bool, str]
)

_SESSION_ID = "!room:localhost:$resolved-thread"


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
        session_id=_SESSION_ID,
        config=config,
        runtime_paths=runtime_paths_for(config),
        correlation_id="corr-tool",
    )


def _tool_runtime_context(
    tmp_path: Path,
    *,
    agent_name: str = "code",
    hook_message_sender: object | None = None,
    room_state_querier: object | None = None,
    room_state_putter: object | None = None,
    message_received_depth: int = 0,
    hook_registry: HookRegistry | None = None,
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
        event_cache=make_event_cache_mock(),
        conversation_cache=make_conversation_cache_mock(),
        session_id=_SESSION_ID,
        correlation_id="corr-runtime",
        hook_registry=hook_registry or HookRegistry.empty(),
        hook_message_sender=hook_message_sender,
        room_state_querier=room_state_querier,
        room_state_putter=room_state_putter,
        message_received_depth=message_received_depth,
    )


def _execution_identity() -> ToolExecutionIdentity:
    return ToolExecutionIdentity(
        channel="matrix",
        agent_name="code",
        requester_id="@user:localhost",
        room_id="!room:localhost",
        thread_id="$thread",
        resolved_thread_id="$resolved-thread",
        session_id=_SESSION_ID,
    )


def _dispatch_context(
    execution_identity: ToolExecutionIdentity | None = None,
) -> ToolDispatchContext | None:
    if execution_identity is None:
        return None
    return ToolDispatchContext(execution_identity=execution_identity)


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


def _initialize_test_approval_store(runtime_paths: RuntimePaths) -> None:
    initialize_approval_store(
        runtime_paths,
        sender=AsyncMock(return_value="$approval"),
        editor=AsyncMock(),
    )


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


@pytest.fixture(autouse=True)
def reset_approval_store() -> Generator[None, None, None]:
    """Keep the module-level approval store isolated per test."""
    asyncio.run(shutdown_approval_store())
    yield
    asyncio.run(shutdown_approval_store())
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
        session_id=_SESSION_ID,
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


def test_build_tool_hook_bridge_returns_bridge_without_tool_hooks() -> None:
    """Failure logging should keep the bridge installed even without plugin hooks."""
    assert build_tool_hook_bridge(HookRegistry.empty(), agent_name="code") is not None


@pytest.mark.asyncio
async def test_tool_hook_bridge_records_failures_without_registered_hooks(tmp_path: Path) -> None:
    """The bridge should durably record failures and re-raise the original exception."""
    runtime_context = _tool_runtime_context(tmp_path)
    bridge = build_tool_hook_bridge(
        HookRegistry.empty(),
        agent_name="code",
        dispatch_context=_dispatch_context(_execution_identity()),
        runtime_paths=runtime_context.runtime_paths,
    )
    error = ValueError("boom {'api_key': 'secret'} https://alice:secret@example.com/private")

    async def explode(**kwargs: object) -> object:
        del kwargs
        raise error

    assert bridge is not None
    with (
        tool_runtime_context(runtime_context),
        tool_execution_identity(_execution_identity()),
        pytest.raises(ValueError, match="boom") as exc_info,
    ):
        await bridge(
            "explode",
            explode,
            {
                "api_key": "secret",
                "nested": [{"refresh_token": "refresh-secret"}],
                "url": "https://alice:secret@example.com/private",
            },
        )

    assert exc_info.value is error

    log_path = runtime_context.runtime_paths.storage_root / "tracking" / "tool_failures.jsonl"
    records = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines() if line]

    assert len(records) == 1
    assert set(records[0]) == {
        "timestamp",
        "tool_name",
        "agent_name",
        "channel",
        "room_id",
        "thread_id",
        "requester_id",
        "session_id",
        "correlation_id",
        "duration_ms",
        "arguments",
        "error_type",
        "error_message",
        "traceback",
    }
    assert records[0]["tool_name"] == "explode"
    assert records[0]["agent_name"] == "code"
    assert records[0]["channel"] == "matrix"
    assert records[0]["room_id"] == "!room:localhost"
    assert records[0]["thread_id"] == "$resolved-thread"
    assert records[0]["requester_id"] == "@user:localhost"
    assert records[0]["session_id"] == _SESSION_ID
    assert records[0]["correlation_id"] == "corr-runtime"
    assert records[0]["error_type"] == "ValueError"
    assert records[0]["arguments"] == {
        "api_key": "***redacted***",
        "nested": [{"refresh_token": "***redacted***"}],
        "url": "https://alice:***@example.com/private",
    }
    assert "secret" not in records[0]["error_message"]
    assert "secret" not in records[0]["traceback"]


@pytest.mark.asyncio
async def test_tool_hook_bridge_preserves_original_error_when_failure_recording_breaks(tmp_path: Path) -> None:
    """Secondary logging failures should not mask the original tool exception."""
    seen_errors: list[BaseException | None] = []

    @hook(EVENT_TOOL_AFTER_CALL)
    async def after(ctx: ToolAfterCallContext) -> None:
        seen_errors.append(ctx.error)

    runtime_context = _tool_runtime_context(tmp_path)
    bridge = build_tool_hook_bridge(
        HookRegistry.from_plugins([_plugin("tool-policy", [after])]),
        agent_name="code",
        dispatch_context=_dispatch_context(_execution_identity()),
        runtime_paths=runtime_context.runtime_paths,
    )
    error = ValueError("boom")

    async def explode(**kwargs: object) -> object:
        del kwargs
        raise error

    assert bridge is not None
    with (
        patch("mindroom.tool_system.tool_hooks.record_tool_failure", side_effect=RuntimeError("disk full")),
        patch("mindroom.tool_system.tool_hooks.logger.exception") as mock_logger_exception,
        tool_runtime_context(runtime_context),
        tool_execution_identity(_execution_identity()),
        pytest.raises(ValueError, match="boom") as exc_info,
    ):
        await bridge("explode", explode, {"api_key": "secret"})

    assert exc_info.value is error
    assert len(seen_errors) == 1
    assert isinstance(seen_errors[0], ValueError)
    assert str(seen_errors[0]) == "boom"
    mock_logger_exception.assert_called_once_with(
        "Failed to record tool failure",
        tool_name="explode",
        correlation_id="corr-runtime",
    )


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
        dispatch_context=_dispatch_context(_execution_identity()),
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
        dispatch_context=_dispatch_context(_execution_identity()),
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
async def test_tool_hook_bridge_runs_sync_tools_off_event_loop() -> None:
    """Blocking sync tools should not stall the async tool-dispatch loop."""
    bridge = build_tool_hook_bridge(HookRegistry.empty(), agent_name="code")
    assert bridge is not None

    loop_thread_id = threading.get_ident()
    stop_ticking = False
    ticks = 0

    async def ticker() -> None:
        nonlocal ticks
        while not stop_ticking:
            await asyncio.sleep(0.01)
            ticks += 1

    def slow_sync_tool() -> int:
        time.sleep(0.1)
        return threading.get_ident()

    ticker_task = asyncio.create_task(ticker())
    tool_thread_id = await bridge("slow_sync_tool", slow_sync_tool, {})
    stop_ticking = True
    await ticker_task

    assert tool_thread_id != loop_thread_id
    assert ticks > 0


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
        dispatch_context=_dispatch_context(_execution_identity()),
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
            _SESSION_ID,
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
            _SESSION_ID,
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
        dispatch_context=_dispatch_context(_execution_identity()),
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
        dispatch_context=_dispatch_context(_execution_identity()),
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
        dispatch_context=_dispatch_context(_execution_identity()),
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
                HOOK_MESSAGE_RECEIVED_DEPTH_KEY: 1,
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
                HOOK_MESSAGE_RECEIVED_DEPTH_KEY: 1,
            },
            False,
        ),
    ]


@pytest.mark.asyncio
async def test_tool_hook_context_send_message_advances_existing_message_received_depth(tmp_path: Path) -> None:
    """Tool hook sends should keep advancing an existing synthetic message:received chain."""
    sent: list[dict[str, object] | None] = []

    async def hook_message_sender(
        room_id: str,
        body: str,
        thread_id: str | None,
        source_hook: str,
        extra_content: dict[str, object] | None,
        *,
        trigger_dispatch: bool = False,
    ) -> str | None:
        del room_id, body, thread_id, source_hook, trigger_dispatch
        sent.append(extra_content)
        return "$dispatch-event"

    @hook(EVENT_TOOL_BEFORE_CALL)
    async def before(ctx: ToolBeforeCallContext) -> None:
        event_id = await ctx.send_message("!room:localhost", "dispatch", trigger_dispatch=True)
        assert event_id == "$dispatch-event"

    registry = HookRegistry.from_plugins([_plugin("tool-policy", [before])])
    bridge = build_tool_hook_bridge(
        registry,
        agent_name="code",
        dispatch_context=_dispatch_context(_execution_identity()),
    )
    assert bridge is not None

    async def next_func(**kwargs: object) -> dict[str, object]:
        return {"echo": kwargs["path"]}

    with (
        tool_runtime_context(
            _tool_runtime_context(
                tmp_path,
                hook_message_sender=hook_message_sender,
                message_received_depth=1,
            ),
        ),
        tool_execution_identity(_execution_identity()),
    ):
        result = await bridge("read_file", next_func, {"path": "notes.txt"})

    assert result == {"echo": "notes.txt"}
    assert sent == [
        {
            "com.mindroom.original_sender": "@user:localhost",
            HOOK_MESSAGE_RECEIVED_DEPTH_KEY: 2,
        },
    ]


@pytest.mark.asyncio
async def test_tool_hook_contexts_expose_router_backed_matrix_admin(tmp_path: Path) -> None:
    """tool:* hook contexts should expose the router-backed matrix admin helper."""
    resolved_aliases: list[str | None] = []

    @hook(EVENT_TOOL_BEFORE_CALL)
    async def before(ctx: ToolBeforeCallContext) -> None:
        assert ctx.matrix_admin is not None
        resolved_aliases.append(await ctx.matrix_admin.resolve_alias("#personal-user:localhost"))

    @hook(EVENT_TOOL_AFTER_CALL)
    async def after(ctx: ToolAfterCallContext) -> None:
        assert ctx.matrix_admin is not None
        resolved_aliases.append(await ctx.matrix_admin.resolve_alias("#personal-user:localhost"))

    config = _config(tmp_path)
    bot = _agent_bot(tmp_path, config=config)
    bot.client = AsyncMock(spec=nio.AsyncClient)
    bot.client.rooms = {}
    bot.event_cache = MagicMock()
    bot.client.homeserver = "http://agent.local:8008"
    bot.client.room_resolve_alias.return_value = nio.RoomResolveAliasError(
        "not found",
        status_code="M_NOT_FOUND",
    )

    router_bot = _agent_bot(tmp_path, config=config, agent_name="router")
    router_bot.client = AsyncMock(spec=nio.AsyncClient)
    router_bot.client.rooms = {}
    router_bot.client.homeserver = "http://localhost:8008"
    router_bot.client.room_resolve_alias.return_value = nio.RoomResolveAliasResponse(
        room_alias="#personal-user:localhost",
        room_id="!personal:localhost",
        servers=["localhost"],
    )

    orchestrator = MultiAgentOrchestrator(runtime_paths=runtime_paths_for(config))
    orchestrator.agent_bots = {"router": router_bot, "code": bot}
    bot.orchestrator = orchestrator

    registry = HookRegistry.from_plugins([_plugin("tool-policy", [before, after])])
    target = MessageTarget.resolve("!room:localhost", "$thread", None)
    execution_identity = bot._tool_runtime_support.build_execution_identity(
        target=target,
        user_id="@user:localhost",
        session_id=target.session_id,
    )
    bridge = build_tool_hook_bridge(
        registry,
        agent_name="code",
        dispatch_context=_dispatch_context(execution_identity),
    )
    assert bridge is not None

    async def next_func(**kwargs: object) -> dict[str, object]:
        return {"echo": kwargs["path"]}

    runtime_context = bot._tool_runtime_support.build_context(target, user_id="@user:localhost")
    assert runtime_context is not None

    with tool_runtime_context(runtime_context), tool_execution_identity(execution_identity):
        result = await bridge("read_file", next_func, {"path": "notes.txt"})

    assert result == {"echo": "notes.txt"}
    assert resolved_aliases == ["!personal:localhost", "!personal:localhost"]
    bot.client.room_resolve_alias.assert_not_awaited()
    assert router_bot.client.room_resolve_alias.await_count == 2


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
        dispatch_context=_dispatch_context(_execution_identity()),
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
                HOOK_MESSAGE_RECEIVED_DEPTH_KEY: 1,
            },
            True,
        ),
    ]


@pytest.mark.asyncio
async def test_agent_bot_tool_runtime_context_room_state_helpers_fallback_to_router(tmp_path: Path) -> None:
    """Bot-built tool runtime contexts should use current-bot-first room-state helpers with router fallback."""
    seen: list[tuple[dict[str, object] | None, bool]] = []
    config = _config(tmp_path)
    bot = _agent_bot(tmp_path, config=config)
    bot.client = AsyncMock(spec=nio.AsyncClient)
    bot.client.rooms = {}
    bot.event_cache = MagicMock()
    bot.client.room_get_state_event.return_value = nio.RoomGetStateEventError(message="forbidden")
    bot.client.room_put_state.return_value = nio.RoomPutStateError(message="forbidden")
    router_bot = _agent_bot(tmp_path, config=config, agent_name="router")
    router_bot.client = AsyncMock(spec=nio.AsyncClient)
    router_bot.client.rooms = {}
    router_bot.client.room_get_state_event.return_value = SimpleNamespace(content={"name": "Router Lobby"})
    router_bot.client.room_put_state.return_value = object()
    orchestrator = MultiAgentOrchestrator(runtime_paths=runtime_paths_for(config))
    orchestrator.agent_bots = {"router": router_bot, "code": bot}
    bot.orchestrator = orchestrator

    @hook(EVENT_TOOL_BEFORE_CALL)
    async def before(ctx: ToolBeforeCallContext) -> None:
        query_result = await ctx.query_room_state("!room:localhost", "m.room.name", "")
        put_result = await ctx.put_room_state(
            "!room:localhost",
            "com.mindroom.thread.tags",
            "$resolved-thread",
            {"tags": {"queued": True}},
        )
        seen.append((query_result, put_result))

    registry = HookRegistry.from_plugins([_plugin("tool-policy", [before])])
    target = MessageTarget.resolve("!room:localhost", "$thread", None)
    execution_identity = bot._tool_runtime_support.build_execution_identity(
        target=target,
        user_id="@user:localhost",
        session_id=target.session_id,
    )
    bridge = build_tool_hook_bridge(
        registry,
        agent_name="code",
        dispatch_context=_dispatch_context(execution_identity),
    )
    assert bridge is not None

    async def next_func(**kwargs: object) -> dict[str, object]:
        return {"echo": kwargs["path"]}

    runtime_context = bot._tool_runtime_support.build_context(target, user_id="@user:localhost")
    assert runtime_context is not None

    with tool_runtime_context(runtime_context), tool_execution_identity(execution_identity):
        result = await bridge("read_file", next_func, {"path": "notes.txt"})

    assert result == {"echo": "notes.txt"}
    assert seen == [({"name": "Router Lobby"}, True)]
    bot.client.room_get_state_event.assert_awaited_once_with("!room:localhost", "m.room.name", "")
    bot.client.room_put_state.assert_awaited_once_with(
        "!room:localhost",
        "com.mindroom.thread.tags",
        {"tags": {"queued": True}},
        state_key="$resolved-thread",
    )
    router_bot.client.room_get_state_event.assert_awaited_once_with("!room:localhost", "m.room.name", "")
    router_bot.client.room_put_state.assert_awaited_once_with(
        "!room:localhost",
        "com.mindroom.thread.tags",
        {"tags": {"queued": True}},
        state_key="$resolved-thread",
    )


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
        dispatch_context=_dispatch_context(_execution_identity()),
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
async def test_sync_tool_approval_send_uses_runtime_loop(tmp_path: Path) -> None:
    """Sync-tool approval sends should hop back to the runtime loop."""
    request_thread = threading.get_ident()
    request_loop = asyncio.get_running_loop()
    runtime_paths = test_runtime_paths(tmp_path)
    config = bind_runtime_paths(
        Config(
            agents={
                "code": AgentConfig(
                    display_name="Code",
                    role="Help with coding.",
                    rooms=["!room:localhost"],
                ),
            },
            models={"default": ModelConfig(provider="openai", id="test-model")},
            tool_approval={
                "timeout_days": 0.000001,
                "rules": [{"match": "echo", "action": "require_approval"}],
            },
        ),
        runtime_paths,
    )
    orchestrator = MultiAgentOrchestrator(runtime_paths=runtime_paths)
    orchestrator._capture_runtime_loop()

    async def mock_room_send(room_id: str, message_type: str, content: dict[str, object]) -> nio.RoomSendResponse:
        current_loop = asyncio.get_running_loop()
        current_thread = threading.get_ident()
        assert current_thread == request_thread
        assert current_loop is request_loop
        assert room_id == "!room:localhost"
        assert message_type == "io.mindroom.tool_approval"
        assert content["status"] == "pending"
        return nio.RoomSendResponse(event_id="$approval", room_id=room_id)

    client = MagicMock()
    client.room_send = AsyncMock(side_effect=mock_room_send)
    bot = MagicMock()
    bot.client = client
    orchestrator.agent_bots = {"code": bot}
    initialize_approval_store(runtime_paths, sender=orchestrator._send_approval_event, editor=AsyncMock())

    bridge = build_tool_hook_bridge(
        HookRegistry.empty(),
        agent_name="code",
        dispatch_context=_dispatch_context(_execution_identity()),
        config=config,
        runtime_paths=runtime_paths,
    )
    assert bridge is not None

    class DemoToolkit(Toolkit):
        def __init__(self) -> None:
            super().__init__(name="demo", tools=[self.echo])

        def echo(self, text: str) -> str:
            return text.upper()

    toolkit = DemoToolkit()
    function = _first_function(toolkit)
    prepend_tool_hook_bridge(toolkit, bridge)

    result = await asyncio.to_thread(
        lambda: FunctionCall(function=function, arguments={"text": "hi"}, call_id="call-1").execute(),
    )

    assert result.status == "success"
    assert result.result == (
        "[TOOL CALL DECLINED]\n"
        "Tool: echo\n"
        "Reason: Tool approval request timed out.\n\n"
        "Adjust your approach — try a different tool or different arguments."
    )
    client.room_send.assert_awaited_once()


@pytest.mark.asyncio
async def test_sync_tool_approval_resumes_after_cross_loop_resolution(tmp_path: Path) -> None:
    """Approval-gated sync tools should resume after approval resolves on another loop."""
    runtime_paths = test_runtime_paths(tmp_path)
    config = bind_runtime_paths(
        Config(
            agents={
                "code": AgentConfig(
                    display_name="Code",
                    role="Help with coding.",
                    rooms=["!room:localhost"],
                ),
            },
            models={"default": ModelConfig(provider="openai", id="test-model")},
            tool_approval={
                "rules": [{"match": "echo", "action": "require_approval"}],
            },
        ),
        runtime_paths,
    )
    orchestrator = MultiAgentOrchestrator(runtime_paths=runtime_paths)
    orchestrator._capture_runtime_loop()

    client = MagicMock()
    client.room_send = AsyncMock(
        return_value=nio.RoomSendResponse(event_id="$approval", room_id="!room:localhost"),
    )
    bot = MagicMock()
    bot.client = client
    orchestrator.agent_bots = {"code": bot}
    editor = AsyncMock()
    initialize_approval_store(runtime_paths, sender=orchestrator._send_approval_event, editor=editor)

    bridge = build_tool_hook_bridge(
        HookRegistry.empty(),
        agent_name="code",
        dispatch_context=_dispatch_context(_execution_identity()),
        config=config,
        runtime_paths=runtime_paths,
    )
    assert bridge is not None

    class DemoToolkit(Toolkit):
        def __init__(self) -> None:
            super().__init__(name="demo", tools=[self.echo])

        def echo(self, text: str) -> str:
            return text.upper()

    toolkit = DemoToolkit()
    function = _first_function(toolkit)
    prepend_tool_hook_bridge(toolkit, bridge)
    result: object | None = None
    error: BaseException | None = None

    def worker() -> None:
        nonlocal result, error

        async def run_execute() -> object:
            return FunctionCall(function=function, arguments={"text": "hi"}, call_id="call-1").execute()

        try:
            result = asyncio.run(run_execute())
        except BaseException as exc:  # pragma: no cover - asserted below
            error = exc

    thread = threading.Thread(target=worker)
    thread.start()

    store = get_approval_store()
    assert store is not None
    async with asyncio.timeout(1):
        while True:
            pending = store.list_pending()
            if pending:
                break
            await asyncio.sleep(0)

    await store.handle_approval_resolution(
        approval_id=pending[0].id,
        status="approved",
        reason=None,
        resolved_by="@user:localhost",
    )
    thread.join(timeout=1)

    assert error is None
    assert not thread.is_alive()
    assert result is not None
    assert result.status == "success"
    assert result.result == "HI"
    client.room_send.assert_awaited_once()
    assert editor.await_args.args[3]["status"] == "approved"


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
        dispatch_context=_dispatch_context(
            ToolExecutionIdentity(
                channel="matrix",
                agent_name="child-agent",
                requester_id="@user:localhost",
                room_id="!room:localhost",
                thread_id="$thread",
                resolved_thread_id="$resolved-thread",
                session_id=_SESSION_ID,
            ),
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
async def test_tool_hook_bridge_does_not_merge_explicit_identity_with_ambient_identity() -> None:
    """An explicit bridge identity should not backfill missing fields from ambient execution state."""
    seen: list[tuple[str | None, str | None, str | None, str | None]] = []

    @hook(EVENT_TOOL_BEFORE_CALL)
    async def before(ctx: ToolBeforeCallContext) -> None:
        seen.append((ctx.room_id, ctx.thread_id, ctx.requester_id, ctx.session_id))

    registry = HookRegistry.from_plugins([_plugin("tool-policy", [before])])
    bridge = build_tool_hook_bridge(
        registry,
        agent_name="child-agent",
        dispatch_context=_dispatch_context(
            ToolExecutionIdentity(
                channel="matrix",
                agent_name="child-agent",
                requester_id=None,
                room_id=None,
                thread_id=None,
                resolved_thread_id=None,
                session_id=None,
            ),
        ),
    )
    assert bridge is not None

    async def next_func(**kwargs: object) -> str:
        return str(kwargs["path"])

    with tool_execution_identity(_execution_identity()):
        result = await bridge("read_file", next_func, {"path": "notes.txt"})

    assert result == "notes.txt"
    assert seen == [(None, None, None, None)]


@pytest.mark.asyncio
async def test_tool_hook_bridge_does_not_merge_explicit_identity_with_ambient_runtime_context(
    tmp_path: Path,
) -> None:
    """An explicit bridge identity should not backfill missing fields from ambient runtime context."""
    seen: list[tuple[str | None, str | None, str | None, str | None]] = []

    @hook(EVENT_TOOL_BEFORE_CALL)
    async def before(ctx: ToolBeforeCallContext) -> None:
        seen.append((ctx.room_id, ctx.thread_id, ctx.requester_id, ctx.session_id))

    registry = HookRegistry.from_plugins([_plugin("tool-policy", [before])])
    bridge = build_tool_hook_bridge(
        registry,
        agent_name="child-agent",
        dispatch_context=_dispatch_context(
            ToolExecutionIdentity(
                channel="matrix",
                agent_name="child-agent",
                requester_id=None,
                room_id=None,
                thread_id=None,
                resolved_thread_id=None,
                session_id=None,
            ),
        ),
    )
    assert bridge is not None

    async def next_func(**kwargs: object) -> str:
        return str(kwargs["path"])

    with tool_runtime_context(_tool_runtime_context(tmp_path, agent_name="parent-agent")):
        result = await bridge("read_file", next_func, {"path": "notes.txt"})

    assert result == "notes.txt"
    assert seen == [(None, None, None, None)]


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
        dispatch_context=_dispatch_context(_execution_identity()),
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
async def test_tool_approval_gate_runs_before_before_call_hooks(tmp_path: Path) -> None:
    """Approval should block tool:before_call hooks until approval is granted."""
    seen: list[str] = []
    runtime_paths = test_runtime_paths(tmp_path)
    config = bind_runtime_paths(
        Config(
            agents={
                "code": AgentConfig(
                    display_name="Code",
                    role="Help with coding.",
                    rooms=[],
                ),
            },
            models={"default": ModelConfig(provider="openai", id="test-model")},
            tool_approval={
                "rules": [
                    {"match": "read_file", "action": "require_approval"},
                ],
            },
        ),
        runtime_paths,
    )
    _initialize_test_approval_store(runtime_paths)

    @hook(EVENT_TOOL_BEFORE_CALL)
    async def before(ctx: ToolBeforeCallContext) -> None:
        del ctx
        seen.append("before")

    registry = HookRegistry.from_plugins([_plugin("tool-policy", [before])])
    bridge = build_tool_hook_bridge(
        registry,
        agent_name="code",
        dispatch_context=_dispatch_context(_execution_identity()),
        config=config,
        runtime_paths=runtime_paths,
    )
    assert bridge is not None

    async def next_func(**kwargs: object) -> str:
        del kwargs
        seen.append("tool")
        return "ok"

    with tool_execution_identity(_execution_identity()):
        task = asyncio.create_task(bridge("read_file", next_func, {"path": "notes.txt"}))
        await asyncio.sleep(0)
        store = get_approval_store()
        assert store is not None
        pending = store.list_pending()
        assert len(pending) == 1
        assert seen == []

        await store.approve(pending[0].id, resolved_by="dashboard-user")
        result = await task

    assert result == "ok"
    assert seen == ["before", "tool"]


@pytest.mark.asyncio
async def test_tool_approval_deny_emits_after_call_as_blocked(tmp_path: Path) -> None:
    """Denied approvals should return the declined result and still emit blocked after-call hooks."""
    after_seen: list[tuple[bool, object | None, BaseException | None]] = []
    runtime_paths = test_runtime_paths(tmp_path)
    config = bind_runtime_paths(
        Config(
            agents={
                "code": AgentConfig(
                    display_name="Code",
                    role="Help with coding.",
                    rooms=[],
                ),
            },
            models={"default": ModelConfig(provider="openai", id="test-model")},
            tool_approval={"rules": [{"match": "read_file", "action": "require_approval"}]},
        ),
        runtime_paths,
    )
    _initialize_test_approval_store(runtime_paths)

    @hook(EVENT_TOOL_AFTER_CALL)
    async def after(ctx: ToolAfterCallContext) -> None:
        after_seen.append((ctx.blocked, ctx.result, ctx.error))

    registry = HookRegistry.from_plugins([_plugin("tool-policy", [after])])
    bridge = build_tool_hook_bridge(
        registry,
        agent_name="code",
        dispatch_context=_dispatch_context(_execution_identity()),
        config=config,
        runtime_paths=runtime_paths,
    )
    assert bridge is not None

    next_func = AsyncMock(return_value="should not run")
    with tool_execution_identity(_execution_identity()):
        task = asyncio.create_task(bridge("read_file", next_func, {"path": "notes.txt"}))
        await asyncio.sleep(0)
        store = get_approval_store()
        assert store is not None
        pending = store.list_pending()
        assert len(pending) == 1
        await store.deny(pending[0].id, reason="Denied by dashboard user.", resolved_by="dashboard-user")
        result = await task

    assert next_func.await_count == 0
    assert result == (
        "[TOOL CALL DECLINED]\n"
        "Tool: read_file\n"
        "Reason: Denied by dashboard user.\n\n"
        "Adjust your approach — try a different tool or different arguments."
    )
    assert after_seen == [(True, result, None)]


@pytest.mark.asyncio
async def test_tool_approval_expiry_emits_after_call_as_blocked(tmp_path: Path) -> None:
    """Expired approvals should return the declined result and emit blocked after-call hooks."""
    after_seen: list[tuple[bool, object | None, BaseException | None]] = []
    runtime_paths = test_runtime_paths(tmp_path)
    config = bind_runtime_paths(
        Config(
            agents={
                "code": AgentConfig(
                    display_name="Code",
                    role="Help with coding.",
                    rooms=[],
                ),
            },
            models={"default": ModelConfig(provider="openai", id="test-model")},
            tool_approval={
                "timeout_days": 0.000001,
                "rules": [{"match": "read_file", "action": "require_approval"}],
            },
        ),
        runtime_paths,
    )
    _initialize_test_approval_store(runtime_paths)

    @hook(EVENT_TOOL_AFTER_CALL)
    async def after(ctx: ToolAfterCallContext) -> None:
        after_seen.append((ctx.blocked, ctx.result, ctx.error))

    registry = HookRegistry.from_plugins([_plugin("tool-policy", [after])])
    bridge = build_tool_hook_bridge(
        registry,
        agent_name="code",
        dispatch_context=_dispatch_context(_execution_identity()),
        config=config,
        runtime_paths=runtime_paths,
    )
    assert bridge is not None

    next_func = AsyncMock(return_value="should not run")
    with tool_execution_identity(_execution_identity()):
        result = await bridge("read_file", next_func, {"path": "notes.txt"})

    assert next_func.await_count == 0
    assert result == (
        "[TOOL CALL DECLINED]\n"
        "Tool: read_file\n"
        "Reason: Tool approval request timed out.\n\n"
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
        dispatch_context=_dispatch_context(_execution_identity()),
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
        dispatch_context=_dispatch_context(_execution_identity()),
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

    plugin_root = tmp_path / "plugins" / "tool-policy"
    plugin_root.mkdir(parents=True)
    (plugin_root / "mindroom.plugin.json").write_text(
        '{"name": "tool-policy", "tools_module": null, "skills": []}',
        encoding="utf-8",
    )
    plugins = [_plugin("tool-policy", [before, on_custom_event])]
    config = _config(tmp_path, tools=[tool_name], plugins=["./plugins/tool-policy"])
    bot = _agent_bot(tmp_path, config=config)
    bot.event_cache = MagicMock()
    bot.hook_registry = HookRegistry.from_plugins(plugins)
    bot.orchestrator = MagicMock(knowledge_managers={}, knowledge_refresh_scheduler=None)

    try:
        with patch("mindroom.model_loading.get_model_instance", return_value=Ollama(id="test-model")):
            target = MessageTarget.resolve(
                room_id="!room:localhost",
                thread_id="$thread",
                reply_to_event_id=None,
            )
            tool_context = bot._tool_runtime_support.build_context(target, user_id="@user:localhost")
            assert tool_context is not None
            assert tool_context.hook_registry.has_hooks(custom_event_name)

            execution_identity = bot._tool_runtime_support.build_execution_identity(
                target=target,
                user_id="@user:localhost",
                session_id=target.session_id,
            )
            toolkit = next(tool for tool in bot.agent.tools if tool.name == "tool-hooks-runtime-demo")
            function = _first_function(toolkit)

            with tool_runtime_context(tool_context), tool_execution_identity(execution_identity):
                result = await FunctionCall(function=function, arguments={"text": "hi"}, call_id="call-1").aexecute()

        assert result.status == "success"
        assert result.result == "HI"
        assert before_seen == [
            ("!room:localhost", "$thread", "@user:localhost", target.session_id),
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
async def test_emit_custom_event_preserves_message_received_depth_and_bound_room_state_accessors(
    tmp_path: Path,
) -> None:
    """Tool-emitted custom events should preserve chain depth and use bound room-state helpers."""
    custom_event_name = "demo:tool_custom_event"
    sent: list[dict[str, object] | None] = []
    seen: list[tuple[dict[str, object] | None, bool]] = []
    room_state_querier = AsyncMock(return_value={"name": "Lobby"})
    room_state_putter = AsyncMock(return_value=True)

    async def hook_message_sender(
        room_id: str,
        body: str,
        thread_id: str | None,
        source_hook: str,
        extra_content: dict[str, object] | None,
        *,
        trigger_dispatch: bool = False,
    ) -> str | None:
        del room_id, body, thread_id, source_hook, trigger_dispatch
        sent.append(extra_content)
        return "$dispatch-event"

    @hook(custom_event_name)
    async def on_custom_event(ctx: CustomEventContext) -> None:
        query_result = await ctx.query_room_state("!room:localhost", "m.room.name", "")
        put_result = await ctx.put_room_state(
            "!room:localhost",
            "com.mindroom.thread.tags",
            "$resolved-thread",
            {"tags": {"queued": True}},
        )
        seen.append((query_result, put_result))
        event_id = await ctx.send_message("!room:localhost", "dispatch", trigger_dispatch=True)
        assert event_id == "$dispatch-event"

    registry = HookRegistry.from_plugins([_plugin("tool-policy", [on_custom_event])])
    runtime_context = _tool_runtime_context(
        tmp_path,
        hook_message_sender=hook_message_sender,
        room_state_querier=room_state_querier,
        room_state_putter=room_state_putter,
        message_received_depth=1,
        hook_registry=registry,
    )

    with tool_runtime_context(runtime_context):
        await emit_custom_event("tool-policy", custom_event_name, {"item_id": "123"})

    assert seen == [({"name": "Lobby"}, True)]
    room_state_querier.assert_awaited_once_with("!room:localhost", "m.room.name", "")
    room_state_putter.assert_awaited_once_with(
        "!room:localhost",
        "com.mindroom.thread.tags",
        "$resolved-thread",
        {"tags": {"queued": True}},
    )
    assert sent == [
        {
            "com.mindroom.original_sender": "@user:localhost",
            HOOK_MESSAGE_RECEIVED_DEPTH_KEY: 2,
        },
    ]


@pytest.mark.asyncio
async def test_emit_custom_event_ignores_raw_room_mode_thread_id(tmp_path: Path) -> None:
    """Custom tool events should not re-scope room-mode contexts from raw thread provenance."""
    custom_event_name = "demo:room_mode_thread_guard"
    seen_thread_ids: list[str | None] = []

    @hook(custom_event_name)
    async def on_custom_event(ctx: CustomEventContext) -> None:
        seen_thread_ids.append(ctx.thread_id)

    registry = HookRegistry.from_plugins([_plugin("tool-policy", [on_custom_event])])
    runtime_context = replace(
        _tool_runtime_context(tmp_path, hook_registry=registry),
        thread_id="$raw-thread",
        resolved_thread_id=None,
    )

    with tool_runtime_context(runtime_context):
        await emit_custom_event("tool-policy", custom_event_name, {"item_id": "123"})

    assert seen_thread_ids == [None]


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
        dispatch_context=_dispatch_context(_execution_identity()),
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
        with patch("mindroom.model_loading.get_model_instance", return_value=Ollama(id="test-model")):
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


def test_get_plugin_state_root_rejects_invalid_plugin_name(tmp_path: Path) -> None:
    """Tool runtime state helpers should reject path-like plugin names."""
    runtime_paths = test_runtime_paths(tmp_path)

    with pytest.raises(ValueError, match="Invalid plugin name"):
        get_plugin_state_root("../../escaped", runtime_paths=runtime_paths)

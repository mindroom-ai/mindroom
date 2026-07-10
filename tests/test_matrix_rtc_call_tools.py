"""Tests for bridging MindRoom agent tools into the realtime call session."""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING, ClassVar

import pytest
from agno.tools.function import Function

from mindroom.config.agent import AgentConfig
from mindroom.config.approval import ApprovalRuleConfig, ToolApprovalConfig
from mindroom.config.main import Config
from mindroom.matrix_rtc.call_tools import _wrap_agno_function, build_call_tools
from mindroom.tool_system.runtime_context import get_tool_runtime_context
from tests.conftest import test_runtime_paths

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.message_target import MessageTarget

AGENT = "helper"


def _config() -> Config:
    return Config(agents={AGENT: AgentConfig(display_name="Helper")}, models={})


def _context() -> SimpleNamespace:
    # The wrapper only stores and re-binds the context; a stand-in suffices.
    return SimpleNamespace(
        room_id="!room:example.org",
        hook_registry=object(),
        orchestrator=None,
    )


def _function(entrypoint: object, parameters: dict | None = None, *, name: str = "add") -> Function:
    return Function(
        name=name,
        description="Add two numbers",
        parameters=parameters or {"type": "object", "properties": {"a": {"type": "integer"}, "b": {"type": "integer"}}},
        entrypoint=entrypoint,
    )


def _wrap(function: Function):  # noqa: ANN202
    return _wrap_agno_function(
        function,
        context=_context(),
        agent_name=AGENT,
        config=_config(),
    )


@pytest.mark.asyncio
async def test_wrapped_tool_executes_sync_entrypoint_in_context() -> None:
    """Sync entrypoints run in a worker thread with the runtime context bound."""
    seen_context: list[object] = []

    def add(a: int, b: int) -> int:
        seen_context.append(get_tool_runtime_context())
        return a + b

    tool = _wrap(_function(add))
    result = await tool({"a": 2, "b": 3})
    assert result == "5"
    assert seen_context
    assert seen_context[0] is not None


@pytest.mark.asyncio
async def test_wrapped_tool_executes_async_entrypoint() -> None:
    """Async entrypoints are awaited directly."""

    async def add(a: int, b: int) -> str:
        return f"sum={a + b}"

    tool = _wrap(_function(add))
    assert await tool({"a": 1, "b": 1}) == "sum=2"


@pytest.mark.asyncio
async def test_wrapped_tool_runs_agno_tool_hooks() -> None:
    """Voice tool execution preserves Agno hook policy and result transformations."""
    calls: list[str] = []

    def add(a: int, b: int) -> int:
        calls.append("tool")
        return a + b

    def hook(name: str, function: object, arguments: dict[str, int]) -> str:
        calls.append(name)
        result = function(**arguments)  # type: ignore[operator]
        return f"hooked={result}"

    function = _function(add)
    function.tool_hooks = [hook]
    tool = _wrap(function)

    assert await tool({"a": 2, "b": 4}) == "hooked=6"
    assert calls == ["add", "tool"]


@pytest.mark.asyncio
async def test_wrapped_tool_refuses_agno_interactive_execution_policy() -> None:
    """Agno-managed confirmation flows do not execute without their text UI."""
    calls: list[str] = []

    def add(a: int, b: int) -> int:
        calls.append("tool")
        return a + b

    function = _function(add)
    function.requires_confirmation = True
    tool = _wrap(function)

    result = await tool({"a": 2, "b": 4})

    assert "text chat" in result
    assert calls == []


@pytest.mark.asyncio
async def test_wrapped_tool_refuses_when_approval_required() -> None:
    """The canonical agent hook owns approval evaluation for voice tools."""
    calls: list[object] = []
    approval_checks: list[dict[str, int]] = []

    def add(a: int, b: int) -> int:
        calls.append((a, b))
        return a + b

    async def approval_hook(name: str, function: object, arguments: dict[str, int]) -> str:
        del name, function
        approval_checks.append(dict(arguments))
        return "Tool approval is required; use the text chat."

    function = _function(add)
    function.tool_hooks = [approval_hook]
    tool = _wrap(function)
    result = await tool({"a": 1, "b": 2})
    assert "approval" in result.lower()
    assert approval_checks == [{"a": 1, "b": 2}]
    assert calls == []


@pytest.mark.asyncio
async def test_wrapped_async_tool_awaits_coroutine_returned_by_sync_hook() -> None:
    """A synchronous hook may delegate to an asynchronous tool entrypoint."""

    async def add(a: int, b: int) -> int:
        return a + b

    def hook(name: str, function: object, arguments: dict[str, int]) -> object:
        del name
        return function(**arguments)  # type: ignore[operator]

    function = _function(add)
    function.tool_hooks = [hook]

    assert await _wrap(function)({"a": 2, "b": 5}) == "7"


@pytest.mark.asyncio
async def test_wrapped_tool_reports_failures_to_the_model() -> None:
    """Tool exceptions come back as spoken-friendly error strings."""

    def boom() -> None:
        msg = "database on fire"
        raise RuntimeError(msg)

    tool = _wrap(_function(boom, parameters={"type": "object", "properties": {}}))
    result = await tool({})
    assert "failed" in result
    assert "database on fire" in result


def test_wrap_processes_unprocessed_toolkit_function_schema() -> None:
    """Toolkit functions start with an empty schema; wrapping must build the real one."""

    def add(a: int, b: int) -> int:
        return a + b

    # Simulate an agno toolkit function before entrypoint processing.
    function = Function(name="add", description="Add two numbers", entrypoint=add)
    assert function.parameters == {"type": "object", "properties": {}, "required": []}
    _wrap(function)
    assert set(function.parameters["properties"]) == {"a", "b"}
    assert set(function.parameters["required"]) == {"a", "b"}


@pytest.mark.asyncio
async def test_build_call_tools_returns_same_agent_prompt_and_tools(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The bridge materializes the chat agent's toolkits and system prompt."""

    def add(a: int, b: int) -> int:
        return a + b

    toolkit = SimpleNamespace(functions={"add": _function(add)}, async_functions={})

    class FakeAgnoAgent:
        tools: ClassVar[list] = [toolkit]

        def get_system_message(self, _session: object) -> SimpleNamespace:
            return SimpleNamespace(content="THE CHAT SYSTEM PROMPT")

    create_calls: list[tuple[object, ...]] = []
    create_kwargs: dict[str, object] = {}
    knowledge = object()
    hook_registry = object()
    refresh_scheduler = object()

    def fake_create_agent(*args: object, **kwargs: object) -> FakeAgnoAgent:
        create_calls.append(args)
        create_kwargs.update(kwargs)
        return FakeAgnoAgent()

    monkeypatch.setattr(
        "mindroom.matrix_rtc.call_tools.create_agent",
        fake_create_agent,
    )
    monkeypatch.setattr(
        "mindroom.matrix_rtc.call_tools.resolve_agent_knowledge_access",
        lambda *_args, **_kwargs: SimpleNamespace(knowledge=knowledge),
    )
    seen_targets: list[MessageTarget] = []

    class StrictToolSupport:
        def build_context(self, target: MessageTarget, *, user_id: str | None) -> SimpleNamespace:
            assert user_id is None
            seen_targets.append(target)
            return SimpleNamespace(
                room_id="!room:example.org",
                hook_registry=hook_registry,
                orchestrator=SimpleNamespace(knowledge_refresh_scheduler=refresh_scheduler),
            )

        def build_execution_identity(
            self,
            *,
            target: MessageTarget,
            user_id: str | None,
            agent_name: str | None = None,
        ) -> SimpleNamespace:
            assert user_id is None
            assert agent_name == AGENT
            seen_targets.append(target)
            return SimpleNamespace()

    tooling = await build_call_tools(
        agent_name=AGENT,
        config=_config(),
        runtime_paths=test_runtime_paths(tmp_path),
        tool_support=StrictToolSupport(),  # type: ignore[arg-type]
        room_id="!room:example.org",
    )
    assert len(tooling.tools) == 1
    assert tooling.instructions == "THE CHAT SYSTEM PROMPT"
    assert create_kwargs["eager_deferred_tools"] is True
    assert create_kwargs["hook_registry"] is hook_registry
    assert create_kwargs["knowledge"] is knowledge
    assert create_kwargs["refresh_scheduler"] is refresh_scheduler
    assert create_calls
    assert create_calls[0][3] is not None
    assert len(seen_targets) == 2
    assert seen_targets[0] is seen_targets[1]
    assert seen_targets[0].session_id == "!room:example.org"


@pytest.mark.asyncio
async def test_build_call_tools_includes_async_only_toolkit_functions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Async-only Agno toolkit registrations are exposed to the realtime model."""

    async def async_add(a: int, b: int) -> int:
        return a + b

    function = _function(async_add)
    toolkit = SimpleNamespace(functions={}, async_functions={"add": function})

    class FakeAgnoAgent:
        tools: ClassVar[list] = [toolkit]

        def get_system_message(self, _session: object) -> SimpleNamespace:
            return SimpleNamespace(content="THE CHAT SYSTEM PROMPT")

    monkeypatch.setattr(
        "mindroom.matrix_rtc.call_tools.create_agent",
        lambda *_args, **_kwargs: FakeAgnoAgent(),
    )
    tool_support = SimpleNamespace(
        build_context=lambda *_a, **_k: _context(),
        build_execution_identity=lambda **_k: SimpleNamespace(),
    )

    tooling = await build_call_tools(
        agent_name=AGENT,
        config=_config(),
        runtime_paths=test_runtime_paths(tmp_path),
        tool_support=tool_support,  # type: ignore[arg-type]
        room_id="!room:example.org",
    )

    assert len(tooling.tools) == 1


@pytest.mark.asyncio
async def test_build_call_tools_hides_functions_needing_text_chat(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unsupported approval and interaction flows never enter the voice schema."""
    functions = {
        name: _function(lambda name=name: name, {"type": "object", "properties": {}}, name=name)
        for name in ("safe", "confirm", "user_input", "external", "agno_approval", "policy_approval")
    }
    functions["confirm"].requires_confirmation = True
    functions["user_input"].requires_user_input = True
    functions["external"].external_execution = True
    functions["agno_approval"].approval_type = "required"
    toolkit = SimpleNamespace(functions=functions, async_functions={})

    class FakeAgnoAgent:
        tools: ClassVar[list] = [toolkit]

        def get_system_message(self, _session: object) -> SimpleNamespace:
            return SimpleNamespace(content="THE CHAT SYSTEM PROMPT")

    config = _config()
    config.tool_approval = ToolApprovalConfig(
        rules=[ApprovalRuleConfig(match="policy_approval", action="require_approval")],
    )
    monkeypatch.setattr("mindroom.matrix_rtc.call_tools.create_agent", lambda *_a, **_k: FakeAgnoAgent())
    monkeypatch.setattr(
        "mindroom.matrix_rtc.call_tools.resolve_agent_knowledge_access",
        lambda *_a, **_k: SimpleNamespace(knowledge=None),
    )
    monkeypatch.setattr(
        "mindroom.matrix_rtc.call_tools._wrap_agno_function",
        lambda function, **_kwargs: function.name,
    )
    tool_support = SimpleNamespace(
        build_context=lambda *_a, **_k: _context(),
        build_execution_identity=lambda **_k: SimpleNamespace(),
    )

    tooling = await build_call_tools(
        agent_name=AGENT,
        config=config,
        runtime_paths=test_runtime_paths(tmp_path),
        tool_support=tool_support,  # type: ignore[arg-type]
        room_id="!room:example.org",
    )

    assert tooling.tools == ("safe",)


@pytest.mark.asyncio
async def test_build_call_tools_requires_runtime_context(tmp_path: Path) -> None:
    """Voice cannot silently downgrade when same-agent tool context is unavailable."""
    tool_support = SimpleNamespace(build_context=lambda *_a, **_k: None)

    with pytest.raises(RuntimeError, match="runtime context unavailable"):
        await build_call_tools(
            agent_name=AGENT,
            config=_config(),
            runtime_paths=test_runtime_paths(tmp_path),
            tool_support=tool_support,  # type: ignore[arg-type]
            room_id="!room:example.org",
        )

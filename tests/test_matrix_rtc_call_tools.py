"""Tests for bridging MindRoom agent tools into the realtime call session."""

from __future__ import annotations

import asyncio
from contextlib import nullcontext
from threading import Event
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from agno.knowledge.knowledge import Knowledge
from agno.models.metrics import Metrics
from agno.run.base import RunStatus
from agno.tools.function import Function

from mindroom.agent_knowledge_descriptions import KnowledgeToolDescribingAgent
from mindroom.config.agent import AgentConfig
from mindroom.config.approval import ApprovalRuleConfig, ToolApprovalConfig
from mindroom.config.main import Config
from mindroom.config.models import ModelConfig
from mindroom.constants import AI_RUN_METADATA_KEY
from mindroom.knowledge import KnowledgeAvailability, KnowledgeAvailabilityDetail
from mindroom.matrix_rtc.call_tools import (
    _CallAgentRunState,
    _CallResponseTracker,
    _wrap_agno_function,
    build_call_tools,
)
from mindroom.memory import MemoryPromptParts
from mindroom.tool_system.events import ToolTraceEntry
from mindroom.tool_system.runtime_context import ToolRuntimeContext, get_tool_runtime_context, tool_runtime_context
from mindroom.tool_system.worker_routing import build_tool_execution_identity
from tests.conftest import bind_runtime_paths, test_runtime_paths

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from pathlib import Path

    from mindroom.message_target import MessageTarget
    from mindroom.response_turn import ResponseTurnContext

AGENT = "helper"
REQUESTER = "@alice:example.org"


class FakeAgnoAgent:
    """Small typed stand-in for Agno's effective async tool surface."""

    def __init__(self, tools: list[Function], *, prompt: str = "THE CHAT SYSTEM PROMPT") -> None:
        self.name = "Helper"
        self.model = SimpleNamespace(supports_native_structured_outputs=False)
        self._tool_instructions: list[str] = []
        self._team = None
        self.tool_hooks = None
        self._tools = tools
        self._prompt = prompt
        self.tool_user_ids: list[str | None] = []

    async def aget_tools(self, **kwargs: object) -> list[Function]:
        """Return the prepared effective tools and capture requester identity."""
        self.tool_user_ids.append(kwargs.get("user_id") if isinstance(kwargs.get("user_id"), str) else None)
        return self._tools

    async def aget_system_message(self, *_args: object, **_kwargs: object) -> SimpleNamespace:
        """Return a deterministic rendered prompt."""
        return SimpleNamespace(content=self._prompt)


def _config() -> Config:
    return Config(agents={AGENT: AgentConfig(display_name="Helper")}, models={})


def _context() -> SimpleNamespace:
    # The wrapper only stores and re-binds the context; a stand-in suffices.
    return SimpleNamespace(
        room_id="!room:example.org",
        hook_registry=object(),
        orchestrator=None,
    )


def _runtime_context(
    *,
    config: Config,
    runtime_paths: object,
    target: MessageTarget,
    hook_registry: object | None = None,
    orchestrator: object | None = None,
) -> ToolRuntimeContext:
    """Build the real typed call context expected by production code."""
    return ToolRuntimeContext(
        agent_name=AGENT,
        target=target,
        requester_id=REQUESTER,
        client=MagicMock(),
        config=config,
        runtime_paths=runtime_paths,  # type: ignore[arg-type]
        event_cache=MagicMock(),
        conversation_cache=MagicMock(),
        hook_registry=hook_registry or MagicMock(),  # type: ignore[arg-type]
        orchestrator=orchestrator,  # type: ignore[arg-type]
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
    bound_filters: list[object] = []

    def add(a: int, b: int) -> int:
        runtime_context = get_tool_runtime_context()
        bound_filters.append(runtime_context.tool_function_filter if runtime_context is not None else None)
        return a + b

    fake_agent = FakeAgnoAgent([_function(add)])

    create_calls: list[tuple[object, ...]] = []
    create_kwargs: dict[str, object] = {}
    knowledge = object()
    hook_registry = object()
    refresh_scheduler = object()
    config = _config()
    runtime_paths = test_runtime_paths(tmp_path)
    execution_identity = build_tool_execution_identity(
        channel="matrix",
        agent_name=AGENT,
        runtime_paths=runtime_paths,
        requester_id=REQUESTER,
        room_id="!room:example.org",
        thread_id=None,
        resolved_thread_id=None,
        session_id="!room:example.org",
    )
    knowledge_calls: list[dict[str, object]] = []

    def fake_create_agent(*args: object, **kwargs: object) -> FakeAgnoAgent:
        create_calls.append(args)
        create_kwargs.update(kwargs)
        return fake_agent

    monkeypatch.setattr(
        "mindroom.matrix_rtc.call_tools.create_agent",
        fake_create_agent,
    )

    def fake_resolve_knowledge(*_args: object, **kwargs: object) -> SimpleNamespace:
        knowledge_calls.append(kwargs)
        return SimpleNamespace(knowledge=knowledge)

    monkeypatch.setattr("mindroom.matrix_rtc.call_tools.resolve_agent_knowledge_access", fake_resolve_knowledge)
    seen_targets: list[MessageTarget] = []

    class StrictToolSupport:
        def build_context(
            self,
            target: MessageTarget,
            *,
            user_id: str | None,
            agent_name: str | None = None,
            active_model_name: str | None = None,
        ) -> ToolRuntimeContext:
            assert user_id == REQUESTER
            assert agent_name == AGENT
            assert active_model_name is None
            seen_targets.append(target)
            return _runtime_context(
                config=config,
                runtime_paths=runtime_paths,
                target=target,
                hook_registry=hook_registry,
                orchestrator=SimpleNamespace(knowledge_refresh_scheduler=refresh_scheduler),
            )

        def build_execution_identity(
            self,
            *,
            target: MessageTarget,
            user_id: str | None,
            agent_name: str | None = None,
        ) -> object:
            assert user_id == REQUESTER
            assert agent_name == AGENT
            seen_targets.append(target)
            return execution_identity

    tooling = await build_call_tools(
        agent_name=AGENT,
        config=config,
        runtime_paths=runtime_paths,
        tool_support=StrictToolSupport(),  # type: ignore[arg-type]
        room_id="!room:example.org",
        requester_id=REQUESTER,
    )
    assert len(tooling.tools) == 1
    assert tooling.instructions == "THE CHAT SYSTEM PROMPT"
    assert create_kwargs["eager_deferred_tools"] is True
    assert create_kwargs["hook_registry"] is hook_registry
    assert create_kwargs["knowledge"] is knowledge
    assert create_kwargs["refresh_scheduler"] is refresh_scheduler
    assert create_kwargs["tool_function_filter"] is not None
    assert create_calls
    assert create_calls[0][3] is execution_identity
    assert knowledge_calls[0]["execution_identity"] is execution_identity
    assert tooling.execution_identity is execution_identity
    assert len(seen_targets) == 2
    assert seen_targets[0] is seen_targets[1]
    assert seen_targets[0].session_id == "!room:example.org"
    assert fake_agent.tool_user_ids == [REQUESTER]
    assert await tooling.tools[0]({"a": 2, "b": 3}) == "5"
    assert bound_filters[0] is create_kwargs["tool_function_filter"]


@pytest.mark.asyncio
async def test_cascaded_responder_uses_normal_agent_turn_and_filters_unsafe_functions(  # noqa: PLR0915
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cascaded transcripts reuse ai_response with full identity and a function-level call policy."""
    config = _config()
    config.tool_approval = ToolApprovalConfig(
        rules=[ApprovalRuleConfig(match="policy_approval", action="require_approval")],
    )
    knowledge = SimpleNamespace(vector_db=None)
    refresh_scheduler = object()
    execution_identity = SimpleNamespace()
    calls: list[tuple[ResponseTurnContext, dict[str, object]]] = []
    persisted_interruptions: list[dict[str, object]] = []
    completed_tools = [
        ToolTraceEntry(type="tool_call_completed", tool_name="weather"),
        ToolTraceEntry(type="tool_call_completed", tool_name="weather"),
    ]
    contexts: list[ToolRuntimeContext] = []
    recorded_tool_uses: list[list[str]] = []
    runtime_paths = test_runtime_paths(tmp_path)

    class StrictToolSupport:
        def build_context(
            self,
            target: MessageTarget,
            *,
            user_id: str | None,
            agent_name: str | None = None,
            active_model_name: str | None = None,
        ) -> ToolRuntimeContext:
            context = ToolRuntimeContext(
                agent_name=agent_name or AGENT,
                target=target,
                requester_id=user_id or REQUESTER,
                client=MagicMock(),
                config=config,
                runtime_paths=runtime_paths,
                event_cache=MagicMock(),
                conversation_cache=MagicMock(),
                hook_registry=MagicMock(),
                orchestrator=SimpleNamespace(knowledge_refresh_scheduler=refresh_scheduler),  # type: ignore[arg-type]
                active_model_name=active_model_name,
            )
            contexts.append(context)
            return context

        def build_execution_identity(self, **_kwargs: object) -> SimpleNamespace:
            return execution_identity

        async def run_in_context(
            self,
            *,
            tool_context: ToolRuntimeContext,
            operation: Callable[[], Awaitable[str]],
        ) -> str:
            assert tool_context.tool_function_filter is not None
            return await operation()

    async def fake_ai_response(turn: ResponseTurnContext, **kwargs: object) -> str:
        calls.append((turn, kwargs))
        run_id_callback = cast("Callable[[str], None]", kwargs["run_id_callback"])
        run_id_callback("call-run-1")
        recorder = kwargs["turn_recorder"]
        recorder.record_completed(  # type: ignore[union-attr]
            run_metadata={"model": "same-chat-model"},
            assistant_text="It is sunny.",
            completed_tools=completed_tools,
        )
        return "It is sunny."

    cached_agent = SimpleNamespace(additional_context="base context")
    create_agent_mock = MagicMock(return_value=cached_agent)
    close_agent_mock = MagicMock()
    monkeypatch.setattr("mindroom.matrix_rtc.call_tools.create_agent", create_agent_mock)
    monkeypatch.setattr("mindroom.matrix_rtc.call_tools.close_agent_runtime_state_dbs", close_agent_mock)
    monkeypatch.setattr(
        "mindroom.matrix_rtc.call_tools.resolve_agent_knowledge_access",
        lambda *_args, **_kwargs: SimpleNamespace(knowledge=knowledge, unavailable=()),
    )
    monkeypatch.setattr("mindroom.ai.ai_response", fake_ai_response)
    monkeypatch.setattr(
        "mindroom.matrix_rtc.call_tools.open_resolved_scope_session_context",
        lambda **_kwargs: nullcontext(SimpleNamespace()),
    )
    monkeypatch.setattr(
        "mindroom.matrix_rtc.call_tools.persist_interrupted_replay",
        lambda **kwargs: persisted_interruptions.append(kwargs),
    )
    tooling = await build_call_tools(
        agent_name=AGENT,
        config=config,
        runtime_paths=runtime_paths,
        tool_support=StrictToolSupport(),  # type: ignore[arg-type]
        room_id="!room:example.org",
        requester_id=REQUESTER,
        session_id="!room:example.org:call:one",
        enable_responder=True,
        voice_instructions="Speak briefly.",
        active_model_name="call_fast",
    )

    assert tooling.tools == ()
    assert tooling.instructions == ""
    assert tooling.execution_identity is execution_identity
    assert tooling.responder is not None
    response = await tooling.responder("What is the weather?", recorded_tool_uses.append)

    assert response.text == "It is sunny."
    assert response.tool_names == ("weather",)
    assert response.turn_id is not None
    assert recorded_tool_uses == [["weather"]]
    create_agent_mock.assert_called_once()
    turn, kwargs = calls[0]
    assert turn.entity_label == AGENT
    assert turn.requester_id == REQUESTER
    assert turn.session_id == "!room:example.org:call:one"
    assert turn.active_model_name == "call_fast"
    assert contexts[0].active_model_name == "call_fast"
    assert turn.system_enrichment_items[0].text == "Speak briefly."
    assert kwargs["config"] is config
    assert kwargs["knowledge"] is knowledge
    assert kwargs["execution_identity"] is execution_identity
    assert kwargs["refresh_scheduler"] is refresh_scheduler
    assert kwargs["include_interactive_questions"] is False
    assert kwargs["show_tool_calls"] is False
    assert kwargs["eager_deferred_tools"] is True
    assert kwargs["reusable_agent"] is cached_agent
    assert tooling.finalize_spoken_response is not None
    finalize = tooling.finalize_spoken_response(response.turn_id, "It is", True)
    assert finalize is not None
    await finalize
    assert persisted_interruptions == [
        {
            "scope_context": SimpleNamespace(),
            "session_id": "!room:example.org:call:one",
            "run_id": "call-run-1",
            "user_message": "What is the weather?",
            "user_message_is_structured": False,
            "partial_text": "It is",
            "completed_tools": tuple(completed_tools),
            "interrupted_tools": (),
            "run_metadata": {"model": "same-chat-model"},
            "is_team": False,
            "original_status": RunStatus.cancelled,
        },
    ]

    async def cancel_before_playout(_turn: ResponseTurnContext, **cancel_kwargs: object) -> str:
        run_id_callback = cast("Callable[[str], None]", cancel_kwargs["run_id_callback"])
        run_id_callback("call-run-2")
        recorder = cancel_kwargs["turn_recorder"]
        recorder.record_interrupted(  # type: ignore[union-attr]
            run_metadata={"model": "same-chat-model"},
            assistant_text="generated but never spoken",
            completed_tools=(ToolTraceEntry(type="tool_call_completed", tool_name="calendar"),),
            interrupted_tools=(),
        )
        raise asyncio.CancelledError

    monkeypatch.setattr("mindroom.ai.ai_response", cancel_before_playout)
    with pytest.raises(asyncio.CancelledError):
        await tooling.responder("Never speak this", recorded_tool_uses.append)
    assert persisted_interruptions[-1]["run_id"] == "call-run-2"
    assert persisted_interruptions[-1]["user_message"] == "Never speak this"
    assert persisted_interruptions[-1]["partial_text"] == ""
    assert persisted_interruptions[-1]["original_status"] is RunStatus.cancelled
    assert recorded_tool_uses[-1] == ["calendar"]

    async def return_error_without_run_id(_turn: ResponseTurnContext, **error_kwargs: object) -> str:
        recorder = error_kwargs["turn_recorder"]
        recorder.record_interrupted(  # type: ignore[union-attr]
            run_metadata={"model": "same-chat-model"},
            assistant_text="provider failed",
            completed_tools=(),
            interrupted_tools=(),
            original_status=RunStatus.error,
        )
        return "provider failed"

    monkeypatch.setattr("mindroom.ai.ai_response", return_error_without_run_id)
    error_response = await tooling.responder("Trigger an error", recorded_tool_uses.append)
    assert error_response.turn_id is not None
    persist_error = tooling.finalize_spoken_response(error_response.turn_id, "provider failed", False)
    assert persist_error is not None
    await persist_error
    assert str(persisted_interruptions[-1]["run_id"]).startswith("!room:example.org:call:one:turn:")
    assert persisted_interruptions[-1]["partial_text"] == "provider failed"
    assert persisted_interruptions[-1]["original_status"] is RunStatus.error

    tool_filter = cast("Callable[[Function], bool]", kwargs["tool_function_filter"])
    safe = _function(lambda: "safe", {"type": "object", "properties": {}}, name="safe")
    confirm = _function(lambda: "confirm", {"type": "object", "properties": {}}, name="confirm")
    confirm.requires_confirmation = True
    policy = _function(
        lambda: "policy",
        {"type": "object", "properties": {}},
        name="policy_approval",
    )
    workflow = _function(
        lambda: "workflow",
        {"type": "object", "properties": {}},
        name="run_workflow",
    )
    spawn = _function(lambda: "spawn", {"type": "object", "properties": {}}, name="sessions_spawn")
    send = _function(lambda: "send", {"type": "object", "properties": {}}, name="sessions_send")
    assert tool_filter(safe) is True
    assert tool_filter(confirm) is False
    assert tool_filter(policy) is False
    assert tool_filter(workflow) is False
    assert tool_filter(spawn) is False
    assert tool_filter(send) is False
    assert contexts[0].tool_function_filter is None
    assert tooling.close is not None
    await tooling.close()
    close_agent_mock.assert_called_once_with(cached_agent)


@pytest.mark.asyncio
async def test_cascaded_responder_records_call_selected_model_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The call override drives real agent preparation and persisted run metadata."""
    runtime_paths = test_runtime_paths(tmp_path)
    config = bind_runtime_paths(
        Config(
            agents={AGENT: AgentConfig(display_name="Helper", model="default")},
            room_models={"lobby": "large"},
            models={
                "default": ModelConfig(provider="openai", id="default-model"),
                "large": ModelConfig(provider="openai", id="large-model", context_window=48_000),
                "call_fast": ModelConfig(provider="openai", id="fast-model", context_window=16_000),
            },
        ),
        runtime_paths,
    )
    execution_identity = SimpleNamespace()
    persisted_interruptions: list[dict[str, object]] = []

    class StrictToolSupport:
        def build_context(
            self,
            target: MessageTarget,
            *,
            user_id: str | None,
            agent_name: str | None = None,
            active_model_name: str | None = None,
        ) -> ToolRuntimeContext:
            return ToolRuntimeContext(
                agent_name=agent_name or AGENT,
                target=target,
                requester_id=user_id or REQUESTER,
                client=MagicMock(),
                config=config,
                runtime_paths=runtime_paths,
                event_cache=MagicMock(),
                conversation_cache=MagicMock(),
                hook_registry=MagicMock(),
                active_model_name=active_model_name,
            )

        def build_execution_identity(self, **_kwargs: object) -> SimpleNamespace:
            return execution_identity

        async def run_in_context(
            self,
            *,
            tool_context: ToolRuntimeContext,
            operation: Callable[[], Awaitable[str]],
        ) -> str:
            assert get_tool_runtime_context() is None
            with tool_runtime_context(tool_context):
                return await operation()

    mock_agent = MagicMock()
    mock_agent.model = MagicMock()
    mock_agent.model.__class__.__name__ = "OpenAIChat"
    mock_agent.model.id = "fast-model"
    mock_agent.name = "Helper"
    mock_agent.additional_context = "base context"
    mock_agent.add_history_to_context = False
    mock_run_output = MagicMock()
    mock_run_output.run_id = "call-run-1"
    mock_run_output.session_id = "!room:example.org:call:metadata"
    mock_run_output.status = RunStatus.completed
    mock_run_output.model = "fast-model"
    mock_run_output.model_provider = "openai"
    mock_run_output.metrics = Metrics(input_tokens=100, output_tokens=20, total_tokens=120)
    mock_run_output.tools = None
    mock_run_output.content = "It is sunny."

    create_agent_mock = MagicMock(return_value=mock_agent)
    ai_close_agent_mock = MagicMock()
    monkeypatch.setattr("mindroom.matrix_rtc.call_tools.create_agent", create_agent_mock)
    monkeypatch.setattr("mindroom.ai.close_agent_runtime_state_dbs", ai_close_agent_mock)
    monkeypatch.setattr(
        "mindroom.ai.build_memory_prompt_parts",
        AsyncMock(return_value=MemoryPromptParts()),
    )
    monkeypatch.setattr(
        "mindroom.ai_runtime.cached_agent_run",
        AsyncMock(return_value=mock_run_output),
    )
    monkeypatch.setattr("mindroom.matrix.state.get_room_alias_from_id", lambda *_args: "lobby")
    monkeypatch.setattr(
        "mindroom.ai.open_resolved_scope_session_context",
        lambda **_kwargs: nullcontext(None),
    )
    monkeypatch.setattr(
        "mindroom.matrix_rtc.call_tools.resolve_agent_knowledge_access",
        lambda *_args, **_kwargs: SimpleNamespace(knowledge=None, unavailable=()),
    )
    monkeypatch.setattr(
        "mindroom.matrix_rtc.call_tools.open_resolved_scope_session_context",
        lambda **_kwargs: nullcontext(SimpleNamespace()),
    )
    monkeypatch.setattr(
        "mindroom.matrix_rtc.call_tools.persist_interrupted_replay",
        lambda **kwargs: persisted_interruptions.append(kwargs),
    )

    tooling = await build_call_tools(
        agent_name=AGENT,
        config=config,
        runtime_paths=runtime_paths,
        tool_support=StrictToolSupport(),  # type: ignore[arg-type]
        room_id="!room:example.org",
        requester_id=REQUESTER,
        session_id="!room:example.org:call:metadata",
        enable_responder=True,
        active_model_name="call_fast",
    )

    assert tooling.responder is not None
    response = await tooling.responder("What is the weather?", None)
    assert response.text == "It is sunny."
    assert response.turn_id is not None
    assert tooling.finalize_spoken_response is not None
    finalize = tooling.finalize_spoken_response(response.turn_id, "It is", True)
    assert finalize is not None
    await finalize

    assert create_agent_mock.call_args.kwargs["active_model_name"] == "call_fast"
    run_metadata = persisted_interruptions[0]["run_metadata"]
    assert isinstance(run_metadata, dict)
    payload = run_metadata[AI_RUN_METADATA_KEY]
    assert payload["model"]["config"] == "call_fast"
    assert payload["model"]["id"] == "fast-model"
    assert payload["context"]["window_tokens"] == 16_000
    assert (mock_agent.additional_context, ai_close_agent_mock.call_count) == ("base context", 0)


def test_call_response_tracker_keeps_fifo_without_retaining_settled_tokens(tmp_path: Path) -> None:
    """Explicit settlement must not leave an ever-growing fallback-order index."""
    tracker = _CallResponseTracker(
        agent_name=AGENT,
        config=_config(),
        runtime_paths=test_runtime_paths(tmp_path),
        execution_identity=SimpleNamespace(),  # type: ignore[arg-type]
    )
    state = _CallAgentRunState(
        session_id="call-session",
        run_id="run",
        user_message="hello",
        completed_tools=(),
        interrupted_tools=(),
        run_metadata={},
        outcome="completed",
        original_status=None,
    )
    first = tracker.register(state)
    second = tracker.register(state)

    assert tracker.finalize(None, "done", False) is None
    assert tuple(tracker.pending) == (second,)
    assert tracker.finalize(second, "done", False) is None
    for _ in range(1_000):
        token = tracker.register(state)
        assert tracker.finalize(token, "done", False) is None

    assert tracker.pending == {}
    assert first not in tracker.pending


@pytest.mark.asyncio
async def test_call_response_tracker_failed_settlement_does_not_abort_next_turn(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """History-write failure stays on the event task instead of failing the next response."""
    tracker = _CallResponseTracker(
        agent_name=AGENT,
        config=_config(),
        runtime_paths=test_runtime_paths(tmp_path),
        execution_identity=SimpleNamespace(),  # type: ignore[arg-type]
    )
    state = _CallAgentRunState(
        session_id="call-session",
        run_id="failed-run",
        user_message="hello",
        completed_tools=(),
        interrupted_tools=(),
        run_metadata={},
        outcome="completed",
        original_status=None,
    )
    started = Event()
    release = Event()

    def fail_persistence(*_args: object) -> None:
        started.set()
        assert release.wait(timeout=5)
        msg = "history unavailable"
        raise RuntimeError(msg)

    monkeypatch.setattr(tracker, "_persist", fail_persistence)
    token = tracker.register(state)
    settlement = tracker.finalize(token, "partial", True)
    assert settlement is not None
    assert await asyncio.to_thread(started.wait, 5)
    next_turn = asyncio.create_task(tracker.wait_for_settlements())
    try:
        await asyncio.sleep(0)
        assert not next_turn.done()
    finally:
        release.set()

    await next_turn
    with pytest.raises(RuntimeError, match="history unavailable"):
        await settlement


@pytest.mark.asyncio
async def test_cascaded_responder_refreshes_knowledge_and_availability_each_turn(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A call can use a knowledge index that becomes ready after the call joins."""
    config = _config()
    runtime_paths = test_runtime_paths(tmp_path)
    first_scheduler = object()
    second_scheduler = object()
    orchestrator = SimpleNamespace(knowledge_refresh_scheduler=first_scheduler)
    execution_identity = SimpleNamespace()
    ready_knowledge = SimpleNamespace(vector_db=None)
    resolver_calls: list[dict[str, object]] = []
    ai_calls: list[tuple[ResponseTurnContext, dict[str, object]]] = []
    resolutions = iter(
        (
            SimpleNamespace(
                knowledge=None,
                unavailable={
                    "docs": KnowledgeAvailabilityDetail(
                        availability=KnowledgeAvailability.INITIALIZING,
                        search_available=False,
                    ),
                },
            ),
            SimpleNamespace(knowledge=ready_knowledge, unavailable={}),
        ),
    )

    class ToolSupport:
        def build_context(self, target: MessageTarget, **_kwargs: object) -> ToolRuntimeContext:
            return _runtime_context(
                config=config,
                runtime_paths=runtime_paths,
                target=target,
                orchestrator=orchestrator,
            )

        def build_execution_identity(self, **_kwargs: object) -> SimpleNamespace:
            return execution_identity

        async def run_in_context(
            self,
            *,
            tool_context: ToolRuntimeContext,
            operation: Callable[[], Awaitable[str]],
        ) -> str:
            assert tool_context.tool_function_filter is not None
            return await operation()

    def resolve_knowledge(*_args: object, **kwargs: object) -> SimpleNamespace:
        resolver_calls.append(kwargs)
        return next(resolutions)

    async def fake_ai_response(turn: ResponseTurnContext, **kwargs: object) -> str:
        ai_calls.append((turn, kwargs))
        return f"answer-{len(ai_calls)}"

    first_agent = SimpleNamespace(additional_context="first base")
    second_agent = SimpleNamespace(additional_context="second base")
    create_agent_mock = MagicMock(side_effect=(first_agent, second_agent))
    close_agent_mock = MagicMock()
    monkeypatch.setattr("mindroom.matrix_rtc.call_tools.resolve_agent_knowledge_access", resolve_knowledge)
    monkeypatch.setattr("mindroom.matrix_rtc.call_tools.create_agent", create_agent_mock)
    monkeypatch.setattr("mindroom.matrix_rtc.call_tools.close_agent_runtime_state_dbs", close_agent_mock)
    monkeypatch.setattr("mindroom.ai.ai_response", fake_ai_response)
    tooling = await build_call_tools(
        agent_name=AGENT,
        config=config,
        runtime_paths=runtime_paths,
        tool_support=ToolSupport(),  # type: ignore[arg-type]
        room_id="!room:example.org",
        requester_id=REQUESTER,
        enable_responder=True,
        voice_instructions="Speak briefly.",
    )
    assert resolver_calls == []
    assert tooling.responder is not None
    assert tooling.finalize_spoken_response is not None

    first = await tooling.responder("first turn", None)
    assert tooling.finalize_spoken_response(first.turn_id, first.text, False) is None
    orchestrator.knowledge_refresh_scheduler = second_scheduler
    second = await tooling.responder("second turn", None)
    assert tooling.finalize_spoken_response(second.turn_id, second.text, False) is None

    assert [call["refresh_scheduler"] for call in resolver_calls] == [first_scheduler, second_scheduler]
    assert all(call["execution_identity"] is execution_identity for call in resolver_calls)
    assert ai_calls[0][1]["knowledge"] is None
    assert ai_calls[1][1]["knowledge"] is ready_knowledge
    assert ai_calls[0][1]["reusable_agent"] is first_agent
    assert ai_calls[1][1]["reusable_agent"] is second_agent
    assert create_agent_mock.call_count == 2
    close_agent_mock.assert_called_once_with(first_agent)
    assert [item.key for item in ai_calls[0][0].system_enrichment_items] == ["voice_call"]
    assert [item.key for item in ai_calls[0][0].transient_enrichment_items] == ["knowledge_availability"]
    assert [item.key for item in ai_calls[1][0].system_enrichment_items] == ["voice_call"]
    assert ai_calls[1][0].transient_enrichment_items == ()
    assert tooling.close is not None
    await tooling.close()
    assert close_agent_mock.call_args_list[-1].args == (second_agent,)


@pytest.mark.asyncio
async def test_cascaded_responder_waits_for_interrupted_playout_settlement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The next Agno run cannot race a whole-session interrupted-history rewrite."""
    config = _config()
    runtime_paths = test_runtime_paths(tmp_path)
    started = Event()
    release = Event()
    ai_prompts: list[str] = []
    persisted: list[str] = []

    class ToolSupport:
        def build_context(self, target: MessageTarget, **_kwargs: object) -> ToolRuntimeContext:
            return _runtime_context(config=config, runtime_paths=runtime_paths, target=target)

        def build_execution_identity(self, **_kwargs: object) -> SimpleNamespace:
            return SimpleNamespace()

        async def run_in_context(
            self,
            *,
            tool_context: ToolRuntimeContext,
            operation: Callable[[], Awaitable[str]],
        ) -> str:
            assert tool_context.tool_function_filter is not None
            return await operation()

    async def fake_ai_response(_turn: ResponseTurnContext, **kwargs: object) -> str:
        prompt = cast("str", kwargs["prompt"])
        ai_prompts.append(prompt)
        recorder = kwargs["turn_recorder"]
        recorder.record_completed(  # type: ignore[union-attr]
            run_metadata={},
            assistant_text=f"answer to {prompt}",
            completed_tools=(),
        )
        return f"answer to {prompt}"

    def persist_interrupted(**kwargs: object) -> None:
        started.set()
        assert release.wait(timeout=5)
        persisted.append(cast("str", kwargs["run_id"]))

    monkeypatch.setattr(
        "mindroom.matrix_rtc.call_tools.resolve_agent_knowledge_access",
        lambda *_args, **_kwargs: SimpleNamespace(knowledge=None, unavailable={}),
    )
    monkeypatch.setattr(
        "mindroom.matrix_rtc.call_tools.create_agent",
        MagicMock(return_value=SimpleNamespace(additional_context="base context")),
    )
    monkeypatch.setattr("mindroom.ai.ai_response", fake_ai_response)
    monkeypatch.setattr(
        "mindroom.matrix_rtc.call_tools.open_resolved_scope_session_context",
        lambda **_kwargs: nullcontext(SimpleNamespace()),
    )
    monkeypatch.setattr("mindroom.matrix_rtc.call_tools.persist_interrupted_replay", persist_interrupted)
    tooling = await build_call_tools(
        agent_name=AGENT,
        config=config,
        runtime_paths=runtime_paths,
        tool_support=ToolSupport(),  # type: ignore[arg-type]
        room_id="!room:example.org",
        requester_id=REQUESTER,
        enable_responder=True,
    )
    assert tooling.responder is not None
    assert tooling.finalize_spoken_response is not None

    first = await tooling.responder("first", None)
    settlement = tooling.finalize_spoken_response(first.turn_id, "partial", True)
    assert settlement is not None
    assert await asyncio.to_thread(started.wait, 5)
    second_task = asyncio.create_task(tooling.responder("second", None))
    try:
        await asyncio.sleep(0.05)
        assert ai_prompts == ["first"]
        assert not second_task.done()
    finally:
        release.set()
    await settlement
    second = await second_task
    assert tooling.finalize_spoken_response(second.turn_id, second.text, False) is None
    assert ai_prompts == ["first", "second"]
    assert len(persisted) == 1


@pytest.mark.asyncio
async def test_build_call_tools_includes_async_only_toolkit_functions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Async-only Agno toolkit registrations are exposed to the realtime model."""

    async def async_add(a: int, b: int) -> int:
        return a + b

    function = _function(async_add)

    monkeypatch.setattr(
        "mindroom.matrix_rtc.call_tools.create_agent",
        lambda *_args, **_kwargs: FakeAgnoAgent([function]),
    )
    config = _config()
    runtime_paths = test_runtime_paths(tmp_path)
    tool_support = SimpleNamespace(
        build_context=lambda target, **_kwargs: _runtime_context(
            config=config,
            runtime_paths=runtime_paths,
            target=target,
        ),
        build_execution_identity=lambda **_k: SimpleNamespace(),
    )

    tooling = await build_call_tools(
        agent_name=AGENT,
        config=config,
        runtime_paths=runtime_paths,
        tool_support=tool_support,  # type: ignore[arg-type]
        room_id="!room:example.org",
        requester_id=REQUESTER,
    )

    assert len(tooling.tools) == 1


@pytest.mark.asyncio
async def test_build_call_tools_includes_agno_added_knowledge_and_skill_functions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Knowledge and skill tools from Agno's effective surface reach realtime."""
    functions = [
        _function(lambda: "knowledge", name="search_knowledge_base"),
        _function(lambda: "skill", name="get_skill_instructions"),
    ]
    monkeypatch.setattr(
        "mindroom.matrix_rtc.call_tools.create_agent",
        lambda *_args, **_kwargs: FakeAgnoAgent(functions),
    )
    monkeypatch.setattr(
        "mindroom.matrix_rtc.call_tools.resolve_agent_knowledge_access",
        lambda *_args, **_kwargs: SimpleNamespace(knowledge=None),
    )
    monkeypatch.setattr(
        "mindroom.matrix_rtc.call_tools._wrap_agno_function",
        lambda function, **_kwargs: function.name,
    )
    config = _config()
    runtime_paths = test_runtime_paths(tmp_path)
    tool_support = SimpleNamespace(
        build_context=lambda target, **_kwargs: _runtime_context(
            config=config,
            runtime_paths=runtime_paths,
            target=target,
        ),
        build_execution_identity=lambda **_kwargs: SimpleNamespace(),
    )

    tooling = await build_call_tools(
        agent_name=AGENT,
        config=config,
        runtime_paths=runtime_paths,
        tool_support=tool_support,  # type: ignore[arg-type]
        room_id="!room:example.org",
        requester_id=REQUESTER,
    )

    assert tooling.tools == ("search_knowledge_base", "get_skill_instructions")


@pytest.mark.asyncio
async def test_build_call_tools_hides_agno_added_knowledge_function_needing_approval(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Call approval policy reaches knowledge functions Agno adds after agent construction."""
    knowledge = Knowledge(name="docs")

    def create_knowledge_agent(*_args: object, **kwargs: object) -> KnowledgeToolDescribingAgent:
        agent = KnowledgeToolDescribingAgent(name="Helper", id=AGENT, knowledge=knowledge, search_knowledge=True)
        agent.tool_function_filter = cast("Callable[[Function], bool]", kwargs["tool_function_filter"])
        monkeypatch.setattr(
            agent,
            "aget_system_message",
            AsyncMock(return_value=SimpleNamespace(content="THE CHAT SYSTEM PROMPT")),
        )
        return agent

    monkeypatch.setattr("mindroom.matrix_rtc.call_tools.create_agent", create_knowledge_agent)
    monkeypatch.setattr(
        "mindroom.matrix_rtc.call_tools.resolve_agent_knowledge_access",
        lambda *_args, **_kwargs: SimpleNamespace(knowledge=knowledge),
    )
    monkeypatch.setattr(
        "mindroom.matrix_rtc.call_tools._wrap_agno_function",
        lambda function, **_kwargs: function.name,
    )
    config = _config()
    config.tool_approval = ToolApprovalConfig(
        rules=[ApprovalRuleConfig(match="search_knowledge_base", action="require_approval")],
    )
    runtime_paths = test_runtime_paths(tmp_path)
    tool_support = SimpleNamespace(
        build_context=lambda target, **_kwargs: _runtime_context(
            config=config,
            runtime_paths=runtime_paths,
            target=target,
        ),
        build_execution_identity=lambda **_kwargs: SimpleNamespace(),
    )

    tooling = await build_call_tools(
        agent_name=AGENT,
        config=config,
        runtime_paths=runtime_paths,
        tool_support=tool_support,  # type: ignore[arg-type]
        room_id="!room:example.org",
        requester_id=REQUESTER,
    )

    assert tooling.tools == ()


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
    config = _config()
    config.tool_approval = ToolApprovalConfig(
        rules=[ApprovalRuleConfig(match="policy_approval", action="require_approval")],
    )
    monkeypatch.setattr(
        "mindroom.matrix_rtc.call_tools.create_agent",
        lambda *_a, **_k: FakeAgnoAgent(list(functions.values())),
    )
    monkeypatch.setattr(
        "mindroom.matrix_rtc.call_tools.resolve_agent_knowledge_access",
        lambda *_a, **_k: SimpleNamespace(knowledge=None),
    )
    monkeypatch.setattr(
        "mindroom.matrix_rtc.call_tools._wrap_agno_function",
        lambda function, **_kwargs: function.name,
    )
    runtime_paths = test_runtime_paths(tmp_path)
    tool_support = SimpleNamespace(
        build_context=lambda target, **_kwargs: _runtime_context(
            config=config,
            runtime_paths=runtime_paths,
            target=target,
        ),
        build_execution_identity=lambda **_k: SimpleNamespace(),
    )

    tooling = await build_call_tools(
        agent_name=AGENT,
        config=config,
        runtime_paths=runtime_paths,
        tool_support=tool_support,  # type: ignore[arg-type]
        room_id="!room:example.org",
        requester_id=REQUESTER,
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
            requester_id=REQUESTER,
        )

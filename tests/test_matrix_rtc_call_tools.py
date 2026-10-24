"""Tests for materializing a MindRoom agent's tools and prompt for realtime and cascaded voice calls."""

from __future__ import annotations

import asyncio
import inspect
import threading
from contextlib import asynccontextmanager
from dataclasses import replace
from threading import Event
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from agno.tools.function import Function
from agno.tools.toolkit import Toolkit

from mindroom import agents
from mindroom.agent_loop.build import AgentBuild, AgentSurface, prompt_for_turn
from mindroom.agent_loop.prompt import PromptSpec
from mindroom.agent_loop.spec import AgentSpec
from mindroom.agent_loop.tools import prepare_run_tools
from mindroom.agents import create_agent
from mindroom.approval.tool_approval import JOB_APPROVAL_TYPE, POLICY_CONFIRMATION_APPROVAL_TYPE
from mindroom.config.agent import AgentConfig, AgentPrivateConfig
from mindroom.config.approval import ApprovalRuleConfig, ToolApprovalConfig
from mindroom.config.budgets import BudgetsConfig
from mindroom.config.main import Config
from mindroom.config.models import BackgroundToolJobsConfig, ModelConfig, ModelPricing
from mindroom.constants import AI_RUN_METADATA_KEY
from mindroom.helper_usage import HelperUsageOwner, get_helper_usage_owner, record_helper_usage
from mindroom.history.turn_recorder import TurnRecorder
from mindroom.history.types import HistoryPolicy, HistoryScope, ResolvedHistorySettings
from mindroom.hooks import EnrichmentItem
from mindroom.knowledge.availability import KnowledgeAvailability
from mindroom.knowledge.search import AgentKnowledge, KnowledgeSource, KnowledgeSourceDescription
from mindroom.knowledge.utils import KnowledgeAvailabilityDetail
from mindroom.llm.provider import ModelIdentity, ModelRequest
from mindroom.llm.runs import RunRecord, RunStatus
from mindroom.llm.usage import ModelMetrics, RunMetrics, TokenCounts
from mindroom.matrix_rtc.call_tools import (
    _TEXT_CHAT_REQUIRED_MESSAGE,
    CallAgentTooling,
    _call_agent_run_state,
    _CallAgentCache,
    _CallAgentRunState,
    _CallResponseTracker,
    _wrap_tool_function,
    build_call_tools,
    matrix_message_available_during_call,
)
from mindroom.matrix_rtc.voice_agent import CallAgentResponse
from mindroom.memory import MemoryPromptParts
from mindroom.runtime_resolution import resolve_agent_runtime
from mindroom.tool_jobs.execution import agent_tool_middleware
from mindroom.tool_system.agno_toolkits import adapt_function, adapt_toolkit
from mindroom.tool_system.call_record import recording_tool_calls
from mindroom.tool_system.events import ToolTraceEntry
from mindroom.tool_system.invocation import ToolCallContext, run_tool_call
from mindroom.tool_system.runtime_context import (
    ToolRuntimeContext,
    get_tool_runtime_context,
    tool_runtime_context,
)
from mindroom.tool_system.worker_routing import build_tool_execution_identity
from mindroom.usage.stats import collect_admin_usage
from tests.authorization_helpers import (
    make_test_tool_runtime_context,
)
from tests.budget_helpers import budget_monitor_with_spend, set_month_spend
from tests.conftest import bind_runtime_paths, make_conversation_reader_mock, make_relation_lookup, test_runtime_paths
from tests.fakes.scripted_model import ScriptedModel, answer

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
    from pathlib import Path

    from mindroom.message_target import MessageTarget
    from mindroom.response_turn import ResponseTurnContext
    from mindroom.tool_system.invocation import ToolCall, ToolMiddleware, ToolNext
    from mindroom.tool_system.tool_function import AdaptedToolkit, ToolFunction

AGENT = "helper"
REQUESTER = "@alice:example.org"
_USAGE_OWNER = HelperUsageOwner(storage_factory=MagicMock(), session_id="call")
_CALL_CONTEXT = ToolCallContext(run_id="call:voice", session_id="call", user_id=REQUESTER)
_EMPTY_SCHEMA = {"type": "object", "properties": {}}


@asynccontextmanager
async def _authorized_call_operation() -> AsyncIterator[bool]:
    """Model a live admitted caller for call-tool unit tests."""
    yield True


async def _unconnected() -> None:
    """Connect nothing, for a function whose toolkit holds no connection."""


def _ready_knowledge() -> AgentKnowledge:
    """Return an agent's knowledge handle over one published index."""
    source = KnowledgeSource(description=KnowledgeSourceDescription(base_id="docs", description=""), index=object())
    return AgentKnowledge(sources=(source,))


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
    return make_test_tool_runtime_context(
        agent_name=AGENT,
        target=target,
        requester_id=REQUESTER,
        client=MagicMock(),
        config=config,
        runtime_paths=runtime_paths,  # type: ignore[arg-type]
        relations=make_relation_lookup(),
        conversation_reader=make_conversation_reader_mock(),
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


def _toolkit(*functions: Function) -> Toolkit:
    toolkit = Toolkit(name="call_tools")
    toolkit.functions = {function.name: function for function in functions}
    return toolkit


def _build(*toolkits: Toolkit, policy: tuple[ToolMiddleware, ...] = (), role: str = "THE CHAT ROLE") -> AgentBuild:
    """Return an agent build offering ``toolkits`` under the tool-call ``policy``, as ``create_agent`` returns one."""
    return AgentBuild(
        spec=AgentSpec(
            id=AGENT,
            name="Helper",
            model=ScriptedModel([]),
            system_message=None,
            toolkits=tuple(adapt_toolkit(toolkit) for toolkit in toolkits),
        ),
        prompt=PromptSpec(role=role),
        request_template=ModelRequest(messages=()),
        history_settings=ResolvedHistorySettings(policy=HistoryPolicy(mode="all"), max_tool_calls_from_history=None),
        store=None,
        tool_owners={},
        surface=AgentSurface(mode="standard", cli_shell=False),
        tool_policy=policy,
    )


def _functions(build: AgentBuild) -> Mapping[str, ToolFunction]:
    return prepare_run_tools(build.spec).functions


def _wrap(function: Function, *, middleware: tuple[ToolMiddleware, ...] = ()):  # noqa: ANN202
    return _wrap_tool_function(
        adapt_function(function),
        middleware=middleware,
        call_context=_CALL_CONTEXT,
        context=_context(),  # type: ignore[arg-type]
        agent_name=AGENT,
        authorize_operation=_authorized_call_operation,
        usage_owner=_USAGE_OWNER,
        connect=_unconnected,
    )


def _tool_names(tools: tuple[Any, ...]) -> list[str]:
    return [tool.info.raw_schema["name"] for tool in tools]


def _ollama_config(runtime_paths: object, **agent: Any) -> Config:  # noqa: ANN401
    """Return a one-agent config on a local model whose builds never contact a provider."""
    return bind_runtime_paths(
        Config(
            agents={AGENT: AgentConfig(display_name="Helper", **agent)},
            models={"default": ModelConfig(provider="ollama", id="test")},
        ),
        runtime_paths,  # type: ignore[arg-type]
    )


def _identity(runtime_paths: object) -> object:
    return build_tool_execution_identity(
        channel="matrix",
        agent_name=AGENT,
        runtime_paths=runtime_paths,  # type: ignore[arg-type]
        requester_id=REQUESTER,
        room_id="!room:example.org",
        thread_id=None,
        resolved_thread_id=None,
        session_id="!room:example.org",
    )


def _tool_support(config: Config, runtime_paths: object) -> SimpleNamespace:
    """Return the call manager's tool support for the requester in one Matrix room."""
    return SimpleNamespace(
        build_context=lambda target, **_kwargs: _runtime_context(
            config=config,
            runtime_paths=runtime_paths,
            target=target,
        ),
        build_execution_identity=lambda **_kwargs: _identity(runtime_paths),
    )


@pytest.mark.asyncio
async def test_realtime_call_tool_does_not_run_after_authorization_is_revoked() -> None:
    """A realtime provider cannot invoke a tool after current call access is denied."""
    calls: list[dict[str, object]] = []

    async def entrypoint(a: int, b: int) -> str:
        calls.append({"a": a, "b": b})
        return "unexpected"

    @asynccontextmanager
    async def denied_operation() -> AsyncIterator[bool]:
        yield False

    wrapped = _wrap_tool_function(
        adapt_function(_function(entrypoint)),
        middleware=(),
        call_context=_CALL_CONTEXT,
        context=_context(),  # type: ignore[arg-type]
        agent_name=AGENT,
        authorize_operation=denied_operation,
        usage_owner=_USAGE_OWNER,
        connect=_unconnected,
    )

    assert await wrapped({"a": 2, "b": 3}) == "Call access was revoked before this tool could run."
    assert calls == []


@pytest.mark.parametrize("jobs", [False, True], ids=["paused-run", "job-approval"])
@pytest.mark.asyncio
async def test_call_agent_keeps_chat_tool_surface_and_refuses_text_chat_functions(
    tmp_path: Path,
    *,
    jobs: bool,
) -> None:
    """The cached call agent offers chat's functions and prompt, so calls reuse chat's prompt cache.

    A gated call is refused in a call whether chat would pause for it or ask as its job, before any other tool-call
    policy sees it, so no card is posted and nothing is recorded.
    """
    runtime_paths = test_runtime_paths(tmp_path)
    config = bind_runtime_paths(
        Config(
            agents={
                AGENT: AgentConfig(display_name="Helper", tools=["calculator"], delegate_to=["other"]),
                "other": AgentConfig(display_name="Other"),
            },
            models={"default": ModelConfig(provider="ollama", id="test")},
            tool_approval=ToolApprovalConfig(rules=[ApprovalRuleConfig(match="add", action="require_approval")]),
            background_tool_jobs=BackgroundToolJobsConfig(enabled=jobs),
        ),
        runtime_paths,
    )
    cache = _CallAgentCache(
        agent_name=AGENT,
        config=config,
        runtime_paths=runtime_paths,
        context=SimpleNamespace(hook_registry=None, tool_function_filter=None),  # type: ignore[arg-type]
        execution_identity=None,  # type: ignore[arg-type]
        active_model_name=None,
    )
    call_build = cache._build(knowledge=None, refresh_scheduler=None)
    chat_build = create_agent(AGENT, config, runtime_paths, None, supports_native_tool_approval=True)
    call_functions = _functions(call_build)
    chat_functions = _functions(chat_build)

    assert list(call_functions) == list(chat_functions)
    assert prompt_for_turn(call_build).content == prompt_for_turn(chat_build).content
    assert chat_functions["add"].approval_type == (JOB_APPROVAL_TYPE if jobs else POLICY_CONFIRMATION_APPROVAL_TYPE)
    add = call_functions["add"]
    assert (add.requires_confirmation, add.external_execution, add.approval_type) == (False, False, None)
    # Delegation runs the child turn directly in calls instead of pausing for chat's driver.
    run_subagent = call_functions["run_subagent"]
    assert (run_subagent.external_execution, run_subagent.external_execution_silent) == (False, False)

    started: list[str] = []

    class _Recorder:
        async def started(self, tool_name: str, _args: Mapping[str, object]) -> str:
            started.append(tool_name)
            return tool_name

        async def finished(self, *_args: object) -> None:
            return None

    with recording_tool_calls(_Recorder()):
        refused = await run_tool_call(
            add,
            {"a": 1, "b": 2},
            _CALL_CONTEXT,
            middleware=agent_tool_middleware(call_build),
        )
        subtracted = await run_tool_call(
            call_functions["subtract"],
            {"a": 3, "b": 1},
            _CALL_CONTEXT,
            middleware=agent_tool_middleware(call_build),
        )

    assert refused == _TEXT_CHAT_REQUIRED_MESSAGE
    assert '"result": 2' in str(subtracted)
    assert started == ["subtract"]


@pytest.mark.asyncio
async def test_call_agent_cache_build_cancellation_waits_for_the_build(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cancellation during a build waits for the accepted build to finish and keeps nothing it produced."""
    build_started = Event()
    release_build = Event()
    build_finished = Event()

    def _create_agent(*_args: object, **_kwargs: object) -> AgentBuild:
        build_started.set()
        if not release_build.wait(timeout=5):
            msg = "test did not release cached agent construction"
            raise TimeoutError(msg)
        build_finished.set()
        return _build()

    async def render(_build: AgentBuild) -> str:
        return "rendered"

    monkeypatch.setattr("mindroom.matrix_rtc.call_tools.create_scope_session_storage", MagicMock())
    monkeypatch.setattr("mindroom.matrix_rtc.call_tools.create_agent", _create_agent)
    cache = _CallAgentCache(
        agent_name=AGENT,
        config=_config(),
        runtime_paths=test_runtime_paths(tmp_path),
        context=SimpleNamespace(hook_registry=None, tool_function_filter=None),  # type: ignore[arg-type]
        execution_identity=SimpleNamespace(),  # type: ignore[arg-type]
        active_model_name="default",
    )

    run_task = asyncio.create_task(
        cache.run(
            knowledge=None,
            refresh_scheduler=None,
            active_model_name=cache.active_model_name,
            operation=render,
        ),
    )
    try:
        assert await asyncio.to_thread(build_started.wait, 5)
        run_task.cancel()
        await asyncio.sleep(0)
        assert not run_task.done()

        release_build.set()
        with pytest.raises(asyncio.CancelledError):
            await run_task
    finally:
        release_build.set()
        await asyncio.gather(run_task, return_exceptions=True)

    assert build_finished.is_set()
    assert cache.build is None


@pytest.mark.asyncio
async def test_wrapped_tool_executes_sync_entrypoint_off_the_loop_in_context() -> None:
    """Sync entrypoints run in a worker thread with the call's runtime context bound."""
    seen: list[tuple[object, int]] = []

    def add(a: int, b: int) -> int:
        seen.append((get_tool_runtime_context(), threading.get_ident()))
        return a + b

    assert await _wrap(_function(add))({"a": 2, "b": 3}) == "5"
    assert seen
    assert seen[0][0] is not None
    assert seen[0][1] != threading.get_ident()


@pytest.mark.asyncio
async def test_wrapped_tool_executes_async_entrypoint() -> None:
    """Async entrypoints are awaited directly."""

    async def add(a: int, b: int) -> str:
        return f"sum={a + b}"

    assert await _wrap(_function(add))({"a": 1, "b": 1}) == "sum=2"


@pytest.mark.asyncio
async def test_wrapped_tool_runs_under_the_builds_tool_call_policy() -> None:
    """A voice tool call runs through the build's policy, which sees the call and may change its result."""
    calls: list[str] = []
    seen: list[tuple[str, dict[str, object], str | None]] = []

    def add(a: int, b: int) -> int:
        calls.append("tool")
        return a + b

    async def policy(call: ToolCall, call_next: ToolNext) -> object:
        seen.append((call.function.name, dict(call.arguments), call.context.user_id))
        return f"hooked={await call_next(call.arguments)}"

    assert await _wrap(_function(add), middleware=(policy,))({"a": 2, "b": 4}) == "hooked=6"
    assert calls == ["tool"]
    assert seen == [("add", {"a": 2, "b": 4}, REQUESTER)]


@pytest.mark.asyncio
async def test_wrapped_tool_does_not_run_when_the_policy_declines_it() -> None:
    """A policy that answers the call itself, such as a declining plugin hook, keeps the tool from running."""
    calls: list[object] = []

    def add(a: int, b: int) -> int:
        calls.append((a, b))
        return a + b

    async def decline(_call: ToolCall, _call_next: ToolNext) -> object:
        return "Tool approval is required; use the text chat."

    result = await _wrap(_function(add), middleware=(decline,))({"a": 1, "b": 2})

    assert "approval" in result.lower()
    assert calls == []


@pytest.mark.asyncio
async def test_wrapped_tool_reports_failures_to_the_model() -> None:
    """Tool exceptions come back as spoken-friendly error strings."""

    def boom() -> None:
        msg = "database on fire"
        raise RuntimeError(msg)

    result = await _wrap(_function(boom, parameters=_EMPTY_SCHEMA))({})
    assert "failed" in result
    assert "database on fire" in result


def _add_numbers(a: int, b: int) -> int:
    """Add two numbers.

    Args:
        a: First number.
        b: Second number.

    """
    return a + b


def _ping() -> str:
    """Check that the service answers."""
    return "pong"


def _mcp_function(parameters: dict[str, Any]) -> Function:
    """Return a function built the way the MCP toolkit builds one: the server's schema, used as is."""

    async def call_tool(**_kwargs: object) -> str:
        return "ok"

    return Function(
        name="docs_search",
        description="Search the docs.",
        parameters=parameters,
        entrypoint=call_tool,
        skip_entrypoint_processing=True,
    )


_SEARCH_SCHEMA = {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}


@pytest.mark.parametrize(
    ("function", "closed"),
    [
        pytest.param(Function(name="add", entrypoint=_add_numbers), True, id="processed-with-arguments"),
        pytest.param(Function(name="ping", entrypoint=_ping), False, id="processed-without-arguments"),
        pytest.param(_mcp_function(_SEARCH_SCHEMA), False, id="mcp-schema"),
    ],
)
def test_realtime_schemas_match_agnos_second_processing_pass(function: Function, *, closed: bool) -> None:
    """Agno's second pass closes a schema it produced from an entrypoint; an MCP tool's schema reaches the model as sent."""
    twice_processed = function.model_copy(deep=True)
    twice_processed.process_entrypoint()
    twice_processed.process_entrypoint()

    parameters = _wrap(function).info.raw_schema["parameters"]

    assert parameters == twice_processed.parameters
    assert ("additionalProperties" in parameters) is closed
    if function.skip_entrypoint_processing:
        assert parameters == _SEARCH_SCHEMA


def test_realtime_schemas_send_an_empty_schema_as_an_object_without_properties() -> None:
    """A tool that supplies an empty schema reaches the realtime model as an object without properties."""
    assert _wrap(_mcp_function({})).info.raw_schema["parameters"] == {"type": "object", "properties": {}}


@pytest.mark.asyncio
async def test_call_responder_prepares_full_requester_prompt_before_first_turn(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Live must receive every context file before delegating, without crossing caller scopes."""
    runtime_paths = test_runtime_paths(tmp_path)
    config = bind_runtime_paths(
        Config(
            agents={
                AGENT: AgentConfig(
                    display_name="Helper",
                    instructions=["Keep the caller's preferences."],
                    private=AgentPrivateConfig(per="user_agent", context_files=["USER.md", "SOUL.md"]),
                ),
            },
            models={"default": ModelConfig(provider="ollama", id="test")},
        ),
        runtime_paths,
    )

    async def respond(_turn: ResponseTurnContext, **kwargs: object) -> str:
        return cast("str", prompt_for_turn(cast("AgentBuild", kwargs["reusable_build"])).content)

    monkeypatch.setattr("mindroom.ai.ai_response", respond)
    for requester, name, other in (("@alice:example.org", "Alice", "Bob"), ("@bob:example.org", "Bob", "Alice")):
        identity = build_tool_execution_identity(
            channel="matrix",
            agent_name=AGENT,
            runtime_paths=runtime_paths,
            requester_id=requester,
            room_id="!call:example.org",
            thread_id=None,
            resolved_thread_id=None,
            session_id=requester,
        )
        workspace = resolve_agent_runtime(
            AGENT,
            config,
            runtime_paths,
            execution_identity=identity,
            create=True,
        ).workspace
        assert workspace is not None
        (workspace.root / "USER.md").write_text(f"The caller is {name}.")
        (workspace.root / "SOUL.md").write_text("Use a warm, concise voice.")
        support = SimpleNamespace(
            build_context=lambda target, *, user_id, **_kwargs: make_test_tool_runtime_context(
                agent_name=AGENT,
                target=target,
                requester_id=user_id,
                client=MagicMock(),
                config=config,
                runtime_paths=runtime_paths,
                relations=make_relation_lookup(),
                conversation_reader=make_conversation_reader_mock(),
                hook_registry=MagicMock(),
            ),
            build_execution_identity=lambda identity=identity, **_kwargs: identity,
            run_in_context=lambda *, operation, **_kwargs: operation(),
        )
        tooling = await build_call_tools(
            agent_name=AGENT,
            config=config,
            runtime_paths=runtime_paths,
            tool_support=support,
            room_id="!call:example.org",
            requester_id=requester,
            session_id=requester,
            authorize_operation=_authorized_call_operation,
            enable_responder=True,
            reconcile_spoken_response=False,
        )
        assert tooling.close is not None
        try:
            assert tooling.get_system_prompt is not None
            prompt = await tooling.get_system_prompt()
            assert f"The caller is {name}." in prompt
            assert f"The caller is {other}." not in prompt
            assert "Use a warm, concise voice." in prompt
            assert "Keep the caller's preferences." in prompt
            assert tooling.tools == ()
            # The first delegated turn reuses the prepared build and its context snapshot.
            (workspace.root / "USER.md").write_text("This file changed after the call started.")
            assert tooling.responder is not None
            response = await tooling.responder("Who am I?", None)
            assert f"The caller is {name}." in response.text
            assert "This file changed" not in response.text
        finally:
            await tooling.close()


@pytest.mark.asyncio
async def test_build_call_tools_returns_same_agent_prompt_and_tools(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The bridge materializes the chat agent's functions and system prompt for the call's requester."""
    bound_filters: list[object] = []
    call_contexts: list[ToolCallContext] = []

    def add(a: int, b: int) -> int:
        runtime_context = get_tool_runtime_context()
        bound_filters.append(runtime_context.tool_function_filter if runtime_context is not None else None)
        return a + b

    async def record_call(call: ToolCall, call_next: ToolNext) -> object:
        call_contexts.append(call.context)
        return await call_next(call.arguments)

    built = _build(_toolkit(_function(add)), policy=(record_call,))
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

    def fake_create_agent(*args: object, **kwargs: object) -> AgentBuild:
        # Bind the real signature so a stale keyword fails here instead of in production.
        inspect.signature(create_agent).bind(*args, **kwargs)
        create_calls.append(args)
        create_kwargs.update(kwargs)
        return built

    monkeypatch.setattr("mindroom.matrix_rtc.call_tools.create_agent", fake_create_agent)

    async def fake_resolve_knowledge(*_args: object, **kwargs: object) -> SimpleNamespace:
        knowledge_calls.append(kwargs)
        return SimpleNamespace(knowledge=knowledge)

    monkeypatch.setattr(
        "mindroom.matrix_rtc.call_tools.resolve_agent_knowledge_access_async",
        fake_resolve_knowledge,
    )
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
            assert active_model_name == "default"
            seen_targets.append(target)
            return _runtime_context(
                config=config,
                runtime_paths=runtime_paths,
                target=target,
                hook_registry=hook_registry,
                orchestrator=SimpleNamespace(knowledge_refresh_scheduler=refresh_scheduler, budgets=None),
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
        authorize_operation=_authorized_call_operation,
    )
    assert _tool_names(tooling.tools) == ["add"]
    assert tooling.instructions == "<your_role>\nTHE CHAT ROLE\n</your_role>"
    assert create_kwargs["eager_deferred_tools"] is True
    assert create_kwargs["include_interactive_questions"] is False
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
    assert await tooling.tools[0]({"a": 2, "b": 3}) == "5"
    assert bound_filters[0] is create_kwargs["tool_function_filter"]
    [call_context] = call_contexts
    assert (call_context.user_id, call_context.session_id) == (REQUESTER, "!room:example.org")


@pytest.mark.asyncio
@pytest.mark.parametrize("reconcile_spoken_response", [True, False], ids=["cascaded", "live"])
async def test_call_responder_uses_normal_agent_turn_and_refuses_unsafe_functions(  # noqa: C901, PLR0915
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    reconcile_spoken_response: bool,
) -> None:
    """Cascaded transcripts reuse ai_response with full identity and a function-level call policy."""
    config = _config()
    config.tool_approval = ToolApprovalConfig(
        rules=[ApprovalRuleConfig(match="policy_approval", action="require_approval")],
    )
    knowledge = _ready_knowledge()
    refresh_scheduler = object()
    execution_identity = SimpleNamespace()
    calls: list[tuple[ResponseTurnContext, dict[str, object]]] = []
    persisted_interruptions: list[dict[str, object]] = []
    completed_tools = [
        ToolTraceEntry(type="tool_call_completed", tool_name="weather"),
        ToolTraceEntry(type="tool_call_completed", tool_name="weather"),
    ]
    contexts: list[ToolRuntimeContext] = []
    turn_contexts: list[ToolRuntimeContext] = []
    recorded_tool_uses: list[list[str]] = []
    runtime_paths = test_runtime_paths(tmp_path)
    authorization_allowed = True

    @asynccontextmanager
    async def authorize_operation() -> AsyncIterator[bool]:
        yield authorization_allowed

    class StrictToolSupport:
        def build_context(
            self,
            target: MessageTarget,
            *,
            user_id: str | None,
            agent_name: str | None = None,
            active_model_name: str | None = None,
        ) -> ToolRuntimeContext:
            context = make_test_tool_runtime_context(
                agent_name=agent_name or AGENT,
                target=target,
                requester_id=user_id or REQUESTER,
                client=MagicMock(),
                config=config,
                runtime_paths=runtime_paths,
                relations=make_relation_lookup(),
                conversation_reader=make_conversation_reader_mock(),
                hook_registry=MagicMock(),
                orchestrator=SimpleNamespace(knowledge_refresh_scheduler=refresh_scheduler, budgets=None),  # type: ignore[arg-type]
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
            turn_contexts.append(tool_context)
            return await operation()

    async def fake_ai_response(turn: ResponseTurnContext, **kwargs: object) -> str:
        calls.append((turn, kwargs))
        on_attempt_start = cast("Callable[[str, str], None]", kwargs["on_attempt_start"])
        on_attempt_start("call-run-1", "default")
        recorder = kwargs["turn_recorder"]
        recorder.record_completed(  # type: ignore[union-attr]
            run_metadata={"model": "same-chat-model"},
            assistant_text="It is sunny.",
            completed_tools=completed_tools,
        )
        return "It is sunny."

    confirm = _function(lambda: "confirm", _EMPTY_SCHEMA, name="confirm")
    confirm.requires_confirmation = True
    cached_build = _build(
        _toolkit(
            _function(lambda: "safe", _EMPTY_SCHEMA, name="safe"),
            confirm,
            _function(lambda: "policy", _EMPTY_SCHEMA, name="policy_approval"),
            _function(lambda: "workflow", _EMPTY_SCHEMA, name="run_workflow"),
        ),
    )
    create_agent_mock = MagicMock(return_value=cached_build)
    history_storage = MagicMock()
    create_scope_session_storage_mock = MagicMock(return_value=history_storage)
    monkeypatch.setattr("mindroom.matrix_rtc.call_tools.create_agent", create_agent_mock)
    monkeypatch.setattr(
        "mindroom.matrix_rtc.call_tools.create_scope_session_storage",
        create_scope_session_storage_mock,
    )
    monkeypatch.setattr(
        "mindroom.matrix_rtc.call_tools.resolve_agent_knowledge_access_async",
        AsyncMock(return_value=SimpleNamespace(knowledge=knowledge, unavailable=())),
    )
    monkeypatch.setattr("mindroom.ai.ai_response", fake_ai_response)
    monkeypatch.setattr(
        "mindroom.matrix_rtc.call_tools.load_scope_session",
        lambda **kwargs: SimpleNamespace(session_id=kwargs["session_id"]),
    )
    monkeypatch.setattr(
        "mindroom.matrix_rtc.call_tools.persist_interrupted_replay",
        lambda **kwargs: persisted_interruptions.append(kwargs),
    )
    if not reconcile_spoken_response:
        monkeypatch.setattr(
            "mindroom.matrix_rtc.call_tools._CallResponseTracker.register",
            MagicMock(side_effect=AssertionError("Live results must not wait for speech reconciliation")),
        )
    tooling = await build_call_tools(
        agent_name=AGENT,
        config=config,
        runtime_paths=runtime_paths,
        tool_support=StrictToolSupport(),  # type: ignore[arg-type]
        room_id="!room:example.org",
        requester_id=REQUESTER,
        authorize_operation=authorize_operation,
        session_id="!room:example.org:call:one",
        enable_responder=True,
        reconcile_spoken_response=reconcile_spoken_response,
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
    assert (response.turn_id is not None) == reconcile_spoken_response
    assert recorded_tool_uses == [["weather"]]
    authorization_allowed = False
    denied = await tooling.responder("Run another turn", recorded_tool_uses.append)
    assert denied == CallAgentResponse(text="")
    assert len(calls) == 1
    authorization_allowed = True
    create_agent_mock.assert_called_once()
    turn, kwargs = calls[0]
    assert turn.entity_label == AGENT
    assert turn.requester_id == REQUESTER
    assert turn.session_id == "!room:example.org:call:one"
    # No reply owns a call's outcomes afterwards, so its tool calls finish inside the turn.
    assert turn.inline_tools
    assert turn.active_model_name == "call_fast"
    assert contexts[0].active_model_name == "call_fast"
    assert turn.system_enrichment_items[0].text == "Speak briefly."
    assert kwargs["config"] is config
    assert kwargs["knowledge"] is knowledge
    assert kwargs["execution_identity"] is execution_identity
    assert kwargs["refresh_scheduler"] is refresh_scheduler
    assert "include_interactive_questions" not in kwargs
    assert "tool_function_filter" not in kwargs
    assert kwargs["show_tool_calls"] is False
    assert kwargs["eager_deferred_tools"] is True
    used_build = cast("AgentBuild", kwargs["reusable_build"])
    assert used_build.prompt is cached_build.prompt
    assert create_agent_mock.call_args.kwargs["history_storage"] is history_storage
    # The cached delegate keeps chat's tools and prompt, so calls reuse chat's prompt cache.
    assert "tool_function_filter" not in create_agent_mock.call_args.kwargs
    assert "include_interactive_questions" not in create_agent_mock.call_args.kwargs
    assert create_agent_mock.call_args.kwargs["supports_native_tool_approval"] is True
    assert "agent_cli_in_shell" not in create_agent_mock.call_args.kwargs
    used = _functions(used_build)
    cached = _functions(cached_build)
    assert list(used) == ["safe", "confirm", "policy_approval", "run_workflow"]
    assert used["safe"] is cached["safe"]
    assert await run_tool_call(used["safe"], {}, _CALL_CONTEXT, middleware=agent_tool_middleware(used_build)) == "safe"
    for name in ("confirm", "policy_approval", "run_workflow"):
        refused = used[name]
        assert refused.requires_confirmation is False
        assert refused.parameters == cached[name].parameters
        result = await run_tool_call(refused, {}, _CALL_CONTEXT, middleware=agent_tool_middleware(used_build))
        assert result == _TEXT_CHAT_REQUIRED_MESSAGE
    create_scope_session_storage_mock.assert_called_once_with(
        agent_name=AGENT,
        scope=HistoryScope(kind="agent", scope_id=AGENT),
        config=config,
        runtime_paths=runtime_paths,
        execution_identity=execution_identity,
    )
    if reconcile_spoken_response:
        assert tooling.finalize_spoken_response is not None
        finalize = tooling.finalize_spoken_response(response.turn_id, "It is", True)
        assert finalize is not None
        await finalize
    else:
        assert tooling.finalize_spoken_response is None
    assert persisted_interruptions == (
        [
            {
                "scope_context": SimpleNamespace(session_id="!room:example.org:call:one"),
                "run_id": "call-run-1",
                "user_message": "What is the weather?",
                "user_message_is_structured": False,
                "partial_text": "It is",
                "completed_tools": tuple(completed_tools),
                "interrupted_tools": (),
                "run_metadata": {"model": "same-chat-model"},
                "original_status": RunStatus.cancelled,
            },
        ]
        if reconcile_spoken_response
        else []
    )

    async def cancel_before_playout(_turn: ResponseTurnContext, **cancel_kwargs: object) -> str:
        on_attempt_start = cast("Callable[[str, str], None]", cancel_kwargs["on_attempt_start"])
        on_attempt_start("call-run-2", "default")
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
    if reconcile_spoken_response:
        assert error_response.turn_id is not None
        assert tooling.finalize_spoken_response is not None
        persist_error = tooling.finalize_spoken_response(error_response.turn_id, "provider failed", False)
        assert persist_error is not None
        await persist_error
    else:
        assert error_response.turn_id is None
    assert str(persisted_interruptions[-1]["run_id"]).startswith("!room:example.org:call:one:turn:")
    assert persisted_interruptions[-1]["partial_text"] == ("provider failed" if reconcile_spoken_response else "")
    assert persisted_interruptions[-1]["original_status"] is RunStatus.error

    # Delegated children and script runs still hide these functions through the turn's runtime context.
    tool_filter = cast("Callable[[Function], bool]", turn_contexts[0].tool_function_filter)
    confirm_again = _function(lambda: "confirm", _EMPTY_SCHEMA, name="confirm")
    confirm_again.requires_confirmation = True
    assert tool_filter(_function(lambda: "safe", _EMPTY_SCHEMA, name="safe")) is True
    assert tool_filter(confirm_again) is False
    assert tool_filter(_function(lambda: "policy", _EMPTY_SCHEMA, name="policy_approval")) is False
    assert tool_filter(_function(lambda: "workflow", _EMPTY_SCHEMA, name="run_workflow")) is False
    assert contexts[0].tool_function_filter is None
    assert tooling.close is not None
    await tooling.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("active_model_name", "expected_model_name", "expected_model_id", "expected_context_window"),
    [
        ("call_fast", "call_fast", "fast-model", 16_000),
        (None, "large", "large-model", 48_000),
    ],
)
async def test_cascaded_responder_records_effective_selected_model_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    active_model_name: str | None,
    expected_model_name: str,
    expected_model_id: str,
    expected_context_window: int,
) -> None:
    """Call and room model selection drive both the call's build and the turn's metadata."""
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
    execution_identity = build_tool_execution_identity(
        channel="matrix",
        agent_name=AGENT,
        runtime_paths=runtime_paths,
        requester_id=REQUESTER,
        room_id="!room:example.org",
        thread_id=None,
        resolved_thread_id=None,
        session_id="!room:example.org:call:metadata",
    )
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
            return make_test_tool_runtime_context(
                agent_name=agent_name or AGENT,
                target=target,
                requester_id=user_id or REQUESTER,
                client=MagicMock(),
                config=config,
                runtime_paths=runtime_paths,
                relations=make_relation_lookup(),
                conversation_reader=make_conversation_reader_mock(),
                hook_registry=MagicMock(),
                active_model_name=active_model_name,
            )

        def build_execution_identity(self, **_kwargs: object) -> object:
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

    def scripted_provider(_config: Config, _runtime_paths: object, model_name: str, _identity: object) -> ScriptedModel:
        model_id = config.models[model_name].id
        return ScriptedModel(
            [answer("It is sunny.")],
            identity=ModelIdentity(id=model_id, provider="OpenAI", provider_name="OpenAI"),
        )

    create_agent_mock = MagicMock(side_effect=create_agent)
    monkeypatch.setattr(agents, "_build_agent_model_provider", scripted_provider)
    monkeypatch.setattr("mindroom.matrix_rtc.call_tools.create_agent", create_agent_mock)
    monkeypatch.setattr("mindroom.ai.build_memory_prompt_parts", AsyncMock(return_value=MemoryPromptParts()))
    monkeypatch.setattr("mindroom.matrix.state.get_room_alias_from_id", lambda *_args: "lobby")
    monkeypatch.setattr(
        "mindroom.matrix_rtc.call_tools.resolve_agent_knowledge_access_async",
        AsyncMock(return_value=SimpleNamespace(knowledge=None, unavailable=())),
    )
    monkeypatch.setattr(
        "mindroom.matrix_rtc.call_tools.load_scope_session",
        lambda **_kwargs: SimpleNamespace(),
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
        authorize_operation=_authorized_call_operation,
        session_id="!room:example.org:call:metadata",
        enable_responder=True,
        active_model_name=active_model_name,
    )

    assert tooling.responder is not None
    response = await tooling.responder("What is the weather?", None)
    assert response.text == "It is sunny."
    assert response.turn_id is not None
    assert tooling.finalize_spoken_response is not None
    finalize = tooling.finalize_spoken_response(response.turn_id, "It is", True)
    assert finalize is not None
    await finalize

    assert create_agent_mock.call_args.kwargs["active_model_name"] == expected_model_name
    run_metadata = persisted_interruptions[0]["run_metadata"]
    assert isinstance(run_metadata, dict)
    payload = run_metadata[AI_RUN_METADATA_KEY]
    assert payload["model"]["config"] == expected_model_name
    assert payload["model"]["id"] == expected_model_id
    assert payload["context"]["window_tokens"] == expected_context_window


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
async def test_cascaded_cancellation_during_knowledge_resolution_persists_transcript(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cancellation during knowledge lookup must persist the admitted transcript."""
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
        session_id="call-session",
    )
    resolution_started = asyncio.Event()

    async def blocked_resolution(*_args: object, **_kwargs: object) -> SimpleNamespace:
        resolution_started.set()
        await asyncio.Event().wait()
        msg = "blocked resolution unexpectedly resumed"
        raise AssertionError(msg)

    persisted_interruptions: list[dict[str, object]] = []
    monkeypatch.setattr(
        "mindroom.matrix_rtc.call_tools.resolve_agent_knowledge_access_async",
        blocked_resolution,
    )
    monkeypatch.setattr(
        "mindroom.matrix_rtc.call_tools.load_scope_session",
        lambda **kwargs: SimpleNamespace(session_id=kwargs["session_id"]),
    )
    monkeypatch.setattr(
        "mindroom.matrix_rtc.call_tools.persist_interrupted_replay",
        lambda **kwargs: persisted_interruptions.append(kwargs),
    )
    tooling = await build_call_tools(
        agent_name=AGENT,
        config=config,
        runtime_paths=runtime_paths,
        tool_support=SimpleNamespace(
            build_context=lambda target, **_kwargs: _runtime_context(
                config=config,
                runtime_paths=runtime_paths,
                target=target,
            ),
            build_execution_identity=lambda **_kwargs: execution_identity,
        ),  # type: ignore[arg-type]
        room_id="!room:example.org",
        requester_id=REQUESTER,
        authorize_operation=_authorized_call_operation,
        session_id="call-session",
        enable_responder=True,
    )
    assert tooling.responder is not None

    response_task = asyncio.create_task(tooling.responder("Never speak this", None))
    await asyncio.wait_for(resolution_started.wait(), timeout=5)
    response_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await response_task

    assert len(persisted_interruptions) == 1
    persisted = persisted_interruptions[0]
    assert persisted["scope_context"] == SimpleNamespace(session_id="call-session")
    assert persisted["user_message"] == "Never speak this"
    assert persisted["partial_text"] == ""
    assert persisted["original_status"] is RunStatus.cancelled


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
async def test_cascaded_responder_uses_the_fallback_for_an_over_budget_caller(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A voice call must not keep an over-budget requester on a priced model."""
    config = Config(
        agents={AGENT: AgentConfig(display_name="Helper", model="large")},
        models={
            "large": ModelConfig(provider="openai", id="large-model", pricing=ModelPricing(input=5, output=30)),
            "cheap": ModelConfig(provider="openai", id="cheap-model"),
        },
        budgets=BudgetsConfig(fallback_model="cheap", monthly_limit_usd=0),
    )
    runtime_paths = test_runtime_paths(tmp_path)
    ai_calls: list[ResponseTurnContext] = []

    class ToolSupport:
        def build_context(self, target: MessageTarget, **kwargs: object) -> ToolRuntimeContext:
            context = _runtime_context(config=config, runtime_paths=runtime_paths, target=target)
            return replace(context, active_model_name=cast("str | None", kwargs.get("active_model_name")))

        def build_execution_identity(self, **_kwargs: object) -> SimpleNamespace:
            return SimpleNamespace()

        async def run_in_context(
            self,
            *,
            tool_context: ToolRuntimeContext,
            operation: Callable[[], Awaitable[str]],
        ) -> str:
            assert tool_context.active_model_name == "cheap"
            return await operation()

    async def resolve_knowledge(*_args: object, **_kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(knowledge=None, unavailable={})

    async def fake_ai_response(turn: ResponseTurnContext, **_kwargs: object) -> str:
        ai_calls.append(turn)
        return "answer"

    monkeypatch.setattr("mindroom.matrix_rtc.call_tools.resolve_agent_knowledge_access_async", resolve_knowledge)
    monkeypatch.setattr("mindroom.matrix_rtc.call_tools.create_agent", MagicMock(return_value=_build()))
    monkeypatch.setattr("mindroom.ai.ai_response", fake_ai_response)
    tooling = await build_call_tools(
        agent_name=AGENT,
        config=config,
        runtime_paths=runtime_paths,
        tool_support=ToolSupport(),  # type: ignore[arg-type]
        room_id="!room:example.org",
        requester_id=REQUESTER,
        authorize_operation=_authorized_call_operation,
        enable_responder=True,
    )
    assert tooling.responder is not None

    await tooling.responder("hello", None)

    assert [turn.active_model_name for turn in ai_calls] == ["cheap"]
    assert tooling.close is not None
    await tooling.close()


@pytest.mark.asyncio
async def test_cascaded_responder_switches_to_the_fallback_when_the_caller_crosses_the_cap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Each new voice reply must check the budget again, not keep the model from call setup."""
    config = Config(
        agents={AGENT: AgentConfig(display_name="Helper", model="large")},
        models={
            "large": ModelConfig(provider="openai", id="large-model", pricing=ModelPricing(input=5, output=30)),
            "cheap": ModelConfig(provider="openai", id="cheap-model"),
        },
        budgets=BudgetsConfig(fallback_model="cheap", monthly_limit_usd=10),
    )
    runtime_paths = test_runtime_paths(tmp_path)
    monitor = budget_monitor_with_spend(runtime_paths, {})
    orchestrator = SimpleNamespace(knowledge_refresh_scheduler=None, budgets=monitor)
    turn_models: list[str | None] = []

    class ToolSupport:
        def build_context(self, target: MessageTarget, **kwargs: object) -> ToolRuntimeContext:
            context = _runtime_context(
                config=config,
                runtime_paths=runtime_paths,
                target=target,
                orchestrator=orchestrator,
            )
            return replace(context, active_model_name=cast("str | None", kwargs.get("active_model_name")))

        def build_execution_identity(self, **_kwargs: object) -> SimpleNamespace:
            return SimpleNamespace()

        async def run_in_context(
            self,
            *,
            tool_context: ToolRuntimeContext,
            operation: Callable[[], Awaitable[str]],
        ) -> str:
            turn_models.append(tool_context.active_model_name)
            return await operation()

    async def resolve_knowledge(*_args: object, **_kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(knowledge=None, unavailable={})

    ai_models: list[str | None] = []

    async def fake_ai_response(turn: ResponseTurnContext, **_kwargs: object) -> str:
        ai_models.append(turn.active_model_name)
        return "answer"

    create_agent_mock = MagicMock(side_effect=lambda *_args, **_kwargs: _build())
    monkeypatch.setattr("mindroom.matrix_rtc.call_tools.resolve_agent_knowledge_access_async", resolve_knowledge)
    monkeypatch.setattr("mindroom.matrix_rtc.call_tools.create_agent", create_agent_mock)
    monkeypatch.setattr("mindroom.ai.ai_response", fake_ai_response)
    tooling = await build_call_tools(
        agent_name=AGENT,
        config=config,
        runtime_paths=runtime_paths,
        tool_support=ToolSupport(),  # type: ignore[arg-type]
        room_id="!room:example.org",
        requester_id=REQUESTER,
        authorize_operation=_authorized_call_operation,
        enable_responder=True,
    )
    assert tooling.responder is not None

    first = await tooling.responder("first", None)
    assert tooling.finalize_spoken_response is not None
    assert tooling.finalize_spoken_response(first.turn_id, first.text, False) is None
    set_month_spend(monitor, {REQUESTER: 12.0})
    await tooling.responder("second", None)

    assert ai_models == ["large", "cheap"]
    assert turn_models == ["large", "cheap"]
    assert [call.kwargs["active_model_name"] for call in create_agent_mock.call_args_list] == ["large", "cheap"]
    assert tooling.close is not None
    await tooling.close()


def _connected_build(log: list[str], **entrypoints: tuple[str, Callable[..., Awaitable[str]]]) -> AgentBuild:
    """Return a build offering one toolkit per name, each holding a connection whose connect and close ``log`` records.

    Each keyword names a toolkit and gives the name and entrypoint of its one function, which takes ``value``.
    """

    def connecting(toolkit: AdaptedToolkit) -> AdaptedToolkit:
        async def close() -> None:
            log.append(f"{toolkit.name} close")

        async def connect() -> Callable[[], Awaitable[None]]:
            log.append(f"{toolkit.name} connect")
            return close

        return replace(toolkit, connect=connect)

    schema = {"type": "object", "properties": {"value": {"type": "string"}}}
    toolkits = []
    for toolkit_name, (function_name, entrypoint) in entrypoints.items():
        toolkit = Toolkit(name=toolkit_name)
        toolkit.functions = {function_name: _function(entrypoint, schema, name=function_name)}
        toolkits.append(toolkit)
    build = _build(*toolkits)
    connected = tuple(connecting(adapted) for adapted in build.spec.toolkits)
    return replace(build, spec=replace(build.spec, toolkits=connected))


async def _call_tools_over(build: AgentBuild, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> CallAgentTooling:
    """Build a realtime call's tools for an agent whose ``create_agent`` returns ``build``."""
    config = _config()
    runtime_paths = test_runtime_paths(tmp_path)
    monkeypatch.setattr("mindroom.matrix_rtc.call_tools.create_agent", MagicMock(return_value=build))
    monkeypatch.setattr(
        "mindroom.matrix_rtc.call_tools.resolve_agent_knowledge_access_async",
        AsyncMock(return_value=SimpleNamespace(knowledge=None)),
    )
    tool_support = SimpleNamespace(
        build_context=lambda target, **_kwargs: _runtime_context(
            config=config,
            runtime_paths=runtime_paths,
            target=target,
        ),
        build_execution_identity=lambda **_kwargs: MagicMock(),
    )
    return await build_call_tools(
        agent_name=AGENT,
        config=config,
        runtime_paths=runtime_paths,
        tool_support=tool_support,  # type: ignore[arg-type]
        room_id="!room:example.org",
        requester_id=REQUESTER,
        authorize_operation=_authorized_call_operation,
    )


@pytest.mark.asyncio
async def test_realtime_call_tools_connect_their_toolkit_when_first_used_and_close_it_with_the_call(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A toolkit that holds a connection for one run is connected once for the call and closed when the call ends."""
    log: list[str] = []

    async def lookup(value: str) -> str:
        return f"found {value}"

    async def query(value: str) -> str:
        return f"rows for {value}"

    build = _connected_build(log, server=("lookup", lookup), database=("query", query))
    tooling = await _call_tools_over(build, tmp_path, monkeypatch)
    tools = dict(zip(_tool_names(tooling.tools), tooling.tools, strict=True))

    assert log == []
    assert [await tools["lookup"]({"value": value}) for value in ("a", "b")] == ["found a", "found b"]
    assert log == ["server connect"]
    assert tooling.close is not None
    await tooling.close()
    # A call the provider sends after the call ended connects nothing, even to a toolkit no call has used yet.
    assert await tools["query"]({"value": "x"}) == "rows for x"
    assert log == ["server connect", "server close"]


@pytest.mark.asyncio
async def test_a_realtime_tool_whose_toolkit_fails_to_connect_answers_with_its_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed connect is the call's own failure, answered as any failed tool call is, and the next call retries."""
    log: list[str] = []

    async def lookup(value: str) -> str:
        return f"found {value}"

    build = _connected_build(log, server=("lookup", lookup))
    [toolkit] = build.spec.toolkits
    connect = toolkit.connect
    assert connect is not None
    attempts: list[str] = []

    async def failing_once() -> Callable[[], Awaitable[None]] | None:
        attempts.append("connect")
        if len(attempts) == 1:
            msg = "server unreachable"
            raise ConnectionError(msg)
        return await connect()

    build = replace(build, spec=replace(build.spec, toolkits=(replace(toolkit, connect=failing_once),)))
    tooling = await _call_tools_over(build, tmp_path, monkeypatch)
    [tool] = tooling.tools

    assert await tool({"value": "a"}) == "Tool lookup failed: server unreachable"
    assert await tool({"value": "b"}) == "found b"
    assert log == ["server connect"]
    assert tooling.close is not None
    await tooling.close()


@pytest.mark.asyncio
async def test_a_realtime_call_connects_the_toolkit_of_the_function_it_offers_under_a_shared_name(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When two toolkits offer a function of one name, the call offers the first and connects only its toolkit."""
    log: list[str] = []

    async def lookup(value: str) -> str:
        return f"found {value}"

    async def shadowed_lookup(value: str) -> str:
        return f"shadowed {value}"

    build = _connected_build(log, server=("lookup", lookup), shadowed=("lookup", shadowed_lookup))
    tooling = await _call_tools_over(build, tmp_path, monkeypatch)
    tools = dict(zip(_tool_names(tooling.tools), tooling.tools, strict=True))

    assert await tools["lookup"]({"value": "a"}) == "found a"
    assert log == ["server connect"]
    assert tooling.close is not None
    await tooling.close()
    assert log == ["server connect", "server close"]


@pytest.mark.asyncio
async def test_a_realtime_tool_call_cancelled_while_connecting_leaves_the_connect_to_the_next_call(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A call interrupted during its toolkit's connect does not count as connecting it for the rest of the call."""
    log: list[str] = []

    async def lookup(value: str) -> str:
        return f"found {value}"

    build = _connected_build(log, server=("lookup", lookup))
    server = build.spec.toolkits[0]
    assert server.connect is not None
    connecting = asyncio.Event()

    async def interrupted() -> None:
        connecting.set()
        await asyncio.Event().wait()

    attempts = iter([interrupted, server.connect])

    async def connect() -> Callable[[], Awaitable[None]] | None:
        return await next(attempts)()

    build = replace(build, spec=replace(build.spec, toolkits=(replace(server, connect=connect),)))
    tooling = await _call_tools_over(build, tmp_path, monkeypatch)
    (tool,) = tooling.tools
    first = asyncio.create_task(tool({"value": "a"}))
    await asyncio.wait_for(connecting.wait(), timeout=5)
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first

    assert await tool({"value": "b"}) == "found b"
    assert log == ["server connect"]
    assert tooling.close is not None
    await tooling.close()
    assert log == ["server connect", "server close"]


@pytest.mark.asyncio
async def test_realtime_call_tools_use_the_callers_budget_and_count_helper_usage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Realtime tools such as claude_agent start on an over-budget caller's fallback and count toward the caller."""
    config = Config(
        agents={AGENT: AgentConfig(display_name="Helper", model="large")},
        models={
            "large": ModelConfig(provider="anthropic", id="claude-opus-5-5", pricing=ModelPricing(input=5, output=25)),
            "cheap": ModelConfig(provider="anthropic", id="claude-haiku-5-5"),
        },
        budgets=BudgetsConfig(fallback_model="cheap", monthly_limit_usd=0),
    )
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

    async def claude_send(prompt: str) -> str:
        # Like claude_agent, a paid helper tool records its usage under the bound owner.
        owner = get_helper_usage_owner()
        assert owner is not None
        model = ModelMetrics(
            tokens=TokenCounts(input_tokens=100, total_tokens=100),
            id="claude-haiku-5-5",
            provider="Anthropic",
        )
        await record_helper_usage(
            RunRecord(
                run_id="turn",
                metrics=RunMetrics(tokens=TokenCounts(input_tokens=100, total_tokens=100), details={"model": [model]}),
            ),
            owner=owner,
            invocation_id="turn",
            kind="claude_agent",
            requester_id=REQUESTER,
        )
        return f"done: {prompt}"

    create_agent_mock = MagicMock(
        return_value=_build(_toolkit(_function(claude_send, {"type": "object"}, name="claude_send"))),
    )
    monkeypatch.setattr("mindroom.matrix_rtc.call_tools.create_agent", create_agent_mock)
    monkeypatch.setattr(
        "mindroom.matrix_rtc.call_tools.resolve_agent_knowledge_access_async",
        AsyncMock(return_value=SimpleNamespace(knowledge=None)),
    )
    tool_support = SimpleNamespace(
        build_context=lambda target, **_kwargs: _runtime_context(
            config=config,
            runtime_paths=runtime_paths,
            target=target,
        ),
        build_execution_identity=lambda **_kwargs: execution_identity,
    )

    tooling = await build_call_tools(
        agent_name=AGENT,
        config=config,
        runtime_paths=runtime_paths,
        tool_support=tool_support,  # type: ignore[arg-type]
        room_id="!room:example.org",
        requester_id=REQUESTER,
        authorize_operation=_authorized_call_operation,
    )

    assert create_agent_mock.call_args.kwargs["active_model_name"] == "cheap"
    assert await tooling.tools[0]({"prompt": "fix it"}) == "done: fix it"
    report = collect_admin_usage(config=config, runtime_paths=runtime_paths)
    assert [(user.user_id, user.totals.input_tokens) for user in report.user_breakdown] == [(REQUESTER, 100)]


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
    orchestrator = SimpleNamespace(knowledge_refresh_scheduler=first_scheduler, budgets=None)
    execution_identity = SimpleNamespace()
    ready_knowledge = _ready_knowledge()
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

    async def resolve_knowledge(*_args: object, **kwargs: object) -> SimpleNamespace:
        resolver_calls.append(kwargs)
        return next(resolutions)

    async def fake_ai_response(turn: ResponseTurnContext, **kwargs: object) -> str:
        ai_calls.append((turn, kwargs))
        return f"answer-{len(ai_calls)}"

    first_build = _build(role="first base")
    second_build = _build(role="second base")
    create_agent_mock = MagicMock(side_effect=(first_build, second_build))
    monkeypatch.setattr(
        "mindroom.matrix_rtc.call_tools.resolve_agent_knowledge_access_async",
        resolve_knowledge,
    )
    monkeypatch.setattr("mindroom.matrix_rtc.call_tools.create_agent", create_agent_mock)
    monkeypatch.setattr("mindroom.ai.ai_response", fake_ai_response)
    tooling = await build_call_tools(
        agent_name=AGENT,
        config=config,
        runtime_paths=runtime_paths,
        tool_support=ToolSupport(),  # type: ignore[arg-type]
        room_id="!room:example.org",
        requester_id=REQUESTER,
        authorize_operation=_authorized_call_operation,
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
    assert cast("AgentBuild", ai_calls[0][1]["reusable_build"]).prompt is first_build.prompt
    assert cast("AgentBuild", ai_calls[1][1]["reusable_build"]).prompt is second_build.prompt
    assert create_agent_mock.call_count == 2
    assert [item.key for item in ai_calls[0][0].system_enrichment_items] == ["voice_call"]
    assert [item.key for item in ai_calls[0][0].transient_enrichment_items] == ["knowledge_availability"]
    assert [item.key for item in ai_calls[1][0].system_enrichment_items] == ["voice_call"]
    assert ai_calls[1][0].transient_enrichment_items == ()
    assert tooling.close is not None
    await tooling.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("origin_brief", "expected_origin_items"),
    [
        (None, ()),
        ("brief text", (EnrichmentItem(key="call_origin", text="brief text", cache_policy="stable"),)),
    ],
)
async def test_call_responder_adds_origin_brief_enrichment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    origin_brief: str | None,
    expected_origin_items: tuple[EnrichmentItem, ...],
) -> None:
    """The delegate agent gets the call origin brief as a stable system enrichment item beside the voice rules."""
    config = _config()
    runtime_paths = test_runtime_paths(tmp_path)
    turns: list[ResponseTurnContext] = []

    async def fake_ai_response(turn: ResponseTurnContext, **_kwargs: object) -> str:
        turns.append(turn)
        return "answer"

    monkeypatch.setattr(
        "mindroom.matrix_rtc.call_tools.resolve_agent_knowledge_access_async",
        AsyncMock(return_value=SimpleNamespace(knowledge=None, unavailable={})),
    )
    monkeypatch.setattr("mindroom.matrix_rtc.call_tools.create_agent", MagicMock(return_value=_build()))
    monkeypatch.setattr("mindroom.ai.ai_response", fake_ai_response)
    tooling = await build_call_tools(
        agent_name=AGENT,
        config=config,
        runtime_paths=runtime_paths,
        tool_support=SimpleNamespace(
            build_context=lambda target, **_kwargs: _runtime_context(
                config=config,
                runtime_paths=runtime_paths,
                target=target,
            ),
            build_execution_identity=lambda **_kwargs: SimpleNamespace(),
            run_in_context=lambda **kwargs: kwargs["operation"](),
        ),  # type: ignore[arg-type]
        room_id="!room:example.org",
        requester_id=REQUESTER,
        authorize_operation=_authorized_call_operation,
        enable_responder=True,
        voice_instructions="Speak briefly.",
        origin_brief=origin_brief,
    )
    assert tooling.responder is not None

    await tooling.responder("What did we decide?", None)

    assert turns[0].system_enrichment_items == (
        EnrichmentItem(key="voice_call", text="Speak briefly.", cache_policy="stable"),
        *expected_origin_items,
    )


@pytest.mark.asyncio
async def test_cascaded_responder_waits_for_interrupted_playout_settlement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The next agent run cannot race a whole-session interrupted-history rewrite."""
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
        "mindroom.matrix_rtc.call_tools.resolve_agent_knowledge_access_async",
        AsyncMock(return_value=SimpleNamespace(knowledge=None, unavailable={})),
    )
    monkeypatch.setattr("mindroom.matrix_rtc.call_tools.create_agent", MagicMock(return_value=_build()))
    monkeypatch.setattr("mindroom.ai.ai_response", fake_ai_response)
    monkeypatch.setattr(
        "mindroom.matrix_rtc.call_tools.load_scope_session",
        lambda **_kwargs: SimpleNamespace(),
    )
    monkeypatch.setattr("mindroom.matrix_rtc.call_tools.persist_interrupted_replay", persist_interrupted)
    tooling = await build_call_tools(
        agent_name=AGENT,
        config=config,
        runtime_paths=runtime_paths,
        tool_support=ToolSupport(),  # type: ignore[arg-type]
        room_id="!room:example.org",
        requester_id=REQUESTER,
        authorize_operation=_authorized_call_operation,
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
async def test_build_call_tools_offers_every_function_of_the_build_in_offer_order(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every function the build offers reaches realtime, async-only and generated ones included, in offer order."""

    async def async_add(a: int, b: int) -> int:
        return a + b

    async_only = Toolkit(name="async_tools")
    async_only.async_functions = {"async_add": _function(async_add, name="async_add")}
    generated = _toolkit(
        _function(lambda: "knowledge", _EMPTY_SCHEMA, name="search_knowledge_base"),
        _function(lambda: "skill", _EMPTY_SCHEMA, name="get_skill_instructions"),
    )
    monkeypatch.setattr(
        "mindroom.matrix_rtc.call_tools.create_agent",
        lambda *_args, **_kwargs: _build(async_only, generated),
    )
    monkeypatch.setattr(
        "mindroom.matrix_rtc.call_tools.resolve_agent_knowledge_access_async",
        AsyncMock(return_value=SimpleNamespace(knowledge=None)),
    )
    config = _config()
    runtime_paths = test_runtime_paths(tmp_path)

    tooling = await build_call_tools(
        agent_name=AGENT,
        config=config,
        runtime_paths=runtime_paths,
        tool_support=_tool_support(config, runtime_paths),  # type: ignore[arg-type]
        room_id="!room:example.org",
        requester_id=REQUESTER,
        authorize_operation=_authorized_call_operation,
    )

    assert _tool_names(tooling.tools) == ["async_add", "search_knowledge_base", "get_skill_instructions"]
    assert await tooling.tools[0]({"a": 1, "b": 2}) == "3"


@pytest.mark.asyncio
@pytest.mark.parametrize("gated", [False, True], ids=["ungated", "gated"])
async def test_build_call_tools_hides_the_knowledge_search_when_it_needs_approval(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    gated: bool,
) -> None:
    """Call approval policy reaches the knowledge search the build adds beside the configured toolkits."""
    runtime_paths = test_runtime_paths(tmp_path)
    config = _ollama_config(runtime_paths)
    if gated:
        config.tool_approval = ToolApprovalConfig(
            rules=[ApprovalRuleConfig(match="search_knowledge_base", action="require_approval")],
        )
    monkeypatch.setattr(
        "mindroom.matrix_rtc.call_tools.resolve_agent_knowledge_access_async",
        AsyncMock(return_value=SimpleNamespace(knowledge=_ready_knowledge())),
    )

    tooling = await build_call_tools(
        agent_name=AGENT,
        config=config,
        runtime_paths=runtime_paths,
        tool_support=_tool_support(config, runtime_paths),  # type: ignore[arg-type]
        room_id="!room:example.org",
        requester_id=REQUESTER,
        authorize_operation=_authorized_call_operation,
    )

    assert ("search_knowledge_base" in _tool_names(tooling.tools)) is not gated


@pytest.mark.asyncio
async def test_build_call_tools_hides_functions_needing_text_chat(tmp_path: Path) -> None:
    """A function an approval rule may gate never enters the voice schema; its toolkit's other functions do."""
    runtime_paths = test_runtime_paths(tmp_path)
    config = _ollama_config(runtime_paths, tools=["calculator"])
    config.tool_approval = ToolApprovalConfig(rules=[ApprovalRuleConfig(match="add", action="require_approval")])

    tooling = await build_call_tools(
        agent_name=AGENT,
        config=config,
        runtime_paths=runtime_paths,
        tool_support=_tool_support(config, runtime_paths),  # type: ignore[arg-type]
        room_id="!room:example.org",
        requester_id=REQUESTER,
        authorize_operation=_authorized_call_operation,
    )

    names = _tool_names(tooling.tools)
    assert "add" not in names
    assert "subtract" in names


@pytest.mark.asyncio
async def test_build_call_tools_hides_offered_functions_needing_text_chat(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Functions a build offers beside its filtered toolkits never enter the voice schema when they need text chat."""
    functions = {
        name: _function(lambda name=name: name, _EMPTY_SCHEMA, name=name)
        for name in ("safe", "confirm", "external", "agno_approval", "policy_approval", "run_workflow")
    }
    functions["confirm"].requires_confirmation = True
    functions["external"].external_execution = True
    functions["agno_approval"].approval_type = "required"
    config = _config()
    config.tool_approval = ToolApprovalConfig(
        rules=[ApprovalRuleConfig(match="policy_approval", action="require_approval")],
    )
    monkeypatch.setattr(
        "mindroom.matrix_rtc.call_tools.create_agent",
        lambda *_args, **_kwargs: _build(_toolkit(*functions.values())),
    )
    monkeypatch.setattr(
        "mindroom.matrix_rtc.call_tools.resolve_agent_knowledge_access_async",
        AsyncMock(return_value=SimpleNamespace(knowledge=None)),
    )
    runtime_paths = test_runtime_paths(tmp_path)

    tooling = await build_call_tools(
        agent_name=AGENT,
        config=config,
        runtime_paths=runtime_paths,
        tool_support=_tool_support(config, runtime_paths),  # type: ignore[arg-type]
        room_id="!room:example.org",
        requester_id=REQUESTER,
        authorize_operation=_authorized_call_operation,
    )

    assert _tool_names(tooling.tools) == ["safe"]


@pytest.mark.asyncio
async def test_build_call_tools_keeps_builtin_invite_router_under_approval_policy(tmp_path: Path) -> None:
    """Voice recovery must stay callable when global policy requires approval for everything else."""
    runtime_paths = test_runtime_paths(tmp_path)
    config = _ollama_config(runtime_paths, tools=["calculator"])
    config.tool_approval = ToolApprovalConfig(default="require_approval")

    tooling = await build_call_tools(
        agent_name=AGENT,
        config=config,
        runtime_paths=runtime_paths,
        tool_support=_tool_support(config, runtime_paths),  # type: ignore[arg-type]
        room_id="!room:example.org",
        requester_id=REQUESTER,
        authorize_operation=_authorized_call_operation,
    )

    assert _tool_names(tooling.tools) == ["invite_router"]


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
            authorize_operation=_authorized_call_operation,
        )


def test_matrix_message_available_during_call_needs_the_tool_without_approval() -> None:
    """Calls hide tools that may need approval, so only an unapproved matrix_message counts."""
    config = _config()
    assert not matrix_message_available_during_call(config, AGENT)
    config.agents[AGENT].tools = ["matrix_message"]
    assert matrix_message_available_during_call(config, AGENT)
    config.tool_approval = ToolApprovalConfig(
        rules=[ApprovalRuleConfig(match="matrix_message", action="require_approval")],
    )
    assert not matrix_message_available_during_call(config, AGENT)


def test_an_interrupted_voice_reply_drops_the_tools_its_fold_summarized() -> None:
    """A voice reply that compacted mid-turn is saved without the calls the summary holds."""
    recorder = TurnRecorder(user_message="question")
    recorder.set_run_metadata({"source": "voice"})
    recorder.completed_tools = [
        ToolTraceEntry(type="tool_call_completed", tool_name="search", tool_call_id="folded"),
        ToolTraceEntry(type="tool_call_completed", tool_name="search", tool_call_id="after"),
    ]
    recorder.record_folded_tool_calls({"folded"})
    recorder.mark_interrupted()

    state = _call_agent_run_state(recorder, session_id="session", fallback_run_id="run")

    assert state.run_metadata["source"] == "voice"
    assert [tool.tool_call_id for tool in state.completed_tools] == ["after"]

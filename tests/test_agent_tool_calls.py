"""Live Agent catalog and ordinary execution without provider requests."""

from __future__ import annotations

import asyncio
import threading
from collections.abc import AsyncIterator, Iterator  # noqa: TC003 - Agno resolves tool annotations
from dataclasses import replace
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import MagicMock

import pytest
from agno.agent import Agent  # noqa: TC002 - Agno resolves injected annotations at runtime
from agno.learn import LearningMachine
from agno.media import Image
from agno.models.message import Message
from agno.models.openai import OpenAIChat
from agno.run import RunContext
from agno.run.agent import RunOutput
from agno.session.agent import AgentSession
from agno.tools.function import Function, FunctionCall, ToolResult
from agno.tools.toolkit import Toolkit

from mindroom import approval_tools
from mindroom.agent_knowledge_descriptions import KnowledgeToolDescribingAgent
from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.message_target import MessageTarget
from mindroom.tool_system import agent_tool_calls
from mindroom.tool_system.runtime_context import get_tool_runtime_context
from mindroom.tool_system.tool_access import ToolKey
from mindroom.tool_system.worker_routing import get_tool_execution_identity
from tests.authorization_helpers import make_test_tool_runtime_context
from tests.conftest import make_conversation_reader_mock, make_relation_lookup, test_runtime_paths

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from pathlib import Path

    from agno.learn.stores.protocol import LearningStore


async def _catalog(tmp_path: Path, tools: list, **kwargs: Any) -> agent_tool_calls.PreparedAgentToolCatalog:  # noqa: ANN401
    agent = KnowledgeToolDescribingAgent(id="helper", model=OpenAIChat(), tools=tools, **kwargs)
    context = RunContext(run_id="run", session_id="session", session_state={})
    output = RunOutput(run_id="run", session_id="session", messages=[])
    session = AgentSession(session_id="session", agent_id="helper", session_data={})
    runtime = make_test_tool_runtime_context(
        agent_name="helper",
        target=MessageTarget("!room:test", "$thread", "$thread", "$event", "session"),
        requester_id="@alice:test",
        client=MagicMock(),
        config=Config(),
        runtime_paths=test_runtime_paths(tmp_path),
        relations=make_relation_lookup(),
        conversation_reader=make_conversation_reader_mock(),
    )
    catalog = agent_tool_calls.PreparedAgentToolCatalog(agent, context, output, session, runtime)
    processed = await agent.aget_tools(output, context, session)
    await catalog.prepare(processed)
    return catalog


async def _events(
    catalog: agent_tool_calls.PreparedAgentToolCatalog,
    toolkit: str,
    function: str,
    arguments: dict | None = None,
) -> list[agent_tool_calls.AgentToolCallEvent]:
    binding = await catalog.bind(ToolKey(toolkit, function))
    return [event async for event in agent_tool_calls.execute_agent_tool_call(binding, "inner-id", arguments or {})]


@pytest.mark.asyncio
async def test_prepared_call_preserves_real_owner_context_and_hooks(tmp_path: Path) -> None:
    """Execution retains owner identity and runs existing hooks once."""
    seen = []
    hooks = []

    def inspect_binding(agent: Agent, run_context: RunContext, fc: FunctionCall) -> str:
        seen.append((agent, run_context, get_tool_runtime_context(), fc))
        identity = get_tool_execution_identity()
        assert identity is not None
        assert identity.requester_id == "@alice:test"
        assert identity.session_id == "session"
        run_context.session_state["changed"] = True
        return "bound"

    async def hook(name: str, function_call: Callable[..., Awaitable[str]], arguments: dict) -> str:
        hooks.append(name)
        return await function_call(**arguments)

    toolkit = Toolkit(name="inspect", tools=[inspect_binding], instructions="Keep identity.")
    toolkit.get_async_functions()["inspect_binding"].tool_hooks = [hook]
    catalog = await _catalog(tmp_path, [toolkit])
    events = await _events(catalog, "inspect", "inspect_binding")
    assert [event.kind for event in events] == ["started", "completed"]
    assert events[-1].execution.result == "bound"
    assert seen[0][:3] == (catalog.agent, catalog.run_context, catalog.runtime_context)
    assert seen[0][0] is catalog.agent
    assert seen[0][1] is catalog.run_context
    assert seen[0][2] is catalog.runtime_context
    assert seen[0][3].call_id == "inner-id"
    assert hooks == ["inspect_binding"]
    assert catalog.run_context.session_state == {"changed": True}
    assert catalog.run_response.messages == []
    assert catalog.run_response.tools is None
    assert get_tool_runtime_context() is None
    descriptor = await catalog.describe(ToolKey("inspect", "inspect_binding"))
    assert descriptor.instructions == ("Keep identity.",)
    assert descriptor.input_schema["properties"] == {}


@pytest.mark.asyncio
async def test_sync_async_generators_media_and_generated_knowledge(tmp_path: Path) -> None:
    """Bound calls retain generators, media and generated knowledge closures."""

    def sync(value: int) -> int:
        return value + 1

    async def asynchronous(value: int) -> int:
        return value + 2

    def generator() -> Iterator[str]:
        assert get_tool_runtime_context() is not None
        yield "one"
        yield "two"

    async def async_generator(run_context: RunContext) -> AsyncIterator[str]:
        yield "three"
        assert get_tool_runtime_context() is not None
        run_context.session_state["streamed"] = True
        yield "four"

    def media() -> ToolResult:
        return ToolResult(content="picture", images=[Image(url="https://example.org/image.png")])

    async def retriever(agent: Agent, query: str, **_kwargs: object) -> list[dict]:
        return [{"content": f"Knowledge about {query} for {agent.id}"}]

    toolkit = Toolkit(name="ordinary", tools=[sync, asynchronous, generator, async_generator, media])
    catalog = await _catalog(tmp_path, [toolkit], knowledge_retriever=retriever, search_knowledge=True)
    for name, args, expected in [
        ("sync", {"value": 2}, "3"),
        ("asynchronous", {"value": 2}, "4"),
        ("generator", {}, "onetwo"),
        ("async_generator", {}, "threefour"),
    ]:
        events = await _events(catalog, "ordinary", name, args)
        assert events[-1].kind == "completed"
        assert events[-1].execution.result == expected
    events = await _events(catalog, "ordinary", "media")
    assert [event.kind for event in events] == ["started", "media", "completed"]
    assert events[1].media.images[0].url == "https://example.org/image.png"
    events = await _events(catalog, "agent", "search_knowledge_base", {"query": "binding"})
    assert "Knowledge about binding for helper" in events[-1].execution.result
    assert catalog.run_context.session_state["streamed"] is True


@pytest.mark.asyncio
async def test_validation_pause_continuation_and_call_budget(tmp_path: Path) -> None:
    """Invalid calls never run; pauses and continuations stay explicit."""
    seen = []

    def action(value: int) -> int:
        seen.append(value)
        return value

    function = Function.from_callable(action)
    function.owning_toolkit = "actions"
    catalog = await _catalog(tmp_path, [function], tool_call_limit=1)
    events = await _events(catalog, "actions", "action", {"value": "wrong"})
    assert events[-1].kind == "failed"
    assert seen == []
    events = await _events(catalog, "actions", "action", {"value": 1})
    assert events[-1].kind == "completed"
    events = await _events(catalog, "actions", "action", {"value": 2})
    assert events[-1].kind == "failed"
    assert seen == [1]

    for flag in ("requires_confirmation", "requires_user_input", "external_execution"):
        function = Function.from_callable(action)
        function.owning_toolkit = "actions"
        setattr(function, flag, True)
        catalog = await _catalog(tmp_path, [function])
        events = await _events(catalog, "actions", "action", {"value": 3})
        assert events[-1].kind == "waiting"
        assert events[-1].requirement.tool_execution.tool_args == {"value": 3}
    assert seen == [1]
    function = Function.from_callable(action)
    function.owning_toolkit = "actions"
    function.stop_after_tool_call = True
    catalog = await _catalog(tmp_path, [function])
    events = await _events(catalog, "actions", "action", {"value": 4})
    assert events[-1].kind == "continuation_required"
    assert events[-1].execution.result == "4"


@pytest.mark.asyncio
async def test_catalog_keeps_qualified_names_and_rejects_collisions(tmp_path: Path) -> None:
    """Qualification prevents Agno name flattening from losing capabilities."""

    def action() -> str:
        return "ok"

    catalog = await _catalog(tmp_path, [Toolkit(name="one", tools=[action]), Toolkit(name="two", tools=[action])])
    assert (await _events(catalog, "one", "action"))[-1].execution.result == "ok"
    assert (await _events(catalog, "two", "action"))[-1].execution.result == "ok"
    with pytest.raises(ValueError, match="Duplicate"):
        await _catalog(tmp_path, [Toolkit(name="one", tools=[action]), Toolkit(name="one", tools=[action])])
    with pytest.raises(ValueError, match="reserved"):
        await _catalog(tmp_path, [Toolkit(name="agent", tools=[action])])


@pytest.mark.asyncio
async def test_lazy_metadata_and_single_materialization(tmp_path: Path) -> None:
    """Discovery stays lazy and concurrent describe builds only once."""
    builds = []

    def action(value: int) -> int:
        return value

    async def materialize() -> Toolkit:
        builds.append(get_tool_runtime_context())
        await asyncio.sleep(0)
        return Toolkit(name="lazy", tools=[action], instructions="Actual lazy instructions")

    catalog = await _catalog(tmp_path, [])
    catalog.add_deferred(agent_tool_calls.DeferredAgentToolkit("lazy", "Lazy operations", materialize))
    assert catalog.metadata() == [{"toolkit": "lazy", "description": "Lazy operations", "deferred": True}]
    assert builds == []
    key = ToolKey("lazy", "action")
    first, second = await asyncio.gather(catalog.describe(key), catalog.describe(key))
    assert first == second
    assert first.instructions == ("Actual lazy instructions",)
    assert builds == [catalog.runtime_context]
    assert (await _events(catalog, "lazy", "action", {"value": 9}))[-1].execution.result == "9"


@pytest.mark.asyncio
async def test_closing_stream_stops_async_generator_before_catalog_release(tmp_path: Path) -> None:
    """Agno's background generator consumer cannot outlive its call owner."""
    closed = asyncio.Event()
    resumed = []
    blocker = asyncio.Event()

    async def streaming() -> AsyncIterator[str]:
        try:
            yield "first"
            await blocker.wait()
            resumed.append(True)
            yield "second"
        finally:
            assert get_tool_runtime_context() is not None
            closed.set()

    function = Function.from_callable(streaming)
    function.show_result = True
    function.owning_toolkit = "stream"
    catalog = await _catalog(tmp_path, [function])
    binding = await catalog.bind(ToolKey("stream", "streaming"))
    stream = agent_tool_calls.execute_agent_tool_call(binding, "stream-id", {})
    assert (await anext(stream)).kind == "started"
    assert (await anext(stream)).kind == "progress"
    await stream.aclose()
    was_closed = closed.is_set()
    blocker.set()
    await asyncio.sleep(0)
    assert was_closed
    assert resumed == []


@pytest.mark.asyncio
async def test_closed_catalog_rejects_later_bindings(tmp_path: Path) -> None:
    """Close is idempotent and fences every later binding."""

    def action() -> str:
        return "ok"

    catalog = await _catalog(tmp_path, [Toolkit(name="existing", tools=[action])])
    await catalog.close()
    await catalog.close()
    with pytest.raises(RuntimeError, match="closed"):
        await catalog.bind(ToolKey("existing", "action"))


@pytest.mark.asyncio
async def test_generated_history_uses_exact_live_session(tmp_path: Path) -> None:
    """Generated closure sees post-binding changes to the actual session."""
    catalog = await _catalog(tmp_path, [], read_chat_history=True)
    binding = await catalog.bind(ToolKey("agent", "get_chat_history"))
    assert binding.catalog is catalog
    catalog.session.runs = [RunOutput(run_id="earlier", messages=[Message(role="user", content="live session")])]
    events = await _events(catalog, "agent", "get_chat_history")
    assert "live session" in events[-1].execution.result


@pytest.mark.asyncio
async def test_provider_native_tools_fail_explicitly(tmp_path: Path) -> None:
    """A provider-only capability must block activation instead of disappearing."""
    with pytest.raises(TypeError, match="Provider-native"):
        await _catalog(tmp_path, [{"type": "web_search"}])


@pytest.mark.asyncio
async def test_arguments_are_frozen_before_started_event(tmp_path: Path) -> None:
    """A caller cannot alter an admitted call while consuming its events."""
    seen = []

    def action(value: int) -> int:
        seen.append(value)
        return value

    catalog = await _catalog(tmp_path, [Toolkit(name="actions", tools=[action])])
    binding = await catalog.bind(ToolKey("actions", "action"))
    arguments: dict[str, object] = {"value": 1}
    stream = agent_tool_calls.execute_agent_tool_call(binding, "id", arguments)
    assert (await anext(stream)).kind == "started"
    arguments["value"] = 2
    events = [event async for event in stream]
    assert events[-1].execution.result == "1"
    assert seen == [1]


@pytest.mark.asyncio
async def test_close_before_dispatch_never_starts_generator(tmp_path: Path) -> None:
    """Closing after admission fences any later generator body side effects."""
    seen = []

    async def streaming() -> AsyncIterator[str]:
        seen.append(True)
        yield "body"

    catalog = await _catalog(tmp_path, [Toolkit(name="stream", tools=[streaming])])
    binding = await catalog.bind(ToolKey("stream", "streaming"))
    stream = agent_tool_calls.execute_agent_tool_call(binding, "id", {})
    assert (await anext(stream)).kind == "started"
    await stream.aclose()
    await asyncio.sleep(0)
    assert seen == []


@pytest.mark.asyncio
async def test_cancelled_sync_leaf_keeps_resources_until_thread_finishes(tmp_path: Path) -> None:
    """Cancellation cannot release toolkit lifetime while its thread is running."""
    entered = threading.Event()
    finish = threading.Event()
    contexts = []

    def action() -> str:
        entered.set()
        finish.wait(timeout=5)
        contexts.append(get_tool_runtime_context())
        return "done"

    catalog = await _catalog(tmp_path, [Toolkit(name="sync", tools=[action])])
    operation = asyncio.create_task(_events(catalog, "sync", "action"))
    assert await asyncio.to_thread(entered.wait, 5)
    try:
        operation.cancel()
        await asyncio.sleep(0)
        close = asyncio.create_task(catalog.close())
        await asyncio.sleep(0)
        assert not close.done()
        for _ in range(10):
            await asyncio.sleep(0)
        operation.cancel()
        for _ in range(10):
            await asyncio.sleep(0)
        assert not close.done()
        assert not operation.done()
    finally:
        finish.set()
    with pytest.raises(asyncio.CancelledError):
        await operation
    await close
    assert contexts == [catalog.runtime_context]


@pytest.mark.asyncio
async def test_authored_alias_and_bare_source_instructions_survive_preparation(tmp_path: Path) -> None:
    """Effective authored ownership and source instructions survive Agno copies."""

    def action() -> str:
        return "ok"

    source = Toolkit(name="implementation", tools=[action], instructions="Source guidance")
    function = source.get_async_functions()["action"]
    function.owning_toolkit = "authored_alias"
    function.source_toolkit = source
    catalog = await _catalog(tmp_path, [function])
    descriptor = await catalog.describe(ToolKey("authored_alias", "action"))
    assert descriptor.instructions == ("Source guidance",)


@pytest.mark.asyncio
async def test_call_local_control_flags_do_not_change_prepared_definition(tmp_path: Path) -> None:
    """The injected FunctionCall controls only this invocation's stop boundary."""

    def stop(fc: FunctionCall) -> str:
        fc.function.stop_after_tool_call = True
        return "stopped"

    catalog = await _catalog(tmp_path, [Toolkit(name="control", tools=[stop])])
    binding = await catalog.bind(ToolKey("control", "stop"))
    events = await _events(catalog, "control", "stop")
    assert events[-1].kind == "continuation_required"
    assert binding.function.stop_after_tool_call is False


@pytest.mark.asyncio
async def test_generator_failure_is_terminal_and_does_not_leak_context(tmp_path: Path) -> None:
    """Partial generator output cannot hide a failed leaf or retain runtime state."""

    async def broken() -> AsyncIterator[str]:
        yield "partial"
        message = "generator failed"
        raise RuntimeError(message)

    catalog = await _catalog(tmp_path, [Toolkit(name="stream", tools=[broken])])
    events = await _events(catalog, "stream", "broken")
    assert events[-1].kind == "failed"
    assert events[-1].execution.tool_call_error
    assert get_tool_runtime_context() is None


@pytest.mark.asyncio
@pytest.mark.parametrize("async_stream", [False, True])
async def test_custom_iterator_closes_once(tmp_path: Path, *, async_stream: bool) -> None:
    """Normal exhaustion and owner cleanup share one close of a custom iterator."""
    closed = []

    class SyncStream:
        def __iter__(self) -> SyncStream:
            return self

        def __next__(self) -> str:
            raise StopIteration

        def close(self) -> None:
            closed.append("sync")

    class AsyncStream:
        def __aiter__(self) -> AsyncStream:
            return self

        async def __anext__(self) -> str:
            raise StopAsyncIteration

        async def aclose(self) -> None:
            closed.append("async")

    async def stream() -> object:
        return AsyncStream() if async_stream else SyncStream()

    catalog = await _catalog(tmp_path, [Toolkit(name="stream", tools=[stream])])
    events = await _events(catalog, "stream", "stream")
    assert events[-1].kind == "completed"
    assert closed == ["async" if async_stream else "sync"]


@pytest.mark.asyncio
async def test_failed_deferred_preparation_retains_agent_cleanup_owner(tmp_path: Path) -> None:
    """Materialization transfers lifetime before schema processing can fail."""
    catalog = await _catalog(tmp_path, [])
    toolkit = Toolkit(name="deferred")
    toolkit.functions["first"] = Function(name="duplicate", entrypoint=lambda: "first")
    toolkit.functions["second"] = Function(name="duplicate", entrypoint=lambda: "second")

    async def materialize() -> Toolkit:
        return toolkit

    catalog.add_deferred(agent_tool_calls.DeferredAgentToolkit("deferred", "Deferred", materialize))
    with pytest.raises(ValueError, match="Duplicate"):
        await catalog.bind(ToolKey("deferred", "duplicate"))
    assert catalog.agent.tools == [toolkit]


@pytest.mark.asyncio
async def test_canonical_shell_releases_only_worker_leaf_and_retains_hooks(tmp_path: Path) -> None:
    """A nested worker request progresses while canonical hooks stay serialized."""
    seen: list[str] = []

    async def run_shell_command(args: str) -> str:
        del args
        pytest.fail("ordinary shell worker must never execute")

    async def mutate(run_context: RunContext) -> str:
        seen.append("mutate")
        run_context.session_state["changed"] = True
        return "changed"

    async def hook(name: str, function_call: Callable[..., Awaitable[str]], arguments: dict) -> str:
        assert name == "run_shell_command"
        seen.append("before")
        result = await function_call(**arguments)
        seen.append("after")
        return result

    shell = Toolkit(name="shell", tools=[run_shell_command])
    shell.get_async_functions()["run_shell_command"].tool_hooks = [hook]
    catalog = await _catalog(tmp_path, [shell, Toolkit(name="state", tools=[mutate])])
    binding = await catalog.bind(ToolKey("shell", "run_shell_command"))

    async def leaf(arguments: dict[str, object]) -> str:
        assert arguments == {"args": "nested"}
        events = await _events(catalog, "state", "mutate")
        assert events[-1].execution.result == "changed"
        return "shell done"

    events = [
        event
        async for event in agent_tool_calls.execute_agent_shell_call(
            binding,
            "shell-inner",
            {"args": "nested"},
            worker_leaf=leaf,
        )
    ]
    assert events[-1].execution.result == "shell done"
    assert seen == ["before", "mutate", "after"]
    assert catalog.run_context.session_state["changed"] is True


@pytest.mark.asyncio
async def test_authorization_is_rechecked_under_dispatch_lock(tmp_path: Path) -> None:
    """Authority changing while a call waits for mutation ownership blocks its body."""
    entered = asyncio.Event()
    release = asyncio.Event()
    effects = []
    allowed = True

    async def blocker() -> str:
        entered.set()
        await release.wait()
        return "released"

    async def effect() -> str:
        effects.append("ran")
        return "ran"

    catalog = await _catalog(tmp_path, [Toolkit(name="calls", tools=[blocker, effect])])
    binding = await catalog.bind(ToolKey("calls", "effect"))
    first = asyncio.create_task(_events(catalog, "calls", "blocker"))
    await entered.wait()

    async def authorize() -> None:
        if not allowed:
            msg = "revoked"
            raise PermissionError(msg)

    async def dispatch() -> list:
        return [
            event
            async for event in agent_tool_calls.execute_agent_tool_call(binding, "second", {}, authorize=authorize)
        ]

    second = asyncio.create_task(dispatch())
    await asyncio.sleep(0)
    allowed = False
    release.set()
    await first
    with pytest.raises(PermissionError, match="revoked"):
        await second
    assert effects == []


@pytest.mark.asyncio
@pytest.mark.parametrize("approved", [True, False])
async def test_exact_native_confirmation_resumes_prepared_call(tmp_path: Path, approved: bool) -> None:
    """Only the exact resolved native requirement releases a prepared call."""
    seen = []

    def action(value: int) -> int:
        seen.append(value)
        return value

    function = Function.from_callable(action)
    function.owning_toolkit = "actions"
    function.requires_confirmation = True
    catalog = await _catalog(tmp_path, [function])
    binding = await catalog.bind(ToolKey("actions", "action"))
    events = await _events(catalog, "actions", "action", {"value": 3})
    requirement = events[-1].requirement
    assert requirement is not None
    if approved:
        requirement.confirm()
    else:
        requirement.reject("Denied")
    resumed = [
        event
        async for event in agent_tool_calls.execute_agent_tool_call(
            binding,
            "inner-id",
            {"value": 3},
            requirement=requirement,
        )
    ]
    assert seen == ([3] if approved else [])
    assert resumed[-1].kind == ("completed" if approved else "failed")
    assert binding.function.requires_confirmation is True
    for changed in (4, 3.0):
        with pytest.raises(ValueError, match="exact"):
            _ = [
                event
                async for event in agent_tool_calls.execute_agent_tool_call(
                    binding,
                    "inner-id",
                    {"value": changed},
                    requirement=requirement,
                )
            ]
    assert seen == ([3] if approved else [])


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["requires_user_input", "external_execution", "approval_type"])
async def test_resolved_confirmation_cannot_bypass_changed_requirement(tmp_path: Path, change: str) -> None:
    """Approval cannot authorize a new requirement added before prepared dispatch."""
    effects = []

    def action() -> str:
        effects.append(True)
        return "executed"

    function = Function.from_callable(action)
    function.owning_toolkit = "actions"
    function.requires_confirmation = True
    function.approval_type = "mindroom_policy"
    catalog = await _catalog(tmp_path, [function])
    requirement = (await _events(catalog, "actions", "action"))[-1].requirement
    requirement.confirm()
    binding = await catalog.bind(ToolKey("actions", "action"))
    setattr(binding.function, change, "authored" if change == "approval_type" else True)
    with pytest.raises(ValueError, match="exact"):
        _ = [
            event
            async for event in agent_tool_calls.execute_agent_tool_call(
                binding,
                "inner-id",
                {},
                requirement=requirement,
            )
        ]
    assert effects == []


@pytest.mark.asyncio
@pytest.mark.parametrize("revocation", ["membership", "config", "filter"])
async def test_shared_cli_authorizer_rejects_revoked_prepared_call(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    revocation: str,
) -> None:
    """Live and restart dispatch share the current native authorization boundary."""
    effects = []

    def action() -> str:
        effects.append(True)
        return "executed"

    function = Function.from_callable(action)
    function.owning_toolkit = "actions"
    catalog = await _catalog(tmp_path, [function])
    config = Config(agents={"helper": AgentConfig(display_name="Helper", tools=["shell"])})
    current = config.model_copy(deep=True) if revocation == "config" else config
    if revocation == "config":
        current.agents["helper"].tools = []
    runtime = replace(
        catalog.runtime_context,
        config=config,
        config_provider=lambda: current,
        tool_function_filter=lambda _: revocation != "filter",
    )
    catalog.runtime_context = runtime
    binding = await catalog.bind(ToolKey("actions", "action"))
    monkeypatch.setattr(
        approval_tools,
        "is_sender_allowed_for_entity_replies_in_room",
        lambda *_args, **_kwargs: revocation != "membership",
    )
    with pytest.raises(PermissionError):
        await approval_tools.authorize_prepared_tool_call(
            binding,
            expected_worker_target=runtime.resolve_worker_target(),
        )
    assert effects == []


@pytest.mark.asyncio
@pytest.mark.parametrize("revocation", [None, "config", "membership", "forged"])
async def test_generated_cli_authorization_keeps_exact_owner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    revocation: str | None,
) -> None:
    """Generated functions need their effective owner and current reply permission."""
    config = Config(agents={"helper": AgentConfig(display_name="Helper")})
    catalog = await _catalog(tmp_path, [], enable_agentic_state=True)
    runtime = replace(catalog.runtime_context, config=config, config_provider=lambda: config)
    catalog.runtime_context = runtime
    catalog._bindings.clear()
    await catalog.prepare(await catalog.agent.aget_tools(catalog.run_response, catalog.run_context, catalog.session))
    binding = await catalog.bind(ToolKey("agent", "update_session_state"))
    target = runtime.resolve_worker_target()
    monkeypatch.setattr(
        approval_tools,
        "is_sender_allowed_for_entity_replies_in_room",
        lambda *_args, **_kwargs: revocation != "membership",
    )
    if revocation == "config":
        current = config.model_copy(deep=True)
        current.agents["helper"].display_name = "Changed"
        catalog.runtime_context = replace(runtime, config_provider=lambda: current)
    if revocation == "forged":
        binding = replace(binding, function=Function(name="arbitrary", entrypoint=lambda: "bad"))
    if revocation:
        with pytest.raises(PermissionError):
            await approval_tools.authorize_prepared_tool_call(binding, expected_worker_target=target)
    else:
        await approval_tools.authorize_prepared_tool_call(binding, expected_worker_target=target)


@pytest.mark.asyncio
async def test_generated_learning_uses_live_requester_session_and_context(tmp_path: Path) -> None:
    """Generated learning closures retain the same identities and mutable state."""
    catalog = await _catalog(tmp_path, [])

    async def learning_tools(
        user_id: str,
        session_id: str,
        agent_id: str,
        run_context: RunContext,
        **kwargs: object,  # noqa: ARG001
    ) -> list:
        async def save_learning(value: str) -> str:
            run_context.session_state["learning"] = value
            return f"{agent_id}/{user_id}/{session_id}: {value}"

        return [save_learning]

    # Isolate persistence/provider work at the learning-store boundary; the real
    # LearningMachine and Agent still construct the generated asynchronous tool.
    store = cast("LearningStore", SimpleNamespace(aget_tools=learning_tools))
    catalog.agent._learning = LearningMachine(custom_stores={"local": store})
    await catalog.prepare(
        await catalog.agent.aget_tools(
            catalog.run_response,
            catalog.run_context,
            catalog.session,
            user_id=catalog.runtime_context.requester_id,
        ),
    )
    events = await _events(catalog, "agent", "save_learning", {"value": "shared lesson"})
    assert events[-1].execution.result == "helper/@alice:test/session: shared lesson"
    assert catalog.run_context.session_state["learning"] == "shared lesson"
    await catalog.close()

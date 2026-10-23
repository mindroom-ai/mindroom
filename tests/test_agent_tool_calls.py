"""The live turn's CLI catalog and its calls, without provider requests."""

from __future__ import annotations

import asyncio
import threading
from collections.abc import AsyncIterator, Iterator  # noqa: TC003 - Agno resolves tool annotations
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from dataclasses import replace
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock

import pytest
from agno.media import Image
from agno.run import RunContext as AgnoRunContext  # noqa: TC002 - Agno resolves tool annotations
from agno.tools.function import Function, ToolResult
from agno.tools.toolkit import Toolkit

from mindroom.agent_loop.spec import RunContext
from mindroom.approval import tool_owners as approval_tools
from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.message_target import MessageTarget
from mindroom.session_storage.types import ConversationSession
from mindroom.tool_jobs.control import JobControl, job_control_context
from mindroom.tool_system import agent_tool_calls
from mindroom.tool_system.agno_toolkits import adapt_function, adapt_toolkit
from mindroom.tool_system.invocation import ToolCall, ToolOutcome, current_tool_call
from mindroom.tool_system.runtime_context import get_tool_runtime_context
from mindroom.tool_system.tool_access import ToolKey, UnknownToolError
from mindroom.tool_system.tool_function import AdaptedToolkit
from mindroom.tool_system.tool_hooks import run_async_entrypoint_blocking_call
from mindroom.tool_system.worker_routing import get_tool_execution_identity
from tests.authorization_helpers import make_test_tool_runtime_context
from tests.conftest import make_conversation_reader_mock, make_relation_lookup, test_runtime_paths

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Mapping, Sequence
    from pathlib import Path

    from mindroom.tool_system.invocation import ToolMiddleware, ToolNext


def _toolkit(name: str, *tools: Callable[..., object], instructions: str | None = None) -> AdaptedToolkit:
    return adapt_toolkit(Toolkit(name=name, tools=list(tools), instructions=instructions))


def _function(entrypoint: Callable[..., object], **flags: object) -> AdaptedToolkit:
    function = Function.from_callable(entrypoint)
    for name, value in flags.items():
        setattr(function, name, value)
    return AdaptedToolkit(name="actions", functions=(adapt_function(function),), instructions=None)


async def _catalog(
    tmp_path: Path,
    toolkits: Sequence[AdaptedToolkit],
    *,
    owners: Mapping[str, str | None] | None = None,
    include: Callable[[Any], bool] | None = None,
    middleware: Sequence[ToolMiddleware] = (),
    tool_call_limit: int | None = None,
) -> agent_tool_calls.PreparedAgentToolCatalog:
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
    session = ConversationSession(session_id="session", kind="agent", owner_id="helper")
    catalog = agent_tool_calls.PreparedAgentToolCatalog(
        agent_name="helper",
        run=RunContext(run_id="run", session_id="session", user_id="@alice:test", session=session),
        runtime_context=runtime,
        middleware=middleware,
        tool_call_limit=tool_call_limit,
    )
    if owners is None:
        owners = {function.name: toolkit.name for toolkit in toolkits for function in toolkit.functions}
    await catalog.prepare(toolkits, owners, include=include)
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
async def test_catalog_prepares_only_included_functions(tmp_path: Path) -> None:
    """Excluded functions are neither listed nor callable through the catalog."""

    async def keep() -> str:
        return "kept"

    async def drop() -> str:
        return "dropped"

    catalog = await _catalog(
        tmp_path,
        [_toolkit("mixed", keep, drop)],
        include=lambda function: function.name != "drop",
    )

    assert [(item["toolkit"], item["function"]) for item in catalog.metadata()] == [("mixed", "keep")]
    with pytest.raises(UnknownToolError, match="no function 'drop'; its functions: keep"):
        await catalog.bind(ToolKey("mixed", "drop"))


@pytest.mark.asyncio
@pytest.mark.parametrize("in_job", [False, True])
async def test_a_cli_call_inside_a_job_blocks_on_its_own_thread(tmp_path: Path, *, in_job: bool) -> None:
    """A job's CLI call has no time limit, so what it offloads, like a worker request, gets its own thread."""
    shared = ThreadPoolExecutor(thread_name_prefix="shared-pool")
    threads: list[str] = []

    def blocking() -> str:
        threads.append(threading.current_thread().name)
        return "done"

    async def offloading() -> str:
        return await run_async_entrypoint_blocking_call(blocking, executor=shared)

    catalog = await _catalog(tmp_path, [_toolkit("threads", offloading)])
    try:
        with job_control_context(JobControl()) if in_job else nullcontext():
            events = await _events(catalog, "threads", "offloading")
        assert events[-1].execution.result == "done"
        [thread] = threads
        assert thread.startswith("mindroom-job-call" if in_job else "shared-pool")
    finally:
        shared.shutdown(wait=False)


@pytest.mark.asyncio
@pytest.mark.parametrize("in_job", [False, True])
async def test_a_failed_offloaded_cli_call_ends_as_that_calls_failure(tmp_path: Path, *, in_job: bool) -> None:
    """A worker request that raises fails only its own call, so the shell command that made it keeps its output."""
    shared = ThreadPoolExecutor(thread_name_prefix="shared-pool")

    def blocking() -> str:
        message = "runner refused the call"
        raise RuntimeError(message)

    async def offloading() -> str:
        return await run_async_entrypoint_blocking_call(blocking, executor=shared)

    catalog = await _catalog(tmp_path, [_toolkit("threads", offloading)])
    try:
        with job_control_context(JobControl()) if in_job else nullcontext():
            events = await _events(catalog, "threads", "offloading")
        assert events[-1].kind == "failed"
        assert "runner refused the call" in str(events[-1].execution.result)
    finally:
        shared.shutdown(wait=False)


@pytest.mark.asyncio
async def test_prepared_call_preserves_real_owner_context_and_middleware(tmp_path: Path) -> None:
    """A call runs as its turn, through the build's tool policy once, and sees its own call id."""
    seen = []
    policy = []

    def inspect_binding(run_context: AgnoRunContext) -> str:
        seen.append((run_context.run_id, get_tool_runtime_context(), current_tool_call()))
        identity = get_tool_execution_identity()
        assert identity is not None
        assert identity.requester_id == "@alice:test"
        assert identity.session_id == "session"
        run_context.session_state["changed"] = True
        return "bound"

    async def middleware(call: ToolCall, call_next: ToolNext) -> object:
        policy.append(call.function.name)
        return await call_next(call.arguments)

    catalog = await _catalog(
        tmp_path,
        [_toolkit("inspect", inspect_binding, instructions="Keep identity.")],
        middleware=(middleware,),
    )
    events = await _events(catalog, "inspect", "inspect_binding")
    assert [event.kind for event in events] == ["started", "completed"]
    assert events[-1].execution.result == "bound"
    [(run_id, runtime, call)] = seen
    assert run_id == "run"
    assert runtime is catalog.runtime_context
    assert call is not None
    assert call.context.call_id == "inner-id"
    assert policy == ["inspect_binding"]
    assert catalog.run.session_state == {"changed": True}
    assert catalog.record is None
    assert get_tool_runtime_context() is None
    descriptor = await catalog.describe(ToolKey("inspect", "inspect_binding"))
    assert descriptor.instructions == ("Keep identity.",)
    assert descriptor.input_schema["properties"] == {}


@pytest.mark.asyncio
async def test_sync_async_generators_media_and_generated_functions(tmp_path: Path) -> None:
    """Bound calls drain generators, return media, and reach the agent's generated functions as `agent`."""

    def sync(value: int) -> int:
        return value + 1

    async def asynchronous(value: int) -> int:
        return value + 2

    def generator() -> Iterator[str]:
        assert get_tool_runtime_context() is not None
        yield "one"
        yield "two"

    async def async_generator(run_context: AgnoRunContext) -> AsyncIterator[str]:
        yield "three"
        assert get_tool_runtime_context() is not None
        run_context.session_state["streamed"] = True
        yield "four"

    def media() -> ToolResult:
        return ToolResult(content="picture", images=[Image(url="https://example.org/image.png")])

    async def search_knowledge_base(query: str) -> str:
        return f"Knowledge about {query}"

    ordinary = _toolkit("ordinary", sync, asynchronous, generator, async_generator, media)
    generated = _toolkit("search_knowledge_base", search_knowledge_base)
    catalog = await _catalog(
        tmp_path,
        [ordinary, generated],
        owners={function.name: "ordinary" for function in ordinary.functions},
    )
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
    assert events[-1].execution.result == "Knowledge about binding"
    assert catalog.run.session_state["streamed"] is True


@pytest.mark.asyncio
async def test_validation_pause_continuation_and_call_budget(tmp_path: Path) -> None:
    """Invalid calls never run; pauses and continuations stay explicit."""
    seen = []

    def action(value: int) -> int:
        seen.append(value)
        return value

    catalog = await _catalog(tmp_path, [_function(action)], tool_call_limit=1)
    events = await _events(catalog, "actions", "action", {"value": "wrong"})
    assert events[-1].kind == "failed"
    assert seen == []
    events = await _events(catalog, "actions", "action", {"value": 1})
    assert events[-1].kind == "completed"
    events = await _events(catalog, "actions", "action", {"value": 2})
    assert events[-1].kind == "failed"
    assert seen == [1]

    for flag in ("requires_confirmation", "external_execution"):
        catalog = await _catalog(tmp_path, [_function(action, **{flag: True})])
        events = await _events(catalog, "actions", "action", {"value": 3})
        assert events[-1].kind == "waiting"
        assert events[-1].requirement.tool_execution.tool_args == {"value": 3}
    assert seen == [1]
    catalog = await _catalog(tmp_path, [_function(action, stop_after_tool_call=True)])
    events = await _events(catalog, "actions", "action", {"value": 4})
    assert events[-1].kind == "continuation_required"
    assert events[-1].execution.result == "4"


@pytest.mark.asyncio
async def test_catalog_keeps_qualified_names_and_rejects_collisions(tmp_path: Path) -> None:
    """Each function is reached under its authored toolkit; the run's first function of a name is the one bound."""

    def action() -> str:
        return "ok"

    def other() -> str:
        return "other"

    catalog = await _catalog(
        tmp_path,
        [_toolkit("one", action), _toolkit("two", action, other)],
        owners={"action": "one", "other": "two"},
    )
    assert (await _events(catalog, "one", "action"))[-1].execution.result == "ok"
    assert (await _events(catalog, "two", "other"))[-1].execution.result == "other"
    with pytest.raises(UnknownToolError):
        await catalog.bind(ToolKey("two", "action"))
    with pytest.raises(ValueError, match="reserved"):
        await _catalog(tmp_path, [_toolkit("agent", action)])
    with pytest.raises(ValueError, match="reserved"):
        catalog.add_deferred(agent_tool_calls.DeferredAgentToolkit("agent", "Reserved", _never_materialized))
    with pytest.raises(ValueError, match="Duplicate toolkit"):
        catalog.add_deferred(agent_tool_calls.DeferredAgentToolkit("one", "Taken", _never_materialized))


async def _never_materialized() -> AdaptedToolkit:
    pytest.fail("a refused deferred toolkit is never materialized")


@pytest.mark.asyncio
async def test_lazy_metadata_and_single_materialization(tmp_path: Path) -> None:
    """Discovery stays lazy and concurrent describe builds only once."""
    builds = []

    def action(value: int) -> int:
        return value

    async def materialize() -> AdaptedToolkit:
        builds.append(get_tool_runtime_context())
        await asyncio.sleep(0)
        return _toolkit("implementation", action, instructions="Actual lazy instructions")

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
async def test_toolkits_the_catalog_connects_stay_connected_until_it_closes(tmp_path: Path) -> None:
    """A minimal turn's toolkits, eager and deferred, are connected through its catalog and closed with it."""
    log: list[str] = []

    def connecting(adapted: AdaptedToolkit) -> AdaptedToolkit:
        async def close() -> None:
            log.append(f"{adapted.name} close")

        async def connect() -> Callable[[], Awaitable[None]]:
            log.append(f"{adapted.name} connect")
            return close

        return replace(adapted, connect=connect)

    def query(sql: str) -> str:
        return sql

    def lookup(term: str) -> str:
        return term

    async def materialize() -> AdaptedToolkit:
        return connecting(_toolkit("server", lookup))

    database = connecting(_toolkit("database", query))
    catalog = await _catalog(tmp_path, [database])
    await catalog.connect([database])
    catalog.add_deferred(agent_tool_calls.DeferredAgentToolkit("server", "An MCP server", materialize))
    assert (await _events(catalog, "server", "lookup", {"term": "x"}))[-1].execution.result == "x"
    assert log == ["database connect", "server connect"]

    await catalog.close()
    await catalog.close()

    assert log == ["database connect", "server connect", "server close", "database close"]


@pytest.mark.asyncio
async def test_cancelling_a_streaming_call_closes_its_generator_before_the_catalog_releases(tmp_path: Path) -> None:
    """A cancelled call's generator is closed inside the call, before anything else can take the catalog."""
    started = asyncio.Event()
    closed = []

    async def streaming() -> AsyncIterator[str]:
        try:
            yield "first"
            started.set()
            await asyncio.Event().wait()
            yield "second"
        finally:
            assert get_tool_runtime_context() is not None
            closed.append(True)

    catalog = await _catalog(tmp_path, [_toolkit("stream", streaming)])
    call = asyncio.create_task(_events(catalog, "stream", "streaming"))
    await started.wait()
    close = asyncio.create_task(catalog.close())
    await asyncio.sleep(0)
    assert not close.done()
    call.cancel()
    with pytest.raises(asyncio.CancelledError):
        await call
    assert closed == [True]
    await close


@pytest.mark.asyncio
async def test_closed_catalog_rejects_later_bindings(tmp_path: Path) -> None:
    """Close is idempotent and fences every later binding."""

    def action() -> str:
        return "ok"

    catalog = await _catalog(tmp_path, [_toolkit("existing", action)])
    await catalog.close()
    await catalog.close()
    with pytest.raises(RuntimeError, match="closed"):
        await catalog.bind(ToolKey("existing", "action"))


@pytest.mark.asyncio
async def test_arguments_are_frozen_before_started_event(tmp_path: Path) -> None:
    """A caller cannot alter an admitted call while consuming its events."""
    seen = []

    def action(value: int) -> int:
        seen.append(value)
        return value

    catalog = await _catalog(tmp_path, [_toolkit("actions", action)])
    binding = await catalog.bind(ToolKey("actions", "action"))
    arguments: dict[str, object] = {"value": 1}
    stream = agent_tool_calls.execute_agent_tool_call(binding, "id", arguments)
    started = await anext(stream)
    assert started.kind == "started"
    arguments["value"] = 2
    started.execution.tool_args["value"] = 3
    events = [event async for event in stream]
    assert events[-1].execution.result == "1"
    assert events[-1].execution.tool_args == {"value": 1}
    assert seen == [1]


@pytest.mark.asyncio
async def test_close_before_dispatch_never_starts_generator(tmp_path: Path) -> None:
    """Closing after admission fences any later generator body side effects."""
    seen = []

    async def streaming() -> AsyncIterator[str]:
        seen.append(True)
        yield "body"

    catalog = await _catalog(tmp_path, [_toolkit("stream", streaming)])
    binding = await catalog.bind(ToolKey("stream", "streaming"))
    stream = agent_tool_calls.execute_agent_tool_call(binding, "id", {})
    assert (await anext(stream)).kind == "started"
    await stream.aclose()
    await asyncio.sleep(0)
    assert seen == []


@pytest.mark.asyncio
async def test_cancelled_sync_leaf_keeps_resources_until_thread_finishes(tmp_path: Path) -> None:
    """Cancellation cannot release the catalog while its thread is running."""
    entered = threading.Event()
    finish = threading.Event()
    contexts = []

    def action() -> str:
        entered.set()
        finish.wait(timeout=5)
        contexts.append(get_tool_runtime_context())
        return "done"

    catalog = await _catalog(tmp_path, [_toolkit("sync", action)])
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
async def test_authored_alias_and_toolkit_guidance_are_described(tmp_path: Path) -> None:
    """A function is described under its authored toolkit, with its toolkit's guidance the prompt does not show."""

    def action() -> str:
        return "ok"

    catalog = await _catalog(
        tmp_path,
        [_toolkit("implementation", action, instructions="Source guidance")],
        owners={"action": "authored_alias"},
    )
    descriptor = await catalog.describe(ToolKey("authored_alias", "action"))
    assert descriptor.instructions == ("Source guidance",)


@pytest.mark.asyncio
async def test_a_call_that_returns_a_stop_controls_only_its_own_invocation(tmp_path: Path) -> None:
    """A call that asks to stop the run is a continuation; the prepared definition keeps running normally."""

    def stop() -> ToolOutcome:
        return ToolOutcome(content="stopped", stop_after_call=True)

    catalog = await _catalog(tmp_path, [_toolkit("control", stop)])
    binding = await catalog.bind(ToolKey("control", "stop"))
    events = await _events(catalog, "control", "stop")
    assert events[-1].kind == "continuation_required"
    assert events[-1].execution.result == "stopped"
    assert binding.function.stop_after_tool_call is False


@pytest.mark.asyncio
async def test_generator_failure_is_terminal_and_does_not_leak_context(tmp_path: Path) -> None:
    """Partial generator output cannot hide a failed leaf or retain runtime state."""

    async def broken() -> AsyncIterator[str]:
        yield "partial"
        message = "generator failed"
        raise RuntimeError(message)

    catalog = await _catalog(tmp_path, [_toolkit("stream", broken)])
    events = await _events(catalog, "stream", "broken")
    assert events[-1].kind == "failed"
    assert events[-1].execution.tool_call_error
    assert get_tool_runtime_context() is None


@pytest.mark.asyncio
async def test_a_deferred_toolkit_whose_functions_collide_is_refused(tmp_path: Path) -> None:
    """A materialized toolkit that names one function twice binds nothing."""
    catalog = await _catalog(tmp_path, [])
    function = adapt_function(Function(name="duplicate", entrypoint=lambda: "first"))

    async def materialize() -> AdaptedToolkit:
        return AdaptedToolkit(name="deferred", functions=(function, function), instructions=None)

    catalog.add_deferred(agent_tool_calls.DeferredAgentToolkit("deferred", "Deferred", materialize))
    with pytest.raises(ValueError, match="Duplicate"):
        await catalog.bind(ToolKey("deferred", "duplicate"))
    assert catalog.metadata() == [{"toolkit": "deferred", "description": "Deferred", "deferred": True}]


@pytest.mark.asyncio
async def test_canonical_shell_releases_only_shell_leaf_and_retains_middleware(tmp_path: Path) -> None:
    """A nested CLI request progresses while the shell call's tool policy stays around it."""
    seen: list[str] = []

    async def run_shell_command(args: str) -> str:
        del args
        pytest.fail("only the owner's shell leaf runs the command")

    async def mutate(run_context: AgnoRunContext) -> str:
        seen.append("mutate")
        run_context.session_state["changed"] = True
        return "changed"

    async def middleware(call: ToolCall, call_next: ToolNext) -> object:
        if call.function.name != "run_shell_command":
            return await call_next(call.arguments)
        seen.append("before")
        result = await call_next(call.arguments)
        seen.append("after")
        return result

    catalog = await _catalog(
        tmp_path,
        [_toolkit("shell", run_shell_command), _toolkit("state", mutate)],
        middleware=(middleware,),
    )
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
            shell_leaf=leaf,
        )
    ]
    assert events[-1].execution.result == "shell done"
    assert seen == ["before", "mutate", "after"]
    assert catalog.run.session_state["changed"] is True


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

    catalog = await _catalog(tmp_path, [_toolkit("calls", blocker, effect)])
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

    catalog = await _catalog(tmp_path, [_function(action, requires_confirmation=True)])
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
@pytest.mark.parametrize("change", ["external_execution", "approval_type"])
async def test_resolved_confirmation_cannot_bypass_changed_requirement(tmp_path: Path, change: str) -> None:
    """Approval cannot authorize a function whose pause changed before prepared dispatch."""
    effects = []

    def action() -> str:
        effects.append(True)
        return "executed"

    catalog = await _catalog(
        tmp_path,
        [_function(action, requires_confirmation=True, approval_type="mindroom_policy")],
    )
    requirement = (await _events(catalog, "actions", "action"))[-1].requirement
    requirement.confirm()
    key = ToolKey("actions", "action")
    binding = await catalog.bind(key)
    changed = replace(binding.function, **{change: "authored" if change == "approval_type" else True})
    catalog._bindings[key] = binding = replace(binding, function=changed)
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

    catalog = await _catalog(tmp_path, [_function(action)])
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
@pytest.mark.parametrize("revocation", [None, "config", "membership", "forged", "stale"])
async def test_generated_cli_authorization_keeps_exact_owner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    revocation: str | None,
) -> None:
    """Generated functions need their exact catalog binding and current reply permission."""
    config = Config(agents={"helper": AgentConfig(display_name="Helper")})

    def update_session_state(value: str) -> str:
        return value

    catalog = await _catalog(tmp_path, [_toolkit("generated", update_session_state)], owners={})
    runtime = replace(catalog.runtime_context, config=config, config_provider=lambda: config)
    catalog.runtime_context = runtime
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
        binding = replace(binding, function=adapt_function(Function(name="arbitrary", entrypoint=lambda: "bad")))
    if revocation == "stale":
        await catalog.close()
    if revocation:
        with pytest.raises(PermissionError):
            await approval_tools.authorize_prepared_tool_call(binding, expected_worker_target=target)
    else:
        await approval_tools.authorize_prepared_tool_call(binding, expected_worker_target=target)

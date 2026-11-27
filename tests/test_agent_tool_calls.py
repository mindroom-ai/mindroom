"""The live turn's CLI catalog and its calls, without provider requests."""

from __future__ import annotations

import asyncio
import threading
import weakref
from collections.abc import AsyncIterator, Iterator  # noqa: TC003 - Agno resolves tool annotations
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from dataclasses import replace
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import MagicMock

import pytest

from mindroom.agent_loop.spec import RunContext, UnstoredSession
from mindroom.agent_loop.tools import offered_toolkits
from mindroom.approval import tool_owners as approval_tools
from mindroom.config.agent import AgentConfig
from mindroom.config.approval import ApprovalRuleConfig
from mindroom.config.budgets import BudgetsConfig
from mindroom.config.calls import CallsConfig, LiveCallProfile
from mindroom.config.main import Config
from mindroom.config.models import CompactionConfig, CompactionOverrideConfig, ModelConfig, ModelPricing
from mindroom.config.personal_agents import PersonalAgentInstance, PersonalAgentTemplateConfig
from mindroom.config.personal_rooms import PersonalRoomsConfig
from mindroom.config.slack import SlackConfig
from mindroom.llm.media import Image
from mindroom.mcp.config import MCPServerConfig
from mindroom.message_target import MessageTarget
from mindroom.session_storage.types import ConversationSession
from mindroom.tool_jobs.control import JobControl, job_control_context
from mindroom.tool_system import agent_tool_calls
from mindroom.tool_system.authoring import Toolkit, current_call, passthrough_tool
from mindroom.tool_system.declarations import ApprovalAlias, ToolCategory, ToolFileAccess, ToolMetadata
from mindroom.tool_system.invocation import ToolCall, current_tool_call
from mindroom.tool_system.registry import ToolRegistry
from mindroom.tool_system.runtime_context import current_execution_identity, get_tool_runtime_context
from mindroom.tool_system.tool_access import ToolKey, UnknownToolError
from mindroom.tool_system.tool_function import AdaptedToolkit, ToolFunction, ToolGate, ToolOutcome, ToolValue
from mindroom.tool_system.tool_hooks import run_async_entrypoint_blocking_call
from mindroom.tool_system.toolkits import adapt_callable, adapt_toolkit
from mindroom.workers.placements import DedicatedWorkerPlacement, dedicated_worker_placement
from tests.authorization_helpers import make_test_tool_runtime_context
from tests.conftest import gc_paused, make_conversation_reader_mock, make_relation_lookup, test_runtime_paths

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Mapping, Sequence
    from pathlib import Path

    from mindroom.tool_system.invocation import ToolMiddleware, ToolNext
    from mindroom.tool_system.runtime_context import ToolRuntimeContext


def _toolkit(name: str, *tools: Callable[..., object], instructions: str | None = None) -> AdaptedToolkit:
    """Adapt a toolkit of ``tools`` an agent is offered under its own name."""
    return adapt_toolkit(Toolkit(name=name, tools=list(tools), instructions=instructions), key=name)


def _function(
    entrypoint: Callable[..., object],
    *,
    gate: ToolGate | None = None,
    requires_confirmation: bool = False,
    stop_after_tool_call: bool = False,
) -> AdaptedToolkit:
    """Adapt ``entrypoint`` with the given gate and stop flag."""
    resolved_gate = gate if gate is not None else (ToolGate.CONFIRM if requires_confirmation else None)
    adapted = replace(adapt_callable(entrypoint), gate=resolved_gate, stop_after_call=stop_after_tool_call)
    return AdaptedToolkit(name="actions", functions=(adapted,), instructions=None)


def _runtime(tmp_path: Path) -> ToolRuntimeContext:
    return make_test_tool_runtime_context(
        agent_name="helper",
        target=MessageTarget("!room:test", "$thread", "$thread", "$event", "session"),
        requester_id="@alice:test",
        client=MagicMock(),
        config=Config(),
        runtime_paths=test_runtime_paths(tmp_path),
        relations=make_relation_lookup(),
        conversation_reader=make_conversation_reader_mock(),
    )


def _authored(toolkits: Sequence[AdaptedToolkit], owners: Mapping[str, str] | None) -> list[AdaptedToolkit]:
    """Return ``toolkits`` with each function naming its authored and registered toolkit, as agent assembly does.

    Each toolkit's name is the registered tool its functions come from. ``owners`` maps each function to its
    authored toolkit, and a function it leaves out is generated for the agent; without it every function is authored
    under its own toolkit's name. The run's first function of a name is kept.
    """
    authored = [
        replace(
            toolkit,
            functions=tuple(
                _named(function, toolkit.name if owners is None else owners.get(function.name), toolkit.name)
                for function in toolkit.functions
            ),
        )
        for toolkit in toolkits
    ]
    return list(offered_toolkits(authored, agent="helper"))


def _named(function: ToolFunction, authored: str | None, registered: str) -> ToolFunction:
    """Return ``function`` authored under ``authored`` from the registered tool ``registered``, or generated."""
    if authored is None:
        return replace(function, toolkit=None, registered_toolkit=None)
    return replace(function, toolkit=authored, registered_toolkit=registered)


async def _catalog(
    tmp_path: Path,
    toolkits: Sequence[AdaptedToolkit],
    *,
    owners: Mapping[str, str] | None = None,
    include: Callable[[Any], bool] | None = None,
    middleware: Sequence[ToolMiddleware] = (),
    max_tool_calls_per_turn: int | None = None,
) -> agent_tool_calls.PreparedAgentToolCatalog:
    session = ConversationSession(session_id="session", kind="agent", owner_id="helper")
    catalog = agent_tool_calls.PreparedAgentToolCatalog(
        run=RunContext(run_id="run", user_id="@alice:test", session=session),
        runtime_context=_runtime(tmp_path),
        store=None,
        middleware=middleware,
        max_tool_calls_per_turn=max_tool_calls_per_turn,
    )
    await catalog.prepare(_authored(toolkits, owners), include=include)
    return catalog


async def _events(
    catalog: agent_tool_calls.PreparedAgentToolCatalog,
    toolkit: str,
    function: str,
    arguments: dict | None = None,
) -> list[agent_tool_calls.AgentToolCallEvent]:
    binding = await catalog.bind(ToolKey(toolkit, function))
    return [event async for event in agent_tool_calls.execute_agent_tool_call(binding, "inner-id", arguments or {})]


def test_a_catalog_cannot_be_built_for_a_run_without_a_session(tmp_path: Path) -> None:
    """A run outside the session its catalog's checkpoints and delegations write fails at once."""
    with pytest.raises(TypeError, match="belongs to a session"):
        agent_tool_calls.PreparedAgentToolCatalog(
            run=RunContext(run_id="run", session=UnstoredSession("session"), user_id="@alice:test"),
            runtime_context=_runtime(tmp_path),
            store=None,
            middleware=(),
            max_tool_calls_per_turn=None,
        )


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
        assert events[-1].call.result == "done"
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
        assert (events[-1].kind, events[-1].call.is_error) == ("completed", True)
        assert "runner refused the call" in str(events[-1].call.result)
    finally:
        shared.shutdown(wait=False)


@pytest.mark.asyncio
async def test_prepared_call_preserves_real_owner_context_and_middleware(tmp_path: Path) -> None:
    """A call runs as its turn, through the build's tool policy once, and sees its own call id."""
    seen = []
    policy = []

    def inspect_binding() -> str:
        tool_call = current_tool_call()
        assert tool_call is not None
        seen.append((tool_call.context.run_id, get_tool_runtime_context(), tool_call))
        identity = current_execution_identity()
        assert identity is not None
        assert identity.requester_id == "@alice:test"
        assert identity.session_id == "session"
        tool_call.context.session_state["changed"] = True
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
    assert events[-1].call.result == "bound"
    [(run_id, runtime, call)] = seen
    assert run_id == "run"
    assert runtime is catalog.runtime_context
    assert call is not None
    assert call.context.call_id == "inner-id"
    assert policy == ["inspect_binding"]
    assert catalog.run.session_state == {"changed": True}
    assert catalog.record is None
    assert get_tool_runtime_context() is None
    described = await catalog.bind(ToolKey("inspect", "inspect_binding"))
    assert described.instructions == ("Keep identity.",)
    assert described.function.parameters["properties"] == {}


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

    async def async_generator() -> AsyncIterator[str]:
        yield "three"
        assert get_tool_runtime_context() is not None
        call = current_call()
        assert call is not None
        call.session_state["streamed"] = True
        yield "four"

    def media() -> ToolValue:
        return ToolValue(content="picture", images=(Image(url="https://example.org/image.png"),))

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
        assert events[-1].call.result == expected
    started, media, completed = await _events(catalog, "ordinary", "media")
    assert (started.kind, completed.kind) == ("started", "completed")
    assert isinstance(media, agent_tool_calls.AgentToolCallMedia)
    assert media.media.images[0].url == "https://example.org/image.png"
    events = await _events(catalog, "agent", "search_knowledge_base", {"query": "binding"})
    assert events[-1].call.result == "Knowledge about binding"
    assert catalog.run.session_state["streamed"] is True


@pytest.mark.asyncio
async def test_validation_pause_continuation_and_call_budget(tmp_path: Path) -> None:
    """Invalid calls never run; pauses and continuations stay explicit."""
    seen = []

    def action(value: int) -> int:
        seen.append(value)
        return value

    catalog = await _catalog(tmp_path, [_function(action)], max_tool_calls_per_turn=1)
    events = await _events(catalog, "actions", "action", {"value": "wrong"})
    assert (events[-1].kind, events[-1].call.is_error) == ("completed", True)
    assert seen == []
    events = await _events(catalog, "actions", "action", {"value": 1})
    assert (events[-1].kind, events[-1].call.is_error) == ("completed", False)
    events = await _events(catalog, "actions", "action", {"value": 2})
    assert (events[-1].kind, events[-1].call.is_error) == ("completed", True)
    assert seen == [1]

    for gate in (ToolGate.CONFIRM, ToolGate.DELEGATION):
        catalog = await _catalog(tmp_path, [_function(action, gate=gate)])
        [waiting] = await _events(catalog, "actions", "action", {"value": 3})
        assert isinstance(waiting, agent_tool_calls.AgentToolCallWaiting)
        assert waiting.requirement.tool_execution.tool_args == {"value": 3}
    assert seen == [1]
    catalog = await _catalog(tmp_path, [_function(action, stop_after_tool_call=True)])
    events = await _events(catalog, "actions", "action", {"value": 4})
    assert events[-1].kind == "continuation_required"
    assert events[-1].call.result == "4"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("gate", "paused"),
    [
        pytest.param(ToolGate.CONFIRM, (True, None, None), id="confirm"),
        pytest.param(ToolGate.POLICY, (True, "mindroom_policy", None), id="policy"),
        pytest.param(ToolGate.DELEGATION, (None, None, True), id="delegation"),
    ],
)
async def test_a_waiting_cli_call_stores_how_its_function_s_gate_pauses_it(
    tmp_path: Path,
    gate: ToolGate,
    paused: tuple[object, ...],
) -> None:
    """A CLI call stores the confirmation, approval type, and external execution its function's gate gives."""

    def action(value: int) -> int:
        return value

    catalog = await _catalog(tmp_path, [_function(action, gate=gate)])
    [waiting] = await _events(catalog, "actions", "action", {"value": 3})

    assert isinstance(waiting, agent_tool_calls.AgentToolCallWaiting)
    stored = waiting.requirement.tool_execution.to_dict()
    keys = ("requires_confirmation", "approval_type", "external_execution_required")
    # A field left unset is not stored, and reads as None.
    assert (*(stored.get(key) for key in keys), stored["tool_args"]) == (*paused, {"value": 3})


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
    assert (await _events(catalog, "one", "action"))[-1].call.result == "ok"
    assert (await _events(catalog, "two", "other"))[-1].call.result == "other"
    with pytest.raises(UnknownToolError):
        await catalog.bind(ToolKey("two", "action"))
    with pytest.raises(ValueError, match=r"^Duplicate qualified tool: one\.action$"):
        await catalog.prepare([_toolkit("one", action)])
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
    first, second = await asyncio.gather(catalog.bind(key), catalog.bind(key))
    assert first is second
    assert first.instructions == ("Actual lazy instructions",)
    assert builds == [catalog.runtime_context]
    assert (await _events(catalog, "lazy", "action", {"value": 9}))[-1].call.result == "9"


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
    assert (await _events(catalog, "server", "lookup", {"term": "x"}))[-1].call.result == "x"
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
@pytest.mark.parametrize("close_fails", [False, True], ids=["closes", "close-fails"])
async def test_closed_catalog_is_freed_without_a_garbage_collection(tmp_path: Path, close_fails: bool) -> None:
    """A closed catalog holds no reference cycle, even when a toolkit fails to close, so dropping it frees it at once."""

    def action() -> str:
        return "ok"

    async def connect() -> Callable[[], Awaitable[None]]:
        async def close() -> None:
            if close_fails:
                msg = "close failed"
                raise RuntimeError(msg)

        return close

    toolkit = replace(_toolkit("existing", action), connect=connect)
    catalog = await _catalog(tmp_path, [toolkit])
    await catalog.connect([toolkit])
    with pytest.raises(RuntimeError, match="close failed") if close_fails else nullcontext():
        await catalog.close()
    released = weakref.ref(catalog)
    with gc_paused():
        del catalog
        assert released() is None


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
    started.call.tool_args["value"] = 3
    events = [event async for event in stream]
    assert events[-1].call.result == "1"
    assert events[-1].call.tool_args == {"value": 1}
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
async def test_a_brought_in_function_answers_to_its_own_tool_and_is_approved_under_the_one_bringing_it(
    tmp_path: Path,
) -> None:
    """A function another tool brings in is described under the tool it comes from, with that toolkit's guidance.

    Its approval names the tool that brings it in, which finds the same binding.
    """

    def action() -> str:
        return "ok"

    catalog = await _catalog(
        tmp_path,
        [_toolkit("implementation", action, instructions="Source guidance")],
        owners={"action": "authored_alias"},
    )
    binding = await catalog.bind(ToolKey("implementation", "action"))
    assert binding.instructions == ("Source guidance",)
    assert binding.approval_toolkit == "authored_alias"
    assert catalog.approved_binding("authored_alias", "action") is binding
    with pytest.raises(UnknownToolError, match="No toolkit 'authored_alias'"):
        await catalog.bind(ToolKey("authored_alias", "action"))
    with pytest.raises(UnknownToolError):
        catalog.approved_binding("implementation", "action")


@pytest.mark.asyncio
async def test_a_call_that_returns_a_stop_controls_only_its_own_invocation(tmp_path: Path) -> None:
    """A call that asks to stop the run is a continuation; the prepared definition keeps running normally."""

    def stop() -> ToolOutcome:
        return ToolOutcome(content="stopped", stop_after_call=True)

    catalog = await _catalog(tmp_path, [_toolkit("control", stop)])
    binding = await catalog.bind(ToolKey("control", "stop"))
    events = await _events(catalog, "control", "stop")
    assert events[-1].kind == "continuation_required"
    assert events[-1].call.result == "stopped"
    assert binding.function.stop_after_call is False


@pytest.mark.asyncio
async def test_generator_failure_is_terminal_and_does_not_leak_context(tmp_path: Path) -> None:
    """Partial generator output cannot hide a failed leaf or retain runtime state."""

    async def broken() -> AsyncIterator[str]:
        yield "partial"
        message = "generator failed"
        raise RuntimeError(message)

    catalog = await _catalog(tmp_path, [_toolkit("stream", broken)])
    events = await _events(catalog, "stream", "broken")
    assert (events[-1].kind, events[-1].call.is_error) == ("completed", True)
    assert get_tool_runtime_context() is None


@pytest.mark.asyncio
async def test_a_deferred_toolkit_whose_functions_collide_is_refused(tmp_path: Path) -> None:
    """A materialized toolkit that names one function twice binds nothing."""

    def duplicate() -> str:
        return "first"

    catalog = await _catalog(tmp_path, [])
    function = adapt_callable(duplicate)

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

    async def mutate() -> str:
        seen.append("mutate")
        current_call().session_state["changed"] = True
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
        assert events[-1].call.result == "changed"
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
    assert events[-1].call.result == "shell done"
    assert seen == ["before", "mutate", "after"]
    assert catalog.run.session_state["changed"] is True


@pytest.mark.asyncio
async def test_only_the_canonical_shell_function_takes_a_shell_leaf(tmp_path: Path) -> None:
    """Replacing the leaf of another shell call would run the owner's shell command in its place."""

    async def check_shell_command(args: str) -> str:
        return args

    catalog = await _catalog(tmp_path, [_toolkit("shell", check_shell_command)])
    binding = await catalog.bind(ToolKey("shell", "check_shell_command"))

    async def leaf(arguments: dict[str, object]) -> str:
        del arguments
        pytest.fail("the leaf of a call that is not the shell's command must never run")

    with pytest.raises(ValueError, match="canonical shell function"):
        agent_tool_calls.execute_agent_shell_call(binding, "call", {}, shell_leaf=leaf)


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
@pytest.mark.parametrize(
    ("approved", "reason", "denial"),
    [(True, None, None), (False, "Denied", "Denied"), (False, None, "Not approved by requester")],
)
async def test_exact_native_confirmation_resumes_prepared_call(
    tmp_path: Path,
    approved: bool,
    reason: str | None,
    denial: str | None,
) -> None:
    """Only the exact resolved native requirement releases a prepared call; a denial fails it with its note."""
    seen = []

    def action(value: int) -> int:
        seen.append(value)
        return value

    catalog = await _catalog(tmp_path, [_function(action, requires_confirmation=True)])
    binding = await catalog.bind(ToolKey("actions", "action"))
    [waiting] = await _events(catalog, "actions", "action", {"value": 3})
    assert isinstance(waiting, agent_tool_calls.AgentToolCallWaiting)
    requirement = waiting.requirement
    if approved:
        requirement.confirm()
    else:
        requirement.reject(reason)
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
    assert (resumed[-1].kind, resumed[-1].call.is_error) == ("completed", not approved)
    assert approved or resumed[-1].call.result == denial
    assert binding.function.gate is ToolGate.CONFIRM
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
@pytest.mark.parametrize(
    ("paused", "now", "runs"),
    [
        (ToolGate.POLICY, ToolGate.DELEGATION, False),
        (ToolGate.POLICY, ToolGate.CONFIRM, False),
        (ToolGate.CONFIRM, ToolGate.POLICY, False),
        (ToolGate.POLICY, ToolGate.POLICY, True),
        (ToolGate.POLICY, ToolGate.JOB, True),
        (ToolGate.POLICY, None, True),
    ],
)
async def test_resolved_confirmation_runs_only_while_its_function_confirms_as_it_paused_or_not_at_all(
    tmp_path: Path,
    paused: ToolGate,
    now: ToolGate | None,
    *,
    runs: bool,
) -> None:
    """Approval cannot authorize a function whose pause changed before prepared dispatch to another kind of wait."""
    effects = []

    def action() -> str:
        effects.append(True)
        return "executed"

    catalog = await _catalog(tmp_path, [_function(action, gate=paused)])
    requirement = (await _events(catalog, "actions", "action"))[-1].requirement
    requirement.confirm()
    # The resumed run prepares its tools again, now with the function's current gate.
    resumed = await _catalog(tmp_path, [_function(action, gate=now)])
    binding = await resumed.bind(ToolKey("actions", "action"))

    async def dispatch() -> list[object]:
        return [
            event
            async for event in agent_tool_calls.execute_agent_tool_call(
                binding,
                "inner-id",
                {},
                requirement=requirement,
            )
        ]

    if runs:
        await dispatch()
    else:
        with pytest.raises(ValueError, match="exact"):
            await dispatch()
    assert effects == ([True] if runs else [])


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
async def test_a_prepared_cli_call_survives_the_placement_its_subagent_turn_adds_to_its_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A target that keeps a subagent turn's own worker still matches the one prepared before the turn placed it."""

    def action() -> str:
        return "executed"

    catalog = await _catalog(tmp_path, [_function(action)])
    config = Config(agents={"helper": AgentConfig(display_name="Helper", tools=["shell"])})
    runtime = replace(catalog.runtime_context, config=config, config_provider=lambda: config)
    catalog.runtime_context = runtime
    binding = await catalog.bind(ToolKey("actions", "action"))
    monkeypatch.setattr(approval_tools, "is_sender_allowed_for_entity_replies_in_room", lambda *_a, **_k: True)
    expected = runtime.resolve_worker_target()

    with dedicated_worker_placement(runtime.session_id, DedicatedWorkerPlacement("c" * 32)):
        assert runtime.resolve_worker_target().placement == DedicatedWorkerPlacement("c" * 32)
        await approval_tools.authorize_prepared_tool_call(binding, expected_worker_target=expected)


def _remove_helper(config: Config) -> None:
    del config.agents["helper"]


def _personal_instance(user_id: str) -> PersonalAgentInstance:
    return PersonalAgentInstance(template="assistant", user_id=user_id, matrix_username=None)


# Each live config edit, and whether a prepared CLI call of `helper` stays permitted after it.
_CONFIG_EDITS: dict[str, tuple[Callable[[Config], object], bool]] = {
    "other_agent_instructions": (
        lambda config: setattr(config.agents["other"], "instructions", ["Be thorough and kind."]),
        True,
    ),
    "own_instructions": (lambda config: setattr(config.agents["helper"], "instructions", ["Be very brief."]), True),
    "own_model": (lambda config: setattr(config.agents["helper"], "model", "other-model"), True),
    "config_file_edited": (lambda config: setattr(config, "_source_fingerprint", "edited"), True),
    "own_tools": (lambda config: setattr(config.agents["helper"], "tools", []), False),
    "own_access": (lambda config: setattr(config.agents["helper"], "allow_self_config", True), False),
    "own_graph_edits": (lambda config: setattr(config.agents["helper"], "knowledge_graph_edits", True), False),
    "approval_default": (lambda config: setattr(config.tool_approval, "default", "require_approval"), False),
    "approved_egress_enabled": (
        lambda config: setattr(config, "_approved_egress_enabled", True),
        False,
    ),
    "other_personal_agent_instance": (
        lambda config: setattr(config, "_personal_agent_instances", {"other": _personal_instance("@bob:test")}),
        True,
    ),
    "own_personal_agent_instance": (
        lambda config: setattr(config, "_personal_agent_instances", {"helper": _personal_instance("@bob:test")}),
        False,
    ),
    "own_removal": (_remove_helper, False),
    # Model entries shape model requests and prompts, never a toolkit or its permissions.
    "model_added": (lambda config: config.models.__setitem__("extra", ModelConfig(provider="openai", id="x")), True),
    "model_removed": (lambda config: config.models.pop("spare"), True),
    "model_renamed": (lambda config: config.models.__setitem__("renamed", config.models.pop("spare")), True),
    "model_display_name": (lambda config: setattr(config.models["default"], "display_name", "Fast"), True),
    "model_pricing": (
        lambda config: setattr(config.models["default"], "pricing", ModelPricing(input=1, output=2)),
        True,
    ),
    "room_model_added": (lambda config: config.room_models.__setitem__("lobby", "spare"), True),
    "own_room_appended": (lambda config: config.agents["helper"].rooms.append("new-room"), True),
    "own_room_removed": (lambda config: config.agents["helper"].rooms.remove("lobby"), False),
    "memory_search_exclude": (lambda config: config.memory.search.exclude.append("archive/**"), False),
    "default_tools": (lambda config: setattr(config.defaults, "tools", ["calculator"]), False),
    "default_worker_routing": (lambda config: setattr(config.defaults, "worker_scope", "user"), False),
    "approval_rule": (
        lambda config: setattr(
            config.tool_approval,
            "rules",
            [ApprovalRuleConfig(match="action", action="require_approval")],
        ),
        False,
    ),
    "approval_rule_script": (
        lambda config: setattr(
            config.tool_approval,
            "rules",
            [ApprovalRuleConfig(match="act*", script="approval_other_agent_only.py")],
        ),
        False,
    ),
    "approval_rule_shell": (
        lambda config: setattr(
            config.tool_approval,
            "rules",
            [ApprovalRuleConfig(match="run_shell_command", action="require_approval")],
        ),
        False,
    ),
    "approval_timeout_days": (lambda config: setattr(config.tool_approval, "timeout_days", 1.0), False),
    "unrelated_approval_rules": (
        lambda config: setattr(
            config.tool_approval,
            "rules",
            [
                ApprovalRuleConfig(match="google_drive_write", script="approval_other_agent_only.py"),
                ApprovalRuleConfig(match="google_sheets_write", script="approval_other_agent_only.py"),
            ],
        ),
        True,
    ),
    "calls_profile_model": (lambda config: setattr(config.calls.profiles["live"], "model", "gpt-live-1.1"), True),
    "other_personal_template_compaction": (
        lambda config: setattr(
            config.personal_agents["mind"].agent,
            "compaction",
            CompactionOverrideConfig(replay_window_tokens=32000),
        ),
        True,
    ),
    "thread_summary_model": (lambda config: setattr(config.defaults, "thread_summary_model", "spare"), True),
    "room_thread_summary_model": (lambda config: config.room_thread_summary_models.__setitem__("lobby", "spare"), True),
    "router_model": (lambda config: setattr(config.router, "model", "spare"), True),
    "defaults_compaction": (
        lambda config: setattr(config.defaults, "compaction", CompactionConfig(replay_window_tokens=32000)),
        False,
    ),
    "external_trigger_policy": (
        lambda config: setattr(config.external_trigger_policy, "enabled", not config.external_trigger_policy.enabled),
        False,
    ),
    "mcp_server": (
        lambda config: config.mcp_servers.__setitem__("demo", MCPServerConfig(transport="stdio", command="npx")),
        False,
    ),
    "voice_stt_host": (lambda config: setattr(config.voice.stt, "host", "http://127.0.0.1:10301"), True),
    "debug_log_llm_requests": (lambda config: setattr(config.debug, "log_llm_requests", True), True),
    "prompts_override": (
        lambda config: config.prompts.__setitem__("CURRENT_MESSAGE_PROMPT_INTRO", "Next:\n"),
        True,
    ),
    "matrix_space_name": (lambda config: setattr(config.matrix_space, "name", "Team Space"), True),
    "server_emoji_enabled": (lambda config: setattr(config.server_emoji, "enabled", True), True),
    "event_journal_backend": (lambda config: setattr(config.event_journal, "backend", "postgres"), True),
    "budgets": (
        lambda config: setattr(config, "budgets", BudgetsConfig(monthly_limit_usd=25.0, fallback_model="spare")),
        False,
    ),
    "slack_identity_mapping": (
        lambda config: setattr(
            config,
            "slack",
            SlackConfig(email_domain="example.com", matrix_user_id_template="@{localpart}:test"),
        ),
        True,
    ),
    "tailnet_ssh_logins": (
        lambda config: config.tailnet_ssh.logins.__setitem__("@alice:test", "alice@example.com"),
        True,
    ),
    "scheduler_catch_up_grace_seconds": (
        lambda config: setattr(config, "scheduler_catch_up_grace_seconds", 600),
        True,
    ),
    "email_invites_approvers": (lambda config: config.email_invites.approvers.append("@alice:test"), False),
    "personal_rooms": (
        lambda config: setattr(
            config,
            "personal_rooms",
            PersonalRoomsConfig(agent="helper", onboarding_rooms=["lobby"]),
        ),
        False,
    ),
    "matrix_sync_mode": (lambda config: setattr(config.matrix_sync, "mode", "sliding"), False),
    "own_tailnet_ssh": (lambda config: setattr(config.agents["helper"], "tailnet_ssh", True), False),
}


@pytest.mark.asyncio
@pytest.mark.parametrize("edit", _CONFIG_EDITS)
async def test_a_prepared_cli_call_survives_config_edits_that_leave_its_agent_unchanged(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    edit: str,
) -> None:
    """A live reload refuses a prepared CLI call only when it changed what its own agent is given."""

    def action() -> str:
        return "executed"

    catalog = await _catalog(tmp_path, [_function(action)])
    config = Config(
        agents={
            "helper": AgentConfig(display_name="Helper", tools=["shell"], instructions=["Be brief."], rooms=["lobby"]),
            "other": AgentConfig(display_name="Other", instructions=["Be thorough."]),
        },
        models={
            "default": ModelConfig(provider="openai", id="default-id"),
            "spare": ModelConfig(provider="openai", id="spare-id"),
        },
        calls=CallsConfig(
            profiles={
                "live": LiveCallProfile(
                    backend="live",
                    model="gpt-live-1",
                    credentials_service="openai",
                    voice="alloy",
                ),
            },
        ),
        personal_agents={
            "mind": PersonalAgentTemplateConfig(
                agent=AgentConfig(
                    display_name="Mind",
                    compaction=CompactionOverrideConfig(replay_window_tokens=16000),
                ),
            ),
        },
    )
    current = config.model_copy(deep=True)
    apply_edit, still_permitted = _CONFIG_EDITS[edit]
    apply_edit(current)
    runtime = replace(catalog.runtime_context, config=config, config_provider=lambda: current)
    catalog.runtime_context = runtime
    binding = await catalog.bind(ToolKey("actions", "action"))
    monkeypatch.setattr(
        approval_tools,
        "is_sender_allowed_for_entity_replies_in_room",
        lambda *_args, **_kwargs: True,
    )
    authorize = approval_tools.authorize_prepared_tool_call(
        binding,
        expected_worker_target=runtime.resolve_worker_target(),
    )
    if still_permitted:
        await authorize
    else:
        with pytest.raises(PermissionError):
            await authorize


@pytest.mark.asyncio
async def test_per_call_approval_and_own_personal_template_stay_fail_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Alias targets and a personal agent's own template still fail closed; scheduler runner skips its own name."""

    def action() -> str:
        return "ok"

    def run_scheduled_call() -> str:
        return "ok"

    catalog = await _catalog(tmp_path, [_function(action), _toolkit("scheduler", run_scheduled_call)])
    config = Config(
        agents={"helper": AgentConfig(display_name="Helper", rooms=["lobby"])},
        personal_agents={
            "assistant": PersonalAgentTemplateConfig(agent=AgentConfig(display_name="Assistant")),
            "other": PersonalAgentTemplateConfig(agent=AgentConfig(display_name="Other")),
        },
    )
    config._personal_agent_instances = {"helper": _personal_instance("@alice:test")}
    alias_meta = ToolMetadata(
        name="actions",
        file_access=ToolFileAccess.NONE,
        display_name="Actions",
        description="Actions",
        category=ToolCategory.DEVELOPMENT,
        approval_aliases={"action": ApprovalAlias(judged_as="judged_target", judge=lambda _args: [])},
    )
    default_tools = ToolRegistry({"actions": alias_meta})
    published_tools = default_tools
    monkeypatch.setattr(
        approval_tools,
        "config_plugins",
        lambda *_args, **_kwargs: SimpleNamespace(tools=published_tools),
    )
    monkeypatch.setattr(
        approval_tools,
        "is_sender_allowed_for_entity_replies_in_room",
        lambda *_args, **_kwargs: True,
    )

    def bind_reload(
        mutate: Callable[[Config], object],
        *,
        old_tools: ToolRegistry = default_tools,
        current_tools: ToolRegistry = default_tools,
    ) -> ToolRuntimeContext:
        nonlocal published_tools
        published_tools = current_tools
        current = config.model_copy(deep=True)
        mutate(current)
        runtime = replace(
            catalog.runtime_context,
            config=config,
            plugins=cast("Any", SimpleNamespace(tools=old_tools)),
            config_provider=lambda: current,
        )
        catalog.runtime_context = runtime
        return runtime

    # A rule matching the alias's judged_as target refuses.
    runtime = bind_reload(
        lambda cfg: setattr(cfg.tool_approval, "rules", [ApprovalRuleConfig(match="judged_*", script="gate.py")]),
    )
    action_binding = await catalog.bind(ToolKey("actions", "action"))
    with pytest.raises(PermissionError):
        await approval_tools.authorize_prepared_tool_call(
            action_binding,
            expected_worker_target=runtime.resolve_worker_target(),
        )

    # An alias published only in the reloaded plugin registry + rule on its judged_as target refuses.
    new_alias_meta = replace(
        alias_meta,
        approval_aliases={"action": ApprovalAlias(judged_as="upload_file", judge=lambda _args: [])},
    )
    runtime = bind_reload(
        lambda cfg: setattr(
            cfg.tool_approval,
            "rules",
            [ApprovalRuleConfig(match="upload_file", action="require_approval")],
        ),
        old_tools=ToolRegistry({}),
        current_tools=ToolRegistry({"actions": new_alias_meta}),
    )
    with pytest.raises(PermissionError):
        await approval_tools.authorize_prepared_tool_call(
            action_binding,
            expected_worker_target=runtime.resolve_worker_target(),
        )

    # A republished alias with a new judged_as target refuses even when both targets' policies are equal.
    runtime = bind_reload(
        lambda cfg: setattr(
            cfg.tool_approval,
            "rules",
            [ApprovalRuleConfig(match="judged_target", action="require_approval")],
        ),
        current_tools=ToolRegistry({"actions": new_alias_meta}),
    )
    with pytest.raises(PermissionError):
        await approval_tools.authorize_prepared_tool_call(
            action_binding,
            expected_worker_target=runtime.resolve_worker_target(),
        )

    # scheduler.run_scheduled_call ignores rules matching its own name, consistent with _approval_may_gate.
    runtime = bind_reload(
        lambda cfg: setattr(
            cfg.tool_approval,
            "rules",
            [ApprovalRuleConfig(match="run_scheduled_call", action="require_approval")],
        ),
    )
    sched_binding = await catalog.bind(ToolKey("scheduler", "run_scheduled_call"))
    await approval_tools.authorize_prepared_tool_call(
        sched_binding,
        expected_worker_target=runtime.resolve_worker_target(),
    )

    # Editing another personal template survives; editing this agent's own template refuses.
    runtime = bind_reload(lambda cfg: setattr(cfg.personal_agents["other"], "workspace_template", None))
    await approval_tools.authorize_prepared_tool_call(
        action_binding,
        expected_worker_target=runtime.resolve_worker_target(),
    )
    runtime = bind_reload(lambda cfg: setattr(cfg.personal_agents["assistant"], "workspace_template", None))
    with pytest.raises(PermissionError):
        await approval_tools.authorize_prepared_tool_call(
            action_binding,
            expected_worker_target=runtime.resolve_worker_target(),
        )


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
        current.agents["helper"].tools = ["shell"]
        catalog.runtime_context = replace(runtime, config_provider=lambda: current)
    if revocation == "forged":

        def arbitrary() -> str:
            return "bad"

        binding = replace(binding, function=adapt_callable(arbitrary))
    if revocation == "stale":
        await catalog.close()
    if revocation:
        with pytest.raises(PermissionError):
            await approval_tools.authorize_prepared_tool_call(binding, expected_worker_target=target)
    else:
        await approval_tools.authorize_prepared_tool_call(binding, expected_worker_target=target)


@pytest.mark.asyncio
async def test_cli_tool_call_allows_explicit_null_only_for_derived_nullable_optionals(tmp_path: Path) -> None:
    """CLI execution admits explicit null for derived ``X | None = None`` and rejects it for non-nullable/authored."""

    def derived_lookup(query: str, tag: str | None = None, count: int = 5) -> str:
        return f"{query}:{tag}:{count}"

    authored_schema = {
        "type": "object",
        "properties": {"query": {"type": "string"}, "tag": {"type": "string"}},
        "required": ["query"],
    }

    @passthrough_tool(name="authored_lookup", parameters=authored_schema)
    def authored_lookup(query: str, tag: str | None = None) -> str:
        return f"{query}:{tag}"

    catalog = await _catalog(tmp_path, [_toolkit("notes", derived_lookup, authored_lookup)])

    async def run_one(name: str, arguments: dict[str, object]) -> agent_tool_calls.AgentToolCallStep:
        events = await _events(catalog, "notes", name, arguments)
        return next(
            event
            for event in events
            if isinstance(event, agent_tool_calls.AgentToolCallStep) and event.kind == "completed"
        )

    ok = await run_one("derived_lookup", {"query": "hi", "tag": None})
    assert not ok.call.is_error
    assert ok.call.result == "hi:None:5"

    non_nullable = await run_one("derived_lookup", {"query": "hi", "count": None})
    assert non_nullable.call.is_error
    assert non_nullable.call.result == "Invalid tool arguments"

    authored_null = await run_one("authored_lookup", {"query": "hi", "tag": None})
    assert authored_null.call.is_error
    assert authored_null.call.result == "Invalid tool arguments"

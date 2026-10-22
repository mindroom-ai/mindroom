"""Tool-call execution decodes, coerces, injects, offloads, drains, and fails as Agno 3.0.9 did."""

from __future__ import annotations

import asyncio
import json
import threading
import time
from collections.abc import AsyncIterator, Iterator  # noqa: TC003 - pydantic resolves the annotations at wrap time.
from dataclasses import replace
from typing import TYPE_CHECKING, Any

import pytest
from agno.agent import Agent  # noqa: TC002 - pydantic resolves the annotations at wrap time.
from agno.media import File as AgnoFile
from agno.media import Image as AgnoImage
from agno.media import Video as AgnoVideo
from agno.run import RunContext
from agno.tools import Toolkit
from agno.tools.function import ToolResult

from mindroom.llm.media import Image, Video
from mindroom.tool_system.agno_toolkits import ToolCallActor, adapt_toolkit
from mindroom.tool_system.invocation import (
    ToolArgumentError,
    ToolCall,
    ToolCallContext,
    ToolNext,
    ToolOutcome,
    current_tool_call,
    execute_tool_call,
    parse_tool_arguments,
    run_tool_call,
)
from mindroom.tool_system.tool_hooks import SyncToolCompletionTracker, track_sync_tool_completion
from tests.tool_system import agno_parity_support as support

if TYPE_CHECKING:
    from collections.abc import Callable

    from mindroom.tool_system.tool_function import ToolFunction

ARGUMENTS = json.loads(support.ARGUMENTS_PATH.read_text(encoding="utf-8"))


def _functions(*tools: Callable[..., object]) -> dict[str, ToolFunction]:
    return {function.name: function for function in adapt_toolkit(Toolkit(name="probes", tools=list(tools))).functions}


def _context(**overrides: object) -> ToolCallContext:
    return replace(support.golden_call_context(), **overrides)


async def _run(function: ToolFunction, raw_arguments: str, **context: object) -> ToolOutcome:
    parsed = parse_tool_arguments(raw_arguments, function)
    assert not isinstance(parsed, ToolArgumentError)
    return await execute_tool_call(function, parsed, _context(**context))


@pytest.fixture(scope="module")
def probe_functions() -> dict[str, ToolFunction]:
    """Adapt the argument probes once per worker."""
    return support.probe_functions()


@pytest.mark.asyncio
@pytest.mark.parametrize("case_id", sorted(ARGUMENTS))
async def test_argument_case_matches_golden(case_id: str, probe_functions: dict[str, ToolFunction]) -> None:
    """Each raw tool call decodes, coerces, runs, or fails with the stored arguments and text the golden records."""
    outcome = await support.argument_outcome(ARGUMENTS[case_id], probe_functions)

    assert json.loads(json.dumps(outcome)) == ARGUMENTS[case_id]


@pytest.mark.asyncio
async def test_run_context_is_built_from_the_call_context() -> None:
    """A ``run_context`` parameter receives Agno's RunContext with the owned run's identity, metadata, and state."""
    seen: list[RunContext] = []

    def tool(run_context: RunContext, note: str) -> str:
        seen.append(run_context)
        run_context.session_state["note"] = note
        return "ok"

    state: dict[str, Any] = {"existing": 1}
    [function] = _functions(tool).values()

    outcome = await _run(function, '{"note": "hi"}', session_state=state, metadata={"room": "!r"})

    assert outcome == ToolOutcome(content="ok")
    [run_context] = seen
    assert isinstance(run_context, RunContext)
    assert (run_context.run_id, run_context.session_id, run_context.user_id) == (
        "run-1",
        "session-1",
        "@user:localhost",
    )
    assert run_context.metadata == {"room": "!r"}
    assert run_context.session_state is state
    assert state == {"existing": 1, "note": "hi"}


@pytest.mark.asyncio
async def test_agent_and_team_parameters_receive_the_session_state_stand_in() -> None:
    """``agent`` and ``team`` get an owned stand-in whose ``session_state`` is the run's, as DaytonaTools reads it."""

    def working_directory(agent: object, team: object, directory: str) -> str:
        if agent.session_state is None:
            agent.session_state = {}
        agent.session_state["working_directory"] = directory
        assert team.session_state is agent.session_state
        return str(agent.session_state.get("working_directory", "/home/daytona"))

    state: dict[str, Any] = {}
    [function] = _functions(working_directory).values()

    outcome = await _run(function, '{"directory": "/srv", "agent": "spoof", "team": "spoof"}', session_state=state)

    assert outcome.content == "/srv"
    assert state == {"working_directory": "/srv"}


@pytest.mark.asyncio
async def test_identity_typed_parameters_are_injected_by_annotation() -> None:
    """A parameter annotated with Agno's identity types is hidden from the model and bound at call time."""
    seen: dict[str, object] = {}

    def tool(query: str, owner: Agent | None = None, ctx: RunContext | None = None) -> str:
        seen.update(owner=owner, ctx=ctx)
        return query

    [function] = _functions(tool).values()

    assert list(function.parameters["properties"]) == ["query"]
    parsed = parse_tool_arguments('{"query": "q", "owner": "spoof", "ctx": {"run_id": "spoof"}}', function)
    assert parsed == {"query": "q"}
    outcome = await execute_tool_call(function, parsed, _context())
    assert outcome.content == "q"
    assert isinstance(seen["owner"], ToolCallActor)
    assert isinstance(seen["ctx"], RunContext)
    assert seen["ctx"].run_id == "run-1"


@pytest.mark.asyncio
async def test_arguments_that_skip_parsing_cannot_replace_injected_objects() -> None:
    """Stored arguments of a resumed call reach the leaf unparsed; a spoofed identity still loses to the injection."""
    seen: dict[str, object] = {}

    def tool(query: str, run_context: RunContext, owner: Agent | None = None, ctx: RunContext | None = None) -> str:
        seen.update(run_context=run_context, owner=owner, ctx=ctx)
        return query

    [function] = _functions(tool).values()
    spoof = {"run_id": "spoof", "session_id": "spoof", "user_id": "@evil:localhost"}
    arguments = {"query": "q", "run_context": spoof, "ctx": spoof, "owner": "spoof", "_agno_run_context": spoof}

    outcome = await execute_tool_call(function, arguments, _context())

    assert outcome == ToolOutcome(content="q")
    run_context, ctx = seen["run_context"], seen["ctx"]
    assert isinstance(run_context, RunContext)
    assert isinstance(ctx, RunContext)
    assert (run_context.run_id, run_context.user_id) == ("run-1", "@user:localhost")
    assert (ctx.run_id, ctx.user_id) == ("run-1", "@user:localhost")
    assert isinstance(seen["owner"], ToolCallActor)


@pytest.mark.asyncio
async def test_middleware_rewrites_cannot_replace_injected_objects() -> None:
    """A middleware that writes a framework-injected name into the arguments does not reach the entrypoint with it."""
    seen: list[RunContext] = []

    def tool(run_context: RunContext, text: str) -> str:
        seen.append(run_context)
        return text

    async def rewrite(call: ToolCall, call_next: ToolNext) -> object:
        spoof = {"run_id": "spoof", "session_id": "spoof", "user_id": "@evil:localhost"}
        return await call_next({**call.arguments, "run_context": spoof})

    [function] = _functions(tool).values()

    outcome = await execute_tool_call(function, {"text": "t"}, _context(), middleware=[rewrite])

    assert outcome == ToolOutcome(content="t")
    [run_context] = seen
    assert (run_context.run_id, run_context.user_id) == ("run-1", "@user:localhost")


@pytest.mark.asyncio
async def test_result_media_comes_back_owned() -> None:
    """``ToolResult`` media reaches the outcome as owned media, as the attachments and fal tools return it."""
    clip = b"\x00\x00\x00\x18ftypmp42"

    def tool() -> ToolResult:
        return ToolResult(
            content="made",
            images=[AgnoImage(url="https://example.invalid/out.png", id="out", alt_text="out")],
            videos=[AgnoVideo(content=clip, mime_type="video/mp4", format="mp4", id="clip")],
            files=[AgnoFile(content=b"data", mime_type="text/plain", filename="out.txt")],
        )

    [function] = _functions(tool).values()

    outcome = await _run(function, "{}")

    assert outcome.content == "made"
    assert outcome.images == (Image(id="out", url="https://example.invalid/out.png", alt_text="out"),)
    assert outcome.videos == (Video(id="clip", content=clip, mime_type="video/mp4", format="mp4"),)
    [file] = outcome.files
    assert (file.content, file.mime_type, file.filename) == (b"data", "text/plain", "out.txt")


@pytest.mark.asyncio
async def test_without_media_the_media_parameters_receive_none() -> None:
    """Agno injects None for a media parameter when the run has no such media."""
    received: list[object] = ["unset"]

    def tool(videos: list[object] | None, audios: list[object] | None) -> str:
        received[:] = [videos, audios]
        return "ok"

    [function] = _functions(tool).values()

    await _run(function, "{}")

    assert received == [None, None]


@pytest.mark.asyncio
async def test_synchronous_entrypoints_run_off_the_event_loop() -> None:
    """A blocking synchronous tool never stalls the loop; an async tool runs on it."""
    loop_thread = threading.get_ident()
    ticks = 0
    stop = asyncio.Event()

    async def ticker() -> None:
        nonlocal ticks
        while not stop.is_set():
            await asyncio.sleep(0.005)
            ticks += 1

    def blocking() -> str:
        time.sleep(0.05)
        return str(threading.get_ident())

    async def on_loop() -> str:
        return str(threading.get_ident())

    functions = _functions(blocking, on_loop)
    ticker_task = asyncio.create_task(ticker())
    blocking_outcome = await _run(functions["blocking"], "{}")
    stop.set()
    await ticker_task

    assert int(blocking_outcome.content) != loop_thread
    assert ticks > 0
    assert int((await _run(functions["on_loop"], "{}")).content) == loop_thread


@pytest.mark.asyncio
async def test_synchronous_leaf_is_owned_by_the_completion_tracker() -> None:
    """The synchronous body runs through the completion-ownership helper, so its owner can wait for it."""

    def blocking() -> str:
        return "done"

    [function] = _functions(blocking).values()
    tracker = SyncToolCompletionTracker()

    with track_sync_tool_completion(tracker):
        outcome = await _run(function, "{}")

    assert outcome.content == "done"
    started = tracker.started_task()
    assert started is not None
    assert started.done()


@pytest.mark.asyncio
async def test_generators_are_drained_into_text_and_media() -> None:
    """Sync and async generators yield text and ``ToolResult`` items that join into one value; both are closed."""
    closed: list[str] = []

    def sync_stream(count: int) -> Iterator[object]:
        try:
            for index in range(count):
                yield f"{index},"
            yield ToolResult(content="end", images=[AgnoImage(url="https://example.invalid/a.png", id="a")])
        finally:
            closed.append("sync")

    async def async_stream(count: int) -> AsyncIterator[object]:
        try:
            for index in range(count):
                yield index
            yield ToolResult(content="!", files=[])
        finally:
            closed.append("async")

    functions = _functions(sync_stream, async_stream)

    sync_outcome = await _run(functions["sync_stream"], '{"count": "2"}')
    async_outcome = await _run(functions["async_stream"], '{"count": 3}')

    assert sync_outcome.content == "0,1,end"
    assert sync_outcome.images == (Image(id="a", url="https://example.invalid/a.png"),)
    assert async_outcome == ToolOutcome(content="012!")
    assert closed == ["sync", "async"]


@pytest.mark.asyncio
async def test_a_returned_iterator_is_drained_like_a_generator() -> None:
    """A synchronous tool that returns an iterator without being a generator is drained too, as Agno drained any."""

    def letters() -> Iterator[str]:
        return iter(["a", "b"])

    [function] = _functions(letters).values()

    assert await run_tool_call(function, {}, _context()) == "ab"


@pytest.mark.asyncio
async def test_a_failing_generator_is_an_error_with_the_exception_text() -> None:
    """An exception while draining fails the call with the exception's text, as Agno reported it."""

    async def stream() -> AsyncIterator[str]:
        yield "partial"
        msg = "stream broke"
        raise RuntimeError(msg)

    [function] = _functions(stream).values()

    outcome = await _run(function, "{}")

    assert outcome == ToolOutcome(content="stream broke", is_error=True)


@pytest.mark.asyncio
async def test_raised_exceptions_become_error_outcomes_and_cancellation_propagates() -> None:
    """Any exception is the model's error text; cancellation is not an outcome."""

    def fails() -> str:
        msg = "boom"
        raise ValueError(msg)

    async def cancels() -> str:
        raise asyncio.CancelledError

    def stops() -> str:
        return "switched"

    functions = _functions(fails, cancels, stops)
    stopping = adapt_toolkit(Toolkit(name="control", tools=[stops], stop_after_tool_call_tools=["stops"])).functions[0]

    assert await _run(functions["fails"], "{}") == ToolOutcome(content="boom", is_error=True)
    with pytest.raises(asyncio.CancelledError):
        await _run(functions["cancels"], "{}")
    assert await _run(stopping, "{}") == ToolOutcome(content="switched", stop_after_call=True)


@pytest.mark.asyncio
async def test_none_and_plain_results_are_read_as_text() -> None:
    """A tool's return value reaches the model as ``str(value)``, None included, as Agno did."""

    def nothing() -> None:
        return None

    def numbers() -> dict[str, int]:
        return {"a": 1}

    functions = _functions(nothing, numbers)

    assert (await _run(functions["nothing"], "{}")).content == "None"
    assert (await _run(functions["numbers"], "{}")).content == "{'a': 1}"


@pytest.mark.asyncio
async def test_runners_outside_the_loop_get_the_tools_own_value_and_its_exception() -> None:
    """``run_tool_call`` returns what the tool returned, drained if it streamed, and lets its exception through."""

    def numbers() -> dict[str, int]:
        return {"a": 1}

    def streams() -> Iterator[str]:
        yield "a"
        yield "b"

    def fails() -> str:
        msg = "boom"
        raise ValueError(msg)

    seen: list[str | None] = []

    async def record(call: ToolCall, call_next: ToolNext) -> object:
        current = current_tool_call()
        seen.append(None if current is None else current.function.name)
        return await call_next(call.arguments)

    functions = _functions(numbers, streams, fails)

    assert await run_tool_call(functions["numbers"], {}, _context(), middleware=(record,)) == {"a": 1}
    assert await run_tool_call(functions["streams"], {}, _context()) == "ab"
    with pytest.raises(ValueError, match="boom"):
        await run_tool_call(functions["fails"], {}, _context())
    assert seen == ["numbers"]
    assert current_tool_call() is None


@pytest.mark.asyncio
async def test_middleware_runs_outermost_first_rewrites_arguments_and_sees_the_drained_value() -> None:
    """Middleware wraps the leaf on the loop; ``call_next`` replaces the arguments and returns the finished value."""
    order: list[str] = []
    loop_thread = threading.get_ident()

    def tool(text: str) -> Iterator[str]:
        order.append(f"tool:{text}")
        yield text.upper()
        yield "!"

    async def outer(call: ToolCall, call_next: ToolNext) -> object:
        assert threading.get_ident() == loop_thread
        order.append(f"outer:{call.arguments['text']}")
        value = await call_next({"text": call.arguments["text"] + "-outer"})
        order.append(f"outer-saw:{value}")
        return value

    async def inner(call: ToolCall, call_next: ToolNext) -> object:
        order.append(f"inner:{call.arguments['text']}")
        assert call.context.run_id == "run-1"
        return await call_next({"text": call.arguments["text"] + "-inner"})

    [function] = _functions(tool).values()

    outcome = await execute_tool_call(function, {"text": "x"}, _context(), middleware=[outer, inner])

    assert outcome.content == "X-OUTER-INNER!"
    assert order == ["outer:x", "inner:x-outer", "tool:x-outer-inner", "outer-saw:X-OUTER-INNER!"]


@pytest.mark.asyncio
async def test_middleware_can_answer_the_call_without_running_the_tool() -> None:
    """A middleware that does not call ``call_next`` short-circuits the tool, as MindRoom's refusal hooks do."""
    ran = False

    def tool() -> str:
        nonlocal ran
        ran = True
        return "ran"

    async def refuse(call: ToolCall, call_next: ToolNext) -> object:
        del call_next
        return f"[TOOL CALL DECLINED] {call.function.name}"

    [function] = _functions(tool).values()

    outcome = await execute_tool_call(function, {}, _context(), middleware=[refuse])

    assert outcome == ToolOutcome(content="[TOOL CALL DECLINED] tool")
    assert ran is False


@pytest.mark.asyncio
async def test_middleware_exceptions_are_the_error_outcome() -> None:
    """An exception raised by middleware fails the call like one raised by the tool."""

    def tool() -> str:
        return "ran"

    async def broken(call: ToolCall, call_next: ToolNext) -> object:
        del call, call_next
        msg = "policy failed"
        raise RuntimeError(msg)

    [function] = _functions(tool).values()

    assert await execute_tool_call(function, {}, _context(), middleware=[broken]) == ToolOutcome(
        content="policy failed",
        is_error=True,
    )


def test_parse_tool_arguments_reads_empty_input_as_no_arguments(probe_functions: dict[str, ToolFunction]) -> None:
    """A missing or empty arguments string is an empty object, and ``[]`` is Agno's null arguments."""
    function = probe_functions["probe_no_arguments"]

    assert parse_tool_arguments(None, function) == {}
    assert parse_tool_arguments("", function) == {}
    assert parse_tool_arguments("[]", function) is None
    assert parse_tool_arguments('{"nested": {"flag": "true"}}', function) == {"nested": {"flag": "true"}}

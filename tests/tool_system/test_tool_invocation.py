"""Tool-call execution decodes, coerces, injects, offloads, drains, and fails as Agno 3.0.9 did."""

from __future__ import annotations

import asyncio
import json
import threading
import time
from collections.abc import AsyncIterator, Iterator  # noqa: TC003 - pydantic resolves the annotations at wrap time.
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from typing import TYPE_CHECKING, Any

import pytest
from agno.agent import Agent  # noqa: TC002 - pydantic resolves the annotations at wrap time.
from agno.media import File as AgnoFile
from agno.media import Image as AgnoImage
from agno.media import Video as AgnoVideo
from agno.run import RunContext
from agno.tools import Toolkit
from agno.tools.function import Function, ToolResult

from mindroom.failure_text import failure_text
from mindroom.llm.media import Audio, File, Image, Video
from mindroom.tool_system.agno_toolkits import ToolCallActor, adapt_toolkit
from mindroom.tool_system.invocation import (
    CallResult,
    ToolArgumentError,
    ToolCall,
    ToolCallContext,
    ToolNext,
    client_tool_result,
    current_tool_call,
    execute_tool_call,
    parse_tool_arguments,
    run_tool_call,
)
from mindroom.tool_system.tool_function import ToolOutcome, ToolValue
from mindroom.tool_system.tool_hooks import (
    SyncToolCompletionTracker,
    owned_sync_tool_completion,
    run_sync_tool_entrypoint,
    track_sync_tool_completion,
)
from mindroom.tool_system.tool_schema import validated_entrypoint
from tests.agent_tool_helpers import offered_agno_function
from tests.conftest import SETTLE_TIMEOUT_SECONDS
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
    return await execute_tool_call(function, parsed, _context(**context), middleware=())


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
async def test_agent_and_team_parameters_share_the_toolkits_own_state_stand_in() -> None:
    """``agent`` and ``team`` get an owned stand-in whose ``session_state`` belongs to the adapted toolkit.

    The toolkit's functions share it across calls, as DaytonaTools keeps its sandbox and working directory there,
    and it is never the run's stored session state; a toolkit adapted again starts empty, as Agno's per-reply agent
    did.
    """

    def change_directory(agent: object, team: object, directory: str) -> str:
        assert team.session_state is agent.session_state
        agent.session_state["working_directory"] = directory
        return directory

    def working_directory(agent: object) -> str:
        return str(agent.session_state.get("working_directory", "/home/daytona"))

    state: dict[str, Any] = {}
    functions = _functions(change_directory, working_directory)

    await _run(
        functions["change_directory"],
        '{"directory": "/srv", "agent": "spoof", "team": "spoof"}',
        session_state=state,
    )
    outcome = await _run(functions["working_directory"], "{}", session_state={})

    assert outcome.content == "/srv"
    assert state == {}
    readapted = _functions(change_directory, working_directory)
    assert (await _run(readapted["working_directory"], "{}")).content == "/home/daytona"


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
    outcome = await execute_tool_call(function, parsed, _context(), middleware=())
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

    outcome = await execute_tool_call(function, arguments, _context(), middleware=())

    assert outcome == ToolOutcome(content="q")
    run_context, ctx = seen["run_context"], seen["ctx"]
    assert isinstance(run_context, RunContext)
    assert isinstance(ctx, RunContext)
    assert (run_context.run_id, run_context.user_id) == ("run-1", "@user:localhost")
    assert (ctx.run_id, ctx.user_id) == ("run-1", "@user:localhost")
    assert isinstance(seen["owner"], ToolCallActor)


@pytest.mark.asyncio
async def test_a_schema_may_give_the_model_a_media_name_but_never_an_identity_name() -> None:
    """A wrapper's schema may expose its wrapped tool's own ``files``; a declared ``agent`` still gets the stand-in."""
    seen: dict[str, object] = {}

    def tool(query: str, agent: object = None, files: list[str] | None = None) -> str:
        seen.update(agent=agent, files=files)
        return query

    schema = {
        "type": "object",
        "properties": {"query": {"type": "string"}, "agent": {"type": "string"}, "files": {"type": "array"}},
        "required": ["query"],
    }
    function = offered_agno_function(
        Function(name="tool", entrypoint=tool, parameters=schema, skip_entrypoint_processing=True),
    )

    outcome = await _run(function, '{"query": "q", "agent": "spoof", "files": ["notes.txt"]}')

    assert outcome == ToolOutcome(content="q")
    assert isinstance(seen["agent"], ToolCallActor)
    assert seen["files"] == ["notes.txt"]


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
async def test_a_vendor_tool_result_reaches_middleware_and_runners_as_its_tool_value() -> None:
    """A vendor tool's ``ToolResult`` is read once, at the entrypoint, so the middleware and runners see a ``ToolValue``."""
    seen: list[object] = []

    def tool() -> ToolResult:
        return ToolResult(content="made", images=[AgnoImage(url="https://example.invalid/out.png", id="out")])

    async def observe(call: ToolCall, call_next: ToolNext) -> object:
        seen.append(await call_next(call.arguments))
        return seen[-1]

    [function] = _functions(tool).values()

    returned = (await run_tool_call(function, {}, _context(), middleware=[observe])).value

    expected = ToolValue(content="made", images=(Image(id="out", url="https://example.invalid/out.png"),))
    assert returned == expected
    assert seen == [expected]
    assert client_tool_result(returned) == {
        "content": "made",
        "attachments": [{"kind": "image", "mime_type": None, "url": "https://example.invalid/out.png"}],
    }


@pytest.mark.asyncio
async def test_without_media_the_media_parameters_receive_none() -> None:
    """A declared media parameter receives None, as Agno passed it for a run without media."""
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
    [started] = tracker.settle()
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
async def test_a_stream_of_tool_values_joins_their_text_and_media() -> None:
    """A generator may yield MindRoom's ``ToolValue`` items beside Agno's and text; their media all reach the outcome."""
    picture = Image(id="picture", url="https://example.invalid/p.png")
    clip = Audio(id="clip", content=b"RIFF", mime_type="audio/wav")

    def stream() -> Iterator[object]:
        yield ToolValue(content="a", images=(picture,))
        yield "b"
        yield ToolResult(content="c", images=[AgnoImage(url="https://example.invalid/q.png", id="q")])
        yield ToolValue(content="d", audios=(clip,))

    [function] = _functions(stream).values()

    outcome = await _run(function, "{}")

    assert outcome.content == "abcd"
    assert outcome.images == (picture, Image(id="q", url="https://example.invalid/q.png"))
    assert outcome.audios == (clip,)


@pytest.mark.asyncio
async def test_a_returned_iterator_is_drained_like_a_generator() -> None:
    """A synchronous tool that returns an iterator without being a generator is drained too, as Agno drained any."""

    def letters() -> Iterator[str]:
        return iter(["a", "b"])

    [function] = _functions(letters).values()

    assert (await run_tool_call(function, {}, _context(), middleware=())).value == "ab"


def _letters(closed: list[str]) -> Iterator[str]:
    try:
        yield "a"
        yield "b"
    finally:
        closed.append("sync")


class _SyncStream:
    """An iterator that is not a generator and records its close."""

    def __init__(self, closed: list[str]) -> None:
        self._items = iter(["a", "b"])
        self._closed = closed

    def __iter__(self) -> _SyncStream:
        return self

    def __next__(self) -> str:
        return next(self._items)

    def close(self) -> None:
        self._closed.append("sync")


class _AsyncStream:
    """An async iterator that is not an async generator and records its close."""

    def __init__(self, closed: list[str]) -> None:
        self._items = iter(["a", "b"])
        self._closed = closed

    def __aiter__(self) -> _AsyncStream:
        return self

    async def __anext__(self) -> str:
        try:
            return next(self._items)
        except StopIteration:
            raise StopAsyncIteration from None

    async def aclose(self) -> None:
        self._closed.append("async")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("async_tool", "stream"),
    [(False, _letters), (False, _SyncStream), (True, _letters), (True, _SyncStream), (True, _AsyncStream)],
    ids=["sync-generator", "sync-iterator", "async-generator", "async-iterator", "async-async-iterator"],
)
async def test_custom_iterator_closes_once(*, async_tool: bool, stream: Callable[[list[str]], object]) -> None:
    """Any iterator a tool returns is drained and closed once; a synchronous one on the call's tracked worker thread."""
    closed: list[str] = []

    def sync_tool() -> object:
        return stream(closed)

    async def async_tool_() -> object:
        return stream(closed)

    [function] = _functions(async_tool_ if async_tool else sync_tool).values()
    tracker = SyncToolCompletionTracker()

    with track_sync_tool_completion(tracker):
        outcome = await _run(function, "{}")

    assert outcome == ToolOutcome(content="ab")
    assert closed == ["async" if stream is _AsyncStream else "sync"]
    assert (tracker.settle() == []) is (stream is _AsyncStream)


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
    """Any exception is the model's error text, or its type when it has none; cancellation is not an outcome."""

    def fails() -> str:
        msg = "boom"
        raise ValueError(msg)

    async def times_out() -> str:
        raise TimeoutError

    async def cancels() -> str:
        raise asyncio.CancelledError

    def stops() -> str:
        return "switched"

    functions = _functions(fails, times_out, cancels, stops)
    stopping = adapt_toolkit(Toolkit(name="control", tools=[stops], stop_after_tool_call_tools=["stops"])).functions[0]

    assert await _run(functions["fails"], "{}") == ToolOutcome(content="boom", is_error=True)
    assert await _run(functions["times_out"], "{}") == ToolOutcome(content="TimeoutError", is_error=True)
    with pytest.raises(asyncio.CancelledError):
        await _run(functions["cancels"], "{}")
    assert await _run(stopping, "{}") == ToolOutcome(content="switched", stop_after_call=True)


@pytest.mark.asyncio
async def test_a_call_refused_for_its_argument_names_lists_the_parameters_the_model_may_send() -> None:
    """The error names the function, its wrong and missing arguments, any other refusal, and each offered parameter."""

    def read_thread(run_context: RunContext, thread: str, limit: int | None = None) -> str:
        del run_context, limit
        return thread

    async def count_up(limit: int) -> AsyncIterator[str]:
        yield str(limit)

    functions = _functions(read_thread, count_up)
    accepted = "Accepted parameters: thread (string, required), limit (integer or null, optional)."

    unexpected = await _run(functions["read_thread"], '{"thread": "t", "count": 5, "offset": 1}')
    renamed = await _run(functions["read_thread"], '{"threads": "t", "limit": "many"}')
    streamed = await _run(functions["count_up"], '{"count": 3}')

    assert unexpected == ToolOutcome(
        content=f"read_thread() got unexpected arguments 'count', 'offset'. {accepted}",
        is_error=True,
    )
    assert renamed.content == (
        "read_thread() got unexpected argument 'threads' and is missing required argument 'thread'. "
        f"Invalid argument 'limit': Input should be a valid integer, unable to parse string as an integer. {accepted}"
    )
    assert streamed.content == (
        "count_up() got unexpected argument 'count' and is missing required argument 'limit'. "
        "Accepted parameters: limit (integer, required)."
    )


@pytest.mark.asyncio
async def test_a_validation_error_the_tool_body_raises_keeps_its_own_text() -> None:
    """A tool that calls another validated function with a wrong argument fails with that function's own refusal."""

    def note(text: str) -> str:
        return text

    def forwards(thread: str) -> str:
        return validated_entrypoint(note)(body=thread)  # ty: ignore[missing-argument, unknown-argument]

    outcome = await _run(_functions(forwards)["forwards"], '{"thread": "t"}')

    assert outcome.is_error
    assert outcome.content.startswith("2 validation errors for ")
    assert "Accepted parameters" not in outcome.content


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
    """``run_tool_call`` keeps what the tool returned, drained if it streamed, and the exception a failed call raised."""

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

    assert await run_tool_call(functions["numbers"], {}, _context(), middleware=(record,)) == CallResult("ok", {"a": 1})
    assert await run_tool_call(functions["streams"], {}, _context(), middleware=()) == CallResult("ok", "ab")
    failed = await run_tool_call(functions["fails"], {}, _context(), middleware=())
    assert failed.status == "failed"
    assert isinstance(failed.error, ValueError)
    assert failed.value is None
    assert failed.failure_text == failure_text(failed.error)
    assert seen == ["numbers"]
    assert current_tool_call() is None


_OAUTH_PAYLOAD: dict[str, object] = {
    "error": "Connect Google to continue.",
    "oauth_connection_required": True,
    "provider": "google",
}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("refusal", "status"),
    [
        (ToolOutcome(content="[TOOL CALL DECLINED] paused", is_error=True, refused_by="plugin_hook"), "declined"),
        (ToolOutcome(content="Not tonight.", is_error=True, refused_by="approval"), "denied"),
    ],
    ids=["plugin-decline", "approval-denial"],
)
async def test_a_call_the_policy_refuses_is_declined_or_denied_with_the_refusals_text(
    refusal: ToolOutcome,
    status: str,
) -> None:
    """A plugin hook's decline is ``declined`` and an approval denial ``denied``; neither runs the body or has an error."""
    ran: list[str] = []

    def tool() -> str:
        ran.append("body")
        return "ran"

    async def refuse(call: ToolCall, call_next: ToolNext) -> object:
        del call, call_next
        return refusal

    [function] = _functions(tool).values()

    result = await run_tool_call(function, {}, _context(), middleware=(refuse,))

    assert result == CallResult(status, refusal)
    assert result.failure_text == refusal.content
    assert result.succeeded is False
    assert ran == []


@pytest.mark.parametrize(
    ("status", "succeeded"),
    [("ok", True), ("connection_required", True), ("failed", False), ("declined", False), ("denied", False)],
)
def test_only_answered_calls_succeed(status: str, succeeded: bool) -> None:
    """Origins outside the model loop record a call as done only when it answered, an OAuth prompt included."""
    assert CallResult(status).succeeded is succeeded


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("answer", "status"),
    [
        (ToolOutcome(content="The account is locked.", is_error=True), "failed"),
        (ToolOutcome(content="fine"), "ok"),
        (_OAUTH_PAYLOAD, "connection_required"),
        (json.dumps(_OAUTH_PAYLOAD), "connection_required"),
        (ToolResult(content=json.dumps(_OAUTH_PAYLOAD)), "connection_required"),
        ({**_OAUTH_PAYLOAD, "oauth_connection_required": "yes"}, "ok"),
        ("not json", "ok"),
    ],
    ids=[
        "failed-outcome",
        "outcome",
        "oauth-payload",
        "oauth-json",
        "oauth-json-tool-result",
        "flag-not-true",
        "text",
    ],
)
async def test_a_call_that_ran_is_read_from_what_its_tool_answered(answer: object, status: str) -> None:
    """A tool's own failed outcome is ``failed`` with no error, and an OAuth prompt in any form needs a connection.

    The value is the tool's answer, with a vendor ``ToolResult`` read as its ``ToolValue``.
    """

    def tool() -> object:
        return answer

    [function] = _functions(tool).values()

    result = await run_tool_call(function, {}, _context(), middleware=())

    expected = ToolValue(content=answer.content) if isinstance(answer, ToolResult) else answer
    assert result == CallResult(status, expected)
    assert result.error is None
    if status == "failed":
        assert result.failure_text == "The account is locked."


@pytest.mark.asyncio
async def test_an_owned_sync_body_is_waited_for_after_cancellation_without_raising_its_failure() -> None:
    """A cancelled call's owner waits for the synchronous body it started, and the body's exception stays its own."""
    started = threading.Event()
    release = threading.Event()
    finished: list[str] = []

    def blocking() -> str:
        started.set()
        assert release.wait(SETTLE_TIMEOUT_SECONDS)
        finished.append("body")
        msg = "slack is down"
        raise RuntimeError(msg)

    [function] = _functions(blocking).values()

    async def owned_call() -> None:
        async with owned_sync_tool_completion():
            await run_tool_call(function, {}, _context(), middleware=())

    running = asyncio.create_task(owned_call())
    finished_when_done: list[list[str]] = []
    running.add_done_callback(lambda _: finished_when_done.append(list(finished)))
    assert await asyncio.to_thread(started.wait, SETTLE_TIMEOUT_SECONDS)

    running.cancel()
    # Long enough for an unowned call's cancellation to finish; an owned one stays pending until its body does.
    await asyncio.wait({running}, timeout=0.2)
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await running

    assert finished_when_done == [["body"]]


def test_clients_get_a_tool_values_text_with_references_to_its_media() -> None:
    """A tool value gives its text, with each medium referenced instead of sent; other values pass unchanged."""
    drawn = ToolValue(
        content="drawn",
        images=(Image(url="https://example.org/cat.png"),),
        audios=(Audio(content=b"RIFF", mime_type="audio/wav"),),
        videos=(Video(url="https://example.org/cat.mp4", mime_type="video/mp4"),),
        files=(File(content=b"a,b", mime_type="text/csv"),),
    )

    assert client_tool_result(ToolValue(content="hello")) == "hello"
    assert client_tool_result(drawn) == {
        "content": "drawn",
        "attachments": [
            {"kind": "image", "mime_type": None, "url": "https://example.org/cat.png"},
            {"kind": "audio", "mime_type": "audio/wav"},
            {"kind": "video", "mime_type": "video/mp4", "url": "https://example.org/cat.mp4"},
            {"kind": "file", "mime_type": "text/csv"},
        ],
    }
    assert client_tool_result({"a": 1}) == {"a": 1}
    assert client_tool_result(None) is None


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


@pytest.mark.asyncio
async def test_a_leaf_still_queued_when_its_owner_settles_never_starts() -> None:
    """Synchronous work a call submitted but its owner's threads had not begun by the call's end never runs."""
    first_started, release_first, second_ran = threading.Event(), threading.Event(), threading.Event()
    tasks: list[asyncio.Task[object]] = []

    def first() -> str:
        first_started.set()
        assert release_first.wait(SETTLE_TIMEOUT_SECONDS)
        return "first"

    def second() -> str:
        second_ran.set()
        return "second"

    async def tool() -> None:
        tasks.append(asyncio.create_task(run_sync_tool_entrypoint(first)))
        assert await asyncio.to_thread(first_started.wait, SETTLE_TIMEOUT_SECONDS)
        # Queued behind the first on the owner's only thread.
        tasks.append(asyncio.create_task(run_sync_tool_entrypoint(second)))
        await asyncio.sleep(0)
        msg = "the tool failed"
        raise RuntimeError(msg)

    async def owned_call() -> None:
        async with owned_sync_tool_completion(ThreadPoolExecutor(max_workers=1)):
            await tool()

    owner = asyncio.create_task(owned_call())
    assert await asyncio.to_thread(first_started.wait, SETTLE_TIMEOUT_SECONDS)
    release_first.set()
    with pytest.raises(RuntimeError, match="the tool failed"):
        await owner
    await asyncio.gather(*tasks, return_exceptions=True)

    assert not second_ran.is_set()


@pytest.mark.asyncio
async def test_work_a_call_submits_after_its_owner_settled_never_runs() -> None:
    """A task the call left behind that starts synchronous work after the call ended gets no thread for it."""
    ran = threading.Event()
    go = asyncio.Event()
    left_behind: list[asyncio.Task[object]] = []

    async def later() -> object:
        await go.wait()
        return await run_sync_tool_entrypoint(ran.set)

    async with owned_sync_tool_completion():
        left_behind.append(asyncio.create_task(later()))
    go.set()
    [outcome] = await asyncio.gather(*left_behind, return_exceptions=True)

    assert isinstance(outcome, asyncio.CancelledError)
    assert not ran.is_set()

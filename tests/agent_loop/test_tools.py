"""Run tools are offered, resolved, limited, classified, run, and answered as Agno 3.0.9 did."""
# ruff: noqa: D103

from __future__ import annotations

import asyncio
import contextvars
import json
import threading
import time
from dataclasses import replace
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest
from agno.media import Image as AgnoImage
from agno.tools.function import ToolResult
from structlog.testing import capture_logs

from mindroom.agent_loop.tools import (
    _PROGRESS_FLUSH_SECONDS,
    MEDIA_FOLLOW_UP_TEXT,
    PreparedTools,
    RefusedCall,
    ToolBatch,
    dispatch_tool_calls,
    media_follow_up,
    offered_toolkits,
    plan_tool_batch,
    toolkit_instructions,
)
from mindroom.llm.events import ToolProgress
from mindroom.redaction import REDACTION_FAILED, redact_sensitive_text
from mindroom.tool_system.invocation import MISSING_TOOL_ERROR_TEXT, ToolCall, ToolNext
from mindroom.tool_system.progress import report_tool_progress, tool_progress_reporter
from tests.agent_loop.helpers import add, boom, context, echo, external, gated, spec, toolkit
from tests.fakes.scripted_model import ScriptedModel, raw_tool_call, tool_call

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Sequence

    from mindroom.agent_loop.tools import PlannedCall
    from mindroom.llm.messages import Message
    from mindroom.tool_system.invocation import ToolMiddleware
    from mindroom.tool_system.tool_function import AdaptedToolkit

# --- offered_toolkits and PreparedTools ------------------------------------------------------------------------------


def test_prepared_tools_keep_offer_order() -> None:
    prepared = PreparedTools(
        functions=spec(ScriptedModel([]), toolkit(add, name="math"), toolkit(echo, name="text")).functions,
    )

    assert list(prepared.functions) == ["add", "echo"]
    assert [tool.name for tool in prepared.specs()] == ["add", "echo"]


def test_prepared_tools_sort_definitions_by_name_for_prompt_cache_stability() -> None:
    prepared = PreparedTools(
        functions=spec(ScriptedModel([]), toolkit(echo, name="text"), toolkit(add, name="math")).functions,
    )

    assert list(prepared.functions) == ["echo", "add"]
    assert [tool.name for tool in prepared.specs()] == ["add", "echo"]


def _loud_echo() -> AdaptedToolkit:
    def echo_v2(text: str) -> str:
        """Return the text, loudly.

        Args:
            text: What to echo.

        """
        return text.upper()

    return replace(
        toolkit(echo_v2, name="loud"),
        functions=tuple(replace(f, name="echo") for f in toolkit(echo_v2).functions),
    )


def test_offered_toolkits_keep_the_first_of_a_repeated_name_and_log_each_drop_once() -> None:
    first = toolkit(add, echo, name="first")
    loud = _loud_echo()
    shadowed = toolkit(echo, name="shadowed")

    with capture_logs() as logs:
        offered = offered_toolkits((first, loud, shadowed), agent="code")

    assert [toolkit.name for toolkit in offered] == ["first", "loud", "shadowed"]
    assert offered[0] == first
    assert (offered[1].functions, offered[2].functions) == ((), ())
    assert spec(ScriptedModel([]), *offered).functions["echo"].description == "Return the text."
    assert [(entry["toolkit"], entry["tool_name"]) for entry in logs if entry["event"] == "duplicate_tool_name"] == [
        ("loud", "echo"),
        ("shadowed", "echo"),
    ]


def test_a_spec_refuses_two_functions_of_one_name() -> None:
    with pytest.raises(ValueError, match="two functions named 'echo'"):
        spec(ScriptedModel([]), toolkit(add, echo, name="first"), _loud_echo())


def test_toolkit_instructions_show_for_each_toolkit_that_offers_a_function() -> None:
    math = replace(toolkit(add, name="math"), instructions="Use math.")
    text = toolkit(echo, name="text")  # A toolkit without instructions adds none.
    errors = replace(toolkit(add, boom, name="errors"), instructions="Fail loudly.")

    offered = offered_toolkits((math, text, replace(math, name="algebra"), errors), agent="code")

    # The algebra copy offers no function, since math took ``add``; errors still offers ``boom``.
    assert [len(toolkit.functions) for toolkit in offered] == [1, 1, 0, 1]
    assert toolkit_instructions(offered) == ("Use math.", "Fail loudly.")


# --- plan_tool_batch -------------------------------------------------------------------------------------------------


def _functions() -> dict[str, object]:
    return dict(spec(ScriptedModel([]), toolkit(add, echo), gated(boom)).functions)


def _refused_answers(batch: ToolBatch) -> list[Message]:
    return [call.answer for call in batch.calls if isinstance(call, RefusedCall)]


def test_unknown_and_undecodable_calls_are_refused_in_call_order_and_bypass_the_limit() -> None:
    functions = spec(ScriptedModel([]), toolkit(add)).functions

    batch = plan_tool_batch(
        [
            tool_call("missing", "c1", x=1),
            raw_tool_call("add", "c2", "{not json"),
            raw_tool_call("add", "c3", '"a string"'),
            tool_call("add", "c4", a=1, b=2),
        ],
        functions,
        calls_so_far=0,
        max_tool_calls_per_turn=1,
        entity="code",
    )

    # Each answer is a failed call, which Claude and Gemini read as a failure.
    messages = _refused_answers(batch)
    assert [(m.role, m.tool_call_id, m.tool_name, m.tool_call_error) for m in messages] == [
        ("tool", "c1", "missing", True),
        ("tool", "c2", "add", True),
        ("tool", "c3", "add", True),
    ]
    assert messages[0].content == MISSING_TOOL_ERROR_TEXT
    assert str(messages[1].content).startswith("Error while decoding function arguments: ")
    assert messages[2].content == "Function arguments are not a valid JSON object.\n\n Please fix and retry."
    # The three errors never counted, so the fourth call is the first within the limit; every call keeps its place.
    assert [call.execution.tool_call_id for call in batch.planned] == ["c4"]
    assert [call.execution.tool_call_id for call in batch.calls] == ["c1", "c2", "c3", "c4"]
    assert [call.counts for call in batch.calls if isinstance(call, RefusedCall)] == [False, False, False]


def test_unknown_and_undecodable_calls_are_logged_without_their_arguments() -> None:
    functions = spec(ScriptedModel([]), toolkit(add)).functions

    with capture_logs() as logs:
        plan_tool_batch(
            [
                tool_call("missing", "c1", x="secret-1"),
                tool_call("m" * 500, "c2"),
                raw_tool_call("add", "c3", "{'a': secret-2"),
                raw_tool_call("add", "c4", '"secret-3"'),
            ],
            functions,
            calls_so_far=0,
            max_tool_calls_per_turn=None,
            entity="code",
        )

    warning = {"log_level": "warning", "entity": "code"}
    # Arguments can hold user data, so neither they nor the decode error, which can quote them, are logged; the
    # model writes an unknown tool's name, so a long one is cut.
    assert logs == [
        {**warning, "event": "tool_not_offered", "tool_name": "missing"},
        {**warning, "event": "tool_not_offered", "tool_name": "m" * 100},
        {**warning, "event": "invalid_tool_arguments", "tool_name": "add", "reason": "decode"},
        {**warning, "event": "invalid_tool_arguments", "tool_name": "add", "reason": "not_object"},
    ]


def test_calls_past_the_limit_get_the_limit_error_in_call_order() -> None:
    functions = spec(ScriptedModel([]), toolkit(add, echo)).functions

    batch = plan_tool_batch(
        [tool_call("add", "c1", a=1, b=2), tool_call("echo", "c2", text="x"), tool_call("add", "c3", a=3, b=4)],
        functions,
        calls_so_far=1,
        max_tool_calls_per_turn=2,
        entity="code",
    )

    assert [call.execution.tool_call_id for call in batch.planned] == ["c1"]
    assert [
        (m.tool_call_id, m.tool_name, m.content, m.tool_call_error, m.tool_args) for m in _refused_answers(batch)
    ] == [
        (
            "c2",
            "echo",
            "Tool call limit reached. Tool call echo not executed. Don't try to execute it again.",
            True,
            {"text": "x"},
        ),
        (
            "c3",
            "add",
            "Tool call limit reached. Tool call add not executed. Don't try to execute it again.",
            True,
            {"a": 3, "b": 4},
        ),
    ]


def test_empty_list_arguments_plan_as_no_arguments() -> None:
    functions = spec(ScriptedModel([]), toolkit(echo)).functions

    batch = plan_tool_batch(
        [raw_tool_call("echo", "c1", "[]")],
        functions,
        calls_so_far=0,
        max_tool_calls_per_turn=None,
        entity="code",
    )

    assert batch.planned[0].execution.tool_args is None
    assert batch.planned[0].execution.tool_args is None


def test_paused_calls_carry_their_pause_flags_and_the_batch_pauses() -> None:
    functions = spec(ScriptedModel([]), toolkit(add), gated(echo), external(boom)).functions

    batch = plan_tool_batch(
        [tool_call("add", "c1", a=1, b=2), tool_call("echo", "c2", text="x"), tool_call("boom", "c3", reason="r")],
        functions,
        calls_so_far=0,
        max_tool_calls_per_turn=None,
        entity="code",
    )

    plain, confirmation, external_call = (call.execution for call in batch.planned)
    assert (plain.requires_confirmation, plain.external_execution_required, plain.approval_type) == (None, None, None)
    assert (
        confirmation.requires_confirmation,
        confirmation.external_execution_required,
        confirmation.approval_type,
    ) == (
        True,
        None,
        "mindroom_policy",
    )
    assert (external_call.requires_confirmation, external_call.external_execution_required) == (None, True)
    assert external_call.approval_type is None
    assert batch.paused


# --- dispatch_tool_calls ----------------------------------------------------------------------------------------------


async def _dispatch(calls: Sequence[PlannedCall], *, middleware: Sequence[ToolMiddleware] = ()) -> list[Message]:
    """Run ``calls`` and return the result messages of those that ran, in the order they finished."""
    return [result async for _, result in dispatch_tool_calls(calls, run=context(), middleware=middleware)]


def _planned(functions: dict, *calls: object) -> list[PlannedCall]:
    return plan_tool_batch(list(calls), functions, calls_so_far=0, max_tool_calls_per_turn=None, entity="code").planned


@pytest.mark.asyncio
async def test_a_running_call_streams_its_progress_before_it_finishes() -> None:
    """A call's latest report reaches the batch's stream before the call's result."""

    def build(target: str) -> str:
        """Report two steps, then finish.

        Args:
            target: What to build.

        """
        report_tool_progress(f"{target}: step 1")
        report_tool_progress(f"{target}: step 2")
        return f"{target} built"

    planned = _planned(spec(ScriptedModel([]), toolkit(build)).functions, tool_call("build", "c1", target="app"))

    items = [item async for item in dispatch_tool_calls(planned, run=context(), middleware=())]

    *reports, (call, result) = items
    assert reports
    assert all(isinstance(report, ToolProgress) and report.tool_call_id == "c1" for report in reports)
    assert reports[-1].text == "app: step 2"
    assert (call.execution.tool_call_id, result.content) == ("c1", "app built")


@pytest.mark.asyncio
async def test_a_running_calls_output_is_redacted_before_it_leaves_the_call() -> None:
    """Credentials a tool prints while it runs are masked as in its result preview."""
    secret = "hunter2" * 3

    def deploy(target: str) -> str:
        """Print the deploy command, then finish.

        Args:
            target: Where to deploy.

        """
        report_tool_progress(f"deploying {target} with API_KEY={secret}")
        return "deployed"

    planned = _planned(spec(ScriptedModel([]), toolkit(deploy)).functions, tool_call("deploy", "c1", target="prod"))

    items = [item async for item in dispatch_tool_calls(planned, run=context(), middleware=())]

    [progress] = [item for item in items if isinstance(item, ToolProgress)]
    assert secret not in progress.text
    assert progress.text == "deploying prod with API_KEY=***redacted***"


@pytest.mark.asyncio
async def test_a_credential_longer_than_the_shown_output_stays_masked() -> None:
    """The output is masked before it is cut to its latest characters, which would drop a long credential's label."""
    secret = "".join(f"{index:04x}" for index in range(800))

    def export(name: str) -> str:
        """Print a long key, then finish.

        Args:
            name: The variable to export.

        """
        report_tool_progress(f"export {name}={secret}\nready")
        return "exported"

    planned = _planned(spec(ScriptedModel([]), toolkit(export)).functions, tool_call("export", "c1", name="API_KEY"))

    items = [item async for item in dispatch_tool_calls(planned, run=context(), middleware=())]

    [progress] = [item for item in items if isinstance(item, ToolProgress)]
    assert secret[-40:] not in progress.text
    assert progress.text.endswith("\nready")


@pytest.mark.asyncio
async def test_output_longer_than_redaction_accepts_still_shows_its_latest_characters() -> None:
    """Huge output, such as long compile lines and template errors together, shows its tail rather than a failure."""

    def compile_all(unit: str) -> str:
        """Compile everything.

        Args:
            unit: The unit to compile.

        """
        report_tool_progress(f"g++ -c {unit} " * 6000 + "\nerror: no matching call")
        return "failed"

    planned = _planned(
        spec(ScriptedModel([]), toolkit(compile_all)).functions,
        tool_call("compile_all", "c1", unit="a.cpp"),
    )

    items = [item async for item in dispatch_tool_calls(planned, run=context(), middleware=())]

    [progress] = [item for item in items if isinstance(item, ToolProgress)]
    assert progress.text != REDACTION_FAILED
    assert progress.text.endswith("\nerror: no matching call")


@pytest.mark.asyncio
async def test_a_calls_progress_stays_with_the_call() -> None:
    """A call run inside another call, as a delegated agent's is, reports its output only for itself."""
    outer: list[str] = []

    def build(app: str) -> str:
        """Build an app.

        Args:
            app: Which app to build.

        """
        report_tool_progress(f"{app}: compiling")
        return "built"

    planned = _planned(spec(ScriptedModel([]), toolkit(build)).functions, tool_call("build", "c1", app="app"))

    with tool_progress_reporter(outer.append):
        items = [item async for item in dispatch_tool_calls(planned, run=context(), middleware=())]

    assert [item for item in items if isinstance(item, ToolProgress)] == [ToolProgress("c1", "app: compiling")]
    assert outer == []


@pytest.mark.asyncio
async def test_a_call_reporting_faster_than_frames_go_out_yields_at_most_ten_events_a_second() -> None:
    """Live frames go out ten times a second, so more reports than that only cost the loop events nobody sees."""

    async def stream(count: int) -> str:
        """Report every hundredth of a second, then finish.

        Args:
            count: How many reports to make.

        """
        for index in range(count):
            report_tool_progress(f"report {index}")
            await asyncio.sleep(0.01)
        return "done"

    planned = _planned(spec(ScriptedModel([]), toolkit(stream)).functions, tool_call("stream", "c1", count=40))

    started = time.monotonic()
    items = [item async for item in dispatch_tool_calls(planned, run=context(), middleware=())]
    elapsed = time.monotonic() - started

    reports = [item for item in items if isinstance(item, ToolProgress)]
    assert len(reports) <= int(elapsed / 0.1) + 2
    assert reports[-1].text == "report 39"


@pytest.mark.asyncio
async def test_a_flood_of_progress_reports_costs_a_few_events_and_keeps_the_latest() -> None:
    """A call that reports far faster than the loop turns, as untrusted worker code can, yields only its newest reports."""

    def flood(count: int) -> str:
        """Report many times from this thread, then finish.

        Args:
            count: How many reports to make.

        """
        for index in range(count):
            report_tool_progress(f"report {index}")
        return "done"

    planned = _planned(spec(ScriptedModel([]), toolkit(flood)).functions, tool_call("flood", "c1", count=20000))

    loop = asyncio.get_running_loop()
    with (
        patch("mindroom.tool_system.progress.redact_sensitive_text", wraps=redact_sensitive_text) as redact,
        patch.object(loop, "call_soon_threadsafe", wraps=loop.call_soon_threadsafe) as hop,
    ):
        items = [item async for item in dispatch_tool_calls(planned, run=context(), middleware=())]

    reports = [item for item in items if isinstance(item, ToolProgress)]
    assert 0 < len(reports) < 1000
    assert redact.call_count == len(reports)
    assert hop.call_count < 1000
    assert reports[-1].text == "report 19999"


@pytest.mark.asyncio
async def test_concurrent_calls_report_only_their_own_progress() -> None:
    """Calls of one batch that report at the same time each report under their own call ID."""
    started = {"one": asyncio.Event(), "two": asyncio.Event()}

    async def step(who: str) -> str:
        """Report this call's name once both calls run.

        Args:
            who: This call's name.

        """
        started[who].set()
        await asyncio.wait_for(started["two" if who == "one" else "one"].wait(), timeout=5)
        report_tool_progress(f"{who} working")
        return who

    planned = _planned(
        spec(ScriptedModel([]), toolkit(step)).functions,
        tool_call("step", "c1", who="one"),
        tool_call("step", "c2", who="two"),
    )

    items = [item async for item in dispatch_tool_calls(planned, run=context(), middleware=())]

    reports = sorted((item.tool_call_id, item.text) for item in items if isinstance(item, ToolProgress))
    assert reports == [("c1", "one working"), ("c2", "two working")]


@pytest.mark.asyncio
async def test_progress_a_call_reports_after_it_returned_is_dropped() -> None:
    """A thread a call leaves behind cannot report for it once the call has returned."""
    report_later = threading.Event()
    reported = threading.Event()
    release_other = asyncio.Event()

    def start(name: str) -> str:
        """Leave a thread that reports after this call returned.

        Args:
            name: The work's name.

        """
        context_of_call = contextvars.copy_context()

        def late() -> None:
            report_later.wait(timeout=5)
            context_of_call.run(report_tool_progress, f"{name} late")
            reported.set()

        threading.Thread(target=late, daemon=True).start()
        return f"{name} started"

    async def hold(name: str) -> str:
        """Keep the batch running until released.

        Args:
            name: The work's name.

        """
        await asyncio.wait_for(release_other.wait(), timeout=5)
        return f"{name} done"

    planned = _planned(
        spec(ScriptedModel([]), toolkit(start, hold)).functions,
        tool_call("start", "c1", name="build"),
        tool_call("hold", "c2", name="watch"),
    )
    stream = dispatch_tool_calls(planned, run=context(), middleware=())

    first = await anext(stream)
    report_later.set()
    await asyncio.to_thread(reported.wait, 5)
    await asyncio.sleep(2 * _PROGRESS_FLUSH_SECONDS)
    release_other.set()
    rest = [item async for item in stream]

    assert isinstance(first, tuple)
    assert first[1].content == "build started"
    assert [item for item in rest if isinstance(item, ToolProgress)] == []
    assert [result.content for _, result in rest] == ["watch done"]


@pytest.mark.asyncio
async def test_dispatch_runs_calls_concurrently_and_the_batch_answers_them_in_call_order() -> None:
    started = {"one": asyncio.Event(), "two": asyncio.Event()}

    async def meet(who: str) -> str:
        """Wait for the other call to start.

        Args:
            who: This call's name.

        """
        started[who].set()
        other = "two" if who == "one" else "one"
        await asyncio.wait_for(started[other].wait(), timeout=5)
        return f"{who} met {other}"

    functions = spec(ScriptedModel([]), toolkit(meet)).functions
    batch = plan_tool_batch(
        [tool_call("meet", "c1", who="one"), tool_call("meet", "c2", who="two")],
        functions,
        calls_so_far=0,
        max_tool_calls_per_turn=None,
        entity="code",
    )

    results = await _dispatch(batch.planned)

    # The second call finds the first already started, so it finishes first and comes first.
    assert [(m.tool_call_id, m.content, m.tool_call_error) for m in results] == [
        ("c2", "two met one", False),
        ("c1", "one met two", False),
    ]
    answers = batch.answers(results)
    assert [(m.tool_call_id, m.content) for m in answers] == [("c1", "one met two"), ("c2", "two met one")]
    for call, answer in zip(batch.planned, answers, strict=True):
        execution = call.execution
        assert (execution.result, execution.is_error) == (answer.content, False)


def _linger(release: asyncio.Event, finished: list[str]) -> Callable[[], Awaitable[str]]:
    """Return a tool that waits for ``release`` and records when it unwinds, released or cancelled."""

    async def linger() -> str:
        """Wait to be released."""
        try:
            await release.wait()
            return "lingered"
        finally:
            finished.append("linger")

    return linger


@pytest.mark.asyncio
async def test_a_call_cancelled_on_its_own_lets_its_siblings_finish_before_the_batch_raises() -> None:
    release = asyncio.Event()
    finished: list[str] = []

    async def give_up() -> str:
        """Cancel this call."""
        raise asyncio.CancelledError

    functions = spec(ScriptedModel([]), toolkit(_linger(release, finished), give_up)).functions
    batch = plan_tool_batch(
        [tool_call("linger", "c1"), tool_call("give_up", "c2")],
        functions,
        calls_so_far=0,
        max_tool_calls_per_turn=None,
        entity="code",
    )
    dispatch = asyncio.create_task(_dispatch(batch.planned))
    for _ in range(5):
        await asyncio.sleep(0)
    assert not dispatch.done()  # give_up is gone, but linger still runs: the batch waits for it.

    release.set()
    with pytest.raises(asyncio.CancelledError):
        await dispatch
    assert finished == ["linger"]
    assert batch.planned[0].execution.result == "lingered"


@pytest.mark.asyncio
async def test_a_cancelled_dispatch_waits_for_every_call_to_unwind() -> None:
    release = asyncio.Event()
    finished: list[str] = []

    functions = spec(ScriptedModel([]), toolkit(_linger(release, finished))).functions
    batch = plan_tool_batch(
        [tool_call("linger", "c1")],
        functions,
        calls_so_far=0,
        max_tool_calls_per_turn=None,
        entity="code",
    )
    dispatch = asyncio.create_task(_dispatch(batch.planned))
    for _ in range(5):
        await asyncio.sleep(0)

    dispatch.cancel()
    with pytest.raises(asyncio.CancelledError):
        await dispatch
    assert finished == ["linger"]


@pytest.mark.asyncio
async def test_each_call_is_recorded_and_yielded_as_it_finishes_and_closing_unwinds_the_rest() -> None:
    release = asyncio.Event()
    finished: list[str] = []

    async def quick() -> str:
        """Answer at once."""
        return "quick answer"

    functions = spec(ScriptedModel([]), toolkit(_linger(release, finished), quick)).functions
    batch = plan_tool_batch(
        [tool_call("linger", "c1"), tool_call("quick", "c2")],
        functions,
        calls_so_far=0,
        max_tool_calls_per_turn=None,
        entity="code",
    )
    linger, quick_call = batch.planned
    dispatch = dispatch_tool_calls(batch.planned, run=context(), middleware=())

    async with asyncio.timeout(5):
        call, result = await anext(dispatch)

    # The later call finished first, so it comes first, recorded while its earlier sibling still runs.
    assert call is quick_call
    assert (result.content, quick_call.execution.result, quick_call.execution.is_error) == (
        "quick answer",
        "quick answer",
        False,
    )
    assert (linger.execution.result, finished) == (None, [])
    await dispatch.aclose()
    assert (linger.execution.result, finished) == (None, ["linger"])


@pytest.mark.asyncio
async def test_dispatch_coerces_arguments_and_turns_failures_into_error_results() -> None:
    functions = spec(ScriptedModel([]), toolkit(add, boom)).functions
    batch = plan_tool_batch(
        [
            raw_tool_call("add", "c1", '{"a": "3", "b": 4}'),
            raw_tool_call("add", "c2", '{"a": "three", "b": 4}'),
            tool_call("boom", "c3", reason="kaboom"),
        ],
        functions,
        calls_so_far=0,
        max_tool_calls_per_turn=None,
        entity="code",
    )

    # The sync calls run on worker threads and may finish in any order; the batch answers them in call order.
    coerced, invalid, failed = batch.answers(await _dispatch(batch.planned))
    assert (coerced.content, coerced.tool_call_error, coerced.tool_args) == ('{"sum": 7}', False, {"a": "3", "b": 4})
    assert invalid.tool_call_error is True
    assert "validation error" in str(invalid.content)
    assert (failed.content, failed.tool_call_error) == ("kaboom", True)
    assert batch.planned[2].execution.result == "kaboom"


@pytest.mark.asyncio
async def test_dispatch_skips_paused_calls_and_passes_the_call_id_through_middleware() -> None:
    seen: list[tuple[str | None, str]] = []

    async def middleware(call: ToolCall, call_next: ToolNext) -> object:
        seen.append((call.context.call_id, call.function.name))
        return await call_next(call.arguments)

    functions = spec(ScriptedModel([]), toolkit(echo), gated(add)).functions
    batch = plan_tool_batch(
        [tool_call("add", "c1", a=1, b=2), tool_call("echo", "c2", text="hi")],
        functions,
        calls_so_far=0,
        max_tool_calls_per_turn=None,
        entity="code",
    )

    results = await _dispatch(batch.planned, middleware=[middleware])

    assert [(m.tool_call_id, m.content) for m in results] == [("c2", "hi")]
    assert seen == [("c2", "echo")]
    assert batch.planned[0].execution.result is None


@pytest.mark.asyncio
async def test_media_results_move_onto_one_follow_up_user_message() -> None:
    def picture(label: str) -> ToolResult:
        """Return a picture.

        Args:
            label: Its label.

        """
        return ToolResult(content=f"picture {label}", images=[AgnoImage(content=label.encode(), format="png")])

    functions = spec(ScriptedModel([]), toolkit(picture, echo)).functions
    batch = plan_tool_batch(
        [
            tool_call("picture", "c1", label="a"),
            tool_call("echo", "c2", text="t"),
            tool_call("picture", "c3", label="b"),
        ],
        functions,
        calls_so_far=0,
        max_tool_calls_per_turn=None,
        entity="code",
    )
    # The sync calls may finish in any order; the batch answers them, and so orders their media, in call order.
    results = batch.answers(await _dispatch(batch.planned))
    assert [len(m.images or ()) for m in results] == [1, 0, 1]

    follow_up = media_follow_up(results)

    assert follow_up is not None
    assert (follow_up.role, follow_up.content) == ("user", MEDIA_FOLLOW_UP_TEXT)
    assert [image.content for image in follow_up.images or ()] == [b"a", b"b"]
    assert all(m.images is None for m in results)
    assert media_follow_up(results) is None
    assert json.loads(json.dumps(results[0].to_dict()))["content"] == "picture a"

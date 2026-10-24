"""Run tools are offered, resolved, limited, classified, run, and answered as Agno 3.0.9 did."""
# ruff: noqa: D103

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from typing import TYPE_CHECKING

import pytest
from agno.media import Image as AgnoImage
from agno.tools.function import ToolResult
from structlog.testing import capture_logs

from mindroom.agent_loop.tools import (
    MEDIA_FOLLOW_UP_TEXT,
    dispatch_tool_calls,
    media_follow_up,
    plan_tool_batch,
    prepare_run_tools,
)
from mindroom.tool_system.invocation import MISSING_TOOL_ERROR_TEXT, ToolCall, ToolNext
from tests.agent_loop.helpers import add, boom, context, echo, external, gated, spec, toolkit
from tests.fakes.scripted_model import ScriptedModel, raw_tool_call, tool_call

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from mindroom.llm.messages import Message

# --- prepare_run_tools -----------------------------------------------------------------------------------------------


def test_prepared_tools_keep_offer_order_and_only_prompt_instructions() -> None:
    first = replace(toolkit(add, name="math"), instructions="Use math.")
    second = toolkit(echo, name="text")  # Agno adds no instructions unless the toolkit asks.

    prepared = prepare_run_tools(spec(ScriptedModel([]), first, second))

    assert list(prepared.functions) == ["add", "echo"]
    assert prepared.instructions == ("Use math.",)
    assert [tool.name for tool in prepared.specs()] == ["add", "echo"]


def test_prepared_tools_sort_definitions_by_name_for_prompt_cache_stability() -> None:
    prepared = prepare_run_tools(spec(ScriptedModel([]), toolkit(echo, name="text"), toolkit(add, name="math")))

    assert list(prepared.functions) == ["echo", "add"]
    assert [tool.name for tool in prepared.specs()] == ["add", "echo"]


def test_prepared_tools_keep_the_first_of_a_repeated_name() -> None:
    def echo_v2(text: str) -> str:
        """Return the text, loudly.

        Args:
            text: What to echo.

        """
        return text.upper()

    second = replace(
        toolkit(echo_v2, name="loud"),
        functions=tuple(replace(f, name="echo") for f in toolkit(echo_v2).functions),
    )
    agent = spec(ScriptedModel([]), toolkit(add, echo, name="first"), second)

    with capture_logs() as logs:
        prepared = prepare_run_tools(agent)

    assert list(prepared.functions) == ["add", "echo"]
    assert prepared.functions["echo"].description == "Return the text."
    assert [entry["tool_name"] for entry in logs if entry["event"] == "duplicate_tool_name"] == ["echo"]


# --- plan_tool_batch -------------------------------------------------------------------------------------------------


def _functions() -> dict[str, object]:
    return dict(prepare_run_tools(spec(ScriptedModel([]), toolkit(add, echo), gated(boom))).functions)


def test_unknown_and_undecodable_calls_answer_at_once_and_bypass_the_limit() -> None:
    functions = prepare_run_tools(spec(ScriptedModel([]), toolkit(add))).functions
    messages: list[Message] = []

    batch = plan_tool_batch(
        [
            tool_call("missing", "c1", x=1),
            raw_tool_call("add", "c2", "{not json"),
            raw_tool_call("add", "c3", '"a string"'),
            tool_call("add", "c4", a=1, b=2),
        ],
        functions,
        messages,
        calls_so_far=0,
        max_tool_calls_per_turn=1,
    )

    assert [(m.role, m.tool_call_id, m.tool_name) for m in messages] == [
        ("tool", "c1", "missing"),
        ("tool", "c2", "add"),
        ("tool", "c3", "add"),
    ]
    assert messages[0].content == MISSING_TOOL_ERROR_TEXT
    assert str(messages[1].content).startswith("Error while decoding function arguments: ")
    assert messages[2].content == "Function arguments are not a valid JSON object.\n\n Please fix and retry."
    # The three errors never counted, so the fourth call is the first within the limit.
    assert [call.call_id for call in batch.planned] == ["c4"]
    assert batch.refused == ()


def test_calls_past_the_limit_get_the_limit_error_in_call_order() -> None:
    functions = prepare_run_tools(spec(ScriptedModel([]), toolkit(add, echo))).functions

    batch = plan_tool_batch(
        [tool_call("add", "c1", a=1, b=2), tool_call("echo", "c2", text="x"), tool_call("add", "c3", a=3, b=4)],
        functions,
        [],
        calls_so_far=1,
        max_tool_calls_per_turn=2,
    )

    assert [call.call_id for call in batch.planned] == ["c1"]
    assert [(m.tool_call_id, m.content, m.tool_call_error, m.tool_args) for m in batch.refused] == [
        (
            "c2",
            "Tool call limit reached. Tool call echo not executed. Don't try to execute it again.",
            True,
            {"text": "x"},
        ),
        (
            "c3",
            "Tool call limit reached. Tool call add not executed. Don't try to execute it again.",
            True,
            {"a": 3, "b": 4},
        ),
    ]


def test_empty_list_arguments_plan_as_no_arguments() -> None:
    functions = prepare_run_tools(spec(ScriptedModel([]), toolkit(echo))).functions

    batch = plan_tool_batch(
        [raw_tool_call("echo", "c1", "[]")],
        functions,
        [],
        calls_so_far=0,
        max_tool_calls_per_turn=None,
    )

    assert batch.planned[0].arguments is None
    assert batch.planned[0].execution.tool_args is None


def test_paused_calls_carry_their_pause_flags_and_the_batch_pauses() -> None:
    functions = prepare_run_tools(spec(ScriptedModel([]), toolkit(add), gated(echo), external(boom))).functions

    batch = plan_tool_batch(
        [tool_call("add", "c1", a=1, b=2), tool_call("echo", "c2", text="x"), tool_call("boom", "c3", reason="r")],
        functions,
        [],
        calls_so_far=0,
        max_tool_calls_per_turn=None,
    )

    plain, confirmation, external_call = (call.execution for call in batch.planned)
    assert plain.to_dict() == {
        **plain.to_dict(),
        "requires_confirmation": None,
        "external_execution_required": None,
        "approval_type": None,
        "external_execution_silent": None,
    }
    assert (confirmation.requires_confirmation, confirmation.approval_type, confirmation.external_execution_silent) == (
        True,
        "mindroom_policy",
        None,
    )
    assert (external_call.external_execution_required, external_call.external_execution_silent) == (True, True)
    assert external_call.requires_confirmation is None
    assert batch.paused


# --- dispatch_tool_calls ----------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatch_runs_calls_concurrently_and_answers_in_call_order() -> None:
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

    functions = prepare_run_tools(spec(ScriptedModel([]), toolkit(meet))).functions
    batch = plan_tool_batch(
        [tool_call("meet", "c1", who="one"), tool_call("meet", "c2", who="two")],
        functions,
        [],
        calls_so_far=0,
        max_tool_calls_per_turn=None,
    )

    results = await dispatch_tool_calls(batch.planned, run=context())

    assert [(m.tool_call_id, m.content, m.tool_call_error) for m in results] == [
        ("c1", "one met two", False),
        ("c2", "two met one", False),
    ]
    for call, result in zip(batch.planned, results, strict=True):
        execution = call.execution
        assert (execution.result, execution.is_error) == (result.content, False)
        assert execution.metrics is not None
        assert execution.metrics.duration is not None
        assert execution.metrics.duration > 0
        assert execution.metrics.end_time is not None
        assert execution.metrics.start_time == pytest.approx(execution.metrics.end_time - execution.metrics.duration)


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

    functions = prepare_run_tools(spec(ScriptedModel([]), toolkit(_linger(release, finished), give_up))).functions
    batch = plan_tool_batch(
        [tool_call("linger", "c1"), tool_call("give_up", "c2")],
        functions,
        [],
        calls_so_far=0,
        max_tool_calls_per_turn=None,
    )
    dispatch = asyncio.create_task(dispatch_tool_calls(batch.planned, run=context()))
    for _ in range(5):
        await asyncio.sleep(0)
    assert not dispatch.done()  # give_up is gone, but linger still runs: the batch waits for it.

    release.set()
    with pytest.raises(asyncio.CancelledError):
        await dispatch
    assert finished == ["linger"]


@pytest.mark.asyncio
async def test_a_cancelled_dispatch_waits_for_every_call_to_unwind() -> None:
    release = asyncio.Event()
    finished: list[str] = []

    functions = prepare_run_tools(spec(ScriptedModel([]), toolkit(_linger(release, finished)))).functions
    batch = plan_tool_batch([tool_call("linger", "c1")], functions, [], calls_so_far=0, max_tool_calls_per_turn=None)
    dispatch = asyncio.create_task(dispatch_tool_calls(batch.planned, run=context()))
    for _ in range(5):
        await asyncio.sleep(0)

    dispatch.cancel()
    with pytest.raises(asyncio.CancelledError):
        await dispatch
    assert finished == ["linger"]


@pytest.mark.asyncio
async def test_dispatch_coerces_arguments_and_turns_failures_into_error_results() -> None:
    functions = prepare_run_tools(spec(ScriptedModel([]), toolkit(add, boom))).functions
    batch = plan_tool_batch(
        [
            raw_tool_call("add", "c1", '{"a": "3", "b": 4}'),
            raw_tool_call("add", "c2", '{"a": "three", "b": 4}'),
            tool_call("boom", "c3", reason="kaboom"),
        ],
        functions,
        [],
        calls_so_far=0,
        max_tool_calls_per_turn=None,
    )

    results = await dispatch_tool_calls(batch.planned, run=context())

    coerced, invalid, failed = results
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

    functions = prepare_run_tools(spec(ScriptedModel([]), toolkit(echo), gated(add))).functions
    batch = plan_tool_batch(
        [tool_call("add", "c1", a=1, b=2), tool_call("echo", "c2", text="hi")],
        functions,
        [],
        calls_so_far=0,
        max_tool_calls_per_turn=None,
    )

    results = await dispatch_tool_calls(batch.planned, run=context(), middleware=[middleware])

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
        return ToolResult(content=f"picture {label}", images=[AgnoImage(content=b"png", format="png")])

    functions = prepare_run_tools(spec(ScriptedModel([]), toolkit(picture, echo))).functions
    batch = plan_tool_batch(
        [
            tool_call("picture", "c1", label="a"),
            tool_call("echo", "c2", text="t"),
            tool_call("picture", "c3", label="b"),
        ],
        functions,
        [],
        calls_so_far=0,
        max_tool_calls_per_turn=None,
    )
    results = await dispatch_tool_calls(batch.planned, run=context())
    assert [len(m.images or ()) for m in results] == [1, 0, 1]

    follow_up = media_follow_up(results)

    assert follow_up is not None
    assert (follow_up.role, follow_up.content) == ("user", MEDIA_FOLLOW_UP_TEXT)
    assert [image.content for image in follow_up.images or ()] == [b"png", b"png"]
    assert all(m.images is None for m in results)
    assert media_follow_up(results) is None
    assert json.loads(json.dumps(results[0].to_dict()))["content"] == "picture a"

"""The model loop requests, runs tools, records usage, and fires each hook where Agno's patched stage was."""
# ruff: noqa: D103

from __future__ import annotations

import asyncio
from dataclasses import replace
from typing import TYPE_CHECKING, Any

import pytest
from agno.media import Image as AgnoImage
from agno.tools.function import ToolResult
from pydantic import BaseModel
from structlog.testing import capture_logs

from mindroom.agent_loop.model_loop import (
    NO_HOOKS,
    EndRun,
    LoopEnded,
    LoopHooks,
    LoopState,
    SendRequest,
    run_model_loop,
)
from mindroom.agent_loop.tools import MEDIA_FOLLOW_UP_TEXT, prepare_run_tools
from mindroom.llm.errors import ProviderError, RateLimited
from mindroom.llm.events import ReasoningDelta, RequestCompleted, ToolCompleted, ToolStarted
from mindroom.llm.messages import AGGREGATE_USAGE_KEY, Message
from mindroom.llm.provider import ModelRequest, TextDelta, TurnCompleted, UsageReported
from mindroom.llm.provider import ReasoningDelta as ReasoningEvent
from mindroom.llm.runs import RunRecord, RunRequirement, RunStatus, ToolExecution
from mindroom.llm.usage import RunMetrics
from mindroom.tool_system.invocation import MISSING_TOOL_ERROR_TEXT, ToolCall, ToolNext
from tests.agent_loop.helpers import add, boom, context, echo, external, gated, spec, toolkit
from tests.fakes.scripted_model import SYNTHETIC, Await, ScriptedModel, answer, calls, raw_tool_call, tool_call, usage

if TYPE_CHECKING:
    from collections.abc import Sequence

    from mindroom.agent_loop.model_loop import LoopEvent
    from mindroom.agent_loop.spec import AgentSpec
    from mindroom.agent_loop.tools import PlannedCall

pytestmark = pytest.mark.asyncio


def _record() -> RunRecord:
    return RunRecord(run_id="run-1", agent_id="code", metrics=RunMetrics())


def _prompt() -> list[Message]:
    return [Message(role="system", content="You are Code."), Message(role="user", content="hi")]


async def _run(
    agent: AgentSpec,
    messages: list[Message],
    *,
    hooks: LoopHooks = NO_HOOKS,
    record: RunRecord | None = None,
    request_template: ModelRequest | None = None,
    resumed: Sequence[RunRequirement] | None = None,
) -> tuple[list[LoopEvent], RunRecord]:
    record = record or _record()
    kwargs: dict[str, Any] = {} if request_template is None else {"request_template": request_template}
    if resumed is not None:
        kwargs["resumed"] = resumed
    events = [
        event
        async for event in run_model_loop(
            agent.model,
            messages,
            prepare_run_tools(agent),
            record=record,
            run=context(),
            hooks=hooks,
            max_tool_calls_per_turn=agent.max_tool_calls_per_turn,
            **kwargs,
        )
    ]
    return events, record


def _names(events: Sequence[LoopEvent]) -> list[str]:
    return [type(event).__name__ for event in events]


# --- plain requests --------------------------------------------------------------------------------------------------


async def test_a_text_answer_streams_deltas_records_usage_and_ends_the_loop() -> None:
    model = ScriptedModel([[ReasoningEvent("hmm"), *answer("Hello, Alice.", usage(12, 3, time_to_first_token=0.25))]])
    messages = _prompt()

    events, record = await _run(spec(model), messages)

    assert events[0] == ReasoningDelta("hmm")
    assert "".join(event.text for event in events if isinstance(event, TextDelta)) == "Hello, Alice."
    assert events[-2:] == [
        RequestCompleted(model_id="scripted-model", provider="Synthetic", usage=usage(12, 3, time_to_first_token=0.25)),
        LoopEnded(paused=False),
    ]
    assert record.content == "Hello, Alice."
    assert record.status is RunStatus.running  # The run, not the loop, ends the record.
    assert record.tools is None
    assert record.requirements is None
    assert record.metrics is not None
    assert record.metrics.to_dict() == {
        "input_tokens": 12,
        "output_tokens": 3,
        "total_tokens": 15,
        "time_to_first_token": 0.25,
        "details": {
            "model": [
                {
                    "input_tokens": 12,
                    "output_tokens": 3,
                    "total_tokens": 15,
                    "id": "scripted-model",
                    "provider": "Synthetic",
                },
            ],
        },
    }
    assistant = messages[-1]
    assert (assistant.role, assistant.content) == ("assistant", "Hello, Alice.")
    assert assistant.provider_data == {"mindroom_model": {"id": "scripted-model", "provider": "Synthetic"}}
    [request] = model.requests
    assert request.messages == tuple(messages[:2])
    assert request.tools == ()


async def test_request_settings_come_from_the_template_and_tools_from_the_spec() -> None:
    class Shape(BaseModel):
        answer: str

    model = ScriptedModel([answer('{"answer": "x"}')])
    template = ModelRequest(
        messages=(),
        tool_choice="none",
        response_format=Shape,
        deferred_tool_names=frozenset({"add"}),
    )

    await _run(spec(model, toolkit(add, echo)), _prompt(), request_template=template)

    [request] = model.requests
    assert (request.tool_choice, request.response_format, request.deferred_tool_names) == (
        "none",
        Shape,
        frozenset({"add"}),
    )
    assert [tool.name for tool in request.tools] == ["add", "echo"]
    assert request.tools[0].parameters == prepare_run_tools(spec(model, toolkit(add))).functions["add"].parameters


async def test_a_stream_that_never_completes_raises_a_provider_error() -> None:
    model = ScriptedModel([[TextDelta("partial")]])

    with pytest.raises(ProviderError, match="without completing the turn") as info:
        await _run(spec(model), _prompt())

    assert info.value.provider == "Synthetic"


# --- tool loop -------------------------------------------------------------------------------------------------------


async def test_tool_calls_run_in_parallel_and_their_results_feed_the_next_request() -> None:
    model = ScriptedModel(
        [
            calls(
                tool_call("add", "c1", a=2, b=3),
                tool_call("echo", "c2", text="yo"),
                content="Checking. ",
                metrics=usage(10, 2),
            ),
            answer("2 + 3 = 5.", usage(20, 4)),
        ],
    )
    messages = _prompt()

    events, record = await _run(spec(model, toolkit(add, echo)), messages)

    assert _names(events) == [
        "TextDelta",
        "RequestCompleted",
        "ToolStarted",
        "ToolStarted",
        "ToolCompleted",
        "ToolCompleted",
        "TextDelta",
        "TextDelta",
        "TextDelta",
        "RequestCompleted",
        "LoopEnded",
    ]
    started = [event.tool for event in events if isinstance(event, ToolStarted)]
    completed = [event.tool for event in events if isinstance(event, ToolCompleted)]
    assert [tool.tool_call_id for tool in started] == ["c1", "c2"] == [tool.tool_call_id for tool in completed]
    assert record.tools is not None
    assert [tool is entry for tool, entry in zip(started, record.tools, strict=True)]
    assert [(tool.result, tool.is_error) for tool in completed] == [('{"sum": 5}', False), ("yo", False)]
    assert record.content == "Checking. 2 + 3 = 5."
    assert [message.role for message in messages] == ["system", "user", "assistant", "tool", "tool", "assistant"]
    assert [m.tool_call_id for m in messages if m.role == "tool"] == ["c1", "c2"]
    second = model.requests[1]
    assert second.messages == tuple(messages[:5])
    assert record.metrics is not None
    assert (record.metrics.input_tokens, record.metrics.output_tokens) == (30, 6)


async def test_unknown_tools_and_bad_arguments_answer_the_model_with_agno_texts() -> None:
    model = ScriptedModel(
        [
            calls(
                tool_call("missing", "c1", x=1),
                raw_tool_call("add", "c2", '{"a": "three", "b": 4}'),
                raw_tool_call("add", "c3", '{"a": "3", "b": "4"}'),
                metrics=usage(1, 1),
            ),
            answer("ok"),
        ],
    )
    messages = _prompt()

    events, record = await _run(spec(model, toolkit(add)), messages)

    tool_messages = [message for message in messages if message.role == "tool"]
    assert [(m.tool_call_id, m.tool_call_error) for m in tool_messages] == [("c1", None), ("c2", True), ("c3", False)]
    assert tool_messages[0].content == MISSING_TOOL_ERROR_TEXT
    assert "validation error" in str(tool_messages[1].content)
    assert tool_messages[2].content == '{"sum": 7}'
    # Only resolved calls are announced; the unknown one was answered without running.
    assert [event.tool.tool_call_id for event in events if isinstance(event, ToolStarted)] == ["c2", "c3"]
    assert record.tools is not None
    assert [tool.tool_call_id for tool in record.tools] == ["c2", "c3"]
    assert model.requests[1].messages[-3:] == tuple(tool_messages)


async def test_the_tool_call_limit_refuses_counted_calls_and_caps_the_requests() -> None:
    model = ScriptedModel(
        [
            calls(tool_call("add", "c1", a=1, b=1), tool_call("add", "c2", a=2, b=2), metrics=usage(1, 1)),
            calls(tool_call("add", "c3", a=3, b=3), metrics=usage(1, 1)),
            calls(tool_call("add", "c4", a=4, b=4), metrics=usage(1, 1)),
            answer("never sent"),
        ],
    )
    messages = _prompt()

    with capture_logs() as logs:
        events, record = await _run(spec(model, toolkit(add), max_tool_calls_per_turn=1), messages)

    limit_text = "Tool call limit reached. Tool call add not executed. Don't try to execute it again."
    tool_messages = [message for message in messages if message.role == "tool"]
    # Within a batch the refused calls answer first, then the executed ones, as Agno appended them.
    assert [(m.tool_call_id, m.content) for m in tool_messages] == [
        ("c2", limit_text),
        ("c1", '{"sum": 2}'),
        ("c3", limit_text),
        ("c4", limit_text),
    ]
    # Refused calls are neither announced nor recorded as executions.
    assert [event.tool.tool_call_id for event in events if isinstance(event, ToolStarted)] == ["c1"]
    assert record.tools is not None
    assert len(record.tools) == 1
    assert len(model.requests) == 3
    assert len(model.turns) == 1
    assert events[-1] == LoopEnded(paused=False)
    assert [entry for entry in logs if entry["event"] == "tool_call_limit_reached"] == [
        {
            "event": "tool_call_limit_reached",
            "log_level": "warning",
            "entity": "code",
            "budget": 1,
            "model_requests": 3,
        },
    ]


async def test_a_stop_after_tool_call_ends_the_loop_without_another_request() -> None:
    model = ScriptedModel([calls(tool_call("echo", "c1", text="switch"), metrics=usage(1, 1)), answer("never sent")])
    messages = _prompt()

    events, record = await _run(spec(model, toolkit(echo, stop_after_call=True)), messages)

    assert len(model.requests) == 1
    assert events[-1] == LoopEnded(paused=False)
    assert record.tools is not None
    assert record.tools[0].stop_after_call is True
    assert messages[-1].role == "tool"
    assert messages[-1].stop_after_tool_call is True


async def test_a_batch_that_stops_the_run_skips_the_batch_hooks_as_agno_did() -> None:
    """No later request reads what a batch hook adds after a stopping batch, so Agno fired none there."""
    batches: list[list[str | None]] = []

    async def after_batch(_messages: list[Message], results: Sequence[Message]) -> None:
        batches.append([result.tool_call_id for result in results])

    model = ScriptedModel(
        [
            calls(tool_call("add", "c1", a=1, b=2), metrics=usage(1, 1)),
            calls(tool_call("echo", "c2", text="switch"), metrics=usage(1, 1)),
        ],
    )

    await _run(
        spec(model, toolkit(add), toolkit(echo, name="stopper", stop_after_call=True)),
        _prompt(),
        hooks=LoopHooks(after_tool_batch=(after_batch,)),
    )

    assert batches == [["c1"]]


async def test_tool_media_reach_the_model_on_a_follow_up_user_message() -> None:
    def picture(label: str) -> ToolResult:
        """Return a picture.

        Args:
            label: Its label.

        """
        return ToolResult(content=f"picture {label}", images=[AgnoImage(content=b"png", format="png")])

    model = ScriptedModel([calls(tool_call("picture", "c1", label="a"), metrics=usage(1, 1)), answer("I see it.")])
    messages = _prompt()

    await _run(spec(model, toolkit(picture)), messages)

    result, follow_up, final = messages[3:]
    assert (result.role, result.content, result.images) == ("tool", "picture a", None)
    assert (follow_up.role, follow_up.content) == ("user", MEDIA_FOLLOW_UP_TEXT)
    assert [image.content for image in follow_up.images or ()] == [b"png"]
    assert final.content == "I see it."
    assert model.requests[1].messages[-2:] == (result, follow_up)


async def test_a_confirmation_pause_announces_the_call_records_a_requirement_and_ends_the_loop() -> None:
    model = ScriptedModel(
        [
            calls(
                tool_call("echo", "c1", text="fine"),
                tool_call("add", "c2", a=6, b=7),
                content="Checking first.",
                metrics=usage(1, 1),
            ),
            answer("never sent"),
        ],
    )
    messages = _prompt()

    events, record = await _run(spec(model, toolkit(echo), gated(add)), messages)

    assert events[-1] == LoopEnded(paused=True)
    assert [event.tool.tool_call_id for event in events if isinstance(event, ToolStarted)] == ["c1", "c2"]
    assert [event.tool.tool_call_id for event in events if isinstance(event, ToolCompleted)] == ["c1"]
    assert len(model.requests) == 1
    assert record.tools is not None
    assert record.requirements is not None
    plain, paused = record.tools
    assert (plain.result, plain.is_error, plain.is_paused) == ("fine", False, False)
    assert paused.to_dict() == {
        **paused.to_dict(),
        "tool_call_id": "c2",
        "tool_name": "add",
        "tool_args": {"a": 6, "b": 7},
        "result": None,
        "tool_call_error": None,
        "requires_confirmation": True,
        "confirmed": None,
        "approval_type": "mindroom_policy",
        "external_execution_required": None,
        "external_execution_silent": None,
    }
    [requirement] = record.requirements
    assert requirement.tool_execution is paused
    assert requirement.needs_confirmation
    assert [m.tool_call_id for m in messages if m.role == "tool"] == ["c1"]
    assert record.content == "Checking first."


async def test_a_tool_that_leaves_a_decision_on_the_record_pauses_the_loop_after_its_batch() -> None:
    """A delegation whose member paused leaves the member's requirement on the record; the loop pauses instead of asking."""
    record = _record()

    def delegate(task: str) -> str:
        """Hand a task to a member that pauses on its own call.

        Args:
            task: What the member should do.

        """
        execution = ToolExecution(tool_call_id="m1", tool_name="multiply", tool_args={"task": task})
        execution.requires_confirmation = True
        record.requirements = [RunRequirement(execution, member_agent_id="checker", member_run_id="member-run")]
        return "Member 'Checker' requires human input before continuing."

    model = ScriptedModel([calls(tool_call("delegate", "c1", task="Multiply 6 by 7"), metrics=usage(1, 1))])

    events, _ = await _run(spec(model, toolkit(delegate)), _prompt(), record=record)

    assert events[-1] == LoopEnded(paused=True)
    assert len(model.requests) == 1
    assert record.tools is not None
    assert (record.tools[0].result, record.tools[0].is_paused) == (
        "Member 'Checker' requires human input before continuing.",
        False,
    )


async def test_a_resume_that_leaves_the_record_waiting_ends_paused_before_any_request() -> None:
    """A team resume's paused member may leave the run waiting again; the loop sends nothing while it waits."""
    record = _record()
    execution = ToolExecution(tool_call_id="m1", tool_name="multiply")
    execution.requires_confirmation = True
    record.requirements = [RunRequirement(execution, member_agent_id="checker", member_run_id="member-run")]
    model = ScriptedModel([])

    events, _ = await _run(spec(model), _prompt(), record=record, resumed=())

    assert events == [LoopEnded(paused=True)]
    assert model.requests == []


async def test_an_external_execution_pause_keeps_the_silent_flag() -> None:
    model = ScriptedModel([calls(tool_call("echo", "c1", text="task"), metrics=usage(1, 1))])

    events, record = await _run(spec(model, external(echo)), _prompt())

    assert events[-1] == LoopEnded(paused=True)
    assert record.tools is not None
    assert record.requirements is not None
    [paused] = record.tools
    assert (paused.external_execution_required, paused.external_execution_silent, paused.requires_confirmation) == (
        True,
        True,
        None,
    )
    assert record.requirements[0].needs_external_execution


# --- hooks -----------------------------------------------------------------------------------------------------------


async def test_every_hook_fires_at_its_documented_point() -> None:
    log: list[tuple[object, ...]] = []

    async def first(state: LoopState) -> SendRequest:
        log.append(("before_request", "first", state.request_index, len(state.messages), len(state.tools)))
        return SendRequest()

    async def second(state: LoopState) -> SendRequest:
        log.append(("before_request", "second", state.request_index))
        return SendRequest()

    async def before_dispatch(state: LoopState, planned: Sequence[PlannedCall]) -> None:
        log.append(("before_dispatch", [call.function.name for call in planned], state.messages[-1].role))

    async def middleware(call: ToolCall, call_next: ToolNext) -> object:
        log.append(("middleware", call.function.name, call.arguments))
        return await call_next(call.arguments)

    async def after_batch(messages: list[Message], results: Sequence[Message]) -> None:
        log.append(("after_tool_batch", [result.tool_call_id for result in results], messages[-1] is results[-1]))

    def loop_end(state: LoopState, request: ModelRequest) -> None:
        log.append(("on_loop_end", state.request_index, len(request.messages), state.messages[-1].content))

    hooks = LoopHooks(
        before_request=(first, second),
        before_dispatch=before_dispatch,
        tool_middleware=(middleware,),
        after_tool_batch=(after_batch,),
        on_loop_end=loop_end,
    )
    model = ScriptedModel([calls(tool_call("echo", "c1", text="x"), metrics=usage(1, 1)), answer("done")])

    await _run(spec(model, toolkit(echo)), _prompt(), hooks=hooks)

    assert log == [
        ("before_request", "first", 0, 2, 1),
        ("before_request", "second", 0),
        ("before_dispatch", ["echo"], "assistant"),
        ("middleware", "echo", {"text": "x"}),
        ("after_tool_batch", ["c1"], True),
        ("before_request", "first", 1, 4, 1),
        ("before_request", "second", 1),
        ("on_loop_end", 2, 4, "done"),
    ]


async def test_a_hook_that_ends_the_run_stops_before_the_request_and_later_hooks() -> None:
    seen: list[str] = []

    async def stay_silent(state: LoopState) -> EndRun | SendRequest:
        seen.append("gate")
        return EndRun() if state.request_index == 1 else SendRequest()

    async def never_after_an_end(state: LoopState) -> SendRequest:
        seen.append(f"after:{state.request_index}")
        return SendRequest()

    model = ScriptedModel(
        [calls(tool_call("echo", "c1", text="x"), content="Partial.", metrics=usage(1, 1)), answer("never")],
    )
    messages = _prompt()

    events, record = await _run(
        spec(model, toolkit(echo)),
        messages,
        hooks=LoopHooks(before_request=(stay_silent, never_after_an_end)),
    )

    assert seen == ["gate", "after:0", "gate"]
    assert len(model.requests) == 1
    assert events[-1] == LoopEnded(paused=False)
    assert record.content == "Partial."
    assert messages[-1].role == "tool"


async def test_a_projected_request_is_sent_without_changing_the_live_list() -> None:
    receipt = Message(role="user", content="Approval receipt", add_to_agent_memory=False)

    async def project(state: LoopState) -> SendRequest:
        return SendRequest(messages=[*state.messages, receipt])

    model = ScriptedModel([answer("seen")])
    messages = _prompt()

    await _run(spec(model), messages, hooks=LoopHooks(before_request=(project,)))

    [request] = model.requests
    assert request.messages == (*messages[:2], receipt)
    assert [message.role for message in messages] == ["system", "user", "assistant"]


async def test_a_pre_request_hook_rewrites_the_live_list_in_place() -> None:
    async def compact(state: LoopState) -> SendRequest:
        if state.request_index == 1:
            del state.messages[1:3]  # Fold the first exchange away before the second request.
        return SendRequest()

    model = ScriptedModel([calls(tool_call("echo", "c1", text="x"), metrics=usage(1, 1)), answer("done")])
    messages = _prompt()

    await _run(spec(model, toolkit(echo)), messages, hooks=LoopHooks(before_request=(compact,)))

    assert [message.role for message in model.requests[1].messages] == ["system", "tool"]
    assert [message.role for message in messages] == ["system", "tool", "assistant"]


async def test_rewritten_tool_calls_are_what_runs_and_what_the_message_stores() -> None:
    def rewrite(tool_calls: list[dict[str, Any]], functions: Any) -> list[dict[str, Any]]:  # noqa: ANN401
        assert set(functions) == {"echo"}
        return [tool_call("echo", "c1", text="real") if call["id"] == "c1" else call for call in tool_calls]

    model = ScriptedModel([calls(tool_call("call_tool", "c1", name="echo"), metrics=usage(1, 1)), answer("done")])
    messages = _prompt()

    events, record = await _run(spec(model, toolkit(echo)), messages, hooks=LoopHooks(rewrite_tool_calls=rewrite))

    assert record.tools is not None
    assert (record.tools[0].tool_name, record.tools[0].result) == ("echo", "real")
    assert messages[2].tool_calls == [tool_call("echo", "c1", text="real")]
    assert [event.tool.tool_name for event in events if isinstance(event, ToolStarted)] == ["echo"]


async def test_a_tool_exception_is_an_error_result_not_a_failed_loop() -> None:
    model = ScriptedModel([calls(tool_call("boom", "c1", reason="kaboom"), metrics=usage(1, 1)), answer("recovered")])
    messages = _prompt()

    events, record = await _run(spec(model, toolkit(boom)), messages)

    assert events[-1] == LoopEnded(paused=False)
    assert record.tools is not None
    assert (record.tools[0].result, record.tools[0].is_error) == ("kaboom", True)
    assert (messages[3].content, messages[3].tool_call_error) == ("kaboom", True)


# --- interrupted requests --------------------------------------------------------------------------------------------


async def test_cancellation_mid_stream_settles_the_received_usage_once() -> None:
    streaming = asyncio.Event()
    hold = asyncio.Event()

    async def wait_to_be_cancelled() -> None:
        streaming.set()
        await hold.wait()

    model = ScriptedModel([[TextDelta("Par"), UsageReported(usage(50, 2), final=False), Await(wait_to_be_cancelled)]])
    messages = _prompt()
    record = _record()

    async def consume() -> None:
        await _run(spec(model), messages, record=record)

    task = asyncio.create_task(consume())
    await asyncio.wait_for(streaming.wait(), timeout=5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    settled = messages[-1]
    assert [message.role for message in messages] == ["system", "user", "assistant"]
    assert (settled.content, settled.metrics) == (None, usage(50, 2))
    assert settled.provider_data == {"mindroom_model": {"id": "scripted-model", "provider": "Synthetic"}}
    assert record.metrics is not None
    assert (record.metrics.input_tokens, record.metrics.output_tokens, record.metrics.total_tokens) == (50, 2, 52)
    assert record.metrics.details is not None
    assert len(record.metrics.details["model"]) == 1
    assert record.content is None


@pytest.mark.parametrize("retried", [False, True], ids=["one-attempt", "retried-sum"])
@pytest.mark.parametrize("ending", ["completes", "cancelled", "fails"])
async def test_a_turn_interrupted_after_it_completed_and_before_the_loop_took_it_counts_its_usage_once(
    ending: str,
    retried: bool,
) -> None:
    draining = asyncio.Event()
    release = asyncio.Event()

    async def finish_the_stream() -> None:
        # What a pipeline stage does after the turn completed, such as writing the response log.
        draining.set()
        await release.wait()
        if ending == "fails":
            msg = "log write failed"
            raise ProviderError(msg, provider="Synthetic")

    # The finished turn alone carries the usage, as a provider that reports none along the way gives it; after a
    # retry it is the attempts' sum, which the pipeline marks.
    marker = {AGGREGATE_USAGE_KEY: True} if retried else None
    finished = TurnCompleted(
        message=Message(role="assistant", content="done", metrics=usage(50, 2), provider_data=marker),
        stop_reason="end",
    )
    model = ScriptedModel([[TextDelta("done"), finished, Await(finish_the_stream)]])
    messages = _prompt()
    record = _record()
    task = asyncio.create_task(_run(spec(model), messages, record=record))
    await asyncio.wait_for(draining.wait(), timeout=5)

    if ending == "cancelled":
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        release.set()
        if ending == "fails":
            with pytest.raises(ProviderError, match="log write failed"):
                await task
        else:
            await task

    assert [message.role for message in messages] == ["system", "user", "assistant"]
    if ending == "completes":
        assert (messages[-1].content, record.content) == ("done", "done")
    else:
        # The loop never took the turn, so only its usage is kept, on a message of its own.
        assert (messages[-1].content, messages[-1].tool_calls, record.content) == (None, None, None)
    assert messages[-1].metrics == usage(50, 2)
    # Kept or cut off, the usage reads as one request only when it was one.
    assert messages[-1].provider_data == {
        **(marker or {}),
        "mindroom_model": {"id": "scripted-model", "provider": "Synthetic"},
    }
    assert record.metrics is not None
    assert (record.metrics.input_tokens, record.metrics.output_tokens, record.metrics.total_tokens) == (50, 2, 52)
    assert record.metrics.details is not None
    assert len(record.metrics.details["model"]) == 1


async def test_a_provider_error_mid_stream_keeps_the_received_usage_and_propagates() -> None:
    async def throttle() -> None:
        msg = "slow down"
        raise RateLimited(msg, provider="Synthetic")

    model = ScriptedModel([[UsageReported(usage(7, 0), final=False), Await(throttle)]])
    messages = _prompt()
    record = _record()

    with pytest.raises(RateLimited, match="slow down"):
        await _run(spec(model), messages, record=record)

    assert messages[-1].metrics == usage(7, 0)
    assert record.metrics is not None
    assert record.metrics.input_tokens == 7


async def test_a_cut_off_request_without_usage_leaves_no_message_behind() -> None:
    async def fail() -> None:
        msg = "slow down"
        raise RateLimited(msg, provider="Synthetic")

    model = ScriptedModel([[TextDelta("Par"), Await(fail)]])
    messages = _prompt()

    with pytest.raises(RateLimited):
        await _run(spec(model), messages)

    assert [message.role for message in messages] == ["system", "user"]


async def test_metrics_time_to_first_token_comes_from_the_first_request_that_reports_one() -> None:
    model = ScriptedModel(
        [
            calls(tool_call("echo", "c1", text="x"), metrics=usage(1, 1)),
            answer("done", usage(2, 2, time_to_first_token=0.4)),
        ],
    )

    _, record = await _run(spec(model, toolkit(echo)), _prompt())

    assert record.metrics is not None
    assert record.metrics.time_to_first_token == 0.4


async def test_identity_strings_reach_the_record_metrics_and_message_stamp() -> None:
    model = ScriptedModel(
        [answer("hi", usage(1, 1))],
        identity=replace(SYNTHETIC, id="m", provider="OpenAI", provider_name="OpenAI Chat"),
    )
    messages = _prompt()

    events, record = await _run(spec(model), messages)

    assert events[-2] == RequestCompleted(model_id="m", provider="OpenAI Chat", usage=usage(1, 1))
    assert record.metrics is not None
    assert record.metrics.details == {
        "model": [replace(record.metrics.details["model"][0], id="m", provider="OpenAI Chat")],
    }
    assert messages[-1].provider_data == {"mindroom_model": {"id": "m", "provider": "OpenAI Chat"}}

"""A run's tool-call budget ends every runaway tool loop through the loop's normal completion path."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from structlog.testing import capture_logs

from mindroom.agent_loop.run import run_agent
from mindroom.llm.runs import RunStatus
from tests.agent_loop.helpers import collect, context, spec, toolkit
from tests.fakes.scripted_model import ScriptedModel, answer, calls, raw_tool_call, usage

if TYPE_CHECKING:
    from mindroom.llm.messages import Message
    from tests.fakes.scripted_model import ScriptStep

_BUDGET = 2
_CAP = _BUDGET + 2

# Each shape is a tool call the model repeats on every request: a real tool the loop counts against the budget,
# and two calls the loop answers before any budget accounting.
_LOOP_SHAPES = {
    "known_tool": ("probe", "{}"),
    "unknown_tool": ("no_such_tool", "{}"),
    "unparseable_arguments": ("probe", "{not json"),
}

executed: list[str] = []


def probe() -> str:
    """Record one call."""
    executed.append("probe")
    return "probed"


def _tool_turn(index: int, tool_name: str, arguments: str) -> list[ScriptStep]:
    return calls(raw_tool_call(tool_name, f"call_{index}", arguments), content="Working. ", metrics=usage(10, 1))


async def _run(
    turns: list[list[ScriptStep]],
    *,
    run_id: str = "run-1",
) -> tuple[ScriptedModel, list[Message], RunStatus]:
    model = ScriptedModel(turns)
    _, record = await collect(
        run_agent(spec(model, toolkit(probe), tool_call_limit=_BUDGET), [], ctx=context(run_id=run_id)),
    )
    return model, record.messages, record.status


def _cap_warnings(logs: list[dict[str, object]]) -> list[dict[str, object]]:
    return [entry for entry in logs if entry.get("event") == "tool_call_limit_reached"]


@pytest.fixture(autouse=True)
def _reset_executed() -> None:
    executed.clear()


@pytest.mark.asyncio
@pytest.mark.parametrize("shape", list(_LOOP_SHAPES))
async def test_model_that_keeps_requesting_tools_ends_its_run(shape: str) -> None:
    """Whatever the loop's tool call, the run completes after budget + 2 model requests with the text so far."""
    tool_name, arguments = _LOOP_SHAPES[shape]
    model = ScriptedModel([_tool_turn(index, tool_name, arguments) for index in range(_CAP + 1)])

    with capture_logs() as logs:
        _, record = await collect(run_agent(spec(model, toolkit(probe), tool_call_limit=_BUDGET), [], ctx=context()))

    assert len(model.requests) == _CAP
    assert executed == (["probe"] * _BUDGET if shape == "known_tool" else [])
    assert record.status is RunStatus.completed
    assert record.content == "Working. " * _CAP
    # The refused request reached no provider, so it adds no usage of its own.
    assert record.metrics is not None
    assert record.metrics.input_tokens == 10 * _CAP
    assert [
        (entry["entity"], entry["budget"], entry["model_requests"], entry["log_level"]) for entry in _cap_warnings(logs)
    ] == [("code", _BUDGET, _CAP, "warning")]


@pytest.mark.asyncio
async def test_model_that_answers_after_the_first_refused_batch_keeps_its_closing_reply() -> None:
    """The request after the first refused batch still reaches the model, and its answer ends the run."""
    turns = [_tool_turn(index, "probe", "{}") for index in range(_BUDGET + 1)]
    turns.append(answer("Closing reply."))
    model = ScriptedModel(turns)

    with capture_logs() as logs:
        _, record = await collect(run_agent(spec(model, toolkit(probe), tool_call_limit=_BUDGET), [], ctx=context()))

    assert len(model.requests) == _CAP
    assert executed == ["probe"] * _BUDGET
    assert record.status is RunStatus.completed
    assert record.content == "Working. " * (_BUDGET + 1) + "Closing reply."
    assert _cap_warnings(logs) == []


@pytest.mark.asyncio
async def test_tool_calls_within_the_budget_are_untouched() -> None:
    """A run that stays within its budget neither warns nor ends early."""
    turns = [_tool_turn(index, "probe", "{}") for index in range(_BUDGET)]
    turns.append(answer("Closing reply."))
    model = ScriptedModel(turns)

    with capture_logs() as logs:
        _, record = await collect(run_agent(spec(model, toolkit(probe), tool_call_limit=_BUDGET), [], ctx=context()))

    assert len(model.requests) == _BUDGET + 1
    assert executed == ["probe"] * _BUDGET
    assert record.content == "Working. " * _BUDGET + "Closing reply."
    assert _cap_warnings(logs) == []


@pytest.mark.asyncio
async def test_each_run_starts_a_fresh_count() -> None:
    """A finished run leaves no count behind for the next run of the same agent."""
    turns = [_tool_turn(index, "probe", "{}") for index in range(2 * _CAP)]
    model = ScriptedModel(turns)
    agent = spec(model, toolkit(probe), tool_call_limit=_BUDGET)

    with capture_logs() as logs:
        await collect(run_agent(agent, [], ctx=context(run_id="run-1")))
        await collect(run_agent(agent, [], ctx=context(run_id="run-2")))

    assert len(model.requests) == 2 * _CAP
    assert executed == ["probe"] * (2 * _BUDGET)
    assert [entry["model_requests"] for entry in _cap_warnings(logs)] == [_CAP, _CAP]

"""A turn's model-call cap ends every runaway tool loop through Agno's normal completion path."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Literal

import pytest
from agno.agent import Agent
from agno.models.message import MessageMetrics
from agno.models.response import ModelResponse
from agno.run.agent import RunCompletedEvent, RunOutput
from agno.run.base import RunStatus
from agno.run.team import RunCompletedEvent as TeamRunCompletedEvent
from agno.run.team import TeamRunOutput
from agno.team import Team
from structlog.testing import capture_logs

from mindroom.synthetic_model import SyntheticModel
from mindroom.tool_call_budget import install_model_call_cap

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from agno.models.message import Message

_BUDGET = 2
_CAP = _BUDGET + 2
_SAFETY_CAP = 50

# Each shape is a tool call the model repeats on every request: a real tool that Agno counts
# against tool_call_limit, and two calls Agno answers before any limit accounting.
_LOOP_SHAPES = {
    "known_tool": ("probe", "{}"),
    "unknown_tool": ("no_such_tool", "{}"),
    "unparseable_arguments": ("probe", "{not json"),
}


class _ToolLoopModel(SyntheticModel):
    """Provider double that asks for the same tool call on every request of the real Agno loop."""

    def __init__(self, *, tool_name: str = "probe", arguments: str = "{}", answer_after: int | None = None) -> None:
        super().__init__(id="test", name="test", provider="test")
        self.requests = 0
        self.tool_name = tool_name
        self.arguments = arguments
        self.answer_after = answer_after

    async def ainvoke(self, messages: list[Message], **_kwargs: object) -> ModelResponse:  # noqa: ARG002
        self.requests += 1
        if self.requests > _SAFETY_CAP:
            msg = "the run kept calling the model"
            raise AssertionError(msg)
        if self.answer_after is not None and self.requests > self.answer_after:
            return ModelResponse(content="Closing reply.")
        return ModelResponse(
            content="Working. ",
            tool_calls=[
                {
                    "id": f"call_{self.requests}",
                    "type": "function",
                    "function": {"name": self.tool_name, "arguments": self.arguments},
                },
            ],
            response_usage=MessageMetrics(input_tokens=10, output_tokens=1, total_tokens=11),
        )

    async def ainvoke_stream(self, messages: list[Message], **kwargs: object) -> AsyncIterator[ModelResponse]:
        yield await self.ainvoke(messages, **kwargs)


def _capped_entity(kind: Literal["agent", "team"], model: SyntheticModel, executed: list[str]) -> Agent | Team:
    def probe() -> str:
        executed.append("probe")
        return "probed"

    install_model_call_cap(model, entity_name=f"runaway_{kind}")
    if kind == "agent":
        return Agent(name="Runaway", model=model, tools=[probe], tool_call_limit=_BUDGET, telemetry=False)
    member = Agent(name="Member", model=SyntheticModel(id="member", name="member", provider="test"), telemetry=False)
    return Team(
        name="Runaway Team",
        model=model,
        members=[member],
        tools=[probe],
        tool_call_limit=_BUDGET,
        telemetry=False,
    )


async def _run(
    entity: Agent | Team,
    *,
    stream: bool,
    prompt: str = "Do the task",
) -> tuple[RunOutput | TeamRunOutput, bool]:
    """Run once and return the final output plus whether a streamed run emitted its completion event."""
    if not stream:
        output = await entity.arun(prompt)
        assert isinstance(output, RunOutput | TeamRunOutput)
        return output, True
    final_output: RunOutput | TeamRunOutput | None = None
    completed = False
    async for event in entity.arun(prompt, stream=True, stream_events=True, yield_run_output=True):
        if isinstance(event, RunCompletedEvent | TeamRunCompletedEvent):
            completed = True
        if isinstance(event, RunOutput | TeamRunOutput):
            final_output = event
    assert final_output is not None
    return final_output, completed


def _cap_warnings(logs: list[dict[str, object]]) -> list[dict[str, object]]:
    return [entry for entry in logs if entry.get("event") == "tool_call_limit_reached"]


@pytest.mark.asyncio
@pytest.mark.parametrize("shape", list(_LOOP_SHAPES))
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("kind", ["agent", "team"])
async def test_model_that_keeps_requesting_tools_ends_its_run(
    kind: Literal["agent", "team"],
    shape: str,
    *,
    stream: bool,
) -> None:
    """Whatever the loop's tool call, the run completes after budget + 2 model requests with the text so far."""
    tool_name, arguments = _LOOP_SHAPES[shape]
    model = _ToolLoopModel(tool_name=tool_name, arguments=arguments)
    executed: list[str] = []
    entity = _capped_entity(kind, model, executed)

    with capture_logs() as logs:
        output, completed = await _run(entity, stream=stream)

    assert model.requests == _CAP
    assert executed == (["probe"] * _BUDGET if shape == "known_tool" else [])
    assert output.status is RunStatus.completed
    assert completed
    assert output.content == "Working. " * _CAP
    # The refused request reached no provider, so it adds no usage of its own.
    assert output.metrics is not None
    assert output.metrics.input_tokens == 10 * _CAP
    assert [
        (entry["entity"], entry["budget"], entry["model_requests"], entry["log_level"]) for entry in _cap_warnings(logs)
    ] == [(f"runaway_{kind}", _BUDGET, _CAP, "warning")]


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("kind", ["agent", "team"])
async def test_model_that_answers_after_the_first_refused_batch_keeps_its_closing_reply(
    kind: Literal["agent", "team"],
    *,
    stream: bool,
) -> None:
    """The request after the first refused batch still reaches the model, and its answer ends the run."""
    model = _ToolLoopModel(answer_after=_BUDGET + 1)
    executed: list[str] = []
    entity = _capped_entity(kind, model, executed)

    with capture_logs() as logs:
        output, completed = await _run(entity, stream=stream)

    assert model.requests == _CAP
    assert executed == ["probe"] * _BUDGET
    assert output.status is RunStatus.completed
    assert completed
    assert output.content == "Working. " * (_BUDGET + 1) + "Closing reply."
    assert _cap_warnings(logs) == []


@pytest.mark.asyncio
async def test_tool_calls_within_the_budget_are_untouched() -> None:
    """A run that stays within its budget neither warns nor ends early."""
    model = _ToolLoopModel(answer_after=_BUDGET)
    executed: list[str] = []
    entity = _capped_entity("agent", model, executed)

    with capture_logs() as logs:
        output, _completed = await _run(entity, stream=False)

    assert model.requests == _BUDGET + 1
    assert executed == ["probe"] * _BUDGET
    assert output.content == "Working. " * _BUDGET + "Closing reply."
    assert _cap_warnings(logs) == []


@pytest.mark.asyncio
async def test_each_run_starts_a_fresh_count() -> None:
    """A finished run leaves no count behind for the next run on the same model."""
    model = _ToolLoopModel()
    entity = _capped_entity("agent", model, [])

    with capture_logs() as logs:
        await _run(entity, stream=False)
        await _run(entity, stream=True)

    assert model.requests == 2 * _CAP
    assert [entry["model_requests"] for entry in _cap_warnings(logs)] == [_CAP, _CAP]


class _PromptedLoopModel(SyntheticModel):
    """Shared provider double: loops forever on "Loop", answers after the budget on "Work"."""

    def __init__(self) -> None:
        super().__init__(id="shared", name="shared", provider="test")
        self.requests = {"Loop": 0, "Work": 0}

    async def ainvoke(self, messages: list[Message], **_kwargs: object) -> ModelResponse:
        prompt = next(message.get_content_string() for message in messages if message.role == "user")
        self.requests[prompt] += 1
        if sum(self.requests.values()) > _SAFETY_CAP:
            msg = "a run kept calling the model"
            raise AssertionError(msg)
        # Yield so the two runs' model requests interleave on the shared model.
        await asyncio.sleep(0)
        if prompt == "Work" and sum(message.role == "tool" for message in messages) >= _BUDGET:
            return ModelResponse(content="Closing reply.")
        call_id = f"{prompt}_{self.requests[prompt]}"
        return ModelResponse(
            content=f"{prompt}. ",
            tool_calls=[{"id": call_id, "type": "function", "function": {"name": "probe", "arguments": "{}"}}],
        )

    async def ainvoke_stream(self, messages: list[Message], **kwargs: object) -> AsyncIterator[ModelResponse]:
        yield await self.ainvoke(messages, **kwargs)


def probe() -> str:
    """Stand-in tool the shared model double requests."""
    return "probed"


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
async def test_concurrent_runs_sharing_a_model_keep_separate_counts(*, stream: bool) -> None:
    """Only the runaway run stops; a concurrent run on the same model object keeps its own full count."""
    model = _PromptedLoopModel()
    install_model_call_cap(model, entity_name="shared_agent")
    runaway_agent, worker_agent = (
        Agent(name=name, model=model, tools=[probe], tool_call_limit=_BUDGET, telemetry=False)
        for name in ("Runaway", "Worker")
    )

    with capture_logs() as logs:
        (runaway, _), (worker, _) = await asyncio.gather(
            _run(runaway_agent, stream=stream, prompt="Loop"),
            _run(worker_agent, stream=stream, prompt="Work"),
        )

    assert model.requests == {"Loop": _CAP, "Work": _BUDGET + 1}
    assert runaway.status is RunStatus.completed
    assert runaway.content == "Loop. " * _CAP
    assert worker.status is RunStatus.completed
    assert worker.content == "Work. " * _BUDGET + "Closing reply."
    assert [(entry["entity"], entry["model_requests"]) for entry in _cap_warnings(logs)] == [("shared_agent", _CAP)]

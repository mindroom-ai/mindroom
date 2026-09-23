"""A turn's tool-call budget ends a runaway tool loop after one closing response."""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

import pytest
from agno.agent import Agent
from agno.models.response import ModelResponse
from agno.run.agent import RunCompletedEvent, RunOutput
from agno.run.base import RunStatus
from agno.run.team import RunCompletedEvent as TeamRunCompletedEvent
from agno.run.team import TeamRunOutput
from agno.team import Team
from structlog.testing import capture_logs

from mindroom.synthetic_model import SyntheticModel
from mindroom.tool_call_budget import install_tool_call_budget

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from agno.models.message import Message

_LIMIT = 2
_SAFETY_CAP = 50


class _ToolLoopModel(SyntheticModel):
    """Provider double that asks for another tool call on every request of the real Agno loop."""

    def __init__(self, *, closing_reply_after: int | None = None) -> None:
        super().__init__(id="test", name="test", provider="test")
        self.requests = 0
        self.closing_reply_after = closing_reply_after

    async def ainvoke(self, messages: list[Message], **_kwargs: object) -> ModelResponse:  # noqa: ARG002
        self.requests += 1
        if self.requests > _SAFETY_CAP:
            msg = "the run kept calling the model"
            raise AssertionError(msg)
        if self.closing_reply_after is not None and self.requests > self.closing_reply_after:
            return ModelResponse(content="Closing reply.")
        return ModelResponse(
            content="Working. ",
            tool_calls=[
                {"id": f"call_{self.requests}", "type": "function", "function": {"name": "probe", "arguments": "{}"}},
            ],
        )

    async def ainvoke_stream(self, messages: list[Message], **kwargs: object) -> AsyncIterator[ModelResponse]:
        yield await self.ainvoke(messages, **kwargs)


def _budgeted_entity(kind: Literal["agent", "team"], model: _ToolLoopModel, executed: list[str]) -> Agent | Team:
    def probe() -> str:
        executed.append("probe")
        return "probed"

    install_tool_call_budget(model, entity_name=f"runaway_{kind}")
    if kind == "agent":
        return Agent(name="Runaway", model=model, tools=[probe], tool_call_limit=_LIMIT, telemetry=False)
    member = Agent(name="Member", model=SyntheticModel(id="member", name="member", provider="test"), telemetry=False)
    return Team(
        name="Runaway Team",
        model=model,
        members=[member],
        tools=[probe],
        tool_call_limit=_LIMIT,
        telemetry=False,
    )


async def _run(entity: Agent | Team, *, stream: bool) -> tuple[RunOutput | TeamRunOutput, bool]:
    """Run once and return the final output plus whether a streamed run emitted its completion event."""
    if not stream:
        output = await entity.arun("Do the task")
        assert isinstance(output, RunOutput | TeamRunOutput)
        return output, True
    final_output: RunOutput | TeamRunOutput | None = None
    completed = False
    async for event in entity.arun("Do the task", stream=True, stream_events=True, yield_run_output=True):
        if isinstance(event, RunCompletedEvent | TeamRunCompletedEvent):
            completed = True
        if isinstance(event, RunOutput | TeamRunOutput):
            final_output = event
    assert final_output is not None
    return final_output, completed


def _limit_warnings(logs: list[dict[str, object]]) -> list[dict[str, object]]:
    return [entry for entry in logs if entry.get("event") == "tool_call_limit_reached"]


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("kind", ["agent", "team"])
async def test_model_that_ignores_the_refusal_ends_its_run(kind: Literal["agent", "team"], *, stream: bool) -> None:
    """After the first refused batch the model gets one response; asking for tools again ends the run normally."""
    model = _ToolLoopModel()
    executed: list[str] = []
    entity = _budgeted_entity(kind, model, executed)

    with capture_logs() as logs:
        output, completed = await _run(entity, stream=stream)

    # Limit batches run, the next batch is refused, and the one grace response asks for tools again.
    assert model.requests == _LIMIT + 2
    assert executed == ["probe"] * _LIMIT
    assert output.status is RunStatus.completed
    assert completed
    assert output.content == "Working. " * (_LIMIT + 2)
    assert [(entry["entity"], entry["limit"], entry["log_level"]) for entry in _limit_warnings(logs)] == [
        (f"runaway_{kind}", _LIMIT, "warning"),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("kind", ["agent", "team"])
async def test_model_that_answers_after_the_refusal_keeps_its_closing_reply(
    kind: Literal["agent", "team"],
    *,
    stream: bool,
) -> None:
    """The grace response after the first refused batch is delivered as the run's closing reply."""
    model = _ToolLoopModel(closing_reply_after=_LIMIT + 1)
    executed: list[str] = []
    entity = _budgeted_entity(kind, model, executed)

    with capture_logs() as logs:
        output, completed = await _run(entity, stream=stream)

    assert model.requests == _LIMIT + 2
    assert executed == ["probe"] * _LIMIT
    assert output.status is RunStatus.completed
    assert completed
    assert isinstance(output.content, str)
    assert output.content.endswith("Closing reply.")
    assert len(_limit_warnings(logs)) == 1


@pytest.mark.asyncio
async def test_tool_calls_within_the_budget_are_untouched() -> None:
    """A run that stays within its budget neither warns nor ends early."""
    model = _ToolLoopModel(closing_reply_after=_LIMIT)
    executed: list[str] = []
    entity = _budgeted_entity("agent", model, executed)

    with capture_logs() as logs:
        output, _completed = await _run(entity, stream=False)

    assert model.requests == _LIMIT + 1
    assert executed == ["probe"] * _LIMIT
    assert output.content == "Working. " * _LIMIT + "Closing reply."
    assert _limit_warnings(logs) == []

"""Pre-request preparation hook on Agno's response loops."""
# ruff: noqa: D103

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
from agno.agent import Agent
from agno.metrics import MessageMetrics
from agno.models.message import Message
from agno.models.response import ModelResponse
from agno.run.agent import RunOutput

from mindroom.agno_compat_model_hooks import install_request_preparation
from mindroom.synthetic_model import SyntheticModel
from mindroom.tool_call_budget import install_model_call_cap

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from agno.run.team import TeamRunOutput


class _TwoBatchModel(SyntheticModel):
    """Request one tool call twice, then answer; record what each provider request received."""

    def __init__(self) -> None:
        super().__init__(id="test", name="test", provider="test")
        self.received: list[list[str]] = []

    async def ainvoke(self, messages: list[Message], **_kwargs: object) -> ModelResponse:
        self.received.append([str(message.content) for message in messages])
        if len(self.received) <= 2:
            return ModelResponse(
                tool_calls=[
                    {
                        "id": f"call_{len(self.received)}",
                        "type": "function",
                        "function": {"name": "probe", "arguments": "{}"},
                    },
                ],
                response_usage=MessageMetrics(input_tokens=10, output_tokens=1, total_tokens=11),
            )
        return ModelResponse(content="done")

    async def ainvoke_stream(self, messages: list[Message], **kwargs: object) -> AsyncIterator[ModelResponse]:
        yield await self.ainvoke(messages, **kwargs)


def probe() -> str:
    return "probed"


async def _run(agent: Agent, *, stream: bool) -> RunOutput:
    if not stream:
        output = await agent.arun("Do the task")
        assert isinstance(output, RunOutput)
        return output
    final: RunOutput | None = None
    async for event in agent.arun("Do the task", stream=True, yield_run_output=True):
        if isinstance(event, RunOutput):
            final = event
    assert final is not None
    return final


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
async def test_request_preparation_runs_before_every_provider_request(*, stream: bool) -> None:
    model = _TwoBatchModel()
    calls: list[dict[str, Any]] = []

    async def prepare(
        messages: list[Message],
        tools: list[dict[str, Any]] | None,
        run_response: RunOutput | TeamRunOutput | None,
    ) -> None:
        calls.append({"messages": messages, "tools": tools, "run_response": run_response})
        messages.append(Message(role="user", content=f"prepared {len(calls)}"))

    install_request_preparation(model, marker="_test_preparation", prepare=prepare)
    agent = Agent(name="Prober", model=model, tools=[probe], telemetry=False)

    output = await _run(agent, stream=stream)

    assert output.content == "done"
    assert len(calls) == 3
    assert all(call["messages"] is calls[0]["messages"] for call in calls)
    assert all(call["run_response"] is not None for call in calls)
    assert {call["run_response"].run_id for call in calls} == {output.run_id}
    assert [tool["function"]["name"] for tool in calls[0]["tools"] or []] == ["probe"]
    assert [received[-1] for received in model.received] == ["prepared 1", "prepared 2", "prepared 3"]


@pytest.mark.asyncio
async def test_installing_request_preparation_twice_runs_it_once_per_request() -> None:
    model = _TwoBatchModel()
    calls: list[int] = []

    async def prepare(*_args: object) -> None:
        calls.append(1)

    install_request_preparation(model, marker="_test_preparation", prepare=prepare)
    install_request_preparation(model, marker="_test_preparation", prepare=prepare)
    await Agent(name="Prober", model=model, tools=[probe], telemetry=False).arun("Do the task")

    assert len(calls) == 3


@pytest.mark.asyncio
async def test_tool_call_cap_installed_later_refuses_before_preparation() -> None:
    model = _TwoBatchModel()
    calls: list[int] = []

    async def prepare(*_args: object) -> None:
        calls.append(1)

    install_request_preparation(model, marker="_test_preparation", prepare=prepare)
    install_model_call_cap(model, entity_name="prober")
    agent = Agent(name="Prober", model=model, tools=[probe], tool_call_limit=0, telemetry=False)

    await agent.arun("Do the task")

    assert len(model.received) == len(calls)

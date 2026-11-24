"""Tools, specs, and collectors the agent-loop tests share."""

from __future__ import annotations

import json
from dataclasses import replace
from typing import TYPE_CHECKING

from agno.tools import Toolkit

from mindroom.agent_loop.model_loop import EMPTY_REQUEST, NO_HOOKS, run_model_loop
from mindroom.agent_loop.spec import AgentSpec, RunContext, UnstoredSession
from mindroom.agent_loop.tools import PreparedTools
from mindroom.llm.events import RunFinished
from mindroom.llm.messages import Message
from mindroom.llm.runs import RunRecord
from mindroom.llm.usage import RunMetrics
from mindroom.tool_system.agno_toolkits import adapt_toolkit
from mindroom.tool_system.tool_function import ToolGate

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable, Sequence

    from mindroom.agent_loop.model_loop import LoopEvent, LoopHooks
    from mindroom.llm.events import RunEvent
    from mindroom.llm.provider import ModelProvider, ModelRequest
    from mindroom.llm.runs import AnyRunRecord, RunRequirement
    from mindroom.tool_system.tool_function import AdaptedToolkit


def add(a: int, b: int) -> str:
    """Add two integers.

    Args:
        a: First number.
        b: Second number.

    """
    return json.dumps({"sum": a + b})


def echo(text: str) -> str:
    """Return the text.

    Args:
        text: What to echo.

    """
    return text


def boom(reason: str) -> str:
    """Fail with the given reason.

    Args:
        reason: The failure text.

    """
    raise RuntimeError(reason)


def toolkit(*tools: Callable[..., object], name: str = "probes", **flags: object) -> AdaptedToolkit:
    """Adapt plain callables as one toolkit; ``flags`` set the loop-read flags on every function."""
    adapted = adapt_toolkit(Toolkit(name=name, tools=list(tools)))
    if not flags:
        return adapted
    return replace(adapted, functions=tuple(replace(function, **flags) for function in adapted.functions))


def gated(*tools: Callable[..., object], name: str = "gated") -> AdaptedToolkit:
    """Adapt callables whose calls pause for confirmation under MindRoom's approval policy."""
    return toolkit(*tools, name=name, gate=ToolGate.POLICY)


def external(*tools: Callable[..., object], name: str = "delegate") -> AdaptedToolkit:
    """Adapt callables whose calls pause for silent external execution, as ``run_subagent`` does."""
    return toolkit(*tools, name=name, gate=ToolGate.DELEGATION)


def spec(
    model: ModelProvider,
    *toolkits: AdaptedToolkit,
    max_tool_calls_per_turn: int | None = None,
) -> AgentSpec:
    """Return a spec for the ``code`` agent with the usual system message."""
    return AgentSpec(
        id="code",
        name="Code",
        model=model,
        system_messages=(Message(role="system", content="You are Code."),),
        toolkits=toolkits,
        max_tool_calls_per_turn=max_tool_calls_per_turn,
    )


def context(run_id: str = "run-1", session_id: str = "session-1") -> RunContext:
    """Return a run context for Alice's run."""
    return RunContext(
        run_id=run_id,
        session=UnstoredSession(session_id),
        user_id="@alice:example.org",
        metadata={"room_id": "!r"},
    )


def run_record() -> RunRecord:
    """Return the default record for a model-loop test."""
    return RunRecord(run_id="run-1", agent_id="code", metrics=RunMetrics())


async def run_loop(
    agent: AgentSpec,
    messages: list[Message],
    *,
    hooks: LoopHooks = NO_HOOKS,
    record: AnyRunRecord | None = None,
    request_template: ModelRequest = EMPTY_REQUEST,
    resumed: Sequence[RunRequirement] | None = None,
) -> tuple[list[LoopEvent], AnyRunRecord]:
    """Run a model loop to completion with the supplied hooks and record."""
    record = record or run_record()
    events = [
        event
        async for event in run_model_loop(
            agent.model,
            messages,
            PreparedTools(functions=agent.functions),
            record=record,
            run=context(),
            hooks=hooks,
            max_tool_calls_per_turn=agent.max_tool_calls_per_turn,
            request_template=request_template,
            resumed=resumed,
        )
    ]
    return events, record


async def collect(events: AsyncIterator[RunEvent]) -> tuple[list[RunEvent], RunRecord]:
    """Drain a run and return its events and the finished record."""
    collected = [event async for event in events]
    finished = collected[-1]
    assert isinstance(finished, RunFinished)
    record = finished.record
    assert isinstance(record, RunRecord)
    return collected, record

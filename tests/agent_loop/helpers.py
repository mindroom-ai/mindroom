"""Tools, specs, and collectors the agent-loop tests share."""

from __future__ import annotations

import json
from dataclasses import replace
from typing import TYPE_CHECKING

from agno.tools import Toolkit

from mindroom.agent_loop.spec import AgentSpec, RunContext
from mindroom.llm.events import RunFinished
from mindroom.llm.messages import Message
from mindroom.llm.runs import RunRecord
from mindroom.tool_system.agno_toolkits import AdaptedToolkit, adapt_toolkit

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable

    from mindroom.llm.events import RunEvent
    from mindroom.tool_system.tool_function import ToolFunction
    from tests.fakes.scripted_model import ScriptedModel


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
    return toolkit(*tools, name=name, requires_confirmation=True, approval_type="mindroom_policy")


def external(*tools: Callable[..., object], name: str = "delegate") -> AdaptedToolkit:
    """Adapt callables whose calls pause for silent external execution, as ``run_subagent`` does."""
    return toolkit(*tools, name=name, external_execution=True, external_execution_silent=True)


def spec(
    model: ScriptedModel,
    *toolkits: AdaptedToolkit,
    tool_call_limit: int | None = None,
    tool_filter: Callable[[ToolFunction], bool] | None = None,
) -> AgentSpec:
    """Return a spec for the ``code`` agent with the usual system message."""
    return AgentSpec(
        id="code",
        name="Code",
        model=model,
        system_message=Message(role="system", content="You are Code."),
        toolkits=toolkits,
        tool_call_limit=tool_call_limit,
        tool_filter=tool_filter,
    )


def context(run_id: str = "run-1", session_id: str = "session-1") -> RunContext:
    """Return a run context for Alice's run."""
    return RunContext(run_id=run_id, session_id=session_id, user_id="@alice:example.org", metadata={"room_id": "!r"})


async def collect(events: AsyncIterator[RunEvent]) -> tuple[list[RunEvent], RunRecord]:
    """Drain a run and return its events and the finished record."""
    collected = [event async for event in events]
    finished = collected[-1]
    assert isinstance(finished, RunFinished)
    record = finished.record
    assert isinstance(record, RunRecord)
    return collected, record

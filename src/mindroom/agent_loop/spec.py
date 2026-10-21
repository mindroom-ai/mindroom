"""What an agent run is built from: the agent's identity, model, prompt, and tools, and the run's own identity."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable

    from mindroom.llm.messages import Message
    from mindroom.llm.provider import ModelProvider
    from mindroom.tool_system.agno_toolkits import AdaptedToolkit
    from mindroom.tool_system.tool_function import ToolFunction


@dataclass(frozen=True, slots=True, kw_only=True)
class AgentSpec:
    """One configured agent as the loop runs it.

    ``id`` and ``name`` are stored on every run as ``agent_id`` and ``agent_name``. ``system_message`` is the
    rendered prompt the loop sends first, or None for an agent without one. ``tool_filter`` keeps a tool when it
    returns True; ``tool_call_limit`` bounds the tool calls of one run, and with it the model requests.
    """

    id: str
    name: str
    model: ModelProvider
    system_message: Message | None
    toolkits: tuple[AdaptedToolkit, ...] = ()
    tool_filter: Callable[[ToolFunction], bool] | None = None
    tool_call_limit: int | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class RunContext:
    """The identity of one run and the state its tools share.

    ``metadata`` is stored on the run record; ``session_state`` is handed to every tool call, which may change it
    in place.
    """

    run_id: str
    session_id: str
    user_id: str | None = None
    metadata: dict[str, Any] | None = None
    session_state: dict[str, Any] = field(default_factory=dict)


__all__ = [
    "AgentSpec",
    "RunContext",
]

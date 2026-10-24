"""What an agent run is built from: the agent's identity, model, prompt, and tools, and the run's own identity."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from mindroom.tool_system.invocation import ToolCallContext

if TYPE_CHECKING:
    from mindroom.history.types import HistoryScope
    from mindroom.llm.messages import Message
    from mindroom.llm.provider import ModelProvider
    from mindroom.session_storage.types import ConversationSession
    from mindroom.tool_system.tool_function import AdaptedToolkit


@dataclass(frozen=True, slots=True, kw_only=True)
class AgentSpec:
    """One configured agent as the loop runs it.

    ``id`` and ``name`` are stored on every run as ``agent_id`` and ``agent_name``. ``system_message`` is the
    rendered prompt the loop sends first, or None for an agent without one. ``max_tool_calls_per_turn`` bounds the
    tool calls of one run, and with it the model requests.
    """

    id: str
    name: str
    model: ModelProvider
    system_message: Message | None
    toolkits: tuple[AdaptedToolkit, ...] = ()
    max_tool_calls_per_turn: int | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class RunContext:
    """The identity of one run and the state its tools share.

    ``parent_run_id`` names the run that started this one, as a team run starts its member runs. ``metadata`` is
    stored on the run record. ``session`` is the stored session the run belongs to, whose ``session_state`` every
    tool call of the run shares and may change in place, and which the store writes back; a run no store keeps has
    none. ``history_scope`` is the stored conversation a team run and its member runs belong to, which tools that
    address history by scope read; None for an agent's own run, whose scope is its agent's. ``model_name`` is the
    configured model the run resolved, which its tool calls carry; a team's member runs carry the leader's.
    """

    run_id: str
    session_id: str
    user_id: str | None = None
    parent_run_id: str | None = None
    metadata: dict[str, Any] | None = None
    session: ConversationSession | None = None
    history_scope: HistoryScope | None = None
    model_name: str | None = None
    # The state a run without a session shares with its tools, and with the member runs ``replace`` makes of it.
    # It is an ``__init__`` field so that ``replace`` carries it over: ``replace`` passes on only ``__init__`` fields,
    # so a member run's context would otherwise start with a new empty dict.
    _own_state: dict[str, Any] = field(default_factory=dict, repr=False, compare=False)

    @property
    def session_state(self) -> dict[str, Any]:
        """Return the state the run's tool calls share: its session's, or its own when no session stores the run."""
        return self.session.session_state if self.session is not None else self._own_state

    def call_context(self, call_id: str | None) -> ToolCallContext:
        """Return the context of the call ``call_id`` this run makes, sharing the run's state and scope."""
        return ToolCallContext(
            run_id=self.run_id,
            session_id=self.session_id,
            user_id=self.user_id,
            call_id=call_id,
            session_state=self.session_state,
            metadata=self.metadata or {},
            history_scope=self.history_scope,
            model_name=self.model_name,
        )


__all__ = [
    "AgentSpec",
    "RunContext",
]

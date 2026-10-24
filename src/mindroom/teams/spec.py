"""What a team run is built from: the leader's identity, model, prompt, and own tools, and its members."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

    from mindroom.agent_loop.model_loop import LoopHooks
    from mindroom.agent_loop.spec import AgentSpec
    from mindroom.llm.messages import Message
    from mindroom.llm.provider import ModelProvider, ModelRequest
    from mindroom.tool_system.tool_function import AdaptedToolkit


class TeamMode(StrEnum):
    """How the leader works its members; the values are the configured ``teams.<name>.mode`` strings.

    In coordinate mode the leader hands sub-tasks to chosen members one call at a time; in collaborate mode one
    call puts the same task to every member.
    """

    COORDINATE = "coordinate"
    COLLABORATE = "collaborate"


@dataclass(frozen=True, slots=True, kw_only=True)
class TeamMember:
    """One member as the leader sees and runs it, in the team one turn or approval resume builds.

    ``spec.id`` is the member id the leader delegates to and the stored ``agent_id``; ``spec.name`` the roster name
    and stored ``agent_name``. ``role`` is the roster text the leader reads. ``new_hooks`` builds the hooks of one
    run of this member; a run takes its hooks only through ``hooks(run_id)``, which keeps them across the run's
    continuations in this team. ``request_template`` applies to every run and resume, except that the team run's
    stream mode overrides its ``stream`` field, so a member streams exactly when its team run does.
    """

    spec: AgentSpec
    role: str
    new_hooks: Callable[[], LoopHooks]
    request_template: ModelRequest
    # The hooks of each run of this member, by run id. It is an ``__init__`` field so that ``replace`` carries it
    # over: ``replace`` passes on only ``__init__`` fields, so a copy would otherwise build new hooks for a run.
    _hooks_by_run: dict[str, LoopHooks] = field(default_factory=dict, repr=False, compare=False)

    def hooks(self, run_id: str) -> LoopHooks:
        """Return the hooks of the member's run ``run_id``, built when this team first runs or resumes that run.

        A hook such as the output-limit count or mid-turn compaction keeps state for the run it serves, so a run
        continued after its delegations keeps that state and a new run starts with its own. An approval resume
        builds its team again, so the run it resumes starts with new hooks, as an agent's approval resume does.
        """
        hooks = self._hooks_by_run.get(run_id)
        if hooks is None:
            hooks = self._hooks_by_run[run_id] = self.new_hooks()
        return hooks


@dataclass(frozen=True, slots=True, kw_only=True)
class TeamSpec:
    """One team as the leader loop runs it.

    ``id`` and ``name`` are stored on every team run as ``team_id`` and ``team_name``. ``system_message`` is the
    rendered leader prompt (``prompt.render_leader_system_message``); ``toolkits`` are the leader's own tools, to
    which each run adds the delegation tool for ``mode``; ``max_tool_calls_per_turn`` bounds the leader's calls,
    delegations included.
    """

    id: str
    name: str
    mode: TeamMode
    model: ModelProvider
    system_message: Message
    toolkits: tuple[AdaptedToolkit, ...]
    max_tool_calls_per_turn: int | None
    members: tuple[TeamMember, ...]


__all__ = [
    "TeamMember",
    "TeamMode",
    "TeamSpec",
]

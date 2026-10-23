"""What a team run is built from: the leader's identity, model, prompt, and own tools, and its members."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING

from mindroom.agent_loop.model_loop import EMPTY_REQUEST, NO_HOOKS, LoopHooks

if TYPE_CHECKING:
    from collections.abc import Callable

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
    """One member as the leader sees and runs it in one turn or approval resume, which each build their team.

    ``spec.id`` is the member id the leader delegates to and the stored ``agent_id``; ``spec.name`` the roster name
    and stored ``agent_name``. ``role`` is the roster text the leader reads. ``new_hooks`` builds the hooks of one
    run of this member, and ``hooks`` holds one set per run; ``request_template`` applies to every run and resume,
    except that the team run's stream mode overrides its ``stream`` field, so a member streams exactly when its team
    run does.
    """

    spec: AgentSpec
    role: str
    new_hooks: Callable[[], LoopHooks] = lambda: NO_HOOKS
    request_template: ModelRequest = EMPTY_REQUEST
    _hooks_by_run: dict[str, LoopHooks] = field(default_factory=dict, init=False, repr=False, compare=False)

    def hooks(self, run_id: str) -> LoopHooks:
        """Return the hooks of the member's run ``run_id``: the set its first run here built, else a new one.

        A hook such as the output-limit count or mid-turn compaction keeps state for the run it serves, so a run
        continued after its delegations keeps that state, and every new run starts afresh, as an approval resume,
        which builds the team again, does.
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
    which each run adds the delegation tool for ``mode``; ``tool_call_limit`` bounds the leader's calls,
    delegations included.
    """

    id: str
    name: str
    mode: TeamMode
    model: ModelProvider
    system_message: Message
    toolkits: tuple[AdaptedToolkit, ...] = ()
    tool_call_limit: int | None = None
    members: tuple[TeamMember, ...]


__all__ = [
    "TeamMember",
    "TeamMode",
    "TeamSpec",
]

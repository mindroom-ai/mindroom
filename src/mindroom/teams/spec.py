"""What a team run is built from: the leader's identity, model, prompt, and own tools, and its members."""

from __future__ import annotations

from dataclasses import dataclass
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
    """One member as the leader sees and runs it.

    ``spec.id`` is the member id the leader delegates to and the stored ``agent_id``; ``spec.name`` the roster name
    and stored ``agent_name``. ``role`` is the roster text the leader reads. ``hooks(resumed)`` builds the hooks of one
    run of this member, or of one resume when ``resumed``, since a hook such as mid-turn compaction keeps state for
    the run it serves and only a resume carries the approval receipt; ``request_template`` applies to every run,
    which streams exactly when its team run does.
    """

    spec: AgentSpec
    role: str
    hooks: Callable[[bool], LoopHooks] = lambda _resumed: NO_HOOKS
    request_template: ModelRequest = EMPTY_REQUEST


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

"""What a team run is built from: the leader, as the agent the loop runs, and the members it works."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from mindroom.agent_loop.model_loop import LoopHooks
    from mindroom.agent_loop.spec import AgentSpec
    from mindroom.llm.provider import ModelRequest


class TeamMode(StrEnum):
    """How the leader works its members; the values are the configured ``teams.<name>.mode`` strings.

    In coordinate mode the leader hands sub-tasks to chosen members one call at a time; in collaborate mode one
    call puts the same task to every member.
    """

    COORDINATE = "coordinate"
    COLLABORATE = "collaborate"


@dataclass(frozen=True, slots=True, kw_only=True)
class MemberBuild:
    """One member as its runs in one team run it: the agent with this turn's system messages, and its run settings.

    ``new_hooks`` builds the hooks of one run of this member; a run takes its hooks only through ``hooks(run_id)``,
    which keeps them across the run's continuations in this team. ``request_template`` applies to every run and resume.
    ``toolkit_owners`` names the authored toolkit of each function the member offers, keyed ``(member id, function)``,
    which approvals check (``tool_owners_by_agent``).
    """

    spec: AgentSpec
    new_hooks: Callable[[], LoopHooks]
    request_template: ModelRequest
    toolkit_owners: Mapping[tuple[str, str], str]
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


@dataclass(slots=True, kw_only=True, eq=False)
class TeamMember:
    """One member of the team one turn or approval resume builds: what the leader reads of it, and its build.

    ``id`` is the member id the leader delegates to and the stored ``agent_id``, ``name`` the roster name and stored
    ``agent_name``, and ``role`` the member's authored role, which the leader's roster shows; all three come from
    config, so the leader's prompt needs no member built. ``build`` builds the member for this team, blocking; the
    member's first run builds it (``ensure_built``), and every later run and resume of the member in this team runs
    ``built``. A member the leader never calls is never built.
    """

    id: str
    name: str
    role: str
    build: Callable[[], MemberBuild] = field(repr=False)
    built: MemberBuild | None = field(default=None, init=False, repr=False)
    _building: asyncio.Lock = field(default_factory=asyncio.Lock, init=False, repr=False)

    async def ensure_built(self) -> MemberBuild:
        """Return ``built``, building the member off the event loop first if no run has yet.

        Runs that start together wait for one build. A build that raises leaves the member unbuilt, so the next run
        tries again.
        """
        async with self._building:
            if self.built is None:
                self.built = await asyncio.to_thread(self.build)
        return self.built


@dataclass(frozen=True, slots=True, kw_only=True)
class TeamSpec:
    """One team as the leader loop runs it: the leader, which works its ``members`` as ``mode`` says.

    ``leader.id`` is the team's id, stored on every team run as ``team_id``; ``leader.name`` is the team's label, which
    no run stores. ``leader.system_messages`` are the rendered leader prompt (``prompt.render_leader_system_messages``);
    ``leader.toolkits`` are the leader's own tools, to which each run adds the delegation tool for ``mode``;
    ``leader.max_tool_calls_per_turn`` bounds the leader's calls, delegations included.
    """

    leader: AgentSpec
    mode: TeamMode
    members: tuple[TeamMember, ...]

    def tool_owners(self) -> dict[tuple[str, str], str]:
        """Return the authored toolkit of each function the built members offer, keyed ``(member id, function)``.

        Only a member that ran is built, and only a run can wait on a call, so no approval names a call of a member
        not built yet.
        """
        return {
            key: toolkit
            for member in self.members
            if member.built is not None
            for key, toolkit in member.built.toolkit_owners.items()
        }


__all__ = [
    "MemberBuild",
    "TeamMember",
    "TeamMode",
    "TeamSpec",
]

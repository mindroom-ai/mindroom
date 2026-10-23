"""Forming a team from one Matrix turn: who was asked, who is eligible, and in which mode the team works.

Team formation is pure policy over mentions, thread history, room membership, and configured access; only the
execution-time mode choice for an ad hoc team asks a model.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Literal, cast

from pydantic import BaseModel, Field

from mindroom.agent_loop.helper import complete_prompt
from mindroom.authorization import get_available_responders_in_room
from mindroom.constants import ROUTER_AGENT_NAME
from mindroom.entity_resolution import entity_identity_registry
from mindroom.helper_usage import record_system_usage
from mindroom.logging_config import get_logger
from mindroom.providers.registry import build_model_provider
from mindroom.teams.spec import TeamMode

if TYPE_CHECKING:
    import nio

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.matrix.identity import MatrixID
    from mindroom.runtime_protocols import OrchestratorRuntime, SupportsRunningState

logger = get_logger(__name__)


class _TeamModeDecision(BaseModel):
    """AI decision for team collaboration mode."""

    mode: Literal["coordinate", "collaborate"] = Field(
        description="coordinate for delegation and synthesis, collaborate for all working on same task",
    )
    reasoning: str = Field(description="Brief explanation of why this mode was chosen")


class TeamIntent(str, Enum):
    """How one team request was formed."""

    EXPLICIT_MEMBERS = "explicit_members"
    CONFIGURED_TEAM = "configured_team"
    IMPLICIT_THREAD_TEAM = "implicit_thread_team"
    DM_AUTO_TEAM = "dm_auto_team"


class TeamMemberStatus(str, Enum):
    """Eligibility of one requested team member."""

    ELIGIBLE = "eligible"
    NOT_IN_ROOM = "not_in_room"
    HIDDEN_FROM_SENDER = "hidden_from_sender"
    UNSUPPORTED_FOR_TEAM = "unsupported_for_team"
    NOT_MATERIALIZABLE = "not_materializable"


class TeamOutcome(str, Enum):
    """Final resolution outcome for one team request."""

    TEAM = "team"
    INDIVIDUAL = "individual"
    NONE = "none"
    REJECT = "reject"


@dataclass(frozen=True)
class TeamResolutionMember:
    """Status of one requested team member."""

    agent: MatrixID
    name: str
    status: TeamMemberStatus
    private_targets: tuple[str, ...] | None = None


@dataclass(frozen=True)
class TeamResolution:
    """First-class team resolution result consumed across bot and API flows."""

    intent: TeamIntent | None
    requested_members: list[MatrixID]
    member_statuses: list[TeamResolutionMember]
    eligible_members: list[MatrixID]
    outcome: TeamOutcome
    reason: str | None = None
    mode: TeamMode | None = None

    @classmethod
    def none(cls) -> TeamResolution:
        """Return the no-team outcome."""
        return cls(
            intent=None,
            requested_members=[],
            member_statuses=[],
            eligible_members=[],
            outcome=TeamOutcome.NONE,
        )

    @classmethod
    def team(
        cls,
        *,
        intent: TeamIntent,
        requested_members: list[MatrixID],
        member_statuses: list[TeamResolutionMember],
        eligible_members: list[MatrixID],
        mode: TeamMode,
    ) -> TeamResolution:
        """Return the successful team outcome."""
        return cls(
            intent=intent,
            requested_members=requested_members,
            member_statuses=member_statuses,
            eligible_members=eligible_members,
            outcome=TeamOutcome.TEAM,
            mode=mode,
        )

    @classmethod
    def individual(
        cls,
        *,
        intent: TeamIntent,
        requested_members: list[MatrixID],
        member_statuses: list[TeamResolutionMember],
        agent: MatrixID,
    ) -> TeamResolution:
        """Return the single-agent degraded outcome."""
        return cls(
            intent=intent,
            requested_members=requested_members,
            member_statuses=member_statuses,
            eligible_members=[agent],
            outcome=TeamOutcome.INDIVIDUAL,
        )


@dataclass(frozen=True)
class _SelectedTeamRequest:
    """Normalized team request selected from one message context."""

    intent: TeamIntent
    requested_members: list[MatrixID]


async def _select_team_mode(
    message: str,
    agent_names: list[str],
    config: Config,
    runtime_paths: RuntimePaths,
) -> TeamMode:
    """Ask the default model whether ``agent_names`` should coordinate or collaborate on ``message``.

    A failed call or an answer outside the schema collaborates.
    """
    prompt = config.render_prompt(
        "TEAM_MODE_SELECTION_PROMPT_TEMPLATE",
        message=message,
        agent_names=", ".join(agent_names),
    )

    try:
        result = await complete_prompt(
            build_model_provider(config, runtime_paths, "default"),
            prompt,
            role="Determine team mode",
            schema=_TeamModeDecision,
        )
        await record_system_usage(result.record, runtime_paths=runtime_paths, kind="team_mode")
    except Exception as e:
        logger.warning("team_mode_decision_failed", error=str(e))
        return TeamMode.COLLABORATE
    decision = result.content
    if not isinstance(decision, _TeamModeDecision):
        logger.debug("team_mode_decision_unexpected_type", response_type=type(decision).__name__)
        return TeamMode.COLLABORATE
    logger.info("team_mode_decided", mode=decision.mode, reasoning=decision.reasoning)
    return TeamMode(decision.mode)


def resolve_live_shared_agent_names(
    orchestrator: OrchestratorRuntime,
    *,
    config: Config | None = None,
) -> set[str] | None:
    """Return running shared agent names when runtime availability is known."""
    active_config = config or orchestrator.config
    assert active_config is not None
    orchestrator_agent_bots = orchestrator.agent_bots
    if not isinstance(orchestrator_agent_bots, dict):
        return None
    running_agent_bots = cast("dict[str, SupportsRunningState]", orchestrator_agent_bots)
    return {
        name
        for name, bot in running_agent_bots.items()
        if name != ROUTER_AGENT_NAME and name in active_config.agents and bot.running
    }


def resolve_team_materializable_agent_names(
    config: Config,
    materializable_agent_names: set[str] | None,
    *,
    allow_direct_private_agents: bool,
) -> set[str] | None:
    """Add on-demand private agents to one known live shared-agent set when allowed."""
    if materializable_agent_names is None or not allow_direct_private_agents:
        return materializable_agent_names
    private_agent_names = {
        agent_name for agent_name, agent_config in config.agents.items() if agent_config.private is not None
    }
    return materializable_agent_names | private_agent_names


def decide_team_formation(
    tagged_agents: list[MatrixID],
    agents_in_thread: list[MatrixID],
    all_mentioned_in_thread: list[MatrixID],
    room: nio.MatrixRoom | None,
    runtime_paths: RuntimePaths,
    config: Config,
    is_dm_room: bool = False,
    is_thread: bool = False,
    available_responders_in_room: list[MatrixID] | None = None,
    materializable_agent_names: set[str] | None = None,
    allow_explicit_private_agents: bool = False,
) -> TeamResolution:
    """Determine if a team should form, purely from mention, thread, and room context.

    ``available_responders_in_room`` are the room's responders the sender may see and ``materializable_agent_names``
    the live agents that can answer now, each when known; ``allow_explicit_private_agents`` lets explicitly tagged
    requester-private agents join. A team carries a heuristic provisional mode: COORDINATE when agents are explicitly
    tagged (they likely have different roles), COLLABORATE when agents come from thread history (likely discussing the
    same topic). The execution layer may refine it with ``select_ad_hoc_team_mode``.
    """
    team_request = _select_team_request(
        tagged_agents,
        all_mentioned_in_thread,
        agents_in_thread,
        room,
        config,
        runtime_paths,
        is_dm_room=is_dm_room,
        is_thread=is_thread,
        available_responders_in_room=available_responders_in_room,
    )
    if team_request is None:
        return TeamResolution.none()

    allow_direct_private_agents = allow_explicit_private_agents and team_request.intent is TeamIntent.EXPLICIT_MEMBERS
    materializable_agent_names = resolve_team_materializable_agent_names(
        config,
        materializable_agent_names,
        allow_direct_private_agents=allow_direct_private_agents,
    )
    member_statuses = _evaluate_team_members(
        team_request.requested_members,
        config,
        runtime_paths,
        room=room,
        sender_visible_responders=available_responders_in_room,
        materializable_agent_names=materializable_agent_names,
        allow_direct_private_agents=allow_direct_private_agents,
    )
    return _resolve_team_request(
        intent=team_request.intent,
        requested_members=team_request.requested_members,
        member_statuses=member_statuses,
        config=config,
        reason_prefix="Team request",
        mode=TeamMode.COORDINATE if len(tagged_agents) > 1 else TeamMode.COLLABORATE,
    )


async def select_ad_hoc_team_mode(
    message: str,
    team_agents: list[MatrixID],
    config: Config,
    runtime_paths: RuntimePaths,
) -> TeamMode:
    """Select the collaboration mode for one ad-hoc team at execution time.

    Team formation itself is pure; this AI refinement must run only where the
    team response actually executes.
    """
    agent_names = [team_member_name(agent_id, config, runtime_paths) for agent_id in team_agents]
    return await _select_team_mode(message, agent_names, config, runtime_paths)


def team_member_name(agent_id: MatrixID, config: Config, runtime_paths: RuntimePaths) -> str:
    """Return the config name of the agent behind one Matrix id, used throughout team resolution."""
    return (
        entity_identity_registry(config, runtime_paths).current_entity_name_for_user_id(
            agent_id.full_id,
            include_router=True,
        )
        or agent_id.username
    )


def _filter_team_request_members(
    agent_ids: list[MatrixID],
    config: Config,
    runtime_paths: RuntimePaths,
) -> list[MatrixID]:
    """Keep only actual teamable agents while preserving the requested order."""
    filtered: list[MatrixID] = []
    for agent_id in agent_ids:
        agent_name = entity_identity_registry(config, runtime_paths).current_entity_name_for_user_id(
            agent_id.full_id,
            include_router=False,
        )
        if agent_name is not None and agent_name in config.agents:
            filtered.append(agent_id)
    return filtered


def _normalize_team_request_members(
    agent_ids: list[MatrixID],
    config: Config,
    runtime_paths: RuntimePaths,
) -> list[MatrixID]:
    """Deduplicate ad hoc candidates after filtering out non-teamable agents."""
    normalized: list[MatrixID] = []
    seen_ids: set[str] = set()
    for agent_id in _filter_team_request_members(agent_ids, config, runtime_paths):
        if agent_id.full_id in seen_ids:
            continue
        normalized.append(agent_id)
        seen_ids.add(agent_id.full_id)
    return normalized


def _select_team_request(
    tagged_agents: list[MatrixID],
    all_mentioned_in_thread: list[MatrixID],
    agents_in_thread: list[MatrixID],
    room: nio.MatrixRoom | None,
    config: Config,
    runtime_paths: RuntimePaths,
    *,
    is_dm_room: bool,
    is_thread: bool,
    available_responders_in_room: list[MatrixID] | None,
) -> _SelectedTeamRequest | None:
    """Return the normalized team request implied by one message context, or None when it implies no team.

    One tagged agent answers alone; with none tagged, the thread's agents or a DM room's responders may form a team.
    """
    tagged_members = _normalize_team_request_members(tagged_agents, config, runtime_paths)
    if len(tagged_members) > 1:
        return _requested_team("tagged_agents", TeamIntent.EXPLICIT_MEMBERS, tagged_members)
    if tagged_members:
        return None
    for trigger, thread_agents in (
        ("previously_mentioned_agents", all_mentioned_in_thread),
        ("thread_agents", agents_in_thread),
    ):
        thread_members = _normalize_team_request_members(thread_agents, config, runtime_paths)
        if len(thread_members) > 1:
            return _requested_team(trigger, TeamIntent.IMPLICIT_THREAD_TEAM, thread_members)
    if not is_dm_room or is_thread or room is None:
        return None
    if available_responders_in_room is None:
        available_responders_in_room = get_available_responders_in_room(room, config, runtime_paths)
    responders = _normalize_team_request_members(available_responders_in_room, config, runtime_paths)
    if len(responders) <= 1:
        return None
    return _requested_team("dm_room_multiple_agents", TeamIntent.DM_AUTO_TEAM, responders)


def _requested_team(trigger: str, intent: TeamIntent, members: list[MatrixID]) -> _SelectedTeamRequest:
    logger.info("team_formation_requested", trigger=trigger, agents=[agent.full_id for agent in members])
    return _SelectedTeamRequest(intent, members)


# How a rejection reads for one agent and for several, by the status that rejected them.
_REJECTION_PHRASES: dict[TeamMemberStatus, tuple[str, str]] = {
    TeamMemberStatus.NOT_IN_ROOM: ("is not available in this room", "are not available in this room"),
    TeamMemberStatus.HIDDEN_FROM_SENDER: (
        "is not available to you in this room",
        "are not available to you in this room",
    ),
    TeamMemberStatus.NOT_MATERIALIZABLE: (
        "could not be materialized for this request",
        "could not be materialized for this request",
    ),
}


def _rejected_agents_message(status: TeamMemberStatus, agent_names: list[str], *, prefix: str) -> str:
    """Return the rejection message for requested agents that ``status`` rejected."""
    one, several = _REJECTION_PHRASES[status]
    if len(agent_names) == 1:
        return f"{prefix} includes agent '{agent_names[0]}' that {one}."
    quoted = ", ".join(f"'{agent_name}'" for agent_name in agent_names)
    return f"{prefix} includes agents {quoted} that {several}."


def not_materializable_team_agents_message(agent_names: list[str], *, prefix: str) -> str:
    """Return the rejection message for members that could not be materialized."""
    return _rejected_agents_message(TeamMemberStatus.NOT_MATERIALIZABLE, agent_names, prefix=prefix)


def _evaluate_team_members(
    requested_members: list[MatrixID],
    config: Config,
    runtime_paths: RuntimePaths,
    *,
    room: nio.MatrixRoom | None,
    sender_visible_responders: list[MatrixID] | None,
    materializable_agent_names: set[str] | None,
    allow_direct_private_agents: bool,
) -> list[TeamResolutionMember]:
    """Evaluate one status and response capability for each requested member, a teamable agent.

    The sender's view of the room decides visibility when given, else the room's membership when known.
    """
    sender_visible_ids = (
        {agent_id.full_id for agent_id in sender_visible_responders} if sender_visible_responders is not None else None
    )
    room_visible_ids = (
        {agent_id.full_id for agent_id in get_available_responders_in_room(room, config, runtime_paths)}
        if sender_visible_ids is None and room is not None
        else None
    )
    agent_names = [team_member_name(agent_id, config, runtime_paths) for agent_id in requested_members]
    unsupported_agents = config.get_unsupported_team_agents(
        agent_names,
        allow_direct_private_agents=allow_direct_private_agents,
    )

    member_statuses: list[TeamResolutionMember] = []
    for agent_id, agent_name in zip(requested_members, agent_names, strict=True):
        private_targets = unsupported_agents.get(agent_name)
        is_room_visible = room_visible_ids is None or agent_id.full_id in room_visible_ids
        is_sender_visible = sender_visible_ids is None or agent_id.full_id in sender_visible_ids
        is_materializable = materializable_agent_names is None or agent_name in materializable_agent_names
        if not is_room_visible:
            status = TeamMemberStatus.NOT_IN_ROOM
        elif not is_sender_visible:
            status = TeamMemberStatus.HIDDEN_FROM_SENDER
        elif agent_name in unsupported_agents:
            status = TeamMemberStatus.UNSUPPORTED_FOR_TEAM
        elif not is_materializable:
            status = TeamMemberStatus.NOT_MATERIALIZABLE
        else:
            status = TeamMemberStatus.ELIGIBLE
        member_statuses.append(
            TeamResolutionMember(
                agent=agent_id,
                name=agent_name,
                status=status,
                private_targets=private_targets,
            ),
        )
    return member_statuses


def _resolve_team_request(
    *,
    intent: TeamIntent,
    requested_members: list[MatrixID],
    member_statuses: list[TeamResolutionMember],
    config: Config,
    reason_prefix: str,
    mode: TeamMode,
) -> TeamResolution:
    """Apply one clear outcome policy after intent and member evaluation.

    An explicit request names at least two members, so with none rejected it always forms a team.
    """
    eligible_members = [member.agent for member in member_statuses if member.status is TeamMemberStatus.ELIGIBLE]
    rejected_members = [member for member in member_statuses if member.status is not TeamMemberStatus.ELIGIBLE]

    if intent in {TeamIntent.EXPLICIT_MEMBERS, TeamIntent.CONFIGURED_TEAM}:
        if rejected_members:
            return TeamResolution(
                intent=intent,
                requested_members=requested_members,
                member_statuses=member_statuses,
                eligible_members=eligible_members,
                outcome=TeamOutcome.REJECT,
                reason=_team_resolution_reason(rejected_members, config, reason_prefix=reason_prefix),
            )
    elif len(eligible_members) == 1:
        return TeamResolution.individual(
            intent=intent,
            requested_members=requested_members,
            member_statuses=member_statuses,
            agent=eligible_members[0],
        )
    elif not eligible_members:
        return TeamResolution(
            intent=intent,
            requested_members=requested_members,
            member_statuses=member_statuses,
            eligible_members=[],
            outcome=TeamOutcome.NONE,
        )
    return TeamResolution.team(
        intent=intent,
        requested_members=requested_members,
        member_statuses=member_statuses,
        eligible_members=eligible_members,
        mode=mode,
    )


def _team_resolution_reason(
    rejected_members: list[TeamResolutionMember],
    config: Config,
    *,
    reason_prefix: str,
) -> str:
    """Return one shared user-facing explanation for a rejected team request."""
    rejection_statuses = {member.status for member in rejected_members}
    if len(rejection_statuses) > 1:
        details = "; ".join(_team_resolution_member_detail(member) for member in rejected_members)
        return f"{reason_prefix} cannot be satisfied: {details}"

    [status] = rejection_statuses
    if status is TeamMemberStatus.UNSUPPORTED_FOR_TEAM:
        first_unsupported_member = rejected_members[0]
        return config.unsupported_team_agent_message(
            first_unsupported_member.name,
            prefix=reason_prefix,
            private_targets=first_unsupported_member.private_targets,
        )
    return _rejected_agents_message(status, [member.name for member in rejected_members], prefix=reason_prefix)


def _unsupported_team_member_detail(member: TeamResolutionMember) -> str:
    """Return the unsupported-team explanation for one member, a configured agent that reaches a private agent."""
    private_targets = member.private_targets
    # Requested members are configured agents, never unknown ones, so an unsupported member names its private targets.
    assert private_targets is not None
    if member.name in private_targets:
        return (
            f"agent '{member.name}' is private and can only join explicit Matrix ad hoc teams with requester identity"
        )
    if len(private_targets) != 1:
        return (
            f"agent '{member.name}' reaches private agents "
            f"{', '.join(repr(target) for target in private_targets)} via delegation "
            "and private delegation is not supported for teams"
        )
    return (
        f"agent '{member.name}' reaches private agent '{private_targets[0]}' via delegation "
        "and private delegation is not supported for teams"
    )


def _team_resolution_member_detail(member: TeamResolutionMember) -> str:
    """Return a member-specific reason fragment for mixed team-request rejects; ``member`` is not eligible."""
    if member.status is TeamMemberStatus.UNSUPPORTED_FOR_TEAM:
        return _unsupported_team_member_detail(member)
    return f"agent '{member.name}' {_REJECTION_PHRASES[member.status][0]}"


def resolve_configured_team(
    team_name: str,
    team_members: list[MatrixID],
    mode: TeamMode,
    config: Config,
    runtime_paths: RuntimePaths,
    *,
    materializable_agent_names: set[str] | None = None,
) -> TeamResolution:
    """Resolve one configured team while preserving the exact configured membership."""
    requested_members = _filter_team_request_members(team_members, config, runtime_paths)
    member_statuses = _evaluate_team_members(
        requested_members,
        config,
        runtime_paths,
        room=None,
        sender_visible_responders=None,
        materializable_agent_names=materializable_agent_names,
        allow_direct_private_agents=False,
    )
    return _resolve_team_request(
        intent=TeamIntent.CONFIGURED_TEAM,
        requested_members=requested_members,
        member_statuses=member_statuses,
        config=config,
        reason_prefix=f"Team '{team_name}'",
        mode=mode,
    )


__all__ = [
    "TeamIntent",
    "TeamMemberStatus",
    "TeamOutcome",
    "TeamResolution",
    "TeamResolutionMember",
    "decide_team_formation",
    "not_materializable_team_agents_message",
    "resolve_configured_team",
    "resolve_live_shared_agent_names",
    "resolve_team_materializable_agent_names",
    "select_ad_hoc_team_mode",
    "team_member_name",
]

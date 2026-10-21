"""Materializing a team's exact members as agent builds, and choosing the leader's and members' models for a turn."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING

from mindroom.agents import create_agent
from mindroom.constants import ROUTER_AGENT_NAME
from mindroom.knowledge.utils import resolve_agent_knowledge_access
from mindroom.logging_config import get_logger
from mindroom.teams.formation import (
    not_materializable_team_agents_message,
    resolve_live_shared_agent_names,
    resolve_team_materializable_agent_names,
)
from mindroom.thread_models import resolve_thread_model_override

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence

    from mindroom.agent_loop.build import AgentBuild
    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.knowledge.refresh_scheduler import KnowledgeRefreshScheduler
    from mindroom.knowledge.utils import KnowledgeAvailabilityDetail
    from mindroom.runtime_protocols import OrchestratorRuntime
    from mindroom.tool_system.worker_routing import ToolExecutionIdentity

logger = get_logger(__name__)

NO_AGENTS_RESPONSE = "Sorry, no agents available for team collaboration."


@dataclass(frozen=True)
class ResolvedExactTeamMembers:
    """One exact requested member set after runtime materialization attempts."""

    requested_agent_names: list[str]
    builds: list[AgentBuild]
    display_names: list[str]
    materialized_agent_names: set[str]
    failed_agent_names: list[str]
    model_names: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class TeamTurnModelSelection:
    """Model aliases frozen together for one team turn."""

    team_model_name: str
    member_model_names: dict[str, str]


def materialize_exact_requested_team_members(
    requested_agent_names: list[str],
    *,
    materializable_agent_names: set[str] | None,
    build_member: Callable[[str], AgentBuild],
) -> ResolvedExactTeamMembers:
    """Materialize the exact requested team members without silent shrinkage."""
    if materializable_agent_names is not None:
        missing_agent_names = [name for name in requested_agent_names if name not in materializable_agent_names]
        if missing_agent_names:
            return ResolvedExactTeamMembers(
                requested_agent_names=requested_agent_names,
                builds=[],
                display_names=[],
                materialized_agent_names=set(),
                failed_agent_names=missing_agent_names,
            )

    builds: list[AgentBuild] = []
    materialized_agent_names: set[str] = set()
    failed_agent_names: list[str] = []
    for name in requested_agent_names:
        try:
            build = build_member(name)
        except Exception:
            logger.warning(
                "Failed to materialize exact team member",
                agent_name=name,
                exc_info=True,
            )
            failed_agent_names.append(name)
            continue
        builds.append(build)
        materialized_agent_names.add(name)

    return ResolvedExactTeamMembers(
        requested_agent_names=requested_agent_names,
        builds=builds,
        display_names=[build.spec.name for build in builds],
        materialized_agent_names=materialized_agent_names,
        failed_agent_names=failed_agent_names,
    )


def materialize_exact_team_members(
    requested_agent_names: list[str],
    *,
    config: Config,
    runtime_paths: RuntimePaths,
    execution_identity: ToolExecutionIdentity | None,
    include_openai_compat_guidance: bool = False,
    materializable_agent_names: set[str] | None = None,
    unavailable_bases: dict[str, KnowledgeAvailabilityDetail] | None = None,
    refresh_scheduler: KnowledgeRefreshScheduler | None = None,
    reason_prefix: str = "Team request",
    supports_native_tool_approval: bool = False,
    active_model_names: Mapping[str, str] | None = None,
    required_tool_names: Mapping[str, tuple[str, ...]] | None = None,
) -> ResolvedExactTeamMembers:
    """Materialize the exact team-member set without silent fallback."""
    if not requested_agent_names:
        raise ValueError(NO_AGENTS_RESPONSE)

    selected_model_names: dict[str, str] = {}

    def _build_member(agent_name: str) -> AgentBuild:
        knowledge_resolution = resolve_agent_knowledge_access(
            agent_name,
            config,
            runtime_paths,
            refresh_scheduler=refresh_scheduler,
            execution_identity=execution_identity,
        )
        if unavailable_bases is not None:
            unavailable_bases.update(knowledge_resolution.unavailable)
        model_name = (
            active_model_names[agent_name]
            if active_model_names is not None
            else config.resolve_runtime_model(
                entity_name=agent_name,
                room_id=execution_identity.room_id if execution_identity is not None else None,
                thread_id=execution_identity.resolved_thread_id if execution_identity is not None else None,
                runtime_paths=runtime_paths,
            ).model_name
        )
        selected_model_names[agent_name] = model_name
        return create_agent(
            agent_name,
            config,
            runtime_paths,
            execution_identity=execution_identity,
            active_model_name=model_name,
            knowledge=knowledge_resolution.knowledge,
            include_interactive_questions=False,
            include_openai_compat_guidance=include_openai_compat_guidance,
            refresh_scheduler=refresh_scheduler,
            supports_native_tool_approval=supports_native_tool_approval,
            required_tool_names=(required_tool_names or {}).get(agent_name, ()),
        )

    team_members = materialize_exact_requested_team_members(
        requested_agent_names,
        materializable_agent_names=materializable_agent_names,
        build_member=_build_member,
    )
    if team_members.failed_agent_names:
        raise ValueError(
            not_materializable_team_agents_message(team_members.failed_agent_names, prefix=reason_prefix),
        )
    return replace(team_members, model_names=selected_model_names)


def requested_team_agent_names(agent_names: Sequence[str]) -> list[str]:
    """Return the requested team members, excluding router placeholders."""
    return [name for name in agent_names if name != ROUTER_AGENT_NAME]


def resolve_team_member_model_names(
    agent_names: Sequence[str],
    room_id: str | None,
    config: Config,
    runtime_paths: RuntimePaths,
    *,
    thread_id: str | None = None,
) -> dict[str, str]:
    """Resolve every exact team member's model alias without yielding control."""
    return {
        agent_name: config.resolve_runtime_model(
            entity_name=agent_name,
            room_id=room_id,
            thread_id=thread_id,
            runtime_paths=runtime_paths,
        ).model_name
        for agent_name in requested_team_agent_names(agent_names)
    }


def materialize_team_members(
    agent_names: list[str],
    orchestrator: OrchestratorRuntime,
    execution_identity: ToolExecutionIdentity | None,
    *,
    configured_team_name: str | None = None,
    unavailable_bases: dict[str, KnowledgeAvailabilityDetail] | None = None,
    reason_prefix: str = "Team request",
    active_model_names: Mapping[str, str] | None = None,
) -> ResolvedExactTeamMembers:
    """Materialize the exact requested team-member set from the live orchestrator without silent fallback."""
    assert orchestrator.config is not None
    materializable_agent_names = resolve_team_materializable_agent_names(
        orchestrator.config,
        resolve_live_shared_agent_names(orchestrator),
        allow_direct_private_agents=allow_direct_private_team_agents(
            execution_identity,
            configured_team_name=configured_team_name,
        ),
    )
    return materialize_exact_team_members(
        requested_team_agent_names(agent_names),
        config=orchestrator.config,
        runtime_paths=orchestrator.runtime_paths,
        execution_identity=execution_identity,
        materializable_agent_names=materializable_agent_names,
        unavailable_bases=unavailable_bases,
        refresh_scheduler=orchestrator.knowledge_refresh_scheduler,
        supports_native_tool_approval=True,
        active_model_names=active_model_names,
        reason_prefix=reason_prefix,
    )


def allow_direct_private_team_agents(
    execution_identity: ToolExecutionIdentity | None,
    *,
    configured_team_name: str | None,
) -> bool:
    """Return whether direct private members may join this team request."""
    return (
        configured_team_name is None
        and execution_identity is not None
        and execution_identity.channel == "matrix"
        and bool(execution_identity.requester_id)
    )


def select_model_for_team(
    team_name: str,
    room_id: str,
    config: Config,
    runtime_paths: RuntimePaths,
    *,
    thread_id: str | None = None,
) -> str:
    """Return the model for a team in a room: the thread override, the persisted or configured room model, else the team's."""
    model_name = config.resolve_runtime_model(
        entity_name=team_name,
        room_id=room_id,
        thread_id=thread_id,
        runtime_paths=runtime_paths,
    ).model_name
    logger.info("selected_team_model", team_name=team_name, model_name=model_name)
    return model_name


def resolve_team_turn_models(
    team_name: str,
    member_names: Sequence[str],
    room_id: str,
    config: Config,
    runtime_paths: RuntimePaths,
    *,
    thread_id: str | None = None,
    active_model_name: str | None = None,
) -> TeamTurnModelSelection:
    """Freeze the coordinator and member model aliases in one synchronous snapshot."""
    # A configured team's access reaches its members, so its thread override governs them during its turns.
    if active_model_name is None and thread_id is not None and team_name in config.teams:
        active_model_name = resolve_thread_model_override(runtime_paths, thread_id, config=config).active.get(team_name)
    if active_model_name is not None:
        return TeamTurnModelSelection(
            team_model_name=active_model_name,
            member_model_names=dict.fromkeys(member_names, active_model_name),
        )
    return TeamTurnModelSelection(
        team_model_name=select_model_for_team(
            team_name,
            room_id,
            config,
            runtime_paths,
            thread_id=thread_id,
        ),
        member_model_names=resolve_team_member_model_names(
            member_names,
            room_id,
            config,
            runtime_paths,
            thread_id=thread_id,
        ),
    )


__all__ = [
    "NO_AGENTS_RESPONSE",
    "ResolvedExactTeamMembers",
    "TeamTurnModelSelection",
    "allow_direct_private_team_agents",
    "materialize_exact_requested_team_members",
    "materialize_exact_team_members",
    "materialize_team_members",
    "requested_team_agent_names",
    "resolve_team_member_model_names",
    "resolve_team_turn_models",
    "select_model_for_team",
]

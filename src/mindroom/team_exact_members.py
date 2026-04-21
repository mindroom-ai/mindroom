"""Authoritative runtime resolution for exact team member materialization."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from mindroom.constants import ROUTER_AGENT_NAME
from mindroom.logging_config import get_logger

if TYPE_CHECKING:
    from collections.abc import Callable

    from agno.agent import Agent

    from mindroom.config.main import Config
    from mindroom.orchestrator import MultiAgentOrchestrator


logger = get_logger(__name__)


@dataclass(frozen=True)
class ResolvedExactTeamMembers:
    """One exact requested member set after runtime materialization attempts."""

    requested_agent_names: list[str]
    agents: list[Agent]
    display_names: list[str]
    materialized_agent_names: set[str]
    failed_agent_names: list[str]


def resolve_live_shared_agent_names(
    orchestrator: MultiAgentOrchestrator,
    *,
    config: Config | None = None,
) -> set[str] | None:
    """Return running shared agent names when runtime availability is known."""
    active_config = config or orchestrator.config
    assert active_config is not None
    orchestrator_agent_bots = orchestrator.agent_bots
    if not isinstance(orchestrator_agent_bots, dict):
        return None
    return {
        name
        for name, bot in orchestrator_agent_bots.items()
        if name != ROUTER_AGENT_NAME and name in active_config.agents and bot.running
    }


def materialize_exact_requested_team_members(
    requested_agent_names: list[str],
    *,
    materializable_agent_names: set[str] | None,
    build_member: Callable[[str], Agent],
) -> ResolvedExactTeamMembers:
    """Materialize the exact requested team members without silent shrinkage."""
    if materializable_agent_names is not None:
        missing_agent_names = [name for name in requested_agent_names if name not in materializable_agent_names]
        if missing_agent_names:
            return ResolvedExactTeamMembers(
                requested_agent_names=requested_agent_names,
                agents=[],
                display_names=[],
                materialized_agent_names=set(),
                failed_agent_names=missing_agent_names,
            )

    agents: list[Agent] = []
    materialized_agent_names: set[str] = set()
    failed_agent_names: list[str] = []
    for name in requested_agent_names:
        try:
            agent = build_member(name)
        except Exception:
            logger.warning(
                "Failed to materialize exact team member",
                agent_name=name,
                exc_info=True,
            )
            failed_agent_names.append(name)
            continue
        agents.append(agent)
        materialized_agent_names.add(name)

    return ResolvedExactTeamMembers(
        requested_agent_names=requested_agent_names,
        agents=agents,
        display_names=[str(agent.name) for agent in agents if agent.name],
        materialized_agent_names=materialized_agent_names,
        failed_agent_names=failed_agent_names,
    )

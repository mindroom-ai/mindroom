"""Runtime availability filtering for responder candidate lists."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from mindroom.constants import ROUTER_AGENT_NAME
from mindroom.entity_resolution import entity_identity_registry
from mindroom.team_exact_members import resolve_live_shared_agent_names
from mindroom.teams import TeamMode, TeamOutcome, resolve_configured_team

if TYPE_CHECKING:
    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.matrix.identity import MatrixID
    from mindroom.runtime_protocols import OrchestratorRuntime, SupportsRunningState


def materializable_agent_names_for_orchestrator(
    orchestrator: OrchestratorRuntime | None,
    config: Config,
) -> set[str] | None:
    """Return concrete agents that can currently produce a response when known."""
    if orchestrator is None:
        return None
    return resolve_live_shared_agent_names(orchestrator, config=config)


def live_responder_entity_names(
    orchestrator: OrchestratorRuntime | None,
    config: Config,
) -> set[str] | None:
    """Return running agent/team responder bot names when runtime state is known."""
    if orchestrator is None:
        return None
    orchestrator_agent_bots = orchestrator.agent_bots
    if not isinstance(orchestrator_agent_bots, dict):
        return None
    running_bots = cast("dict[str, SupportsRunningState]", orchestrator_agent_bots)
    configured_responder_names = set(config.agents) | set(config.teams)
    return {
        name
        for name, bot in running_bots.items()
        if name != ROUTER_AGENT_NAME and name in configured_responder_names and bot.running
    }


def _configured_team_is_materializable(
    team_name: str,
    config: Config,
    runtime_paths: RuntimePaths,
    *,
    materializable_agent_names: set[str],
    live_entity_names: set[str] | None,
) -> bool:
    """Return whether one configured team responder can currently answer."""
    team_config = config.teams.get(team_name)
    if team_config is None:
        return False
    if live_entity_names is not None and team_name not in live_entity_names:
        return False

    registry = entity_identity_registry(config, runtime_paths)
    team_agents = [registry.current_id(agent_name) for agent_name in team_config.agents]
    configured_mode = TeamMode.COORDINATE if team_config.mode == "coordinate" else TeamMode.COLLABORATE
    team_resolution = resolve_configured_team(
        team_name,
        team_agents,
        configured_mode,
        config,
        runtime_paths,
        materializable_agent_names=materializable_agent_names,
    )
    return team_resolution.outcome is TeamOutcome.TEAM


def filter_materializable_responders(
    responder_ids: list[MatrixID],
    config: Config,
    runtime_paths: RuntimePaths,
    *,
    materializable_agent_names: set[str] | None,
    live_entity_names: set[str] | None = None,
) -> list[MatrixID]:
    """Keep responder candidates that are live/materializable when runtime state is known."""
    if materializable_agent_names is None:
        return responder_ids

    registry = entity_identity_registry(config, runtime_paths)
    filtered_responders: list[MatrixID] = []
    for responder_id in responder_ids:
        entity_name = registry.current_entity_name_for_user_id(responder_id.full_id, include_router=False)
        is_materializable_agent = entity_name in config.agents and entity_name in materializable_agent_names
        is_materializable_team = entity_name in config.teams and _configured_team_is_materializable(
            entity_name,
            config,
            runtime_paths,
            materializable_agent_names=materializable_agent_names,
            live_entity_names=live_entity_names,
        )
        if is_materializable_agent or is_materializable_team:
            filtered_responders.append(responder_id)
    return filtered_responders

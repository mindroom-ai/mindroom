"""A generated agent is its base agent's raw config plus its owner's edits."""

from __future__ import annotations

from collections import Counter
from copy import deepcopy
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from mindroom.logging_config import get_logger

if TYPE_CHECKING:
    from mindroom.config.main import Config
    from mindroom.config.personal_agents import CreatedAgentEntry, GeneratedAgentEdits, PersonalAgentRoster

logger = get_logger(__name__)


@dataclass(frozen=True)
class GeneratedAgentInfo:
    """What the effective config remembers about one personal or created agent."""

    base: str
    owner: str
    co_owners: tuple[str, ...]
    created: bool
    # The base's pass-one tool names, which the owner's tools edit may choose from.
    allowed_tools: tuple[str, ...]


def tool_ceiling(config: Config, agent_name: str) -> tuple[str, ...]:
    """Return the tool names a generated agent based on this agent may choose from."""
    return tuple(entry.name for entry in config._get_agent_authored_tool_configs(agent_name))


def _generated_agent_data(
    base: Config,
    base_agent: str,
    edits: GeneratedAgentEdits | None,
    *,
    created: CreatedAgentEntry | None = None,
) -> tuple[dict[str, Any], list[str]]:
    """Build the raw agent entry of a generated agent and the chosen tool names the base does not offer."""
    data = base.agents[base_agent].model_dump(mode="json", exclude_unset=True)
    dropped: list[str] = []
    if edits is not None:
        for field in ("display_name", "role", "instructions"):
            if (value := getattr(edits, field)) is not None:
                data[field] = value
        if edits.tools is not None:
            offered = {entry.name: entry for entry in base._get_agent_authored_tool_configs(base_agent)}
            chosen = list(dict.fromkeys(edits.tools))
            dropped = [name for name in chosen if name not in offered]
            # Each entry is copied whole and defaults stay off, so no tool comes back with other settings.
            # A flag is written only when set, because presets and control-plane tools may not carry one at all.
            data["tools"] = [
                {
                    "name": name,
                    "overrides": deepcopy(offered[name].tool_config_overrides),
                    **{flag: True for flag in ("defer", "initial") if getattr(offered[name], flag)},
                }
                for name in chosen
                if name in offered
            ]
            data["include_default_tools"] = False
    if created is not None:
        owners = [created.owner, *created.co_owners]
        data |= {
            "access": {"users": owners, "members_of_rooms": []},
            "credential_managers": owners,
            "accept_invites": owners,
            "rooms": [],
            "automations": None,
            "delegate_to": [],
            "allow_self_config": False,
            "created_agents_per_owner": 0,
        }
    return data, dropped


def _created_skip_reason(
    base: Config,
    created: CreatedAgentEntry,
    taken: set[str],
    counts: Counter[tuple[str, str]],
) -> str | None:
    if created.pending:
        return "pending"
    if (creator := base.agents.get(created.creator)) is None:
        return "creator_missing"
    # Active entries count in roster order, so lowering the cap keeps an owner's oldest agents.
    counts[created.creator, created.owner] += 1
    if counts[created.creator, created.owner] > creator.created_agents_per_owner:
        return "over_cap"
    return "name_taken" if created.agent_name in taken else None


def expand_generated_agents(
    base: Config,
    data: dict[str, Any],
    roster: PersonalAgentRoster,
) -> tuple[dict[str, Any] | None, dict[str, GeneratedAgentInfo]]:
    """Apply owners' edits and add created agents to the pass-one data; None means nothing changed."""
    infos: dict[str, GeneratedAgentInfo] = {}
    builds: list[tuple[str, str, GeneratedAgentEdits, CreatedAgentEntry | None]] = []
    for entry in roster.entries:
        name = entry.agent_name
        if base.personal_agent_instance(name) is not None:
            infos[name] = GeneratedAgentInfo(name, entry.user_id, (), False, tool_ceiling(base, name))
            if entry.edits is not None:
                builds.append((name, name, entry.edits, None))

    agents = dict(data.get("agents") or {})
    taken = {*agents, *base.teams, *base.personal_agents, *(entry.agent_name for entry in roster.entries)}
    counts: Counter[tuple[str, str]] = Counter()
    for created in roster.created:
        name, creator = created.agent_name, created.creator
        if (reason := _created_skip_reason(base, created, taken, counts)) is not None:
            logger.warning("created_agent_skipped", agent_name=name, creator=creator, reason=reason)
            continue
        taken.add(name)
        co_owners = tuple(created.co_owners)
        infos[name] = GeneratedAgentInfo(creator, created.owner, co_owners, True, tool_ceiling(base, creator))
        builds.append((name, creator, created.edits, created))

    for name, base_agent, edits, created in builds:
        agents[name], dropped = _generated_agent_data(base, base_agent, edits, created=created)
        if dropped:
            logger.warning("generated_agent_tool_dropped", agent_name=name, base=base_agent, tools=dropped)
    return ({**data, "agents": agents} if builds else None), infos

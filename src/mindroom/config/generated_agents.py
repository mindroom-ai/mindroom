"""A generated agent is its base agent's raw config plus its owner's edits."""

from __future__ import annotations

from copy import deepcopy
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from mindroom.config.main import Config
    from mindroom.config.personal_agents import CreatedAgentEntry, GeneratedAgentEdits


def tool_ceiling(config: Config, agent_name: str) -> tuple[str, ...]:
    """Return the tool names a generated agent based on this agent may choose from."""
    return tuple(entry.name for entry in config._get_agent_authored_tool_configs(agent_name))


def generated_agent_data(
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
            data["tools"] = [
                {
                    "name": name,
                    "overrides": deepcopy(offered[name].tool_config_overrides),
                    "defer": offered[name].defer,
                    "initial": offered[name].initial,
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

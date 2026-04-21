"""Shared behavior-level descriptions for configured agents and teams."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.constants import ROUTER_AGENT_NAME

if TYPE_CHECKING:
    from mindroom.config.main import Config

__all__ = ["describe_agent"]

_MAX_INSTRUCTION_LENGTH = 100


def describe_agent(agent_name: str, config: Config) -> str:
    """Generate a description of an agent or team based on its configuration."""
    if agent_name == ROUTER_AGENT_NAME:
        return (
            "router\n"
            "  - Route messages to the most appropriate agent based on context and expertise.\n"
            "  - Analyzes incoming messages and determines which agent is best suited to respond."
        )

    if agent_name in config.teams:
        team_config = config.teams[agent_name]
        parts = [f"{agent_name}"]
        if team_config.role:
            parts.append(f"- {team_config.role}")
        parts.append(f"- Team of agents: {', '.join(team_config.agents)}")
        parts.append(f"- Collaboration mode: {team_config.mode}")
        return "\n  ".join(parts)

    if agent_name not in config.agents:
        return f"{agent_name}: Unknown agent or team"

    agent_config = config.agents[agent_name]
    parts = [f"{agent_name}"]
    if agent_config.role:
        parts.append(f"- {agent_config.role}")

    effective_tools = config.get_agent_tools(agent_name)
    if effective_tools:
        parts.append(f"- Tools: {', '.join(effective_tools)}")

    if agent_config.delegate_to:
        parts.append(f"- Can delegate to: {', '.join(agent_config.delegate_to)}")

    if agent_config.instructions:
        first_instruction = agent_config.instructions[0]
        if len(first_instruction) < _MAX_INSTRUCTION_LENGTH:
            parts.append(f"- {first_instruction}")

    return "\n  ".join(parts)

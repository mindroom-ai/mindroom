"""Team-based collaboration for multiple agents."""

from __future__ import annotations

from enum import Enum
from typing import TYPE_CHECKING, NamedTuple

from agno.agent import Agent
from agno.run.team import TeamRunResponse
from agno.team import Team

from .agent_config import ROUTER_AGENT_NAME
from .ai import get_model_instance
from .logging_config import get_logger

if TYPE_CHECKING:
    from .bot import MultiAgentOrchestrator


logger = get_logger(__name__)


class TeamMode(str, Enum):
    """Team collaboration modes."""

    COORDINATE = "coordinate"  # Sequential, building on each other
    COLLABORATE = "collaborate"  # Parallel, synthesized


class ShouldFormTeamResult(NamedTuple):
    """Result of should_form_team."""

    should_form_team: bool
    agents: list[str]
    mode: TeamMode


def should_form_team(
    tagged_agents: list[str],
    agents_in_thread: list[str],
    all_mentioned_in_thread: list[str],
) -> ShouldFormTeamResult:
    """Determine if a team should form and with which mode."""
    # Case 1: Multiple agents explicitly tagged
    if len(tagged_agents) > 1:
        logger.info(f"Team formation needed for tagged agents: {tagged_agents}")
        return ShouldFormTeamResult(
            should_form_team=True,
            agents=tagged_agents,
            mode=TeamMode.COORDINATE,
        )

    # Case 2: No agents tagged but multiple were mentioned before in thread
    if not tagged_agents and len(all_mentioned_in_thread) > 1:
        logger.info(f"Team formation needed for previously mentioned agents: {all_mentioned_in_thread}")
        return ShouldFormTeamResult(
            should_form_team=True,
            agents=all_mentioned_in_thread,
            mode=TeamMode.COLLABORATE,
        )

    # Case 3: No agents tagged but multiple in thread
    if not tagged_agents and len(agents_in_thread) > 1:
        logger.info(f"Team formation needed for thread agents: {agents_in_thread}")
        return ShouldFormTeamResult(
            should_form_team=True,
            agents=agents_in_thread,
            mode=TeamMode.COLLABORATE,
        )

    return ShouldFormTeamResult(
        should_form_team=False,
        agents=[],
        mode=TeamMode.COLLABORATE,
    )


async def create_team_response(
    agent_names: list[str],
    mode: TeamMode,
    message: str,
    orchestrator: MultiAgentOrchestrator,
    thread_history: list[dict] | None = None,
) -> str:
    """Create a team and execute response."""

    # Get existing agent instances from the orchestrator
    agents: list[Agent] = []
    for name in agent_names:
        if name == ROUTER_AGENT_NAME:
            continue

        # Use the existing agent instance from the bot
        agent_bot = orchestrator.agent_bots[name]
        agents.append(agent_bot.agent)

    if not agents:
        return "Sorry, no agents available for team collaboration."

    # Build team identity context
    agent_identities = []
    agent_tools = []
    for agent in agents:
        # Extract a brief role description
        role_desc = "Team member"
        if hasattr(agent, "role") and agent.role:
            # Take the first line or first 100 chars of the role
            role_lines = str(agent.role).split("\n")
            for line in role_lines:
                if line.strip() and not line.startswith("#"):
                    role_desc = line.strip()[:100]
                    break

        agent_identities.append(f"- **{agent.name}**: {role_desc}")

        # List available tools for each agent
        if hasattr(agent, "tools") and agent.tools:
            tools_list = [
                tool.name if hasattr(tool, "name") else str(tool) for tool in agent.tools[:5]
            ]  # First 5 tools
            if tools_list:
                agent_tools.append(f"- **{agent.name}** has access to: {', '.join(tools_list)}")

    tools_section = ""
    if agent_tools:
        tools_section = f"\n## Available Tools\n{chr(10).join(agent_tools)}\n"

    # Determine team mode instruction
    mode_instruction = ""
    if mode == TeamMode.COORDINATE:
        mode_instruction = "\n**Mode: COORDINATE** - Work sequentially, building on each other's contributions. Each agent should add their unique perspective."
    elif mode == TeamMode.COLLABORATE:
        mode_instruction = (
            "\n**Mode: COLLABORATE** - Work together in parallel, combining your expertise into a unified response."
        )

    team_identity = f"""## Team Collaboration Context

You are working as a team with the following members:
{chr(10).join(agent_identities)}
{tools_section}{mode_instruction}

**Important Instructions:**
1. Each agent IS one of the team members listed above - you collectively control these agents
2. Leverage each agent's specific expertise and tools to provide a comprehensive response
3. Work as a coordinated team to address the user's request
4. When the user mentions specific agents (like NewsAgent or CodeAgent), recognize that YOU are those agents

"""

    # Build prompt with context
    prompt = team_identity + message
    if thread_history:
        recent_messages = thread_history[-3:]  # Last 3 messages for context
        context_parts = []
        for msg in recent_messages:
            sender = msg.get("sender", "Unknown")
            body = msg.get("content", {}).get("body", "")
            if body and len(body) < 200:
                context_parts.append(f"{sender}: {body}")

        if context_parts:
            prompt = f"{team_identity}## Thread Context:\n{'\n'.join(context_parts)}\n\n## User Request:\n{message}"

    # Create and run team
    team = Team(
        members=agents,  # type: ignore[arg-type]
        mode=mode.value,
        name=f"Team-{'-'.join(agent_names)}",
        model=get_model_instance("default"),
    )

    logger.info(f"Executing team response with {len(agents)} agents")
    response = await team.arun(prompt)

    # Extract response content
    if isinstance(response, TeamRunResponse):
        team_response = str(response.content)
    else:
        logger.warning(f"Unexpected response type: {type(response)}", response=response)
        team_response = str(response)

    # Prepend team information to the response
    # Don't use @ mentions as that would trigger the agents again
    agent_list = ", ".join([name for name in agent_names if name != ROUTER_AGENT_NAME])
    team_header = f"🤝 **Team Response** ({agent_list}):\n\n"

    return team_header + team_response

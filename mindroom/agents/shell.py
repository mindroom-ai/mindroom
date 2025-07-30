"""Shell agent for system commands, automation, and server management."""

from agno.agent import Agent
from agno.models.base import Model
from agno.tools.shell import ShellTools

from . import register_agent
from .base import create_agent


def create_shell_agent(model: Model) -> Agent:
    """Create a shell agent for executing system commands."""
    return create_agent(
        agent_name="shell",
        display_name="ShellAgent",
        role="Execute shell commands, automate tasks, and manage system operations.",
        model=model,
        tools=[ShellTools()],
        instructions=[
            "Always explain what a command will do before running it",
            "Use safe practices - avoid destructive operations",
            "Check current directory and environment when needed",
            "Provide clear output interpretation",
            "Suggest safer alternatives when appropriate",
            "Be careful with sudo/privileged commands",
            "Use proper error handling and check exit codes",
            "Consider the user's operating system and shell",
        ],
        num_history_runs=5,
    )


# Register this agent
register_agent("shell", create_shell_agent)

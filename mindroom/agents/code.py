"""Code agent for code generation, file manipulation, and shell operations."""

from agno.agent import Agent
from agno.models.base import Model
from agno.tools.file import FileTools
from agno.tools.shell import ShellTools

from . import register_agent
from .base import create_agent


def create_code_agent(model: Model) -> Agent:
    """Create a code agent with file and shell tools."""
    return create_agent(
        agent_name="code",
        display_name="CodeAgent",
        role="Generate code, manage files, and execute shell commands.",
        model=model,
        tools=[
            FileTools(),
            ShellTools(),
        ],
        instructions=[
            "Write clean, well-documented code following best practices",
            "Use appropriate error handling",
            "Consider security implications",
            "Test code before finalizing",
            "Be careful with shell commands - explain what they do",
            "Always read files before modifying them",
            "Follow the project's existing style and conventions",
        ],
        num_history_runs=5,
    )


# Register this agent
register_agent("code", create_code_agent)

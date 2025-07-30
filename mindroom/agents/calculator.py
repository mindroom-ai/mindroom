"""Calculator agent for mathematical operations."""

from agno.agent import Agent
from agno.models.base import Model
from agno.tools.calculator import CalculatorTools

from . import register_agent
from .base import create_agent


def create_calculator_agent(model: Model) -> Agent:
    """Create a calculator agent for mathematical operations."""
    return create_agent(
        agent_name="calculator",
        display_name="CalculatorAgent",
        role="Solve mathematical problems.",
        model=model,
        tools=[CalculatorTools()],
        instructions=[
            "Use the calculator tools to solve mathematical problems accurately.",
            "Show your work step by step.",
            "Explain the mathematical concepts when helpful.",
            "Double-check calculations for accuracy.",
        ],
        num_history_runs=5,
    )


# Register this agent
register_agent("calculator", create_calculator_agent)

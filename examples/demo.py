#!/usr/bin/env python3
"""Demo script showing how to use the new agents."""

import asyncio
import os

from agno.models.ollama import Ollama
from dotenv import load_dotenv

from mindroom.agents import get_agent, list_agents

# Load environment variables
load_dotenv()


async def main():
    """Demo the agent system."""
    # List available agents
    print("Available agents:")
    for agent_name in list_agents():
        print(f"  - {agent_name}")
    print()

    # Get model
    model_id = os.getenv("AGNO_MODEL", "ollama:llama3.2:3b")
    if model_id.startswith("ollama:"):
        model = Ollama(id=model_id.split(":", 1)[1])
    else:
        print(f"Demo only supports Ollama models, got: {model_id}")
        return

    # Demo calculator agent
    print("=== Calculator Agent Demo ===")
    calculator = get_agent("calculator", model)
    result = await calculator.arun("What is 25 * 4 + 10?")
    print(f"Result: {result.content}")
    print()

    # Demo code agent
    print("=== Code Agent Demo ===")
    code = get_agent("code", model)
    result = await code.arun("Write a Python function to calculate fibonacci numbers")
    print(f"Result: {result.content}")
    print()

    # Demo summary agent
    print("=== Summary Agent Demo ===")
    summary = get_agent("summary", model)
    text = """
    The Matrix AI system is a multi-agent chat bot that runs on Matrix protocol.
    It supports multiple specialized agents like calculator, code, research, and more.
    Each agent has its own memory and can maintain context across conversations.
    The system uses the Agno framework for AI integration and matrix-nio for Matrix communication.
    """
    result = await summary.arun(f"Summarize this text in one sentence: {text}")
    print(f"Result: {result.content}")
    print()

    # Demo general agent
    print("=== General Agent Demo ===")
    general = get_agent("general", model)
    result = await general.arun("What are the benefits of using multiple specialized AI agents?")
    print(f"Result: {result.content}")


if __name__ == "__main__":
    asyncio.run(main())

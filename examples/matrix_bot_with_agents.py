#!/usr/bin/env python3
"""Example showing how to use the Matrix bot with multiple agents.

This example demonstrates:
1. Starting the Matrix bot
2. Using different agents via @agent_name: syntax
3. Thread conversations with context preservation
"""

import asyncio
import os

from dotenv import load_dotenv

# Load environment variables
load_dotenv()


def print_usage():
    """Print usage instructions."""
    print("""
Matrix AI Bot - Multi-Agent System
==================================

Available agents:
- @general: General conversation and assistance
- @calculator: Mathematical calculations
- @code: Code generation and file operations
- @shell: Shell command execution
- @summary: Text summarization
- @research: Web research (requires additional dependencies)
- @finance: Financial data (requires additional dependencies)
- @news: News and current events (requires additional dependencies)

Usage examples:
1. General conversation:
   "@bot Hello! Can you help me understand quantum computing?"

2. Using specific agents:
   "@calculator: What is 25 * 4 + 10?"
   "@code: Write a Python function to sort a list"
   "@summary: Summarize this article: [paste article]"

3. In threads:
   - All messages in a thread are automatically treated as mentions
   - Agents can see the full thread history when mentioned
   - Use different agents in the same thread for complex tasks

Configuration:
- Set MATRIX_HOMESERVER, MATRIX_USER_ID, and MATRIX_PASSWORD in .env
- Set AGNO_MODEL to choose the AI model (e.g., "openai:gpt-4", "anthropic:claude-3-opus")
""")


async def main():
    """Run the Matrix bot with agents."""
    print_usage()

    # Check required environment variables
    required_vars = ["MATRIX_HOMESERVER", "MATRIX_USER_ID", "MATRIX_PASSWORD", "AGNO_MODEL"]
    missing_vars = [var for var in required_vars if not os.getenv(var)]

    if missing_vars:
        print(f"\nError: Missing required environment variables: {', '.join(missing_vars)}")
        print("Please set them in your .env file")
        return

    print("\nStarting Matrix bot...")
    print(f"Homeserver: {os.getenv('MATRIX_HOMESERVER')}")
    print(f"Bot user: {os.getenv('MATRIX_USER_ID')}")
    print(f"AI model: {os.getenv('AGNO_MODEL')}")

    # Import and run the bot
    from bot import main as run_bot

    try:
        await run_bot()
    except KeyboardInterrupt:
        print("\nBot stopped by user")
    except Exception as e:
        print(f"\nError running bot: {e}")


if __name__ == "__main__":
    asyncio.run(main())

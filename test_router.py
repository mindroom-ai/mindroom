#!/usr/bin/env python3
"""Test script for router agent functionality."""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from src.mindroom.router_agent import RouterAgent, should_router_handle
from src.mindroom.thread_utils import extract_agent_name, get_agents_in_thread


def test_extract_agent_name():
    """Test agent name extraction."""
    print("Testing extract_agent_name...")

    tests = [
        ("@mindroom_calculator:localhost", "calculator"),
        ("@mindroom_general:localhost", "general"),
        ("@mindroom_user_12345:localhost", None),  # Regular user
        ("@regular_user:localhost", None),  # Not an agent
        ("invalid", None),  # Invalid format
    ]

    for input_val, expected in tests:
        result = extract_agent_name(input_val)
        status = "✓" if result == expected else "✗"
        print(f"  {status} {input_val} -> {result} (expected: {expected})")


def test_get_agents_in_thread():
    """Test agent detection in thread."""
    print("\nTesting get_agents_in_thread...")

    thread_history = [
        {"sender": "@mindroom_calculator:localhost", "body": "The answer is 42"},
        {"sender": "@mindroom_user_123:localhost", "body": "Thanks!"},
        {"sender": "@mindroom_general:localhost", "body": "You're welcome"},
        {"sender": "@mindroom_calculator:localhost", "body": "Indeed"},
    ]

    agents = get_agents_in_thread(thread_history)
    print(f"  Found agents: {agents}")
    assert agents == ["calculator", "general"]
    print("  ✓ Correctly identified agents in thread")


def test_should_router_handle():
    """Test router decision logic."""
    print("\nTesting should_router_handle...")

    tests = [
        # (mentioned_agents, agents_in_thread, is_thread, expected)
        (["calculator"], ["calculator", "general"], True, False),  # Agent mentioned
        ([], ["calculator"], True, False),  # Single agent in thread
        ([], ["calculator", "general"], True, True),  # Multiple agents, none mentioned
        ([], [], False, False),  # Not in thread
        ([], ["calculator", "general"], False, False),  # Not in thread
    ]

    for mentioned, in_thread, is_thread, expected in tests:
        result = should_router_handle(mentioned, in_thread, is_thread)
        status = "✓" if result == expected else "✗"
        print(f"  {status} mentioned={mentioned}, in_thread={in_thread}, is_thread={is_thread} -> {result}")


async def test_router_agent():
    """Test router agent suggestion."""
    print("\nTesting RouterAgent...")

    router = RouterAgent()

    # Test routing prompt creation
    message = "Can you help me calculate the compound interest on my investment?"
    available = ["calculator", "general", "finance"]

    prompt = router.create_routing_prompt(message, available)
    print(f"  ✓ Created routing prompt ({len(prompt)} chars)")

    # Test thread summarization
    thread_context = [
        {"sender": "@user:localhost", "body": "I need help with my portfolio"},
        {"sender": "@mindroom_finance:localhost", "body": "I can help analyze your portfolio"},
    ]

    summary = router._summarize_thread(thread_context)
    print(f"  ✓ Thread summary: {summary}")

    print("\nRouter agent tests completed!")


async def main():
    """Run all tests."""
    print("=" * 60)
    print("Router Agent Test Suite")
    print("=" * 60)

    test_extract_agent_name()
    test_get_agents_in_thread()
    test_should_router_handle()
    await test_router_agent()

    print("\n✅ All tests completed!")


if __name__ == "__main__":
    asyncio.run(main())

#!/usr/bin/env python3
"""Test script for router agent structured output functionality."""

import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

sys.path.insert(0, str(Path(__file__).parent))

from src.mindroom.router_agent import AgentSuggestion, RouterAgent


class MockToolUse:
    """Mock tool use block from Anthropic response."""

    def __init__(self, input_data):
        self.type = "tool_use"
        self.input = input_data


class MockTextBlock:
    """Mock text block from Anthropic response."""

    def __init__(self, text):
        self.type = "text"
        self.text = text


async def test_router_structured_output():
    """Test router agent with mocked structured output."""
    print("\nTesting RouterAgent structured output...")

    # Create router
    router = RouterAgent()

    # Mock the get_client import
    from unittest.mock import patch

    # Create mock response
    mock_client = AsyncMock()
    mock_response = MagicMock()

    # Create structured output
    suggestion_data = {
        "agent_name": "calculator",
        "reasoning": "The user is asking about compound interest calculation",
        "confidence": 0.85,
    }

    # Mock response content with tool use
    mock_response.content = [
        MockTextBlock("I'll analyze this message and suggest the appropriate agent."),
        MockToolUse(suggestion_data),
    ]

    mock_client.messages.create = AsyncMock(return_value=mock_response)

    with patch("src.mindroom.ai.get_client", return_value=mock_client):
        # Test the suggestion
        message = "Can you help me calculate the compound interest on my investment?"
        available = ["calculator", "general", "finance"]

        suggestion = await router.suggest_agent(message, available)

        # Verify results
        assert suggestion is not None, "Should get a suggestion"
        assert isinstance(suggestion, AgentSuggestion), "Should be AgentSuggestion instance"
        assert suggestion.agent_name == "calculator", f"Expected calculator, got {suggestion.agent_name}"
        assert suggestion.confidence == 0.85, f"Expected 0.85, got {suggestion.confidence}"
        assert "compound interest" in suggestion.reasoning.lower(), "Reasoning should mention compound interest"

        print(f"  ✓ Got suggestion: {suggestion.agent_name} (confidence: {suggestion.confidence})")
        print(f"  ✓ Reasoning: {suggestion.reasoning}")

        # Verify the API was called correctly
        mock_client.messages.create.assert_called_once()
        call_args = mock_client.messages.create.call_args.kwargs

        assert call_args["model"] == "claude-3-5-sonnet-20241022"
        assert call_args["max_tokens"] == 500
        assert call_args["temperature"] == 0.3
        assert len(call_args["tools"]) == 1
        assert call_args["tools"][0]["name"] == "suggest_agent"
        assert call_args["tool_choice"]["type"] == "tool"
        assert call_args["tool_choice"]["name"] == "suggest_agent"

        print("  ✓ API called with correct parameters")


async def test_router_error_handling():
    """Test router agent error handling."""
    print("\nTesting RouterAgent error handling...")

    router = RouterAgent()

    # Test with API error
    from unittest.mock import patch

    mock_client = AsyncMock()
    mock_client.messages.create = AsyncMock(side_effect=Exception("API Error"))

    with patch("src.mindroom.ai.get_client", return_value=mock_client):
        suggestion = await router.suggest_agent("test message", ["general"])
        assert suggestion is None, "Should return None on error"
        print("  ✓ Handles API errors gracefully")

    # Test with missing tool use
    mock_response = MagicMock()
    mock_response.content = [MockTextBlock("Just text, no tool use")]
    mock_client.messages.create = AsyncMock(return_value=mock_response)

    with patch("src.mindroom.ai.get_client", return_value=mock_client):
        suggestion = await router.suggest_agent("test message", ["general"])
        assert suggestion is None, "Should return None when no tool use found"
        print("  ✓ Handles missing tool use gracefully")


async def main():
    """Run all tests."""
    print("=" * 60)
    print("Router Agent Structured Output Test Suite")
    print("=" * 60)

    await test_router_structured_output()
    await test_router_error_handling()

    print("\n✅ All structured output tests completed!")


if __name__ == "__main__":
    asyncio.run(main())

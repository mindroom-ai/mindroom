#!/usr/bin/env python3
"""Test router agent structured output with mocks."""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.mindroom.router_agent import AgentSuggestion, RouterAgent


async def test_router_structured_output():
    """Test router agent with mocked AI response."""
    print("Testing RouterAgent structured output...")

    router = RouterAgent()

    import json
    from unittest.mock import patch

    # Create mock JSON response
    suggestion_data = {
        "agent_name": "calculator",
        "reasoning": "The user is asking about compound interest calculation",
        "confidence": 0.85,
    }
    mock_json_response = json.dumps(suggestion_data)

    with patch("src.mindroom.ai.ai_response", return_value=mock_json_response):
        # Test the suggestion
        message = "Can you help me calculate the compound interest on my investment?"
        available = ["calculator", "general", "finance"]

        suggestion = await router.suggest_agent(message, available)

        # Verify results
        assert suggestion is not None, "Should get a suggestion"
        assert isinstance(suggestion, AgentSuggestion), "Should be AgentSuggestion instance"
        assert suggestion.agent_name == "calculator", f"Expected calculator, got {suggestion.agent_name}"
        assert suggestion.confidence == 0.85, f"Expected 0.85, got {suggestion.confidence}"

        print(f"  ✓ Got suggestion: {suggestion.agent_name} (confidence: {suggestion.confidence})")
        print(f"  ✓ Reasoning: {suggestion.reasoning}")
        print("  ✓ Uses existing AI infrastructure (not hardcoded Anthropic)")


async def test_router_error_handling():
    """Test router agent error handling."""
    print("\nTesting RouterAgent error handling...")

    router = RouterAgent()

    from unittest.mock import patch

    # Test with AI response error
    with patch("src.mindroom.ai.ai_response", side_effect=Exception("AI Error")):
        suggestion = await router.suggest_agent("test message", ["general"])
        assert suggestion is None, "Should return None on error"
        print("  ✓ Handles AI errors gracefully")

    # Test with invalid JSON response
    with patch("src.mindroom.ai.ai_response", return_value="Invalid JSON response"):
        suggestion = await router.suggest_agent("test message", ["general"])
        assert suggestion is None, "Should return None for invalid JSON"
        print("  ✓ Handles invalid JSON gracefully")


async def main():
    """Run all tests."""
    print("=" * 60)
    print("Router Agent Structured Output Tests")
    print("=" * 60)

    await test_router_structured_output()
    await test_router_error_handling()

    print("\n✅ All structured output tests completed!")


if __name__ == "__main__":
    asyncio.run(main())

"""Historical Responses message shapes the OpenAI-family formatters still read from stored runs.

Each repair returns request-only copies; stored history is never rewritten.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from mindroom.llm.messages import Message

__all__ = ["repair_legacy_responses_span"]


# LEGACY_COMPAT: Responses history without reusable ordered provider output.
# Legacy format: Agno-only Responses spans retain a lossy reasoning tail and original function-call item IDs.
# Last legacy release: No universal writer cutoff; v2026.9.128 predates ordered capture in v2026.9.129,
# but current stored, nonportable responses can still omit ordered output.
# Handling: When ordered output cannot be reused, remove the tail and optional item IDs from request-only copies.
# Coverage: tests/providers/test_openai_family_requests.py::test_legacy_responses_replay_rebuilds_calls_without_reasoning
def repair_legacy_responses_span(message: Message, formatted_span: list[Any]) -> list[dict[str, Any]]:
    """Remove unverifiable replay remnants while preserving normalized call/result links."""
    if not message.tool_calls:
        return formatted_span[:1]
    return [
        {key: value for key, value in item.items() if key != "id"}
        for item in formatted_span
        if isinstance(item, dict) and item.get("type") == "function_call"
    ]

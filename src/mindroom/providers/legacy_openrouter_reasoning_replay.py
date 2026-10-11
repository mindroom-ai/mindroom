"""Compatibility repair for historical OpenRouter reasoning-detail replay."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.providers.agno_compat_openrouter_reasoning import coalesced_openrouter_reasoning_details

if TYPE_CHECKING:
    from agno.models.message import Message


# LEGACY_COMPAT: Persisted OpenRouter reasoning details stored as streamed token fragments.
# Legacy format: Streamed OpenRouter assistant messages stored one `reasoning.text` dictionary per chunk in `provider_data["reasoning_details"]`.
# Last legacy release: v2026.10.243; replacement: the next release coalesces streamed fragments before persistence.
# Handling: Join adjacent text fragments with identical metadata in a request-only copy, leaving stored history unchanged.
# Coverage: tests/test_openai_models.py::test_openrouter_reasoning_details_replay_coalesces_without_mutating_history.
def repair_legacy_openrouter_reasoning_details(message: Message) -> Message:
    """Return a replay copy whose fragmented reasoning details are coalesced."""
    if message.role != "assistant" or not message.provider_data:
        return message
    details = message.provider_data.get("reasoning_details")
    normalized_details = coalesced_openrouter_reasoning_details(details)
    if normalized_details == details:
        return message
    return message.model_copy(
        update={"provider_data": {**message.provider_data, "reasoning_details": normalized_details}},
    )

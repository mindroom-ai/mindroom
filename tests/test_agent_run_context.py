"""Tests for shared agent-run context helpers."""

from __future__ import annotations

from mindroom.agent_run_context import append_knowledge_availability_enrichment, prepend_knowledge_availability_notice
from mindroom.hooks import EnrichmentItem
from mindroom.knowledge.availability import KnowledgeAvailability
from mindroom.knowledge.utils import KnowledgeAvailabilityDetail


def test_append_knowledge_availability_enrichment_adds_volatile_notice() -> None:
    """Unavailable knowledge should add one volatile system enrichment item."""
    existing = (EnrichmentItem(key="room", text="Room context", cache_policy="stable"),)
    enriched = append_knowledge_availability_enrichment(
        existing,
        {
            "docs": KnowledgeAvailabilityDetail(
                availability=KnowledgeAvailability.INITIALIZING,
                search_available=False,
            ),
        },
    )

    assert enriched[:-1] == existing
    assert enriched[-1].key == "knowledge_availability"
    assert enriched[-1].cache_policy == "volatile"
    assert "Knowledge base `docs` is initializing" in enriched[-1].text


def test_prepend_knowledge_availability_notice_leaves_ready_prompt_unchanged() -> None:
    """Prompts should not change when every configured knowledge base is ready."""
    assert prepend_knowledge_availability_notice("Answer the question", {}) == "Answer the question"


def test_prepend_knowledge_availability_notice_prefixes_degraded_prompt() -> None:
    """The OpenAI-compatible adapter should receive the same degraded-knowledge wording."""
    prompt = prepend_knowledge_availability_notice(
        "Answer the question",
        {
            "docs": KnowledgeAvailabilityDetail(
                availability=KnowledgeAvailability.REFRESH_FAILED,
                search_available=True,
            ),
        },
    )

    assert prompt.endswith("\n\nAnswer the question")
    assert "Knowledge base `docs` had a recent refresh failure" in prompt

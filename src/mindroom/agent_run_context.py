"""Shared agent-run context helpers used by Matrix and OpenAI-compatible adapters."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.hooks import EnrichmentItem
from mindroom.knowledge import format_knowledge_availability_notice

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from mindroom.knowledge import KnowledgeAvailabilityDetail


def append_knowledge_availability_enrichment(
    enrichment_items: Sequence[EnrichmentItem],
    unavailable_bases: Mapping[str, KnowledgeAvailabilityDetail],
) -> tuple[EnrichmentItem, ...]:
    """Append one transient knowledge-availability notice when needed."""
    notice = format_knowledge_availability_notice(unavailable_bases)
    if notice is None:
        return tuple(enrichment_items)
    return (
        *enrichment_items,
        EnrichmentItem(
            key="knowledge_availability",
            text=notice,
            cache_policy="volatile",
            persist=False,
        ),
    )


def prepend_knowledge_availability_notice(
    prompt: str,
    unavailable_bases: Mapping[str, KnowledgeAvailabilityDetail],
) -> str:
    """Prefix one user prompt with the degraded-knowledge notice when needed."""
    notice = format_knowledge_availability_notice(unavailable_bases)
    return f"{notice}\n\n{prompt}" if notice else prompt

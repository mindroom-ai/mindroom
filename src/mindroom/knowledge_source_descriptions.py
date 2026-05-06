"""Structured source metadata for model-facing knowledge search descriptions."""

from __future__ import annotations

from dataclasses import dataclass

from agno.knowledge.knowledge import Knowledge


@dataclass(frozen=True)
class KnowledgeSourceDescription:
    """Agent-visible description of one queryable knowledge source."""

    base_id: str
    description: str


@dataclass
class KnowledgeWithSourceDescriptions(Knowledge):
    """Knowledge handle carrying structured source descriptions for merged bases."""

    source_descriptions: tuple[KnowledgeSourceDescription, ...] = ()

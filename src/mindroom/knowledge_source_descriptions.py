"""Structured source metadata for model-facing knowledge search descriptions."""

from __future__ import annotations

from dataclasses import dataclass

from mindroom.strict_knowledge import StrictSearchKnowledge


@dataclass(frozen=True)
class KnowledgeSourceDescription:
    """Agent-visible description of one queryable knowledge source."""

    base_id: str
    description: str


@dataclass
class KnowledgeWithSourceDescriptions(StrictSearchKnowledge):
    """Knowledge handle carrying structured source descriptions for merged bases."""

    source_descriptions: tuple[KnowledgeSourceDescription, ...] = ()

"""Plain-data types knowledge search returns."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

#: The stored chunk metadata key, and so the hit ``meta_data`` key, naming the chunk's source file relative to its base.
SOURCE_PATH_KEY = "source_path"


@dataclass(frozen=True)
class SearchHit:
    """One stored chunk a search found, as the knowledge search tool shows it to the model."""

    content: str
    #: The source file's basename the chunk was stored under.
    name: str | None = None
    #: Every stored key except ``name`` and ``content_id``, then the hit's cosine distance as ``distances`` and
    #: ``similarity_score``.
    meta_data: dict[str, Any] = field(default_factory=dict)

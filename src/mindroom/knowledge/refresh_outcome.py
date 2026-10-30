"""The result contract between a knowledge refresh and its caller."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class RefreshOutcome:
    """What one ``KnowledgeManager.reindex_all`` call actually did: it published, or it failed and says why.

    ``error`` is already credential-redacted and is safe to persist or show.
    """

    #: Files this pass embedded, as opposed to reused from an earlier candidate.
    indexed_count: int
    #: Whether this pass swapped a complete candidate in as the published index.
    published: bool
    #: Why this refresh failed, or ``None`` when it published.
    error: str | None

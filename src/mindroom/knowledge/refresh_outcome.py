"""The result contract between a knowledge refresh and its caller."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class RefreshOutcome:
    """What one ``KnowledgeManager.reindex_all`` call actually did.

    Three outcomes are possible, so ``published`` and ``error`` are
    independent facts rather than two views of one flag:

    * **published** -- a candidate matching the source became the published
      index.
    * **no-op** -- the base builds no vectors (``mode`` is not ``semantic``),
      so there was nothing to publish and nothing failed.
    * **failed** -- the pass could not publish, and ``error`` says why.

    ``error`` is already credential-redacted and is safe to persist or show.
    """

    #: Files this pass embedded, as opposed to reused from an earlier candidate.
    indexed_count: int
    #: Whether this pass swapped a complete candidate in as the published index.
    published: bool
    #: Why this refresh failed, or ``None`` when nothing failed. ``None`` does
    #: not imply a publication -- a non-semantic base reports neither.
    error: str | None

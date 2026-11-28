"""The result contract between a knowledge refresh and its caller."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class RefreshOutcome:
    """What one knowledge refresh or candidate indexing pass actually did.

    ``error`` is already credential-redacted and is safe to persist or show.
    """

    #: Published file count on an unchanged republish, or files embedded this pass on a candidate run.
    indexed_count: int = 0
    #: Whether this pass published or republished the index.
    published: bool = False
    #: Why this refresh failed, credential-redacted, or ``None`` when it did not fail.
    error: str | None = None
    #: Whether a content-gated empty base deferred publication because its source has no files yet.
    deferred: bool = False
    #: Why other bases over the same source must refresh too (``"git_source_updated"`` or ``"source_changed"``).
    source_changed: str | None = None

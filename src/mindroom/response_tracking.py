"""Process-local identities for currently active response operations."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterator


@dataclass(frozen=True)
class ResponseIdentity:
    """Known responder and canonical requester, without message content."""

    responder: str | None = None
    requester_id: str | None = None


@dataclass
class ResponseTrackingHandle:
    """Replaceable immutable metadata for one live operation."""

    identity: ResponseIdentity


@dataclass
class ResponseActivityTracker:
    """Track live operations with synchronous, cancellation-safe cleanup.

    The owning runtime event loop must perform all mutations and reads; this
    process-local registry does not provide cross-thread synchronization.
    """

    _entries: dict[object, ResponseTrackingHandle] = field(default_factory=dict, init=False, repr=False)

    @contextmanager
    def track(
        self,
        *,
        responder: str | None = None,
        requester_id: str | None = None,
    ) -> Iterator[ResponseTrackingHandle]:
        """Own one slot until scope exit, allowing identity refinement in place."""
        token = object()
        handle = ResponseTrackingHandle(ResponseIdentity(responder, requester_id))
        self._entries[token] = handle
        try:
            yield handle
        finally:
            del self._entries[token]

    def snapshot(self) -> tuple[ResponseIdentity, ...]:
        """Return immutable metadata for each currently tracked slot."""
        return tuple(handle.identity for handle in self._entries.values())

    @property
    def count(self) -> int:
        """Return the number of active tracked slots."""
        return len(self._entries)

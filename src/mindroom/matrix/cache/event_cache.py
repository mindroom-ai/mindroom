"""Storage-agnostic Matrix event-cache contract."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from .agent_message_snapshot import AgentMessageSnapshot


@dataclass(frozen=True, slots=True)
class ThreadCacheState:
    """Durable freshness and invalidation metadata for one cached thread."""

    validated_at: float | None
    invalidated_at: float | None
    invalidation_reason: str | None
    room_invalidated_at: float | None
    room_invalidation_reason: str | None


class EventCacheBackendUnavailableError(RuntimeError):
    """Raised when cache storage is temporarily unreachable but not logically corrupt."""


@runtime_checkable
class ConversationEventCache(Protocol):
    """Principal-bound cache API for Matrix event and thread lookups.

    Every returned row belongs to ``principal_id`` and ``room_id``.
    A runtime-wide storage service must expose one bound view per authenticated Matrix
    account instead of sharing this interface directly between bots.
    """

    @property
    def principal_id(self) -> str:
        """Return the Matrix user ID that owns every row visible through this view."""

    @property
    def cache_generation(self) -> str | None:
        """Return the durable generation used to certify restart checkpoints when available."""

    @property
    def durable_writes_available(self) -> bool:
        """Return whether cache writes can durably persist data."""

    @property
    def is_initialized(self) -> bool:
        """Return whether the backing storage is currently initialized."""

    async def initialize(self) -> None:
        """Initialize any backing storage."""

    def for_principal(self, principal_id: str) -> ConversationEventCache:
        """Return a cache view that can access only ``principal_id`` rows."""

    async def close(self) -> None:
        """Close any backing storage."""

    async def get_thread_events(self, room_id: str, thread_id: str) -> list[dict[str, Any]] | None:
        """Return cached events for one thread sorted by timestamp."""

    async def get_recent_room_thread_ids(self, room_id: str, *, limit: int) -> list[str]:
        """Return locally known thread IDs for one room ordered by newest cached activity."""

    async def get_thread_cache_state(self, room_id: str, thread_id: str) -> ThreadCacheState | None:
        """Return durable freshness metadata for one cached thread."""

    async def get_event(self, room_id: str, event_id: str) -> dict[str, Any] | None:
        """Return one cached event payload by event ID."""

    async def get_recent_room_events(
        self,
        room_id: str,
        *,
        event_type: str,
        since_ts_ms: int,
        limit: int = 500,
    ) -> list[dict[str, Any]]:
        """Return recent cached room events of one type, newest first."""

    async def get_latest_edit(
        self,
        room_id: str,
        original_event_id: str,
        *,
        sender: str | None = None,
    ) -> dict[str, Any] | None:
        """Return the latest cached edit event for one original event."""

    async def get_latest_agent_message_snapshot(
        self,
        room_id: str,
        thread_id: str | None,
        sender: str,
        *,
        runtime_started_at: float | None,
    ) -> AgentMessageSnapshot | None:
        """Return the latest visible cached message from one sender in the given scope."""

    async def get_mxc_text(self, room_id: str, event_id: str, mxc_url: str) -> str | None:
        """Return MXC plaintext only while a visible owning event still references it."""

    async def store_event(self, event_id: str, room_id: str, event_data: dict[str, Any]) -> None:
        """Insert or replace one individually cached Matrix event."""

    async def store_events_batch(self, events: list[tuple[str, str, dict[str, Any]]]) -> None:
        """Insert or replace a batch of individually cached Matrix events."""

    async def store_mxc_text(self, room_id: str, event_id: str, mxc_url: str, text: str) -> bool:
        """Cache MXC plaintext only for a visible, non-tombstoned owning event."""

    async def replace_thread_if_not_newer(
        self,
        room_id: str,
        thread_id: str,
        events: list[dict[str, Any]],
        *,
        fetch_started_at: float,
        validated_at: float | None = None,
    ) -> bool:
        """Replace one cached thread snapshot only when nothing newer touched it after fetch start."""

    async def invalidate_thread(self, room_id: str, thread_id: str) -> None:
        """Delete cached events for one thread."""

    async def invalidate_room_threads(self, room_id: str) -> None:
        """Delete every cached thread snapshot for one room."""

    async def mark_thread_stale(self, room_id: str, thread_id: str, *, reason: str) -> None:
        """Persist one durable thread invalidation marker."""

    async def mark_room_threads_stale(self, room_id: str, *, reason: str) -> None:
        """Persist a durable invalidate-and-refetch marker for every cached thread in one room."""

    async def append_event(self, room_id: str, thread_id: str, event: dict[str, Any]) -> bool:
        """Append one event when the thread already has cached data."""

    async def revalidate_thread_after_incremental_update(
        self,
        room_id: str,
        thread_id: str,
    ) -> bool:
        """Refresh thread validation after a safe incremental update."""

    async def get_thread_id_for_event(self, room_id: str, event_id: str) -> str | None:
        """Return the cached thread ID for one event."""

    async def redact_event(
        self,
        room_id: str,
        event_id: str,
    ) -> bool:
        """Delete one cached event after a redaction."""

    async def purge_room(self, room_id: str) -> None:
        """Delete only this principal's cached ownership for one left or banned room."""

    def mark_room_departed(self, room_id: str) -> int:
        """Synchronously reject access, queue durable cleanup, and return the new room-fence epoch."""

    def room_departure_epoch(self, room_id: str) -> int:
        """Return the current room-fence epoch for ordering queued membership work."""

    async def mark_room_joined(
        self,
        room_id: str,
        *,
        expected_departure_epoch: int | None = None,
    ) -> None:
        """Allow cache access after an authoritative rejoin finishes any pending purge."""

    async def purge_principal(self) -> None:
        """Delete every cached row owned by this principal."""

    def disable(self, reason: str) -> None:
        """Disable the advisory cache for the rest of the runtime."""

    def runtime_diagnostics(self) -> dict[str, object]:
        """Return log-safe runtime state for sync certification diagnostics."""

    def pending_durable_write_room_ids(self) -> tuple[str, ...]:
        """Return rooms with runtime-only writes that must persist before certifying a sync token."""

    async def flush_pending_durable_writes(self, room_id: str) -> None:
        """Persist runtime-only writes for one room before certifying a sync token."""


@runtime_checkable
class SharedConversationEventCache(Protocol):
    """Runtime-owned cache storage that creates isolated principal views."""

    @property
    def durable_writes_available(self) -> bool:
        """Return whether cache writes can durably persist data."""

    @property
    def is_initialized(self) -> bool:
        """Return whether the backing storage is currently initialized."""

    async def initialize(self) -> None:
        """Initialize any backing storage."""

    async def close(self) -> None:
        """Close any backing storage."""

    def disable(self, reason: str) -> None:
        """Disable the advisory cache for the rest of the runtime."""

    def for_principal(self, principal_id: str) -> ConversationEventCache:
        """Return a cache view that can access only ``principal_id`` rows."""

"""Shared storage-maintenance values for durable Matrix event-cache backends."""

from __future__ import annotations

from dataclasses import dataclass

from mindroom.constants import (
    STREAM_STATUS_CANCELLED,
    STREAM_STATUS_COMPLETED,
    STREAM_STATUS_ERROR,
    STREAM_STATUS_INTERRUPTED,
    STREAM_STATUS_PENDING,
    STREAM_STATUS_STREAMING,
)

NONTERMINAL_STREAM_STATUSES = frozenset({STREAM_STATUS_PENDING, STREAM_STATUS_STREAMING})
TERMINAL_STREAM_STATUSES = frozenset(
    {
        STREAM_STATUS_CANCELLED,
        STREAM_STATUS_COMPLETED,
        STREAM_STATUS_ERROR,
        STREAM_STATUS_INTERRUPTED,
    },
)


class CorruptEventCachePayloadError(RuntimeError):
    """Raised when one compressed cache payload cannot be reconstructed safely."""


@dataclass(frozen=True, slots=True)
class CacheMaintenanceReport:
    """Log-safe result of one backend startup maintenance transaction."""

    schema_version: int
    migrated_from_schema_version: int | None = None
    destructive_reset: bool = False
    storage_bytes: int | None = None
    namespace_payload_bytes: int | None = None
    event_rows: int = 0
    thread_event_reference_rows: int = 0
    edit_index_rows: int = 0
    thread_index_rows: int = 0
    tombstone_rows: int = 0
    mxc_rows: int = 0
    thread_state_rows: int = 0
    room_state_rows: int = 0
    stale_thread_markers: int = 0
    stale_room_markers: int = 0
    nonterminal_streaming_edit_rows: int = 0
    terminal_streaming_edit_rows: int = 0
    compacted_streaming_edit_archive_rows: int = 0
    compacted_streaming_edit_archive_bytes: int = 0
    orphan_edit_indexes_before: int = 0
    orphan_edit_indexes_after: int = 0
    orphan_thread_indexes_before: int = 0
    orphan_thread_indexes_after: int = 0
    orphan_thread_event_references_before: int = 0
    orphan_thread_event_references_after: int = 0
    repaired_edit_indexes: int = 0
    repaired_thread_indexes: int = 0
    repaired_thread_event_references: int = 0
    compacted_nonterminal_streaming_edits: int = 0

    @property
    def startup_requires_sync_reset(self) -> bool:
        """Return whether startup discarded cache contents certified by saved sync tokens."""
        return self.destructive_reset

    def as_runtime_diagnostics(self) -> dict[str, object]:
        """Return flat structured-log fields without connection details or event content."""
        diagnostics: dict[str, object] = {
            "cache_metrics_snapshot": "startup",
            "cache_schema_version": self.schema_version,
            "cache_schema_destructive_reset": self.destructive_reset,
            "cache_event_rows": self.event_rows,
            "cache_thread_event_reference_rows": self.thread_event_reference_rows,
            "cache_edit_index_rows": self.edit_index_rows,
            "cache_thread_index_rows": self.thread_index_rows,
            "cache_tombstone_rows": self.tombstone_rows,
            "cache_mxc_rows": self.mxc_rows,
            "cache_thread_state_rows": self.thread_state_rows,
            "cache_room_state_rows": self.room_state_rows,
            "cache_stale_thread_markers": self.stale_thread_markers,
            "cache_stale_room_markers": self.stale_room_markers,
            "cache_nonterminal_streaming_edit_rows": self.nonterminal_streaming_edit_rows,
            "cache_terminal_streaming_edit_rows": self.terminal_streaming_edit_rows,
            "cache_compacted_streaming_edit_archive_rows": self.compacted_streaming_edit_archive_rows,
            "cache_compacted_streaming_edit_archive_bytes": self.compacted_streaming_edit_archive_bytes,
            "cache_orphan_edit_indexes_before": self.orphan_edit_indexes_before,
            "cache_orphan_edit_indexes_after": self.orphan_edit_indexes_after,
            "cache_orphan_thread_indexes_before": self.orphan_thread_indexes_before,
            "cache_orphan_thread_indexes_after": self.orphan_thread_indexes_after,
            "cache_orphan_thread_event_references_before": self.orphan_thread_event_references_before,
            "cache_orphan_thread_event_references_after": self.orphan_thread_event_references_after,
            "cache_repaired_edit_indexes": self.repaired_edit_indexes,
            "cache_repaired_thread_indexes": self.repaired_thread_indexes,
            "cache_repaired_thread_event_references": self.repaired_thread_event_references,
            "cache_compacted_nonterminal_streaming_edits_startup": self.compacted_nonterminal_streaming_edits,
        }
        if self.migrated_from_schema_version is not None:
            diagnostics["cache_schema_migrated_from"] = self.migrated_from_schema_version
        if self.storage_bytes is not None:
            diagnostics["cache_storage_bytes"] = self.storage_bytes
        if self.namespace_payload_bytes is not None:
            diagnostics["cache_namespace_payload_bytes"] = self.namespace_payload_bytes
        return diagnostics

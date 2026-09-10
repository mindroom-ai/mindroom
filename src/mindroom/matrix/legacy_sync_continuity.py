"""Recognize released sync-continuity payload versions during durable reads."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

_LEGACY_RECORD_VERSIONS = ("mindroom-sync-continuity-v2", "mindroom-sync-continuity-v3")

# Legacy format: v2 sync continuity with a cache-generation checkpoint and pending join fences.
# Last legacy release: v2026.8.30; replacement: v2026.8.31 wrote v3 store-generation checkpoints.
# Handling: Validate the complete record, discard the obsolete checkpoint, and preserve fences in v4.
# Coverage: tests/test_sync_continuity_store.py::test_old_checkpoint_records_upgrade_preserving_join_fences.

# Legacy format: v3 sync continuity with a store-generation checkpoint and pending join fences.
# Last legacy release: v2026.9.28; replacement: v2026.9.29 removed the checkpoint in v4.
# Handling: v2026.9.30 added conversion that validates the complete record and preserves only fences in v4.
# Coverage: tests/test_sync_continuity_store.py::test_interrupted_legacy_conversion_retries_without_losing_fences.


def normalize_legacy_sync_continuity_payload(
    payload: Mapping[str, object],
    *,
    path: Path,
    current_version: str,
) -> dict[str, object] | None:
    """Validate a v2/v3 payload and return its canonical v4 representation."""
    if payload.get("version") not in _LEGACY_RECORD_VERSIONS:
        return None
    revision = payload.get("revision")
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 0:
        raise _format_error(path, "invalid revision")
    raw_fences = payload.get("pending_join_decrypt_fences")
    if (
        not isinstance(raw_fences, list)
        or any(not isinstance(room_id, str) or not room_id for room_id in raw_fences)
        or len(set(raw_fences)) != len(raw_fences)
        or set(payload) != {"checkpoint", "pending_join_decrypt_fences", "revision", "version"}
    ):
        raise _format_error(path, "invalid join fences")
    return {
        "pending_join_decrypt_fences": raw_fences,
        "revision": revision + 1,
        "version": current_version,
    }


def _format_error(path: Path, detail: str) -> RuntimeError:
    return RuntimeError(f"Invalid Matrix sync continuity record at {path}: {detail}")

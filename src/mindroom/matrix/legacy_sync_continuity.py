"""Recognize released sync-continuity payload versions during durable reads."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

_LEGACY_RECORD_VERSIONS = ("mindroom-sync-continuity-v2", "mindroom-sync-continuity-v3")


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

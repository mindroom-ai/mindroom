"""Parse historical desktop JSON receipts for the current journal's one-time import."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from mindroom.desktop.protocol import DesktopResponse

if TYPE_CHECKING:
    from collections.abc import Sequence

# These bounds belong to the retired JSON format, independently of SQLite capacity.
_MAX_ENTRIES = 1024
_MAX_SESSIONS = 128

# LEGACY_COMPAT: Desktop JSON v1 command receipts without executable command bodies.
# Legacy format: command_journal.json with v=1, entries, and sequence_high_watermarks.
# Last legacy release: v2026.9.147; replacement: v2026.9.148 introduced the SQLite inbox/outbox.
# Handling: Validate bounded receipts and sequence identities before the owner imports them once.
# The owner keeps permissions and its transaction, preserves sequence maxima, never reconstructs
# executable bodies, and marks saved responses delivered until explicitly replayed.
# Coverage: tests/test_desktop_command_journal.py::test_legacy_receipts_import_without_repeating_started_control;
# tests/test_desktop_command_journal.py::test_legacy_completed_receipts_wait_for_explicit_replay;
# tests/test_desktop_command_journal.py::test_full_legacy_started_cache_does_not_block_new_admission;
# tests/test_desktop_command_journal.py::test_malformed_legacy_import_preserves_existing_work_and_can_retry.


def parse_legacy_records(
    payload: object,
) -> tuple[list[tuple[str, str, DesktopResponse | None]], list[tuple[str, int]]]:
    """Validate the JSON v1 journal without admitting or publishing any work."""
    record = _legacy_mapping(payload)
    if record.get("v") != 1:
        msg = "Unsupported legacy journal."
        raise ValueError(msg)
    raw_entries, raw_sequences = record.get("entries"), record.get("sequence_high_watermarks")
    if not isinstance(raw_entries, list) or not isinstance(raw_sequences, list):
        msg = "Malformed legacy records."
        raise TypeError(msg)
    if len(raw_entries) > _MAX_ENTRIES or len(raw_sequences) > _MAX_SESSIONS:
        msg = "Legacy journal exceeds bounds."
        raise ValueError(msg)
    return _legacy_entries(raw_entries), _legacy_sequences(raw_sequences)


def _legacy_entries(raw_entries: Sequence[object]) -> list[tuple[str, str, DesktopResponse | None]]:
    entries: list[tuple[str, str, DesktopResponse | None]] = []
    request_ids: set[str] = set()
    for raw in raw_entries:
        entry = _legacy_mapping(raw)
        request_id = _legacy_identifier(entry.get("request_id"))
        fingerprint = entry.get("command_fingerprint")
        if request_id in request_ids or not isinstance(fingerprint, str) or len(fingerprint) != 64:
            msg = "Malformed legacy identity."
            raise ValueError(msg)
        if any(character not in "0123456789abcdef" for character in fingerprint):
            msg = "Malformed legacy fingerprint."
            raise ValueError(msg)
        response = DesktopResponse.from_content(entry["response"]) if entry.get("response") is not None else None
        if response is not None and response.request_id != request_id:
            msg = "Legacy response identifies a different request."
            raise ValueError(msg)
        entries.append((request_id, fingerprint, response))
        request_ids.add(request_id)
    return entries


def _legacy_sequences(raw_sequences: Sequence[object]) -> list[tuple[str, int]]:
    sequences: list[tuple[str, int]] = []
    session_ids: set[str] = set()
    for raw in raw_sequences:
        entry = _legacy_mapping(raw)
        session_id = _legacy_identifier(entry.get("session_id"))
        sequence = entry.get("sequence")
        if type(sequence) is not int or sequence < 0 or session_id in session_ids:
            msg = "Malformed legacy sequence."
            raise ValueError(msg)
        sequences.append((session_id, sequence))
        session_ids.add(session_id)
    return sequences


def _legacy_mapping(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        msg = "Malformed legacy record."
        raise TypeError(msg)
    return cast("dict[str, object]", value)


def _legacy_identifier(value: object) -> str:
    if not isinstance(value, str) or not value or len(value) > 128:
        msg = "Malformed legacy identifier."
        raise ValueError(msg)
    return value

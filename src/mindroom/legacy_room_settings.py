"""Room overrides an older runtime stored in files, each of which moves into its room's settings when first read."""

# LEGACY_COMPAT: Per-room overrides kept in tracking files instead of room state.
# Legacy format: `tracking/room_reply_modes.json`, `tracking/room_thread_modes.json`, and `tracking/room_model_overrides.json`, each a JSON object from room ID to a record whose `mode` or `model` string is the room's override.
# Last legacy release: v2026.11.409 for `room_reply_modes.json`, replaced in v2026.11.410 by the `reply_mode` room setting; v2026.11.511 for the other two, replaced in the next release, unreleased when this was written, by the `thread_mode` and `model` room settings.
# Handling: `mindroom.room_settings` applies a record while the runtime has not read its room's setting, writes it into a room that has no such state event, and removes it once that write or a read of an existing event confirms the room's state; a room that refuses the write keeps the record applying, and the files are never written otherwise.
# Coverage: tests/test_room_reply_modes.py::test_an_override_an_older_runtime_stored_moves_into_the_room,
# tests/test_room_settings.py::test_an_older_runtimes_override_applies_until_it_moves_into_the_room,
# tests/test_room_settings.py::test_a_setting_the_room_already_has_supersedes_an_older_runtimes_override.

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.constants import tracking_dir
from mindroom.durable_write import load_cached_override_records, write_override_records

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.constants import RuntimePaths
    from mindroom.durable_write import OverrideRecord


def _records(runtime_paths: RuntimePaths, filename: str, field: str) -> tuple[Path, dict[str, OverrideRecord]]:
    def is_record(_room_id: str, record: dict[object, object]) -> bool:
        return isinstance(record.get(field), str)

    path = tracking_dir(runtime_paths) / filename
    return path, load_cached_override_records(path, is_record)


def legacy_room_override(runtime_paths: RuntimePaths, room_id: str, filename: str, field: str) -> str | None:
    """Return the override ``filename`` holds for a room in its records' ``field``, None without one."""
    record = _records(runtime_paths, filename, field)[1].get(room_id)
    return None if record is None else record[field]


def drop_legacy_room_override(runtime_paths: RuntimePaths, room_id: str, filename: str, field: str) -> None:
    """Remove a room's override from ``filename``, leaving every other room's."""
    # Read the file now, not before the room's state was written: another room's move may have rewritten it meanwhile.
    path, records = _records(runtime_paths, filename, field)
    if records.pop(room_id, None) is not None:
        write_override_records(path, records)

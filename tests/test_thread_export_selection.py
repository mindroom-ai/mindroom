"""Tests for thread-export room selection."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.thread_export.selection import export_rooms
from tests.conftest import runtime_paths_for
from tests.thread_export_helpers import thread_export_config, write_thread_export_matrix_state

if TYPE_CHECKING:
    from pathlib import Path


def test_export_rooms_filters_by_room_metadata_substring(tmp_path: Path) -> None:
    """Room filtering should match substrings across user-facing room fields."""
    config = thread_export_config(tmp_path)
    runtime_paths = runtime_paths_for(config)
    write_thread_export_matrix_state(tmp_path)

    assert [room.key for room in export_rooms(runtime_paths, "obb")] == ["lobby"]
    assert {room.key for room in export_rooms(runtime_paths, "LOCALHOST")} == {"lobby", "dev"}

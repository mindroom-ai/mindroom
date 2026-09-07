"""Tests for thread-export room selection."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.thread_export.models import ThreadExportGroup, ThreadExportRoom
from mindroom.thread_export.selection import build_export_groups, export_rooms
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


def test_export_groups_use_runtime_entities_without_reading_credentials() -> None:
    """Configured rooms use the router; invited rooms use their own entity."""
    configured = ThreadExportRoom("lobby", "!lobby:localhost", "", "Lobby")
    invited = ThreadExportRoom("!invited:localhost", "!invited:localhost", "", "", invited=True)
    assert build_export_groups(state_rooms=[configured], invited_groups=[("general", [invited])]) == [
        ThreadExportGroup(rooms=(configured,), entity_name="router"),
        ThreadExportGroup(rooms=(invited,), entity_name="general"),
    ]

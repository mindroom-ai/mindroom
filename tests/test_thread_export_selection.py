"""Tests for thread-export room selection."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.matrix.users import INTERNAL_USER_ACCOUNT_KEY
from mindroom.thread_export.models import ThreadExportGroup, ThreadExportGroupFailure, ThreadExportRoom
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


def test_build_export_groups_separates_ready_and_failed_account_states(tmp_path: Path) -> None:
    """Ready groups always own a user, while missing accounts produce failure groups."""
    config = thread_export_config(tmp_path)
    runtime_paths = runtime_paths_for(config)
    write_thread_export_matrix_state(tmp_path, account_keys=(INTERNAL_USER_ACCOUNT_KEY,))
    invited_room = ThreadExportRoom(
        key="!invited:localhost",
        room_id="!invited:localhost",
        alias=None,
        name=None,
        invited=True,
    )

    groups = build_export_groups(
        runtime_paths=runtime_paths,
        homeserver="http://localhost:8008",
        state_rooms=export_rooms(runtime_paths, None),
        invited_groups=[("general", [invited_room])],
    )

    assert isinstance(groups[0], ThreadExportGroup)
    assert groups[0].user.agent_name == "user"
    assert isinstance(groups[1], ThreadExportGroupFailure)
    assert groups[1].rooms == (invited_room,)

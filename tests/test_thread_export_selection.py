"""Tests for thread-export room selection."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.matrix.users import INTERNAL_USER_ACCOUNT_KEY
from mindroom.thread_export.models import ThreadExportGroup, ThreadExportGroupFailure, ThreadExportRoom
from mindroom.thread_export.selection import build_export_groups, export_rooms, select_export_account
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


def test_configured_rooms_export_with_the_router_rather_than_the_internal_user(tmp_path: Path) -> None:
    """Export reads the projection of whoever it logs in as, so it logs in as a bot.

    The internal user account exists but runs no bot, so nothing keeps a
    projection warm for it: choosing it would re-hydrate every thread from the
    homeserver on every pass. The router is the managed entity that joins every
    configured room, which is what the rooms in ``matrix_state.yaml`` are.
    """
    config = thread_export_config(tmp_path)
    runtime_paths = runtime_paths_for(config)
    write_thread_export_matrix_state(
        tmp_path,
        account_keys=(INTERNAL_USER_ACCOUNT_KEY, "agent_router", "agent_general"),
    )

    account = select_export_account(runtime_paths, "http://localhost:8008")

    assert account.agent_name == "router"


def test_an_install_with_only_the_internal_user_still_exports(tmp_path: Path) -> None:
    """Last preference, not a prohibition: a correct slow export beats no export."""
    config = thread_export_config(tmp_path)
    runtime_paths = runtime_paths_for(config)
    write_thread_export_matrix_state(tmp_path, account_keys=(INTERNAL_USER_ACCOUNT_KEY,))

    account = select_export_account(runtime_paths, "http://localhost:8008")

    assert account.agent_name == "user"

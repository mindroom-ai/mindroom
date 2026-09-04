"""Tests for thread-export room selection."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from mindroom.config.agent import AgentConfig
from mindroom.matrix.users import INTERNAL_USER_ACCOUNT_KEY
from mindroom.thread_export.models import ThreadExportGroup, ThreadExportGroupFailure, ThreadExportRoom
from mindroom.thread_export.selection import (
    build_export_groups,
    export_rooms,
    invited_export_rooms,
    select_export_account,
)
from tests.conftest import runtime_paths_for
from tests.thread_export_helpers import (
    thread_export_config,
    write_invited_rooms,
    write_thread_export_matrix_state,
)

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path


def test_export_rooms_filters_by_room_metadata_substring(tmp_path: Path) -> None:
    """Room filtering should match substrings across user-facing room fields."""
    config = thread_export_config(tmp_path)
    runtime_paths = runtime_paths_for(config)
    write_thread_export_matrix_state(tmp_path)

    assert [room.key for room in export_rooms(config, runtime_paths, "obb")] == ["lobby"]
    assert {room.key for room in export_rooms(config, runtime_paths, "LOCALHOST")} == {"lobby", "dev"}


def test_export_rooms_attributes_configured_room_owners(tmp_path: Path) -> None:
    """A configured room carries agent ownership independently of current Matrix membership."""
    config = thread_export_config(tmp_path)
    config.agents["general"].rooms = ["lobby"]
    config.agents["other"] = AgentConfig(display_name="Other Agent", rooms=["dev"])
    runtime_paths = runtime_paths_for(config)
    write_thread_export_matrix_state(tmp_path)

    rooms = export_rooms(config, runtime_paths, None)

    assert {room.key: room.source_entity_names for room in rooms} == {
        "lobby": ("general",),
        "dev": ("other",),
    }


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
        state_rooms=export_rooms(config, runtime_paths, None),
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


def test_invited_room_selection_rejects_multiple_persisted_owners(tmp_path: Path) -> None:
    """Config iteration order must never choose between conflicting invited-room claimants."""
    config = thread_export_config(tmp_path)
    config.agents["other"] = AgentConfig(display_name="Other Agent")
    runtime_paths = runtime_paths_for(config)
    room_id = "!private:localhost"
    write_invited_rooms(runtime_paths, "general", [room_id])
    write_invited_rooms(runtime_paths, "other", [room_id])

    selection = invited_export_rooms(
        config,
        runtime_paths,
        None,
        known_room_ids=set(),
    )

    assert selection.groups == ()
    assert len(selection.conflicts) == 1
    assert selection.conflicts[0].room.room_id == room_id
    assert selection.conflicts[0].room.source_entity_names == ("general", "other")


def test_explicit_room_owner_resolves_ambiguous_invited_room_without_matrix_state(tmp_path: Path) -> None:
    """A raw room-ID assignment must resolve legacy overlap before Matrix state discovers it."""
    room_id = "!private:localhost"
    config = thread_export_config(tmp_path)
    config.agents["general"].rooms = [room_id]
    config.agents["other"] = AgentConfig(display_name="Other Agent")
    runtime_paths = runtime_paths_for(config)
    write_invited_rooms(runtime_paths, "general", [room_id])
    write_invited_rooms(runtime_paths, "other", [room_id])

    selection = invited_export_rooms(
        config,
        runtime_paths,
        None,
        known_room_ids=set(),
    )

    assert selection.conflicts == ()
    assert len(selection.groups) == 1
    owner_name, rooms = selection.groups[0]
    assert owner_name == "general"
    assert rooms[0].room_id == room_id
    assert rooms[0].source_entity_names == ("general",)


def test_shared_explicit_room_keeps_every_source_without_matrix_state(tmp_path: Path) -> None:
    """A shared raw room-ID assignment is explicit authority, not invite ambiguity."""
    room_id = "!shared:localhost"
    config = thread_export_config(tmp_path)
    config.agents["general"].rooms = [room_id]
    config.agents["other"] = AgentConfig(display_name="Other Agent", rooms=[room_id])
    runtime_paths = runtime_paths_for(config)
    write_invited_rooms(runtime_paths, "general", [room_id])
    write_invited_rooms(runtime_paths, "other", [room_id])

    selection = invited_export_rooms(
        config,
        runtime_paths,
        None,
        known_room_ids=set(),
    )

    assert selection.conflicts == ()
    assert len(selection.groups) == 1
    _reader_name, rooms = selection.groups[0]
    assert rooms[0].source_entity_names == ("general", "other")


def test_retired_entity_invite_claim_keeps_current_claim_ambiguous(tmp_path: Path) -> None:
    """Removing an entity from config cannot hide its durable claim and expose its room."""
    room_id = "!private:localhost"
    config = thread_export_config(tmp_path)
    runtime_paths = runtime_paths_for(config)
    write_invited_rooms(runtime_paths, "general", [room_id])
    write_invited_rooms(runtime_paths, "retired_paul", [room_id])

    selection = invited_export_rooms(
        config,
        runtime_paths,
        None,
        known_room_ids=set(),
    )

    assert selection.groups == ()
    assert len(selection.conflicts) == 1
    assert selection.conflicts[0].room.room_id == room_id


@pytest.mark.parametrize("claimant_kind", ["current", "retired"])
@pytest.mark.parametrize("symlink_kind", ["state-root", "claim-file"])
def test_symlinked_invite_claim_fails_closed(
    tmp_path: Path,
    claimant_kind: str,
    symlink_kind: str,
) -> None:
    """A symlink cannot hide current or retired ownership evidence."""
    room_id = "!private:localhost"
    config = thread_export_config(tmp_path)
    runtime_paths = runtime_paths_for(config)
    if claimant_kind == "retired":
        write_invited_rooms(runtime_paths, "general", [room_id])
    claim_root = runtime_paths.storage_root / "agents" / ("general" if claimant_kind == "current" else "retired_paul")
    claim_root.parent.mkdir(parents=True, exist_ok=True)
    backing_root = runtime_paths.storage_root / "retired-paul-backing"
    backing_root.mkdir()
    backing_claim = backing_root / "invited_rooms.json"
    backing_claim.write_text(f'["{room_id}"]\n', encoding="utf-8")
    if symlink_kind == "state-root":
        claim_root.symlink_to(backing_root, target_is_directory=True)
    else:
        claim_root.mkdir()
        (claim_root / "invited_rooms.json").symlink_to(backing_claim)

    with pytest.raises(RuntimeError, match="Unsafe invited-room claim"):
        invited_export_rooms(
            config,
            runtime_paths,
            None,
            known_room_ids=set(),
        )


def test_broken_invited_room_state_root_symlink_fails_closed(tmp_path: Path) -> None:
    """A broken state-root symlink is unsafe evidence, not an absent directory."""
    config = thread_export_config(tmp_path)
    runtime_paths = runtime_paths_for(config)
    (runtime_paths.storage_root / "agents").symlink_to(
        runtime_paths.storage_root / "missing-agents",
        target_is_directory=True,
    )

    with pytest.raises(RuntimeError, match="Unsafe invited-room state root"):
        invited_export_rooms(
            config,
            runtime_paths,
            None,
            known_room_ids=set(),
        )


@pytest.mark.parametrize("claimant_kind", ["current", "retired"])
def test_invalid_invited_room_claim_fails_closed(tmp_path: Path, claimant_kind: str) -> None:
    """Unreadable ownership evidence cannot be treated as an empty claim."""
    config = thread_export_config(tmp_path)
    runtime_paths = runtime_paths_for(config)
    claim_path = (
        runtime_paths.storage_root
        / "agents"
        / ("general" if claimant_kind == "current" else "retired_paul")
        / "invited_rooms.json"
    )
    claim_path.parent.mkdir(parents=True)
    claim_path.write_text("not-json\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match="Invalid invited-room claim file"):
        invited_export_rooms(
            config,
            runtime_paths,
            None,
            known_room_ids=set(),
        )


def test_unreadable_invited_room_claim_fails_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A claim read error cannot be collapsed into missing ownership evidence."""
    config = thread_export_config(tmp_path)
    runtime_paths = runtime_paths_for(config)
    claim_path = runtime_paths.storage_root / "agents" / "general" / "invited_rooms.json"
    claim_path.parent.mkdir(parents=True)
    path_type = type(claim_path)
    original_read_text = path_type.read_text

    def read_text(path: Path, *args: object, **kwargs: object) -> str:
        if path == claim_path:
            msg = "denied"
            raise PermissionError(msg)
        return original_read_text(path, *args, **kwargs)

    monkeypatch.setattr(path_type, "read_text", read_text)

    with pytest.raises(RuntimeError, match="Invalid invited-room claim file"):
        invited_export_rooms(
            config,
            runtime_paths,
            None,
            known_room_ids=set(),
        )


def test_unreadable_invited_room_state_root_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A state-root enumeration error cannot hide every persisted claimant."""
    config = thread_export_config(tmp_path)
    runtime_paths = runtime_paths_for(config)
    agents_root = runtime_paths.storage_root / "agents"
    agents_root.mkdir(parents=True)
    path_type = type(agents_root)
    original_iterdir = path_type.iterdir

    def iterdir(path: Path) -> Iterator[Path]:
        if path == agents_root:
            msg = "denied"
            raise PermissionError(msg)
        return original_iterdir(path)

    monkeypatch.setattr(path_type, "iterdir", iterdir)

    with pytest.raises(RuntimeError, match="Unsafe invited-room state root"):
        invited_export_rooms(
            config,
            runtime_paths,
            None,
            known_room_ids=set(),
        )

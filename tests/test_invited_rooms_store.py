"""Tests for the unified invited-room lifecycle ledger."""

import json
from pathlib import Path

import pytest

from mindroom.matrix.invited_rooms_store import (
    PendingRoomInvite,
    PendingRoomInvitePhase,
    RoomInviteState,
    load_invited_rooms,
    load_room_invite_states,
    save_room_invite_states,
)


def test_room_invite_states_round_trip_and_project_accepted_rooms(tmp_path: Path) -> None:
    """The ledger retains pending work while public reads expose accepted rooms only."""
    path = tmp_path / "invited_rooms.json"
    states = {
        "!accepted:localhost": RoomInviteState(accepted=True),
        "!observed:localhost": RoomInviteState(
            pending=PendingRoomInvite(
                inviter_id="@observed:localhost",
                phase=PendingRoomInvitePhase.OBSERVED,
            ),
        ),
        "!authorized:localhost": RoomInviteState(
            pending=PendingRoomInvite(
                inviter_id="@authorized:localhost",
                phase=PendingRoomInvitePhase.AUTHORIZED,
            ),
        ),
        "!accepted-pending:localhost": RoomInviteState(
            accepted=True,
            pending=PendingRoomInvite(
                inviter_id="@accepted:localhost",
                phase=PendingRoomInvitePhase.AUTHORIZED,
            ),
        ),
    }

    assert save_room_invite_states(path, states)

    assert load_room_invite_states(path) == states
    assert load_invited_rooms(path) == {
        "!accepted:localhost",
        "!accepted-pending:localhost",
    }


def test_failed_room_invite_state_save_preserves_complete_prior_record(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A failed replacement cannot expose a partial lifecycle transition."""
    path = tmp_path / "invited_rooms.json"
    prior = {
        "!room:localhost": RoomInviteState(
            pending=PendingRoomInvite(
                inviter_id="@member:localhost",
                phase=PendingRoomInvitePhase.AUTHORIZED,
            ),
        ),
    }
    replacement = {
        "!room:localhost": RoomInviteState(
            accepted=True,
            pending=prior["!room:localhost"].pending,
        ),
    }
    assert save_room_invite_states(path, prior)

    def fail_replace(_source: Path, _destination: Path) -> None:
        error_message = "disk unavailable"
        raise OSError(error_message)

    monkeypatch.setattr("mindroom.matrix.invited_rooms_store.safe_replace", fail_replace)

    assert not save_room_invite_states(path, replacement)
    assert load_room_invite_states(path) == prior


@pytest.mark.parametrize(
    ("accepted_rooms", "pending_invites", "expected"),
    [
        (
            ["!accepted:localhost"],
            {},
            {"!accepted:localhost": RoomInviteState(accepted=True)},
        ),
        (
            [],
            {"!pending:localhost": "@member:localhost"},
            {
                "!pending:localhost": RoomInviteState(
                    pending=PendingRoomInvite(
                        inviter_id="@member:localhost",
                        phase=PendingRoomInvitePhase.OBSERVED,
                    ),
                ),
            },
        ),
        (
            ["!room:localhost"],
            {"!room:localhost": "@member:localhost"},
            {
                "!room:localhost": RoomInviteState(
                    accepted=True,
                    pending=PendingRoomInvite(
                        inviter_id="@member:localhost",
                        phase=PendingRoomInvitePhase.OBSERVED,
                    ),
                ),
            },
        ),
    ],
)
def test_legacy_invite_files_migrate_to_combined_ledger(
    tmp_path: Path,
    accepted_rooms: list[str],
    pending_invites: dict[str, str],
    expected: dict[str, RoomInviteState],
) -> None:
    """Upgrade preserves accepted rooms while pending evidence remains untrusted."""
    path = tmp_path / "invited_rooms.json"
    pending_path = tmp_path / "pending_room_invites.json"
    if accepted_rooms:
        path.write_text(json.dumps(accepted_rooms), encoding="utf-8")
    if pending_invites:
        pending_path.write_text(json.dumps(pending_invites), encoding="utf-8")

    assert load_room_invite_states(path) == expected
    assert json.loads(path.read_text(encoding="utf-8")) == {
        room_id: {
            "accepted": state.accepted,
            "pending": (
                {
                    "inviter_id": state.pending.inviter_id,
                    "phase": PendingRoomInvitePhase.OBSERVED.value,
                }
                if state.pending is not None
                else None
            ),
        }
        for room_id, state in expected.items()
    }


def test_failed_legacy_migration_keeps_recoverable_state(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A failed format replacement still exposes all legacy lifecycle state."""
    path = tmp_path / "invited_rooms.json"
    pending_path = tmp_path / "pending_room_invites.json"
    path.write_text('["!accepted:localhost"]', encoding="utf-8")
    pending_path.write_text(
        '{"!pending:localhost": "@member:localhost"}',
        encoding="utf-8",
    )

    def fail_replace(_source: Path, _destination: Path) -> None:
        error_message = "disk unavailable"
        raise OSError(error_message)

    monkeypatch.setattr("mindroom.matrix.invited_rooms_store.safe_replace", fail_replace)

    assert load_room_invite_states(path) == {
        "!accepted:localhost": RoomInviteState(accepted=True),
        "!pending:localhost": RoomInviteState(
            pending=PendingRoomInvite(
                inviter_id="@member:localhost",
                phase=PendingRoomInvitePhase.OBSERVED,
            ),
        ),
    }
    assert json.loads(path.read_text(encoding="utf-8")) == ["!accepted:localhost"]


def test_unreadable_legacy_pending_file_defers_format_replacement(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A transient pending-file read failure must not make its records unreachable."""
    path = tmp_path / "invited_rooms.json"
    pending_path = tmp_path / "pending_room_invites.json"
    path.write_text('["!accepted:localhost"]', encoding="utf-8")
    pending_path.write_text(
        '{"!pending:localhost": "@member:localhost"}',
        encoding="utf-8",
    )
    original_read_text = Path.read_text

    def fail_pending_read(read_path: Path, *args: object, **kwargs: object) -> str:
        if read_path == pending_path:
            error_message = "disk unavailable"
            raise OSError(error_message)
        return original_read_text(read_path, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "read_text", fail_pending_read)

    assert load_room_invite_states(path) == {"!accepted:localhost": RoomInviteState(accepted=True)}
    assert json.loads(original_read_text(path, encoding="utf-8")) == ["!accepted:localhost"]

    monkeypatch.setattr(Path, "read_text", original_read_text)
    states = load_room_invite_states(path)
    assert states["!pending:localhost"].pending == PendingRoomInvite(
        inviter_id="@member:localhost",
        phase=PendingRoomInvitePhase.OBSERVED,
    )

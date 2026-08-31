"""Tests for the unified invited-room lifecycle ledger."""

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

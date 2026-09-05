"""Tests for router reply-membership sync coordination."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import MagicMock
from uuid import UUID

import nio
import pytest

from mindroom.agent_reply_membership import AgentReplyMembershipIndex
from mindroom.agent_reply_membership_sync import (
    AgentReplyMembershipSync,
    ReplyMembershipPreAdmission,
)
from mindroom.config.main import Config
from mindroom.event_journal import (
    DepartureSource,
    IngestionBatchAdmission,
    IngestionRecordDisposition,
)
from tests.conftest import test_runtime_paths

if TYPE_CHECKING:
    from pathlib import Path


def _ingestion_admission(
    disposition: IngestionRecordDisposition,
    *,
    source: DepartureSource | None = None,
    room_id: str | None = None,
    previous_membership: str | None = None,
    membership: str | None = None,
) -> IngestionBatchAdmission:
    return IngestionBatchAdmission(
        schema_version=1,
        consumer_generation=UUID(int=1),
        stream_id=UUID(int=2),
        sequence=0,
        sha256=b"0" * 32,
        record_id="record",
        disposition=disposition,
        source=source,
        room_id=room_id,
        previous_membership=previous_membership,
        membership=membership,
        previous_membership_epoch=0 if room_id is not None else None,
        membership_epoch=1 if room_id is not None else None,
        event=None,
        projected=None,
    )


def test_history_loss_invalidates_all_reply_membership_grants(tmp_path: Path) -> None:
    """A history-loss carrier requests global fail-closed invalidation."""
    memberships = MagicMock(spec=AgentReplyMembershipIndex)
    membership_sync = AgentReplyMembershipSync(memberships)
    config = Config()

    effects = membership_sync.pre_admit_ingestion(
        config,
        test_runtime_paths(tmp_path),
        _ingestion_admission(
            IngestionRecordDisposition.HISTORY_LOSS,
            room_id="!uncertain:localhost",
        ),
    )

    assert effects == ReplyMembershipPreAdmission(invalidate_reason="uncertain_sync_response")
    memberships.invalidate.assert_not_called()
    memberships.mark_control_room_unready.assert_not_called()


def test_reported_control_departure_fences_room_before_admission(tmp_path: Path) -> None:
    """A reported control departure revokes that room before admission."""
    memberships = MagicMock(spec=AgentReplyMembershipIndex)
    memberships.mark_control_room_unready.return_value = True
    membership_sync = AgentReplyMembershipSync(memberships)
    config = Config()
    runtime_paths = test_runtime_paths(tmp_path)

    effects = membership_sync.pre_admit_ingestion(
        config,
        runtime_paths,
        _ingestion_admission(
            IngestionRecordDisposition.ROOM_LIFECYCLE,
            source=DepartureSource.REPORTED,
            room_id="!departed:localhost",
            previous_membership="join",
            membership="leave",
        ),
    )

    assert effects == ReplyMembershipPreAdmission(authorization_changed=True)
    memberships.mark_control_room_unready.assert_called_once_with(
        config,
        runtime_paths,
        "!departed:localhost",
        reason="control_client_departed",
    )


@pytest.mark.parametrize(
    "admission",
    [
        _ingestion_admission(IngestionRecordDisposition.COMPATIBILITY_ONLY),
        _ingestion_admission(
            IngestionRecordDisposition.ROOM_LIFECYCLE,
            source=DepartureSource.LOCAL,
            room_id="!local:localhost",
            previous_membership="join",
            membership="leave",
        ),
        _ingestion_admission(
            IngestionRecordDisposition.ROOM_LIFECYCLE,
            source=DepartureSource.REPORTED,
            room_id="!joined:localhost",
            previous_membership="leave",
            membership="join",
        ),
    ],
)
def test_non_reported_departure_carriers_do_not_change_reply_memberships(
    tmp_path: Path,
    admission: IngestionBatchAdmission,
) -> None:
    """Non-departure carriers leave reply authorization unchanged."""
    memberships = MagicMock(spec=AgentReplyMembershipIndex)
    membership_sync = AgentReplyMembershipSync(memberships)

    effects = membership_sync.pre_admit_ingestion(
        Config(),
        test_runtime_paths(tmp_path),
        admission,
    )

    assert effects == ReplyMembershipPreAdmission()
    memberships.invalidate.assert_not_called()
    memberships.mark_control_room_unready.assert_not_called()


@pytest.mark.asyncio
async def test_live_transition_retries_unfinished_effects_after_event_replay(tmp_path: Path) -> None:
    """A no-op replay must retry effects left pending by an earlier failure."""
    memberships = MagicMock(spec=AgentReplyMembershipIndex)
    memberships.apply_member_event.side_effect = [True, False, False]
    membership_sync = AgentReplyMembershipSync(memberships)
    event = nio.RoomMemberEvent.from_dict(
        {
            "type": "m.room.member",
            "event_id": "$grant",
            "sender": "@alice:localhost",
            "state_key": "@alice:localhost",
            "origin_server_ts": 1,
            "content": {"membership": "join"},
        },
    )
    assert isinstance(event, nio.RoomMemberEvent)
    effect_attempts = 0

    async def reconcile_effects() -> None:
        nonlocal effect_attempts
        effect_attempts += 1
        if effect_attempts == 1:
            msg = "effect failed"
            raise RuntimeError(msg)

    async def apply_transition() -> None:
        await membership_sync.apply_live_transition(
            Config(),
            test_runtime_paths(tmp_path),
            "!grant:localhost",
            event,
            control_user_id="@router:localhost",
            reconcile_effects=reconcile_effects,
        )

    with pytest.raises(RuntimeError, match="effect failed"):
        await apply_transition()
    await apply_transition()
    await apply_transition()

    assert effect_attempts == 2

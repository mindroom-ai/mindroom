"""Tests for router reply-membership sync coordination."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import MagicMock

import nio
import pytest

from mindroom.agent_reply_membership import AgentReplyMembershipIndex
from mindroom.agent_reply_membership_sync import AgentReplyMembershipSync
from mindroom.config.main import Config
from tests.conftest import test_runtime_paths

if TYPE_CHECKING:
    from pathlib import Path


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

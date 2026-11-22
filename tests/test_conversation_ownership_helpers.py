"""Seam tests for extracted conversation-ownership helpers used by the reply path."""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import nio
import pytest

from mindroom.authorization import ReplyMembershipPendingError
from mindroom.config.access import ResponderAccessConfig
from mindroom.config.agent import AgentConfig, TeamConfig
from mindroom.matrix.state import MatrixState
from tests.access_schema_support import refresh_memberships
from tests.conftest import unwrap_extracted_collaborator
from tests.matrix_state_helpers import save_matrix_state
from tests.response_runner_helpers import _bot, _plain_request, _target

if TYPE_CHECKING:
    from pathlib import Path


@pytest.mark.usefixtures("enforce_turn_authorization")
@pytest.mark.asyncio
async def test_requester_remains_authorized_checks_resolved_membership_and_pending_raises(tmp_path: Path) -> None:
    """Post-lock re-authorization distinguishes allowed, revoked, and pending membership."""
    bot = _bot(tmp_path)
    runner = unwrap_extracted_collaborator(bot._response_runner)
    bot.config.agents["general"].access = ResponderAccessConfig(members_of_rooms=["grant"])
    state = MatrixState.load(runtime_paths=bot.runtime_paths)
    state.add_room("grant", "!grant:localhost", "#grant:localhost", "Grant")
    save_matrix_state(state, bot.runtime_paths)
    request = _plain_request(_target())

    with pytest.raises(ReplyMembershipPendingError):
        runner.locked.requester_remains_authorized("@user:localhost", request.room_id)
    with pytest.raises(ReplyMembershipPendingError):
        await runner.locked.request_remains_authorized(request)

    membership_client = AsyncMock(spec=nio.AsyncClient)
    membership_client.joined_rooms.return_value = nio.JoinedRoomsResponse(rooms=["!grant:localhost"])
    membership_client.joined_members.return_value = nio.JoinedMembersResponse(
        members=[nio.RoomMember("@user:localhost", None, None)],
        room_id="!grant:localhost",
    )
    await refresh_memberships(
        runner.deps.runtime.agent_reply_memberships,
        bot.config,
        bot.runtime_paths,
        membership_client,
    )

    assert runner.locked.requester_remains_authorized("@user:localhost", request.room_id) is True
    assert await runner.locked.request_remains_authorized(request) is True

    membership_client.joined_members.return_value = nio.JoinedMembersResponse(
        members=[],
        room_id="!grant:localhost",
    )
    await refresh_memberships(
        runner.deps.runtime.agent_reply_memberships,
        bot.config,
        bot.runtime_paths,
        membership_client,
    )
    assert runner.locked.requester_remains_authorized("@user:localhost", request.room_id) is False
    assert await runner.locked.request_remains_authorized(request) is False


@pytest.mark.usefixtures("enforce_turn_authorization")
@pytest.mark.asyncio
async def test_requester_remains_authorized_uses_configured_team_policy(tmp_path: Path) -> None:
    """A configured team bot evaluates the team policy rather than requiring each member grant."""
    bot = _bot(tmp_path)
    bot.config.agents["member"] = AgentConfig(
        display_name="Member",
        access=ResponderAccessConfig(users=["@only-member:localhost"]),
    )
    bot.config.teams["crew"] = TeamConfig(
        display_name="Crew",
        role="Coordinate",
        agents=["member"],
        access=ResponderAccessConfig(users=["@alice:localhost"]),
    )
    runner = unwrap_extracted_collaborator(bot._response_runner)
    runner.locked.deps = replace(runner.deps, agent_name="crew")

    assert (
        runner.locked.requester_remains_authorized(
            "@alice:localhost",
            "!room:localhost",
            reply_entity_names=("member",),
        )
        is True
    )
    assert (
        runner.locked.requester_remains_authorized(
            "@only-member:localhost",
            "!room:localhost",
            reply_entity_names=("member",),
        )
        is False
    )

"""Authorization invariants for long-lived tool runtime contexts."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import nio
import pytest

from mindroom.agent_reply_membership import AgentReplyMembershipIndex
from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.custom_tools.attachment_helpers import room_access_allowed
from mindroom.message_target import MessageTarget
from mindroom.tool_system.runtime_context import ToolRuntimeContext
from tests.conftest import bind_runtime_paths, make_conversation_reader_mock, make_relation_lookup, test_runtime_paths

if TYPE_CHECKING:
    from pathlib import Path


def _config(tmp_path: Path, *, allowed: bool) -> Config:
    user_id = "@alice:example.org"
    return bind_runtime_paths(
        Config(
            agents={
                "general": AgentConfig(
                    display_name="General",
                    access={"users": [user_id] if allowed else []},
                ),
            },
        ),
        test_runtime_paths(tmp_path),
    )


def _joined_members_response(*user_ids: str) -> nio.JoinedMembersResponse:
    return nio.JoinedMembersResponse(
        [nio.RoomMember(user_id, user_id, None) for user_id in user_ids],
        "!other:example.org",
    )


def _context(tmp_path: Path, *, current_config: Config, client: AsyncMock) -> ToolRuntimeContext:
    return ToolRuntimeContext(
        agent_name="general",
        target=MessageTarget.resolve(
            room_id="!current:example.org",
            thread_id=None,
            reply_to_event_id=None,
        ),
        requester_id="@alice:example.org",
        client=client,
        config=_config(tmp_path, allowed=True),
        runtime_paths=test_runtime_paths(tmp_path),
        conversation_reader=make_conversation_reader_mock(),
        relations=make_relation_lookup(),
        agent_reply_memberships=AgentReplyMembershipIndex(),
        config_provider=lambda: current_config,
    )


@pytest.mark.asyncio
@pytest.mark.usefixtures("enforce_turn_authorization")
async def test_cross_room_tool_access_uses_current_authorization(tmp_path: Path) -> None:
    """Cross-room tools must not retain room access revoked after context construction."""
    client = AsyncMock()
    client.joined_members.return_value = _joined_members_response("@alice:example.org")
    context = _context(tmp_path, current_config=_config(tmp_path, allowed=False), client=client)

    assert not await room_access_allowed(context, "!other:example.org")


@pytest.mark.asyncio
@pytest.mark.usefixtures("enforce_turn_authorization")
async def test_cross_room_tool_access_requires_requester_membership(tmp_path: Path) -> None:
    """Responder access alone must not open rooms the requester is not part of."""
    client = AsyncMock()
    client.joined_members.return_value = _joined_members_response("@victim:example.org")
    context = _context(tmp_path, current_config=_config(tmp_path, allowed=True), client=client)

    assert not await room_access_allowed(context, "!other:example.org")


@pytest.mark.asyncio
@pytest.mark.usefixtures("enforce_turn_authorization")
async def test_cross_room_tool_access_allows_joined_requester(tmp_path: Path) -> None:
    """An allowed requester keeps access to rooms they are currently joined to."""
    client = AsyncMock()
    client.joined_members.return_value = _joined_members_response("@alice:example.org", "@victim:example.org")
    context = _context(tmp_path, current_config=_config(tmp_path, allowed=True), client=client)

    assert await room_access_allowed(context, "!other:example.org")


@pytest.mark.asyncio
@pytest.mark.usefixtures("enforce_turn_authorization")
async def test_cross_room_tool_access_fails_closed_on_unresolved_membership(tmp_path: Path) -> None:
    """An unreadable member list must deny rather than assume membership."""
    client = AsyncMock()
    client.joined_members.return_value = nio.JoinedMembersError.from_dict(
        {"errcode": "M_FORBIDDEN", "error": "not in room"},
        "!other:example.org",
    )
    context = _context(tmp_path, current_config=_config(tmp_path, allowed=True), client=client)

    assert not await room_access_allowed(context, "!other:example.org")


@pytest.mark.asyncio
@pytest.mark.usefixtures("enforce_turn_authorization")
async def test_current_room_access_needs_no_membership_fetch(tmp_path: Path) -> None:
    """The conversation room stays authorized without an extra membership round-trip."""
    client = AsyncMock()
    context = _context(tmp_path, current_config=_config(tmp_path, allowed=False), client=client)

    assert await room_access_allowed(context, "!current:example.org")
    client.joined_members.assert_not_awaited()

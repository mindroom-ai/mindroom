"""Cross-room tool targets require the requester to be joined to that room.

Responder access answers whether a sender may converse with an agent, so on its
own it would let any authorized requester drive the agent into every other room
it serves, including other users' personal rooms.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import nio
import pytest

from mindroom.custom_tools.matrix_api import MatrixApiTools
from mindroom.custom_tools.matrix_message import MatrixMessageTools
from mindroom.custom_tools.matrix_room import MatrixRoomTools
from mindroom.message_target import MessageTarget
from mindroom.tool_system.runtime_context import tool_runtime_context
from tests.access_schema_support import membership_config, membership_index
from tests.authorization_helpers import make_test_tool_runtime_context
from tests.conftest import make_conversation_reader_mock, make_relation_lookup, runtime_paths_for

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.tool_system.runtime_context import ToolRuntimeContext

_REQUESTER_ID = "@member:example.com"
_VICTIM_ID = "@victim:example.com"
_AGENT_ID = "@mindroom_talent:example.com"
_CONVERSATION_ROOM_ID = "!current:example.com"
_TARGET_ROOM_ID = "!other:example.com"
# Personal-room aliases are derived from the owner's user ID, so a requester can
# name another user's private room without ever seeing it.
_PERSONAL_ROOM_ALIAS = "#personal_0123456789abcdef0123:example.com"


async def _context(tmp_path: Path, *, target_room_members: tuple[str, ...]) -> ToolRuntimeContext:
    """Build a tool context whose requester is allowed by a grant room only."""
    config = membership_config(
        tmp_path,
        agent_rooms=["grant"],
        access={"current_room_members": False, "members_of_rooms": ["grant"]},
    )
    memberships = await membership_index(config, {"grant": {_REQUESTER_ID}})

    client = AsyncMock()
    client.user_id = _AGENT_ID
    client.rooms = {}
    client.joined_members.return_value = nio.JoinedMembersResponse(
        members=[nio.RoomMember(user_id, None, None) for user_id in target_room_members],
        room_id=_TARGET_ROOM_ID,
    )
    return make_test_tool_runtime_context(
        agent_name="talent",
        target=MessageTarget.resolve(
            room_id=_CONVERSATION_ROOM_ID,
            thread_id=None,
            reply_to_event_id=None,
        ),
        requester_id=_REQUESTER_ID,
        client=client,
        config=config,
        runtime_paths=runtime_paths_for(config),
        relations=make_relation_lookup(),
        conversation_reader=make_conversation_reader_mock(),
        room=None,
        agent_reply_memberships=memberships,
    )


def _assert_denied(payload_json: str, *, room_id: str) -> None:
    payload = json.loads(payload_json)
    assert payload["status"] == "error"
    assert payload["room_id"] == room_id
    assert payload["message"] == "Not authorized to access the target room."


@pytest.mark.asyncio
@pytest.mark.usefixtures("enforce_turn_authorization")
async def test_matrix_message_read_denies_unjoined_target_room(tmp_path: Path) -> None:
    """A grant-room requester cannot read a room they are not joined to."""
    context = await _context(tmp_path, target_room_members=(_VICTIM_ID, _AGENT_ID))

    with tool_runtime_context(context):
        payload = await MatrixMessageTools().matrix_message(action="read", room_id=_TARGET_ROOM_ID)

    _assert_denied(payload, room_id=_TARGET_ROOM_ID)
    context.client.room_messages.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.usefixtures("enforce_turn_authorization")
@pytest.mark.parametrize("action", ["members", "threads", "state"])
async def test_matrix_room_denies_unjoined_target_room(tmp_path: Path, action: str) -> None:
    """Room introspection must not expose rooms the requester is not part of."""
    context = await _context(tmp_path, target_room_members=(_VICTIM_ID, _AGENT_ID))

    with tool_runtime_context(context):
        payload = await MatrixRoomTools().matrix_room(action=action, room_id=_TARGET_ROOM_ID)

    _assert_denied(payload, room_id=_TARGET_ROOM_ID)
    context.client.room_get_state.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.usefixtures("enforce_turn_authorization")
@pytest.mark.parametrize(
    ("action", "kwargs"),
    [
        ("search", {"search_term": "secret"}),
        ("get_event", {"event_id": "$evt:example.com"}),
        ("send_event", {"event_type": "com.example.event", "content": {"body": "x"}}),
    ],
)
async def test_matrix_api_denies_unjoined_target_room(
    tmp_path: Path,
    action: str,
    kwargs: dict[str, object],
) -> None:
    """Low-level Matrix reads and writes must stay inside the requester's rooms."""
    context = await _context(tmp_path, target_room_members=(_VICTIM_ID, _AGENT_ID))

    with tool_runtime_context(context):
        payload = await MatrixApiTools().matrix_api(action=action, room_id=_TARGET_ROOM_ID, **kwargs)

    _assert_denied(payload, room_id=_TARGET_ROOM_ID)
    context.client.room_send.assert_not_awaited()
    context.client.room_get_event.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.usefixtures("enforce_turn_authorization")
async def test_matrix_message_read_denies_other_users_personal_room(tmp_path: Path) -> None:
    """Another user's deterministic personal-room alias must not resolve into access."""
    context = await _context(tmp_path, target_room_members=(_VICTIM_ID, _AGENT_ID))

    with tool_runtime_context(context):
        payload = await MatrixMessageTools().matrix_message(action="read", room_id=_PERSONAL_ROOM_ALIAS)

    _assert_denied(payload, room_id=_PERSONAL_ROOM_ALIAS)
    context.client.room_messages.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.usefixtures("enforce_turn_authorization")
async def test_matrix_room_allows_joined_target_room(tmp_path: Path) -> None:
    """A requester joined to the target room keeps authorized cross-room access."""
    context = await _context(tmp_path, target_room_members=(_REQUESTER_ID, _AGENT_ID))
    context.client.room_get_state.return_value = nio.RoomGetStateResponse([], _TARGET_ROOM_ID)

    with tool_runtime_context(context):
        payload = json.loads(await MatrixRoomTools().matrix_room(action="state", room_id=_TARGET_ROOM_ID))

    assert payload["status"] == "ok"
    assert payload["room_id"] == _TARGET_ROOM_ID

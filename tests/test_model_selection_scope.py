"""Actual Matrix membership and readable root proof for the picker."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock

import nio
import pytest

from mindroom.agent_reply_membership import AgentReplyMembershipIndex
from mindroom.config.access import ResponderAccessConfig
from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.config.models import ModelConfig
from mindroom.entity_resolution import entity_identity_registry
from mindroom.model_selection_scope import validate_model_picker_scope
from tests.conftest import bind_runtime_paths, runtime_paths_for, test_runtime_paths
from tests.test_conversation_hydration import encrypted

pytestmark = pytest.mark.usefixtures("enforce_turn_authorization")

if TYPE_CHECKING:
    from pathlib import Path

ROOM = "!room:localhost"
USER = "@user:localhost"


def picker_setup(tmp_path: Path) -> tuple:
    """Use real config, identity registry, room and membership response types."""
    config = bind_runtime_paths(
        Config(
            agents={"helper": AgentConfig(display_name="Helper", access=ResponderAccessConfig(users=[USER]))},
            models={"default": ModelConfig(provider="openai", id="test-model")},
        ),
        test_runtime_paths(tmp_path),
    )
    paths = runtime_paths_for(config)
    registry = entity_identity_registry(config, paths)
    router = registry.current_id("router").full_id
    agent = registry.current_id("helper").full_id
    client = AsyncMock(spec=nio.AsyncClient)
    client.user_id = router
    client.device_id = "DEVICE"
    client.olm = None
    room = nio.MatrixRoom(ROOM, router)
    for member in (USER, router, agent):
        room.add_member(member, member, None)
    room.members_synced = True
    client.rooms = {ROOM: room}
    client.joined_members.return_value = joined_response(USER, router, agent)
    client.room_get_event.return_value = nio.RoomGetEventResponse.from_dict(root_event().source)
    client.room_messages.return_value = nio.RoomMessagesResponse(room_id=ROOM, chunk=[], start="", end=None)
    return client, config, paths, AgentReplyMembershipIndex(), router, agent


def joined_response(*users: str) -> nio.JoinedMembersResponse:
    """Build an authoritative response at the network boundary."""
    return nio.JoinedMembersResponse.from_dict({"joined": {user: {} for user in users}}, ROOM)


def root_event(**changes: object) -> nio.Event:
    """Parse real Matrix message shapes, including malformed scope hints."""
    source = {
        "type": "m.room.message",
        "event_id": "$root",
        "room_id": ROOM,
        "sender": USER,
        "origin_server_ts": 1,
        "content": {"msgtype": "m.text", "body": "Root"},
    }
    source.update(changes)
    return nio.Event.parse_event(source)


@pytest.mark.asyncio
async def test_scope_requires_actual_joined_authorized_agent(tmp_path: Path) -> None:
    """Configured presence alone must not advertise inaccessible models."""
    client, config, paths, index, router, agent = picker_setup(tmp_path)
    scope = await validate_model_picker_scope(
        client=client,
        config=config,
        runtime_paths=paths,
        membership_index=index,
        room_id=ROOM,
        requester_user_id=USER,
        thread_id="$root",
    )
    assert scope is not None
    assert scope.agent_user_ids == (agent,)
    assert scope.entity_names == ("helper",)
    client.joined_members.return_value = joined_response(USER, router)
    assert (
        await validate_model_picker_scope(
            client=client,
            config=config,
            runtime_paths=paths,
            membership_index=index,
            room_id=ROOM,
            requester_user_id=USER,
            thread_id="$root",
        )
        is None
    )


@pytest.mark.parametrize(
    "root",
    [
        root_event(room_id="!foreign:localhost"),
        root_event(event_id="$wrong"),
        root_event(
            content={
                "msgtype": "m.text",
                "body": "Child",
                "m.relates_to": {"rel_type": "m.thread", "event_id": "$parent"},
            },
        ),
        root_event(
            content={
                "msgtype": "m.text",
                "body": "Edit",
                "m.relates_to": {"rel_type": "m.replace", "event_id": "$parent"},
            },
        ),
        root_event(content={}, unsigned={"redacted_because": {}}),
    ],
)
@pytest.mark.asyncio
async def test_invalid_root_cannot_grant_scope(tmp_path: Path, root: nio.Event) -> None:
    """A native thread relation shortcut cannot prove the referenced root."""
    client, config, paths, index, _, _ = picker_setup(tmp_path)
    client.room_get_event.return_value = nio.RoomGetEventResponse.from_dict(root.source)
    assert (
        await validate_model_picker_scope(
            client=client,
            config=config,
            runtime_paths=paths,
            membership_index=index,
            room_id=ROOM,
            requester_user_id=USER,
            thread_id="$root",
        )
        is None
    )


@pytest.mark.asyncio
async def test_membership_loss_during_root_fetch_denies_scope(tmp_path: Path) -> None:
    """A requester leaving during awaited validation must not retain access."""
    client, config, paths, index, router, agent = picker_setup(tmp_path)

    async def fetch(*_args: object) -> nio.RoomGetEventResponse:
        client.joined_members.return_value = joined_response(router, agent)
        return nio.RoomGetEventResponse.from_dict(root_event().source)

    client.room_get_event.side_effect = fetch
    assert (
        await validate_model_picker_scope(
            client=client,
            config=config,
            runtime_paths=paths,
            membership_index=index,
            room_id=ROOM,
            requester_user_id=USER,
            thread_id="$root",
        )
        is None
    )


@pytest.mark.parametrize("absent", ["requester", "router"])
@pytest.mark.asyncio
async def test_missing_joined_member_denies_scope(tmp_path: Path, absent: str) -> None:
    """Cached or invited users cannot replace actual joined membership."""
    client, config, paths, index, router, agent = picker_setup(tmp_path)
    users = {"requester": USER, "router": router, "agent": agent}
    client.joined_members.return_value = joined_response(*(user for name, user in users.items() if name != absent))
    assert (
        await validate_model_picker_scope(
            client=client,
            config=config,
            runtime_paths=paths,
            membership_index=index,
            room_id=ROOM,
            requester_user_id=USER,
            thread_id=None,
        )
        is None
    )


@pytest.mark.asyncio
async def test_joined_but_unauthorized_agent_does_not_grant_scope(tmp_path: Path) -> None:
    """Actual presence cannot bypass responder access rules."""
    client, config, paths, index, _, _ = picker_setup(tmp_path)
    config.agents["helper"].access = ResponderAccessConfig(current_room_members=False, users=["@someone:localhost"])
    assert (
        await validate_model_picker_scope(
            client=client,
            config=config,
            runtime_paths=paths,
            membership_index=index,
            room_id=ROOM,
            requester_user_id=USER,
            thread_id="$root",
        )
        is None
    )


@pytest.mark.parametrize("decrypted", ["root", "child", "foreign", "unreadable"])
@pytest.mark.asyncio
async def test_encrypted_root_proof_uses_only_readable_content(tmp_path: Path, decrypted: str) -> None:
    """Outer encrypted relation hints cannot authorize an unreadable or non-root event."""
    client, config, paths, index, _, _ = picker_setup(tmp_path)
    source = encrypted("$root", sender=USER)
    source["room_id"] = ROOM
    source["content"]["m.relates_to"] = {"rel_type": "m.thread", "event_id": "$outer"}
    client.room_get_event.return_value = nio.RoomGetEventResponse.from_dict(source)
    client.olm = MagicMock()
    if decrypted == "unreadable":
        client.decrypt_event.side_effect = nio.EncryptionError("No key")
    else:
        clear = root_event()
        if decrypted == "child":
            clear = root_event(
                content={
                    "msgtype": "m.text",
                    "body": "child",
                    "m.relates_to": {"rel_type": "m.thread", "event_id": "$parent"},
                },
            )
        elif decrypted == "foreign":
            clear = root_event(room_id="!foreign:localhost")
        client.decrypt_event.return_value = clear
    scope = await validate_model_picker_scope(
        client=client,
        config=config,
        runtime_paths=paths,
        membership_index=index,
        room_id=ROOM,
        requester_user_id=USER,
        thread_id="$root",
    )
    assert (scope is not None) == (decrypted == "root")

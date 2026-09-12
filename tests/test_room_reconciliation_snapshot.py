"""Fresh room snapshots replace repeated state reads, never remote authority."""

from pathlib import Path
from unittest.mock import AsyncMock, patch

import aiohttp
import nio
import pytest

from mindroom.matrix import client_room_admin, room_reconciliation
from mindroom.matrix import rooms as matrix_rooms
from mindroom.topic_generator import ensure_room_has_topic
from tests.access_schema_support import membership_config
from tests.conftest import runtime_paths_for


@pytest.mark.asyncio
async def test_one_snapshot_serves_name_join_rule_and_space_children() -> None:
    """Satisfied policy uses one complete state GET and no per-event GET or PUT."""
    client = AsyncMock()
    room_id = "!space:example.com"
    client.room_get_state.return_value = nio.RoomGetStateResponse(
        [
            {"type": "m.room.name", "state_key": "", "content": {"name": "Workspace"}},
            {"type": "m.room.join_rules", "state_key": "", "content": {"join_rule": "invite"}},
            {
                "type": "m.space.child",
                "state_key": "!child:example.com",
                "content": {"via": ["example.com"], "suggested": True},
            },
            {"type": "m.room.member", "state_key": "@invited:example.com", "content": {"membership": "invite"}},
            {"type": "m.room.member", "state_key": "@joined:example.com", "content": {"membership": "join"}},
            {"type": "m.room.member", "state_key": "@left:example.com", "content": {"membership": "leave"}},
        ],
        room_id,
    )
    snapshot = await room_reconciliation.read_room_state(client, room_id)
    assert snapshot is not None
    assert snapshot.present_user_ids() == {"@invited:example.com", "@joined:example.com"}
    assert await client_room_admin.ensure_room_name(client, room_id, "Workspace", snapshot=snapshot)
    assert await client_room_admin.ensure_room_join_rule(client, room_id, "invite", snapshot=snapshot)
    assert await client_room_admin.add_room_to_space(
        client,
        room_id,
        "!child:example.com",
        "example.com",
        snapshot=snapshot,
    )
    client.room_get_state.assert_awaited_once_with(room_id)
    client.room_get_state_event.assert_not_awaited()
    client.room_put_state.assert_not_awaited()


@pytest.mark.asyncio
async def test_unavailable_snapshot_is_not_an_empty_room() -> None:
    """A state failure cannot turn missing knowledge into permission to write."""
    client = AsyncMock()
    client.room_get_state.return_value = nio.RoomGetStateError("forbidden", "M_FORBIDDEN", "!room:example.com")
    assert await room_reconciliation.read_room_state(client, "!room:example.com") is None
    client.room_put_state.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("content", [None, [], "invalid"])
async def test_snapshot_rejects_content_not_validated_by_nio(content: object) -> None:
    """Nio validates the state envelope but leaves content shape unchecked."""
    client = AsyncMock()
    room_id = "!room:example.com"
    client.room_get_state.return_value = nio.RoomGetStateResponse.from_dict(
        [
            {
                "event_id": "$state",
                "sender": "@a:example.com",
                "type": "m.room.topic",
                "state_key": "",
                "origin_server_ts": 1,
                "content": content,
            },
        ],
        room_id,
    )
    assert isinstance(client.room_get_state.return_value, nio.RoomGetStateResponse)
    assert await room_reconciliation.read_room_state(client, room_id) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [aiohttp.ClientConnectionError(), TimeoutError(), ValueError("bad JSON")])
async def test_snapshot_transport_failure_is_unavailable(error: Exception) -> None:
    """A failed external read cannot abort unrelated room administration."""
    client = AsyncMock()
    client.room_get_state.side_effect = error
    assert await room_reconciliation.read_room_state(client, "!room:example.com") is None


@pytest.mark.asyncio
async def test_snapshot_preserves_power_levels_and_existing_topic(tmp_path: Path) -> None:
    """Managed policy updates preserve foreign power keys and reuse known topic/encryption."""
    config = membership_config(tmp_path, agent_rooms=["lobby"])
    client = AsyncMock()
    room_id = "!room:example.com"
    snapshot = room_reconciliation.RoomStateSnapshot(
        room_id,
        {
            ("m.room.power_levels", ""): {"users": {"@owner:example.com": 100}, "custom": "keep"},
            ("m.room.topic", ""): {"topic": "Existing human topic"},
            ("m.room.encryption", ""): {"algorithm": "m.megolm.v1.aes-sha2"},
        },
    )
    client.room_put_state.return_value = nio.RoomPutStateResponse("$power", room_id)
    client.room_get_state_event.return_value = nio.RoomGetStateEventResponse(
        {"users": {"@owner:example.com": 100}, "custom": "keep"},
        "m.room.power_levels",
        "",
        room_id,
    )
    assert await client_room_admin.ensure_managed_room_power_levels(client, room_id, snapshot=snapshot)
    assert client.room_put_state.await_args.kwargs["content"]["custom"] == "keep"
    assert client.room_put_state.await_args.kwargs["content"]["users"] == {"@owner:example.com": 100}
    assert await client_room_admin.ensure_room_encryption_enabled(client, room_id, snapshot=snapshot)
    assert await ensure_room_has_topic(
        client,
        room_id,
        "lobby",
        "Lobby",
        config,
        runtime_paths_for(config),
        snapshot=snapshot,
    )
    client.room_get_state_event.assert_awaited_once_with(room_id, "m.room.power_levels", "")
    assert client.room_put_state.await_count == 1


@pytest.mark.asyncio
async def test_power_write_does_not_restore_revoked_admin_from_snapshot() -> None:
    """A stale policy snapshot may avoid a write but cannot supply a write's user grants."""
    client = AsyncMock()
    room_id = "!room:example.com"
    snapshot = room_reconciliation.RoomStateSnapshot(
        room_id,
        {
            ("m.room.power_levels", ""): {"users": {"@revoked:example.com": 100}},
        },
    )
    client.room_get_state_event.return_value = nio.RoomGetStateEventResponse(
        {"users": {}, "new_custom": "preserve"},
        "m.room.power_levels",
        "",
        room_id,
    )
    client.room_put_state.return_value = nio.RoomPutStateResponse("$power", room_id)
    assert await client_room_admin.ensure_managed_room_power_levels(client, room_id, snapshot=snapshot)
    assert client.room_put_state.await_args.kwargs["content"]["users"] == {}
    assert client.room_put_state.await_args.kwargs["content"]["new_custom"] == "preserve"


@pytest.mark.asyncio
async def test_one_failed_room_does_not_cancel_healthy_reconciliation(tmp_path: Path) -> None:
    """A room-local policy failure cannot abort sibling rooms."""
    config = membership_config(tmp_path, agent_rooms=["broken", "healthy"])
    client = AsyncMock()
    client.user_id = "@router:example.com"
    client.room_get_state.side_effect = lambda room_id: nio.RoomGetStateResponse(
        [
            {"type": "m.room.member", "state_key": client.user_id, "content": {"membership": "join"}},
        ],
        room_id,
    )

    async def policy(_client: nio.AsyncClient, room_key: str, *_args: object, **_kwargs: object) -> None:
        if room_key == "broken":
            message = "one room policy unavailable"
            raise RuntimeError(message)

    with patch.object(matrix_rooms, "_reconcile_joined_existing_room", side_effect=policy):
        result = await matrix_rooms.reconcile_managed_rooms(
            client,
            config,
            runtime_paths_for(config),
            {"broken": "!broken:example.com", "healthy": "!healthy:example.com"},
        )
    assert set(result) == {"!healthy:example.com"}


@pytest.mark.asyncio
@pytest.mark.parametrize("membership", ["join", "invite", "leave"])
async def test_readable_unjoined_room_does_not_start_policy_work(tmp_path: Path, membership: str) -> None:
    """State readability alone does not make a room joined or router-managed."""
    config = membership_config(tmp_path, agent_rooms=["lobby"])
    client = AsyncMock()
    client.user_id = "@router:example.com"
    room_id = "!room:example.com"
    client.room_get_state.return_value = nio.RoomGetStateResponse(
        [
            {"type": "m.room.member", "state_key": client.user_id, "content": {"membership": membership}},
        ],
        room_id,
    )
    with patch.object(matrix_rooms, "_reconcile_joined_existing_room", new=AsyncMock()) as policy:
        result = await matrix_rooms.reconcile_managed_rooms(
            client,
            config,
            runtime_paths_for(config),
            {"lobby": room_id},
        )
    assert policy.await_count == (1 if membership == "join" else 0)
    assert set(result) == ({room_id} if membership == "join" else set())


@pytest.mark.asyncio
async def test_root_space_reuses_one_fresh_snapshot(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A large existing Space does not issue one GET per child on each startup."""
    config = membership_config(tmp_path, agent_rooms=["lobby"])
    config.matrix_space.enabled = True
    client = AsyncMock()
    client.homeserver = "https://example.com"
    space_id = "!space:example.com"
    monkeypatch.setattr(matrix_rooms, "_ensure_root_space_exists", AsyncMock(return_value=space_id))
    monkeypatch.setattr(matrix_rooms, "_set_room_avatar_if_available", AsyncMock())
    client.room_get_state.return_value = nio.RoomGetStateResponse(
        [
            {"type": "m.room.name", "state_key": "", "content": {"name": config.matrix_space.name}},
            {
                "type": "m.space.child",
                "state_key": "!child:example.com",
                "content": {"via": ["example.com"], "suggested": True},
            },
        ],
        space_id,
    )
    result = await matrix_rooms.ensure_root_space(
        client,
        config,
        runtime_paths_for(config),
        {"lobby": "!child:example.com"},
    )
    assert result == space_id
    client.room_get_state.assert_awaited_once_with(space_id)
    client.room_get_state_event.assert_not_awaited()
    client.room_put_state.assert_not_awaited()


@pytest.mark.asyncio
async def test_managed_policy_snapshot_failure_skips_mutations(tmp_path: Path) -> None:
    """An inaccessible room cannot trigger topic generation or policy writes."""
    config = membership_config(tmp_path, agent_rooms=["lobby"])
    client = AsyncMock()
    client.room_get_state.return_value = nio.RoomGetStateError("forbidden", "M_FORBIDDEN", "!room:example.com")
    assert (
        await matrix_rooms.reconcile_managed_rooms(
            client,
            config,
            runtime_paths_for(config),
            {"lobby": "!room:example.com"},
        )
        == {}
    )
    client.room_put_state.assert_not_awaited()
    client.room_get_state_event.assert_not_awaited()
    client.room_get_visibility.assert_not_awaited()


@pytest.mark.asyncio
async def test_each_policy_pass_repairs_fresh_remote_drift(tmp_path: Path) -> None:
    """An unchanged config does not hide an administrator's later remote change."""
    config = membership_config(tmp_path, agent_rooms=["lobby"], rooms={"lobby": {"display_name": "Lobby"}})
    client = AsyncMock()
    client.user_id = "@router:example.com"
    room_id = "!room:example.com"
    client.room_get_state.side_effect = [
        nio.RoomGetStateResponse(
            [
                {"type": "m.room.member", "state_key": client.user_id, "content": {"membership": "join"}},
                {"type": "m.room.name", "state_key": "", "content": {"name": name}},
                {"type": "m.room.topic", "state_key": "", "content": {"topic": "Human topic"}},
                {"type": "m.room.power_levels", "state_key": "", "content": {}},
                {"type": "m.room.join_rules", "state_key": "", "content": {"join_rule": "invite"}},
            ],
            room_id,
        )
        for name in ("Lobby", "Remote change")
    ]
    client.room_get_visibility.return_value = nio.RoomGetVisibilityResponse(room_id=room_id, visibility="private")
    client.room_get_state_event.return_value = nio.RoomGetStateEventResponse({}, "m.room.power_levels", "", room_id)
    client.room_put_state.return_value = nio.RoomPutStateResponse("$updated", room_id)
    for _ in range(2):
        await matrix_rooms.reconcile_managed_rooms(client, config, runtime_paths_for(config), {"lobby": room_id})
    names = [
        call.kwargs["content"]
        for call in client.room_put_state.await_args_list
        if call.kwargs.get("event_type") == "m.room.name"
    ]
    assert names == [{"name": "Lobby"}]
    assert client.room_get_state.await_count == 2
    assert [call.args[1] for call in client.room_get_state_event.await_args_list] == [
        "m.room.power_levels",
        "m.room.power_levels",
    ]

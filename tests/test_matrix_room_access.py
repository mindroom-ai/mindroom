"""Tests for managed Matrix room access and discoverability settings."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import nio
import pytest

from mindroom import topic_generator
from mindroom.config.main import Config
from mindroom.constants import SCHEDULED_TASK_EVENT_TYPE, resolve_runtime_paths
from mindroom.matrix import client as matrix_client
from mindroom.matrix import rooms as matrix_rooms
from mindroom.matrix.presence import is_user_online
from mindroom.thread_tags import THREAD_TAGS_EVENT_TYPE
from tests.conftest import TEST_ACCESS_TOKEN, bind_runtime_paths, runtime_paths_for


class _FakeHttpResponse:
    """Simple fake aiohttp response for low-level Matrix API tests."""

    def __init__(self, status: int, body: str) -> None:
        self.status = status
        self._body = body
        self.released = False

    async def text(self) -> str:
        return self._body

    def release(self) -> None:
        self.released = True


def _config_with_runtime_paths(
    tmp_path: Path,
    **config_data: object,
) -> Config:
    runtime_paths = resolve_runtime_paths(config_path=tmp_path / "config.yaml", storage_path=tmp_path / "mindroom_data")
    return bind_runtime_paths(
        Config.model_validate(config_data, context={"runtime_paths": runtime_paths}),
        runtime_paths,
    )


def test_matrix_room_access_defaults() -> None:
    """Matrix room access config should default to private/single-user behavior."""
    config = Config()

    assert config.matrix_room_access.mode == "single_user_private"
    assert config.matrix_room_access.multi_user_join_rule == "public"
    assert config.matrix_room_access.publish_to_room_directory is False
    assert config.matrix_room_access.invite_only_rooms == []
    assert config.matrix_room_access.reconcile_existing_rooms is False


def test_matrix_room_access_yaml_null_uses_defaults(tmp_path: Path) -> None:
    """`matrix_room_access: null` should be treated the same as omitting the block."""
    config_path = tmp_path / "config.yaml"
    config_path.write_text("matrix_room_access: null\n", encoding="utf-8")

    config = Config.from_yaml(config_path)
    assert config.matrix_room_access.mode == "single_user_private"


def test_matrix_room_access_invite_only_matching() -> None:
    """Invite-only matching should work for room key, alias, and room ID."""
    config = _config_with_runtime_paths(
        Path(),
        matrix_room_access={
            "mode": "multi_user",
            "invite_only_rooms": ["lobby", "#ops:example.com", "!secret:example.com"],
        },
    )
    access = config.matrix_room_access
    runtime_paths = runtime_paths_for(config)

    assert access.is_invite_only_room("lobby", runtime_paths)
    assert access.is_invite_only_room("ops", runtime_paths, room_alias="#ops:example.com")
    assert access.is_invite_only_room("random", runtime_paths, room_id="!secret:example.com")
    assert not access.is_invite_only_room("public-room", runtime_paths)


@pytest.mark.asyncio
async def test_configure_managed_room_access_public_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    """Multi-user mode should configure non-restricted rooms as joinable/publishable when enabled."""
    config = _config_with_runtime_paths(
        Path(),
        matrix_room_access={
            "mode": "multi_user",
            "multi_user_join_rule": "public",
            "publish_to_room_directory": True,
        },
    )
    mock_client = AsyncMock()
    ensure_join_rule = AsyncMock(return_value=True)
    ensure_directory_visibility = AsyncMock(return_value=True)
    monkeypatch.setattr(matrix_rooms, "ensure_room_join_rule", ensure_join_rule)
    monkeypatch.setattr(matrix_rooms, "ensure_room_directory_visibility", ensure_directory_visibility)

    result = await matrix_rooms._configure_managed_room_access(
        client=mock_client,
        room_key="lobby",
        room_id="!lobby:example.com",
        config=config,
        runtime_paths=runtime_paths_for(config),
        context="test",
    )

    assert result is True
    ensure_join_rule.assert_awaited_once_with(mock_client, "!lobby:example.com", "public")
    ensure_directory_visibility.assert_awaited_once_with(mock_client, "!lobby:example.com", "public")


@pytest.mark.asyncio
async def test_configure_managed_room_access_invite_only_override(monkeypatch: pytest.MonkeyPatch) -> None:
    """Invite-only room overrides should force invite/private targets even in multi-user mode."""
    config = _config_with_runtime_paths(
        Path(),
        matrix_room_access={
            "mode": "multi_user",
            "multi_user_join_rule": "public",
            "publish_to_room_directory": True,
            "invite_only_rooms": ["lobby"],
        },
    )
    mock_client = AsyncMock()
    ensure_join_rule = AsyncMock(return_value=True)
    ensure_directory_visibility = AsyncMock(return_value=True)
    monkeypatch.setattr(matrix_rooms, "ensure_room_join_rule", ensure_join_rule)
    monkeypatch.setattr(matrix_rooms, "ensure_room_directory_visibility", ensure_directory_visibility)

    result = await matrix_rooms._configure_managed_room_access(
        client=mock_client,
        room_key="lobby",
        room_id="!lobby:example.com",
        config=config,
        runtime_paths=runtime_paths_for(config),
        context="test",
    )

    assert result is True
    ensure_join_rule.assert_awaited_once_with(mock_client, "!lobby:example.com", "invite")
    ensure_directory_visibility.assert_awaited_once_with(mock_client, "!lobby:example.com", "private")


@pytest.mark.asyncio
@pytest.mark.parametrize(("reconcile_existing", "expected_calls"), [(False, 0), (True, 1)])
async def test_existing_room_reconciliation_respects_flag(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    reconcile_existing: bool,
    expected_calls: int,
) -> None:
    """Existing room updates should be gated behind `reconcile_existing_rooms`."""
    config = _config_with_runtime_paths(
        tmp_path,
        matrix_room_access={
            "mode": "multi_user",
            "reconcile_existing_rooms": reconcile_existing,
        },
    )
    mock_client = AsyncMock()
    mock_client.homeserver = "https://example.com"
    mock_client.rooms = {}
    mock_client.room_resolve_alias.return_value = nio.RoomResolveAliasResponse(
        room_alias="#lobby:example.com",
        room_id="!lobby:example.com",
        servers=["example.com"],
    )

    monkeypatch.setattr(matrix_rooms, "load_rooms", dict)
    monkeypatch.setattr(matrix_rooms, "_add_room", MagicMock())
    monkeypatch.setattr(matrix_rooms, "get_joined_rooms", AsyncMock(return_value=["!lobby:example.com"]))
    monkeypatch.setattr(matrix_rooms, "ensure_room_has_topic", AsyncMock())
    ensure_thread_tags_power_level = AsyncMock(return_value=True)
    ensure_scheduled_task_power_level_removed = AsyncMock(return_value=True)
    monkeypatch.setattr(
        matrix_rooms,
        "ensure_thread_tags_power_level",
        ensure_thread_tags_power_level,
    )
    monkeypatch.setattr(
        matrix_rooms,
        "ensure_scheduled_task_power_level_removed",
        ensure_scheduled_task_power_level_removed,
    )
    configure_access = AsyncMock(return_value=True)
    monkeypatch.setattr(matrix_rooms, "_configure_managed_room_access", configure_access)

    room_id = await matrix_rooms._ensure_room_exists(
        client=mock_client,
        room_key="lobby",
        config=config,
        runtime_paths=runtime_paths_for(config),
        room_name="Lobby",
        power_users=[],
    )

    assert room_id == "!lobby:example.com"
    ensure_thread_tags_power_level.assert_awaited_once_with(
        mock_client,
        "!lobby:example.com",
    )
    ensure_scheduled_task_power_level_removed.assert_awaited_once_with(
        mock_client,
        "!lobby:example.com",
    )
    assert configure_access.await_count == expected_calls


@pytest.mark.asyncio
async def test_new_room_creation_applies_access_policy_in_multi_user_mode(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Newly created managed rooms should apply access policy when multi-user mode is enabled."""
    config = _config_with_runtime_paths(
        tmp_path,
        matrix_room_access={
            "mode": "multi_user",
            "multi_user_join_rule": "public",
            "publish_to_room_directory": True,
        },
    )
    mock_client = AsyncMock()
    mock_client.homeserver = "https://example.com"
    mock_client.room_resolve_alias.return_value = nio.RoomResolveAliasError("not found", status_code="M_NOT_FOUND")

    monkeypatch.setattr(matrix_rooms, "load_rooms", dict)
    monkeypatch.setattr(matrix_rooms, "generate_room_topic_ai", AsyncMock(return_value="topic"))
    monkeypatch.setattr(matrix_rooms, "create_room", AsyncMock(return_value="!lobby:example.com"))
    monkeypatch.setattr(matrix_rooms, "_add_room", MagicMock())
    configure_access = AsyncMock(return_value=True)
    monkeypatch.setattr(matrix_rooms, "_configure_managed_room_access", configure_access)

    room_id = await matrix_rooms._ensure_room_exists(
        client=mock_client,
        room_key="lobby",
        config=config,
        runtime_paths=runtime_paths_for(config),
        room_name="Lobby",
        power_users=[],
    )

    assert room_id == "!lobby:example.com"
    configure_access.assert_awaited_once_with(
        client=mock_client,
        room_key="lobby",
        room_id="!lobby:example.com",
        config=config,
        runtime_paths=runtime_paths_for(config),
        room_alias="#lobby:example.com",
        context="new_room_creation",
    )


@pytest.mark.asyncio
async def test_create_room_seeds_thread_tags_power_level(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Managed room creation should seed only the thread-tag state-event override."""
    mock_client = AsyncMock()
    mock_client.user_id = "@router:example.com"
    mock_client.room_create.return_value = nio.RoomCreateResponse(room_id="!lobby:example.com")
    invite_to_room = AsyncMock(return_value=matrix_client.RoomInviteResult(outcome="invited"))
    monkeypatch.setattr(matrix_client, "invite_to_room", invite_to_room)

    room_id = await matrix_client.create_room(
        client=mock_client,
        name="Lobby",
        alias="lobby",
        topic="topic",
        power_users=["@agent:example.com"],
    )

    assert room_id == "!lobby:example.com"
    _, kwargs = mock_client.room_create.await_args
    initial_state = kwargs["initial_state"]
    assert len(initial_state) == 1
    assert initial_state[0]["type"] == "m.room.power_levels"
    power_levels = initial_state[0]["content"]
    assert power_levels["state_default"] == 50
    assert power_levels["events"][THREAD_TAGS_EVENT_TYPE] == 0
    assert "com.mindroom.scheduled.task" not in power_levels["events"]
    assert power_levels["users"]["@agent:example.com"] == 50
    assert power_levels["users"]["@router:example.com"] == 100
    invite_to_room.assert_awaited_once_with(mock_client, "!lobby:example.com", "@agent:example.com")


@pytest.mark.asyncio
async def test_ensure_thread_tags_power_level_preserves_existing_content() -> None:
    """Reconciliation should preserve existing power-level content while adding the override."""
    mock_client = AsyncMock()
    mock_client.room_get_state_event.return_value = nio.RoomGetStateEventResponse(
        content={
            "events": {"m.room.name": 50},
            "state_default": 50,
            "users": {"@router:example.com": 100},
        },
        event_type="m.room.power_levels",
        state_key="",
        room_id="!room:example.com",
    )
    mock_client.room_put_state.return_value = nio.RoomPutStateResponse.from_dict(
        {"event_id": "$state"},
        room_id="!room:example.com",
    )

    result = await matrix_client.ensure_thread_tags_power_level(mock_client, "!room:example.com")

    assert result is True
    mock_client.room_put_state.assert_awaited_once()
    _, kwargs = mock_client.room_put_state.await_args
    assert kwargs["room_id"] == "!room:example.com"
    assert kwargs["event_type"] == "m.room.power_levels"
    assert kwargs["content"]["users"] == {"@router:example.com": 100}
    assert kwargs["content"]["state_default"] == 50
    assert kwargs["content"]["events"]["m.room.name"] == 50
    assert kwargs["content"]["events"][THREAD_TAGS_EVENT_TYPE] == 0


@pytest.mark.asyncio
async def test_ensure_thread_tags_power_level_always_fetches_fresh_power_levels() -> None:
    """Write-back reconciliation must fetch fresh power levels, not use cached ones."""
    mock_client = AsyncMock()
    room = nio.MatrixRoom("!room:example.com", "@router:example.com")
    room.power_levels.defaults.state_default = 50
    room.power_levels.users["@router:example.com"] = 100
    room.power_levels.events["m.room.name"] = 50
    mock_client.rooms = {"!room:example.com": room}
    mock_client.room_get_state_event.return_value = nio.RoomGetStateEventResponse(
        content={
            "events": {"m.room.name": 50},
            "state_default": 50,
            "users": {"@router:example.com": 100},
        },
        event_type="m.room.power_levels",
        state_key="",
        room_id="!room:example.com",
    )
    mock_client.room_put_state.return_value = nio.RoomPutStateResponse.from_dict(
        {"event_id": "$state"},
        room_id="!room:example.com",
    )

    result = await matrix_client.ensure_thread_tags_power_level(mock_client, "!room:example.com")

    assert result is True
    mock_client.room_get_state_event.assert_awaited_once_with("!room:example.com", "m.room.power_levels")
    mock_client.room_put_state.assert_awaited_once()


@pytest.mark.asyncio
async def test_ensure_thread_tags_power_level_does_not_restore_removed_overrides() -> None:
    """Stale cached overrides must not be written back when adding thread-tags PL."""
    mock_client = AsyncMock()
    room = nio.MatrixRoom("!room:example.com", "@router:example.com")
    room.power_levels.users["@router:example.com"] = 100
    room.power_levels.users["@removed:example.com"] = 50
    mock_client.rooms = {"!room:example.com": room}
    mock_client.room_get_state_event.return_value = nio.RoomGetStateEventResponse(
        content={
            "events": {},
            "state_default": 50,
            "users": {"@router:example.com": 100},
        },
        event_type="m.room.power_levels",
        state_key="",
        room_id="!room:example.com",
    )
    mock_client.room_put_state.return_value = nio.RoomPutStateResponse.from_dict(
        {"event_id": "$state"},
        room_id="!room:example.com",
    )

    result = await matrix_client.ensure_thread_tags_power_level(mock_client, "!room:example.com")

    assert result is True
    _, kwargs = mock_client.room_put_state.await_args
    assert "@removed:example.com" not in kwargs["content"].get("users", {})


@pytest.mark.asyncio
async def test_ensure_scheduled_task_power_level_removed_drops_stale_override() -> None:
    """Reconciliation should remove the stale scheduled-task override from existing rooms."""
    mock_client = AsyncMock()
    mock_client.room_get_state_event.return_value = nio.RoomGetStateEventResponse(
        content={
            "events": {
                THREAD_TAGS_EVENT_TYPE: 0,
                SCHEDULED_TASK_EVENT_TYPE: 0,
                "m.room.name": 50,
            },
            "state_default": 50,
            "users": {"@router:example.com": 100},
        },
        event_type="m.room.power_levels",
        state_key="",
        room_id="!room:example.com",
    )
    mock_client.room_put_state.return_value = nio.RoomPutStateResponse.from_dict(
        {"event_id": "$state"},
        room_id="!room:example.com",
    )

    result = await matrix_client.ensure_scheduled_task_power_level_removed(mock_client, "!room:example.com")

    assert result is True
    _, kwargs = mock_client.room_put_state.await_args
    assert kwargs["content"]["events"][THREAD_TAGS_EVENT_TYPE] == 0
    assert kwargs["content"]["events"]["m.room.name"] == 50
    assert SCHEDULED_TASK_EVENT_TYPE not in kwargs["content"]["events"]


@pytest.mark.asyncio
async def test_ensure_scheduled_task_power_level_removed_preserves_admin_override() -> None:
    """Reconciliation should preserve intentional non-zero scheduled-task overrides."""
    mock_client = AsyncMock()
    mock_client.room_get_state_event.return_value = nio.RoomGetStateEventResponse(
        content={
            "events": {
                THREAD_TAGS_EVENT_TYPE: 0,
                SCHEDULED_TASK_EVENT_TYPE: 100,
                "m.room.name": 50,
            },
            "state_default": 50,
            "users": {"@router:example.com": 100},
        },
        event_type="m.room.power_levels",
        state_key="",
        room_id="!room:example.com",
    )

    result = await matrix_client.ensure_scheduled_task_power_level_removed(mock_client, "!room:example.com")

    assert result is True
    mock_client.room_put_state.assert_not_awaited()


@pytest.mark.asyncio
async def test_ensure_thread_tags_power_level_idempotent() -> None:
    """Reconciliation should skip writes when the override already exists."""
    mock_client = AsyncMock()
    mock_client.room_get_state_event.return_value = nio.RoomGetStateEventResponse(
        content={
            "events": {THREAD_TAGS_EVENT_TYPE: 0},
            "state_default": 50,
            "users": {"@router:example.com": 100},
        },
        event_type="m.room.power_levels",
        state_key="",
        room_id="!room:example.com",
    )

    result = await matrix_client.ensure_thread_tags_power_level(mock_client, "!room:example.com")

    assert result is True
    mock_client.room_put_state.assert_not_awaited()


@pytest.mark.asyncio
async def test_ensure_thread_tags_power_level_returns_false_on_state_read_exception() -> None:
    """Power-level reconciliation should treat transport read failures as soft failures."""
    mock_client = AsyncMock()
    mock_client.room_get_state_event.side_effect = RuntimeError("boom transport")

    result = await matrix_client.ensure_thread_tags_power_level(mock_client, "!room:example.com")

    assert result is False
    mock_client.room_put_state.assert_not_awaited()


@pytest.mark.asyncio
async def test_ensure_thread_tags_power_level_returns_false_on_state_write_exception() -> None:
    """Power-level reconciliation should treat transport write failures as soft failures."""
    mock_client = AsyncMock()
    mock_client.room_get_state_event.return_value = nio.RoomGetStateEventResponse(
        content={
            "events": {},
            "state_default": 50,
            "users": {"@router:example.com": 100},
        },
        event_type="m.room.power_levels",
        state_key="",
        room_id="!room:example.com",
    )
    mock_client.room_put_state.side_effect = RuntimeError("boom transport")

    result = await matrix_client.ensure_thread_tags_power_level(mock_client, "!room:example.com")

    assert result is False


@pytest.mark.asyncio
async def test_ensure_room_join_rule_idempotent(monkeypatch: pytest.MonkeyPatch) -> None:
    """Join-rule reconciliation should be idempotent when already in desired state."""
    mock_client = AsyncMock()
    monkeypatch.setattr(matrix_client, "_get_room_join_rule", AsyncMock(return_value="public"))
    set_room_join_rule = AsyncMock(return_value=True)
    monkeypatch.setattr(matrix_client, "_set_room_join_rule", set_room_join_rule)

    result = await matrix_client.ensure_room_join_rule(mock_client, "!room:example.com", "public")

    assert result is True
    set_room_join_rule.assert_not_awaited()


@pytest.mark.asyncio
async def test_get_room_join_rule_falls_back_to_state_event_when_room_missing() -> None:
    """Join-rule reads should still hit the homeserver when the room cache is unavailable."""
    mock_client = AsyncMock()
    mock_client.rooms = {}
    mock_client.room_get_state_event.return_value = nio.RoomGetStateEventResponse(
        content={"join_rule": "public"},
        event_type="m.room.join_rules",
        state_key="",
        room_id="!room:example.com",
    )

    result = await matrix_client._get_room_join_rule(mock_client, "!room:example.com")

    assert result == "public"
    mock_client.room_get_state_event.assert_awaited_once_with("!room:example.com", "m.room.join_rules")


@pytest.mark.asyncio
async def test_get_room_join_rule_falls_back_to_state_event_when_room_not_synced() -> None:
    """Unsynced rooms should not trust nio's default `invite` join rule."""
    mock_client = AsyncMock()
    room = nio.MatrixRoom("!room:example.com", "@router:example.com")
    mock_client.rooms = {"!room:example.com": room}
    mock_client.room_get_state_event.return_value = nio.RoomGetStateEventResponse(
        content={"join_rule": "public"},
        event_type="m.room.join_rules",
        state_key="",
        room_id="!room:example.com",
    )

    result = await matrix_client._get_room_join_rule(mock_client, "!room:example.com")

    assert result == "public"
    mock_client.room_get_state_event.assert_awaited_once_with("!room:example.com", "m.room.join_rules")


@pytest.mark.asyncio
async def test_ensure_room_directory_visibility_idempotent(monkeypatch: pytest.MonkeyPatch) -> None:
    """Directory visibility reconciliation should be idempotent when already in desired state."""
    mock_client = AsyncMock()
    monkeypatch.setattr(matrix_client, "_get_room_directory_visibility", AsyncMock(return_value="private"))
    set_room_directory_visibility = AsyncMock(return_value=True)
    monkeypatch.setattr(matrix_client, "_set_room_directory_visibility", set_room_directory_visibility)

    result = await matrix_client.ensure_room_directory_visibility(mock_client, "!room:example.com", "private")

    assert result is True
    set_room_directory_visibility.assert_not_awaited()


@pytest.mark.asyncio
async def test_set_room_join_rule_logs_actionable_permission_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """Permission failures should log actionable guidance for join-rule updates."""
    mock_client = AsyncMock()
    mock_client.room_put_state.return_value = nio.RoomPutStateError("Not allowed", "M_FORBIDDEN")

    warning = MagicMock()
    monkeypatch.setattr(matrix_client.logger, "warning", warning)

    result = await matrix_client._set_room_join_rule(mock_client, "!room:example.com", "public")

    assert result is False
    assert warning.call_count == 1
    _, kwargs = warning.call_args
    assert "service account" in kwargs["hint"]


@pytest.mark.asyncio
async def test_ensure_room_has_topic_repairs_missing_state_even_when_cache_is_stale() -> None:
    """Topic generation should repair missing state instead of trusting cached room metadata."""
    mock_client = AsyncMock()
    room = MagicMock(spec=nio.MatrixRoom)
    room.topic = "Existing topic"
    mock_client.rooms = {"!room:example.com": room}
    mock_client.room_get_state_event.return_value = nio.RoomGetStateEventResponse(
        content={},
        event_type="m.room.topic",
        state_key="",
        room_id="!room:example.com",
    )
    mock_client.room_put_state.return_value = nio.RoomPutStateResponse.from_dict(
        {"event_id": "$topic"},
        room_id="!room:example.com",
    )

    with patch("mindroom.topic_generator.generate_room_topic_ai", new=AsyncMock(return_value="Fresh topic")):
        result = await topic_generator.ensure_room_has_topic(
            mock_client,
            "!room:example.com",
            "lobby",
            "Lobby",
            MagicMock(),
            MagicMock(),
        )

    assert result is True
    mock_client.room_get_state_event.assert_awaited_once_with("!room:example.com", "m.room.topic")
    mock_client.room_put_state.assert_awaited_once_with(
        room_id="!room:example.com",
        event_type="m.room.topic",
        content={"topic": "Fresh topic"},
    )


@pytest.mark.asyncio
async def test_ensure_room_has_topic_falls_back_to_state_event_when_room_missing() -> None:
    """Topic generation should still read room state when the nio room cache is empty."""
    mock_client = AsyncMock()
    mock_client.rooms = {}
    mock_client.room_get_state_event.return_value = nio.RoomGetStateEventResponse(
        content={"topic": "Existing topic"},
        event_type="m.room.topic",
        state_key="",
        room_id="!room:example.com",
    )

    result = await topic_generator.ensure_room_has_topic(
        mock_client,
        "!room:example.com",
        "lobby",
        "Lobby",
        MagicMock(),
        MagicMock(),
    )

    assert result is True
    mock_client.room_get_state_event.assert_awaited_once_with("!room:example.com", "m.room.topic")


@pytest.mark.asyncio
async def test_ensure_room_has_topic_falls_back_to_state_event_when_cached_topic_missing() -> None:
    """A cached room without a topic should still read the topic state event."""
    mock_client = AsyncMock()
    room = MagicMock(spec=nio.MatrixRoom)
    room.topic = None
    mock_client.rooms = {"!room:example.com": room}
    mock_client.room_get_state_event.return_value = nio.RoomGetStateEventResponse(
        content={"topic": "Existing topic"},
        event_type="m.room.topic",
        state_key="",
        room_id="!room:example.com",
    )

    result = await topic_generator.ensure_room_has_topic(
        mock_client,
        "!room:example.com",
        "lobby",
        "Lobby",
        MagicMock(),
        MagicMock(),
    )

    assert result is True
    mock_client.room_get_state_event.assert_awaited_once_with("!room:example.com", "m.room.topic")


@pytest.mark.asyncio
async def test_ensure_room_name_repairs_stale_cache_hit() -> None:
    """Room-name reconciliation should not trust a stale cached name match."""
    mock_client = AsyncMock()
    room = MagicMock(spec=nio.MatrixRoom)
    room.name = "Lobby"
    mock_client.rooms = {"!room:example.com": room}
    mock_client.room_get_state_event.return_value = nio.RoomGetStateEventResponse(
        content={"name": "Old lobby"},
        event_type="m.room.name",
        state_key="",
        room_id="!room:example.com",
    )
    mock_client.room_put_state.return_value = nio.RoomPutStateResponse.from_dict(
        {"event_id": "$name"},
        room_id="!room:example.com",
    )

    result = await matrix_client.ensure_room_name(mock_client, "!room:example.com", "Lobby")

    assert result is True
    mock_client.room_get_state_event.assert_awaited_once_with("!room:example.com", "m.room.name")
    mock_client.room_put_state.assert_awaited_once_with(
        room_id="!room:example.com",
        event_type="m.room.name",
        content={"name": "Lobby"},
    )


@pytest.mark.asyncio
async def test_ensure_room_name_falls_back_to_state_event_when_cached_name_missing() -> None:
    """A cached room without `room.name` should still read the state event before writing."""
    mock_client = AsyncMock()
    room = MagicMock(spec=nio.MatrixRoom)
    room.name = None
    mock_client.rooms = {"!room:example.com": room}
    mock_client.room_get_state_event.return_value = nio.RoomGetStateEventResponse(
        content={"name": "Lobby"},
        event_type="m.room.name",
        state_key="",
        room_id="!room:example.com",
    )

    result = await matrix_client.ensure_room_name(mock_client, "!room:example.com", "Lobby")

    assert result is True
    mock_client.room_get_state_event.assert_awaited_once_with("!room:example.com", "m.room.name")
    mock_client.room_put_state.assert_not_awaited()


@pytest.mark.asyncio
async def test_ensure_room_name_confirms_stale_cached_mismatch_before_writing() -> None:
    """A stale cached name should not trigger a write when the homeserver already matches."""
    mock_client = AsyncMock()
    room = MagicMock(spec=nio.MatrixRoom)
    room.name = "Old lobby"
    mock_client.rooms = {"!room:example.com": room}
    mock_client.room_get_state_event.return_value = nio.RoomGetStateEventResponse(
        content={"name": "Lobby"},
        event_type="m.room.name",
        state_key="",
        room_id="!room:example.com",
    )

    result = await matrix_client.ensure_room_name(mock_client, "!room:example.com", "Lobby")

    assert result is True
    mock_client.room_get_state_event.assert_awaited_once_with("!room:example.com", "m.room.name")
    mock_client.room_put_state.assert_not_awaited()


@pytest.mark.asyncio
async def test_get_room_name_rechecks_state_even_when_cache_has_name() -> None:
    """Room-name reads should prefer the homeserver over a potentially stale cache hit."""
    mock_client = AsyncMock()
    room = MagicMock(spec=nio.MatrixRoom)
    room.name = "Lobby"
    mock_client.rooms = {"!room:example.com": room}
    mock_client.room_get_state_event.return_value = nio.RoomGetStateEventResponse(
        content={"name": "Server lobby"},
        event_type="m.room.name",
        state_key="",
        room_id="!room:example.com",
    )

    result = await matrix_client.get_room_name(mock_client, "!room:example.com")

    assert result == "Server lobby"
    mock_client.room_get_state_event.assert_awaited_once_with("!room:example.com", "m.room.name")


@pytest.mark.asyncio
async def test_get_room_name_falls_back_to_state_event_when_cached_name_missing() -> None:
    """A cached room without `room.name` should still read the name state event."""
    mock_client = AsyncMock()
    room = MagicMock(spec=nio.MatrixRoom)
    room.name = None
    mock_client.rooms = {"!room:example.com": room}
    mock_client.room_get_state_event.return_value = nio.RoomGetStateEventResponse(
        content={"name": "Lobby"},
        event_type="m.room.name",
        state_key="",
        room_id="!room:example.com",
    )

    result = await matrix_client.get_room_name(mock_client, "!room:example.com")

    assert result == "Lobby"
    mock_client.room_get_state_event.assert_awaited_once_with("!room:example.com", "m.room.name")


@pytest.mark.asyncio
async def test_set_room_directory_visibility_logs_actionable_permission_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Permission failures should log actionable guidance for room directory updates."""
    mock_client = AsyncMock()
    mock_client.access_token = TEST_ACCESS_TOKEN
    mock_client.send.return_value = _FakeHttpResponse(
        status=403,
        body='{"errcode":"M_FORBIDDEN","error":"This server requires you to be a moderator in the room"}',
    )

    warning = MagicMock()
    monkeypatch.setattr(matrix_client.logger, "warning", warning)

    result = await matrix_client._set_room_directory_visibility(mock_client, "!room:example.com", "public")

    assert result is False
    assert warning.call_count == 1
    _, kwargs = warning.call_args
    assert kwargs["http_status"] == 403
    assert "moderator/admin" in kwargs["hint"]


@pytest.mark.asyncio
async def test_set_room_directory_visibility_releases_response_on_success() -> None:
    """Successful updates should release the underlying HTTP response."""
    mock_client = AsyncMock()
    mock_client.access_token = TEST_ACCESS_TOKEN
    response = _FakeHttpResponse(status=200, body="")
    mock_client.send.return_value = response

    result = await matrix_client._set_room_directory_visibility(mock_client, "!room:example.com", "public")

    assert result is True
    assert response.released is True


@pytest.mark.asyncio
async def test_existing_room_reconciliation_skipped_when_not_joined(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Reconciliation should not run when the service account cannot join the room."""
    config = _config_with_runtime_paths(
        tmp_path,
        matrix_room_access={
            "mode": "multi_user",
            "reconcile_existing_rooms": True,
        },
    )
    mock_client = AsyncMock()
    mock_client.homeserver = "https://example.com"
    mock_client.rooms = {}
    mock_client.room_resolve_alias.return_value = nio.RoomResolveAliasResponse(
        room_alias="#lobby:example.com",
        room_id="!lobby:example.com",
        servers=["example.com"],
    )

    monkeypatch.setattr(matrix_rooms, "load_rooms", dict)
    monkeypatch.setattr(matrix_rooms, "_add_room", MagicMock())
    monkeypatch.setattr(matrix_rooms, "get_joined_rooms", AsyncMock(return_value=[]))
    monkeypatch.setattr(matrix_rooms, "ensure_room_has_topic", AsyncMock())
    ensure_thread_tags_power_level = AsyncMock(return_value=True)
    ensure_scheduled_task_power_level_removed = AsyncMock(return_value=True)
    monkeypatch.setattr(
        matrix_rooms,
        "ensure_thread_tags_power_level",
        ensure_thread_tags_power_level,
    )
    monkeypatch.setattr(
        matrix_rooms,
        "ensure_scheduled_task_power_level_removed",
        ensure_scheduled_task_power_level_removed,
    )
    configure_access = AsyncMock(return_value=True)
    monkeypatch.setattr(matrix_rooms, "_configure_managed_room_access", configure_access)

    room_id = await matrix_rooms._ensure_room_exists(
        client=mock_client,
        room_key="lobby",
        config=config,
        runtime_paths=runtime_paths_for(config),
        room_name="Lobby",
        power_users=[],
    )

    assert room_id == "!lobby:example.com"
    ensure_thread_tags_power_level.assert_not_awaited()
    ensure_scheduled_task_power_level_removed.assert_not_awaited()
    configure_access.assert_not_awaited()


@pytest.mark.asyncio
async def test_configure_managed_room_access_partial_failure_surfaces_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Partial failure should log an error with actionable detail, not just a boolean summary."""
    config = _config_with_runtime_paths(
        Path(),
        matrix_room_access={
            "mode": "multi_user",
            "multi_user_join_rule": "public",
            "publish_to_room_directory": True,
        },
    )
    mock_client = AsyncMock()
    monkeypatch.setattr(matrix_rooms, "ensure_room_join_rule", AsyncMock(return_value=True))
    monkeypatch.setattr(matrix_rooms, "ensure_room_directory_visibility", AsyncMock(return_value=False))

    error_log = MagicMock()
    monkeypatch.setattr(matrix_rooms.logger, "error", error_log)

    result = await matrix_rooms._configure_managed_room_access(
        client=mock_client,
        room_key="lobby",
        room_id="!lobby:example.com",
        config=config,
        runtime_paths=runtime_paths_for(config),
        context="test",
    )

    assert result is False
    error_log.assert_called_once()
    _, kwargs = error_log.call_args
    assert kwargs["directory_visibility_success"] is False
    assert kwargs["join_rule_success"] is True
    assert "directory_visibility" in str(kwargs["failed_components"])
    assert "hint" in kwargs


@pytest.mark.asyncio
async def test_set_room_directory_visibility_releases_response_on_error() -> None:
    """Error responses should be released to avoid connection leaks."""
    mock_client = AsyncMock()
    mock_client.access_token = TEST_ACCESS_TOKEN
    response = _FakeHttpResponse(
        status=403,
        body='{"errcode":"M_FORBIDDEN","error":"Not allowed"}',
    )
    mock_client.send.return_value = response

    result = await matrix_client._set_room_directory_visibility(mock_client, "!room:example.com", "public")

    assert result is False
    assert response.released is True


@pytest.mark.asyncio
async def test_existing_room_reconciliation_runs_after_later_join(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Reconciliation should run on a later retry once the service account is joined."""
    config = _config_with_runtime_paths(
        tmp_path,
        matrix_room_access={
            "mode": "multi_user",
            "reconcile_existing_rooms": True,
        },
    )
    mock_client = AsyncMock()
    mock_client.homeserver = "https://example.com"
    mock_client.rooms = {}
    mock_client.room_resolve_alias.return_value = nio.RoomResolveAliasResponse(
        room_alias="#lobby:example.com",
        room_id="!lobby:example.com",
        servers=["example.com"],
    )

    monkeypatch.setattr(matrix_rooms, "load_rooms", dict)
    monkeypatch.setattr(matrix_rooms, "_add_room", MagicMock())
    monkeypatch.setattr(matrix_rooms, "get_joined_rooms", AsyncMock(side_effect=[[], ["!lobby:example.com"]]))
    ensure_room_has_topic = AsyncMock()
    monkeypatch.setattr(matrix_rooms, "ensure_room_has_topic", ensure_room_has_topic)
    ensure_thread_tags_power_level = AsyncMock(return_value=True)
    ensure_scheduled_task_power_level_removed = AsyncMock(return_value=True)
    monkeypatch.setattr(
        matrix_rooms,
        "ensure_thread_tags_power_level",
        ensure_thread_tags_power_level,
    )
    monkeypatch.setattr(
        matrix_rooms,
        "ensure_scheduled_task_power_level_removed",
        ensure_scheduled_task_power_level_removed,
    )
    configure_access = AsyncMock(return_value=True)
    monkeypatch.setattr(matrix_rooms, "_configure_managed_room_access", configure_access)

    first_room_id = await matrix_rooms._ensure_room_exists(
        client=mock_client,
        room_key="lobby",
        config=config,
        runtime_paths=runtime_paths_for(config),
        room_name="Lobby",
        power_users=[],
    )

    assert first_room_id == "!lobby:example.com"
    ensure_room_has_topic.assert_not_awaited()
    ensure_thread_tags_power_level.assert_not_awaited()
    ensure_scheduled_task_power_level_removed.assert_not_awaited()
    configure_access.assert_not_awaited()

    mock_client.rooms = {"!lobby:example.com": object()}

    second_room_id = await matrix_rooms._ensure_room_exists(
        client=mock_client,
        room_key="lobby",
        config=config,
        runtime_paths=runtime_paths_for(config),
        room_name="Lobby",
        power_users=[],
    )

    assert second_room_id == "!lobby:example.com"
    ensure_room_has_topic.assert_awaited_once_with(
        mock_client,
        "!lobby:example.com",
        "lobby",
        "Lobby",
        config,
        runtime_paths_for(config),
    )
    ensure_thread_tags_power_level.assert_awaited_once_with(
        mock_client,
        "!lobby:example.com",
    )
    ensure_scheduled_task_power_level_removed.assert_awaited_once_with(
        mock_client,
        "!lobby:example.com",
    )
    configure_access.assert_awaited_once_with(
        client=mock_client,
        room_key="lobby",
        room_id="!lobby:example.com",
        config=config,
        runtime_paths=runtime_paths_for(config),
        room_alias="#lobby:example.com",
        context="existing_room_reconciliation",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("membership", "expected_outcome"),
    [("join", "already_joined"), ("invite", "already_invited")],
)
async def test_invite_to_room_classifies_existing_membership_without_parsing_error_text(
    membership: str,
    expected_outcome: str,
) -> None:
    """Invite classification should use membership state instead of English error text."""
    mock_client = AsyncMock()
    mock_client.room_invite.return_value = nio.RoomInviteError("homeserver wording changed", "M_FORBIDDEN")
    mock_client.room_get_state_event.return_value = nio.RoomGetStateEventResponse(
        content={"membership": membership},
        event_type="m.room.member",
        state_key="@agent:example.com",
        room_id="!room:example.com",
    )

    result = await matrix_client.invite_to_room(mock_client, "!room:example.com", "@agent:example.com")

    assert result.outcome == expected_outcome
    assert result.error_code == "M_FORBIDDEN"
    mock_client.room_get_state_event.assert_awaited_once_with(
        "!room:example.com",
        "m.room.member",
        "@agent:example.com",
    )


@pytest.mark.asyncio
async def test_invite_to_room_handles_membership_probe_exception() -> None:
    """Invite fallback probes should not raise when the membership lookup itself fails."""
    mock_client = AsyncMock()
    mock_client.room_invite.return_value = nio.RoomInviteError("homeserver wording changed", "M_FORBIDDEN")
    mock_client.room_get_state_event.side_effect = RuntimeError("boom membership probe")

    result = await matrix_client.invite_to_room(mock_client, "!room:example.com", "@agent:example.com")

    assert result.outcome == "failed"
    assert result.error_code == "M_FORBIDDEN"


@pytest.mark.asyncio
async def test_configure_managed_room_access_respects_alias_invite_only(monkeypatch: pytest.MonkeyPatch) -> None:
    """Invite-only matching via room_alias should work when passed through configure_managed_room_access."""
    config = _config_with_runtime_paths(
        Path(),
        matrix_room_access={
            "mode": "multi_user",
            "multi_user_join_rule": "public",
            "publish_to_room_directory": True,
            "invite_only_rooms": ["#secret:example.com"],
        },
    )
    mock_client = AsyncMock()
    ensure_join_rule = AsyncMock(return_value=True)
    ensure_directory_visibility = AsyncMock(return_value=True)
    monkeypatch.setattr(matrix_rooms, "ensure_room_join_rule", ensure_join_rule)
    monkeypatch.setattr(matrix_rooms, "ensure_room_directory_visibility", ensure_directory_visibility)

    result = await matrix_rooms._configure_managed_room_access(
        client=mock_client,
        room_key="secret",
        room_id="!secret:example.com",
        config=config,
        runtime_paths=runtime_paths_for(config),
        room_alias="#secret:example.com",
        context="test",
    )

    assert result is True
    ensure_join_rule.assert_awaited_once_with(mock_client, "!secret:example.com", "invite")
    ensure_directory_visibility.assert_awaited_once_with(mock_client, "!secret:example.com", "private")


@pytest.mark.asyncio
async def test_ensure_all_rooms_exist_continues_after_room_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """A single room setup failure should not abort setup for remaining rooms."""
    config = _config_with_runtime_paths(Path())
    mock_client = AsyncMock()

    monkeypatch.setattr(Config, "get_all_configured_rooms", lambda _self: ["lobby", "ops"])
    monkeypatch.setattr("mindroom.agents.get_agent_ids_for_room", lambda _room_key, _config, _runtime_paths: [])

    async def _ensure_room_exists(*, room_key: str, **_kwargs: object) -> str:
        if room_key == "lobby":
            msg = "join failed"
            raise RuntimeError(msg)
        return "!ops:example.com"

    ensure_room_exists_mock = AsyncMock(side_effect=_ensure_room_exists)
    monkeypatch.setattr(matrix_rooms, "_ensure_room_exists", ensure_room_exists_mock)
    logger_exception = MagicMock()
    monkeypatch.setattr(matrix_rooms.logger, "exception", logger_exception)

    room_ids = await matrix_rooms.ensure_all_rooms_exist(mock_client, config, runtime_paths_for(config))
    assert room_ids == {"ops": "!ops:example.com"}
    assert ensure_room_exists_mock.await_count == 2
    logger_exception.assert_called_once()


@pytest.mark.asyncio
async def test_is_user_online_scans_cached_rooms_when_room_id_is_none() -> None:
    """Presence checks without a room hint should still use cached room membership first."""
    mock_client = AsyncMock(spec=nio.AsyncClient)
    mock_client.rooms = {
        "!room:example.com": MagicMock(
            room_id="!room:example.com",
            users={
                "@user:example.com": MagicMock(
                    presence="online",
                    last_active_ago=1000,
                ),
            },
        ),
    }

    result = await is_user_online(mock_client, "@user:example.com", room_id=None)

    assert result is True
    mock_client.get_presence.assert_not_called()

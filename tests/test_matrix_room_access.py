"""Tests for managed Matrix room access and discoverability settings."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock

import nio
import pytest

from mindroom.config import Config
from mindroom.matrix import client as matrix_client
from mindroom.matrix import rooms as matrix_rooms
from mindroom.matrix.state import MatrixRoom
from tests.conftest import TEST_ACCESS_TOKEN

if TYPE_CHECKING:
    from pathlib import Path


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


def test_matrix_room_access_defaults() -> None:
    """Matrix room access config should default to private/single-user behavior."""
    config = Config()

    assert config.matrix_room_access.mode == "single_user_private"
    assert config.matrix_room_access.multi_user_join_rule == "public"
    assert config.matrix_room_access.publish_to_room_directory is False
    assert config.matrix_room_access.invite_only_rooms == []
    assert config.matrix_room_access.reconcile_existing_rooms is False
    assert config.matrix_room_access.auto_invite_authorized_users is False


def test_matrix_room_access_yaml_null_uses_defaults(tmp_path: Path) -> None:
    """`matrix_room_access: null` should be treated the same as omitting the block."""
    config_path = tmp_path / "config.yaml"
    config_path.write_text("matrix_room_access: null\n", encoding="utf-8")

    config = Config.from_yaml(config_path)
    assert config.matrix_room_access.mode == "single_user_private"


def test_matrix_room_access_invite_only_matching() -> None:
    """Invite-only matching should work for room key, alias, and room ID."""
    config = Config(
        matrix_room_access={
            "mode": "multi_user",
            "invite_only_rooms": ["lobby", "#ops:example.com", "!secret:example.com"],
        },
    )
    access = config.matrix_room_access

    assert access.is_invite_only_room("lobby")
    assert access.is_invite_only_room("ops", room_alias="#ops:example.com")
    assert access.is_invite_only_room("random", room_id="!secret:example.com")
    assert not access.is_invite_only_room("public-room")


@pytest.mark.asyncio
async def test_configure_managed_room_access_public_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    """Multi-user mode should configure non-restricted rooms as joinable/publishable when enabled."""
    config = Config(
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

    result = await matrix_rooms.configure_managed_room_access(
        client=mock_client,
        room_key="lobby",
        room_id="!lobby:example.com",
        config=config,
        context="test",
    )

    assert result is True
    ensure_join_rule.assert_awaited_once_with(mock_client, "!lobby:example.com", "public")
    ensure_directory_visibility.assert_awaited_once_with(mock_client, "!lobby:example.com", "public")


@pytest.mark.asyncio
async def test_configure_managed_room_access_invite_only_override(monkeypatch: pytest.MonkeyPatch) -> None:
    """Invite-only room overrides should force invite/private targets even in multi-user mode."""
    config = Config(
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

    result = await matrix_rooms.configure_managed_room_access(
        client=mock_client,
        room_key="lobby",
        room_id="!lobby:example.com",
        config=config,
        context="test",
    )

    assert result is True
    ensure_join_rule.assert_awaited_once_with(mock_client, "!lobby:example.com", "invite")
    ensure_directory_visibility.assert_awaited_once_with(mock_client, "!lobby:example.com", "private")


@pytest.mark.asyncio
@pytest.mark.parametrize(("reconcile_existing", "expected_calls"), [(False, 0), (True, 1)])
async def test_existing_room_reconciliation_respects_flag(
    monkeypatch: pytest.MonkeyPatch,
    reconcile_existing: bool,
    expected_calls: int,
) -> None:
    """Existing room updates should be gated behind `reconcile_existing_rooms`."""
    config = Config(
        matrix_room_access={
            "mode": "multi_user",
            "reconcile_existing_rooms": reconcile_existing,
        },
    )
    mock_client = AsyncMock()
    mock_client.homeserver = "https://example.com"
    mock_client.room_resolve_alias.return_value = nio.RoomResolveAliasResponse(
        room_alias="#lobby:example.com",
        room_id="!lobby:example.com",
        servers=["example.com"],
    )

    monkeypatch.setattr(matrix_rooms, "load_rooms", dict)
    monkeypatch.setattr(matrix_rooms, "add_room", MagicMock())
    monkeypatch.setattr(matrix_rooms, "join_room", AsyncMock(return_value=True))
    monkeypatch.setattr(matrix_rooms, "ensure_room_has_topic", AsyncMock())
    configure_access = AsyncMock(return_value=True)
    monkeypatch.setattr(matrix_rooms, "configure_managed_room_access", configure_access)

    room_id = await matrix_rooms.ensure_room_exists(
        client=mock_client,
        room_key="lobby",
        config=config,
        room_name="Lobby",
        power_users=[],
    )

    assert room_id == "!lobby:example.com"
    assert configure_access.await_count == expected_calls


@pytest.mark.asyncio
async def test_new_room_creation_applies_access_policy_in_multi_user_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    """Newly created managed rooms should apply access policy when multi-user mode is enabled."""
    config = Config(
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
    monkeypatch.setattr(matrix_rooms, "add_room", MagicMock())
    configure_access = AsyncMock(return_value=True)
    monkeypatch.setattr(matrix_rooms, "configure_managed_room_access", configure_access)

    room_id = await matrix_rooms.ensure_room_exists(
        client=mock_client,
        room_key="lobby",
        config=config,
        room_name="Lobby",
        power_users=[],
    )

    assert room_id == "!lobby:example.com"
    configure_access.assert_awaited_once_with(
        client=mock_client,
        room_key="lobby",
        room_id="!lobby:example.com",
        config=config,
        room_alias="#lobby:example.com",
        context="new_room_creation",
    )


@pytest.mark.asyncio
async def test_ensure_room_join_rule_idempotent(monkeypatch: pytest.MonkeyPatch) -> None:
    """Join-rule reconciliation should be idempotent when already in desired state."""
    mock_client = AsyncMock()
    monkeypatch.setattr(matrix_client, "get_room_join_rule", AsyncMock(return_value="public"))
    set_room_join_rule = AsyncMock(return_value=True)
    monkeypatch.setattr(matrix_client, "set_room_join_rule", set_room_join_rule)

    result = await matrix_client.ensure_room_join_rule(mock_client, "!room:example.com", "public")

    assert result is True
    set_room_join_rule.assert_not_awaited()


@pytest.mark.asyncio
async def test_ensure_room_directory_visibility_idempotent(monkeypatch: pytest.MonkeyPatch) -> None:
    """Directory visibility reconciliation should be idempotent when already in desired state."""
    mock_client = AsyncMock()
    monkeypatch.setattr(matrix_client, "get_room_directory_visibility", AsyncMock(return_value="private"))
    set_room_directory_visibility = AsyncMock(return_value=True)
    monkeypatch.setattr(matrix_client, "set_room_directory_visibility", set_room_directory_visibility)

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

    result = await matrix_client.set_room_join_rule(mock_client, "!room:example.com", "public")

    assert result is False
    assert warning.call_count == 1
    _, kwargs = warning.call_args
    assert "service account" in kwargs["hint"]


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

    result = await matrix_client.set_room_directory_visibility(mock_client, "!room:example.com", "public")

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

    result = await matrix_client.set_room_directory_visibility(mock_client, "!room:example.com", "public")

    assert result is True
    assert response.released is True


@pytest.mark.asyncio
async def test_existing_room_reconciliation_skipped_when_not_joined(monkeypatch: pytest.MonkeyPatch) -> None:
    """Reconciliation should not run when the service account cannot join the room."""
    config = Config(
        matrix_room_access={
            "mode": "multi_user",
            "reconcile_existing_rooms": True,
        },
    )
    mock_client = AsyncMock()
    mock_client.homeserver = "https://example.com"
    mock_client.room_resolve_alias.return_value = nio.RoomResolveAliasResponse(
        room_alias="#lobby:example.com",
        room_id="!lobby:example.com",
        servers=["example.com"],
    )

    monkeypatch.setattr(matrix_rooms, "load_rooms", dict)
    monkeypatch.setattr(matrix_rooms, "add_room", MagicMock())
    monkeypatch.setattr(matrix_rooms, "join_room", AsyncMock(return_value=False))
    configure_access = AsyncMock(return_value=True)
    monkeypatch.setattr(matrix_rooms, "configure_managed_room_access", configure_access)

    room_id = await matrix_rooms.ensure_room_exists(
        client=mock_client,
        room_key="lobby",
        config=config,
        room_name="Lobby",
        power_users=[],
    )

    assert room_id == "!lobby:example.com"
    configure_access.assert_not_awaited()


@pytest.mark.asyncio
async def test_configure_managed_room_access_respects_alias_invite_only(monkeypatch: pytest.MonkeyPatch) -> None:
    """Invite-only matching via room_alias should work when passed through configure_managed_room_access."""
    config = Config(
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

    result = await matrix_rooms.configure_managed_room_access(
        client=mock_client,
        room_key="secret",
        room_id="!secret:example.com",
        config=config,
        room_alias="#secret:example.com",
        context="test",
    )

    assert result is True
    ensure_join_rule.assert_awaited_once_with(mock_client, "!secret:example.com", "invite")
    ensure_directory_visibility.assert_awaited_once_with(mock_client, "!secret:example.com", "private")


@pytest.mark.asyncio
async def test_auto_invite_authorized_users_invites_missing_members(monkeypatch: pytest.MonkeyPatch) -> None:
    """Auto-invite should invite authorized users who are not yet room members."""
    config = Config(
        matrix_room_access={
            "mode": "multi_user",
            "auto_invite_authorized_users": True,
            "invite_only_rooms": ["secret"],
        },
        authorization={
            "global_users": ["@alice:example.com"],
            "room_permissions": {"secret": ["@bob:example.com"]},
        },
    )
    mock_client = AsyncMock()

    managed_rooms = {
        "secret": MatrixRoom(room_id="!secret:example.com", alias="#secret:example.com", name="Secret"),
        "public": MatrixRoom(room_id="!public:example.com", alias="#public:example.com", name="Public"),
    }
    monkeypatch.setattr(matrix_rooms, "load_rooms", lambda: managed_rooms)

    get_members = AsyncMock(return_value={"@router:example.com"})
    monkeypatch.setattr(matrix_rooms, "get_room_members", get_members)
    invite = AsyncMock(return_value=True)
    monkeypatch.setattr(matrix_rooms, "invite_to_room", invite)

    await matrix_rooms.auto_invite_authorized_users(
        client=mock_client,
        joined_rooms=["!secret:example.com", "!public:example.com"],
        config=config,
    )

    invited_user_ids = sorted(call.args[2] for call in invite.call_args_list)
    assert invited_user_ids == ["@alice:example.com", "@bob:example.com"]
    assert all(call.args[1] == "!secret:example.com" for call in invite.call_args_list)


@pytest.mark.asyncio
async def test_auto_invite_skips_already_joined_members(monkeypatch: pytest.MonkeyPatch) -> None:
    """Auto-invite should not re-invite users who are already room members."""
    config = Config(
        matrix_room_access={
            "mode": "multi_user",
            "auto_invite_authorized_users": True,
            "invite_only_rooms": ["secret"],
        },
        authorization={"global_users": ["@alice:example.com"]},
    )
    mock_client = AsyncMock()

    managed_rooms = {
        "secret": MatrixRoom(room_id="!secret:example.com", alias="#secret:example.com", name="Secret"),
    }
    monkeypatch.setattr(matrix_rooms, "load_rooms", lambda: managed_rooms)
    monkeypatch.setattr(
        matrix_rooms,
        "get_room_members",
        AsyncMock(return_value={"@router:example.com", "@alice:example.com"}),
    )
    invite = AsyncMock(return_value=True)
    monkeypatch.setattr(matrix_rooms, "invite_to_room", invite)

    await matrix_rooms.auto_invite_authorized_users(
        client=mock_client,
        joined_rooms=["!secret:example.com"],
        config=config,
    )

    invite.assert_not_awaited()


@pytest.mark.asyncio
async def test_auto_invite_includes_room_permission_alias_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    """Auto-invite should include users granted via room_permissions alias keys."""
    config = Config(
        matrix_room_access={
            "mode": "multi_user",
            "auto_invite_authorized_users": True,
            "invite_only_rooms": ["secret"],
        },
        authorization={
            "room_permissions": {"#secret:example.com": ["@bob:example.com"]},
        },
    )
    mock_client = AsyncMock()

    managed_rooms = {
        "secret": MatrixRoom(room_id="!secret:example.com", alias="#secret:example.com", name="Secret"),
    }
    monkeypatch.setattr(matrix_rooms, "load_rooms", lambda: managed_rooms)
    monkeypatch.setattr(
        matrix_rooms,
        "get_room_members",
        AsyncMock(return_value={"@router:example.com"}),
    )
    invite = AsyncMock(return_value=True)
    monkeypatch.setattr(matrix_rooms, "invite_to_room", invite)

    await matrix_rooms.auto_invite_authorized_users(
        client=mock_client,
        joined_rooms=["!secret:example.com"],
        config=config,
    )

    invite.assert_awaited_once_with(mock_client, "!secret:example.com", "@bob:example.com")


@pytest.mark.asyncio
async def test_auto_invite_matches_invite_only_by_alias(monkeypatch: pytest.MonkeyPatch) -> None:
    """Auto-invite should match invite_only_rooms by alias when the alias is configured."""
    config = Config(
        matrix_room_access={
            "mode": "multi_user",
            "auto_invite_authorized_users": True,
            "invite_only_rooms": ["#secret:example.com"],
        },
        authorization={"global_users": ["@alice:example.com"]},
    )
    mock_client = AsyncMock()

    managed_rooms = {
        "secret": MatrixRoom(room_id="!secret:example.com", alias="#secret:example.com", name="Secret"),
    }
    monkeypatch.setattr(matrix_rooms, "load_rooms", lambda: managed_rooms)
    monkeypatch.setattr(
        matrix_rooms,
        "get_room_members",
        AsyncMock(return_value={"@router:example.com"}),
    )
    invite = AsyncMock(return_value=True)
    monkeypatch.setattr(matrix_rooms, "invite_to_room", invite)

    await matrix_rooms.auto_invite_authorized_users(
        client=mock_client,
        joined_rooms=["!secret:example.com"],
        config=config,
    )

    invite.assert_awaited_once_with(mock_client, "!secret:example.com", "@alice:example.com")

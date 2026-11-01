"""Tests for hook-facing Matrix admin helpers."""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import nio
import pytest

from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.config.models import ModelConfig
from mindroom.constants import ROUTER_AGENT_NAME
from mindroom.hooks import HookContext
from mindroom.hooks.matrix_admin import build_hook_matrix_admin
from mindroom.logging_config import get_logger
from mindroom.matrix.agent_message_snapshot import AgentMessageSnapshot
from mindroom.matrix.invited_rooms_store import invited_rooms_path, load_invited_rooms
from mindroom.matrix.room_cleanup import cleanup_all_orphaned_bots
from mindroom.matrix.state import MatrixState
from mindroom.matrix.users import AgentMatrixUser
from tests.bot_helpers import make_test_agent_bot
from tests.conftest import (
    TEST_PASSWORD,
    bind_runtime_paths,
    install_runtime_journal_support,
    runtime_paths_for,
    test_runtime_paths,
)
from tests.identity_helpers import entity_ids, persist_entity_accounts

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from pathlib import Path


def _config(tmp_path: Path) -> Config:
    runtime_paths = test_runtime_paths(tmp_path)
    return bind_runtime_paths(
        Config(models={"default": ModelConfig(provider="openai", id="test-model")}),
        runtime_paths,
    )


def _private_room_config(tmp_path: Path) -> Config:
    runtime_paths = test_runtime_paths(tmp_path)
    config = bind_runtime_paths(
        Config(
            agents={"general": AgentConfig(display_name="General", rooms=[])},
            models={"default": ModelConfig(provider="openai", id="test-model")},
        ),
        runtime_paths,
    )
    persist_entity_accounts(
        config,
        runtime_paths_for(config),
        usernames={ROUTER_AGENT_NAME: "mindroom_router", "general": "mindroom_general"},
    )
    return config


def _router_user(user_id: str) -> AgentMatrixUser:
    return AgentMatrixUser(
        agent_name=ROUTER_AGENT_NAME,
        user_id=user_id,
        display_name="Router",
        password=TEST_PASSWORD,
    )


def test_hooks_package_reexports_hook_matrix_admin_api() -> None:
    """The public hooks package should export the matrix admin hook API."""
    hooks = importlib.import_module("mindroom.hooks")

    assert hasattr(hooks, "HookMatrixAdmin")
    assert hasattr(hooks, "build_hook_matrix_admin")


@pytest.mark.asyncio
async def test_hook_context_delegates_latest_agent_message_snapshot_reads(tmp_path: Path) -> None:
    """HookContext should route latest-agent-message snapshot reads through the bound helper."""
    config = _config(tmp_path)
    reader = AsyncMock(
        return_value=AgentMessageSnapshot(
            content={"body": "Working...", "msgtype": "m.text"},
            origin_server_ts=2000,
        ),
    )
    context = HookContext(
        event_name="message:enrich",
        plugin_name="workloop",
        settings={},
        config=config,
        runtime_paths=runtime_paths_for(config),
        logger=get_logger("tests.hook_matrix_admin"),
        correlation_id="corr-snapshot",
        runtime_started_at=1234.0,
        agent_message_snapshot_reader=reader,
    )

    snapshot = await context.get_latest_agent_message_snapshot(
        "!room:localhost",
        "@agent:localhost",
        thread_id="$thread_root",
    )

    assert snapshot == AgentMessageSnapshot(
        content={"body": "Working...", "msgtype": "m.text"},
        origin_server_ts=2000,
    )
    reader.assert_awaited_once_with(
        room_id="!room:localhost",
        thread_id="$thread_root",
        sender="@agent:localhost",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("available", [True, False])
async def test_hook_matrix_admin_reads_bound_account_joined_rooms(tmp_path: Path, available: bool) -> None:
    """Plugins can distinguish current memberships from an unavailable snapshot."""
    client = AsyncMock(spec=nio.AsyncClient)
    client.joined_rooms.return_value = (
        nio.JoinedRoomsResponse(rooms=["!existing:localhost"])
        if available
        else nio.JoinedRoomsError("unavailable", status_code="M_UNKNOWN")
    )
    admin = build_hook_matrix_admin(client, runtime_paths=test_runtime_paths(tmp_path))

    assert await admin.get_joined_rooms() == (["!existing:localhost"] if available else None)


@pytest.mark.asyncio
async def test_build_hook_matrix_admin_resolve_alias_returns_room_id(tmp_path: Path) -> None:
    """Alias resolution should return the resolved room ID on success."""
    client = AsyncMock(spec=nio.AsyncClient)
    client.homeserver = "http://localhost:8008"
    client.room_resolve_alias.return_value = nio.RoomResolveAliasResponse(
        room_alias="#personal-user:localhost",
        room_id="!personal:localhost",
        servers=["localhost"],
    )

    admin = build_hook_matrix_admin(client, runtime_paths=test_runtime_paths(tmp_path))
    room_id = await admin.resolve_alias("#personal-user:localhost")

    assert room_id == "!personal:localhost"
    client.room_resolve_alias.assert_awaited_once_with("#personal-user:localhost")


@pytest.mark.asyncio
async def test_build_hook_matrix_admin_resolve_alias_returns_none_on_error(tmp_path: Path) -> None:
    """Alias resolution should fail closed on Matrix error responses."""
    client = AsyncMock(spec=nio.AsyncClient)
    client.homeserver = "http://localhost:8008"
    client.room_resolve_alias.return_value = nio.RoomResolveAliasError("not found", status_code="M_NOT_FOUND")

    admin = build_hook_matrix_admin(client, runtime_paths=test_runtime_paths(tmp_path))
    room_id = await admin.resolve_alias("#personal-user:localhost")

    assert room_id is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("response", "avatar"),
    [
        pytest.param(
            nio.ProfileGetResponse(displayname="Ada", avatar_url="mxc://localhost/ada"),
            "mxc://localhost/ada",
            id="content-uri",
        ),
        pytest.param(nio.ProfileGetResponse(displayname="Ada"), None, id="no-avatar"),
        pytest.param(nio.ProfileGetResponse(displayname="Ada", avatar_url=""), None, id="empty-avatar"),
        pytest.param(nio.ProfileGetError("not found", status_code="M_NOT_FOUND"), None, id="lookup-error"),
    ],
)
async def test_hook_matrix_admin_get_profile_avatar(
    tmp_path: Path,
    response: nio.ProfileGetResponse | nio.ProfileGetError,
    avatar: str | None,
) -> None:
    """Profile avatar reads expose the Matrix content URI; no avatar, an empty one, or a failed lookup stays unset."""
    client = AsyncMock(spec=nio.AsyncClient)
    client.homeserver = "http://localhost:8008"
    client.get_profile.return_value = response

    admin = build_hook_matrix_admin(client, runtime_paths=test_runtime_paths(tmp_path))

    assert await admin.get_profile_avatar("@ada:localhost") == avatar
    client.get_profile.assert_awaited_once_with("@ada:localhost")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("response", "result"),
    [
        pytest.param(
            nio.RoomGetStateEventResponse(
                content={"url": "mxc://localhost/ada"},
                event_type="m.room.avatar",
                state_key="",
                room_id="!personal:localhost",
            ),
            (True, {"url": "mxc://localhost/ada"}),
            id="content",
        ),
        pytest.param(
            nio.RoomGetStateEventError("missing", status_code="M_NOT_FOUND", room_id="!personal:localhost"),
            (True, None),
            id="missing",
        ),
        pytest.param(
            nio.RoomGetStateEventError("forbidden", status_code="M_FORBIDDEN", room_id="!personal:localhost"),
            (False, None),
            id="failed",
        ),
        pytest.param(
            nio.RoomGetStateEventResponse(
                content=["not", "an", "object"],
                event_type="m.room.avatar",
                state_key="",
                room_id="!personal:localhost",
            ),
            (False, None),
            id="malformed-content",
        ),
    ],
)
async def test_hook_matrix_admin_get_room_state_event(
    tmp_path: Path,
    response: nio.RoomGetStateEventResponse | nio.RoomGetStateEventError,
    result: tuple[bool, dict[str, object] | None],
) -> None:
    """Single-event reads expose content, confirm a missing event, and fail closed on a failed read or non-object content."""
    client = AsyncMock(spec=nio.AsyncClient)
    client.homeserver = "http://localhost:8008"
    client.room_get_state_event.return_value = response

    admin = build_hook_matrix_admin(client, runtime_paths=test_runtime_paths(tmp_path))

    assert await admin.get_room_state_event("!personal:localhost", "m.room.avatar", "") == result
    client.room_get_state_event.assert_awaited_once_with("!personal:localhost", "m.room.avatar", "")


@pytest.mark.asyncio
async def test_build_hook_matrix_admin_delegates_existing_room_helpers(tmp_path: Path) -> None:
    """The hook builder should reuse the existing Matrix helper functions."""
    runtime_paths = runtime_paths_for(_config(tmp_path))
    client = AsyncMock(spec=nio.AsyncClient)
    client.homeserver = "http://localhost:8008"

    with (
        patch(
            "mindroom.hooks.matrix_admin.create_room",
            new=AsyncMock(return_value="!created:localhost"),
        ) as mock_create,
        patch("mindroom.hooks.matrix_admin.invite_to_room", new=AsyncMock(return_value=True)) as mock_invite,
        patch(
            "mindroom.hooks.matrix_admin.get_room_members",
            new=AsyncMock(return_value={"@user:localhost", "@mindroom_router:localhost"}),
        ) as mock_members,
        patch("mindroom.hooks.matrix_admin.add_room_to_space", new=AsyncMock(return_value=True)) as mock_add,
    ):
        admin = build_hook_matrix_admin(client, runtime_paths=runtime_paths)

        room_id = await admin.create_room(name="Personal Room", alias_localpart="personal-user", topic="Hello")
        invited = await admin.invite_user("!created:localhost", "@user:localhost")
        client.room_kick.return_value = nio.RoomKickResponse()
        kicked = await admin.kick_user(
            "!created:localhost",
            "@user:localhost",
            reason="Reset membership",
        )
        members = await admin.get_room_members("!created:localhost")
        added = await admin.add_room_to_space("!space:localhost", "!created:localhost")
        client.room_put_state.return_value = nio.RoomPutStateResponse.from_dict(
            {"event_id": "$state"},
            room_id="!created:localhost",
        )
        wrote_state = await admin.put_room_state(
            "!created:localhost",
            "com.mindroom.scheduled.task",
            "task123",
            {"status": "pending"},
        )

    assert room_id == "!created:localhost"
    assert invited is True
    assert kicked is True
    assert members == {"@user:localhost", "@mindroom_router:localhost"}
    assert added is True
    assert wrote_state is True
    mock_create.assert_awaited_once_with(
        client=client,
        name="Personal Room",
        alias="personal-user",
        topic="Hello",
        power_users=None,
    )
    mock_invite.assert_awaited_once_with(client, "!created:localhost", "@user:localhost")
    client.room_kick.assert_awaited_once_with(
        "!created:localhost",
        "@user:localhost",
        reason="Reset membership",
    )
    client.room_put_state.assert_awaited_once_with(
        room_id="!created:localhost",
        event_type="com.mindroom.scheduled.task",
        content={"status": "pending"},
        state_key="task123",
    )
    mock_members.assert_awaited_once_with(client, "!created:localhost")
    mock_add.assert_awaited_once()


@pytest.mark.asyncio
async def test_hook_matrix_admin_kick_user_returns_false_on_error(tmp_path: Path) -> None:
    """User kicks should fail closed on Matrix error responses."""
    client = AsyncMock(spec=nio.AsyncClient)
    client.homeserver = "http://localhost:8008"
    client.room_kick.return_value = nio.RoomKickError("forbidden", status_code="M_FORBIDDEN")

    admin = build_hook_matrix_admin(client, runtime_paths=test_runtime_paths(tmp_path))

    assert await admin.kick_user("!room:localhost", "@user:localhost") is False
    client.room_kick.assert_awaited_once_with("!room:localhost", "@user:localhost", reason=None)


@pytest.mark.asyncio
async def test_hook_matrix_admin_force_join_user_calls_synapse_admin_api(tmp_path: Path) -> None:
    """Force-join should invite first so Tuwunel can join a private room."""
    client = AsyncMock(spec=nio.AsyncClient)
    client.homeserver = "http://localhost:8008"
    client.access_token = TEST_PASSWORD
    response = MagicMock(status=200)
    client.send.return_value = response

    admin = build_hook_matrix_admin(client, runtime_paths=test_runtime_paths(tmp_path))

    with patch("mindroom.hooks.matrix_admin.invite_to_room", new=AsyncMock(return_value=True)) as mock_invite:
        assert await admin.force_join_user("!personal:localhost", "@user:localhost") is True

    mock_invite.assert_awaited_once_with(client, "!personal:localhost", "@user:localhost")
    client.send.assert_awaited_once_with(
        "POST",
        "/_synapse/admin/v1/join/%21personal%3Alocalhost",
        data='{"user_id": "@user:localhost"}',
        headers={
            "Authorization": f"Bearer {TEST_PASSWORD}",
            "Content-Type": "application/json",
        },
    )
    response.release.assert_called_once_with()


@pytest.mark.asyncio
async def test_hook_matrix_admin_force_join_user_fails_without_access_token(tmp_path: Path) -> None:
    """Force-join should fail closed when no authenticated admin token exists."""
    client = AsyncMock(spec=nio.AsyncClient)
    client.homeserver = "http://localhost:8008"
    client.access_token = None

    admin = build_hook_matrix_admin(client, runtime_paths=test_runtime_paths(tmp_path))

    assert await admin.force_join_user("!personal:localhost", "@user:localhost") is False
    client.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_hook_matrix_admin_invite_user_with_config_delegates_to_raw_invite(tmp_path: Path) -> None:
    """Single-user invite should not run managed private-room reconciliation."""
    config = _private_room_config(tmp_path)
    runtime_paths = runtime_paths_for(config)
    client = AsyncMock(spec=nio.AsyncClient)
    client.homeserver = "http://localhost:8008"

    with patch("mindroom.hooks.matrix_admin.invite_to_room", new=AsyncMock(return_value=False)) as mock_invite:
        admin = build_hook_matrix_admin(client, runtime_paths=runtime_paths, config=config)
        invited = await admin.invite_user("!created:localhost", "@user:localhost")

    assert invited is False
    client.joined_members.assert_not_awaited()
    mock_invite.assert_awaited_once_with(client, "!created:localhost", "@user:localhost")
    assert load_invited_rooms(invited_rooms_path(runtime_paths, ROUTER_AGENT_NAME)) == set()
    assert load_invited_rooms(invited_rooms_path(runtime_paths, "general")) == set()


@pytest.mark.asyncio
async def test_hook_matrix_admin_create_room_persists_room_for_managed_creator(tmp_path: Path) -> None:
    """A managed bot's created room is recorded so lifecycle cleanup preserves it for the creator."""
    config = _private_room_config(tmp_path)
    runtime_paths = runtime_paths_for(config)
    ids = entity_ids(config, runtime_paths)
    client = AsyncMock(spec=nio.AsyncClient)
    client.homeserver = "http://localhost:8008"
    client.user_id = ids[ROUTER_AGENT_NAME].full_id

    with patch("mindroom.hooks.matrix_admin.create_room", new=AsyncMock(return_value="!private:localhost")):
        admin = build_hook_matrix_admin(client, runtime_paths=runtime_paths, config=config)
        room_id = await admin.create_room(name="Private Room", alias_localpart="private-user")

    assert room_id == "!private:localhost"
    assert "!private:localhost" in load_invited_rooms(
        invited_rooms_path(runtime_paths, ROUTER_AGENT_NAME),
    )
    # Only the creator records the room; invited bots rely on the invite-accept lifecycle instead.
    assert load_invited_rooms(invited_rooms_path(runtime_paths, "general")) == set()


@pytest.mark.asyncio
async def test_hook_matrix_admin_create_room_does_not_persist_for_unmanaged_creator(tmp_path: Path) -> None:
    """A non-managed creator records nothing, even when config is present."""
    config = _private_room_config(tmp_path)
    runtime_paths = runtime_paths_for(config)
    client = AsyncMock(spec=nio.AsyncClient)
    client.homeserver = "http://localhost:8008"
    client.user_id = "@stranger:localhost"

    with patch("mindroom.hooks.matrix_admin.create_room", new=AsyncMock(return_value="!private:localhost")):
        admin = build_hook_matrix_admin(client, runtime_paths=runtime_paths, config=config)
        room_id = await admin.create_room(name="Private Room", alias_localpart="private-user")

    assert room_id == "!private:localhost"
    assert load_invited_rooms(invited_rooms_path(runtime_paths, ROUTER_AGENT_NAME)) == set()
    assert load_invited_rooms(invited_rooms_path(runtime_paths, "general")) == set()


@pytest.mark.asyncio
@pytest.mark.parametrize("new_room", [True, False])
async def test_hook_matrix_admin_created_room_survives_lifecycle_cleanup(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    new_room: bool,
) -> None:
    """New and reconciled plugin rooms must survive the bot's own cleanup."""
    config = _private_room_config(tmp_path)
    runtime_paths = runtime_paths_for(config)
    ids = entity_ids(config, runtime_paths)
    client = AsyncMock(spec=nio.AsyncClient)
    client.homeserver = "http://localhost:8008"
    client.user_id = ids[ROUTER_AGENT_NAME].full_id

    # Startup sweeps before plugins reconcile existing rooms. The active bot
    # must keep control of its own membership even without a retention record.
    with (
        patch("mindroom.matrix.room_cleanup.get_joined_rooms", return_value=["!private:localhost"]),
        patch("mindroom.matrix.room_cleanup.get_room_members", return_value={client.user_id}),
        patch("mindroom.matrix.room_cleanup.is_dm_room", return_value=False),
    ):
        assert await cleanup_all_orphaned_bots(client, config, runtime_paths) == {}
    client.room_leave.assert_not_awaited()
    client.room_kick.assert_not_awaited()

    with patch("mindroom.hooks.matrix_admin.create_room", new=AsyncMock(return_value="!private:localhost")):
        admin = build_hook_matrix_admin(client, runtime_paths=runtime_paths, config=config)
        if new_room:
            await admin.create_room(name="Private Room", alias_localpart="private-user")
        else:
            admin.retain_room("!private:localhost")

    bot = make_test_agent_bot(
        agent_user=_router_user(ids[ROUTER_AGENT_NAME].full_id),
        config=config,
        runtime_paths=runtime_paths,
        rooms=[],
    )
    install_runtime_journal_support(bot)
    bot.client = AsyncMock()
    left_room_ids: list[str] = []

    async def mock_leave_non_dm_rooms(
        _client: AsyncMock,
        room_ids: list[str],
        *,
        leave_room_action: Callable[[str], Awaitable[bool]],
    ) -> list[str]:
        assert callable(leave_room_action)
        left_room_ids.extend(room_ids)
        return room_ids

    monkeypatch.setattr(
        "mindroom.bot_room_lifecycle.get_joined_rooms",
        AsyncMock(return_value=["!private:localhost", "!old:localhost"]),
    )
    monkeypatch.setattr("mindroom.bot_room_lifecycle.leave_non_dm_rooms", mock_leave_non_dm_rooms)
    monkeypatch.setattr("mindroom.bot_room_lifecycle.matrix_state_for_runtime", lambda *_args, **_kwargs: MatrixState())

    await bot.leave_unconfigured_rooms()

    assert bot._room_lifecycle.invited_room_ids() == {"!private:localhost"}
    assert left_room_ids == ["!old:localhost"]


def test_a_room_a_plugin_retains_for_the_running_router_owns_its_approvals_at_once(tmp_path: Path) -> None:
    """Approval transport sees a retained room as soon as it is retained, not at the next room setup."""
    config = _private_room_config(tmp_path)
    runtime_paths = runtime_paths_for(config)
    router_id = entity_ids(config, runtime_paths)[ROUTER_AGENT_NAME].full_id
    bot = make_test_agent_bot(
        agent_user=_router_user(router_id),
        config=config,
        runtime_paths=runtime_paths,
        rooms=[],
    )
    assert "!private:localhost" not in bot.approval_room_ids
    client = AsyncMock(spec=nio.AsyncClient)
    client.user_id = router_id
    admin = build_hook_matrix_admin(client, runtime_paths=runtime_paths, config=config)

    admin.retain_room("!private:localhost")

    assert "!private:localhost" in bot.approval_room_ids


def test_hook_matrix_admin_retention_preserves_other_rooms(tmp_path: Path) -> None:
    """Reconciliation adds to durable retention without dropping earlier rooms."""
    config = _private_room_config(tmp_path)
    runtime_paths = runtime_paths_for(config)
    client = AsyncMock(spec=nio.AsyncClient)
    client.user_id = entity_ids(config, runtime_paths)[ROUTER_AGENT_NAME].full_id
    admin = build_hook_matrix_admin(client, runtime_paths=runtime_paths, config=config)

    for room_id in ("!first:localhost", "!second:localhost", "!first:localhost"):
        admin.retain_room(room_id)

    assert load_invited_rooms(invited_rooms_path(runtime_paths, ROUTER_AGENT_NAME)) == {
        "!first:localhost",
        "!second:localhost",
    }
    assert load_invited_rooms(invited_rooms_path(runtime_paths, "general")) == set()


def test_hook_matrix_admin_retention_surfaces_storage_failure(tmp_path: Path) -> None:
    """A failed retention write must not let a plugin report reconciliation success."""
    config = _private_room_config(tmp_path)
    runtime_paths = runtime_paths_for(config)
    client = AsyncMock(spec=nio.AsyncClient)
    client.user_id = entity_ids(config, runtime_paths)[ROUTER_AGENT_NAME].full_id
    admin = build_hook_matrix_admin(client, runtime_paths=runtime_paths, config=config)

    with (
        patch("mindroom.matrix.invited_rooms_store._save_invited_rooms", return_value=False),
        pytest.raises(OSError, match="retain invited room"),
    ):
        admin.retain_room("!private:localhost")

    assert load_invited_rooms(invited_rooms_path(runtime_paths, ROUTER_AGENT_NAME)) == set()


@pytest.mark.parametrize(("managed", "accept_invites"), [(False, True), (True, False)])
def test_hook_matrix_admin_retention_respects_entity_policy(
    tmp_path: Path,
    managed: bool,
    accept_invites: bool,
) -> None:
    """Retention cannot enroll unmanaged accounts or override disabled invites."""
    config = _private_room_config(tmp_path)
    config.router.accept_invites = accept_invites
    runtime_paths = runtime_paths_for(config)
    client = AsyncMock(spec=nio.AsyncClient)
    client.user_id = entity_ids(config, runtime_paths)[ROUTER_AGENT_NAME].full_id if managed else "@outsider:localhost"
    admin = build_hook_matrix_admin(client, runtime_paths=runtime_paths, config=config)

    admin.retain_room("!private:localhost")

    assert load_invited_rooms(invited_rooms_path(runtime_paths, ROUTER_AGENT_NAME)) == set()

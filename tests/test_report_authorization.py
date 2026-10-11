"""Tests for exact-room report authorization against synced Matrix membership."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import nio
import pytest

from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.config.models import ModelConfig
from mindroom.entity_resolution import entity_identity_registry
from mindroom.orchestration.report_authorization_runtime import _OriginRoomReportAuthorizer
from mindroom.report_publishing.authorization import ReportAuthorizationReason
from mindroom.report_publishing.store import OriginRoomBinding
from tests.conftest import bind_runtime_paths, runtime_paths_for, test_runtime_paths

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.matrix.identity import MatrixID

_ORIGIN_ROOM_ID = "!origin:localhost"
_VIEWER_ID = "@alice:localhost"


@dataclass
class _FakeBot:
    """Minimal live bot surface used by report authorization."""

    client: AsyncMock | None
    running: bool
    matrix_id: MatrixID


def _config(tmp_path: Path) -> Config:
    runtime_paths = test_runtime_paths(tmp_path)
    return bind_runtime_paths(
        Config(
            agents={"general": AgentConfig(display_name="General")},
            models={"default": ModelConfig(provider="openai", id="gpt-6-astra")},
        ),
        runtime_paths,
    )


def _publisher_id(config: Config) -> MatrixID:
    return entity_identity_registry(config, runtime_paths_for(config)).current_id("general")


def _origin_room(config: Config, **changes: str) -> OriginRoomBinding:
    origin_room = OriginRoomBinding(
        room_id=_ORIGIN_ROOM_ID,
        publisher_entity_name="general",
        publisher_matrix_user_id=_publisher_id(config).full_id,
    )
    return replace(origin_room, **changes)


def _room(config: Config, members: set[str], *, members_synced: bool = True) -> nio.MatrixRoom:
    room = nio.MatrixRoom(_ORIGIN_ROOM_ID, _publisher_id(config).full_id)
    for user_id in members:
        room.add_member(user_id, None, None)
    room.members_synced = members_synced
    return room


def _authorizer(
    config: Config,
    rooms: dict[str, nio.MatrixRoom],
) -> tuple[_OriginRoomReportAuthorizer, AsyncMock]:
    client = AsyncMock(spec=nio.AsyncClient)
    client.rooms = rooms
    bot = _FakeBot(client=client, running=True, matrix_id=_publisher_id(config))
    authorizer = _OriginRoomReportAuthorizer(
        config=config,
        bots={"general": bot},  # type: ignore[dict-item]
        runtime_paths=runtime_paths_for(config),
    )
    return authorizer, client


@pytest.mark.asyncio
async def test_origin_room_authorization_allows_exact_joined_room(tmp_path: Path) -> None:
    """Viewer and publisher currently joined to exact origin room should pass."""
    config = _config(tmp_path)
    room = _room(config, {_publisher_id(config).full_id, _VIEWER_ID})
    authorizer, client = _authorizer(config, {_ORIGIN_ROOM_ID: room})

    reason = await authorizer.authorize(_origin_room(config), _VIEWER_ID)

    assert reason is ReportAuthorizationReason.AUTHORIZED
    client.joined_members.assert_not_awaited()


@pytest.mark.asyncio
async def test_origin_room_authorization_rejects_invited_viewer(tmp_path: Path) -> None:
    """An invite is not current joined membership."""
    config = _config(tmp_path)
    room = _room(config, {_publisher_id(config).full_id})
    room.add_member(_VIEWER_ID, None, None, invited=True)
    authorizer, _client = _authorizer(config, {_ORIGIN_ROOM_ID: room})

    reason = await authorizer.authorize(_origin_room(config), _VIEWER_ID)

    assert reason is ReportAuthorizationReason.VIEWER_NOT_JOINED


@pytest.mark.asyncio
async def test_origin_room_authorization_applies_synced_departure_immediately(tmp_path: Path) -> None:
    """A synced leave should revoke access on the next request without a cache window."""
    config = _config(tmp_path)
    room = _room(config, {_publisher_id(config).full_id, _VIEWER_ID})
    authorizer, _client = _authorizer(config, {_ORIGIN_ROOM_ID: room})

    before = await authorizer.authorize(_origin_room(config), _VIEWER_ID)
    room.remove_member(_VIEWER_ID)
    after = await authorizer.authorize(_origin_room(config), _VIEWER_ID)

    assert before is ReportAuthorizationReason.AUTHORIZED
    assert after is ReportAuthorizationReason.VIEWER_NOT_JOINED


@pytest.mark.asyncio
async def test_origin_room_authorization_rejects_room_the_publisher_left(tmp_path: Path) -> None:
    """Sharing another room with the publisher must not authorize the origin room."""
    config = _config(tmp_path)
    other_room = nio.MatrixRoom("!other:localhost", _publisher_id(config).full_id)
    other_room.add_member(_VIEWER_ID, None, None)
    authorizer, _client = _authorizer(config, {"!other:localhost": other_room})

    reason = await authorizer.authorize(_origin_room(config), _VIEWER_ID)

    assert reason is ReportAuthorizationReason.PUBLISHER_NOT_JOINED


@pytest.mark.asyncio
async def test_origin_room_authorization_rejects_stored_publisher_identity_mismatch(tmp_path: Path) -> None:
    """Stored publisher identity must match current configured runtime identity."""
    config = _config(tmp_path)
    room = _room(config, {_publisher_id(config).full_id, _VIEWER_ID})
    authorizer, _client = _authorizer(config, {_ORIGIN_ROOM_ID: room})

    reason = await authorizer.authorize(
        _origin_room(config, publisher_matrix_user_id="@old-general:localhost"),
        _VIEWER_ID,
    )

    assert reason is ReportAuthorizationReason.PUBLISHER_IDENTITY_MISMATCH


@pytest.mark.asyncio
@pytest.mark.parametrize("runtime_state", ["missing", "stopped", "client_missing"])
async def test_origin_room_authorization_treats_configured_publisher_outage_as_backend_unavailable(
    tmp_path: Path,
    runtime_state: str,
) -> None:
    """Configured publisher runtime outages should produce a retryable failure."""
    config = _config(tmp_path)
    publisher_bot = _FakeBot(
        client=None if runtime_state == "client_missing" else AsyncMock(spec=nio.AsyncClient),
        running=runtime_state != "stopped",
        matrix_id=_publisher_id(config),
    )
    bots = {} if runtime_state == "missing" else {"general": publisher_bot}
    authorizer = _OriginRoomReportAuthorizer(
        config=config,
        bots=bots,  # type: ignore[arg-type]
        runtime_paths=runtime_paths_for(config),
    )

    reason = await authorizer.authorize(_origin_room(config), _VIEWER_ID)

    assert reason is ReportAuthorizationReason.AUTHORIZATION_BACKEND_UNAVAILABLE


@pytest.mark.asyncio
async def test_origin_room_authorization_treats_removed_publisher_as_identity_mismatch(tmp_path: Path) -> None:
    """A publisher removed from current config should remain a denial, not an outage."""
    original_config = _config(tmp_path)
    runtime_paths = runtime_paths_for(original_config)
    current_config = bind_runtime_paths(
        Config(models={"default": ModelConfig(provider="openai", id="gpt-6-astra")}),
        runtime_paths,
    )
    authorizer = _OriginRoomReportAuthorizer(
        config=current_config,
        bots={},
        runtime_paths=runtime_paths,
    )

    reason = await authorizer.authorize(_origin_room(original_config), _VIEWER_ID)

    assert reason is ReportAuthorizationReason.PUBLISHER_IDENTITY_MISMATCH


@pytest.mark.asyncio
async def test_origin_room_authorization_completes_partial_membership_once(tmp_path: Path) -> None:
    """A lazily loaded member list is completed authoritatively and reused until membership changes."""
    config = _config(tmp_path)
    publisher_id = _publisher_id(config).full_id
    room = _room(config, {publisher_id}, members_synced=False)
    authorizer, client = _authorizer(config, {_ORIGIN_ROOM_ID: room})
    client.joined_members.return_value = nio.JoinedMembersResponse(
        [nio.RoomMember(publisher_id, "", ""), nio.RoomMember(_VIEWER_ID, "", "")],
        _ORIGIN_ROOM_ID,
    )

    first = await authorizer.authorize(_origin_room(config), _VIEWER_ID)
    second = await authorizer.authorize(_origin_room(config), _VIEWER_ID)

    assert first is ReportAuthorizationReason.AUTHORIZED
    assert second is ReportAuthorizationReason.AUTHORIZED
    client.joined_members.assert_awaited_once_with(_ORIGIN_ROOM_ID)


@pytest.mark.asyncio
async def test_origin_room_authorization_fails_closed_on_matrix_error(tmp_path: Path) -> None:
    """Matrix transport failures must never become successful authorization."""
    config = _config(tmp_path)
    room = _room(config, {_publisher_id(config).full_id, _VIEWER_ID}, members_synced=False)
    authorizer, client = _authorizer(config, {_ORIGIN_ROOM_ID: room})
    client.joined_members.side_effect = RuntimeError("homeserver unavailable")

    reason = await authorizer.authorize(_origin_room(config), _VIEWER_ID)

    assert reason is ReportAuthorizationReason.AUTHORIZATION_BACKEND_UNAVAILABLE

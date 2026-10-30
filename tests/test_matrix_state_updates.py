"""Concurrent writers of ``matrix_state.yaml`` keep each other's changes."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, patch

import nio
import pytest

from mindroom.config.main import Config
from mindroom.matrix import rooms as matrix_rooms
from mindroom.matrix import users as matrix_users
from mindroom.matrix.state import MatrixState
from tests.conftest import bind_runtime_paths, orchestrator_runtime_paths, runtime_paths_for

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.constants import RuntimePaths


def _space_config(tmp_path: Path) -> Config:
    return bind_runtime_paths(
        Config(agents={"general": {"display_name": "General", "rooms": ["lobby"]}}, matrix_space={"enabled": True}),
        orchestrator_runtime_paths(tmp_path, config_path=tmp_path / "config.yaml"),
    )


def _persist_login(runtime_paths: RuntimePaths, *, device_id: str, access_token: str) -> None:
    """Persist the device and token a bot writes when it logs in."""
    matrix_users._persist_agent_session(
        "general",
        "mindroom_general",
        "secret",
        domain="localhost",
        device_id=device_id,
        access_token=access_token,
        runtime_paths=runtime_paths,
    )


@pytest.mark.asyncio
async def test_root_space_setup_keeps_a_login_persisted_while_it_awaited(tmp_path: Path) -> None:
    """A bot that logs in while the root space is being created keeps its new device after a restart."""
    config = _space_config(tmp_path)
    runtime_paths = runtime_paths_for(config)
    _persist_login(runtime_paths, device_id="OLDDEVICE", access_token="old-token")  # noqa: S106
    client = AsyncMock()
    client.homeserver = "http://localhost:8008"
    client.rooms = {}
    client.room_resolve_alias.return_value = nio.RoomResolveAliasError("not found", status_code="M_NOT_FOUND")

    async def create_space_while_a_bot_logs_in(**_kwargs: object) -> str:
        _persist_login(runtime_paths, device_id="NEWDEVICE", access_token="new-token")  # noqa: S106
        return "!space:localhost"

    with (
        patch("mindroom.matrix.rooms.get_joined_rooms", new=AsyncMock(return_value=[])),
        patch("mindroom.matrix.rooms.create_space", new=create_space_while_a_bot_logs_in),
    ):
        space_id = await matrix_rooms._ensure_root_space_exists(client, config, runtime_paths)

    assert space_id == "!space:localhost"
    state = MatrixState.load(runtime_paths=runtime_paths)
    assert state.space_room_id == "!space:localhost"
    account = state.get_account("agent_general")
    assert account is not None
    assert (account.device_id, account.access_token) == ("NEWDEVICE", "new-token")


@pytest.mark.asyncio
async def test_resolved_root_space_keeps_a_login_persisted_while_it_awaited(tmp_path: Path) -> None:
    """Adopting an existing root space by alias does not overwrite a concurrent login either."""
    config = _space_config(tmp_path)
    runtime_paths = runtime_paths_for(config)
    client = AsyncMock()
    client.homeserver = "http://localhost:8008"
    client.rooms = {}

    async def resolve_alias_while_a_bot_logs_in(*_args: object, **_kwargs: object) -> str:
        _persist_login(runtime_paths, device_id="NEWDEVICE", access_token="new-token")  # noqa: S106
        return "!space:localhost"

    with (
        patch("mindroom.matrix.rooms.get_joined_rooms", new=AsyncMock(return_value=["!space:localhost"])),
        patch("mindroom.matrix.rooms._resolve_managed_alias", new=resolve_alias_while_a_bot_logs_in),
    ):
        space_id = await matrix_rooms._ensure_root_space_exists(client, config, runtime_paths)

    assert space_id == "!space:localhost"
    state = MatrixState.load(runtime_paths=runtime_paths)
    assert state.space_room_id == "!space:localhost"
    account = state.get_account("agent_general")
    assert account is not None
    assert account.device_id == "NEWDEVICE"

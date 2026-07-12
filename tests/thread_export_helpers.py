"""Shared fixtures for thread-export collaborator tests."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, Mock

from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.matrix.invited_rooms_store import invited_rooms_path
from mindroom.matrix.state import MatrixAccount, MatrixRoom, MatrixState
from mindroom.thread_export.models import ThreadExportAccumulator
from tests.conftest import bind_runtime_paths, test_runtime_paths

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    from mindroom.constants import RuntimePaths
    from mindroom.thread_export import ThreadExportTarget
    from mindroom.thread_export.models import ThreadExportRoom


def thread_export_config(tmp_path: Path) -> Config:
    """Return a minimal config bound to one test runtime."""
    return bind_runtime_paths(
        Config(agents={"general": AgentConfig(display_name="General Agent")}),
        test_runtime_paths(tmp_path),
    )


def write_thread_export_matrix_state(tmp_path: Path, *, account_keys: tuple[str, ...] = ()) -> None:
    """Persist two rooms and the requested Matrix account fixtures."""
    state = MatrixState()
    state.rooms = {
        "lobby": MatrixRoom(
            room_id="!lobby:localhost",
            alias="#lobby:localhost",
            name="Lobby",
        ),
        "dev": MatrixRoom(
            room_id="!dev:localhost",
            alias="#dev:localhost",
            name="Dev",
        ),
    }
    state.accounts = {
        account_key: MatrixAccount(
            username=account_key,
            password="pw",  # noqa: S106
            device_id="DEV",
            access_token="tok",  # noqa: S106
        )
        for account_key in account_keys
    }
    state.save(test_runtime_paths(tmp_path))


def write_invited_rooms(runtime_paths: RuntimePaths, entity_name: str, room_ids: list[str]) -> None:
    """Persist invited-room IDs for one managed entity."""
    path = invited_rooms_path(runtime_paths.storage_root, entity_name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(room_ids), encoding="utf-8")


def mock_runtime_support() -> Mock:
    """Return initialized-cache-shaped runtime support."""
    support = Mock()
    support.event_cache = Mock()
    support.event_cache.initialize = AsyncMock()
    return support


def successful_group_result(
    *,
    targets: Sequence[ThreadExportTarget],
    rooms: Sequence[ThreadExportRoom],
    **_kwargs: object,
) -> tuple[ThreadExportAccumulator, ...]:
    """Return one successful internal result per requested export target."""
    return tuple(
        ThreadExportAccumulator(
            target=target,
            rooms_exported=1,
            threads_exported=1,
            retained_room_keys={room.key for room in rooms},
        )
        for target in targets
    )

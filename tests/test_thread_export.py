"""Tests for Matrix thread export."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, Mock, patch

import pytest
import yaml

from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.matrix.client_visible_messages import ResolvedVisibleMessage
from mindroom.matrix.state import MatrixRoom, MatrixState
from mindroom.thread_export import (
    _export_rooms,
    _export_threads_for_client,
    _fsync_directory,
    _safe_path_segment,
    export_threads_once,
)
from tests.conftest import bind_runtime_paths, runtime_paths_for, test_runtime_paths

if TYPE_CHECKING:
    from pathlib import Path


def _config(tmp_path: Path) -> Config:
    return bind_runtime_paths(
        Config(agents={"general": AgentConfig(display_name="General Agent")}),
        test_runtime_paths(tmp_path),
    )


def _write_matrix_state(tmp_path: Path) -> None:
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
    state.save(test_runtime_paths(tmp_path))


def test_export_rooms_filters_by_room_metadata_substring(tmp_path: Path) -> None:
    """Room filtering should match substrings across user-facing room fields."""
    config = _config(tmp_path)
    runtime_paths = runtime_paths_for(config)
    _write_matrix_state(tmp_path)

    assert [room.key for room in _export_rooms(runtime_paths, "obb")] == ["lobby"]
    assert {room.key for room in _export_rooms(runtime_paths, "LOCALHOST")} == {"lobby", "dev"}


def test_fsync_directory_ignores_unsupported_directory_fsync(tmp_path: Path) -> None:
    """Directory fsync is a best-effort durability hint on filesystems that support it."""
    with (
        patch("mindroom.thread_export.os.open", return_value=123) as open_directory,
        patch("mindroom.thread_export.os.fsync", side_effect=OSError("unsupported")) as fsync_directory,
        patch("mindroom.thread_export.os.close") as close_directory,
    ):
        _fsync_directory(tmp_path)

    open_directory.assert_called_once()
    fsync_directory.assert_called_once_with(123)
    close_directory.assert_called_once_with(123)


def test_safe_path_segment_blocks_dot_directory_segments() -> None:
    """Path segments should not allow current or parent directory traversal."""
    assert _safe_path_segment(".") == "%2E"
    assert _safe_path_segment("..") == "%2E%2E"
    assert _safe_path_segment("%2E") == "%252E"


@pytest.mark.asyncio
async def test_export_threads_fetches_from_matrix_source_and_writes_yaml(tmp_path: Path) -> None:
    """Exporter should enumerate Matrix threads, fetch source history, and write grep-friendly YAML."""
    config = _config(tmp_path)
    runtime_paths = runtime_paths_for(config)
    _write_matrix_state(tmp_path)

    fetch_result = [
        ResolvedVisibleMessage.synthetic(
            sender="@alice:localhost",
            body="Root decision",
            timestamp=1_700_000_000_000,
            event_id="$thread/root:localhost",
            thread_id=None,
        ),
        ResolvedVisibleMessage.synthetic(
            sender="@mindroom_general:localhost",
            body="Follow-up details",
            timestamp=1_700_000_001_000,
            event_id="$reply:localhost",
            thread_id="$thread/root:localhost",
        ),
    ]

    with (
        patch(
            "mindroom.thread_export.enumerate_room_thread_root_ids",
            new=AsyncMock(return_value=(["$thread/root:localhost"], False)),
        ) as enumerate_threads,
        patch(
            "mindroom.thread_export.refresh_thread_history_from_source",
            new=AsyncMock(return_value=fetch_result),
        ) as fetch_thread,
    ):
        stats = await _export_threads_for_client(
            client=Mock(),
            config=config,
            runtime_paths=runtime_paths,
            event_cache=Mock(),
            output_dir=tmp_path / "exports",
            room_filter="lobby",
        )

    assert stats.rooms_exported == 1
    assert stats.threads_exported == 1
    assert stats.failures == 0
    enumerate_threads.assert_awaited_once()
    fetch_thread.assert_awaited_once()
    assert fetch_thread.await_args.kwargs["allow_stale_fallback"] is False

    exported_files = list((tmp_path / "exports" / "lobby").glob("*.yaml"))
    assert len(exported_files) == 1
    payload = yaml.safe_load(exported_files[0].read_text(encoding="utf-8"))
    assert payload["room"] == {
        "key": "lobby",
        "id": "!lobby:localhost",
        "name": "Lobby",
        "alias": "#lobby:localhost",
    }
    assert payload["thread"]["id"] == "$thread/root:localhost"
    assert payload["thread"]["source"] == "matrix"
    assert payload["messages"] == [
        {
            "event_id": "$thread/root:localhost",
            "latest_event_id": "$thread/root:localhost",
            "sender": "@alice:localhost",
            "timestamp": 1_700_000_000_000,
            "timestamp_iso": "2023-11-14T22:13:20+00:00",
            "body": "Root decision",
        },
        {
            "event_id": "$reply:localhost",
            "latest_event_id": "$reply:localhost",
            "sender": "@mindroom_general:localhost",
            "timestamp": 1_700_000_001_000,
            "timestamp_iso": "2023-11-14T22:13:21+00:00",
            "thread_id": "$thread/root:localhost",
            "body": "Follow-up details",
        },
    ]


@pytest.mark.asyncio
async def test_export_threads_continues_after_one_thread_failure(tmp_path: Path) -> None:
    """One failed thread should not stop other thread exports in the same room."""
    config = _config(tmp_path)
    runtime_paths = runtime_paths_for(config)
    _write_matrix_state(tmp_path)

    async def fetch_side_effect(*args: object, **_kwargs: object) -> list[ResolvedVisibleMessage]:
        thread_id = args[2]
        if thread_id == "$bad:localhost":
            msg = "fetch failed"
            raise RuntimeError(msg)
        return [
            ResolvedVisibleMessage.synthetic(
                sender="@alice:localhost",
                body="Good thread",
                event_id="$good:localhost",
            ),
        ]

    with (
        patch(
            "mindroom.thread_export.enumerate_room_thread_root_ids",
            new=AsyncMock(return_value=(["$bad:localhost", "$good:localhost"], False)),
        ),
        patch(
            "mindroom.thread_export.refresh_thread_history_from_source",
            new=AsyncMock(side_effect=fetch_side_effect),
        ),
    ):
        stats = await _export_threads_for_client(
            client=Mock(),
            config=config,
            runtime_paths=runtime_paths,
            event_cache=Mock(),
            output_dir=tmp_path / "exports",
            room_filter="lobby",
        )

    assert stats.threads_seen == 2
    assert stats.threads_exported == 1
    assert stats.failures == 1
    assert len(list((tmp_path / "exports" / "lobby").glob("*.yaml"))) == 1


@pytest.mark.asyncio
async def test_export_threads_counts_only_enumerated_rooms(tmp_path: Path) -> None:
    """rooms_exported should exclude rooms that fail before thread enumeration completes."""
    config = _config(tmp_path)
    runtime_paths = runtime_paths_for(config)
    _write_matrix_state(tmp_path)

    async def enumerate_side_effect(_client: object, room_id: str, **_kwargs: object) -> tuple[list[str], bool]:
        if room_id == "!lobby:localhost":
            msg = "enumeration failed"
            raise RuntimeError(msg)
        return [], False

    with patch(
        "mindroom.thread_export.enumerate_room_thread_root_ids",
        new=AsyncMock(side_effect=enumerate_side_effect),
    ):
        stats = await _export_threads_for_client(
            client=Mock(),
            config=config,
            runtime_paths=runtime_paths,
            event_cache=Mock(),
            output_dir=tmp_path / "exports",
        )

    assert stats.rooms_exported == 1
    assert stats.failures == 1
    assert stats.failed_items[0].room_key == "lobby"


@pytest.mark.asyncio
async def test_export_threads_once_closes_client_when_runtime_support_creation_fails(tmp_path: Path) -> None:
    """The Matrix client should close even when runtime support construction fails."""
    config = _config(tmp_path)
    runtime_paths = runtime_paths_for(config)
    client = Mock()
    client.close = AsyncMock()

    with (
        patch("mindroom.thread_export._select_export_account", return_value=Mock()),
        patch("mindroom.thread_export.login_agent_user", new=AsyncMock(return_value=client)),
        patch("mindroom.thread_export.build_owned_runtime_support", side_effect=RuntimeError("support failed")),
        patch("mindroom.thread_export.close_owned_runtime_support", new=AsyncMock()) as close_support,
        pytest.raises(RuntimeError, match="support failed"),
    ):
        await export_threads_once(config=config, runtime_paths=runtime_paths)

    client.close.assert_awaited_once()
    close_support.assert_not_awaited()

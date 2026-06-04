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
from mindroom.thread_export import _export_threads_for_client
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

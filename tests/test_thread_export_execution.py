"""Tests for per-client thread-export execution."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, Mock, patch
from urllib.parse import quote

import nio
import pytest
import yaml

from mindroom.matrix.client_visible_messages import ResolvedVisibleMessage
from mindroom.thread_export import ThreadExportTarget
from mindroom.thread_export import storage as thread_export_storage
from mindroom.thread_export.execution import (
    export_threads_for_targets_for_client as _export_threads_for_targets_for_client,
)
from mindroom.thread_export.models import (
    ThreadExportRoom as _ThreadExportRoom,
)
from mindroom.thread_export.selection import export_rooms as _export_rooms
from mindroom.thread_export.storage import write_room_index, write_thread_payload
from tests.conftest import runtime_paths_for
from tests.thread_export_helpers import (
    mark_thread_export_root,
)
from tests.thread_export_helpers import (
    thread_export_config as _config,
)
from tests.thread_export_helpers import (
    write_thread_export_matrix_state as _write_matrix_state,
)

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.thread_export import ThreadExportStats


async def _export_threads_for_client(
    *,
    client: nio.AsyncClient,
    config: Config,
    runtime_paths: RuntimePaths,
    rooms: Sequence[_ThreadExportRoom],
    output_dir: Path,
    max_thread_roots: int = 2000,
    required_member_user_id: str | None = None,
) -> ThreadExportStats:
    """Adapt the multi-target execution seam for single-target test cases."""
    accumulators = await _export_threads_for_targets_for_client(
        client=client,
        reader=Mock(),
        config=config,
        runtime_paths=runtime_paths,
        rooms=rooms,
        targets=(
            ThreadExportTarget(
                output_dir=output_dir,
                required_member_user_id=required_member_user_id,
            ),
        ),
        max_thread_roots=max_thread_roots,
    )
    return accumulators[0].stats()


@pytest.mark.asyncio
async def test_export_threads_fetches_from_matrix_source_and_writes_yaml(tmp_path: Path) -> None:
    """Exporter should enumerate Matrix threads, fetch source history, and write grep-friendly YAML."""
    config = _config(tmp_path)
    runtime_paths = runtime_paths_for(config)
    _write_matrix_state(tmp_path)

    edited_reply = ResolvedVisibleMessage.synthetic(
        sender="@mindroom_general:localhost",
        body="Follow-up details",
        timestamp=1_700_000_001_000,
        event_id="$reply:localhost",
        thread_id="$thread/root:localhost",
    )
    edited_reply.apply_edit(
        body="Revised follow-up details",
        timestamp=1_700_000_002_000,
        latest_event_id="$reply-edit:localhost",
        content={"body": "Revised follow-up details", "msgtype": "m.text"},
    )
    fetch_result = [
        ResolvedVisibleMessage.synthetic(
            sender="@alice:localhost",
            body="Root decision",
            timestamp=1_700_000_000_000,
            event_id="$thread/root:localhost",
            thread_id=None,
        ),
        edited_reply,
    ]

    with (
        patch(
            "mindroom.thread_export.execution.enumerate_room_thread_root_ids",
            new=AsyncMock(return_value=(["$thread/root:localhost"], False)),
        ) as enumerate_threads,
        patch(
            "mindroom.thread_export.execution.fetch_projected_thread_history",
            new=AsyncMock(return_value=fetch_result),
        ) as fetch_thread,
    ):
        stats = await _export_threads_for_client(
            client=Mock(),
            config=config,
            runtime_paths=runtime_paths,
            output_dir=tmp_path / "exports",
            rooms=_export_rooms(runtime_paths, "lobby"),
        )

    assert stats.rooms_exported == 1
    assert stats.threads_exported == 1
    assert stats.failures == 0
    enumerate_threads.assert_awaited_once()
    fetch_thread.assert_awaited_once()
    assert fetch_thread.await_args.kwargs["room_id"] == "!lobby:localhost"
    assert fetch_thread.await_args.kwargs["thread_id"] == "$thread/root:localhost"

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
            "latest_event_id": "$reply-edit:localhost",
            "sender": "@mindroom_general:localhost",
            "timestamp": 1_700_000_001_000,
            "timestamp_iso": "2023-11-14T22:13:21+00:00",
            "edited_timestamp": 1_700_000_002_000,
            "edited_timestamp_iso": "2023-11-14T22:13:22+00:00",
            "thread_id": "$thread/root:localhost",
            "body": "Revised follow-up details",
        },
    ]


@pytest.mark.asyncio
async def test_export_writes_room_index_with_summary_and_participants(tmp_path: Path) -> None:
    """Each exported room should get an index.json mapping thread files to their metadata."""
    config = _config(tmp_path)
    runtime_paths = runtime_paths_for(config)
    _write_matrix_state(tmp_path, account_keys=("agent_router", "agent_general"))

    histories = {
        "$t1:localhost": [
            ResolvedVisibleMessage.synthetic(
                sender="@alice:localhost",
                body="Root decision",
                timestamp=1_700_000_000_000,
                event_id="$t1:localhost",
            ),
            ResolvedVisibleMessage.synthetic(
                sender="@agent_general:localhost",
                body="Deploy pipeline fix",
                timestamp=1_700_000_002_000,
                event_id="$t1-summary:localhost",
                thread_id="$t1:localhost",
                content={
                    "msgtype": "m.notice",
                    "io.mindroom.thread_summary": {"version": 1, "summary": "Deploy pipeline fix"},
                },
            ),
            ResolvedVisibleMessage.synthetic(
                sender="@alice:localhost",
                body="Forged latest summary",
                timestamp=1_700_000_003_000,
                event_id="$t1-forged-summary:localhost",
                thread_id="$t1:localhost",
                content={
                    "msgtype": "m.notice",
                    "io.mindroom.thread_summary": {"version": 1, "summary": "Forged latest summary"},
                },
            ),
        ],
        "$t2:localhost": [
            ResolvedVisibleMessage.synthetic(
                sender="@bob:localhost",
                body="Newer thread",
                timestamp=1_700_000_005_000,
                event_id="$t2:localhost",
            ),
        ],
    }

    async def fetch_side_effect(*_args: object, thread_id: str, **_kwargs: object) -> list[ResolvedVisibleMessage]:
        return histories[thread_id]

    with (
        patch(
            "mindroom.thread_export.execution.enumerate_room_thread_root_ids",
            new=AsyncMock(return_value=(list(histories), False)),
        ),
        patch(
            "mindroom.thread_export.execution.fetch_projected_thread_history",
            new=AsyncMock(side_effect=fetch_side_effect),
        ),
    ):
        stats = await _export_threads_for_client(
            client=Mock(),
            config=config,
            runtime_paths=runtime_paths,
            output_dir=tmp_path / "exports",
            rooms=_export_rooms(runtime_paths, "lobby"),
        )

    assert stats.failures == 0
    thread_one = yaml.safe_load(
        (tmp_path / "exports" / "lobby" / f"{quote('$t1:localhost', safe='')}.yaml").read_text(encoding="utf-8"),
    )
    assert thread_one["thread"]["summary"] == "Deploy pipeline fix"

    index = json.loads((tmp_path / "exports" / "lobby" / "index.json").read_text(encoding="utf-8"))
    assert index["room"]["key"] == "lobby"
    assert index["thread_count"] == 2
    newest, older = index["threads"]
    assert newest["thread_id"] == "$t2:localhost"
    assert newest["participants"] == ["@bob:localhost"]
    assert newest["last_timestamp"] == 1_700_000_005_000
    assert "summary" not in newest
    assert older["thread_id"] == "$t1:localhost"
    assert older["file"] == f"{quote('$t1:localhost', safe='')}.yaml"
    assert older["message_count"] == 3
    assert older["participants"] == ["@agent_general:localhost", "@alice:localhost"]
    assert older["summary"] == "Deploy pipeline fix"


@pytest.mark.asyncio
async def test_room_index_not_rewritten_when_unchanged(tmp_path: Path) -> None:
    """A second unchanged pass should skip both index rebuild parsing and replacement."""
    config = _config(tmp_path)
    runtime_paths = runtime_paths_for(config)
    _write_matrix_state(tmp_path)

    history = [
        ResolvedVisibleMessage.synthetic(
            sender="@alice:localhost",
            body="Stable content",
            timestamp=1_700_000_000_000,
            event_id="$stable:localhost",
        ),
    ]

    with (
        patch(
            "mindroom.thread_export.execution.enumerate_room_thread_root_ids",
            new=AsyncMock(return_value=(["$stable:localhost"], False)),
        ),
        patch(
            "mindroom.thread_export.execution.fetch_projected_thread_history",
            new=AsyncMock(return_value=history),
        ),
    ):
        await _export_threads_for_client(
            client=Mock(),
            config=config,
            runtime_paths=runtime_paths,
            output_dir=tmp_path / "exports",
            rooms=_export_rooms(runtime_paths, "lobby"),
        )
        index_path = tmp_path / "exports" / "lobby" / "index.json"
        first_mtime = index_path.stat().st_mtime_ns
        with patch(
            "mindroom.thread_export.storage._room_index_payload",
            wraps=thread_export_storage._room_index_payload,
        ) as build_index:
            await _export_threads_for_client(
                client=Mock(),
                config=config,
                runtime_paths=runtime_paths,
                output_dir=tmp_path / "exports",
                rooms=_export_rooms(runtime_paths, "lobby"),
            )

    assert index_path.stat().st_mtime_ns == first_mtime
    build_index.assert_not_called()


@pytest.mark.asyncio
async def test_nonempty_enumeration_repairs_index_after_committed_yaml_removal(tmp_path: Path) -> None:
    """An unchanged pass must remove an index entry left stale by an interrupted prior pass."""
    config = _config(tmp_path)
    runtime_paths = runtime_paths_for(config)
    _write_matrix_state(tmp_path)
    output_dir = tmp_path / "exports"
    rooms = _export_rooms(runtime_paths, "lobby")
    retained_thread_id = "$retained:localhost"
    removed_thread_id = "$removed:localhost"
    histories = {
        retained_thread_id: [
            ResolvedVisibleMessage.synthetic(
                sender="@alice:localhost",
                body="Retained",
                event_id=retained_thread_id,
            ),
        ],
        removed_thread_id: [
            ResolvedVisibleMessage.synthetic(
                sender="@alice:localhost",
                body="Removed",
                event_id=removed_thread_id,
            ),
        ],
    }

    async def fetch_history(*_args: object, thread_id: str, **_kwargs: object) -> object:
        return histories[thread_id]

    with (
        patch(
            "mindroom.thread_export.execution.enumerate_room_thread_root_ids",
            new=AsyncMock(return_value=([retained_thread_id, removed_thread_id], False)),
        ),
        patch(
            "mindroom.thread_export.execution.fetch_projected_thread_history",
            new=AsyncMock(side_effect=fetch_history),
        ),
    ):
        await _export_threads_for_client(
            client=Mock(),
            config=config,
            runtime_paths=runtime_paths,
            output_dir=output_dir,
            rooms=rooms,
        )

    room_dir = output_dir / "lobby"
    removed_file = room_dir / f"{quote(removed_thread_id, safe='')}.yaml"
    removed_file.unlink()
    index_path = room_dir / "index.json"
    assert json.loads(index_path.read_text(encoding="utf-8"))["thread_count"] == 2

    with (
        patch(
            "mindroom.thread_export.execution.enumerate_room_thread_root_ids",
            new=AsyncMock(return_value=([retained_thread_id], False)),
        ),
        patch(
            "mindroom.thread_export.execution.fetch_projected_thread_history",
            new=AsyncMock(return_value=histories[retained_thread_id]),
        ),
    ):
        stats = await _export_threads_for_client(
            client=Mock(),
            config=config,
            runtime_paths=runtime_paths,
            output_dir=output_dir,
            rooms=rooms,
        )

    assert stats.threads_unchanged == 1
    index = json.loads(index_path.read_text(encoding="utf-8"))
    assert [entry["thread_id"] for entry in index["threads"]] == [retained_thread_id]


@pytest.mark.asyncio
async def test_export_threads_skips_rewrite_when_content_unchanged(tmp_path: Path) -> None:
    """A second pass with identical thread content should leave the file untouched."""
    config = _config(tmp_path)
    runtime_paths = runtime_paths_for(config)
    _write_matrix_state(tmp_path)

    history = [
        ResolvedVisibleMessage.synthetic(
            sender="@alice:localhost",
            body="Stable content",
            event_id="$stable:localhost",
        ),
    ]

    with (
        patch(
            "mindroom.thread_export.execution.enumerate_room_thread_root_ids",
            new=AsyncMock(return_value=(["$stable:localhost"], False)),
        ),
        patch(
            "mindroom.thread_export.execution.fetch_projected_thread_history",
            new=AsyncMock(return_value=history),
        ),
    ):
        first_stats = await _export_threads_for_client(
            client=Mock(),
            config=config,
            runtime_paths=runtime_paths,
            output_dir=tmp_path / "exports",
            rooms=_export_rooms(runtime_paths, "lobby"),
        )
        exported_file = next((tmp_path / "exports" / "lobby").glob("*.yaml"))
        first_bytes = exported_file.read_bytes()
        second_stats = await _export_threads_for_client(
            client=Mock(),
            config=config,
            runtime_paths=runtime_paths,
            output_dir=tmp_path / "exports",
            rooms=_export_rooms(runtime_paths, "lobby"),
        )

    assert first_stats.threads_unchanged == 0
    assert second_stats.threads_exported == 1
    assert second_stats.threads_unchanged == 1
    assert exported_file.read_bytes() == first_bytes


@pytest.mark.asyncio
async def test_export_threads_rewrites_when_content_changed(tmp_path: Path) -> None:
    """A pass with new thread messages should rewrite the existing file."""
    config = _config(tmp_path)
    runtime_paths = runtime_paths_for(config)
    _write_matrix_state(tmp_path)

    first_history = [
        ResolvedVisibleMessage.synthetic(
            sender="@alice:localhost",
            body="Original",
            event_id="$original:localhost",
        ),
    ]
    second_history = [
        *first_history,
        ResolvedVisibleMessage.synthetic(
            sender="@alice:localhost",
            body="Follow-up",
            event_id="$followup:localhost",
        ),
    ]

    with patch(
        "mindroom.thread_export.execution.enumerate_room_thread_root_ids",
        new=AsyncMock(return_value=(["$original:localhost"], False)),
    ):
        with patch(
            "mindroom.thread_export.execution.fetch_projected_thread_history",
            new=AsyncMock(return_value=first_history),
        ):
            await _export_threads_for_client(
                client=Mock(),
                config=config,
                runtime_paths=runtime_paths,
                output_dir=tmp_path / "exports",
                rooms=_export_rooms(runtime_paths, "lobby"),
            )
        with patch(
            "mindroom.thread_export.execution.fetch_projected_thread_history",
            new=AsyncMock(return_value=second_history),
        ):
            stats = await _export_threads_for_client(
                client=Mock(),
                config=config,
                runtime_paths=runtime_paths,
                output_dir=tmp_path / "exports",
                rooms=_export_rooms(runtime_paths, "lobby"),
            )

    assert stats.threads_unchanged == 0
    assert stats.threads_exported == 1
    payload = yaml.safe_load(next((tmp_path / "exports" / "lobby").glob("*.yaml")).read_text(encoding="utf-8"))
    assert [message["body"] for message in payload["messages"]] == ["Original", "Follow-up"]


@pytest.mark.asyncio
async def test_export_threads_rewrites_when_existing_file_corrupt(tmp_path: Path) -> None:
    """A corrupt existing export file should be rewritten instead of raising."""
    config = _config(tmp_path)
    runtime_paths = runtime_paths_for(config)
    _write_matrix_state(tmp_path)

    history = [
        ResolvedVisibleMessage.synthetic(
            sender="@alice:localhost",
            body="Fresh content",
            event_id="$fresh:localhost",
        ),
    ]
    corrupt_path = tmp_path / "exports" / "lobby" / f"{quote('$fresh:localhost', safe='')}.yaml"
    mark_thread_export_root(tmp_path / "exports")
    corrupt_path.parent.mkdir(parents=True)
    corrupt_path.write_text("{not: [valid yaml", encoding="utf-8")

    with (
        patch(
            "mindroom.thread_export.execution.enumerate_room_thread_root_ids",
            new=AsyncMock(return_value=(["$fresh:localhost"], False)),
        ),
        patch(
            "mindroom.thread_export.execution.fetch_projected_thread_history",
            new=AsyncMock(return_value=history),
        ),
    ):
        stats = await _export_threads_for_client(
            client=Mock(),
            config=config,
            runtime_paths=runtime_paths,
            output_dir=tmp_path / "exports",
            rooms=_export_rooms(runtime_paths, "lobby"),
        )

    assert stats.threads_exported == 1
    assert stats.threads_unchanged == 0
    payload = yaml.safe_load(corrupt_path.read_text(encoding="utf-8"))
    assert payload["messages"][0]["body"] == "Fresh content"


@pytest.mark.asyncio
async def test_export_threads_rewrites_existing_file_with_invalid_utf8(tmp_path: Path) -> None:
    """An invalid UTF-8 export should be treated as corrupt and rewritten."""
    config = _config(tmp_path)
    runtime_paths = runtime_paths_for(config)
    _write_matrix_state(tmp_path)
    export_path = tmp_path / "exports" / "lobby" / f"{quote('$fresh:localhost', safe='')}.yaml"
    mark_thread_export_root(tmp_path / "exports")
    export_path.parent.mkdir(parents=True)
    export_path.write_bytes(b"\x80")
    history = [
        ResolvedVisibleMessage.synthetic(
            sender="@alice:localhost",
            body="Fresh content",
            event_id="$fresh:localhost",
        ),
    ]

    with (
        patch(
            "mindroom.thread_export.execution.enumerate_room_thread_root_ids",
            new=AsyncMock(return_value=(["$fresh:localhost"], False)),
        ),
        patch(
            "mindroom.thread_export.execution.fetch_projected_thread_history",
            new=AsyncMock(return_value=history),
        ),
    ):
        stats = await _export_threads_for_client(
            client=Mock(),
            config=config,
            runtime_paths=runtime_paths,
            output_dir=tmp_path / "exports",
            rooms=_export_rooms(runtime_paths, "lobby"),
        )

    assert stats.failures == 0
    assert yaml.safe_load(export_path.read_text(encoding="utf-8"))["messages"][0]["body"] == "Fresh content"


@pytest.mark.asyncio
async def test_multi_target_export_fetches_each_thread_once(tmp_path: Path) -> None:
    """Multiple destinations should share enumeration and source history retrieval."""
    config = _config(tmp_path)
    runtime_paths = runtime_paths_for(config)
    _write_matrix_state(tmp_path)
    history = [
        ResolvedVisibleMessage.synthetic(
            sender="@alice:localhost",
            body="Shared source fetch",
            event_id="$shared:localhost",
        ),
    ]
    enumerate_threads = AsyncMock(return_value=(["$shared:localhost"], False))
    fetch_thread = AsyncMock(return_value=history)
    targets = (
        ThreadExportTarget(output_dir=tmp_path / "first"),
        ThreadExportTarget(output_dir=tmp_path / "second"),
    )

    with (
        patch("mindroom.thread_export.execution.enumerate_room_thread_root_ids", new=enumerate_threads),
        patch("mindroom.thread_export.execution.fetch_projected_thread_history", new=fetch_thread),
    ):
        accumulators = await _export_threads_for_targets_for_client(
            client=Mock(),
            reader=Mock(),
            config=config,
            runtime_paths=runtime_paths,
            rooms=_export_rooms(runtime_paths, "lobby"),
            targets=targets,
        )

    enumerate_threads.assert_awaited_once()
    fetch_thread.assert_awaited_once()
    assert [accumulator.stats().threads_exported for accumulator in accumulators] == [1, 1]
    assert all(len(list((target.output_dir / "lobby").glob("*.yaml"))) == 1 for target in targets)


@pytest.mark.asyncio
async def test_complete_room_export_removes_stale_thread_files(tmp_path: Path) -> None:
    """A complete enumeration should remove vanished threads before rebuilding the index."""
    config = _config(tmp_path)
    runtime_paths = runtime_paths_for(config)
    _write_matrix_state(tmp_path)
    room = _export_rooms(runtime_paths, "lobby")
    histories = {
        "$old:localhost": [
            ResolvedVisibleMessage.synthetic(sender="@alice:localhost", body="Old", event_id="$old:localhost"),
        ],
        "$new:localhost": [
            ResolvedVisibleMessage.synthetic(sender="@alice:localhost", body="New", event_id="$new:localhost"),
        ],
    }

    async def fetch_history(*_args: object, thread_id: str, **_kwargs: object) -> object:
        return histories[thread_id]

    with patch(
        "mindroom.thread_export.execution.fetch_projected_thread_history",
        new=AsyncMock(side_effect=fetch_history),
    ):
        with patch(
            "mindroom.thread_export.execution.enumerate_room_thread_root_ids",
            new=AsyncMock(return_value=(["$old:localhost"], False)),
        ):
            await _export_threads_for_client(
                client=Mock(),
                config=config,
                runtime_paths=runtime_paths,
                output_dir=tmp_path / "exports",
                rooms=room,
            )
        with patch(
            "mindroom.thread_export.execution.enumerate_room_thread_root_ids",
            new=AsyncMock(return_value=(["$new:localhost"], False)),
        ):
            await _export_threads_for_client(
                client=Mock(),
                config=config,
                runtime_paths=runtime_paths,
                output_dir=tmp_path / "exports",
                rooms=room,
            )

    room_dir = tmp_path / "exports" / "lobby"
    assert {path.name for path in room_dir.glob("*.yaml")} == {f"{quote('$new:localhost', safe='')}.yaml"}
    index = json.loads((room_dir / "index.json").read_text(encoding="utf-8"))
    assert [entry["thread_id"] for entry in index["threads"]] == ["$new:localhost"]


@pytest.mark.asyncio
async def test_empty_complete_enumeration_preserves_existing_thread_exports(tmp_path: Path) -> None:
    """An empty complete enumeration must not erase an existing on-disk room corpus."""
    config = _config(tmp_path)
    runtime_paths = runtime_paths_for(config)
    _write_matrix_state(tmp_path)
    output_dir = tmp_path / "exports"
    room = _export_rooms(runtime_paths, "lobby")
    history = [
        ResolvedVisibleMessage.synthetic(
            sender="@alice:localhost",
            body="Retain me",
            event_id="$existing:localhost",
        ),
    ]

    with (
        patch(
            "mindroom.thread_export.execution.enumerate_room_thread_root_ids",
            new=AsyncMock(return_value=(["$existing:localhost"], False)),
        ),
        patch(
            "mindroom.thread_export.execution.fetch_projected_thread_history",
            new=AsyncMock(return_value=history),
        ),
    ):
        await _export_threads_for_client(
            client=Mock(),
            config=config,
            runtime_paths=runtime_paths,
            output_dir=output_dir,
            rooms=room,
        )

    exported_file = next((output_dir / "lobby").glob("*.yaml"))
    original_bytes = exported_file.read_bytes()
    with (
        patch(
            "mindroom.thread_export.execution.enumerate_room_thread_root_ids",
            new=AsyncMock(return_value=([], False)),
        ),
        patch("mindroom.thread_export.execution.logger.warning") as warning,
    ):
        for _ in range(2):
            stats = await _export_threads_for_client(
                client=Mock(),
                config=config,
                runtime_paths=runtime_paths,
                output_dir=output_dir,
                rooms=room,
            )

    assert stats.rooms_exported == 1
    assert stats.threads_seen == 0
    assert exported_file.read_bytes() == original_bytes
    assert warning.call_count == 2
    for warning_call in warning.call_args_list:
        assert warning_call.args == ("Skipping stale thread reconciliation after empty enumeration",)
        assert warning_call.kwargs == {
            "output_dir": str(output_dir),
            "room_key": "lobby",
            "room_id": "!lobby:localhost",
        }


@pytest.mark.asyncio
@pytest.mark.parametrize("existing_index", [False, True], ids=["missing-index", "stale-index"])
async def test_empty_complete_enumeration_repairs_index_after_committed_yaml_addition(
    tmp_path: Path,
    *,
    existing_index: bool,
) -> None:
    """Preserved YAML must repair missing or stale index state after an interrupted prior pass."""
    config = _config(tmp_path)
    runtime_paths = runtime_paths_for(config)
    _write_matrix_state(tmp_path)
    output_dir = tmp_path / "exports"
    rooms = _export_rooms(runtime_paths, "lobby")
    room = rooms[0]
    indexed_thread_id = "$indexed:localhost"
    committed_thread_id = "$committed:localhost"

    def payload(thread_id: str) -> dict[str, object]:
        return {
            "version": 1,
            "room": {
                "key": room.key,
                "id": room.room_id,
                "name": room.name,
                "alias": room.alias,
            },
            "thread": {
                "id": thread_id,
                "message_count": 1,
            },
            "messages": [
                {
                    "sender": "@alice:localhost",
                    "timestamp": 1_700_000_000_000,
                },
            ],
        }

    assert write_thread_payload(output_dir, room, indexed_thread_id, payload(indexed_thread_id)) is True
    index_path = output_dir / "lobby" / "index.json"
    if existing_index:
        write_room_index(output_dir, room)
        assert json.loads(index_path.read_text(encoding="utf-8"))["thread_count"] == 1
    else:
        assert not index_path.exists()
    assert write_thread_payload(output_dir, room, committed_thread_id, payload(committed_thread_id)) is True

    with patch(
        "mindroom.thread_export.execution.enumerate_room_thread_root_ids",
        new=AsyncMock(return_value=([], False)),
    ):
        stats = await _export_threads_for_client(
            client=Mock(),
            config=config,
            runtime_paths=runtime_paths,
            output_dir=output_dir,
            rooms=rooms,
        )

    assert stats.failures == 0
    index = json.loads(index_path.read_text(encoding="utf-8"))
    assert {entry["thread_id"] for entry in index["threads"]} == {
        indexed_thread_id,
        committed_thread_id,
    }


@pytest.mark.asyncio
async def test_definitive_non_member_on_non_aliased_target_removes_room_export(
    tmp_path: Path,
) -> None:
    """A fresh non-membership answer should still retract a non-aliased target's room."""
    config = _config(tmp_path)
    runtime_paths = runtime_paths_for(config)
    _write_matrix_state(tmp_path)

    members_by_room = {
        "!lobby:localhost": ["@alice:localhost", "@mindroom_general:localhost"],
        "!dev:localhost": ["@bob:localhost"],
    }

    async def joined_members(room_id: str) -> nio.JoinedMembersResponse:
        return nio.JoinedMembersResponse(
            members=[nio.RoomMember(user_id, "", "") for user_id in members_by_room[room_id]],
            room_id=room_id,
        )

    client = Mock()
    client.joined_members = AsyncMock(side_effect=joined_members)
    mark_thread_export_root(tmp_path / "exports")
    stale_dev_dir = tmp_path / "exports" / "dev"
    stale_dev_dir.mkdir(parents=True)
    (stale_dev_dir / "index.json").write_text("{}\n", encoding="utf-8")
    (stale_dev_dir / f"{quote('$old:localhost', safe='')}.yaml").write_text("secret", encoding="utf-8")
    history = [
        ResolvedVisibleMessage.synthetic(
            sender="@alice:localhost",
            body="Members only",
            event_id="$member:localhost",
        ),
    ]

    with (
        patch(
            "mindroom.thread_export.execution.enumerate_room_thread_root_ids",
            new=AsyncMock(return_value=(["$member:localhost"], False)),
        ) as enumerate_threads,
        patch(
            "mindroom.thread_export.execution.fetch_projected_thread_history",
            new=AsyncMock(return_value=history),
        ),
    ):
        stats = await _export_threads_for_client(
            client=client,
            config=config,
            runtime_paths=runtime_paths,
            output_dir=tmp_path / "exports",
            rooms=_export_rooms(runtime_paths, None),
            required_member_user_id="@alice:localhost",
        )

    assert stats.rooms_exported == 1
    assert stats.failures == 0
    enumerate_threads.assert_awaited_once_with(client, "!lobby:localhost", max_thread_roots=2000)
    assert (tmp_path / "exports" / "lobby").is_dir()
    assert not (tmp_path / "exports" / "dev").exists()


@pytest.mark.asyncio
async def test_admitted_empty_root_handles_retraction_and_zero_thread_export_without_failures(
    tmp_path: Path,
) -> None:
    """An admitted empty root should handle rejected and empty-room operations."""
    config = _config(tmp_path)
    runtime_paths = runtime_paths_for(config)
    output_dir = tmp_path / "exports"
    mark_thread_export_root(output_dir)
    client = Mock()
    rooms = (
        _ThreadExportRoom(
            key="!invited:localhost",
            room_id="!invited:localhost",
            alias="",
            name="Invited",
            invited=True,
        ),
        _ThreadExportRoom(
            key="lobby",
            room_id="!lobby:localhost",
            alias="#lobby:localhost",
            name="Lobby",
        ),
    )

    with patch(
        "mindroom.thread_export.execution.enumerate_room_thread_root_ids",
        new=AsyncMock(return_value=([], False)),
    ) as enumerate_threads:
        accumulators = await _export_threads_for_targets_for_client(
            client=client,
            reader=Mock(),
            config=config,
            runtime_paths=runtime_paths,
            rooms=rooms,
            targets=(
                ThreadExportTarget(
                    output_dir=output_dir,
                    include_invited_rooms=False,
                ),
            ),
        )

    stats = accumulators[0].stats()
    assert stats.rooms_exported == 1
    assert stats.threads_seen == 0
    assert stats.failures == 0
    enumerate_threads.assert_awaited_once_with(client, "!lobby:localhost", max_thread_roots=2000)
    assert (output_dir / ".mindroom-thread-exports").is_file()


@pytest.mark.parametrize(
    ("invited", "required_member_user_id"),
    [
        pytest.param(True, None, id="excluded-invited-room"),
        pytest.param(False, "@alice:localhost", id="definitive-non-member"),
    ],
)
@pytest.mark.asyncio
async def test_room_removal_failure_is_scoped_to_the_rejected_target(
    tmp_path: Path,
    *,
    invited: bool,
    required_member_user_id: str | None,
) -> None:
    """A failed retraction should not prevent another target from exporting the room."""
    config = _config(tmp_path)
    runtime_paths = runtime_paths_for(config)
    room = _ThreadExportRoom(
        key="room",
        room_id="!room:localhost",
        alias="#room:localhost",
        name="Room",
        invited=invited,
    )
    client = Mock()
    if required_member_user_id is not None:
        client.joined_members = AsyncMock(
            return_value=nio.JoinedMembersResponse(members=[], room_id=room.room_id),
        )
    rejected_target = ThreadExportTarget(
        output_dir=tmp_path / "rejected",
        required_member_user_id=required_member_user_id,
        include_invited_rooms=not invited,
    )
    healthy_target = ThreadExportTarget(output_dir=tmp_path / "healthy")

    with (
        patch(
            "mindroom.thread_export.execution.remove_room_export",
            side_effect=RuntimeError("storage unavailable"),
        ) as remove_export,
        patch(
            "mindroom.thread_export.execution.enumerate_room_thread_root_ids",
            new=AsyncMock(return_value=([], False)),
        ) as enumerate_threads,
    ):
        accumulators = await _export_threads_for_targets_for_client(
            client=client,
            reader=Mock(),
            config=config,
            runtime_paths=runtime_paths,
            rooms=(room,),
            targets=(rejected_target, healthy_target),
        )

    remove_export.assert_called_once_with(rejected_target.output_dir, room)
    enumerate_threads.assert_awaited_once_with(client, room.room_id, max_thread_roots=2000)
    assert accumulators[0].rooms_exported == 0
    assert accumulators[0].failed_items[0].error == "Room removal failed: storage unavailable"
    assert accumulators[1].rooms_exported == 1
    assert accumulators[1].failed_items == []


@pytest.mark.asyncio
async def test_target_membership_and_invited_room_setting_are_both_enforced(tmp_path: Path) -> None:
    """Every target should require membership, with invited rooms as an additional opt-in category."""
    config = _config(tmp_path)
    runtime_paths = runtime_paths_for(config)
    rooms = [
        _ThreadExportRoom(
            key="lobby",
            room_id="!lobby:localhost",
            alias="#lobby:localhost",
            name="Lobby",
        ),
        _ThreadExportRoom(
            key="dev",
            room_id="!dev:localhost",
            alias="#dev:localhost",
            name="Dev",
        ),
        _ThreadExportRoom(
            key="!invited:localhost",
            room_id="!invited:localhost",
            alias="",
            name="",
            invited=True,
        ),
    ]
    members_by_room = {
        "!lobby:localhost": ["@mindroom_code:localhost"],
        "!dev:localhost": ["@mindroom_research:localhost"],
        "!invited:localhost": [
            "@mindroom_code:localhost",
            "@mindroom_research:localhost",
        ],
    }

    async def joined_members(room_id: str) -> nio.JoinedMembersResponse:
        return nio.JoinedMembersResponse(
            members=[nio.RoomMember(user_id, "", "") for user_id in members_by_room[room_id]],
            room_id=room_id,
        )

    client = Mock()
    client.joined_members = AsyncMock(side_effect=joined_members)
    enumerate_threads = AsyncMock(return_value=([], False))
    targets = (
        ThreadExportTarget(
            output_dir=tmp_path / "code",
            required_member_user_id="@mindroom_code:localhost",
            include_invited_rooms=True,
        ),
        ThreadExportTarget(
            output_dir=tmp_path / "research",
            required_member_user_id="@mindroom_research:localhost",
            include_invited_rooms=False,
        ),
    )

    with patch(
        "mindroom.thread_export.execution.enumerate_room_thread_root_ids",
        new=enumerate_threads,
    ):
        accumulators = await _export_threads_for_targets_for_client(
            client=client,
            reader=Mock(),
            config=config,
            runtime_paths=runtime_paths,
            rooms=rooms,
            targets=targets,
        )

    assert [accumulator.rooms_exported for accumulator in accumulators] == [2, 1]
    assert accumulators[0].retained_room_keys == {"lobby", "!invited:localhost"}
    assert accumulators[1].retained_room_keys == {"dev"}
    assert client.joined_members.await_count == 3
    assert enumerate_threads.await_count == 3


@pytest.mark.asyncio
async def test_member_filter_lookup_failure_keeps_exports_and_records_failure(tmp_path: Path) -> None:
    """A failed membership lookup must keep existing exports: unknown is not revoked."""
    config = _config(tmp_path)
    runtime_paths = runtime_paths_for(config)
    _write_matrix_state(tmp_path)
    client = Mock()
    client.joined_members = AsyncMock(return_value=Mock())
    mark_thread_export_root(tmp_path / "exports")
    for room_key in ("lobby", "dev"):
        stale_room_dir = tmp_path / "exports" / room_key
        stale_room_dir.mkdir(parents=True)
        (stale_room_dir / "old.yaml").write_text("secret", encoding="utf-8")

    with patch(
        "mindroom.thread_export.execution.enumerate_room_thread_root_ids",
        new=AsyncMock(),
    ) as enumerate_threads:
        accumulators = await _export_threads_for_targets_for_client(
            client=client,
            reader=Mock(),
            config=config,
            runtime_paths=runtime_paths,
            rooms=_export_rooms(runtime_paths, None),
            targets=(
                ThreadExportTarget(
                    output_dir=tmp_path / "exports",
                    required_member_user_id="@alice:localhost",
                ),
            ),
        )

    accumulator = accumulators[0]
    stats = accumulator.stats()
    assert stats.rooms_exported == 0
    assert stats.failures == 2
    assert all("Membership lookup failed" in failure.error for failure in stats.failed_items)
    assert accumulator.retained_room_keys == {"lobby", "dev"}
    enumerate_threads.assert_not_awaited()
    for room_key in ("lobby", "dev"):
        assert (tmp_path / "exports" / room_key / "old.yaml").read_text(encoding="utf-8") == "secret"


@pytest.mark.asyncio
async def test_export_threads_continues_after_one_thread_failure(tmp_path: Path) -> None:
    """One failed thread should not stop other thread exports in the same room."""
    config = _config(tmp_path)
    runtime_paths = runtime_paths_for(config)
    _write_matrix_state(tmp_path)

    async def fetch_side_effect(*_args: object, thread_id: str, **_kwargs: object) -> list[ResolvedVisibleMessage]:
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
            "mindroom.thread_export.execution.enumerate_room_thread_root_ids",
            new=AsyncMock(return_value=(["$bad:localhost", "$good:localhost"], False)),
        ),
        patch(
            "mindroom.thread_export.execution.fetch_projected_thread_history",
            new=AsyncMock(side_effect=fetch_side_effect),
        ),
    ):
        stats = await _export_threads_for_client(
            client=Mock(),
            config=config,
            runtime_paths=runtime_paths,
            output_dir=tmp_path / "exports",
            rooms=_export_rooms(runtime_paths, "lobby"),
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
        "mindroom.thread_export.execution.enumerate_room_thread_root_ids",
        new=AsyncMock(side_effect=enumerate_side_effect),
    ):
        stats = await _export_threads_for_client(
            client=Mock(),
            config=config,
            runtime_paths=runtime_paths,
            output_dir=tmp_path / "exports",
            rooms=_export_rooms(runtime_paths, None),
        )

    assert stats.rooms_exported == 1
    assert stats.failures == 1
    assert stats.failed_items[0].room_key == "lobby"

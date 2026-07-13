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
from mindroom.thread_export.execution import (
    export_threads_for_targets_for_client as _export_threads_for_targets_for_client,
)
from mindroom.thread_export.models import (
    ThreadExportRoom as _ThreadExportRoom,
)
from mindroom.thread_export.selection import export_rooms as _export_rooms
from tests.conftest import runtime_paths_for
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
    from mindroom.matrix.cache import ConversationEventCache
    from mindroom.thread_export import ThreadExportStats


async def _export_threads_for_client(
    *,
    client: nio.AsyncClient,
    config: Config,
    runtime_paths: RuntimePaths,
    event_cache: ConversationEventCache,
    rooms: Sequence[_ThreadExportRoom],
    output_dir: Path,
    max_thread_roots: int = 2000,
    prefer_cache: bool = False,
    required_member_user_id: str | None = None,
) -> ThreadExportStats:
    """Adapt the multi-target execution seam for single-target test cases."""
    accumulators = await _export_threads_for_targets_for_client(
        client=client,
        config=config,
        runtime_paths=runtime_paths,
        event_cache=event_cache,
        rooms=rooms,
        targets=(
            ThreadExportTarget(
                output_dir=output_dir,
                required_member_user_id=required_member_user_id,
            ),
        ),
        max_thread_roots=max_thread_roots,
        prefer_cache=prefer_cache,
    )
    return accumulators[0].stats()


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
            "mindroom.thread_export.execution.enumerate_room_thread_root_ids",
            new=AsyncMock(return_value=(["$thread/root:localhost"], False)),
        ) as enumerate_threads,
        patch(
            "mindroom.thread_export.execution.refresh_thread_history_from_source",
            new=AsyncMock(return_value=fetch_result),
        ) as fetch_thread,
    ):
        stats = await _export_threads_for_client(
            client=Mock(),
            config=config,
            runtime_paths=runtime_paths,
            event_cache=Mock(),
            output_dir=tmp_path / "exports",
            rooms=_export_rooms(runtime_paths, "lobby"),
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
async def test_export_threads_prefer_cache_uses_cache_first_fetch(tmp_path: Path) -> None:
    """prefer_cache should read thread history through the cache-first fetch path."""
    config = _config(tmp_path)
    runtime_paths = runtime_paths_for(config)
    _write_matrix_state(tmp_path)

    history = [
        ResolvedVisibleMessage.synthetic(
            sender="@alice:localhost",
            body="Cached thread",
            event_id="$cached:localhost",
        ),
    ]

    with (
        patch(
            "mindroom.thread_export.execution.enumerate_room_thread_root_ids",
            new=AsyncMock(return_value=(["$cached:localhost"], False)),
        ),
        patch(
            "mindroom.thread_export.execution.fetch_thread_history",
            new=AsyncMock(return_value=history),
        ) as cache_fetch,
        patch(
            "mindroom.thread_export.execution.refresh_thread_history_from_source",
            new=AsyncMock(),
        ) as source_fetch,
    ):
        stats = await _export_threads_for_client(
            client=Mock(),
            config=config,
            runtime_paths=runtime_paths,
            event_cache=Mock(),
            output_dir=tmp_path / "exports",
            rooms=_export_rooms(runtime_paths, "lobby"),
            prefer_cache=True,
        )

    assert stats.threads_exported == 1
    assert stats.failures == 0
    source_fetch.assert_not_awaited()
    cache_fetch.assert_awaited_once()
    assert cache_fetch.await_args.kwargs["caller_label"] == "thread_export"
    assert len(list((tmp_path / "exports" / "lobby").glob("*.yaml"))) == 1


@pytest.mark.asyncio
async def test_export_writes_room_index_with_summary_and_participants(tmp_path: Path) -> None:
    """Each exported room should get an index.json mapping thread files to their metadata."""
    config = _config(tmp_path)
    runtime_paths = runtime_paths_for(config)
    _write_matrix_state(tmp_path)

    histories = {
        "$t1:localhost": [
            ResolvedVisibleMessage.synthetic(
                sender="@alice:localhost",
                body="Root decision",
                timestamp=1_700_000_000_000,
                event_id="$t1:localhost",
            ),
            ResolvedVisibleMessage.synthetic(
                sender="@mindroom_general:localhost",
                body="Deploy pipeline fix",
                timestamp=1_700_000_002_000,
                event_id="$t1-summary:localhost",
                thread_id="$t1:localhost",
                content={
                    "msgtype": "m.notice",
                    "io.mindroom.thread_summary": {"version": 1, "summary": "Deploy pipeline fix"},
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

    async def fetch_side_effect(*args: object, **_kwargs: object) -> list[ResolvedVisibleMessage]:
        return histories[str(args[2])]

    with (
        patch(
            "mindroom.thread_export.execution.enumerate_room_thread_root_ids",
            new=AsyncMock(return_value=(list(histories), False)),
        ),
        patch(
            "mindroom.thread_export.execution.refresh_thread_history_from_source",
            new=AsyncMock(side_effect=fetch_side_effect),
        ),
    ):
        stats = await _export_threads_for_client(
            client=Mock(),
            config=config,
            runtime_paths=runtime_paths,
            event_cache=Mock(),
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
    assert older["message_count"] == 2
    assert older["participants"] == ["@alice:localhost", "@mindroom_general:localhost"]
    assert older["summary"] == "Deploy pipeline fix"


@pytest.mark.asyncio
async def test_room_index_not_rewritten_when_unchanged(tmp_path: Path) -> None:
    """A second pass with identical content should leave index.json untouched."""
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
            "mindroom.thread_export.execution.refresh_thread_history_from_source",
            new=AsyncMock(return_value=history),
        ),
    ):
        await _export_threads_for_client(
            client=Mock(),
            config=config,
            runtime_paths=runtime_paths,
            event_cache=Mock(),
            output_dir=tmp_path / "exports",
            rooms=_export_rooms(runtime_paths, "lobby"),
        )
        index_path = tmp_path / "exports" / "lobby" / "index.json"
        first_mtime = index_path.stat().st_mtime_ns
        with patch("mindroom.thread_export.execution.write_room_index") as write_index:
            await _export_threads_for_client(
                client=Mock(),
                config=config,
                runtime_paths=runtime_paths,
                event_cache=Mock(),
                output_dir=tmp_path / "exports",
                rooms=_export_rooms(runtime_paths, "lobby"),
            )

    assert index_path.stat().st_mtime_ns == first_mtime
    write_index.assert_not_called()


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
            "mindroom.thread_export.execution.refresh_thread_history_from_source",
            new=AsyncMock(return_value=history),
        ),
    ):
        first_stats = await _export_threads_for_client(
            client=Mock(),
            config=config,
            runtime_paths=runtime_paths,
            event_cache=Mock(),
            output_dir=tmp_path / "exports",
            rooms=_export_rooms(runtime_paths, "lobby"),
        )
        exported_file = next((tmp_path / "exports" / "lobby").glob("*.yaml"))
        first_bytes = exported_file.read_bytes()
        second_stats = await _export_threads_for_client(
            client=Mock(),
            config=config,
            runtime_paths=runtime_paths,
            event_cache=Mock(),
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
            "mindroom.thread_export.execution.refresh_thread_history_from_source",
            new=AsyncMock(return_value=first_history),
        ):
            await _export_threads_for_client(
                client=Mock(),
                config=config,
                runtime_paths=runtime_paths,
                event_cache=Mock(),
                output_dir=tmp_path / "exports",
                rooms=_export_rooms(runtime_paths, "lobby"),
            )
        with patch(
            "mindroom.thread_export.execution.refresh_thread_history_from_source",
            new=AsyncMock(return_value=second_history),
        ):
            stats = await _export_threads_for_client(
                client=Mock(),
                config=config,
                runtime_paths=runtime_paths,
                event_cache=Mock(),
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
    corrupt_path.parent.mkdir(parents=True)
    corrupt_path.write_text("{not: [valid yaml", encoding="utf-8")

    with (
        patch(
            "mindroom.thread_export.execution.enumerate_room_thread_root_ids",
            new=AsyncMock(return_value=(["$fresh:localhost"], False)),
        ),
        patch(
            "mindroom.thread_export.execution.refresh_thread_history_from_source",
            new=AsyncMock(return_value=history),
        ),
    ):
        stats = await _export_threads_for_client(
            client=Mock(),
            config=config,
            runtime_paths=runtime_paths,
            event_cache=Mock(),
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
            "mindroom.thread_export.execution.refresh_thread_history_from_source",
            new=AsyncMock(return_value=history),
        ),
    ):
        stats = await _export_threads_for_client(
            client=Mock(),
            config=config,
            runtime_paths=runtime_paths,
            event_cache=Mock(),
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
        patch("mindroom.thread_export.execution.refresh_thread_history_from_source", new=fetch_thread),
    ):
        accumulators = await _export_threads_for_targets_for_client(
            client=Mock(),
            config=config,
            runtime_paths=runtime_paths,
            event_cache=Mock(),
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

    async def fetch_history(
        _client: object,
        _room_id: str,
        thread_id: str,
        *_args: object,
        **_kwargs: object,
    ) -> object:
        return histories[thread_id]

    with patch(
        "mindroom.thread_export.execution.refresh_thread_history_from_source",
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
                event_cache=Mock(),
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
                event_cache=Mock(),
                output_dir=tmp_path / "exports",
                rooms=room,
            )

    room_dir = tmp_path / "exports" / "lobby"
    assert {path.name for path in room_dir.glob("*.yaml")} == {f"{quote('$new:localhost', safe='')}.yaml"}
    index = json.loads((room_dir / "index.json").read_text(encoding="utf-8"))
    assert [entry["thread_id"] for entry in index["threads"]] == ["$new:localhost"]


@pytest.mark.asyncio
async def test_member_filter_exports_only_rooms_with_member(tmp_path: Path) -> None:
    """required_member_user_id should skip rooms the user is not currently joined to."""
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
    stale_dev_dir = tmp_path / "exports" / "dev"
    stale_dev_dir.mkdir(parents=True)
    (stale_dev_dir / "old.yaml").write_text("secret", encoding="utf-8")
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
            "mindroom.thread_export.execution.refresh_thread_history_from_source",
            new=AsyncMock(return_value=history),
        ),
    ):
        stats = await _export_threads_for_client(
            client=client,
            config=config,
            runtime_paths=runtime_paths,
            event_cache=Mock(),
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
            config=config,
            runtime_paths=runtime_paths,
            event_cache=Mock(),
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
    for room_key in ("lobby", "dev"):
        stale_room_dir = tmp_path / "exports" / room_key
        stale_room_dir.mkdir(parents=True)
        (stale_room_dir / "old.yaml").write_text("secret", encoding="utf-8")

    with patch(
        "mindroom.thread_export.execution.enumerate_room_thread_root_ids",
        new=AsyncMock(),
    ) as enumerate_threads:
        stats = await _export_threads_for_client(
            client=client,
            config=config,
            runtime_paths=runtime_paths,
            event_cache=Mock(),
            output_dir=tmp_path / "exports",
            rooms=_export_rooms(runtime_paths, None),
            required_member_user_id="@alice:localhost",
        )

    assert stats.rooms_exported == 0
    assert stats.failures == 2
    assert all("Membership lookup failed" in failure.error for failure in stats.failed_items)
    enumerate_threads.assert_not_awaited()
    for room_key in ("lobby", "dev"):
        assert (tmp_path / "exports" / room_key / "old.yaml").read_text(encoding="utf-8") == "secret"


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
            "mindroom.thread_export.execution.enumerate_room_thread_root_ids",
            new=AsyncMock(return_value=(["$bad:localhost", "$good:localhost"], False)),
        ),
        patch(
            "mindroom.thread_export.execution.refresh_thread_history_from_source",
            new=AsyncMock(side_effect=fetch_side_effect),
        ),
    ):
        stats = await _export_threads_for_client(
            client=Mock(),
            config=config,
            runtime_paths=runtime_paths,
            event_cache=Mock(),
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
            event_cache=Mock(),
            output_dir=tmp_path / "exports",
            rooms=_export_rooms(runtime_paths, None),
        )

    assert stats.rooms_exported == 1
    assert stats.failures == 1
    assert stats.failed_items[0].room_key == "lobby"


@pytest.mark.asyncio
async def test_prefer_cache_bulk_backfills_untrusted_threads(tmp_path: Path) -> None:
    """Many untrusted threads should trigger one bulk warm-up and fail scan-proven-missing roots."""
    config = _config(tmp_path)
    runtime_paths = runtime_paths_for(config)
    _write_matrix_state(tmp_path)
    thread_ids = [f"$t{index}:localhost" for index in range(11)] + ["$missing:localhost"]
    history = [
        ResolvedVisibleMessage.synthetic(
            sender="@alice:localhost",
            body="cached",
            event_id="$t0:localhost",
        ),
    ]
    bulk_stats = Mock(missing_root_ids=frozenset({"$missing:localhost"}))

    with (
        patch(
            "mindroom.thread_export.execution.enumerate_room_thread_root_ids",
            new=AsyncMock(return_value=(thread_ids, False)),
        ),
        patch(
            "mindroom.thread_export.execution.untrusted_cached_thread_ids",
            new=AsyncMock(return_value=tuple(thread_ids)),
        ),
        patch(
            "mindroom.thread_export.execution.bulk_refresh_room_thread_histories",
            new=AsyncMock(return_value=bulk_stats),
        ) as bulk_refresh,
        patch(
            "mindroom.thread_export.execution.fetch_thread_history",
            new=AsyncMock(return_value=history),
        ) as cache_fetch,
    ):
        stats = await _export_threads_for_client(
            client=Mock(),
            config=config,
            runtime_paths=runtime_paths,
            event_cache=Mock(),
            rooms=_export_rooms(runtime_paths, "lobby"),
            output_dir=tmp_path / "exports",
            prefer_cache=True,
        )

    bulk_refresh.assert_awaited_once()
    assert set(bulk_refresh.await_args.kwargs["thread_root_ids"]) == set(thread_ids)
    assert cache_fetch.await_count == 11
    assert stats.failures == 1
    assert stats.failed_items[0].thread_id == "$missing:localhost"
    assert "bulk room scan" in stats.failed_items[0].error


@pytest.mark.asyncio
async def test_prefer_cache_skips_bulk_when_cache_is_trusted(tmp_path: Path) -> None:
    """Rooms whose threads would all serve from cache should not pay for a bulk room scan."""
    config = _config(tmp_path)
    runtime_paths = runtime_paths_for(config)
    _write_matrix_state(tmp_path)
    thread_ids = ["$t0:localhost", "$t1:localhost", "$t2:localhost"]
    history = [
        ResolvedVisibleMessage.synthetic(
            sender="@alice:localhost",
            body="cached",
            event_id="$t0:localhost",
        ),
    ]

    with (
        patch(
            "mindroom.thread_export.execution.enumerate_room_thread_root_ids",
            new=AsyncMock(return_value=(thread_ids, False)),
        ),
        patch(
            "mindroom.thread_export.execution.untrusted_cached_thread_ids",
            new=AsyncMock(return_value=()),
        ),
        patch(
            "mindroom.thread_export.execution.bulk_refresh_room_thread_histories",
            new=AsyncMock(),
        ) as bulk_refresh,
        patch(
            "mindroom.thread_export.execution.fetch_thread_history",
            new=AsyncMock(return_value=history),
        ) as cache_fetch,
    ):
        stats = await _export_threads_for_client(
            client=Mock(),
            config=config,
            runtime_paths=runtime_paths,
            event_cache=Mock(),
            rooms=_export_rooms(runtime_paths, "lobby"),
            output_dir=tmp_path / "exports",
            prefer_cache=True,
        )

    bulk_refresh.assert_not_awaited()
    assert cache_fetch.await_count == 3
    assert stats.failures == 0

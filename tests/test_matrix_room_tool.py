"""Tests for the native matrix_room tool."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import cast
from unittest.mock import AsyncMock, MagicMock, patch

import nio
import pytest
from aiohttp import ClientError

import mindroom.tools  # noqa: F401
from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.custom_tools.matrix_room import MatrixRoomTools
from mindroom.matrix.client import RoomThreadsPageError
from mindroom.tool_system.metadata import TOOL_METADATA, get_tool_by_name
from mindroom.tool_system.runtime_context import ToolRuntimeContext, tool_runtime_context
from tests.conftest import (
    bind_runtime_paths,
    make_conversation_cache_mock,
    make_event_cache_mock,
    runtime_paths_for,
    test_runtime_paths,
)

_NEXT_BATCH_PAGE_TOKEN = "next_batch"  # noqa: S105
_THREAD_PAGE_TOKEN = "tok123"  # noqa: S105


@pytest.fixture(autouse=True)
def _reset_matrix_room_rate_limit() -> None:
    MatrixRoomTools._recent_actions.clear()


def _make_context(
    *,
    room_id: str = "!room:localhost",
    thread_id: str | None = "$thread:localhost",
) -> ToolRuntimeContext:
    runtime_root = Path(tempfile.mkdtemp())
    config = bind_runtime_paths(
        Config(agents={"general": AgentConfig(display_name="General Agent")}),
        test_runtime_paths(runtime_root),
    )
    client = AsyncMock()
    client.rooms = {}
    client.user_id = "@mindroom_general:localhost"
    return ToolRuntimeContext(
        agent_name="general",
        room_id=room_id,
        thread_id=thread_id,
        resolved_thread_id=thread_id,
        requester_id="@user:localhost",
        client=client,
        config=config,
        runtime_paths=runtime_paths_for(config),
        event_cache=make_event_cache_mock(),
        conversation_cache=make_conversation_cache_mock(),
        room=None,
    )


def _make_cached_room(
    room_id: str = "!room:localhost",
    *,
    name: str = "Test Room",
    topic: str = "A test room",
    encrypted: bool = False,
    member_count: int = 3,
    join_rule: str = "invite",
) -> MagicMock:
    room = MagicMock(spec=nio.MatrixRoom)
    room.room_id = room_id
    room.name = name
    room.topic = topic
    room.encrypted = encrypted
    room.member_count = member_count
    room.join_rule = join_rule
    room.canonical_alias = "#test:localhost"
    room.room_version = "10"
    room.guest_access = "forbidden"
    power_levels = MagicMock()
    power_levels.defaults.ban = 50
    power_levels.defaults.invite = 50
    power_levels.defaults.kick = 50
    power_levels.defaults.redact = 50
    power_levels.defaults.state_default = 0
    power_levels.defaults.events_default = 0
    power_levels.defaults.users_default = 0
    power_levels.get_user_level = MagicMock(return_value=100)
    room.power_levels = power_levels
    return room


# --- Registration ---


def test_matrix_room_tool_registered_and_instantiates() -> None:
    """Matrix room tool should be available from metadata registry."""
    config = bind_runtime_paths(
        Config(agents={"general": AgentConfig(display_name="General Agent")}),
        test_runtime_paths(Path(tempfile.mkdtemp())),
    )
    assert "matrix_room" in TOOL_METADATA
    assert isinstance(
        get_tool_by_name("matrix_room", runtime_paths_for(config), worker_target=None),
        MatrixRoomTools,
    )


# --- Context required ---


@pytest.mark.asyncio
async def test_matrix_room_requires_runtime_context() -> None:
    """Tool should fail clearly when called without Matrix runtime context."""
    payload = json.loads(await MatrixRoomTools().matrix_room(action="room-info"))
    assert payload["status"] == "error"
    assert payload["tool"] == "matrix_room"
    assert "context" in payload["message"]


# --- Unknown action ---


@pytest.mark.asyncio
async def test_matrix_room_rejects_unsupported_action() -> None:
    """Unsupported actions should return a clear validation error."""
    tool = MatrixRoomTools()
    ctx = _make_context()
    with tool_runtime_context(ctx):
        payload = json.loads(await tool.matrix_room(action="delete"))
    assert payload["status"] == "error"
    assert payload["action"] == "delete"
    assert "Unsupported action" in payload["message"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kwargs", "expected_action", "expected_message"),
    [
        ({"action": None}, "invalid", "action must be a string"),
        ({"action": "threads", "limit": "3"}, "threads", "limit must be an integer"),
        ({"action": "room-info", "room_id": 123}, "room-info", "room_id must be a string"),
    ],
)
async def test_matrix_room_rejects_malformed_arguments(
    kwargs: dict[str, object],
    expected_action: str,
    expected_message: str,
) -> None:
    """Malformed arguments should return structured validation errors."""
    tool = MatrixRoomTools()
    ctx = _make_context()

    with tool_runtime_context(ctx):
        payload = json.loads(await tool.matrix_room(**kwargs))

    assert payload["status"] == "error"
    assert payload["action"] == expected_action
    assert expected_message in payload["message"]


# --- Authorization ---


@pytest.mark.asyncio
async def test_matrix_room_explicit_room_requires_authorization() -> None:
    """Explicit room targeting should enforce authorization checks."""
    tool = MatrixRoomTools()
    ctx = _make_context()
    with tool_runtime_context(ctx):
        payload = json.loads(await tool.matrix_room(action="room-info", room_id="!other:localhost"))
    assert payload["status"] == "error"
    assert "Not authorized" in payload["message"]


@pytest.mark.asyncio
async def test_matrix_room_explicit_cross_room_members_allowed() -> None:
    """Authorized cross-room member lookups should target the requested room."""
    tool = MatrixRoomTools()
    ctx = _make_context()
    members_resp = MagicMock(spec=nio.JoinedMembersResponse)
    members_resp.members = []
    ctx.client.joined_members = AsyncMock(return_value=members_resp)

    with (
        patch("mindroom.custom_tools.matrix_room.room_access_allowed", return_value=True),
        tool_runtime_context(ctx),
    ):
        payload = json.loads(await tool.matrix_room(action="members", room_id="!other:localhost"))

    assert payload["status"] == "ok"
    assert payload["room_id"] == "!other:localhost"
    ctx.client.joined_members.assert_awaited_once_with("!other:localhost")


# --- Rate limiting ---


@pytest.mark.asyncio
async def test_matrix_room_rate_limit_guardrail() -> None:
    """Tool should block rapid repeated actions in the same room context."""
    tool = MatrixRoomTools()
    ctx = _make_context()
    cached_room = _make_cached_room()
    ctx.client.rooms = {"!room:localhost": cached_room}
    create_resp = MagicMock(spec=nio.RoomGetStateEventResponse)
    create_resp.content = {"creator": "@admin:localhost"}
    ctx.client.room_get_state_event = AsyncMock(return_value=create_resp)

    with (
        patch.object(MatrixRoomTools, "_RATE_LIMIT_MAX_ACTIONS", 1),
        patch.object(MatrixRoomTools, "_RATE_LIMIT_WINDOW_SECONDS", 60.0),
        tool_runtime_context(ctx),
    ):
        first = json.loads(await tool.matrix_room(action="room-info"))
        second = json.loads(await tool.matrix_room(action="room-info"))

    assert first["status"] == "ok"
    assert second["status"] == "error"
    assert "Rate limit exceeded" in second["message"]


# --- room-info ---


@pytest.mark.asyncio
async def test_room_info_happy_path() -> None:
    """room-info should return room metadata from cached state."""
    tool = MatrixRoomTools()
    ctx = _make_context()
    cached_room = _make_cached_room()
    ctx.client.rooms = {"!room:localhost": cached_room}
    create_resp = MagicMock(spec=nio.RoomGetStateEventResponse)
    create_resp.content = {"creator": "@admin:localhost"}
    ctx.client.room_get_state_event = AsyncMock(return_value=create_resp)

    with tool_runtime_context(ctx):
        payload = json.loads(await tool.matrix_room(action="room-info"))

    assert payload["status"] == "ok"
    assert payload["action"] == "room-info"
    assert payload["room_id"] == "!room:localhost"
    assert payload["name"] == "Test Room"
    assert payload["topic"] == "A test room"
    assert payload["member_count"] == 3
    assert payload["encrypted"] is False
    assert payload["join_rule"] == "invite"
    assert payload["canonical_alias"] == "#test:localhost"
    assert payload["creator"] == "@admin:localhost"
    assert payload["power_levels_summary"]["ban"] == 50
    ctx.client.room_get_state_event.assert_awaited_once_with("!room:localhost", "m.room.create")


@pytest.mark.asyncio
async def test_room_info_room_not_found() -> None:
    """room-info should fail gracefully when room is not in client state."""
    tool = MatrixRoomTools()
    ctx = _make_context()
    ctx.client.rooms = {}

    with tool_runtime_context(ctx):
        payload = json.loads(await tool.matrix_room(action="room-info"))

    assert payload["status"] == "error"
    assert "Room not found" in payload["message"]


@pytest.mark.asyncio
async def test_room_info_creator_fallback_when_state_fails() -> None:
    """room-info should return None creator when m.room.create state event fails."""
    tool = MatrixRoomTools()
    ctx = _make_context()
    cached_room = _make_cached_room()
    ctx.client.rooms = {"!room:localhost": cached_room}
    ctx.client.room_get_state_event = AsyncMock(
        return_value=MagicMock(spec=nio.RoomGetStateEventError),
    )

    with tool_runtime_context(ctx):
        payload = json.loads(await tool.matrix_room(action="room-info"))

    assert payload["status"] == "ok"
    assert payload["creator"] is None


@pytest.mark.asyncio
async def test_room_info_creator_fallback_when_create_content_malformed() -> None:
    """room-info should ignore malformed m.room.create content instead of crashing."""
    tool = MatrixRoomTools()
    ctx = _make_context()
    cached_room = _make_cached_room()
    ctx.client.rooms = {"!room:localhost": cached_room}
    create_resp = MagicMock(spec=nio.RoomGetStateEventResponse)
    create_resp.content = ["bad-create-content"]
    ctx.client.room_get_state_event = AsyncMock(return_value=create_resp)

    with tool_runtime_context(ctx):
        payload = json.loads(await tool.matrix_room(action="room-info"))

    assert payload["status"] == "ok"
    assert payload["creator"] is None


@pytest.mark.asyncio
async def test_room_info_creator_fallback_when_create_lookup_has_transport_error() -> None:
    """room-info should keep cached metadata when creator lookup has a transport error."""
    tool = MatrixRoomTools()
    ctx = _make_context()
    ctx.client.rooms = {"!room:localhost": _make_cached_room()}
    ctx.client.room_get_state_event = AsyncMock(side_effect=TimeoutError())

    with tool_runtime_context(ctx):
        payload = json.loads(await tool.matrix_room(action="room-info"))

    assert payload["status"] == "ok"
    assert payload["action"] == "room-info"
    assert payload["room_id"] == "!room:localhost"
    assert payload["name"] == "Test Room"
    assert payload["creator"] is None


# --- members ---


@pytest.mark.asyncio
async def test_members_happy_path() -> None:
    """Members should return joined members with power levels."""
    tool = MatrixRoomTools()
    ctx = _make_context()
    cached_room = _make_cached_room()
    cached_room.power_levels.get_user_level = MagicMock(side_effect=lambda uid: 100 if uid == "@admin:localhost" else 0)
    ctx.client.rooms = {"!room:localhost": cached_room}

    members_resp = MagicMock(spec=nio.JoinedMembersResponse)
    members_resp.members = [
        nio.RoomMember("@admin:localhost", "Admin", "mxc://avatar1"),
        nio.RoomMember("@user:localhost", "User", None),
    ]
    ctx.client.joined_members = AsyncMock(return_value=members_resp)

    with tool_runtime_context(ctx):
        payload = json.loads(await tool.matrix_room(action="members"))

    assert payload["status"] == "ok"
    assert payload["action"] == "members"
    assert payload["count"] == 2
    assert payload["members"][0]["user_id"] == "@admin:localhost"
    assert payload["members"][0]["display_name"] == "Admin"
    assert payload["members"][0]["power_level"] == 100
    assert payload["members"][1]["user_id"] == "@user:localhost"
    assert payload["members"][1]["power_level"] == 0
    ctx.client.joined_members.assert_awaited_once_with("!room:localhost")


@pytest.mark.asyncio
async def test_members_error_response() -> None:
    """Members should handle nio error responses."""
    tool = MatrixRoomTools()
    ctx = _make_context()
    ctx.client.joined_members = AsyncMock(return_value=MagicMock(spec=nio.JoinedMembersError))

    with tool_runtime_context(ctx):
        payload = json.loads(await tool.matrix_room(action="members"))

    assert payload["status"] == "error"
    assert "Failed to fetch members" in payload["message"]


@pytest.mark.asyncio
async def test_members_transport_error_returns_structured_error() -> None:
    """Members should convert transport errors into tool payloads."""
    tool = MatrixRoomTools()
    ctx = _make_context()
    ctx.client.joined_members = AsyncMock(side_effect=ClientError("boom"))

    with tool_runtime_context(ctx):
        payload = json.loads(await tool.matrix_room(action="members"))

    assert payload["status"] == "error"
    assert payload["action"] == "members"
    assert "Matrix request failed" in payload["message"]


# --- threads ---

_MOCK_TARGET = "mindroom.custom_tools.matrix_room.get_room_threads_page"


def _thread_event(
    event_id: str = "$thread1",
    sender: str = "@alice:localhost",
    body: str = "Thread root",
    ts: int = 1000,
    reply_count: int = 0,
) -> nio.RoomMessageText:
    src: dict[str, object] = {
        "type": "m.room.message",
        "event_id": event_id,
        "sender": sender,
        "origin_server_ts": ts,
        "content": {"msgtype": "m.text", "body": body},
    }
    if reply_count:
        src["unsigned"] = {"m.relations": {"m.thread": {"count": reply_count}}}
    return cast("nio.RoomMessageText", nio.RoomMessageText.from_dict(src))


def _make_bundled_replacement(
    *,
    event_id: str,
    body: str,
    bundle_key: str | None = None,
    sender: str = "@editor:localhost",
    visible_body: str | None = None,
    msgtype: str = "m.text",
    long_text: dict[str, object] | None = None,
    url: str | None = None,
) -> dict[str, object]:
    new_content: dict[str, object] = {
        "body": body,
        "msgtype": msgtype,
    }
    if visible_body is not None:
        new_content["io.mindroom.visible_body"] = visible_body
    if long_text is not None:
        new_content["io.mindroom.long_text"] = long_text
    if url is not None:
        new_content["url"] = url

    replacement_event: dict[str, object] = {
        "type": "m.room.message",
        "event_id": f"{event_id}-edit",
        "sender": sender,
        "origin_server_ts": 9999,
        "content": {
            "body": f"* {body}",
            "msgtype": "m.text",
            "m.new_content": new_content,
            "m.relates_to": {
                "rel_type": "m.replace",
                "event_id": event_id,
            },
        },
    }
    if bundle_key is None:
        return replacement_event
    return {bundle_key: replacement_event}


@pytest.mark.asyncio
async def test_threads_happy_path() -> None:
    """Threads should return thread roots via get_room_threads_page."""
    tool = MatrixRoomTools()
    ctx = _make_context()

    e1 = _thread_event("$thread1", "@alice:localhost", "Thread root message", 1000, reply_count=5)
    e2 = _thread_event("$thread2", "@bob:localhost", "Another thread", 2000, reply_count=2)

    with tool_runtime_context(ctx), patch(_MOCK_TARGET, return_value=([e1, e2], None)) as mock:
        payload = json.loads(await tool.matrix_room(action="threads"))

    mock.assert_awaited_once()
    assert payload["status"] == "ok"
    assert payload["action"] == "threads"
    assert payload["count"] == 2
    assert payload["next_token"] is None
    assert payload["has_more"] is False
    assert payload["threads"][0]["thread_id"] == "$thread1"
    assert payload["threads"][0]["sender"] == "@alice:localhost"
    assert payload["threads"][0]["body_preview"] == "Thread root message"
    assert payload["threads"][0]["reply_count"] == 5
    assert payload["threads"][1]["thread_id"] == "$thread2"
    assert payload["threads"][1]["reply_count"] == 2


@pytest.mark.asyncio
async def test_threads_precompute_trusted_sender_ids_once_for_previews() -> None:
    """Thread listings should resolve the trust set once and pass it through every preview."""
    tool = MatrixRoomTools()
    ctx = _make_context()
    trusted_sender_ids = frozenset({"@mindroom_general:localhost"})
    events = [
        _thread_event("$thread1", body="Thread root message", reply_count=5),
        _thread_event("$thread2", body="Another thread", reply_count=2),
    ]

    async def _preview(
        event: nio.Event,
        *,
        client: nio.AsyncClient,
        config: Config,
        runtime_paths: object,
        trusted_sender_ids: frozenset[str],
    ) -> str:
        assert client is ctx.client
        assert config is ctx.config
        assert runtime_paths == ctx.runtime_paths
        assert trusted_sender_ids is trusted_sender_ids_for_assertion
        return event.body

    trusted_sender_ids_for_assertion = trusted_sender_ids

    with (
        tool_runtime_context(ctx),
        patch(_MOCK_TARGET, return_value=(events, None)),
        patch(
            "mindroom.custom_tools.matrix_room.trusted_visible_sender_ids",
            return_value=trusted_sender_ids,
        ) as mock_trusted_sender_ids,
        patch(
            "mindroom.custom_tools.matrix_room.thread_root_body_preview",
            new=AsyncMock(side_effect=_preview),
        ) as mock_preview,
    ):
        payload = json.loads(await tool.matrix_room(action="threads"))

    assert payload["status"] == "ok"
    assert [thread["body_preview"] for thread in payload["threads"]] == [
        "Thread root message",
        "Another thread",
    ]
    mock_trusted_sender_ids.assert_called_once_with(ctx.config, ctx.runtime_paths)
    assert mock_preview.await_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("bundle_key", [None, "event", "latest_event"])
async def test_threads_preview_prefers_bundled_replacement_body(
    bundle_key: str | None,
) -> None:
    """Threads should mirror matrix_message across supported bundled edit shapes."""
    tool = MatrixRoomTools()
    ctx = _make_context()

    event = _thread_event("$thread1", body="Original body", reply_count=3)
    event.source["unsigned"] = {
        "m.relations": {
            "m.thread": {"count": 3},
            "m.replace": _make_bundled_replacement(
                event_id="$thread1",
                body="Edited body",
                bundle_key=bundle_key,
            ),
        },
    }

    with tool_runtime_context(ctx), patch(_MOCK_TARGET, return_value=([event], None)):
        payload = json.loads(await tool.matrix_room(action="threads"))

    assert payload["status"] == "ok"
    assert payload["threads"][0]["body_preview"] == "Edited body"
    assert payload["threads"][0]["reply_count"] == 3


@pytest.mark.asyncio
async def test_threads_preview_prefers_trusted_canonical_body_from_bundled_replacement() -> None:
    """Threads should hide transient warmup text for trusted local bundled edits."""
    tool = MatrixRoomTools()
    ctx = _make_context()

    event = _thread_event("$thread1", body="Original body", reply_count=3)
    event.source["unsigned"] = {
        "m.relations": {
            "m.thread": {"count": 3},
            "m.replace": _make_bundled_replacement(
                event_id="$thread1",
                body="Edited body\n\n⏳ Preparing isolated worker...",
                sender="@mindroom_general:localhost",
                visible_body="Edited body",
            ),
        },
    }

    with tool_runtime_context(ctx), patch(_MOCK_TARGET, return_value=([event], None)):
        payload = json.loads(await tool.matrix_room(action="threads"))

    assert payload["status"] == "ok"
    assert payload["threads"][0]["body_preview"] == "Edited body"


@pytest.mark.asyncio
async def test_threads_preview_prefers_nested_bundled_replacement_over_wrapper_preview() -> None:
    """Threads should prefer nested bundled edits over stale wrapper previews."""
    tool = MatrixRoomTools()
    ctx = _make_context()

    wrapper_replacement = _make_bundled_replacement(
        event_id="$thread1",
        body="wrapper body\n\n⏳ Preparing isolated worker...",
        sender="@mindroom_general:localhost",
    )
    wrapper_replacement["latest_event"] = _make_bundled_replacement(
        event_id="$thread1",
        body="Edited body\n\n⏳ Preparing isolated worker...",
        sender="@mindroom_general:localhost",
        visible_body="Edited body",
    )
    event = _thread_event("$thread1", body="Original body", reply_count=3)
    event.source["unsigned"] = {
        "m.relations": {
            "m.thread": {"count": 3},
            "m.replace": wrapper_replacement,
        },
    }

    with tool_runtime_context(ctx), patch(_MOCK_TARGET, return_value=([event], None)):
        payload = json.loads(await tool.matrix_room(action="threads"))

    assert payload["status"] == "ok"
    assert payload["threads"][0]["body_preview"] == "Edited body"


@pytest.mark.asyncio
async def test_threads_preview_prefers_trusted_visible_body_without_bundled_replacement() -> None:
    """Threads should use canonical visible-body metadata for trusted non-bundled roots too."""
    tool = MatrixRoomTools()
    ctx = _make_context()

    event = _thread_event(
        "$thread1",
        sender="@mindroom_general:localhost",
        body="Final root message\n\n⏳ Preparing isolated worker...",
        reply_count=3,
    )
    event.source["content"]["io.mindroom.visible_body"] = "Final root message"

    with tool_runtime_context(ctx), patch(_MOCK_TARGET, return_value=([event], None)):
        payload = json.loads(await tool.matrix_room(action="threads"))

    assert payload["status"] == "ok"
    assert payload["threads"][0]["body_preview"] == "Final root message"


@pytest.mark.asyncio
async def test_threads_preview_resolves_large_file_root_through_canonical_visible_body() -> None:
    """Threads should hydrate large streamed m.file roots before building previews."""
    tool = MatrixRoomTools()
    ctx = _make_context()
    ctx.client.download = AsyncMock(
        return_value=MagicMock(
            spec=nio.DownloadResponse,
            body=json.dumps(
                {
                    "msgtype": "m.text",
                    "body": "Final large root message\n\n⏳ Preparing isolated worker...",
                    "io.mindroom.visible_body": "Final large root message",
                },
            ).encode("utf-8"),
        ),
    )
    event = nio.RoomMessageFile.from_dict(
        {
            "type": "m.room.message",
            "event_id": "$thread-large",
            "sender": "@mindroom_general:localhost",
            "origin_server_ts": 1000,
            "content": {
                "msgtype": "m.file",
                "body": "Preview root...",
                "info": {"mimetype": "application/json"},
                "io.mindroom.long_text": {
                    "version": 2,
                    "encoding": "matrix_event_content_json",
                },
                "url": "mxc://server/thread-large",
            },
            "unsigned": {"m.relations": {"m.thread": {"count": 3}}},
        },
    )

    with tool_runtime_context(ctx), patch(_MOCK_TARGET, return_value=([event], None)):
        payload = json.loads(await tool.matrix_room(action="threads"))

    assert payload["status"] == "ok"
    assert payload["threads"][0]["body_preview"] == "Final large root message"


@pytest.mark.asyncio
async def test_threads_preview_preserves_empty_bundled_replacement_body() -> None:
    """Threads should keep empty bundled latest-edit bodies instead of falling back to stale root text."""
    tool = MatrixRoomTools()
    ctx = _make_context()

    event = _thread_event("$thread1", body="ORIGINAL ROOT", reply_count=3)
    event.source["unsigned"] = {
        "m.relations": {
            "m.thread": {"count": 3},
            "m.replace": _make_bundled_replacement(
                event_id="$thread1",
                body="",
                sender="@mindroom_general:localhost",
            ),
        },
    }

    with tool_runtime_context(ctx), patch(_MOCK_TARGET, return_value=([event], None)):
        payload = json.loads(await tool.matrix_room(action="threads"))

    assert payload["status"] == "ok"
    assert payload["threads"][0]["body_preview"] == ""


@pytest.mark.asyncio
async def test_threads_respects_limit() -> None:
    """Threads should forward the clamped limit and report has_more when next_token present."""
    tool = MatrixRoomTools()
    ctx = _make_context()

    events = [_thread_event(f"$t{i}", body=f"Thread {i}", ts=i * 1000) for i in range(3)]

    with tool_runtime_context(ctx), patch(_MOCK_TARGET, return_value=(events, _NEXT_BATCH_PAGE_TOKEN)) as mock:
        payload = json.loads(await tool.matrix_room(action="threads", limit=3))

    mock.assert_awaited_once_with(ctx.client, "!room:localhost", limit=3, page_token=None)
    assert payload["status"] == "ok"
    assert payload["count"] == 3
    assert payload["has_more"] is True
    assert payload["next_token"] == _NEXT_BATCH_PAGE_TOKEN


def test_threads_max_limit_enforced() -> None:
    """Threads should enforce max limit of 50."""
    assert MatrixRoomTools._thread_limit(999) == 50
    assert MatrixRoomTools._thread_limit(None) == 20
    assert MatrixRoomTools._thread_limit(0) == 1


@pytest.mark.asyncio
async def test_threads_encrypted_event_preview() -> None:
    """Threads should show [encrypted] for MegolmEvent thread roots."""
    tool = MatrixRoomTools()
    ctx = _make_context()

    encrypted_event = MagicMock(spec=nio.MegolmEvent)
    encrypted_event.event_id = "$enc_thread"
    encrypted_event.sender = "@alice:localhost"
    encrypted_event.server_timestamp = 1000
    encrypted_event.source = {}

    with tool_runtime_context(ctx), patch(_MOCK_TARGET, return_value=([encrypted_event], None)):
        payload = json.loads(await tool.matrix_room(action="threads"))

    assert payload["threads"][0]["body_preview"] == "[encrypted]"


@pytest.mark.asyncio
async def test_threads_malformed_thread_metadata_falls_back_to_zero_reply_count() -> None:
    """Malformed m.thread metadata should not crash thread serialization."""
    tool = MatrixRoomTools()
    ctx = _make_context()

    event = _thread_event("$thread1", body="Thread root")
    event.source["unsigned"] = {"m.relations": {"m.thread": []}}

    with tool_runtime_context(ctx), patch(_MOCK_TARGET, return_value=([event], None)):
        payload = json.loads(await tool.matrix_room(action="threads"))

    assert payload["status"] == "ok"
    assert payload["threads"][0]["reply_count"] == 0


@pytest.mark.asyncio
async def test_threads_pagination_token_passthrough() -> None:
    """page_token should be forwarded to get_room_threads_page."""
    tool = MatrixRoomTools()
    ctx = _make_context()

    with tool_runtime_context(ctx), patch(_MOCK_TARGET, return_value=([], None)) as mock:
        payload = json.loads(await tool.matrix_room(action="threads", page_token=_THREAD_PAGE_TOKEN))

    mock.assert_awaited_once_with(ctx.client, "!room:localhost", limit=20, page_token=_THREAD_PAGE_TOKEN)
    assert payload["status"] == "ok"
    assert payload["count"] == 0


@pytest.mark.asyncio
async def test_threads_empty_room() -> None:
    """Threads should return empty list for rooms with no threads."""
    tool = MatrixRoomTools()
    ctx = _make_context()

    with tool_runtime_context(ctx), patch(_MOCK_TARGET, return_value=([], None)):
        payload = json.loads(await tool.matrix_room(action="threads"))

    assert payload["status"] == "ok"
    assert payload["count"] == 0
    assert payload["threads"] == []
    assert payload["next_token"] is None
    assert payload["has_more"] is False


@pytest.mark.asyncio
async def test_threads_last_page() -> None:
    """Last page should have next_token=None and has_more=False."""
    tool = MatrixRoomTools()
    ctx = _make_context()

    e1 = _thread_event("$last", body="Last thread")

    with tool_runtime_context(ctx), patch(_MOCK_TARGET, return_value=([e1], None)):
        payload = json.loads(await tool.matrix_room(action="threads"))

    assert payload["status"] == "ok"
    assert payload["count"] == 1
    assert payload["next_token"] is None
    assert payload["has_more"] is False


@pytest.mark.asyncio
async def test_threads_error_propagation() -> None:
    """RoomThreadsPageError should propagate as structured error."""
    tool = MatrixRoomTools()
    ctx = _make_context()

    with (
        tool_runtime_context(ctx),
        patch(_MOCK_TARGET, side_effect=RoomThreadsPageError(response="forbidden", errcode="M_FORBIDDEN")),
    ):
        payload = json.loads(await tool.matrix_room(action="threads"))

    assert payload["status"] == "error"
    assert payload["action"] == "threads"
    assert payload["errcode"] == "M_FORBIDDEN"
    assert payload["response"] == "forbidden"
    assert "retry_after_ms" not in payload


@pytest.mark.asyncio
async def test_threads_error_with_retry() -> None:
    """RoomThreadsPageError with retry_after_ms should include it in the response."""
    tool = MatrixRoomTools()
    ctx = _make_context()

    with (
        tool_runtime_context(ctx),
        patch(
            _MOCK_TARGET,
            side_effect=RoomThreadsPageError(response="rate limited", errcode="M_LIMIT_EXCEEDED", retry_after_ms=5000),
        ),
    ):
        payload = json.loads(await tool.matrix_room(action="threads"))

    assert payload["status"] == "error"
    assert payload["retry_after_ms"] == 5000
    assert payload["errcode"] == "M_LIMIT_EXCEEDED"


@pytest.mark.asyncio
async def test_threads_limit_clamping_end_to_end() -> None:
    """Limit of 999 should be clamped to 50 before reaching get_room_threads_page."""
    tool = MatrixRoomTools()
    ctx = _make_context()

    with tool_runtime_context(ctx), patch(_MOCK_TARGET, return_value=([], None)) as mock:
        await tool.matrix_room(action="threads", limit=999)

    mock.assert_awaited_once_with(ctx.client, "!room:localhost", limit=50, page_token=None)


@pytest.mark.asyncio
async def test_threads_first_page_forwards_none_token() -> None:
    """First-page requests should explicitly forward page_token=None."""
    tool = MatrixRoomTools()
    ctx = _make_context()

    with tool_runtime_context(ctx), patch(_MOCK_TARGET, return_value=([], None)) as mock:
        await tool.matrix_room(action="threads")

    mock.assert_awaited_once_with(ctx.client, "!room:localhost", limit=20, page_token=None)


# --- state ---


@pytest.mark.asyncio
async def test_state_specific_event_type() -> None:
    """State with event_type should return specific state event content."""
    tool = MatrixRoomTools()
    ctx = _make_context()

    state_resp = MagicMock(spec=nio.RoomGetStateEventResponse)
    state_resp.content = {"name": "My Room"}
    ctx.client.room_get_state_event = AsyncMock(return_value=state_resp)

    with tool_runtime_context(ctx):
        payload = json.loads(await tool.matrix_room(action="state", event_type="m.room.name"))

    assert payload["status"] == "ok"
    assert payload["action"] == "state"
    assert payload["event_type"] == "m.room.name"
    assert payload["content"] == {"name": "My Room"}
    ctx.client.room_get_state_event.assert_awaited_once_with("!room:localhost", "m.room.name", "")


@pytest.mark.asyncio
async def test_state_specific_event_with_state_key() -> None:
    """State with event_type and state_key should pass both to the API."""
    tool = MatrixRoomTools()
    ctx = _make_context()

    state_resp = MagicMock(spec=nio.RoomGetStateEventResponse)
    state_resp.content = {"membership": "join", "displayname": "Alice"}
    ctx.client.room_get_state_event = AsyncMock(return_value=state_resp)

    with tool_runtime_context(ctx):
        payload = json.loads(
            await tool.matrix_room(
                action="state",
                event_type="m.room.member",
                state_key="@alice:localhost",
            ),
        )

    assert payload["status"] == "ok"
    assert payload["content"]["membership"] == "join"
    ctx.client.room_get_state_event.assert_awaited_once_with(
        "!room:localhost",
        "m.room.member",
        "@alice:localhost",
    )


@pytest.mark.asyncio
async def test_state_specific_event_error() -> None:
    """State should handle error when fetching specific state event."""
    tool = MatrixRoomTools()
    ctx = _make_context()
    ctx.client.room_get_state_event = AsyncMock(return_value=MagicMock(spec=nio.RoomGetStateEventError))

    with tool_runtime_context(ctx):
        payload = json.loads(await tool.matrix_room(action="state", event_type="m.room.name"))

    assert payload["status"] == "error"
    assert "Failed to fetch state event" in payload["message"]


@pytest.mark.asyncio
async def test_state_specific_event_transport_error_returns_structured_error() -> None:
    """State event lookups should convert transport errors into tool payloads."""
    tool = MatrixRoomTools()
    ctx = _make_context()
    ctx.client.room_get_state_event = AsyncMock(side_effect=ClientError("boom"))

    with tool_runtime_context(ctx):
        payload = json.loads(await tool.matrix_room(action="state", event_type="m.room.name"))

    assert payload["status"] == "error"
    assert payload["action"] == "state"
    assert "Matrix request failed" in payload["message"]


@pytest.mark.asyncio
async def test_state_full_dump() -> None:
    """State without event_type should return summarized state events."""
    tool = MatrixRoomTools()
    ctx = _make_context()

    state_resp = MagicMock(spec=nio.RoomGetStateResponse)
    state_resp.events = [
        {"type": "m.room.name", "state_key": "", "content": {"name": "Test"}},
        {"type": "m.room.topic", "state_key": "", "content": {"topic": "A topic"}},
        {"type": "m.room.member", "state_key": "@alice:localhost", "content": {"membership": "join"}},
        {"type": "m.room.member", "state_key": "@bob:localhost", "content": {"membership": "join"}},
    ]
    ctx.client.room_get_state = AsyncMock(return_value=state_resp)

    with tool_runtime_context(ctx):
        payload = json.loads(await tool.matrix_room(action="state"))

    assert payload["status"] == "ok"
    assert payload["action"] == "state"
    # m.room.member events should be elided from the events list
    assert payload["count"] == 2
    event_types = [e["type"] for e in payload["events"]]
    assert "m.room.member" not in event_types
    assert "m.room.name" in event_types
    assert "m.room.topic" in event_types
    # But state_summary should count all types
    assert payload["state_summary"]["m.room.member"] == 2
    assert payload["state_summary"]["m.room.name"] == 1


@pytest.mark.asyncio
async def test_state_full_dump_caps_at_max() -> None:
    """State full dump should cap at MAX_STATE_EVENTS."""
    tool = MatrixRoomTools()
    ctx = _make_context()

    state_resp = MagicMock(spec=nio.RoomGetStateResponse)
    state_resp.events = [{"type": f"custom.type.{i}", "state_key": "", "content": {"data": i}} for i in range(150)]
    ctx.client.room_get_state = AsyncMock(return_value=state_resp)

    with tool_runtime_context(ctx):
        payload = json.loads(await tool.matrix_room(action="state"))

    assert payload["status"] == "ok"
    assert payload["count"] == MatrixRoomTools._MAX_STATE_EVENTS


@pytest.mark.asyncio
async def test_state_full_dump_error() -> None:
    """State should handle error when fetching full room state."""
    tool = MatrixRoomTools()
    ctx = _make_context()
    ctx.client.room_get_state = AsyncMock(return_value=MagicMock(spec=nio.RoomGetStateError))

    with tool_runtime_context(ctx):
        payload = json.loads(await tool.matrix_room(action="state"))

    assert payload["status"] == "error"
    assert "Failed to fetch room state" in payload["message"]


@pytest.mark.asyncio
async def test_state_full_dump_transport_error_returns_structured_error() -> None:
    """Full state dumps should convert transport errors into tool payloads."""
    tool = MatrixRoomTools()
    ctx = _make_context()
    ctx.client.room_get_state = AsyncMock(side_effect=TimeoutError())

    with tool_runtime_context(ctx):
        payload = json.loads(await tool.matrix_room(action="state"))

    assert payload["status"] == "error"
    assert payload["action"] == "state"
    assert "Matrix request failed" in payload["message"]


# --- Default room_id ---


@pytest.mark.asyncio
async def test_matrix_room_defaults_to_context_room() -> None:
    """Actions should default room_id to the context room."""
    tool = MatrixRoomTools()
    ctx = _make_context()
    cached_room = _make_cached_room()
    ctx.client.rooms = {"!room:localhost": cached_room}
    create_resp = MagicMock(spec=nio.RoomGetStateEventResponse)
    create_resp.content = {"creator": "@admin:localhost"}
    ctx.client.room_get_state_event = AsyncMock(return_value=create_resp)

    with tool_runtime_context(ctx):
        payload = json.loads(await tool.matrix_room(action="room-info"))

    assert payload["room_id"] == "!room:localhost"


# --- Implied tool auto-include ---


def test_matrix_room_implied_by_matrix_message() -> None:
    """matrix_room should be auto-included when matrix_message is in an agent's tools."""
    assert "matrix_room" in Config.IMPLIED_TOOLS.get("matrix_message", ())

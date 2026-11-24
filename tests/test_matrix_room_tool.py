"""Tests for the native matrix_room tool."""

from __future__ import annotations

import json
import tempfile
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar, cast
from unittest.mock import ANY, AsyncMock, MagicMock, patch

import nio
import pytest
from aiohttp import ClientError
from pydantic import ValidationError

import mindroom.tools  # noqa: F401
from mindroom.agent_reply_membership import AgentReplyMembershipIndex
from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.custom_tools.attachment_helpers import room_access_allowed
from mindroom.custom_tools.matrix_room import MatrixRoomTools
from mindroom.interactive_models import InteractivePrompt, interactive_prompt_content
from mindroom.matrix.client_visible_messages import ResolvedVisibleMessage
from mindroom.matrix.room_history_reads import RoomThreadsPageError
from mindroom.message_target import MessageTarget
from mindroom.tool_system.metadata import get_tool_by_name
from mindroom.tool_system.registry import builtin_tool_registry
from mindroom.tool_system.runtime_context import ToolRuntimeContext, tool_runtime_context
from tests.access_schema_support import membership_config
from tests.agent_tool_helpers import toolkit_offers
from tests.authorization_helpers import (
    make_test_tool_runtime_context,
)
from tests.conftest import (
    bind_runtime_paths,
    make_conversation_reader_mock,
    make_matrix_client_mock,
    make_relation_lookup,
    runtime_paths_for,
    serve_conversation_reader,
    serve_media_from_download,
    test_runtime_paths,
)
from tests.scheduling_helpers import persist_schedule_writer

if TYPE_CHECKING:
    from collections.abc import Iterator

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
    client.room_get_state = AsyncMock(return_value=nio.RoomGetStateResponse(events=[], room_id=room_id))
    return make_test_tool_runtime_context(
        agent_name="general",
        target=MessageTarget.resolve(
            room_id=room_id,
            thread_id=thread_id,
            reply_to_event_id=None,
        ),
        requester_id="@user:localhost",
        client=client,
        config=config,
        runtime_paths=runtime_paths_for(config),
        relations=make_relation_lookup(),
        conversation_reader=make_conversation_reader_mock(),
        room=None,
    )


def _make_cached_room(room_id: str = "!room:localhost") -> MagicMock:
    room = MagicMock(spec=nio.MatrixRoom)
    room.room_id = room_id
    room.name = "Test Room"
    room.topic = "A test room"
    room.encrypted = False
    room.member_count = 3
    room.join_rule = "invite"
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
    assert "matrix_room" in builtin_tool_registry()
    assert isinstance(
        get_tool_by_name("matrix_room", runtime_paths_for(config), worker_target=None, tools=builtin_tool_registry()),
        MatrixRoomTools,
    )


def test_matrix_room_schema_exposes_supported_actions() -> None:
    """Agents must receive the allowed actions as a schema enum, not an unrestricted string."""
    function = toolkit_offers(MatrixRoomTools(), "matrix_room")
    assert function.parameters["properties"]["action"]["enum"] == [
        "room-info",
        "members",
        "agents",
        "threads",
        "state",
    ]


def test_matrix_room_schema_exposes_workflow_values() -> None:
    """The workflow filter is a schema enum without a done value."""
    function = toolkit_offers(MatrixRoomTools(), "matrix_room")
    assert function.parameters["properties"]["workflow"]["enum"] == ["needs_you", "working", "later", "all"]


# --- Context required ---


@pytest.mark.asyncio
async def test_matrix_room_requires_runtime_context() -> None:
    """Tool should fail clearly when called without Matrix runtime context."""
    payload = json.loads(await MatrixRoomTools().matrix_room(action="room-info"))
    assert payload["status"] == "error"
    assert payload["tool"] == "matrix_room"
    assert "context" in payload["message"]


# --- Argument validation ---


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kwargs",
    [
        {"action": "delete"},
        {"action": None},
        {"action": "threads", "limit": "many"},
        {"action": "room-info", "room_id": 123},
        {"action": "threads", "include_summaries": "maybe"},
        {"action": "threads", "workflow": "done"},
    ],
)
async def test_matrix_room_rejects_malformed_arguments(kwargs: dict[str, object]) -> None:
    """Arguments that disagree with the signature fail call validation before the tool body runs."""
    entrypoint = toolkit_offers(MatrixRoomTools(), "matrix_room").entrypoint
    ctx = _make_context()

    with tool_runtime_context(ctx), pytest.raises(ValidationError):
        await entrypoint(**kwargs)

    ctx.client.room_get_state_event.assert_not_called()


# --- Authorization ---


@pytest.mark.asyncio
@pytest.mark.usefixtures("enforce_turn_authorization")
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


@pytest.mark.asyncio
async def test_members_read_refused_for_a_membership_change_is_read_again() -> None:
    """A member list the client refused for a membership change during the read is read again."""
    tool = MatrixRoomTools()
    ctx = _make_context()
    ctx.client.joined_members = AsyncMock(
        side_effect=[
            nio.LocalProtocolError("room membership changed during lookup"),
            nio.JoinedMembersResponse([nio.RoomMember("@user:localhost", "User", None)], "!room:localhost"),
        ],
    )

    with tool_runtime_context(ctx):
        payload = json.loads(await tool.matrix_room(action="members"))

    assert payload["status"] == "ok"
    assert [member["user_id"] for member in payload["members"]] == ["@user:localhost"]


# --- threads ---

_MOCK_TARGET = "mindroom.custom_tools.matrix_room.get_room_threads_page"


def _thread_event(
    event_id: str = "$thread1",
    sender: str = "@alice:localhost",
    body: str = "Thread root",
    ts: int = 1000,
    reply_count: int = 0,
    latest_reply: dict[str, object] | None = None,
) -> nio.RoomMessageText:
    src: dict[str, object] = {
        "type": "m.room.message",
        "event_id": event_id,
        "sender": sender,
        "origin_server_ts": ts,
        "content": {"msgtype": "m.text", "body": body},
    }
    thread: dict[str, object] = {"count": reply_count} if reply_count else {}
    if latest_reply is not None:
        thread["io.mindroom.latest_reply"] = latest_reply
    if thread:
        src["unsigned"] = {"m.relations": {"m.thread": thread}}
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
    assert "summary" not in payload["threads"][0]
    assert "workflow" not in payload["threads"][0]
    ctx.conversation_reader.read_strict.assert_not_awaited()
    ctx.client.room_get_state.assert_not_awaited()


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
    serve_media_from_download(ctx.client)
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
@pytest.mark.parametrize(
    "unsigned",
    [
        [],
        {"m.relations": []},
        {"m.relations": {"m.thread": "malformed"}},
        {"m.relations": {"m.thread": {"count": True, "latest_event": "malformed"}}},
        {"m.relations": {"m.thread": {"count": "3", "latest_event": {"origin_server_ts": True}}}},
    ],
)
async def test_threads_malformed_thread_metadata_falls_back_to_zero_reply_count(unsigned: object) -> None:
    """Malformed m.thread metadata at any level should not crash thread serialization."""
    tool = MatrixRoomTools()
    ctx = _make_context()

    event = _thread_event("$thread1", body="Thread root")
    event.source["unsigned"] = unsigned

    with tool_runtime_context(ctx), patch(_MOCK_TARGET, return_value=([event], None)):
        payload = json.loads(await tool.matrix_room(action="threads"))

    assert payload["status"] == "ok"
    assert payload["threads"][0]["reply_count"] == 0
    assert "latest_activity_ts" not in payload["threads"][0]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("page_token", "expected_page_token"),
    [
        (None, None),
        (_THREAD_PAGE_TOKEN, _THREAD_PAGE_TOKEN),
        (f"  {_THREAD_PAGE_TOKEN}  ", _THREAD_PAGE_TOKEN),
        ("   ", None),
    ],
)
async def test_threads_page_token_is_forwarded_stripped(
    page_token: str | None,
    expected_page_token: str | None,
) -> None:
    """page_token reaches get_room_threads_page stripped; an omitted or blank one starts from the first page."""
    tool = MatrixRoomTools()
    ctx = _make_context()

    with tool_runtime_context(ctx), patch(_MOCK_TARGET, return_value=([], None)) as mock:
        payload = json.loads(await tool.matrix_room(action="threads", page_token=page_token))

    mock.assert_awaited_once_with(ctx.client, "!room:localhost", limit=20, page_token=expected_page_token)
    assert payload["status"] == "ok"


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
@pytest.mark.parametrize(("limit", "expected_limit"), [(None, 20), (0, 1), (999, 50)])
async def test_threads_limit_clamping_end_to_end(limit: int | None, expected_limit: int) -> None:
    """The thread limit defaults to 20 and is clamped to [1, 50] before reaching get_room_threads_page."""
    tool = MatrixRoomTools()
    ctx = _make_context()

    with tool_runtime_context(ctx), patch(_MOCK_TARGET, return_value=([], None)) as mock:
        await tool.matrix_room(action="threads", limit=limit)

    mock.assert_awaited_once_with(ctx.client, "!room:localhost", limit=expected_limit, page_token=None)


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
async def test_state_strips_event_type_and_state_key() -> None:
    """Padded event_type and state_key are stripped before the state lookup."""
    tool = MatrixRoomTools()
    ctx = _make_context()
    state_resp = MagicMock(spec=nio.RoomGetStateEventResponse)
    state_resp.content = {"name": "Room"}
    ctx.client.room_get_state_event = AsyncMock(return_value=state_resp)

    with tool_runtime_context(ctx):
        payload = json.loads(await tool.matrix_room(action="state", event_type=" m.room.name ", state_key="  "))

    assert payload["status"] == "ok"
    assert payload["state_key"] == ""
    ctx.client.room_get_state_event.assert_awaited_once_with("!room:localhost", "m.room.name", "")


@pytest.mark.asyncio
async def test_state_blank_event_type_reads_whole_room_state() -> None:
    """A blank event_type reads the room state summary instead of one state event."""
    tool = MatrixRoomTools()
    ctx = _make_context()
    state_resp = MagicMock(spec=nio.RoomGetStateResponse)
    state_resp.events = []
    ctx.client.room_get_state = AsyncMock(return_value=state_resp)

    with tool_runtime_context(ctx):
        payload = json.loads(await tool.matrix_room(action="state", event_type="   "))

    assert payload["status"] == "ok"
    ctx.client.room_get_state.assert_awaited_once_with("!room:localhost")
    ctx.client.room_get_state_event.assert_not_called()


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


# --- Implied tool auto-include ---


def test_matrix_room_implied_by_matrix_message() -> None:
    """matrix_room should be auto-included when matrix_message is in an agent's tools."""
    assert "matrix_room" in Config.IMPLIED_TOOLS.get("matrix_message", ())


@pytest.mark.asyncio
async def test_threads_includes_latest_activity_ts() -> None:
    """Threads should expose latest activity separately from root creation time."""
    tool = MatrixRoomTools()
    ctx = _make_context()
    thread_root = _thread_event(
        event_id="$thread-root",
        sender="@alice:localhost",
        ts=1234,
        body="Root message body",
        reply_count=4,
    )
    thread_root.source["unsigned"] = {
        "m.relations": {
            "m.thread": {
                "count": 4,
                "latest_event": {
                    "event_id": "$thread-reply",
                    "origin_server_ts": 5678,
                },
            },
        },
    }

    with (
        patch(
            "mindroom.custom_tools.matrix_room.get_room_threads_page",
            new=AsyncMock(return_value=([thread_root], None)),
        ),
        patch(
            "mindroom.custom_tools.matrix_room.thread_root_body_preview",
            new=AsyncMock(return_value="Resolved root message body"),
        ) as mock_preview,
        tool_runtime_context(ctx),
    ):
        payload = json.loads(await tool.matrix_room(action="threads", limit=1))

    assert payload["status"] == "ok"
    assert payload["threads"] == [
        {
            "thread_id": "$thread-root",
            "sender": "@alice:localhost",
            "timestamp": 1234,
            "latest_activity_ts": 5678,
            "body_preview": "Resolved root message body",
            "reply_count": 4,
        },
    ]
    mock_preview.assert_awaited_once()


@pytest.mark.asyncio
async def test_threads_resolves_large_bundled_replacement_through_canonical_visible_body() -> None:
    """Threads should hydrate large bundled latest edits before building previews."""
    tool = MatrixRoomTools()
    ctx = _make_context()
    serve_media_from_download(ctx.client)
    ctx.client.download = AsyncMock(
        return_value=MagicMock(
            spec=nio.DownloadResponse,
            body=json.dumps(
                {
                    "msgtype": "m.text",
                    "body": "Final bundled edit\n\n⏳ Preparing isolated worker...",
                    "io.mindroom.visible_body": "Final bundled edit",
                },
            ).encode("utf-8"),
        ),
    )
    thread_root = _thread_event(
        event_id="$thread-root",
        sender="@alice:localhost",
        ts=1234,
        body="Original root",
        reply_count=4,
    )
    thread_root.source["unsigned"] = {
        "m.relations": {
            "m.thread": {"count": 4},
            "m.replace": _make_bundled_replacement(
                event_id="$thread-root",
                body="Preview latest edit...",
                msgtype="m.file",
                sender="@mindroom_general:localhost",
                long_text={
                    "version": 2,
                    "encoding": "matrix_event_content_json",
                },
                url="mxc://server/thread-root-edit",
            ),
        },
    }

    with (
        patch(
            "mindroom.custom_tools.matrix_room.get_room_threads_page",
            new=AsyncMock(return_value=([thread_root], None)),
        ),
        tool_runtime_context(ctx),
    ):
        payload = json.loads(await tool.matrix_room(action="threads", limit=1))

    assert payload["status"] == "ok"
    assert payload["threads"][0]["body_preview"] == "Final bundled edit"


@pytest.mark.asyncio
async def test_threads_skips_malformed_roots() -> None:
    """Threads should skip malformed roots instead of crashing the whole action."""
    tool = MatrixRoomTools()
    ctx = _make_context()
    thread_root = _thread_event(
        event_id="$thread-root",
        sender="@alice:localhost",
        ts=1234,
        body="Root message body",
        reply_count=4,
    )

    class MalformedThreadRoot:
        event_id: ClassVar[object] = None
        sender: ClassVar[str] = "@broken:localhost"
        server_timestamp: ClassVar[int] = 1234
        source: ClassVar[dict[str, object]] = {
            "type": "m.room.message",
            "content": {"msgtype": "m.text", "body": "broken"},
        }

    with (
        patch(
            "mindroom.custom_tools.matrix_room.get_room_threads_page",
            new=AsyncMock(return_value=([MalformedThreadRoot(), thread_root], None)),
        ),
        patch(
            "mindroom.custom_tools.matrix_room.thread_root_body_preview",
            new=AsyncMock(return_value="Resolved root message body"),
        ) as mock_preview,
        patch("mindroom.custom_tools.matrix_room.logger.warning") as mock_warning,
        tool_runtime_context(ctx),
    ):
        payload = json.loads(await tool.matrix_room(action="threads"))

    assert payload["status"] == "ok"
    assert payload["count"] == 1
    assert payload["threads"] == [
        {
            "thread_id": "$thread-root",
            "sender": "@alice:localhost",
            "timestamp": 1234,
            "body_preview": "Resolved root message body",
            "reply_count": 4,
        },
    ]
    mock_preview.assert_awaited_once_with(
        thread_root,
        client=ctx.client,
        config=ctx.config,
        runtime_paths=ctx.runtime_paths,
        trusted_sender_ids=ANY,
    )
    mock_warning.assert_called_once()


_AGENT_ID = "@mindroom_general:localhost"


def _thread_message(event_id: str, body: str, ts: int) -> ResolvedVisibleMessage:
    return ResolvedVisibleMessage.synthetic(
        sender="@alice:localhost",
        body=body,
        event_id=event_id,
        timestamp=ts,
        thread_id="$thread1",
    )


def _summary_notice(
    event_id: str,
    summary: str,
    ts: int,
    *,
    model: str = "summary-model",
    pinned: bool | None = None,
    sender: str = _AGENT_ID,
) -> ResolvedVisibleMessage:
    metadata: dict[str, object] = {
        "version": 1,
        "summary": summary,
        "message_count": 2,
        "generated_at": f"2026-01-01T00:00:0{ts // 1000}+00:00",
        "model": model,
    }
    if pinned is not None:
        metadata["pinned"] = pinned
    return ResolvedVisibleMessage.synthetic(
        sender=sender,
        body=summary,
        event_id=event_id,
        timestamp=ts,
        content={"msgtype": "m.notice", "body": summary, "io.mindroom.thread_summary": metadata},
        thread_id="$thread1",
    )


def _edited(message: ResolvedVisibleMessage, edit: ResolvedVisibleMessage) -> ResolvedVisibleMessage:
    message.apply_edit(body=edit.body, timestamp=edit.timestamp, latest_event_id=edit.event_id, content=edit.content)
    return message


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("summaries", "expected_summary", "expected_pinned"),
    [
        pytest.param([], None, False, id="no-summary"),
        pytest.param([_summary_notice("$auto", "Automatic title", 3000)], "Automatic title", False, id="automatic"),
        pytest.param(
            [
                _summary_notice("$auto", "Automatic title", 3000),
                _summary_notice("$manual", "Manual title", 4000, model="manual", pinned=True),
            ],
            "Manual title",
            True,
            id="manual-pinned",
        ),
        pytest.param(
            [
                _summary_notice("$manual", "Manual title", 3000, model="manual", pinned=True),
                _summary_notice("$release", "Released title", 4000, model="manual", pinned=False),
                _summary_notice("$auto", "Newer automatic title", 5000),
            ],
            "Newer automatic title",
            False,
            id="later-unpinned",
        ),
        pytest.param(
            [
                _edited(
                    _summary_notice("$manual", "Manual title", 3000, model="manual", pinned=True),
                    _summary_notice("$manual-edit", "Edited title", 5000, model="manual", pinned=True),
                ),
                _summary_notice("$auto", "Automatic title", 4000),
            ],
            "Edited title",
            True,
            id="edited-after-later-summary",
        ),
    ],
)
async def test_threads_show_current_summary_and_pin_state(
    summaries: list[ResolvedVisibleMessage],
    expected_summary: str | None,
    *,
    expected_pinned: bool,
) -> None:
    """Each thread row carries the title the thread shows now and whether automatic summaries keep off it."""
    tool = MatrixRoomTools()
    ctx = _make_context()
    history = [
        _thread_message("$thread1", "Thread root", 1000),
        _thread_message("$reply", "A reply", 2000),
        *summaries,
    ]
    serve_conversation_reader(ctx.conversation_reader, history, room_id="!room:localhost", thread_id="$thread1")

    with tool_runtime_context(ctx), patch(_MOCK_TARGET, return_value=([_thread_event("$thread1")], None)):
        payload = json.loads(await tool.matrix_room(action="threads", include_summaries=True))

    assert payload["status"] == "ok"
    thread = payload["threads"][0]
    assert thread["summary"] == expected_summary
    assert thread["summary_pinned"] is expected_pinned
    ctx.conversation_reader.read_strict.assert_awaited_once_with(
        room_id="!room:localhost",
        thread_id="$thread1",
        limit=ANY,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["truncated", "read-error"])
async def test_threads_omit_summary_when_thread_history_is_incomplete(failure: str) -> None:
    """A thread whose full history is unavailable gets no summary fields rather than a guess from a suffix."""
    tool = MatrixRoomTools()
    ctx = _make_context()
    history = [_summary_notice("$auto", "Automatic title", 3000)]
    serve_conversation_reader(ctx.conversation_reader, history, room_id="!room:localhost", thread_id="$thread1")
    if failure == "truncated":
        ctx.conversation_reader.hydration_was_truncated.return_value = True
    else:
        ctx.conversation_reader.read_strict.side_effect = RuntimeError("history unavailable")

    with tool_runtime_context(ctx), patch(_MOCK_TARGET, return_value=([_thread_event("$thread1")], None)):
        payload = json.loads(await tool.matrix_room(action="threads", include_summaries=True))

    assert payload["status"] == "ok"
    thread = payload["threads"][0]
    assert thread["thread_id"] == "$thread1"
    assert "summary" not in thread
    assert "summary_pinned" not in thread


def _human_pin_context(tmp_path: Path, *, sender: str, room_cached: bool) -> ToolRuntimeContext:
    """Return a context whose thread holds one manual pinned title written by a person."""
    config = membership_config(tmp_path, access={"users": ["@owner:example.com"]})
    client = make_matrix_client_mock()
    if not room_cached:
        client.rooms = {}
    ctx = make_test_tool_runtime_context(
        agent_name="talent",
        target=MessageTarget.resolve(room_id="!room:x", thread_id="$thread1", reply_to_event_id=None),
        requester_id="@owner:example.com",
        client=client,
        config=config,
        runtime_paths=runtime_paths_for(config),
        relations=make_relation_lookup(),
        conversation_reader=make_conversation_reader_mock(),
        room=None,
        agent_reply_memberships=AgentReplyMembershipIndex(),
    )
    notice = _summary_notice("$human", "Human title", 3000, model="manual", pinned=True, sender=sender)
    history = [_thread_message("$thread1", "Thread root", 1000), notice]
    serve_conversation_reader(ctx.conversation_reader, history, room_id="!room:x", thread_id="$thread1")
    return ctx


@pytest.mark.asyncio
@pytest.mark.usefixtures("enforce_turn_authorization")
async def test_threads_show_title_pinned_by_a_person(tmp_path: Path) -> None:
    """A title a person pinned shows up like one the agent pinned."""
    tool = MatrixRoomTools()
    ctx = _human_pin_context(tmp_path, sender="@owner:example.com", room_cached=True)

    with tool_runtime_context(ctx), patch(_MOCK_TARGET, return_value=([_thread_event("$thread1")], None)):
        payload = json.loads(await tool.matrix_room(action="threads", include_summaries=True))

    assert payload["status"] == "ok"
    assert payload["threads"][0]["summary"] == "Human title"
    assert payload["threads"][0]["summary_pinned"] is True


@pytest.mark.asyncio
@pytest.mark.usefixtures("enforce_turn_authorization")
async def test_threads_omit_summary_while_pin_authority_is_pending(tmp_path: Path) -> None:
    """A person's pin that room membership cannot yet authorize leaves the fields out instead of guessing."""
    tool = MatrixRoomTools()
    ctx = _human_pin_context(tmp_path, sender="@outsider:example.com", room_cached=False)

    with tool_runtime_context(ctx), patch(_MOCK_TARGET, return_value=([_thread_event("$thread1")], None)):
        payload = json.loads(await tool.matrix_room(action="threads", include_summaries=True))

    assert payload["status"] == "ok"
    assert payload["threads"][0]["thread_id"] == "$thread1"
    assert "summary" not in payload["threads"][0]
    assert "summary_pinned" not in payload["threads"][0]


@pytest.mark.asyncio
async def test_threads_returns_structured_transport_error() -> None:
    """Threads should convert transport exceptions into structured tool errors."""
    tool = MatrixRoomTools()
    ctx = _make_context()

    with (
        patch(
            "mindroom.custom_tools.matrix_room.get_room_threads_page",
            new=AsyncMock(
                side_effect=RoomThreadsPageError(
                    response="TimeoutError: request timed out",
                ),
            ),
        ),
        tool_runtime_context(ctx),
    ):
        payload = json.loads(await tool.matrix_room(action="threads"))

    assert payload["status"] == "error"
    assert payload["action"] == "threads"
    assert payload["room_id"] == ctx.room_id
    assert payload["response"] == "TimeoutError: request timed out"
    assert "errcode" not in payload
    assert "retry_after_ms" not in payload


@pytest.mark.asyncio
@pytest.mark.parametrize("other_room", [False, True])
async def test_room_info_exposes_only_selected_rooms_conversation(*, other_room: bool) -> None:
    """Current targeting metadata belongs to room discovery and must not leak across rooms."""
    ctx = _make_context(thread_id="$current-thread")
    room_id = "!other:localhost" if other_room else ctx.room_id
    ctx.client.rooms[room_id] = _make_cached_room(room_id)
    with tool_runtime_context(ctx), patch("mindroom.custom_tools.matrix_room.room_access_allowed", return_value=True):
        payload = json.loads(await MatrixRoomTools().matrix_room(action="room-info", room_id=room_id))
    assert payload["status"] == "ok"
    assert payload["thread_id"] == (None if other_room else "$current-thread")
    assert payload["reply_to_event_id"] is None
    assert payload["requester_id"] == ctx.requester_id
    assert payload["agent_name"] == ctx.agent_name


# --- threads: workflow groups ---


def _tag_event(thread_id: str, tag: str, data: dict[str, object] | None = None) -> dict[str, object]:
    return {
        "type": "com.mindroom.thread.tags",
        "state_key": json.dumps([thread_id, tag], separators=(",", ":")),
        "sender": "@user:localhost",
        "origin_server_ts": _RESOLVED_AT_MS,
        "content": {"set_by": "@user:localhost", "set_at": "2026-01-01T00:00:00+00:00", "data": data or {}},
    }


_RESOLVED_AT_MS = int(datetime(2026, 1, 1, tzinfo=UTC).timestamp() * 1000)


_SNOOZE_END_MS = int(datetime(2999, 1, 1, tzinfo=UTC).timestamp() * 1000)


def _workflow_context(events: list[dict[str, object]]) -> ToolRuntimeContext:
    """A context whose room state holds `events` and in which the general agent is replying in `$busy`."""
    ctx = _make_context()
    ctx.client.room_get_state.return_value = nio.RoomGetStateResponse(events=events, room_id="!room:localhost")
    bot = MagicMock(active_thread_ids_for_room=MagicMock(return_value=frozenset({"$busy"})))
    return replace(ctx, orchestrator=MagicMock(agent_bots={"general": bot}))


def _four_roots() -> tuple[list[nio.Event], list[dict[str, object]]]:
    roots = [_thread_event(event_id) for event_id in ("$open", "$resolved", "$snoozed", "$busy")]
    state = [_tag_event("$resolved", "resolved"), _tag_event("$snoozed", "snoozed", {"until": "2999-01-01T00:00:00Z"})]
    return roots, state


@pytest.mark.asyncio
async def test_threads_rows_carry_their_workflow_group() -> None:
    """With a workflow, every row is labelled from one room-state read and the bots' running replies."""
    roots, state = _four_roots()
    ctx = _workflow_context(state)

    with tool_runtime_context(ctx), patch(_MOCK_TARGET, return_value=(roots, None)):
        payload = json.loads(await MatrixRoomTools().matrix_room(action="threads", workflow="all"))

    labels = [{key: row[key] for key in ("workflow", "why", "back_at") if key in row} for row in payload["threads"]]
    assert labels == [
        {"workflow": "needs_you", "why": "your_turn"},
        {"workflow": "done"},
        {"workflow": "later", "back_at": _SNOOZE_END_MS},
        {"workflow": "working"},
    ]
    ctx.client.room_get_state.assert_awaited_once_with("!room:localhost")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("workflow", "expected"),
    [
        ("needs_you", ["$open"]),
        ("working", ["$busy"]),
        ("later", ["$snoozed"]),
        ("all", ["$open", "$resolved", "$snoozed", "$busy"]),
    ],
)
async def test_threads_workflow_filter_keeps_only_the_asked_group(workflow: str, expected: list[str]) -> None:
    """Rows of other groups are dropped before their previews are built."""
    roots, state = _four_roots()
    ctx = _workflow_context(state)

    with (
        tool_runtime_context(ctx),
        patch(_MOCK_TARGET, return_value=(roots, None)),
        patch(
            "mindroom.custom_tools.matrix_room.thread_root_body_preview",
            new=AsyncMock(return_value="preview"),
        ) as mock_preview,
    ):
        payload = json.loads(await MatrixRoomTools().matrix_room(action="threads", workflow=workflow))

    assert [row["thread_id"] for row in payload["threads"]] == expected
    assert mock_preview.await_count == len(expected)


@pytest.mark.asyncio
async def test_threads_filtered_page_can_be_empty_and_still_have_more() -> None:
    """A page with no match keeps its token so the agent can read older threads."""
    ctx = _workflow_context([_tag_event("$a", "resolved"), _tag_event("$b", "resolved")])

    with (
        tool_runtime_context(ctx),
        patch(_MOCK_TARGET, return_value=([_thread_event("$a"), _thread_event("$b")], _THREAD_PAGE_TOKEN)),
    ):
        payload = json.loads(await MatrixRoomTools().matrix_room(action="threads", workflow="needs_you"))

    assert payload["count"] == 0
    assert payload["threads"] == []
    assert payload["next_token"] == _THREAD_PAGE_TOKEN
    assert payload["has_more"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "state_read",
    [
        {"return_value": nio.RoomGetStateError(message="boom")},
        {"side_effect": ClientError("down")},
    ],
)
async def test_threads_state_read_failure_is_an_error(state_read: dict[str, object]) -> None:
    """Unknown groups would hide threads silently, so a failed state read fails the call."""
    ctx = _workflow_context([])
    ctx.client.room_get_state = AsyncMock(**state_read)

    with tool_runtime_context(ctx), patch(_MOCK_TARGET, return_value=([_thread_event("$a")], None)):
        payload = json.loads(await MatrixRoomTools().matrix_room(action="threads", workflow="all"))

    assert payload["status"] == "error"
    assert payload["action"] == "threads"
    assert "threads" not in payload


_APPROVAL_STORE = "mindroom.custom_tools.matrix_room.get_approval_store"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("state", "expected"),
    [
        ([], {"workflow": "needs_you", "why": "approval"}),
        (
            [_tag_event("$t1", "snoozed", {"until": "2999-01-01T00:00:00Z"})],
            {"workflow": "needs_you", "why": "approval"},
        ),
        ([_tag_event("$t1", "resolved")], {"workflow": "done"}),
    ],
)
async def test_threads_row_waits_on_an_approval(state: list[dict[str, object]], expected: dict[str, object]) -> None:
    """An open approval card for the requester wins over a snooze; one older than the resolve is answered."""
    ctx = _workflow_context(state)
    manager = MagicMock(pending_approval_threads=AsyncMock(return_value={"$t1": 5000}))

    with (
        tool_runtime_context(ctx),
        patch(_MOCK_TARGET, return_value=([_thread_event("$t1")], None)),
        patch(_APPROVAL_STORE, return_value=manager),
    ):
        payload = json.loads(await MatrixRoomTools().matrix_room(action="threads", workflow="all"))

    row = payload["threads"][0]
    assert {key: row[key] for key in ("workflow", "why") if key in row} == expected
    call = manager.pending_approval_threads.await_args
    assert call.kwargs["room_id"] == "!room:localhost"
    assert "@user:localhost" in call.kwargs["approver_user_ids"]


@pytest.mark.asyncio
async def test_threads_row_waits_on_a_question_only_for_the_thread_starter() -> None:
    """An agent's question in the newest reply counts only in a thread the requester started, when it asks them."""
    ctx = _workflow_context([])
    persist_schedule_writer(ctx.runtime_paths, _AGENT_ID, entity_name="general")
    prompt = interactive_prompt_content(
        InteractivePrompt(
            creator_agent="general",
            question_text="Pick one",
            options={"👍": "a", "1": "a"},
            option_labels={"👍": "A", "1": "A"},
            source_event_id="$src",
        ),
    )
    now_ms = int(datetime.now(UTC).timestamp() * 1000)
    latest_reply = {"sender": _AGENT_ID, "origin_server_ts": now_ms, "content": {"body": "Pick one", **prompt}}
    to_bob = {**latest_reply, "content": {**latest_reply["content"], "com.mindroom.acting_requester": "@bob:localhost"}}
    roots = [
        _thread_event("$mine", sender="@user:localhost", latest_reply=latest_reply),
        _thread_event("$theirs", sender="@alice:localhost", latest_reply=latest_reply),
        _thread_event("$to_bob", sender="@user:localhost", latest_reply=to_bob),
    ]

    with (
        tool_runtime_context(ctx),
        patch(_MOCK_TARGET, return_value=(roots, None)),
        patch(_APPROVAL_STORE, return_value=None),
    ):
        payload = json.loads(await MatrixRoomTools().matrix_room(action="threads", workflow="needs_you"))

    assert [(row["thread_id"], row["why"]) for row in payload["threads"]] == [
        ("$mine", "question"),
        ("$theirs", "your_turn"),
        ("$to_bob", "your_turn"),
    ]


@pytest.mark.asyncio
async def test_threads_without_an_approval_store_still_lists() -> None:
    """Before the approval domain is ready, rows are labelled from their other facts."""
    ctx = _workflow_context([])

    with (
        tool_runtime_context(ctx),
        patch(_MOCK_TARGET, return_value=([_thread_event("$t1")], None)),
        patch(_APPROVAL_STORE, return_value=None),
    ):
        payload = json.loads(await MatrixRoomTools().matrix_room(action="threads", workflow="all"))

    assert payload["status"] == "ok"
    assert payload["threads"][0]["why"] == "your_turn"


# --- threads: room_id="all" ---


@pytest.fixture
def real_membership(enforce_turn_authorization: None) -> Iterator[None]:
    """Run the real joined-member check of `room_access_allowed`; only who may talk to the agent is allowed."""
    del enforce_turn_authorization
    with patch("mindroom.custom_tools.attachment_helpers.is_sender_allowed_for_responder", return_value=True):
        yield


def _joined(*user_ids: str) -> MagicMock:
    response = MagicMock(spec=nio.JoinedMembersResponse)
    response.members = [MagicMock(user_id=user_id) for user_id in user_ids]
    return response


def _all_rooms_context(shared_members: object = None, **members: object) -> ToolRuntimeContext:
    """The agent is in three rooms; the requester is in the current room and `!shared`, not in `!private`.

    `members` adds or replaces rooms by their localpart, as their joined-member answer (an exception is raised).
    """
    ctx = _workflow_context([])
    joined = {
        "!room:localhost": _joined("@user:localhost", _AGENT_ID),
        "!shared:localhost": shared_members or _joined("@user:localhost", _AGENT_ID),
        "!private:localhost": _joined(_AGENT_ID),
        **{f"!{name}:localhost": answer for name, answer in members.items()},
    }
    ctx.client.rooms = {room_id: MagicMock() for room_id in joined}

    async def joined_members(room_id: str) -> object:
        if isinstance(answer := joined[room_id], Exception):
            raise answer
        return answer

    ctx.client.joined_members = AsyncMock(side_effect=joined_members)
    ctx.client.room_get_state = AsyncMock(
        side_effect=lambda room_id: nio.RoomGetStateResponse(events=[], room_id=room_id),
    )
    return ctx


def _pages(pages: dict[str, object]) -> AsyncMock:
    """Answer `get_room_threads_page` per room; an exception value is raised."""

    async def page(_client: object, room_id: str, **_kwargs: object) -> object:
        result = pages.get(room_id, ([], None))
        if isinstance(result, Exception):
            raise result
        return result

    return AsyncMock(side_effect=page)


def _read_rooms(mock: AsyncMock) -> set[str]:
    return {call.args[1] for call in mock.await_args_list}


_ONE_ROOT_EACH = {
    "!room:localhost": ([_thread_event("$here", ts=3000)], None),
    "!shared:localhost": ([_thread_event("$there", ts=2000)], None),
    "!private:localhost": ([_thread_event("$secret", ts=1000)], None),
}


@pytest.mark.asyncio
@pytest.mark.usefixtures("real_membership")
async def test_threads_all_rooms_skips_rooms_the_requester_is_not_in() -> None:
    """A room the agent is in and the requester is not is never read."""
    ctx = _all_rooms_context()
    pages = _pages(_ONE_ROOT_EACH)

    with tool_runtime_context(ctx), patch(_MOCK_TARGET, new=pages):
        payload = json.loads(await MatrixRoomTools().matrix_room(action="threads", room_id="all", workflow="all"))

    assert payload["status"] == "ok"
    assert payload["room_id"] == "all"
    assert [(row["thread_id"], row["room_id"]) for row in payload["threads"]] == [
        ("$here", "!room:localhost"),
        ("$there", "!shared:localhost"),
    ]
    assert "failed_room_ids" not in payload
    assert "!private:localhost" not in _read_rooms(pages) | {
        c.args[0] for c in ctx.client.room_get_state.await_args_list
    }


@pytest.mark.asyncio
@pytest.mark.usefixtures("real_membership")
async def test_threads_all_rooms_treats_an_unreadable_membership_as_not_shared() -> None:
    """A room whose member list cannot be read counts as not shared, not as failed."""
    ctx = _all_rooms_context(shared_members=nio.JoinedMembersError(message="boom"))

    with tool_runtime_context(ctx), patch(_MOCK_TARGET, new=_pages(_ONE_ROOT_EACH)):
        payload = json.loads(await MatrixRoomTools().matrix_room(action="threads", room_id="all"))

    assert [row["room_id"] for row in payload["threads"]] == ["!room:localhost"]
    assert "failed_room_ids" not in payload


@pytest.mark.asyncio
@pytest.mark.usefixtures("real_membership")
@pytest.mark.parametrize(("limit", "expected"), [(None, ["$asked", "$new", "$old"]), (2, ["$asked", "$new"])])
async def test_threads_all_rooms_puts_waiting_rows_first_then_newest(limit: int | None, expected: list[str]) -> None:
    """Rows that wait on an approval lead, then the newest activity; the cut sets has_more."""
    ctx = _all_rooms_context()
    pages = _pages(
        {
            "!room:localhost": ([_thread_event("$new", ts=9000)], None),
            "!shared:localhost": ([_thread_event("$old", ts=5000), _thread_event("$asked", ts=1000)], None),
        },
    )
    approvals = {"!shared:localhost": {"$asked": 1}}
    manager = MagicMock(
        pending_approval_threads=AsyncMock(side_effect=lambda *, room_id, **_: approvals.get(room_id, {})),
    )

    with tool_runtime_context(ctx), patch(_MOCK_TARGET, new=pages), patch(_APPROVAL_STORE, return_value=manager):
        payload = json.loads(
            await MatrixRoomTools().matrix_room(action="threads", room_id="all", workflow="needs_you", limit=limit),
        )

    assert [row["thread_id"] for row in payload["threads"]] == expected
    assert payload["has_more"] is (limit == 2)


@pytest.mark.asyncio
@pytest.mark.usefixtures("real_membership")
async def test_threads_all_rooms_reports_more_when_a_room_has_older_threads() -> None:
    """A room with older threads makes has_more true, but there is no cross-room token."""
    ctx = _all_rooms_context()
    pages = _pages({"!shared:localhost": ([_thread_event("$there")], _THREAD_PAGE_TOKEN)})

    with tool_runtime_context(ctx), patch(_MOCK_TARGET, new=pages):
        payload = json.loads(await MatrixRoomTools().matrix_room(action="threads", room_id="all"))

    assert payload["has_more"] is True
    assert payload["next_token"] is None


@pytest.mark.asyncio
@pytest.mark.usefixtures("real_membership")
async def test_threads_all_rooms_names_a_room_it_could_not_read() -> None:
    """A failing room is named and the other rooms are still listed."""
    ctx = _all_rooms_context()
    pages = _pages({**_ONE_ROOT_EACH, "!shared:localhost": RoomThreadsPageError(response="boom")})

    with tool_runtime_context(ctx), patch(_MOCK_TARGET, new=pages):
        payload = json.loads(await MatrixRoomTools().matrix_room(action="threads", room_id="all", workflow="all"))

    assert payload["status"] == "ok"
    assert [row["thread_id"] for row in payload["threads"]] == ["$here"]
    assert payload["failed_room_ids"] == ["!shared:localhost"]


@pytest.mark.asyncio
@pytest.mark.usefixtures("real_membership")
async def test_threads_all_rooms_refuses_a_page_token() -> None:
    """Paging happens in one named room."""
    ctx = _all_rooms_context()
    pages = _pages(_ONE_ROOT_EACH)

    with tool_runtime_context(ctx), patch(_MOCK_TARGET, new=pages):
        payload = json.loads(
            await MatrixRoomTools().matrix_room(action="threads", room_id="all", page_token=_THREAD_PAGE_TOKEN),
        )

    assert payload["status"] == "error"
    assert "page_token" in payload["message"]
    pages.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.usefixtures("real_membership")
async def test_threads_all_rooms_reads_summaries_only_for_returned_rows() -> None:
    """Summaries are read after the cut, in each returned row's own room."""
    ctx = _all_rooms_context()
    pages = _pages(
        {
            "!room:localhost": ([_thread_event("$a", ts=3000)], None),
            "!shared:localhost": ([_thread_event("$b", ts=2000), _thread_event("$c", ts=1000)], None),
        },
    )

    with (
        tool_runtime_context(ctx),
        patch(_MOCK_TARGET, new=pages),
        patch("mindroom.custom_tools.matrix_room.current_thread_summary", new=AsyncMock(return_value=None)) as summary,
    ):
        await MatrixRoomTools().matrix_room(action="threads", room_id="all", limit=2, include_summaries=True)

    assert sorted(call.args[1:3] for call in summary.await_args_list) == [
        ("!room:localhost", "$a"),
        ("!shared:localhost", "$b"),
    ]


@pytest.mark.asyncio
@pytest.mark.usefixtures("real_membership")
async def test_all_is_a_room_only_for_threads() -> None:
    """Other actions treat `all` as an unknown room."""
    ctx = _all_rooms_context()

    with tool_runtime_context(ctx):
        payload = json.loads(await MatrixRoomTools().matrix_room(action="members", room_id="all"))

    assert payload["status"] == "error"
    assert "Unknown room" in payload["message"]


@pytest.mark.asyncio
@pytest.mark.usefixtures("real_membership")
async def test_threads_all_rooms_counts_one_rate_limited_action() -> None:
    """An `all` call counts as one action against the current room's rate limit."""
    ctx = _all_rooms_context()

    with (
        patch.object(MatrixRoomTools, "_RATE_LIMIT_MAX_ACTIONS", 1),
        patch.object(MatrixRoomTools, "_RATE_LIMIT_WINDOW_SECONDS", 60.0),
        tool_runtime_context(ctx),
        patch(_MOCK_TARGET, new=_pages(_ONE_ROOT_EACH)),
    ):
        first = json.loads(await MatrixRoomTools().matrix_room(action="threads", room_id="all"))
        second = json.loads(await MatrixRoomTools().matrix_room(action="threads", room_id="all"))

    assert first["status"] == "ok"
    assert second["status"] == "error"
    assert "Rate limit exceeded" in second["message"]


@pytest.mark.asyncio
@pytest.mark.usefixtures("real_membership")
@pytest.mark.parametrize("workflow", [None, "all"])
async def test_threads_all_rooms_reads_the_invoking_room_only_while_the_requester_is_joined(
    workflow: str | None,
) -> None:
    """The room the request came from is listed only if its member list still holds the requester."""
    ctx = _all_rooms_context(room=_joined(_AGENT_ID))
    pages = _pages(_ONE_ROOT_EACH)

    with tool_runtime_context(ctx), patch(_MOCK_TARGET, new=pages):
        payload = json.loads(await MatrixRoomTools().matrix_room(action="threads", room_id="all", workflow=workflow))

    assert [row["room_id"] for row in payload["threads"]] == ["!shared:localhost"]
    read = _read_rooms(pages) | {call.args[0] for call in ctx.client.room_get_state.await_args_list}
    assert read <= {"!shared:localhost"}


@pytest.mark.asyncio
@pytest.mark.usefixtures("real_membership")
@pytest.mark.parametrize("workflow", [None, "all"])
async def test_threads_all_rooms_skips_a_contact_room_its_owner_has_not_joined(workflow: str | None) -> None:
    """A contact room open to its invited owner by name is not listed until the owner joins it."""
    ctx = _all_rooms_context(contact=_joined(_AGENT_ID))
    record = MagicMock()
    record.party.return_value.owner = "@user:localhost"
    pages = _pages({"!contact:localhost": ([_thread_event("$contact")], None)})

    with (
        tool_runtime_context(ctx),
        patch(_MOCK_TARGET, new=pages),
        patch(
            "mindroom.custom_tools.attachment_helpers.contact_room",
            side_effect=lambda _config, _paths, room_id, _agent: (
                (record, "a") if room_id == "!contact:localhost" else None
            ),
        ),
    ):
        assert await room_access_allowed(ctx, "!contact:localhost", contact_room_ok=True)
        payload = json.loads(await MatrixRoomTools().matrix_room(action="threads", room_id="all", workflow=workflow))

    assert payload["threads"] == []
    assert "!contact:localhost" not in _read_rooms(pages)


@pytest.mark.asyncio
@pytest.mark.usefixtures("real_membership")
async def test_threads_all_rooms_omits_a_room_whose_membership_read_times_out() -> None:
    """A room whose membership cannot be proven is left out silently, never named as failed."""
    ctx = _all_rooms_context(shared_members=TimeoutError("slow"))

    with tool_runtime_context(ctx), patch(_MOCK_TARGET, new=_pages(_ONE_ROOT_EACH)):
        payload = json.loads(await MatrixRoomTools().matrix_room(action="threads", room_id="all", workflow="all"))

    assert [row["thread_id"] for row in payload["threads"]] == ["$here"]
    assert "failed_room_ids" not in payload


@pytest.mark.asyncio
@pytest.mark.usefixtures("real_membership")
async def test_threads_all_rooms_skips_bad_records_and_fails_only_the_room_that_broke() -> None:
    """Malformed state records are skipped; a room whose approval journal read fails is named, the others listed."""
    ctx = _all_rooms_context()
    garbage = ["not an event", {"type": "com.mindroom.thread.tags", "state_key": ["$here"], "content": []}]
    states = {
        "!room:localhost": [*garbage, _tag_event("$here", "resolved")],
        "!shared:localhost": [],
    }
    ctx.client.room_get_state = AsyncMock(
        side_effect=lambda room_id: nio.RoomGetStateResponse(events=states[room_id], room_id=room_id),
    )

    async def approvals(*, room_id: str, **_kwargs: object) -> dict[str | None, int]:
        if room_id == "!shared:localhost":
            msg = "journal unavailable"
            raise RuntimeError(msg)
        return {}

    manager = MagicMock(pending_approval_threads=AsyncMock(side_effect=approvals))
    with (
        tool_runtime_context(ctx),
        patch(_MOCK_TARGET, new=_pages(_ONE_ROOT_EACH)),
        patch(_APPROVAL_STORE, return_value=manager),
    ):
        payload = json.loads(await MatrixRoomTools().matrix_room(action="threads", room_id="all", workflow="all"))

    assert [(row["thread_id"], row["workflow"]) for row in payload["threads"]] == [("$here", "done")]
    assert payload["failed_room_ids"] == ["!shared:localhost"]


@pytest.mark.asyncio
@pytest.mark.usefixtures("real_membership")
async def test_threads_all_rooms_reads_previews_only_for_returned_rows() -> None:
    """Rows are ranked and cut across rooms before any preview is read."""
    ctx = _all_rooms_context()
    pages = _pages(
        {
            "!room:localhost": ([_thread_event("$a", ts=3000), _thread_event("$b", ts=1000)], None),
            "!shared:localhost": ([_thread_event("$c", ts=2000)], None),
        },
    )

    with (
        tool_runtime_context(ctx),
        patch(_MOCK_TARGET, new=pages),
        patch(
            "mindroom.custom_tools.matrix_room.thread_root_body_preview",
            new=AsyncMock(return_value="preview"),
        ) as mock_preview,
    ):
        payload = json.loads(await MatrixRoomTools().matrix_room(action="threads", room_id="all", limit=2))

    assert [row["thread_id"] for row in payload["threads"]] == ["$a", "$c"]
    assert [call.args[0].event_id for call in mock_preview.await_args_list] == ["$a", "$c"]


@pytest.mark.asyncio
async def test_threads_state_read_keeps_the_homeservers_retry_hint() -> None:
    """A rate-limited state read reports its errcode and retry time as a rate-limited page read does."""
    ctx = _workflow_context([])
    ctx.client.room_get_state = AsyncMock(
        return_value=nio.RoomGetStateError(message="slow down", status_code="M_LIMIT_EXCEEDED", retry_after_ms=5000),
    )

    with tool_runtime_context(ctx), patch(_MOCK_TARGET, return_value=([_thread_event("$a")], None)):
        payload = json.loads(await MatrixRoomTools().matrix_room(action="threads", workflow="needs_you"))

    assert payload["status"] == "error"
    assert payload["errcode"] == "M_LIMIT_EXCEEDED"
    assert payload["retry_after_ms"] == 5000


@pytest.mark.asyncio
async def test_threads_without_workflow_lets_a_preview_transport_error_through() -> None:
    """Without `workflow` a listing fails as before: a transport error while reading a preview is not caught."""
    ctx = _make_context()

    with (
        tool_runtime_context(ctx),
        patch(_MOCK_TARGET, return_value=([_thread_event("$a")], None)),
        patch(
            "mindroom.custom_tools.matrix_room.thread_root_body_preview",
            new=AsyncMock(side_effect=ClientError("down")),
        ),
        pytest.raises(ClientError),
    ):
        await MatrixRoomTools().matrix_room(action="threads")
    ctx.client.room_get_state.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.usefixtures("real_membership")
async def test_threads_all_rooms_keeps_rows_when_one_rows_preview_or_summary_fails() -> None:
    """A preview or summary read that fails after the cut drops only that row's preview or summary."""
    ctx = _all_rooms_context()

    async def preview(event: nio.Event, **_kwargs: object) -> str:
        if event.event_id == "$there":
            raise ClientError
        return "preview"

    async def summary(_client: object, _room_id: str, thread_id: str, **_kwargs: object) -> None:
        if thread_id == "$there":
            raise ClientError

    with (
        tool_runtime_context(ctx),
        patch(_MOCK_TARGET, new=_pages(_ONE_ROOT_EACH)),
        patch("mindroom.custom_tools.matrix_room.thread_root_body_preview", new=AsyncMock(side_effect=preview)),
        patch("mindroom.custom_tools.matrix_room.current_thread_summary", new=AsyncMock(side_effect=summary)),
    ):
        payload = json.loads(
            await MatrixRoomTools().matrix_room(action="threads", room_id="all", include_summaries=True),
        )

    assert payload["status"] == "ok"
    assert [(row["thread_id"], row["body_preview"]) for row in payload["threads"]] == [
        ("$here", "preview"),
        ("$there", ""),
    ]


@pytest.mark.asyncio
@pytest.mark.usefixtures("real_membership")
async def test_threads_all_rooms_keeps_a_failed_rooms_retry_hint() -> None:
    """A rate-limited room's retry hint survives in the merged listing."""
    ctx = _all_rooms_context()
    limited = RoomThreadsPageError(response="slow down", errcode="M_LIMIT_EXCEEDED", retry_after_ms=5000)

    with tool_runtime_context(ctx), patch(_MOCK_TARGET, new=_pages({**_ONE_ROOT_EACH, "!shared:localhost": limited})):
        payload = json.loads(await MatrixRoomTools().matrix_room(action="threads", room_id="all", workflow="all"))

    assert payload["failed_room_ids"] == ["!shared:localhost"]
    assert payload["retry_after_ms"] == 5000


@pytest.mark.asyncio
async def test_threads_named_room_reports_an_unreadable_approval_journal() -> None:
    """An approval journal that cannot be read gives the tool's error payload, not an exception."""
    ctx = _workflow_context([])
    manager = MagicMock(pending_approval_threads=AsyncMock(side_effect=RuntimeError("journal unavailable")))

    with (
        tool_runtime_context(ctx),
        patch(_MOCK_TARGET, return_value=([_thread_event("$a")], None)),
        patch(_APPROVAL_STORE, return_value=manager),
    ):
        payload = json.loads(await MatrixRoomTools().matrix_room(action="threads", workflow="all"))

    assert payload["status"] == "error"
    assert payload["action"] == "threads"
    assert "threads" not in payload

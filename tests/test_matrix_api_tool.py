"""Tests for the generic Matrix API tool."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, patch

import nio
import pytest

import mindroom.tools  # noqa: F401
from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.custom_tools.matrix_api import MatrixApiTools
from mindroom.tool_system.metadata import TOOL_METADATA, get_tool_by_name
from mindroom.tool_system.runtime_context import ToolRuntimeContext, tool_runtime_context
from tests.conftest import bind_runtime_paths, make_event_cache_mock, runtime_paths_for, test_runtime_paths

_DEFAULT_CONVERSATION_ACCESS = object()


@pytest.fixture(autouse=True)
def _reset_matrix_api_rate_limit() -> None:
    MatrixApiTools._recent_write_units.clear()


def _make_context(
    *,
    room_id: str = "!room:localhost",
    conversation_cache: object | None = _DEFAULT_CONVERSATION_ACCESS,
) -> ToolRuntimeContext:
    runtime_root = Path(tempfile.mkdtemp())
    config = bind_runtime_paths(
        Config(agents={"general": AgentConfig(display_name="General Agent")}),
        test_runtime_paths(runtime_root),
    )
    client = AsyncMock()
    client.room_send = AsyncMock()
    client.room_get_state_event = AsyncMock()
    client.room_put_state = AsyncMock()
    client.room_redact = AsyncMock()
    client.room_get_event = AsyncMock()
    resolved_conversation_cache = (
        AsyncMock() if conversation_cache is _DEFAULT_CONVERSATION_ACCESS else conversation_cache
    )
    return ToolRuntimeContext(
        agent_name="general",
        room_id=room_id,
        thread_id="$thread:localhost",
        resolved_thread_id="$thread:localhost",
        requester_id="@user:localhost",
        client=client,
        config=config,
        runtime_paths=runtime_paths_for(config),
        conversation_cache=resolved_conversation_cache,
        event_cache=make_event_cache_mock(),
        room=None,
        reply_to_event_id="$reply:localhost",
        storage_path=runtime_root,
    )


def _state_response(
    *,
    content: dict[str, object],
    event_type: str = "com.example.state",
    state_key: str = "",
    room_id: str = "!room:localhost",
) -> nio.RoomGetStateEventResponse:
    return nio.RoomGetStateEventResponse(
        content=content,
        event_type=event_type,
        state_key=state_key,
        room_id=room_id,
    )


def _state_error(
    *,
    message: str = "missing",
    status_code: str = "M_NOT_FOUND",
    room_id: str = "!room:localhost",
) -> nio.RoomGetStateEventError:
    return nio.RoomGetStateEventError(
        message,
        status_code=status_code,
        room_id=room_id,
    )


def _event_response(
    *,
    event_id: str = "$evt:localhost",
    event_type: str = "m.room.message",
    room_id: str = "!room:localhost",
    sender: str = "@alice:localhost",
    origin_server_ts: int = 123,
    content: dict[str, object] | None = None,
) -> nio.RoomGetEventResponse:
    return nio.RoomGetEventResponse.from_dict(
        {
            "content": content or {"body": "hello", "msgtype": "m.text"},
            "event_id": event_id,
            "sender": sender,
            "origin_server_ts": origin_server_ts,
            "room_id": room_id,
            "type": event_type,
        },
    )


def test_matrix_api_tool_registered_and_instantiates() -> None:
    """Matrix API tool should be available from the metadata registry."""
    runtime_root = Path(tempfile.mkdtemp())
    config = bind_runtime_paths(
        Config(agents={"general": AgentConfig(display_name="General Agent")}),
        test_runtime_paths(runtime_root),
    )

    assert "matrix_api" in TOOL_METADATA
    assert isinstance(
        get_tool_by_name("matrix_api", runtime_paths_for(config), worker_target=None),
        MatrixApiTools,
    )


@pytest.mark.asyncio
async def test_matrix_api_requires_runtime_context() -> None:
    """Tool should fail clearly when no Matrix runtime context is available."""
    payload = json.loads(await MatrixApiTools().matrix_api(action="send_event"))

    assert payload["status"] == "error"
    assert payload["tool"] == "matrix_api"
    assert "context" in payload["message"]


@pytest.mark.asyncio
async def test_matrix_api_send_event_happy_path() -> None:
    """send_event should call room_send and return the event id."""
    tool = MatrixApiTools()
    ctx = _make_context()
    ctx.client.room_send.return_value = nio.RoomSendResponse(
        event_id="$send:localhost",
        room_id=ctx.room_id,
    )

    with (
        patch("mindroom.custom_tools.matrix_api.logger.warning"),
        tool_runtime_context(ctx),
    ):
        payload = json.loads(
            await tool.matrix_api(
                action="send_event",
                event_type="com.example.event",
                content={"body": "hello"},
            ),
        )

    assert payload == {
        "action": "send_event",
        "event_id": "$send:localhost",
        "event_type": "com.example.event",
        "room_id": ctx.room_id,
        "status": "ok",
        "tool": "matrix_api",
    }
    ctx.client.room_send.assert_awaited_once_with(
        room_id=ctx.room_id,
        message_type="com.example.event",
        content={"body": "hello"},
    )


@pytest.mark.asyncio
async def test_matrix_api_get_state_happy_path() -> None:
    """get_state should return the fetched content."""
    tool = MatrixApiTools()
    ctx = _make_context()
    ctx.client.room_get_state_event.return_value = _state_response(
        content={"enabled": True},
        event_type="com.example.state",
        room_id=ctx.room_id,
    )

    with tool_runtime_context(ctx):
        payload = json.loads(await tool.matrix_api(action="get_state", event_type="com.example.state"))

    assert payload == {
        "action": "get_state",
        "content": {"enabled": True},
        "event_type": "com.example.state",
        "found": True,
        "room_id": ctx.room_id,
        "state_key": "",
        "status": "ok",
        "tool": "matrix_api",
    }
    ctx.client.room_get_state_event.assert_awaited_once_with(
        room_id=ctx.room_id,
        event_type="com.example.state",
        state_key="",
    )


@pytest.mark.asyncio
async def test_matrix_api_put_state_happy_path() -> None:
    """put_state should write state and return the resulting event id."""
    tool = MatrixApiTools()
    ctx = _make_context()
    ctx.client.room_put_state.return_value = nio.RoomPutStateResponse.from_dict(
        {"event_id": "$state:localhost"},
        room_id=ctx.room_id,
    )

    with (
        patch("mindroom.custom_tools.matrix_api.logger.warning"),
        tool_runtime_context(ctx),
    ):
        payload = json.loads(
            await tool.matrix_api(
                action="put_state",
                event_type="com.example.state",
                content={"enabled": True},
            ),
        )

    assert payload == {
        "action": "put_state",
        "event_id": "$state:localhost",
        "event_type": "com.example.state",
        "room_id": ctx.room_id,
        "state_key": "",
        "status": "ok",
        "tool": "matrix_api",
    }
    ctx.client.room_put_state.assert_awaited_once_with(
        room_id=ctx.room_id,
        event_type="com.example.state",
        state_key="",
        content={"enabled": True},
    )


@pytest.mark.asyncio
async def test_matrix_api_redact_happy_path() -> None:
    """Redact should call room_redact and return the redaction event id."""
    tool = MatrixApiTools()
    ctx = _make_context()
    ctx.client.room_redact.return_value = nio.RoomRedactResponse(
        event_id="$redaction:localhost",
        room_id=ctx.room_id,
    )

    with (
        patch("mindroom.custom_tools.matrix_api.logger.warning"),
        tool_runtime_context(ctx),
    ):
        payload = json.loads(
            await tool.matrix_api(
                action="redact",
                event_id="$target:localhost",
                reason="cleanup",
            ),
        )

    assert payload == {
        "action": "redact",
        "reason": "cleanup",
        "redaction_event_id": "$redaction:localhost",
        "room_id": ctx.room_id,
        "status": "ok",
        "target_event_id": "$target:localhost",
        "tool": "matrix_api",
    }
    ctx.client.room_redact.assert_awaited_once_with(
        room_id=ctx.room_id,
        event_id="$target:localhost",
        reason="cleanup",
    )


@pytest.mark.asyncio
async def test_matrix_api_get_event_happy_path() -> None:
    """get_event should return the raw Matrix event even when conversation cache is available."""
    tool = MatrixApiTools()
    ctx = _make_context()
    ctx.conversation_cache.get_event.return_value = _event_response(
        room_id=ctx.room_id,
        content={"body": "edited view", "msgtype": "m.text"},
        origin_server_ts=999,
    )
    ctx.client.room_get_event.return_value = _event_response(
        room_id=ctx.room_id,
        content={"body": "raw body", "msgtype": "m.text"},
        origin_server_ts=123,
    )

    with tool_runtime_context(ctx):
        payload = json.loads(await tool.matrix_api(action="get_event", event_id="$evt:localhost"))

    assert payload == {
        "action": "get_event",
        "event": {
            "content": {"body": "raw body", "msgtype": "m.text"},
            "event_id": "$evt:localhost",
            "origin_server_ts": 123,
            "room_id": ctx.room_id,
            "sender": "@alice:localhost",
            "type": "m.room.message",
        },
        "event_id": "$evt:localhost",
        "event_type": "m.room.message",
        "found": True,
        "origin_server_ts": 123,
        "room_id": ctx.room_id,
        "sender": "@alice:localhost",
        "status": "ok",
        "tool": "matrix_api",
    }
    ctx.conversation_cache.get_event.assert_not_awaited()
    ctx.client.room_get_event.assert_awaited_once_with(ctx.room_id, "$evt:localhost")


@pytest.mark.asyncio
async def test_matrix_api_get_event_falls_back_to_cached_room_lookup_without_conversation_cache() -> None:
    """get_event should call the Matrix client directly when conversation cache is unavailable."""
    tool = MatrixApiTools()
    ctx = _make_context(conversation_cache=None)
    ctx.client.room_get_event = AsyncMock(
        return_value=_event_response(
            room_id=ctx.room_id,
            content={"body": "cached hello", "msgtype": "m.text"},
        ),
    )

    with (
        tool_runtime_context(ctx),
    ):
        payload = json.loads(await tool.matrix_api(action="get_event", event_id="$evt:localhost"))

    assert payload == {
        "action": "get_event",
        "event": {
            "content": {"body": "cached hello", "msgtype": "m.text"},
            "event_id": "$evt:localhost",
            "origin_server_ts": 123,
            "room_id": ctx.room_id,
            "sender": "@alice:localhost",
            "type": "m.room.message",
        },
        "event_id": "$evt:localhost",
        "event_type": "m.room.message",
        "found": True,
        "origin_server_ts": 123,
        "room_id": ctx.room_id,
        "sender": "@alice:localhost",
        "status": "ok",
        "tool": "matrix_api",
    }
    ctx.client.room_get_event.assert_awaited_once_with(ctx.room_id, "$evt:localhost")


@pytest.mark.asyncio
async def test_matrix_api_send_event_dry_run() -> None:
    """send_event dry runs should not call Matrix."""
    tool = MatrixApiTools()
    ctx = _make_context()

    with tool_runtime_context(ctx):
        payload = json.loads(
            await tool.matrix_api(
                action="send_event",
                event_type="com.example.event",
                content={"body": "preview"},
                dry_run=True,
            ),
        )

    assert payload == {
        "action": "send_event",
        "dry_run": True,
        "event_type": "com.example.event",
        "room_id": ctx.room_id,
        "status": "ok",
        "tool": "matrix_api",
        "would_send": {
            "content": {"body": "preview"},
            "event_type": "com.example.event",
        },
    }
    ctx.client.room_send.assert_not_awaited()


@pytest.mark.asyncio
async def test_matrix_api_put_state_dry_run() -> None:
    """put_state dry runs should not call Matrix."""
    tool = MatrixApiTools()
    ctx = _make_context()

    with tool_runtime_context(ctx):
        payload = json.loads(
            await tool.matrix_api(
                action="put_state",
                event_type="com.example.state",
                content={"enabled": True},
                dry_run=True,
            ),
        )

    assert payload == {
        "action": "put_state",
        "dangerous": False,
        "dry_run": True,
        "event_type": "com.example.state",
        "room_id": ctx.room_id,
        "state_key": "",
        "status": "ok",
        "tool": "matrix_api",
        "would_put": {
            "content": {"enabled": True},
            "event_type": "com.example.state",
            "state_key": "",
        },
    }
    ctx.client.room_put_state.assert_not_awaited()


@pytest.mark.asyncio
async def test_matrix_api_put_state_blocks_room_create() -> None:
    """m.room.create should be hard-blocked before any Matrix write."""
    tool = MatrixApiTools()
    ctx = _make_context()

    with tool_runtime_context(ctx):
        payload = json.loads(
            await tool.matrix_api(
                action="put_state",
                event_type="m.room.create",
                content={"creator": "@user:localhost"},
            ),
        )

    assert payload["status"] == "error"
    assert payload["action"] == "put_state"
    assert "blocked" in payload["message"]
    ctx.client.room_put_state.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("event_type", ["m.room.power_levels", "m.room.guest_access"])
async def test_matrix_api_put_state_requires_allow_dangerous(event_type: str) -> None:
    """Dangerous state writes should require explicit opt-in."""
    tool = MatrixApiTools()
    ctx = _make_context()

    with tool_runtime_context(ctx):
        payload = json.loads(
            await tool.matrix_api(
                action="put_state",
                event_type=event_type,
                content={"users": {"@user:localhost": 100}},
            ),
        )

    assert payload["status"] == "error"
    assert payload["event_type"] == event_type
    assert payload["dangerous"] is True
    assert "allow_dangerous" in payload["message"]
    ctx.client.room_put_state.assert_not_awaited()


@pytest.mark.asyncio
async def test_matrix_api_put_state_allow_dangerous_succeeds() -> None:
    """Dangerous state writes should succeed when explicitly allowed."""
    tool = MatrixApiTools()
    ctx = _make_context()
    ctx.client.room_put_state.return_value = nio.RoomPutStateResponse.from_dict(
        {"event_id": "$power:localhost"},
        room_id=ctx.room_id,
    )

    with (
        patch("mindroom.custom_tools.matrix_api.logger.warning"),
        tool_runtime_context(ctx),
    ):
        payload = json.loads(
            await tool.matrix_api(
                action="put_state",
                event_type="m.room.power_levels",
                content={"users": {"@user:localhost": 100}},
                allow_dangerous=True,
            ),
        )

    assert payload["status"] == "ok"
    assert payload["event_id"] == "$power:localhost"
    ctx.client.room_put_state.assert_awaited_once()


@pytest.mark.asyncio
async def test_matrix_api_redact_dry_run() -> None:
    """Redact dry runs should not call Matrix."""
    tool = MatrixApiTools()
    ctx = _make_context()

    with tool_runtime_context(ctx):
        payload = json.loads(
            await tool.matrix_api(
                action="redact",
                event_id="$target:localhost",
                reason="preview",
                dry_run=True,
            ),
        )

    assert payload == {
        "action": "redact",
        "dry_run": True,
        "reason": "preview",
        "room_id": ctx.room_id,
        "status": "ok",
        "target_event_id": "$target:localhost",
        "tool": "matrix_api",
        "would_redact": {
            "event_id": "$target:localhost",
            "reason": "preview",
        },
    }
    ctx.client.room_redact.assert_not_awaited()


@pytest.mark.asyncio
async def test_matrix_api_get_state_maps_not_found_to_found_false() -> None:
    """M_NOT_FOUND state reads should return found:false instead of an error."""
    tool = MatrixApiTools()
    ctx = _make_context()
    ctx.client.room_get_state_event.return_value = _state_error(room_id=ctx.room_id)

    with tool_runtime_context(ctx):
        payload = json.loads(await tool.matrix_api(action="get_state", event_type="com.example.state"))

    assert payload == {
        "action": "get_state",
        "event_type": "com.example.state",
        "found": False,
        "room_id": ctx.room_id,
        "state_key": "",
        "status": "ok",
        "tool": "matrix_api",
    }


@pytest.mark.asyncio
async def test_matrix_api_get_state_returns_normalized_non_not_found_error() -> None:
    """Non-M_NOT_FOUND state read errors should return normalized error details."""
    tool = MatrixApiTools()
    ctx = _make_context()
    ctx.client.room_get_state_event.return_value = _state_error(
        message="forbidden",
        status_code="M_FORBIDDEN",
        room_id=ctx.room_id,
    )

    with tool_runtime_context(ctx):
        payload = json.loads(await tool.matrix_api(action="get_state", event_type="com.example.state"))

    assert payload == {
        "action": "get_state",
        "event_type": "com.example.state",
        "message": "Failed to fetch Matrix state event.",
        "response": "RoomGetStateEventError: M_FORBIDDEN forbidden",
        "room_id": ctx.room_id,
        "state_key": "",
        "status": "error",
        "status_code": "M_FORBIDDEN",
        "tool": "matrix_api",
    }


@pytest.mark.asyncio
async def test_matrix_api_get_event_maps_not_found_to_found_false() -> None:
    """M_NOT_FOUND event reads should return found:false instead of an error."""
    tool = MatrixApiTools()
    ctx = _make_context()
    ctx.client.room_get_event.return_value = nio.RoomGetEventError(
        "missing",
        status_code="M_NOT_FOUND",
    )

    with tool_runtime_context(ctx):
        payload = json.loads(await tool.matrix_api(action="get_event", event_id="$missing:localhost"))

    assert payload == {
        "action": "get_event",
        "event_id": "$missing:localhost",
        "found": False,
        "room_id": ctx.room_id,
        "status": "ok",
        "tool": "matrix_api",
    }
    ctx.conversation_cache.get_event.assert_not_awaited()
    ctx.client.room_get_event.assert_awaited_once_with(ctx.room_id, "$missing:localhost")


@pytest.mark.asyncio
async def test_matrix_api_send_event_blocks_redaction_type() -> None:
    """send_event should reject redaction events so they use the dedicated redact path."""
    tool = MatrixApiTools()
    ctx = _make_context()

    with tool_runtime_context(ctx):
        payload = json.loads(
            await tool.matrix_api(
                action="send_event",
                event_type="m.room.redaction",
                content={"redacts": "$target:localhost"},
            ),
        )

    assert payload["status"] == "error"
    assert payload["action"] == "send_event"
    assert payload["event_type"] == "m.room.redaction"
    assert "redact" in payload["message"]
    ctx.client.room_send.assert_not_awaited()


@pytest.mark.asyncio
async def test_matrix_api_send_event_blocks_dangerous_state_types() -> None:
    """send_event should reject dangerous state event types instead of bypassing put_state guards."""
    tool = MatrixApiTools()
    ctx = _make_context()

    with tool_runtime_context(ctx):
        payload = json.loads(
            await tool.matrix_api(
                action="send_event",
                event_type="m.room.encryption",
                content={"algorithm": "m.megolm.v1.aes-sha2"},
            ),
        )

    assert payload["status"] == "error"
    assert payload["action"] == "send_event"
    assert payload["dangerous"] is True
    assert "put_state" in payload["message"]
    ctx.client.room_send.assert_not_awaited()


@pytest.mark.asyncio
async def test_matrix_api_rate_limit_uses_weighted_budget() -> None:
    """Real writes should consume the shared 8-unit budget with action weights."""
    tool = MatrixApiTools()
    ctx = _make_context()
    ctx.client.room_put_state.return_value = nio.RoomPutStateResponse.from_dict(
        {"event_id": "$state:localhost"},
        room_id=ctx.room_id,
    )
    ctx.client.room_send.return_value = nio.RoomSendResponse(
        event_id="$send:localhost",
        room_id=ctx.room_id,
    )

    with (
        patch("mindroom.custom_tools.matrix_api.logger.warning"),
        tool_runtime_context(ctx),
    ):
        first = json.loads(
            await tool.matrix_api(
                action="put_state",
                event_type="com.example.one",
                content={"value": 1},
            ),
        )
        second = json.loads(
            await tool.matrix_api(
                action="put_state",
                event_type="com.example.two",
                content={"value": 2},
            ),
        )
        third = json.loads(
            await tool.matrix_api(
                action="put_state",
                event_type="com.example.three",
                content={"value": 3},
            ),
        )
        fourth = json.loads(
            await tool.matrix_api(
                action="put_state",
                event_type="com.example.four",
                content={"value": 4},
            ),
        )
        fifth = json.loads(
            await tool.matrix_api(
                action="send_event",
                event_type="com.example.extra",
                content={"value": 5},
            ),
        )

    assert [payload["status"] for payload in (first, second, third, fourth)] == ["ok", "ok", "ok", "ok"]
    assert fifth["status"] == "error"
    assert "Rate limit exceeded" in fifth["message"]
    assert ctx.client.room_put_state.await_count == 4
    ctx.client.room_send.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("room_id", "expected_message"),
    [
        (123, "non-empty Matrix room ID string"),
        (False, "non-empty Matrix room ID string"),
        ("   ", "non-empty Matrix room ID string"),
        ("#lobby:localhost", "!room:server form"),
        ("lobby", "!room:server form"),
    ],
)
async def test_matrix_api_rejects_invalid_room_id(room_id: object, expected_message: str) -> None:
    """Explicit room_id values must be canonical Matrix room IDs."""
    tool = MatrixApiTools()
    ctx = _make_context()

    with tool_runtime_context(ctx):
        payload = json.loads(
            await tool.matrix_api(
                action="send_event",
                room_id=room_id,
                event_type="com.example.event",
                content={"body": "x"},
            ),
        )

    assert payload["status"] == "error"
    assert payload["action"] == "send_event"
    assert expected_message in payload["message"]
    ctx.client.room_send.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kwargs", "field_name"),
    [
        (
            {
                "action": "send_event",
                "event_type": "com.example.event",
                "content": {"body": "preview"},
                "dry_run": "false",
            },
            "dry_run",
        ),
        (
            {
                "action": "put_state",
                "event_type": "m.room.power_levels",
                "content": {"users": {"@user:localhost": 100}},
                "allow_dangerous": "false",
            },
            "allow_dangerous",
        ),
    ],
)
async def test_matrix_api_rejects_non_bool_flags(kwargs: dict[str, object], field_name: str) -> None:
    """Boolean flags must reject stringified truthy values instead of using Python truthiness."""
    tool = MatrixApiTools()
    ctx = _make_context()

    with tool_runtime_context(ctx):
        payload = json.loads(await tool.matrix_api(**kwargs))

    assert payload["status"] == "error"
    assert payload["action"] == kwargs["action"]
    assert field_name in payload["message"]
    ctx.client.room_send.assert_not_awaited()
    ctx.client.room_put_state.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("action", "kwargs"),
    [
        ("send_event", {"event_type": "com.example.event", "content": {"body": "x"}}),
        ("get_state", {"event_type": "com.example.state"}),
        ("put_state", {"event_type": "com.example.state", "content": {"enabled": True}}),
        ("redact", {"event_id": "$evt:localhost"}),
        ("get_event", {"event_id": "$evt:localhost"}),
    ],
)
async def test_matrix_api_cross_room_access_is_denied(action: str, kwargs: dict[str, object]) -> None:
    """Every action should enforce room access checks before touching another room."""
    tool = MatrixApiTools()
    ctx = _make_context()

    with tool_runtime_context(ctx):
        payload = json.loads(await tool.matrix_api(action=action, room_id="!other:localhost", **kwargs))

    assert payload["status"] == "error"
    assert payload["room_id"] == "!other:localhost"
    assert "Not authorized" in payload["message"]
    ctx.client.room_send.assert_not_awaited()
    ctx.client.room_get_state_event.assert_not_awaited()
    ctx.client.room_put_state.assert_not_awaited()
    ctx.client.room_redact.assert_not_awaited()
    ctx.client.room_get_event.assert_not_awaited()
    ctx.conversation_cache.get_event.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("action", "kwargs", "client_attr", "response"),
    [
        (
            "send_event",
            {"event_type": "com.example.event", "content": {"body": "x"}},
            "room_send",
            nio.RoomSendResponse(event_id="$send:localhost", room_id="!other:localhost"),
        ),
        (
            "get_state",
            {"event_type": "com.example.state"},
            "room_get_state_event",
            _state_response(
                content={"enabled": True},
                event_type="com.example.state",
                room_id="!other:localhost",
            ),
        ),
        (
            "put_state",
            {"event_type": "com.example.state", "content": {"enabled": True}},
            "room_put_state",
            nio.RoomPutStateResponse.from_dict(
                {"event_id": "$state:localhost"},
                room_id="!other:localhost",
            ),
        ),
        (
            "redact",
            {"event_id": "$evt:localhost"},
            "room_redact",
            nio.RoomRedactResponse(event_id="$redaction:localhost", room_id="!other:localhost"),
        ),
    ],
)
async def test_matrix_api_cross_room_access_allowed_uses_target_room_id(
    action: str,
    kwargs: dict[str, object],
    client_attr: str,
    response: object,
) -> None:
    """Authorized cross-room actions should dispatch using the requested room id."""
    tool = MatrixApiTools()
    ctx = _make_context()
    getattr(ctx.client, client_attr).return_value = response

    with (
        patch("mindroom.custom_tools.matrix_api.room_access_allowed", return_value=True),
        patch("mindroom.custom_tools.matrix_api.logger.warning"),
        tool_runtime_context(ctx),
    ):
        payload = json.loads(await tool.matrix_api(action=action, room_id="!other:localhost", **kwargs))

    assert payload["status"] == "ok"
    assert payload["room_id"] == "!other:localhost"
    if action == "send_event":
        ctx.client.room_send.assert_awaited_once_with(
            room_id="!other:localhost",
            message_type="com.example.event",
            content={"body": "x"},
        )
    elif action == "get_state":
        ctx.client.room_get_state_event.assert_awaited_once_with(
            room_id="!other:localhost",
            event_type="com.example.state",
            state_key="",
        )
    elif action == "put_state":
        ctx.client.room_put_state.assert_awaited_once_with(
            room_id="!other:localhost",
            event_type="com.example.state",
            state_key="",
            content={"enabled": True},
        )
    elif action == "redact":
        ctx.client.room_redact.assert_awaited_once_with(
            room_id="!other:localhost",
            event_id="$evt:localhost",
            reason=None,
        )


@pytest.mark.asyncio
async def test_matrix_api_cross_room_get_event_uses_target_room_id() -> None:
    """Authorized cross-room get_event should fetch the raw Matrix event for that room."""
    tool = MatrixApiTools()
    ctx = _make_context()
    ctx.client.room_get_event.return_value = _event_response(room_id="!other:localhost")

    with (
        patch("mindroom.custom_tools.matrix_api.room_access_allowed", return_value=True),
        tool_runtime_context(ctx),
    ):
        payload = json.loads(
            await tool.matrix_api(
                action="get_event",
                room_id="!other:localhost",
                event_id="$evt:localhost",
            ),
        )

    assert payload["status"] == "ok"
    assert payload["room_id"] == "!other:localhost"
    ctx.conversation_cache.get_event.assert_not_awaited()
    ctx.client.room_get_event.assert_awaited_once_with("!other:localhost", "$evt:localhost")


@pytest.mark.asyncio
async def test_matrix_api_rejects_invalid_action() -> None:
    """Unsupported actions should return a clear error listing valid options."""
    tool = MatrixApiTools()
    ctx = _make_context()

    with tool_runtime_context(ctx):
        payload = json.loads(await tool.matrix_api(action="delete_room"))

    assert payload["status"] == "error"
    assert payload["action"] == "delete_room"
    assert "send_event" in payload["message"]
    assert "get_event" in payload["message"]


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["send_event", "put_state"])
async def test_matrix_api_rejects_non_dict_content(action: str) -> None:
    """Write actions should require dict content payloads."""
    tool = MatrixApiTools()
    ctx = _make_context()

    with tool_runtime_context(ctx):
        payload = json.loads(
            await tool.matrix_api(
                action=action,
                event_type="com.example.event",
                content="hello",
            ),
        )

    assert payload["status"] == "error"
    assert payload["action"] == action
    assert "dict" in payload["message"]


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["send_event", "get_state", "put_state"])
async def test_matrix_api_rejects_empty_event_type(action: str) -> None:
    """Actions that require event_type should reject blank values."""
    tool = MatrixApiTools()
    ctx = _make_context()

    with tool_runtime_context(ctx):
        payload = json.loads(
            await tool.matrix_api(
                action=action,
                event_type="   ",
                content={"body": "x"},
            ),
        )

    assert payload["status"] == "error"
    assert payload["action"] == action
    assert "event_type" in payload["message"]


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["redact", "get_event"])
async def test_matrix_api_rejects_empty_event_id(action: str) -> None:
    """Actions that require event_id should reject blank values."""
    tool = MatrixApiTools()
    ctx = _make_context()

    with tool_runtime_context(ctx):
        payload = json.loads(await tool.matrix_api(action=action, event_id="   "))

    assert payload["status"] == "error"
    assert payload["action"] == action
    assert "event_id" in payload["message"]


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["get_state", "put_state"])
async def test_matrix_api_rejects_non_string_state_key(action: str) -> None:
    """State actions should require string state keys."""
    tool = MatrixApiTools()
    ctx = _make_context()

    with tool_runtime_context(ctx):
        payload = json.loads(
            await tool.matrix_api(
                action=action,
                event_type="com.example.state",
                state_key=123,
                content={"enabled": True},
            ),
        )

    assert payload["status"] == "error"
    assert payload["action"] == action
    assert "state_key" in payload["message"]


@pytest.mark.asyncio
async def test_matrix_api_preserves_retry_after_ms_in_error_output() -> None:
    """Normalized Matrix errors should keep retry-after details for rate-limited calls."""
    tool = MatrixApiTools()
    ctx = _make_context()
    ctx.client.room_send.return_value = nio.RoomSendError(
        "rate limited",
        status_code="M_LIMIT_EXCEEDED",
        retry_after_ms=5000,
        room_id=ctx.room_id,
    )

    with (
        patch("mindroom.custom_tools.matrix_api.logger.warning"),
        tool_runtime_context(ctx),
    ):
        payload = json.loads(
            await tool.matrix_api(
                action="send_event",
                event_type="com.example.event",
                content={"body": "hello"},
            ),
        )

    assert payload["status"] == "error"
    assert payload["status_code"] == "M_LIMIT_EXCEEDED"
    assert payload["response"] == "RoomSendError: M_LIMIT_EXCEEDED rate limited - retry after 5000ms"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("action", "kwargs", "expected_summary"),
    [
        (
            "send_event",
            {
                "event_type": "com.example.event",
                "content": {"safe": "value", "secret": "dont-log-me"},
            },
            {"content_bytes", "content_keys"},
        ),
        (
            "put_state",
            {
                "event_type": "com.example.state",
                "content": {"enabled": True, "secret": "dont-log-me"},
            },
            {"content_bytes", "content_keys"},
        ),
        (
            "redact",
            {
                "event_id": "$target:localhost",
                "reason": "cleanup",
            },
            set(),
        ),
    ],
)
async def test_matrix_api_audit_logs_real_writes(
    action: str,
    kwargs: dict[str, object],
    expected_summary: set[str],
) -> None:
    """Every real write should emit one summarized warning-level audit record."""
    tool = MatrixApiTools()
    ctx = _make_context()
    ctx.client.room_send.return_value = nio.RoomSendResponse(event_id="$send:localhost", room_id=ctx.room_id)
    ctx.client.room_put_state.return_value = nio.RoomPutStateResponse.from_dict(
        {"event_id": "$state:localhost"},
        room_id=ctx.room_id,
    )
    ctx.client.room_redact.return_value = nio.RoomRedactResponse(
        event_id="$redaction:localhost",
        room_id=ctx.room_id,
    )

    with (
        patch("mindroom.custom_tools.matrix_api.logger.warning") as mock_warning,
        tool_runtime_context(ctx),
    ):
        payload = json.loads(await tool.matrix_api(action=action, **kwargs))

    assert payload["status"] == "ok"
    mock_warning.assert_called_once()
    assert mock_warning.call_args.args[0] == "matrix_api_write_audit"
    audit_payload = mock_warning.call_args.kwargs
    assert audit_payload["action"] == action
    assert audit_payload["agent"] == ctx.agent_name
    assert audit_payload["user_id"] == ctx.requester_id
    assert audit_payload["room_id"] == ctx.room_id
    assert audit_payload["status"] == "ok"
    assert set(audit_payload).issuperset({"action", "agent", "user_id", "room_id", "status"})
    assert set(audit_payload).issuperset(expected_summary)
    assert "dont-log-me" not in repr(audit_payload)

"""Tests for the thread summary tool."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

import mindroom.tools  # noqa: F401
from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.custom_tools.thread_summary import ThreadSummaryTools
from mindroom.thread_summary import (
    _last_summary_counts,
    _thread_locks,
    thread_summary_cache_key,
    update_last_summary_count,
)
from mindroom.tool_system.metadata import TOOL_METADATA, get_tool_by_name
from mindroom.tool_system.runtime_context import ToolRuntimeContext, tool_runtime_context
from tests.conftest import bind_runtime_paths, runtime_paths_for, test_runtime_paths


def _make_context(
    *,
    room_id: str = "!room:localhost",
    thread_id: str | None = "$thread:localhost",
    reply_to_event_id: str | None = None,
) -> ToolRuntimeContext:
    runtime_root = Path(tempfile.mkdtemp())
    config = bind_runtime_paths(
        Config(agents={"general": AgentConfig(display_name="General Agent")}),
        test_runtime_paths(runtime_root),
    )
    return ToolRuntimeContext(
        agent_name="general",
        room_id=room_id,
        thread_id=thread_id,
        resolved_thread_id=thread_id,
        requester_id="@user:localhost",
        client=AsyncMock(),
        config=config,
        runtime_paths=runtime_paths_for(config),
        room=None,
        reply_to_event_id=reply_to_event_id,
        storage_path=None,
    )


@pytest.fixture(autouse=True)
def _clear_summary_counts() -> None:
    """Reset cached summary counts between tests."""
    _last_summary_counts.clear()
    _thread_locks.clear()


def _thread_history(count: int) -> list[dict[str, object]]:
    """Build a fake thread history with a fixed number of messages."""
    return [
        {
            "sender": f"@user{i}:localhost",
            "body": f"Message {i}",
            "event_id": f"$event{i}:localhost",
        }
        for i in range(count)
    ]


def test_thread_summary_tool_registered_and_instantiates() -> None:
    """Thread summary should be available from the metadata registry."""
    runtime_root = Path(tempfile.mkdtemp())
    config = bind_runtime_paths(
        Config(agents={"general": AgentConfig(display_name="General Agent")}),
        test_runtime_paths(runtime_root),
    )

    assert "thread_summary" in TOOL_METADATA
    assert isinstance(
        get_tool_by_name("thread_summary", runtime_paths_for(config), worker_target=None),
        ThreadSummaryTools,
    )


@pytest.mark.asyncio
async def test_thread_summary_tool_requires_runtime_context() -> None:
    """Tool calls should fail clearly outside Matrix runtime context."""
    payload = json.loads(await ThreadSummaryTools().set_thread_summary("summary"))

    assert payload["status"] == "error"
    assert payload["tool"] == "thread_summary"
    assert payload["action"] == "set"
    assert "context" in payload["message"]


@pytest.mark.asyncio
async def test_set_thread_summary_defaults_to_context_room_and_thread() -> None:
    """The tool should default to the current room and resolved thread context."""
    tool = ThreadSummaryTools()
    context = _make_context(thread_id="$ctx-thread:localhost")

    with (
        patch(
            "mindroom.custom_tools.thread_summary.normalize_thread_root_event_id",
            new=AsyncMock(return_value="$ctx-thread:localhost"),
        ) as mock_normalize,
        patch(
            "mindroom.custom_tools.thread_summary.fetch_thread_history",
            new=AsyncMock(return_value=_thread_history(3)),
        ) as mock_history,
        patch(
            "mindroom.custom_tools.thread_summary.send_thread_summary_event",
            new=AsyncMock(return_value="$summary-event:localhost"),
        ) as mock_send,
        tool_runtime_context(context),
    ):
        payload = json.loads(await tool.set_thread_summary("  🧵 Ready\nfor\t review  "))

    assert payload == {
        "action": "set",
        "event_id": "$summary-event:localhost",
        "message_count": 3,
        "room_id": "!room:localhost",
        "status": "ok",
        "summary": "🧵 Ready for review",
        "thread_id": "$ctx-thread:localhost",
        "tool": "thread_summary",
    }
    mock_normalize.assert_awaited_once_with(
        context.client,
        context.room_id,
        "$ctx-thread:localhost",
    )
    mock_history.assert_awaited_once_with(
        context.client,
        context.room_id,
        "$ctx-thread:localhost",
    )
    mock_send.assert_awaited_once_with(
        context.client,
        context.room_id,
        "$ctx-thread:localhost",
        "🧵 Ready for review",
        3,
        "manual",
    )
    assert _last_summary_counts[thread_summary_cache_key(context.room_id, "$ctx-thread:localhost")] == 3


@pytest.mark.asyncio
async def test_set_thread_summary_rejects_blank_room_id() -> None:
    """Explicit blank room IDs should not silently fall back to the context room."""
    tool = ThreadSummaryTools()
    context = _make_context()

    with tool_runtime_context(context):
        payload = json.loads(await tool.set_thread_summary("done", room_id="   "))

    assert payload["status"] == "error"
    assert payload["room_id"] == "   "
    assert payload["message"] == "room_id must be a non-empty string when provided."


@pytest.mark.asyncio
async def test_set_thread_summary_normalizes_explicit_thread_id() -> None:
    """Explicit event IDs should be normalized to the canonical thread root."""
    tool = ThreadSummaryTools()
    context = _make_context(thread_id=None)

    with (
        patch(
            "mindroom.custom_tools.thread_summary.normalize_thread_root_event_id",
            new=AsyncMock(return_value="$thread-root:localhost"),
        ) as mock_normalize,
        patch(
            "mindroom.custom_tools.thread_summary.fetch_thread_history",
            new=AsyncMock(return_value=_thread_history(4)),
        ),
        patch(
            "mindroom.custom_tools.thread_summary.send_thread_summary_event",
            new=AsyncMock(return_value="$summary-event:localhost"),
        ),
        tool_runtime_context(context),
    ):
        payload = json.loads(await tool.set_thread_summary("done", thread_id="$reply-event:localhost"))

    assert payload["status"] == "ok"
    assert payload["thread_id"] == "$thread-root:localhost"
    mock_normalize.assert_awaited_once_with(
        context.client,
        context.room_id,
        "$reply-event:localhost",
    )


@pytest.mark.asyncio
async def test_set_thread_summary_falls_back_to_reply_to_event_id_for_room_timeline_root() -> None:
    """Room-level replies with no active thread context should target the replied-to thread root."""
    tool = ThreadSummaryTools()
    context = _make_context(
        thread_id=None,
        reply_to_event_id="$root-event:localhost",
    )

    with (
        patch(
            "mindroom.custom_tools.thread_summary.normalize_thread_root_event_id",
            new=AsyncMock(return_value="$root-event:localhost"),
        ) as mock_normalize,
        patch(
            "mindroom.custom_tools.thread_summary.fetch_thread_history",
            new=AsyncMock(return_value=_thread_history(3)),
        ),
        patch(
            "mindroom.custom_tools.thread_summary.send_thread_summary_event",
            new=AsyncMock(return_value="$summary-event:localhost"),
        ) as mock_send,
        tool_runtime_context(context),
    ):
        payload = json.loads(await tool.set_thread_summary("done"))

    assert payload["status"] == "ok"
    assert payload["thread_id"] == "$root-event:localhost"
    mock_normalize.assert_awaited_once_with(
        context.client,
        context.room_id,
        "$root-event:localhost",
    )
    mock_send.assert_awaited_once_with(
        context.client,
        context.room_id,
        "$root-event:localhost",
        "done",
        3,
        "manual",
    )


@pytest.mark.asyncio
async def test_set_thread_summary_cross_room_requires_authorization() -> None:
    """Explicit room targeting should enforce the same room access checks as other Matrix tools."""
    tool = ThreadSummaryTools()
    context = _make_context()

    with tool_runtime_context(context):
        payload = json.loads(await tool.set_thread_summary("done", room_id="!other:localhost"))

    assert payload["status"] == "error"
    assert payload["action"] == "set"
    assert payload["room_id"] == "!other:localhost"
    assert "Not authorized" in payload["message"]


@pytest.mark.asyncio
async def test_set_thread_summary_cross_room_does_not_inherit_context_thread() -> None:
    """Cross-room writes should not silently reuse the origin room thread context."""
    tool = ThreadSummaryTools()
    context = _make_context(thread_id="$origin-thread:localhost")

    with (
        patch("mindroom.custom_tools.thread_summary.room_access_allowed", return_value=True),
        tool_runtime_context(context),
    ):
        payload = json.loads(await tool.set_thread_summary("done", room_id="!other:localhost"))

    assert payload["status"] == "error"
    assert payload["action"] == "set"
    assert payload["room_id"] == "!other:localhost"
    assert "thread_id is required" in payload["message"]


@pytest.mark.asyncio
async def test_set_thread_summary_rejects_blank_summary() -> None:
    """Blank summaries should be rejected before any Matrix work starts."""
    tool = ThreadSummaryTools()
    context = _make_context()

    with tool_runtime_context(context):
        payload = json.loads(await tool.set_thread_summary("   "))

    assert payload["status"] == "error"
    assert payload["action"] == "set"
    assert payload["room_id"] == context.room_id
    assert "summary must be a non-empty string" in payload["message"]


@pytest.mark.asyncio
async def test_set_thread_summary_rejects_non_string_summary() -> None:
    """Malformed tool args should return the normal error payload instead of crashing."""
    tool = ThreadSummaryTools()
    context = _make_context()
    invalid_summary: Any = 123

    with tool_runtime_context(context):
        payload = json.loads(await tool.set_thread_summary(invalid_summary))

    assert payload["status"] == "error"
    assert payload["action"] == "set"
    assert payload["room_id"] == context.room_id
    assert "summary must be a non-empty string" in payload["message"]


@pytest.mark.asyncio
async def test_set_thread_summary_rejects_overlong_summary() -> None:
    """Oversized summaries should fail before any Matrix work starts."""
    tool = ThreadSummaryTools()
    context = _make_context()

    with tool_runtime_context(context):
        payload = json.loads(await tool.set_thread_summary("x" * 501))

    assert payload["status"] == "error"
    assert payload["room_id"] == context.room_id
    assert payload["message"] == "summary must be 500 characters or fewer after whitespace normalization."


@pytest.mark.asyncio
async def test_set_thread_summary_send_failure_leaves_cache_unchanged() -> None:
    """A failed send should not update the last-summary count cache."""
    tool = ThreadSummaryTools()
    context = _make_context(thread_id="$ctx-thread:localhost")
    update_last_summary_count(context.room_id, "$ctx-thread:localhost", 2)

    with (
        patch(
            "mindroom.custom_tools.thread_summary.normalize_thread_root_event_id",
            new=AsyncMock(return_value="$ctx-thread:localhost"),
        ),
        patch(
            "mindroom.custom_tools.thread_summary.fetch_thread_history",
            new=AsyncMock(return_value=_thread_history(5)),
        ),
        patch(
            "mindroom.custom_tools.thread_summary.send_thread_summary_event",
            new=AsyncMock(return_value=None),
        ),
        tool_runtime_context(context),
    ):
        payload = json.loads(await tool.set_thread_summary("failed write"))

    assert payload["status"] == "error"
    assert payload["thread_id"] == "$ctx-thread:localhost"
    assert _last_summary_counts[thread_summary_cache_key(context.room_id, "$ctx-thread:localhost")] == 2


@pytest.mark.asyncio
async def test_set_thread_summary_returns_error_when_normalize_raises() -> None:
    """Normalization exceptions should return the standard error payload."""
    tool = ThreadSummaryTools()
    context = _make_context(thread_id=None)

    with (
        patch(
            "mindroom.custom_tools.thread_summary.normalize_thread_root_event_id",
            new=AsyncMock(side_effect=TimeoutError("timed out")),
        ),
        tool_runtime_context(context),
    ):
        payload = json.loads(await tool.set_thread_summary("done", thread_id="$reply-event:localhost"))

    assert payload["status"] == "error"
    assert payload["thread_id"] == "$reply-event:localhost"
    assert payload["message"] == "Failed to resolve a canonical thread root for the target event."


@pytest.mark.asyncio
async def test_set_thread_summary_returns_error_when_fetch_raises() -> None:
    """History fetch exceptions should return the standard error payload."""
    tool = ThreadSummaryTools()
    context = _make_context(thread_id="$ctx-thread:localhost")

    with (
        patch(
            "mindroom.custom_tools.thread_summary.normalize_thread_root_event_id",
            new=AsyncMock(return_value="$ctx-thread:localhost"),
        ),
        patch(
            "mindroom.custom_tools.thread_summary.fetch_thread_history",
            new=AsyncMock(side_effect=TimeoutError("timed out")),
        ),
        tool_runtime_context(context),
    ):
        payload = json.loads(await tool.set_thread_summary("done"))

    assert payload["status"] == "error"
    assert payload["thread_id"] == "$ctx-thread:localhost"
    assert payload["message"] == "Failed to fetch thread history for the target thread."


@pytest.mark.asyncio
async def test_set_thread_summary_returns_error_when_send_raises() -> None:
    """Send exceptions should return the standard error payload."""
    tool = ThreadSummaryTools()
    context = _make_context(thread_id="$ctx-thread:localhost")
    update_last_summary_count(context.room_id, "$ctx-thread:localhost", 2)

    with (
        patch(
            "mindroom.custom_tools.thread_summary.normalize_thread_root_event_id",
            new=AsyncMock(return_value="$ctx-thread:localhost"),
        ),
        patch(
            "mindroom.custom_tools.thread_summary.fetch_thread_history",
            new=AsyncMock(return_value=_thread_history(5)),
        ),
        patch(
            "mindroom.custom_tools.thread_summary.send_thread_summary_event",
            new=AsyncMock(side_effect=TimeoutError("timed out")),
        ),
        tool_runtime_context(context),
    ):
        payload = json.loads(await tool.set_thread_summary("failed write"))

    assert payload["status"] == "error"
    assert payload["thread_id"] == "$ctx-thread:localhost"
    assert payload["message"] == "Failed to send thread summary event."
    assert _last_summary_counts[thread_summary_cache_key(context.room_id, "$ctx-thread:localhost")] == 2

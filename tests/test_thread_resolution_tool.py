"""Tests for the thread resolution tool."""

from __future__ import annotations

import json
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

import mindroom.tools  # noqa: F401
from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.custom_tools.thread_resolution import ThreadResolutionTools
from mindroom.thread_resolution import ThreadResolutionError, ThreadResolutionRecord
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


def _record(thread_root_id: str) -> ThreadResolutionRecord:
    timestamp = datetime(2026, 3, 21, 19, 2, 3, tzinfo=UTC)
    return ThreadResolutionRecord(
        room_id="!room:localhost",
        thread_root_id=thread_root_id,
        status="resolved",
        resolved_by="@user:localhost",
        resolved_at=timestamp,
        updated_at=timestamp,
    )


def test_thread_resolution_tool_registered_and_instantiates() -> None:
    """Thread resolution should be available from the metadata registry."""
    runtime_root = Path(tempfile.mkdtemp())
    config = bind_runtime_paths(
        Config(agents={"general": AgentConfig(display_name="General Agent")}),
        test_runtime_paths(runtime_root),
    )

    assert "thread_resolution" in TOOL_METADATA
    assert isinstance(
        get_tool_by_name("thread_resolution", runtime_paths_for(config), worker_target=None),
        ThreadResolutionTools,
    )


@pytest.mark.asyncio
async def test_thread_resolution_tool_requires_runtime_context() -> None:
    """Tool calls should fail clearly outside Matrix runtime context."""
    payload = json.loads(await ThreadResolutionTools().resolve_thread())

    assert payload["status"] == "error"
    assert payload["tool"] == "thread_resolution"
    assert "context" in payload["message"]


@pytest.mark.asyncio
async def test_unresolve_thread_requires_runtime_context() -> None:
    """Unresolve should fail clearly outside Matrix runtime context."""
    payload = json.loads(await ThreadResolutionTools().unresolve_thread())

    assert payload["status"] == "error"
    assert payload["tool"] == "thread_resolution"
    assert "context" in payload["message"]


@pytest.mark.asyncio
async def test_resolve_thread_defaults_to_context_resolved_thread_id() -> None:
    """Resolve should use the active resolved thread root when not overridden."""
    tool = ThreadResolutionTools()
    context = _make_context(thread_id="$ctx-thread:localhost")

    with (
        patch(
            "mindroom.custom_tools.thread_resolution.normalize_thread_root_event_id",
            new=AsyncMock(return_value="$ctx-thread:localhost"),
        ) as mock_normalize,
        patch(
            "mindroom.custom_tools.thread_resolution.set_thread_resolved",
            new=AsyncMock(return_value=_record("$ctx-thread:localhost")),
        ) as mock_set,
        tool_runtime_context(context),
    ):
        payload = json.loads(await tool.resolve_thread())

    assert payload["status"] == "ok"
    assert payload["action"] == "resolve"
    assert payload["thread_id"] == "$ctx-thread:localhost"
    mock_normalize.assert_awaited_once_with(
        context.client,
        context.room_id,
        "$ctx-thread:localhost",
    )
    mock_set.assert_awaited_once_with(
        context.client,
        context.room_id,
        "$ctx-thread:localhost",
        context.requester_id,
    )


@pytest.mark.asyncio
async def test_unresolve_thread_defaults_to_context_resolved_thread_id() -> None:
    """Unresolve should use the active resolved thread root when not overridden."""
    tool = ThreadResolutionTools()
    context = _make_context(thread_id="$ctx-thread:localhost")

    with (
        patch(
            "mindroom.custom_tools.thread_resolution.normalize_thread_root_event_id",
            new=AsyncMock(return_value="$ctx-thread:localhost"),
        ) as mock_normalize,
        patch(
            "mindroom.custom_tools.thread_resolution.clear_thread_resolution",
            new=AsyncMock(),
        ) as mock_clear,
        tool_runtime_context(context),
    ):
        payload = json.loads(await tool.unresolve_thread())

    assert payload["status"] == "ok"
    assert payload["action"] == "unresolve"
    assert payload["thread_id"] == "$ctx-thread:localhost"
    assert payload["resolved"] is False
    assert payload["updated_by"] == context.requester_id
    assert "updated_at" in payload
    assert datetime.fromisoformat(payload["updated_at"]).tzinfo is not None
    assert "resolved_by" not in payload
    assert "resolved_at" not in payload
    mock_normalize.assert_awaited_once_with(
        context.client,
        context.room_id,
        "$ctx-thread:localhost",
    )
    mock_clear.assert_awaited_once_with(
        context.client,
        context.room_id,
        "$ctx-thread:localhost",
        requester_user_id=context.requester_id,
    )


@pytest.mark.asyncio
async def test_thread_resolution_explicit_room_target_requires_authorization() -> None:
    """Explicit room targeting should enforce the same room access checks as matrix_message."""
    tool = ThreadResolutionTools()
    context = _make_context()

    with tool_runtime_context(context):
        payload = json.loads(await tool.resolve_thread(room_id="!other:localhost"))

    assert payload["status"] == "error"
    assert payload["room_id"] == "!other:localhost"
    assert "Not authorized" in payload["message"]


@pytest.mark.asyncio
async def test_unresolve_thread_explicit_room_target_requires_authorization() -> None:
    """Explicit room targeting should also enforce authorization for unresolve."""
    tool = ThreadResolutionTools()
    context = _make_context()

    with tool_runtime_context(context):
        payload = json.loads(await tool.unresolve_thread(room_id="!other:localhost"))

    assert payload["status"] == "error"
    assert payload["room_id"] == "!other:localhost"
    assert payload["action"] == "unresolve"
    assert "Not authorized" in payload["message"]


@pytest.mark.asyncio
async def test_thread_resolution_cross_room_does_not_inherit_context_thread() -> None:
    """Cross-room resolution should not silently reuse the origin room thread context."""
    tool = ThreadResolutionTools()
    context = _make_context(thread_id="$origin-thread:localhost")

    with (
        patch("mindroom.custom_tools.thread_resolution.room_access_allowed", return_value=True),
        tool_runtime_context(context),
    ):
        payload = json.loads(await tool.resolve_thread(room_id="!other:localhost"))

    assert payload["status"] == "error"
    assert payload["action"] == "resolve"
    assert "thread_id is required" in payload["message"]


@pytest.mark.asyncio
async def test_unresolve_thread_cross_room_does_not_inherit_context_thread() -> None:
    """Cross-room unresolve should not silently reuse the origin room thread context."""
    tool = ThreadResolutionTools()
    context = _make_context(thread_id="$origin-thread:localhost")

    with (
        patch("mindroom.custom_tools.thread_resolution.room_access_allowed", return_value=True),
        tool_runtime_context(context),
    ):
        payload = json.loads(await tool.unresolve_thread(room_id="!other:localhost"))

    assert payload["status"] == "error"
    assert payload["action"] == "unresolve"
    assert "thread_id is required" in payload["message"]


@pytest.mark.asyncio
async def test_thread_resolution_normalizes_explicit_thread_id_before_write() -> None:
    """Explicit event IDs should be normalized to the canonical thread root."""
    tool = ThreadResolutionTools()
    context = _make_context(thread_id=None)

    with (
        patch(
            "mindroom.custom_tools.thread_resolution.normalize_thread_root_event_id",
            new=AsyncMock(return_value="$thread-root:localhost"),
        ) as mock_normalize,
        patch(
            "mindroom.custom_tools.thread_resolution.set_thread_resolved",
            new=AsyncMock(return_value=_record("$thread-root:localhost")),
        ) as mock_set,
        tool_runtime_context(context),
    ):
        payload = json.loads(await tool.resolve_thread(thread_id="$reply-event:localhost"))

    assert payload["status"] == "ok"
    assert payload["thread_id"] == "$thread-root:localhost"
    mock_normalize.assert_awaited_once_with(
        context.client,
        context.room_id,
        "$reply-event:localhost",
    )
    mock_set.assert_awaited_once_with(
        context.client,
        context.room_id,
        "$thread-root:localhost",
        context.requester_id,
    )


@pytest.mark.asyncio
async def test_thread_resolution_returns_error_when_normalization_fails() -> None:
    """Normalization failures should surface as structured errors instead of guessing."""
    tool = ThreadResolutionTools()
    context = _make_context(thread_id="$reply:localhost")

    with (
        patch(
            "mindroom.custom_tools.thread_resolution.normalize_thread_root_event_id",
            new=AsyncMock(return_value=None),
        ),
        tool_runtime_context(context),
    ):
        payload = json.loads(await tool.resolve_thread())

    assert payload["status"] == "error"
    assert payload["action"] == "resolve"
    assert payload["thread_id"] == "$reply:localhost"
    assert "canonical thread root" in payload["message"]


@pytest.mark.asyncio
async def test_unresolve_thread_returns_error_when_normalization_fails() -> None:
    """Unresolve should surface normalization failures as structured errors."""
    tool = ThreadResolutionTools()
    context = _make_context(thread_id="$reply:localhost")

    with (
        patch(
            "mindroom.custom_tools.thread_resolution.normalize_thread_root_event_id",
            new=AsyncMock(return_value=None),
        ),
        tool_runtime_context(context),
    ):
        payload = json.loads(await tool.unresolve_thread())

    assert payload["status"] == "error"
    assert payload["action"] == "unresolve"
    assert payload["thread_id"] == "$reply:localhost"
    assert "canonical thread root" in payload["message"]


@pytest.mark.asyncio
async def test_thread_resolution_surfaces_write_failures() -> None:
    """State write failures should return structured tool errors."""
    tool = ThreadResolutionTools()
    context = _make_context()

    with (
        patch(
            "mindroom.custom_tools.thread_resolution.normalize_thread_root_event_id",
            new=AsyncMock(return_value="$thread:localhost"),
        ),
        patch(
            "mindroom.custom_tools.thread_resolution.set_thread_resolved",
            new=AsyncMock(side_effect=ThreadResolutionError("write failed")),
        ),
        tool_runtime_context(context),
    ):
        payload = json.loads(await tool.resolve_thread())

    assert payload["status"] == "error"
    assert payload["thread_id"] == "$thread:localhost"
    assert payload["message"] == "write failed"


@pytest.mark.asyncio
async def test_unresolve_thread_surfaces_clear_failures() -> None:
    """State clear failures should return structured tool errors."""
    tool = ThreadResolutionTools()
    context = _make_context()

    with (
        patch(
            "mindroom.custom_tools.thread_resolution.normalize_thread_root_event_id",
            new=AsyncMock(return_value="$thread:localhost"),
        ),
        patch(
            "mindroom.custom_tools.thread_resolution.clear_thread_resolution",
            new=AsyncMock(side_effect=ThreadResolutionError("clear failed")),
        ),
        tool_runtime_context(context),
    ):
        payload = json.loads(await tool.unresolve_thread())

    assert payload["status"] == "error"
    assert payload["action"] == "unresolve"
    assert payload["thread_id"] == "$thread:localhost"
    assert payload["message"] == "clear failed"


@pytest.mark.asyncio
async def test_resolve_thread_falls_back_to_reply_to_event_id_for_room_timeline_root() -> None:
    """Room-level messages with no thread context should use reply_to_event_id as the thread root."""
    tool = ThreadResolutionTools()
    context = _make_context(
        thread_id=None,
        reply_to_event_id="$root-event:localhost",
    )

    with (
        patch(
            "mindroom.custom_tools.thread_resolution.normalize_thread_root_event_id",
            new=AsyncMock(return_value="$root-event:localhost"),
        ) as mock_normalize,
        patch(
            "mindroom.custom_tools.thread_resolution.set_thread_resolved",
            new=AsyncMock(return_value=_record("$root-event:localhost")),
        ) as mock_set,
        tool_runtime_context(context),
    ):
        payload = json.loads(await tool.resolve_thread())

    assert payload["status"] == "ok"
    assert payload["thread_id"] == "$root-event:localhost"
    mock_normalize.assert_awaited_once_with(
        context.client,
        context.room_id,
        "$root-event:localhost",
    )
    mock_set.assert_awaited_once_with(
        context.client,
        context.room_id,
        "$root-event:localhost",
        context.requester_id,
    )


@pytest.mark.asyncio
async def test_unresolve_thread_canonical_skips_normalization() -> None:
    """Canonical mode should clear the marker without fetching the live event."""
    tool = ThreadResolutionTools()
    context = _make_context(thread_id=None, reply_to_event_id="$orphaned-root:localhost")

    with (
        patch(
            "mindroom.custom_tools.thread_resolution.normalize_thread_root_event_id",
            new=AsyncMock(return_value=None),
        ) as mock_normalize,
        patch(
            "mindroom.custom_tools.thread_resolution.clear_thread_resolution",
            new=AsyncMock(),
        ) as mock_clear,
        tool_runtime_context(context),
    ):
        payload = json.loads(
            await tool.unresolve_thread(thread_id="$orphaned-root:localhost", canonical=True),
        )

    assert payload["status"] == "ok"
    assert payload["action"] == "unresolve"
    assert payload["thread_id"] == "$orphaned-root:localhost"
    assert payload["resolved"] is False
    mock_normalize.assert_not_awaited()
    mock_clear.assert_awaited_once_with(
        context.client,
        context.room_id,
        "$orphaned-root:localhost",
        requester_user_id=context.requester_id,
    )

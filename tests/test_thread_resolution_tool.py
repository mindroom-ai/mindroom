"""Tests for explicit thread resolution tools."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, patch

import pytest

import mindroom.tools  # noqa: F401
from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.custom_tools.thread_resolution import ThreadResolutionTools
from mindroom.message_target import MessageTarget
from mindroom.thread_tags import RESOLVED_THREAD_TAG, ThreadTagsError
from mindroom.tool_system.metadata import TOOL_METADATA, get_tool_by_name
from mindroom.tool_system.runtime_context import ToolRuntimeContext, tool_runtime_context
from tests.conftest import bind_runtime_paths, make_event_cache_mock, runtime_paths_for, test_runtime_paths

if TYPE_CHECKING:
    from pathlib import Path

ROOM_ID = "!room:localhost"
THREAD_ID = "$thread:localhost"


def _context(tmp_path: Path, *, thread_id: str | None = THREAD_ID) -> ToolRuntimeContext:
    config = bind_runtime_paths(
        Config(agents={"general": AgentConfig(display_name="General Agent")}),
        test_runtime_paths(tmp_path),
    )
    return ToolRuntimeContext(
        agent_name="general",
        target=MessageTarget.resolve(room_id=ROOM_ID, thread_id=thread_id, reply_to_event_id=None),
        requester_id="@user:localhost",
        client=AsyncMock(),
        config=config,
        runtime_paths=runtime_paths_for(config),
        conversation_cache=AsyncMock(),
        event_cache=make_event_cache_mock(),
    )


def test_thread_resolution_tool_registered_and_instantiates(tmp_path: Path) -> None:
    """Explicit resolution should be registered as one room-context capability."""
    context = _context(tmp_path)
    metadata = TOOL_METADATA["thread_resolution"]

    assert metadata.requires_room_context
    assert metadata.function_names == ("reopen_thread", "resolve_thread")
    assert isinstance(
        get_tool_by_name("thread_resolution", context.runtime_paths, worker_target=None),
        ThreadResolutionTools,
    )


@pytest.mark.asyncio
async def test_thread_resolution_requires_runtime_context() -> None:
    """Tool calls should fail clearly outside Matrix runtime context."""
    payload = json.loads(await ThreadResolutionTools().resolve_thread())

    assert payload["status"] == "error"
    assert payload["tool"] == "thread_resolution"
    assert "context" in payload["message"]


@pytest.mark.asyncio
async def test_thread_resolution_requires_active_thread(tmp_path: Path) -> None:
    """Resolution should never target a room-level conversation."""
    context = _context(tmp_path, thread_id=None)

    with tool_runtime_context(context):
        payload = json.loads(await ThreadResolutionTools().resolve_thread())

    assert payload["status"] == "error"
    assert "active thread" in payload["message"]


@pytest.mark.asyncio
async def test_resolve_thread_sets_lifecycle_tag(tmp_path: Path) -> None:
    """Resolve should write fixed lifecycle state for current canonical thread."""
    context = _context(tmp_path)

    with (
        patch("mindroom.custom_tools.thread_resolution.set_thread_tag", new=AsyncMock()) as mock_set,
        tool_runtime_context(context),
    ):
        payload = json.loads(await ThreadResolutionTools().resolve_thread())

    assert payload == {
        "action": "resolve",
        "resolved": True,
        "room_id": ROOM_ID,
        "status": "ok",
        "thread_id": THREAD_ID,
        "tool": "thread_resolution",
    }
    mock_set.assert_awaited_once_with(
        context.client,
        ROOM_ID,
        THREAD_ID,
        RESOLVED_THREAD_TAG,
        set_by=context.requester_id,
    )


@pytest.mark.asyncio
async def test_reopen_thread_removes_lifecycle_tag(tmp_path: Path) -> None:
    """Reopen should remove fixed lifecycle state from current canonical thread."""
    context = _context(tmp_path)

    with (
        patch("mindroom.custom_tools.thread_resolution.remove_thread_tag", new=AsyncMock()) as mock_remove,
        tool_runtime_context(context),
    ):
        payload = json.loads(await ThreadResolutionTools().reopen_thread())

    assert payload["status"] == "ok"
    assert payload["action"] == "reopen"
    assert payload["resolved"] is False
    mock_remove.assert_awaited_once_with(
        context.client,
        ROOM_ID,
        THREAD_ID,
        RESOLVED_THREAD_TAG,
        requester_user_id=context.requester_id,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method_name", "dependency", "action"),
    [
        ("resolve_thread", "set_thread_tag", "resolve"),
        ("reopen_thread", "remove_thread_tag", "reopen"),
    ],
)
async def test_thread_resolution_surfaces_state_errors(
    tmp_path: Path,
    method_name: str,
    dependency: str,
    action: str,
) -> None:
    """Low-level permission and state errors should remain structured."""
    context = _context(tmp_path)
    tool = ThreadResolutionTools()
    method = tool.resolve_thread if method_name == "resolve_thread" else tool.reopen_thread

    with (
        patch(
            f"mindroom.custom_tools.thread_resolution.{dependency}",
            new=AsyncMock(side_effect=ThreadTagsError("state failed")),
        ),
        tool_runtime_context(context),
    ):
        payload = json.loads(await method())

    assert payload["status"] == "error"
    assert payload["action"] == action
    assert payload["thread_id"] == THREAD_ID
    assert payload["message"] == "state failed"

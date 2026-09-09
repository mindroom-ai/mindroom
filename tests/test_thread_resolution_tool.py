"""Tests for explicit thread resolution tools."""

from __future__ import annotations

import json
from dataclasses import replace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, patch

import pytest
from nio import RoomGetEventError

import mindroom.tools  # noqa: F401
from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.custom_tools.thread_resolution import ThreadResolutionTools
from mindroom.message_target import MessageTarget
from mindroom.thread_tags import RESOLVED_THREAD_TAG, ThreadTagsError
from mindroom.tool_system.metadata import TOOL_METADATA, get_tool_by_name
from mindroom.tool_system.runtime_context import ToolRuntimeContext, tool_runtime_context
from tests.authorization_helpers import (
    make_test_tool_runtime_context,
)
from tests.conftest import (
    bind_runtime_paths,
    make_conversation_reader_mock,
    make_relation_lookup,
    runtime_paths_for,
    test_runtime_paths,
)

if TYPE_CHECKING:
    from pathlib import Path

ROOM_ID = "!room:localhost"
THREAD_ID = "$thread:localhost"


def _context(tmp_path: Path, *, thread_id: str | None = THREAD_ID) -> ToolRuntimeContext:
    config = bind_runtime_paths(
        Config(agents={"general": AgentConfig(display_name="General Agent")}),
        test_runtime_paths(tmp_path),
    )
    return make_test_tool_runtime_context(
        agent_name="general",
        target=MessageTarget.resolve(room_id=ROOM_ID, thread_id=thread_id, reply_to_event_id=None),
        requester_id="@user:localhost",
        client=AsyncMock(),
        config=config,
        runtime_paths=runtime_paths_for(config),
        relations=make_relation_lookup(),
        conversation_reader=make_conversation_reader_mock(),
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
@pytest.mark.parametrize("reopen", [False, True])
@pytest.mark.parametrize("active_thread_id", [THREAD_ID, None])
@pytest.mark.parametrize("target_id", ["$other-root:localhost", "$other-reply:localhost"])
async def test_thread_resolution_targets_explicit_thread(
    tmp_path: Path,
    reopen: bool,
    active_thread_id: str | None,
    target_id: str,
) -> None:
    """An explicit root or reply must mutate its canonical thread, even from the room timeline."""
    context = _context(tmp_path, thread_id=active_thread_id)
    context.client.room_get_event = AsyncMock(return_value=RoomGetEventError("missing", status_code="M_NOT_FOUND"))
    context = replace(
        context,
        relations=make_relation_lookup(
            threads={
                "$other-root:localhost": "$other-root:localhost",
                "$other-reply:localhost": "$other-root:localhost",
            },
            client=context.client,
        ),
    )
    tool = ThreadResolutionTools()
    method = tool.reopen_thread if reopen else tool.resolve_thread
    dependency = "remove_thread_tag" if reopen else "set_thread_tag"
    requester_kwarg = "requester_user_id" if reopen else "set_by"

    with (
        patch(f"mindroom.custom_tools.thread_resolution.{dependency}", new=AsyncMock()) as mock_write,
        tool_runtime_context(context),
    ):
        payload = json.loads(await method(thread_id=target_id))

    assert payload["status"] == "ok"
    assert payload["thread_id"] == "$other-root:localhost"
    assert payload["room_id"] == ROOM_ID
    assert payload["resolved"] is not reopen
    mock_write.assert_awaited_once_with(
        context.client,
        ROOM_ID,
        "$other-root:localhost",
        RESOLVED_THREAD_TAG,
        **{requester_kwarg: context.requester_id},
    )
    context.client.room_get_event.assert_awaited_once_with(ROOM_ID, target_id)


@pytest.mark.asyncio
@pytest.mark.parametrize("reopen", [False, True])
@pytest.mark.parametrize("target_id", ["", "   ", "$missing-or-other-room:localhost"])
async def test_thread_resolution_rejects_unresolved_explicit_target(
    tmp_path: Path,
    reopen: bool,
    target_id: str,
) -> None:
    """An invalid explicit target must fail without changing the active thread."""
    context = _context(tmp_path)
    context.client.room_get_event = AsyncMock(return_value=RoomGetEventError("missing", status_code="M_NOT_FOUND"))
    context = replace(context, relations=make_relation_lookup(client=context.client))
    tool = ThreadResolutionTools()
    method = tool.reopen_thread if reopen else tool.resolve_thread

    with (
        patch("mindroom.custom_tools.thread_resolution.set_thread_tag", new=AsyncMock()) as mock_set,
        patch("mindroom.custom_tools.thread_resolution.remove_thread_tag", new=AsyncMock()) as mock_remove,
        tool_runtime_context(context),
    ):
        payload = json.loads(await method(thread_id=target_id))

    assert payload["status"] == "error"
    assert payload["thread_id"] == target_id
    assert "canonical thread root" in payload["message"]
    mock_set.assert_not_awaited()
    mock_remove.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("reopen", [False, True])
async def test_thread_resolution_returns_error_when_target_lookup_raises(tmp_path: Path, reopen: bool) -> None:
    """Lookup failures must return a structured error without changing lifecycle state."""
    context = _context(tmp_path)
    tool = ThreadResolutionTools()
    method = tool.reopen_thread if reopen else tool.resolve_thread

    with (
        patch(
            "mindroom.custom_tools.thread_resolution.resolve_thread_root_event_id_for_client",
            new=AsyncMock(side_effect=RuntimeError("lookup failed")),
        ),
        patch("mindroom.custom_tools.thread_resolution.set_thread_tag", new=AsyncMock()) as mock_set,
        patch("mindroom.custom_tools.thread_resolution.remove_thread_tag", new=AsyncMock()) as mock_remove,
        tool_runtime_context(context),
    ):
        payload = json.loads(await method(thread_id="$other-root:localhost"))

    assert payload["status"] == "error"
    assert payload["thread_id"] == "$other-root:localhost"
    assert "canonical thread root" in payload["message"]
    mock_set.assert_not_awaited()
    mock_remove.assert_not_awaited()


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

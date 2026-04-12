"""Tests for the thread tags tool."""

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
from mindroom.custom_tools.thread_tags import ThreadTagsTools
from mindroom.thread_tags import ThreadTagRecord, ThreadTagsError, ThreadTagsState
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
        conversation_access=AsyncMock(),
        room=None,
        reply_to_event_id=reply_to_event_id,
        storage_path=None,
    )


def _state(thread_root_id: str, **tags: ThreadTagRecord) -> ThreadTagsState:
    return ThreadTagsState(
        room_id="!room:localhost",
        thread_root_id=thread_root_id,
        tags=tags,
    )


def _record(
    *,
    note: str | None = None,
    data: dict[str, object] | None = None,
) -> ThreadTagRecord:
    return ThreadTagRecord(
        set_by="@user:localhost",
        set_at=datetime(2026, 3, 21, 19, 2, 3, tzinfo=UTC),
        note=note,
        data=data or {},
    )


def test_thread_tags_tool_registered_and_instantiates() -> None:
    """Thread tags should be available from the metadata registry."""
    runtime_root = Path(tempfile.mkdtemp())
    config = bind_runtime_paths(
        Config(agents={"general": AgentConfig(display_name="General Agent")}),
        test_runtime_paths(runtime_root),
    )

    assert "thread_tags" in TOOL_METADATA
    assert isinstance(
        get_tool_by_name("thread_tags", runtime_paths_for(config), worker_target=None),
        ThreadTagsTools,
    )


@pytest.mark.asyncio
async def test_thread_tags_tool_requires_runtime_context() -> None:
    """Tool calls should fail clearly outside Matrix runtime context."""
    payload = json.loads(await ThreadTagsTools().tag_thread("resolved"))

    assert payload["status"] == "error"
    assert payload["tool"] == "thread_tags"
    assert "context" in payload["message"]


@pytest.mark.asyncio
async def test_untag_thread_requires_runtime_context() -> None:
    """Untag should fail clearly outside Matrix runtime context."""
    payload = json.loads(await ThreadTagsTools().untag_thread("resolved"))

    assert payload["status"] == "error"
    assert payload["tool"] == "thread_tags"
    assert "context" in payload["message"]


@pytest.mark.asyncio
async def test_list_thread_tags_requires_runtime_context() -> None:
    """List should fail clearly outside Matrix runtime context."""
    payload = json.loads(await ThreadTagsTools().list_thread_tags())

    assert payload["status"] == "error"
    assert payload["tool"] == "thread_tags"
    assert "context" in payload["message"]


@pytest.mark.asyncio
async def test_tag_thread_defaults_to_context_thread_id() -> None:
    """Tag should use the active thread root when not overridden."""
    tool = ThreadTagsTools()
    context = _make_context(thread_id="$ctx-thread:localhost")

    with (
        patch(
            "mindroom.custom_tools.thread_tags.normalize_thread_root_event_id",
            new=AsyncMock(return_value="$ctx-thread:localhost"),
        ) as mock_normalize,
        patch(
            "mindroom.custom_tools.thread_tags.set_thread_tag",
            new=AsyncMock(return_value=_state("$ctx-thread:localhost", resolved=_record())),
        ) as mock_set,
        tool_runtime_context(context),
    ):
        payload = json.loads(await tool.tag_thread("resolved"))

    assert payload["status"] == "ok"
    assert payload["action"] == "tag"
    assert payload["thread_id"] == "$ctx-thread:localhost"
    mock_normalize.assert_awaited_once_with(
        context.client,
        context.room_id,
        "$ctx-thread:localhost",
        access=context.conversation_access,
    )
    mock_set.assert_awaited_once_with(
        context.client,
        context.room_id,
        "$ctx-thread:localhost",
        "resolved",
        set_by=context.requester_id,
        note=None,
        data=None,
    )


@pytest.mark.asyncio
async def test_tag_thread_explicit_thread_id_overrides_same_room_context() -> None:
    """An explicit thread target should win over the active same-room thread context."""
    tool = ThreadTagsTools()
    context = _make_context(thread_id="$ctx-thread:localhost")

    with (
        patch(
            "mindroom.custom_tools.thread_tags.normalize_thread_root_event_id",
            new=AsyncMock(return_value="$explicit-thread:localhost"),
        ) as mock_normalize,
        patch(
            "mindroom.custom_tools.thread_tags.set_thread_tag",
            new=AsyncMock(return_value=_state("$explicit-thread:localhost", resolved=_record())),
        ) as mock_set,
        tool_runtime_context(context),
    ):
        payload = json.loads(await tool.tag_thread("resolved", thread_id="$explicit-event:localhost"))

    assert payload["status"] == "ok"
    assert payload["thread_id"] == "$explicit-thread:localhost"
    mock_normalize.assert_awaited_once_with(
        context.client,
        context.room_id,
        "$explicit-event:localhost",
        access=context.conversation_access,
    )
    mock_set.assert_awaited_once_with(
        context.client,
        context.room_id,
        "$explicit-thread:localhost",
        "resolved",
        set_by=context.requester_id,
        note=None,
        data=None,
    )


@pytest.mark.asyncio
async def test_tag_thread_explicit_same_room_id_keeps_context_thread_fallback() -> None:
    """Repeating the current room ID should still target the active thread for writes."""
    tool = ThreadTagsTools()
    context = _make_context(thread_id="$ctx-thread:localhost")

    with (
        patch(
            "mindroom.custom_tools.thread_tags.normalize_thread_root_event_id",
            new=AsyncMock(return_value="$ctx-thread:localhost"),
        ) as mock_normalize,
        patch(
            "mindroom.custom_tools.thread_tags.set_thread_tag",
            new=AsyncMock(return_value=_state("$ctx-thread:localhost", resolved=_record())),
        ) as mock_set,
        tool_runtime_context(context),
    ):
        payload = json.loads(await tool.tag_thread("resolved", room_id=context.room_id))

    assert payload["status"] == "ok"
    assert payload["thread_id"] == "$ctx-thread:localhost"
    mock_normalize.assert_awaited_once_with(
        context.client,
        context.room_id,
        "$ctx-thread:localhost",
        access=context.conversation_access,
    )
    mock_set.assert_awaited_once_with(
        context.client,
        context.room_id,
        "$ctx-thread:localhost",
        "resolved",
        set_by=context.requester_id,
        note=None,
        data=None,
    )


@pytest.mark.asyncio
async def test_untag_thread_defaults_to_context_thread_id() -> None:
    """Untag should use the active thread root when not overridden."""
    tool = ThreadTagsTools()
    context = _make_context(thread_id="$ctx-thread:localhost")

    with (
        patch(
            "mindroom.custom_tools.thread_tags.normalize_thread_root_event_id",
            new=AsyncMock(return_value="$ctx-thread:localhost"),
        ) as mock_normalize,
        patch(
            "mindroom.custom_tools.thread_tags.remove_thread_tag",
            new=AsyncMock(return_value=_state("$ctx-thread:localhost")),
        ) as mock_remove,
        tool_runtime_context(context),
    ):
        payload = json.loads(await tool.untag_thread("resolved"))

    assert payload["status"] == "ok"
    assert payload["action"] == "untag"
    assert payload["thread_id"] == "$ctx-thread:localhost"
    assert payload["tags"] == {}
    mock_normalize.assert_awaited_once_with(
        context.client,
        context.room_id,
        "$ctx-thread:localhost",
        access=context.conversation_access,
    )
    mock_remove.assert_awaited_once_with(
        context.client,
        context.room_id,
        "$ctx-thread:localhost",
        "resolved",
        requester_user_id=context.requester_id,
    )


@pytest.mark.asyncio
async def test_untag_thread_explicit_thread_id_overrides_same_room_context() -> None:
    """An explicit untag target should not be silently replaced by context state."""
    tool = ThreadTagsTools()
    context = _make_context(thread_id="$ctx-thread:localhost")

    with (
        patch(
            "mindroom.custom_tools.thread_tags.normalize_thread_root_event_id",
            new=AsyncMock(return_value="$explicit-thread:localhost"),
        ) as mock_normalize,
        patch(
            "mindroom.custom_tools.thread_tags.remove_thread_tag",
            new=AsyncMock(return_value=_state("$explicit-thread:localhost")),
        ) as mock_remove,
        tool_runtime_context(context),
    ):
        payload = json.loads(await tool.untag_thread("resolved", thread_id="$explicit-event:localhost"))

    assert payload["status"] == "ok"
    assert payload["thread_id"] == "$explicit-thread:localhost"
    mock_normalize.assert_awaited_once_with(
        context.client,
        context.room_id,
        "$explicit-event:localhost",
        access=context.conversation_access,
    )
    mock_remove.assert_awaited_once_with(
        context.client,
        context.room_id,
        "$explicit-thread:localhost",
        "resolved",
        requester_user_id=context.requester_id,
    )


@pytest.mark.asyncio
async def test_untag_thread_explicit_same_room_id_keeps_context_thread_fallback() -> None:
    """Repeating the current room ID should still target the active thread for untag."""
    tool = ThreadTagsTools()
    context = _make_context(thread_id="$ctx-thread:localhost")

    with (
        patch(
            "mindroom.custom_tools.thread_tags.normalize_thread_root_event_id",
            new=AsyncMock(return_value="$ctx-thread:localhost"),
        ) as mock_normalize,
        patch(
            "mindroom.custom_tools.thread_tags.remove_thread_tag",
            new=AsyncMock(return_value=_state("$ctx-thread:localhost")),
        ) as mock_remove,
        tool_runtime_context(context),
    ):
        payload = json.loads(await tool.untag_thread("resolved", room_id=context.room_id))

    assert payload["status"] == "ok"
    assert payload["thread_id"] == "$ctx-thread:localhost"
    mock_normalize.assert_awaited_once_with(
        context.client,
        context.room_id,
        "$ctx-thread:localhost",
        access=context.conversation_access,
    )
    mock_remove.assert_awaited_once_with(
        context.client,
        context.room_id,
        "$ctx-thread:localhost",
        "resolved",
        requester_user_id=context.requester_id,
    )


@pytest.mark.asyncio
async def test_list_thread_tags_defaults_to_context_thread_id() -> None:
    """List should use the active thread root when not overridden."""
    tool = ThreadTagsTools()
    context = _make_context(thread_id="$ctx-thread:localhost")

    with (
        patch(
            "mindroom.custom_tools.thread_tags.normalize_thread_root_event_id",
            new=AsyncMock(return_value="$ctx-thread:localhost"),
        ) as mock_normalize,
        patch(
            "mindroom.custom_tools.thread_tags.get_thread_tags",
            new=AsyncMock(return_value=_state("$ctx-thread:localhost", resolved=_record(note="done"))),
        ) as mock_get,
        tool_runtime_context(context),
    ):
        payload = json.loads(await tool.list_thread_tags())

    assert payload["status"] == "ok"
    assert payload["action"] == "list"
    assert payload["thread_id"] == "$ctx-thread:localhost"
    assert payload["tags"]["resolved"]["note"] == "done"
    mock_normalize.assert_awaited_once_with(
        context.client,
        context.room_id,
        "$ctx-thread:localhost",
        access=context.conversation_access,
    )
    mock_get.assert_awaited_once_with(
        context.client,
        context.room_id,
        "$ctx-thread:localhost",
    )


@pytest.mark.asyncio
async def test_list_thread_tags_explicit_thread_id_overrides_same_room_context() -> None:
    """Room-wide context should not override an explicit list target in the same room."""
    tool = ThreadTagsTools()
    context = _make_context(thread_id="$ctx-thread:localhost")

    with (
        patch(
            "mindroom.custom_tools.thread_tags.normalize_thread_root_event_id",
            new=AsyncMock(return_value="$explicit-thread:localhost"),
        ) as mock_normalize,
        patch(
            "mindroom.custom_tools.thread_tags.get_thread_tags",
            new=AsyncMock(return_value=_state("$explicit-thread:localhost", resolved=_record(note="done"))),
        ) as mock_get,
        tool_runtime_context(context),
    ):
        payload = json.loads(await tool.list_thread_tags(thread_id="$explicit-event:localhost"))

    assert payload["status"] == "ok"
    assert payload["thread_id"] == "$explicit-thread:localhost"
    assert payload["tags"]["resolved"]["note"] == "done"
    mock_normalize.assert_awaited_once_with(
        context.client,
        context.room_id,
        "$explicit-event:localhost",
        access=context.conversation_access,
    )
    mock_get.assert_awaited_once_with(
        context.client,
        context.room_id,
        "$explicit-thread:localhost",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("method_name", ["tag_thread", "untag_thread", "list_thread_tags"])
async def test_thread_tags_explicit_room_target_requires_authorization(method_name: str) -> None:
    """Explicit room targeting should enforce the same room access checks as matrix_message."""
    tool = ThreadTagsTools()
    context = _make_context()

    with tool_runtime_context(context):
        if method_name == "tag_thread":
            payload = json.loads(await tool.tag_thread("resolved", room_id="!other:localhost"))
        elif method_name == "untag_thread":
            payload = json.loads(await tool.untag_thread("resolved", room_id="!other:localhost"))
        else:
            payload = json.loads(await tool.list_thread_tags(room_id="!other:localhost"))

    assert payload["status"] == "error"
    assert payload["room_id"] == "!other:localhost"
    assert "Not authorized" in payload["message"]


@pytest.mark.asyncio
async def test_thread_tags_cross_room_does_not_inherit_context_thread() -> None:
    """Cross-room tagging should not silently reuse the origin room thread context."""
    tool = ThreadTagsTools()
    context = _make_context(thread_id="$origin-thread:localhost")

    with (
        patch("mindroom.custom_tools.thread_tags.room_access_allowed", return_value=True),
        tool_runtime_context(context),
    ):
        payload = json.loads(await tool.tag_thread("resolved", room_id="!other:localhost"))

    assert payload["status"] == "error"
    assert payload["action"] == "tag"
    assert "thread_id is required" in payload["message"]


@pytest.mark.asyncio
async def test_tag_thread_normalizes_explicit_thread_id_before_write() -> None:
    """Explicit event IDs should be normalized to the canonical thread root."""
    tool = ThreadTagsTools()
    context = _make_context(thread_id=None)

    with (
        patch(
            "mindroom.custom_tools.thread_tags.normalize_thread_root_event_id",
            new=AsyncMock(return_value="$thread-root:localhost"),
        ) as mock_normalize,
        patch(
            "mindroom.custom_tools.thread_tags.set_thread_tag",
            new=AsyncMock(
                return_value=_state(
                    "$thread-root:localhost",
                    blocked=_record(data={"blocked_by": ["$other:localhost"]}),
                ),
            ),
        ) as mock_set,
        tool_runtime_context(context),
    ):
        payload = json.loads(
            await tool.tag_thread(
                "blocked",
                thread_id="$reply-event:localhost",
                data={"blocked_by": ["$other:localhost"]},
            ),
        )

    assert payload["status"] == "ok"
    assert payload["thread_id"] == "$thread-root:localhost"
    mock_normalize.assert_awaited_once_with(
        context.client,
        context.room_id,
        "$reply-event:localhost",
        access=context.conversation_access,
    )
    mock_set.assert_awaited_once_with(
        context.client,
        context.room_id,
        "$thread-root:localhost",
        "blocked",
        set_by=context.requester_id,
        note=None,
        data={"blocked_by": ["$other:localhost"]},
    )


@pytest.mark.asyncio
async def test_tag_thread_returns_error_when_normalization_fails() -> None:
    """Normalization failures should surface as structured errors instead of guessing."""
    tool = ThreadTagsTools()
    context = _make_context(thread_id="$reply:localhost")

    with (
        patch(
            "mindroom.custom_tools.thread_tags.normalize_thread_root_event_id",
            new=AsyncMock(return_value=None),
        ),
        tool_runtime_context(context),
    ):
        payload = json.loads(await tool.tag_thread("resolved"))

    assert payload["status"] == "error"
    assert payload["action"] == "tag"
    assert payload["thread_id"] == "$reply:localhost"
    assert "canonical thread root" in payload["message"]


@pytest.mark.asyncio
async def test_tag_thread_surfaces_write_failures() -> None:
    """State write failures should return structured tool errors."""
    tool = ThreadTagsTools()
    context = _make_context()

    with (
        patch(
            "mindroom.custom_tools.thread_tags.normalize_thread_root_event_id",
            new=AsyncMock(return_value="$thread:localhost"),
        ),
        patch(
            "mindroom.custom_tools.thread_tags.set_thread_tag",
            new=AsyncMock(side_effect=ThreadTagsError("write failed")),
        ),
        tool_runtime_context(context),
    ):
        payload = json.loads(await tool.tag_thread("resolved"))

    assert payload["status"] == "error"
    assert payload["thread_id"] == "$thread:localhost"
    assert payload["message"] == "write failed"


@pytest.mark.asyncio
async def test_list_thread_tags_falls_back_to_reply_to_event_id_for_room_timeline_root() -> None:
    """Room-level messages with no thread context should use reply_to_event_id as the fallback root."""
    tool = ThreadTagsTools()
    context = _make_context(
        thread_id=None,
        reply_to_event_id="$root-event:localhost",
    )

    with (
        patch(
            "mindroom.custom_tools.thread_tags.normalize_thread_root_event_id",
            new=AsyncMock(return_value="$root-event:localhost"),
        ) as mock_normalize,
        patch(
            "mindroom.custom_tools.thread_tags.get_thread_tags",
            new=AsyncMock(return_value=_state("$root-event:localhost", resolved=_record())),
        ) as mock_get,
        tool_runtime_context(context),
    ):
        payload = json.loads(await tool.list_thread_tags())

    assert payload["status"] == "ok"
    assert payload["thread_id"] == "$root-event:localhost"
    mock_normalize.assert_awaited_once_with(
        context.client,
        context.room_id,
        "$root-event:localhost",
        access=context.conversation_access,
    )
    mock_get.assert_awaited_once_with(
        context.client,
        context.room_id,
        "$root-event:localhost",
    )


@pytest.mark.asyncio
async def test_list_thread_tags_lists_room_wide_when_no_thread_is_available() -> None:
    """No thread target should switch the tool into room-wide listing mode."""
    tool = ThreadTagsTools()
    context = _make_context(thread_id=None, reply_to_event_id=None)

    with (
        patch(
            "mindroom.custom_tools.thread_tags.list_tagged_threads",
            new=AsyncMock(
                return_value={
                    "$thread-two:localhost": _state(
                        "$thread-two:localhost",
                        blocked=_record(data={"blocked_by": ["$other:localhost"]}),
                        resolved=_record(note="done"),
                    ),
                },
            ),
        ) as mock_list,
        tool_runtime_context(context),
    ):
        payload = json.loads(await tool.list_thread_tags(tag="blocked"))

    assert payload["status"] == "ok"
    assert payload["room_wide"] is True
    assert payload["tag"] == "blocked"
    assert list(payload["threads"]) == ["$thread-two:localhost"]
    assert list(payload["threads"]["$thread-two:localhost"]) == ["blocked"]
    assert payload["threads"]["$thread-two:localhost"]["blocked"]["data"] == {"blocked_by": ["$other:localhost"]}
    assert payload["threads"]["$thread-two:localhost"]["blocked"]["set_by"] == "@user:localhost"
    assert datetime.fromisoformat(payload["threads"]["$thread-two:localhost"]["blocked"]["set_at"]).tzinfo is not None
    mock_list.assert_awaited_once_with(
        context.client,
        context.room_id,
        tag="blocked",
    )


@pytest.mark.asyncio
async def test_list_thread_tags_room_wide_filters_by_include_tag_only() -> None:
    """Room-wide listing should keep only threads that carry the included tag."""
    tool = ThreadTagsTools()
    context = _make_context(thread_id=None, reply_to_event_id=None)

    with (
        patch(
            "mindroom.custom_tools.thread_tags.list_tagged_threads",
            new=AsyncMock(
                return_value={
                    "$thread-one:localhost": _state(
                        "$thread-one:localhost",
                        blocked=_record(data={"blocked_by": ["$other:localhost"]}),
                    ),
                    "$thread-two:localhost": _state(
                        "$thread-two:localhost",
                        resolved=_record(note="done"),
                    ),
                    "$thread-three:localhost": _state(
                        "$thread-three:localhost",
                        blocked=_record(data={"blocked_by": ["$other:localhost"]}),
                        resolved=_record(note="done"),
                    ),
                },
            ),
        ) as mock_list,
        tool_runtime_context(context),
    ):
        payload = json.loads(await tool.list_thread_tags(include_tag="blocked"))

    assert payload["status"] == "ok"
    assert payload["room_wide"] is True
    assert payload["include_tag"] == "blocked"
    assert payload["exclude_tag"] is None
    assert list(payload["threads"]) == ["$thread-one:localhost", "$thread-three:localhost"]
    assert set(payload["threads"]["$thread-one:localhost"]) == {"blocked"}
    assert set(payload["threads"]["$thread-three:localhost"]) == {"blocked", "resolved"}
    mock_list.assert_awaited_once_with(
        context.client,
        context.room_id,
        tag=None,
    )


@pytest.mark.asyncio
async def test_list_thread_tags_room_wide_requires_tag_and_include_tag_together() -> None:
    """Room-wide listing should require both tag filters when tag and include_tag are combined."""
    tool = ThreadTagsTools()
    context = _make_context(thread_id=None, reply_to_event_id=None)

    with (
        patch(
            "mindroom.custom_tools.thread_tags.list_tagged_threads",
            new=AsyncMock(
                return_value={
                    "$thread-one:localhost": _state(
                        "$thread-one:localhost",
                        blocked=_record(data={"blocked_by": ["$other:localhost"]}),
                    ),
                    "$thread-two:localhost": _state(
                        "$thread-two:localhost",
                        blocked=_record(data={"blocked_by": ["$other:localhost"]}),
                        waiting=_record(data={"waiting_on": ["@owner:localhost"]}),
                    ),
                    "$thread-three:localhost": _state(
                        "$thread-three:localhost",
                        waiting=_record(data={"waiting_on": ["@owner:localhost"]}),
                    ),
                },
            ),
        ) as mock_list,
        tool_runtime_context(context),
    ):
        payload = json.loads(await tool.list_thread_tags(tag="blocked", include_tag="waiting"))

    assert payload["status"] == "ok"
    assert payload["room_wide"] is True
    assert payload["tag"] == "blocked"
    assert payload["include_tag"] == "waiting"
    assert list(payload["threads"]) == ["$thread-two:localhost"]
    assert list(payload["threads"]["$thread-two:localhost"]) == ["blocked"]
    mock_list.assert_awaited_once_with(
        context.client,
        context.room_id,
        tag=None,
    )


@pytest.mark.asyncio
async def test_list_thread_tags_room_wide_requires_tag_without_excluded_tag() -> None:
    """Room-wide listing should keep threads with the tag unless they also carry the excluded tag."""
    tool = ThreadTagsTools()
    context = _make_context(thread_id=None, reply_to_event_id=None)

    with (
        patch(
            "mindroom.custom_tools.thread_tags.list_tagged_threads",
            new=AsyncMock(
                return_value={
                    "$thread-one:localhost": _state(
                        "$thread-one:localhost",
                        blocked=_record(data={"blocked_by": ["$other:localhost"]}),
                    ),
                    "$thread-two:localhost": _state(
                        "$thread-two:localhost",
                        blocked=_record(data={"blocked_by": ["$other:localhost"]}),
                        resolved=_record(note="done"),
                    ),
                    "$thread-three:localhost": _state(
                        "$thread-three:localhost",
                        waiting=_record(data={"waiting_on": ["@owner:localhost"]}),
                    ),
                },
            ),
        ) as mock_list,
        tool_runtime_context(context),
    ):
        payload = json.loads(await tool.list_thread_tags(tag="blocked", exclude_tag="resolved"))

    assert payload["status"] == "ok"
    assert payload["room_wide"] is True
    assert payload["tag"] == "blocked"
    assert payload["exclude_tag"] == "resolved"
    assert list(payload["threads"]) == ["$thread-one:localhost"]
    assert list(payload["threads"]["$thread-one:localhost"]) == ["blocked"]
    mock_list.assert_awaited_once_with(
        context.client,
        context.room_id,
        tag=None,
    )


@pytest.mark.asyncio
async def test_list_thread_tags_room_wide_returns_empty_when_include_tag_matches_nothing() -> None:
    """Room-wide listing should return an empty result when no thread has the included tag."""
    tool = ThreadTagsTools()
    context = _make_context(thread_id=None, reply_to_event_id=None)

    with (
        patch(
            "mindroom.custom_tools.thread_tags.list_tagged_threads",
            new=AsyncMock(
                return_value={
                    "$thread-one:localhost": _state(
                        "$thread-one:localhost",
                        blocked=_record(data={"blocked_by": ["$other:localhost"]}),
                    ),
                    "$thread-two:localhost": _state(
                        "$thread-two:localhost",
                        resolved=_record(note="done"),
                    ),
                },
            ),
        ) as mock_list,
        tool_runtime_context(context),
    ):
        payload = json.loads(await tool.list_thread_tags(include_tag="nonexistent"))

    assert payload["status"] == "ok"
    assert payload["room_wide"] is True
    assert payload["include_tag"] == "nonexistent"
    assert payload["threads"] == {}
    mock_list.assert_awaited_once_with(
        context.client,
        context.room_id,
        tag=None,
    )


@pytest.mark.asyncio
async def test_list_thread_tags_room_wide_returns_empty_when_exclude_tag_filters_all_threads() -> None:
    """Room-wide listing should return an empty result when every thread has the excluded tag."""
    tool = ThreadTagsTools()
    context = _make_context(thread_id=None, reply_to_event_id=None)

    with (
        patch(
            "mindroom.custom_tools.thread_tags.list_tagged_threads",
            new=AsyncMock(
                return_value={
                    "$thread-one:localhost": _state(
                        "$thread-one:localhost",
                        blocked=_record(data={"blocked_by": ["$other:localhost"]}),
                        resolved=_record(note="done"),
                    ),
                    "$thread-two:localhost": _state(
                        "$thread-two:localhost",
                        resolved=_record(note="done"),
                    ),
                },
            ),
        ) as mock_list,
        tool_runtime_context(context),
    ):
        payload = json.loads(await tool.list_thread_tags(exclude_tag="resolved"))

    assert payload["status"] == "ok"
    assert payload["room_wide"] is True
    assert payload["exclude_tag"] == "resolved"
    assert payload["threads"] == {}
    mock_list.assert_awaited_once_with(
        context.client,
        context.room_id,
        tag=None,
    )


@pytest.mark.asyncio
async def test_list_thread_tags_room_wide_normalizes_mixed_case_include_and_exclude_tags() -> None:
    """Room-wide listing should normalize include and exclude tag inputs before filtering."""
    tool = ThreadTagsTools()
    context = _make_context(thread_id=None, reply_to_event_id=None)

    with (
        patch(
            "mindroom.custom_tools.thread_tags.list_tagged_threads",
            new=AsyncMock(
                return_value={
                    "$thread-one:localhost": _state(
                        "$thread-one:localhost",
                        blocked=_record(data={"blocked_by": ["$other:localhost"]}),
                    ),
                    "$thread-two:localhost": _state(
                        "$thread-two:localhost",
                        blocked=_record(data={"blocked_by": ["$other:localhost"]}),
                        resolved=_record(note="done"),
                    ),
                },
            ),
        ) as mock_list,
        tool_runtime_context(context),
    ):
        payload = json.loads(await tool.list_thread_tags(include_tag="BloCked", exclude_tag="ReSoLved"))

    assert payload["status"] == "ok"
    assert payload["room_wide"] is True
    assert payload["include_tag"] == "blocked"
    assert payload["exclude_tag"] == "resolved"
    assert list(payload["threads"]) == ["$thread-one:localhost"]
    mock_list.assert_awaited_once_with(
        context.client,
        context.room_id,
        tag=None,
    )


@pytest.mark.asyncio
async def test_list_thread_tags_explicit_same_room_target_can_list_room_wide_from_thread_context() -> None:
    """An explicit same-room target should disable thread fallback and allow room-wide listing."""
    tool = ThreadTagsTools()
    context = _make_context(thread_id="$ctx-thread:localhost")

    with (
        patch(
            "mindroom.custom_tools.thread_tags.normalize_thread_root_event_id",
            new=AsyncMock(),
        ) as mock_normalize,
        patch(
            "mindroom.custom_tools.thread_tags.get_thread_tags",
            new=AsyncMock(),
        ) as mock_get,
        patch(
            "mindroom.custom_tools.thread_tags.list_tagged_threads",
            new=AsyncMock(
                return_value={
                    "$thread-two:localhost": _state(
                        "$thread-two:localhost",
                        blocked=_record(data={"blocked_by": ["$other:localhost"]}),
                        resolved=_record(note="done"),
                    ),
                },
            ),
        ) as mock_list,
        tool_runtime_context(context),
    ):
        payload = json.loads(await tool.list_thread_tags(room_id=context.room_id, tag="blocked"))

    assert payload["status"] == "ok"
    assert payload["room_wide"] is True
    assert list(payload["threads"]) == ["$thread-two:localhost"]
    assert list(payload["threads"]["$thread-two:localhost"]) == ["blocked"]
    mock_list.assert_awaited_once_with(
        context.client,
        context.room_id,
        tag="blocked",
    )
    mock_normalize.assert_not_awaited()
    mock_get.assert_not_awaited()


@pytest.mark.asyncio
async def test_list_thread_tags_room_wide_returns_error_on_room_state_failure() -> None:
    """Room-wide listing should surface helper read failures as tool errors."""
    tool = ThreadTagsTools()
    context = _make_context(thread_id=None, reply_to_event_id=None)

    with (
        patch(
            "mindroom.custom_tools.thread_tags.list_tagged_threads",
            new=AsyncMock(side_effect=ThreadTagsError("room state forbidden")),
        ),
        tool_runtime_context(context),
    ):
        payload = json.loads(await tool.list_thread_tags())

    assert payload["status"] == "error"
    assert payload["action"] == "list"
    assert payload["room_id"] == context.room_id
    assert payload["message"] == "room state forbidden"


@pytest.mark.asyncio
async def test_list_thread_tags_filters_thread_specific_payload() -> None:
    """Thread-specific listing should support a tag filter without a second read."""
    tool = ThreadTagsTools()
    context = _make_context(thread_id="$ctx-thread:localhost")

    with (
        patch(
            "mindroom.custom_tools.thread_tags.normalize_thread_root_event_id",
            new=AsyncMock(return_value="$ctx-thread:localhost"),
        ),
        patch(
            "mindroom.custom_tools.thread_tags.get_thread_tags",
            new=AsyncMock(
                return_value=_state(
                    "$ctx-thread:localhost",
                    resolved=_record(),
                    blocked=_record(data={"blocked_by": ["$other:localhost"]}),
                ),
            ),
        ),
        tool_runtime_context(context),
    ):
        payload = json.loads(await tool.list_thread_tags(tag="blocked"))

    assert payload["status"] == "ok"
    assert payload["tags"]["blocked"]["data"] == {"blocked_by": ["$other:localhost"]}
    assert payload["tags"]["blocked"]["set_by"] == "@user:localhost"
    assert datetime.fromisoformat(payload["tags"]["blocked"]["set_at"]).tzinfo is not None


@pytest.mark.asyncio
async def test_list_thread_tags_thread_specific_include_exclude_filters() -> None:
    """Thread-specific listing should respect include_tag and exclude_tag filters."""
    tool = ThreadTagsTools()
    context = _make_context(thread_id="$ctx-thread:localhost")

    with (
        patch(
            "mindroom.custom_tools.thread_tags.normalize_thread_root_event_id",
            new=AsyncMock(return_value="$ctx-thread:localhost"),
        ),
        patch(
            "mindroom.custom_tools.thread_tags.get_thread_tags",
            new=AsyncMock(
                return_value=_state(
                    "$ctx-thread:localhost",
                    resolved=_record(),
                    blocked=_record(data={"blocked_by": ["$other:localhost"]}),
                ),
            ),
        ),
        tool_runtime_context(context),
    ):
        # include_tag matches → thread returned with all tags
        payload = json.loads(await tool.list_thread_tags(include_tag="blocked"))
        assert payload["status"] == "ok"
        assert "blocked" in payload["tags"]
        assert "resolved" in payload["tags"]

        # exclude_tag matches → thread excluded (empty tags)
        payload = json.loads(await tool.list_thread_tags(exclude_tag="resolved"))
        assert payload["status"] == "ok"
        assert payload["tags"] == {}

        # include_tag matches but exclude_tag also matches → excluded
        payload = json.loads(
            await tool.list_thread_tags(include_tag="blocked", exclude_tag="resolved"),
        )
        assert payload["status"] == "ok"
        assert payload["tags"] == {}

        # include_tag doesn't match → excluded
        payload = json.loads(await tool.list_thread_tags(include_tag="nonexistent"))
        assert payload["status"] == "ok"
        assert payload["tags"] == {}


@pytest.mark.asyncio
async def test_untag_thread_canonical_skips_normalization() -> None:
    """Canonical mode should clear the marker without fetching the live event."""
    tool = ThreadTagsTools()
    context = _make_context(thread_id=None, reply_to_event_id="$orphaned-root:localhost")

    with (
        patch(
            "mindroom.custom_tools.thread_tags.normalize_thread_root_event_id",
            new=AsyncMock(return_value=None),
        ) as mock_normalize,
        patch(
            "mindroom.custom_tools.thread_tags.remove_thread_tag",
            new=AsyncMock(return_value=_state("$orphaned-root:localhost")),
        ) as mock_remove,
        tool_runtime_context(context),
    ):
        payload = json.loads(
            await tool.untag_thread("resolved", thread_id="$orphaned-root:localhost", canonical=True),
        )

    assert payload["status"] == "ok"
    assert payload["action"] == "untag"
    assert payload["thread_id"] == "$orphaned-root:localhost"
    mock_normalize.assert_not_awaited()
    mock_remove.assert_awaited_once_with(
        context.client,
        context.room_id,
        "$orphaned-root:localhost",
        "resolved",
        requester_user_id=context.requester_id,
    )

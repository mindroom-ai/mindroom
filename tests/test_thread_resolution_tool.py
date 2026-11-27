"""Tests for explicit thread resolution tools."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Literal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from nio import RoomGetEventError, RoomGetEventResponse

import mindroom.tools  # noqa: F401
from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.custom_tools.thread_move import ThreadMoveTools
from mindroom.custom_tools.thread_resolution import ThreadResolutionTools
from mindroom.custom_tools.thread_tags import ThreadTagsTools
from mindroom.message_target import MessageTarget
from mindroom.thread_tags import (
    RESOLVED_THREAD_TAG,
    SNOOZED_THREAD_TAG,
    ThreadTagRecord,
    ThreadTagsError,
    ThreadTagsState,
)
from mindroom.tool_system.metadata import get_tool_by_name
from mindroom.tool_system.registry import builtin_tool_registry
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
    metadata = builtin_tool_registry().entries["thread_resolution"]

    assert metadata.requires_room_context
    assert metadata.function_names == ("thread_resolution",)
    assert metadata.action_scoped_grants == ("thread_resolution",)
    assert isinstance(
        get_tool_by_name("thread_resolution", context.runtime_paths, worker_target=None, tools=builtin_tool_registry()),
        ThreadResolutionTools,
    )


@pytest.mark.asyncio
async def test_thread_resolution_requires_runtime_context() -> None:
    """Tool calls should fail clearly outside Matrix runtime context."""
    payload = json.loads(await ThreadResolutionTools().thread_resolution("resolve"))

    assert payload["status"] == "error"
    assert payload["tool"] == "thread_resolution"
    assert "context" in payload["message"]


@pytest.mark.asyncio
async def test_thread_resolution_requires_active_thread(tmp_path: Path) -> None:
    """Resolution should never target a room-level conversation."""
    context = _context(tmp_path, thread_id=None)

    with tool_runtime_context(context):
        payload = json.loads(await ThreadResolutionTools().thread_resolution("resolve"))

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
        payload = json.loads(await ThreadResolutionTools().thread_resolution("resolve"))

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
        payload = json.loads(await ThreadResolutionTools().thread_resolution("reopen"))

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
    action: Literal["reopen", "resolve"] = "reopen" if reopen else "resolve"
    dependency = "remove_thread_tag" if reopen else "set_thread_tag"
    requester_kwarg = "requester_user_id" if reopen else "set_by"

    with (
        patch(f"mindroom.custom_tools.thread_resolution.{dependency}", new=AsyncMock()) as mock_write,
        tool_runtime_context(context),
    ):
        payload = json.loads(await tool.thread_resolution(action, thread_id=target_id))

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
    action: Literal["reopen", "resolve"] = "reopen" if reopen else "resolve"

    with (
        patch("mindroom.custom_tools.thread_resolution.set_thread_tag", new=AsyncMock()) as mock_set,
        patch("mindroom.custom_tools.thread_resolution.remove_thread_tag", new=AsyncMock()) as mock_remove,
        tool_runtime_context(context),
    ):
        payload = json.loads(await tool.thread_resolution(action, thread_id=target_id))

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
    action: Literal["reopen", "resolve"] = "reopen" if reopen else "resolve"

    with (
        patch(
            "mindroom.custom_tools.attachment_helpers.resolve_thread_root_event_id",
            new=AsyncMock(side_effect=RuntimeError("lookup failed")),
        ),
        patch("mindroom.custom_tools.thread_resolution.set_thread_tag", new=AsyncMock()) as mock_set,
        patch("mindroom.custom_tools.thread_resolution.remove_thread_tag", new=AsyncMock()) as mock_remove,
        tool_runtime_context(context),
    ):
        payload = json.loads(await tool.thread_resolution(action, thread_id="$other-root:localhost"))

    assert payload["status"] == "error"
    assert payload["thread_id"] == "$other-root:localhost"
    assert "canonical thread root" in payload["message"]
    mock_set.assert_not_awaited()
    mock_remove.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("dependency", "action"),
    [
        ("set_thread_tag", "resolve"),
        ("remove_thread_tag", "reopen"),
    ],
)
async def test_thread_resolution_surfaces_state_errors(
    tmp_path: Path,
    dependency: str,
    action: Literal["resolve", "reopen"],
) -> None:
    """Low-level permission and state errors should remain structured."""
    context = _context(tmp_path)
    tool = ThreadResolutionTools()

    with (
        patch(
            f"mindroom.custom_tools.thread_resolution.{dependency}",
            new=AsyncMock(side_effect=ThreadTagsError("state failed")),
        ),
        tool_runtime_context(context),
    ):
        payload = json.loads(await tool.thread_resolution(action))

    assert payload["status"] == "error"
    assert payload["action"] == action
    assert payload["thread_id"] == THREAD_ID
    assert payload["message"] == "state failed"


def _state_with(*tags: str) -> MagicMock:
    state = MagicMock()
    state.tags = dict.fromkeys(tags, MagicMock())
    return state


@pytest.mark.asyncio
@pytest.mark.parametrize("remove_fails", [False, True])
async def test_resolve_thread_removes_an_existing_snooze(tmp_path: Path, remove_fails: bool) -> None:
    """Resolving a snoozed thread ends its snooze, and a failed cleanup does not fail the resolve."""
    orchestrator = MagicMock()
    context = replace(_context(tmp_path), orchestrator=orchestrator)
    remove = AsyncMock(side_effect=ThreadTagsError("nope") if remove_fails else None)

    with (
        patch(
            "mindroom.custom_tools.thread_resolution.set_thread_tag",
            new=AsyncMock(return_value=_state_with(RESOLVED_THREAD_TAG, SNOOZED_THREAD_TAG)),
        ),
        patch("mindroom.custom_tools.thread_resolution.remove_thread_tag", new=remove),
        tool_runtime_context(context),
    ):
        payload = json.loads(await ThreadResolutionTools().thread_resolution("resolve"))

    assert payload["status"] == "ok"
    remove.assert_awaited_once_with(
        context.client,
        ROOM_ID,
        THREAD_ID,
        SNOOZED_THREAD_TAG,
        requester_user_id=context.requester_id,
    )
    if remove_fails:
        orchestrator.thread_snooze.unsnoozed.assert_not_called()
    else:
        orchestrator.thread_snooze.unsnoozed.assert_called_once_with(ROOM_ID, THREAD_ID)


@pytest.mark.asyncio
async def test_resolve_thread_keeps_a_snooze_set_again_meanwhile(tmp_path: Path) -> None:
    """A snooze written again while the resolve removes the old one stays indexed, so it still wakes."""
    orchestrator = MagicMock()
    context = replace(_context(tmp_path), orchestrator=orchestrator)

    with (
        patch(
            "mindroom.custom_tools.thread_resolution.set_thread_tag",
            new=AsyncMock(return_value=_state_with(RESOLVED_THREAD_TAG, SNOOZED_THREAD_TAG)),
        ),
        patch(
            "mindroom.custom_tools.thread_resolution.remove_thread_tag",
            new=AsyncMock(return_value=_state_with(RESOLVED_THREAD_TAG, SNOOZED_THREAD_TAG)),
        ),
        tool_runtime_context(context),
    ):
        payload = json.loads(await ThreadResolutionTools().thread_resolution("resolve"))

    assert payload["status"] == "ok"
    orchestrator.thread_snooze.unsnoozed.assert_not_called()


@pytest.mark.asyncio
async def test_reopen_thread_reports_a_resolve_written_again_meanwhile(tmp_path: Path) -> None:
    """Reopen reports the state its removal left, which a concurrent resolve can keep resolved."""
    context = _context(tmp_path)

    with (
        patch(
            "mindroom.custom_tools.thread_resolution.remove_thread_tag",
            new=AsyncMock(return_value=_state_with(RESOLVED_THREAD_TAG)),
        ),
        tool_runtime_context(context),
    ):
        payload = json.loads(await ThreadResolutionTools().thread_resolution("reopen"))

    assert payload["status"] == "ok"
    assert payload["resolved"] is True


@pytest.mark.asyncio
async def test_resolve_thread_leaves_other_state_alone(tmp_path: Path) -> None:
    """Without a snooze, resolving removes nothing."""
    context = _context(tmp_path)

    with (
        patch(
            "mindroom.custom_tools.thread_resolution.set_thread_tag",
            new=AsyncMock(return_value=_state_with(RESOLVED_THREAD_TAG)),
        ),
        patch("mindroom.custom_tools.thread_resolution.remove_thread_tag", new=AsyncMock()) as mock_remove,
        tool_runtime_context(context),
    ):
        await ThreadResolutionTools().thread_resolution("resolve")

    mock_remove.assert_not_awaited()


@pytest.mark.asyncio
async def test_standalone_room_message_resolves_for_resolution_and_tags_not_thread_move(tmp_path: Path) -> None:
    """A relation-free room message with no replies resolves for resolution/tags but stays strict for thread_move."""
    standalone_id = "$standalone:localhost"
    state_event_id = "$topic:localhost"
    encrypted_id = "$undecrypted:localhost"
    events = {
        standalone_id: {
            "event_id": standalone_id,
            "type": "m.room.message",
            "content": {"msgtype": "m.text", "body": "hi"},
        },
        state_event_id: {"event_id": state_event_id, "type": "m.room.topic", "content": {"topic": "ops"}},
        encrypted_id: {"event_id": encrypted_id, "type": "m.room.encrypted", "content": {"ciphertext": "opaque"}},
    }

    async def get_event(_room_id: str, event_id: str) -> RoomGetEventResponse | RoomGetEventError:
        if event_id not in events:
            return RoomGetEventError("missing", status_code="M_NOT_FOUND")
        resp = RoomGetEventResponse()
        resp.event = MagicMock(source=events[event_id])
        return resp

    context = _context(tmp_path, thread_id=None)
    context.client.room_get_event = AsyncMock(side_effect=get_event)
    context = replace(context, relations=make_relation_lookup(client=context.client))

    with (
        patch("mindroom.custom_tools.thread_resolution.set_thread_tag", new=AsyncMock()) as mock_resolve_set,
        patch(
            "mindroom.custom_tools.thread_tags.set_thread_tag",
            new=AsyncMock(
                return_value=ThreadTagsState(
                    room_id=ROOM_ID,
                    thread_root_id=standalone_id,
                    tags={"ops": ThreadTagRecord(set_by="@user:localhost", set_at=datetime.now(UTC))},
                ),
            ),
        ) as mock_tag_set,
        tool_runtime_context(context),
    ):
        resolved = json.loads(await ThreadResolutionTools().thread_resolution("resolve", thread_id=standalone_id))
        tagged = json.loads(await ThreadTagsTools().thread_tags("tag", tag="ops", thread_id=standalone_id))
        moved = json.loads(await ThreadMoveTools().move_thread(room_id="!dest:localhost", thread_id=standalone_id))
        rejected_state = json.loads(
            await ThreadResolutionTools().thread_resolution("resolve", thread_id=state_event_id),
        )
        rejected_encrypted = json.loads(
            await ThreadResolutionTools().thread_resolution("resolve", thread_id=encrypted_id),
        )

    assert resolved["status"] == "ok"
    assert resolved["thread_id"] == standalone_id
    mock_resolve_set.assert_awaited_once()
    assert tagged["status"] == "ok"
    assert tagged["thread_id"] == standalone_id
    mock_tag_set.assert_awaited_once()
    assert moved["status"] == "error"
    assert "canonical thread root" in moved["message"]
    assert rejected_state["status"] == "error"
    assert "canonical thread root" in rejected_state["message"]
    assert rejected_encrypted["status"] == "error"
    assert "canonical thread root" in rejected_encrypted["message"]


@pytest.mark.asyncio
async def test_thread_resolution_rejects_unsupported_action(tmp_path: Path) -> None:
    """Unsupported action returns a structured error payload."""
    context = _context(tmp_path)

    with tool_runtime_context(context):
        payload = json.loads(await ThreadResolutionTools().thread_resolution("bogus"))  # type: ignore[arg-type]

    assert payload["status"] == "error"
    assert payload["tool"] == "thread_resolution"
    assert "Unsupported thread_resolution action" in payload["message"]

"""Tests for the native thread snooze tool."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import mindroom.tools  # noqa: F401
from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.custom_tools.thread_snooze import ThreadSnoozeTools
from mindroom.message_target import MessageTarget
from mindroom.thread_tags import SNOOZED_THREAD_TAG, ThreadTagRecord, ThreadTagsError, ThreadTagsState
from mindroom.tool_system.metadata import get_tool_by_name
from mindroom.tool_system.registry import builtin_tool_registry
from mindroom.tool_system.runtime_context import ToolRuntimeContext, tool_runtime_context
from tests.authorization_helpers import make_test_tool_runtime_context
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
_MODULE = "mindroom.custom_tools.thread_snooze"


def _context(tmp_path: Path, *, orchestrator: object | None = None) -> ToolRuntimeContext:
    config = bind_runtime_paths(
        Config(agents={"general": AgentConfig(display_name="General Agent")}),
        test_runtime_paths(tmp_path),
    )
    context = make_test_tool_runtime_context(
        agent_name="general",
        target=MessageTarget.resolve(room_id=ROOM_ID, thread_id=THREAD_ID, reply_to_event_id=None),
        requester_id="@user:localhost",
        client=AsyncMock(),
        config=config,
        runtime_paths=runtime_paths_for(config),
        relations=make_relation_lookup(),
        conversation_reader=make_conversation_reader_mock(),
    )
    return replace(context, orchestrator=orchestrator)  # type: ignore[arg-type]


def _future() -> datetime:
    return (datetime.now(UTC) + timedelta(days=1)).replace(microsecond=0)


def test_thread_snooze_tool_registered_and_instantiates(tmp_path: Path) -> None:
    """The toolkit is registered as one room-context capability."""
    context = _context(tmp_path)
    metadata = builtin_tool_registry().entries["thread_snooze"]

    assert metadata.requires_room_context
    assert metadata.function_names == ("snooze_thread", "unsnooze_thread")
    assert isinstance(
        get_tool_by_name("thread_snooze", context.runtime_paths, worker_target=None, tools=builtin_tool_registry()),
        ThreadSnoozeTools,
    )


@pytest.mark.asyncio
async def test_snooze_thread_writes_the_tag_and_tells_the_runtime(tmp_path: Path) -> None:
    """A snooze is a tag write followed by a runtime notification."""
    orchestrator = MagicMock()
    context = _context(tmp_path, orchestrator=orchestrator)

    with (
        patch(f"{_MODULE}.set_thread_tag", new=AsyncMock()) as mock_set,
        tool_runtime_context(context),
    ):
        payload = json.loads(await ThreadSnoozeTools().snooze_thread("2099-11-14T10:00:00+02:00", note="check CI"))

    assert payload == {
        "action": "snooze",
        "room_id": ROOM_ID,
        "snoozed": True,
        "status": "ok",
        "thread_id": THREAD_ID,
        "tool": "thread_snooze",
        "until": "2099-11-14T08:00:00+00:00",
    }
    mock_set.assert_awaited_once_with(
        context.client,
        ROOM_ID,
        THREAD_ID,
        SNOOZED_THREAD_TAG,
        set_by="@user:localhost",
        note="check CI",
        data={"until": "2099-11-14T08:00:00+00:00"},
    )
    orchestrator.thread_snooze.snoozed.assert_called_once_with(
        ROOM_ID,
        THREAD_ID,
        until=datetime(2099, 11, 14, 8, tzinfo=UTC),
        note="check CI",
        set_by="@user:localhost",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("until", "message"),
    [
        ("2000-01-01T00:00:00+00:00", "until must be in the future."),
        ("not a time", "until must be an ISO-8601 datetime."),
        ("2099-11-14", "until must be an ISO-8601 datetime."),
        ("", "until must be an ISO-8601 datetime."),
        (12345, "until must be an ISO-8601 datetime."),
        ("9999-12-31T23:59:59-01:00", "until must be an ISO-8601 datetime."),
    ],
)
async def test_snooze_thread_refuses_a_past_time_and_a_non_timestamp(
    tmp_path: Path,
    until: object,
    message: str,
) -> None:
    """Bad times change nothing."""
    orchestrator = MagicMock()
    context = _context(tmp_path, orchestrator=orchestrator)

    with (
        patch(f"{_MODULE}.set_thread_tag", new=AsyncMock()) as mock_set,
        tool_runtime_context(context),
    ):
        payload = json.loads(await ThreadSnoozeTools().snooze_thread(until))  # type: ignore[arg-type]

    assert payload["status"] == "error"
    assert payload["message"] == message
    mock_set.assert_not_awaited()
    orchestrator.thread_snooze.snoozed.assert_not_called()


@pytest.mark.asyncio
async def test_snooze_thread_treats_a_naive_time_as_utc(tmp_path: Path) -> None:
    """A datetime without an offset is UTC."""
    context = _context(tmp_path)
    naive = _future().replace(tzinfo=None).isoformat()

    with (
        patch(f"{_MODULE}.set_thread_tag", new=AsyncMock()) as mock_set,
        tool_runtime_context(context),
    ):
        payload = json.loads(await ThreadSnoozeTools().snooze_thread(naive))

    assert payload["status"] == "ok"
    assert payload["until"] == naive + "+00:00"
    mock_set.assert_awaited_once()


@pytest.mark.asyncio
async def test_snooze_thread_reports_a_tag_error(tmp_path: Path) -> None:
    """State write failures stay structured and the runtime hears nothing."""
    orchestrator = MagicMock()
    context = _context(tmp_path, orchestrator=orchestrator)

    with (
        patch(f"{_MODULE}.set_thread_tag", new=AsyncMock(side_effect=ThreadTagsError("state failed"))),
        tool_runtime_context(context),
    ):
        payload = json.loads(await ThreadSnoozeTools().snooze_thread(_future().isoformat()))

    assert payload["status"] == "error"
    assert payload["action"] == "snooze"
    assert payload["message"] == "state failed"
    orchestrator.thread_snooze.snoozed.assert_not_called()


@pytest.mark.asyncio
async def test_snooze_thread_works_without_an_orchestrator(tmp_path: Path) -> None:
    """The tag is the durable record, so no runtime is needed to write it."""
    context = _context(tmp_path)

    with (
        patch(f"{_MODULE}.set_thread_tag", new=AsyncMock()) as mock_set,
        tool_runtime_context(context),
    ):
        payload = json.loads(await ThreadSnoozeTools().snooze_thread(_future().isoformat()))

    assert payload["status"] == "ok"
    mock_set.assert_awaited_once()


@pytest.mark.asyncio
async def test_unsnooze_thread_removes_the_tag_and_tells_the_runtime(tmp_path: Path) -> None:
    """Unsnoozing removes the tag, then drops the runtime entry."""
    orchestrator = MagicMock()
    context = _context(tmp_path, orchestrator=orchestrator)

    with (
        patch(f"{_MODULE}.get_thread_tag_content", new=AsyncMock(return_value={"until": "x"})),
        patch(f"{_MODULE}.remove_thread_tag", new=AsyncMock()) as mock_remove,
        tool_runtime_context(context),
    ):
        payload = json.loads(await ThreadSnoozeTools().unsnooze_thread())

    assert payload == {
        "action": "unsnooze",
        "room_id": ROOM_ID,
        "snoozed": False,
        "status": "ok",
        "thread_id": THREAD_ID,
        "tool": "thread_snooze",
    }
    mock_remove.assert_awaited_once_with(
        context.client,
        ROOM_ID,
        THREAD_ID,
        SNOOZED_THREAD_TAG,
        requester_user_id="@user:localhost",
    )
    orchestrator.thread_snooze.unsnoozed.assert_called_once_with(ROOM_ID, THREAD_ID)


@pytest.mark.asyncio
async def test_unsnooze_thread_keeps_a_snooze_set_again_meanwhile(tmp_path: Path) -> None:
    """A snooze written again during the unsnooze survives it, so the runtime keeps it indexed."""
    orchestrator = MagicMock()
    context = _context(tmp_path, orchestrator=orchestrator)
    resnoozed = ThreadTagsState(
        room_id=ROOM_ID,
        thread_root_id=THREAD_ID,
        tags={
            SNOOZED_THREAD_TAG: ThreadTagRecord(
                set_by="@other:localhost",
                set_at=datetime(2026, 11, 13, tzinfo=UTC),
                data={"until": "2099-11-15T08:00:00+00:00"},
            ),
        },
    )

    with (
        patch(f"{_MODULE}.get_thread_tag_content", new=AsyncMock(return_value={"until": "x"})),
        patch(f"{_MODULE}.remove_thread_tag", new=AsyncMock(return_value=resnoozed)),
        tool_runtime_context(context),
    ):
        payload = json.loads(await ThreadSnoozeTools().unsnooze_thread())

    assert payload["status"] == "ok"
    assert payload["snoozed"] is True
    orchestrator.thread_snooze.unsnoozed.assert_not_called()


@pytest.mark.asyncio
async def test_unsnooze_thread_on_a_thread_that_is_not_snoozed(tmp_path: Path) -> None:
    """An absent snooze is an error and changes nothing."""
    context = _context(tmp_path, orchestrator=MagicMock())

    with (
        patch(f"{_MODULE}.get_thread_tag_content", new=AsyncMock(return_value=None)),
        patch(f"{_MODULE}.remove_thread_tag", new=AsyncMock()) as mock_remove,
        tool_runtime_context(context),
    ):
        payload = json.loads(await ThreadSnoozeTools().unsnooze_thread())

    assert payload["status"] == "error"
    assert payload["message"] == "Thread is not snoozed."
    mock_remove.assert_not_awaited()

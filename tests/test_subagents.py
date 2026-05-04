"""Tests for the standalone subagents toolkit and session registry helpers."""

from __future__ import annotations

import json
from dataclasses import replace
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, Mock

import nio
import pytest

import mindroom.tools  # noqa: F401
from mindroom.constants import ORIGINAL_SENDER_KEY, resolve_runtime_paths
from mindroom.custom_tools import subagents as subagents_module
from mindroom.custom_tools.subagents import SubAgentsTools
from mindroom.thread_summary import THREAD_SUMMARY_MAX_LENGTH
from mindroom.thread_utils import create_session_id
from mindroom.tool_system.metadata import TOOL_METADATA, get_tool_by_name
from mindroom.tool_system.runtime_context import ToolRuntimeContext, tool_runtime_context
from tests.conftest import delivered_matrix_side_effect, make_event_cache_mock

if TYPE_CHECKING:
    from pathlib import Path


EXPECTED_SUBAGENT_TOOL_NAMES = {
    "agents_list",
    "sessions_send",
    "sessions_spawn",
    "list_sessions",
}
TEST_SUMMARY = "test summary"
TEST_TAG = "test-tag"


def _make_config(*, thread_mode: str = "thread") -> MagicMock:
    config = MagicMock()
    config.agents = {
        "openclaw": SimpleNamespace(tools=["shell"]),
        "code": SimpleNamespace(tools=["shell"]),
        "research": SimpleNamespace(tools=["shell"]),
    }
    config.get_domain = MagicMock(return_value="localhost")
    config.get_entity_thread_mode = MagicMock(return_value=thread_mode)
    return config


def _make_context(
    tmp_path: Path,
    *,
    config: MagicMock | None = None,
    room_id: str = "!room:localhost",
    thread_id: str | None = "$ctx-thread:localhost",
    requester_id: str = "@alice:localhost",
) -> ToolRuntimeContext:
    async def _latest_thread_event_id(
        _room_id: str,
        thread_id: str | None,
        *_args: object,
        **_kwargs: object,
    ) -> str | None:
        return thread_id

    runtime_paths = resolve_runtime_paths(config_path=tmp_path / "config.yaml", storage_path=tmp_path)
    conversation_cache = AsyncMock()
    conversation_cache.get_latest_thread_event_id_if_needed.side_effect = _latest_thread_event_id
    conversation_cache.notify_outbound_message = Mock()
    conversation_cache.notify_outbound_redaction = Mock()
    return ToolRuntimeContext(
        agent_name="openclaw",
        room_id=room_id,
        thread_id=thread_id,
        resolved_thread_id=thread_id,
        requester_id=requester_id,
        client=MagicMock(),
        config=config or _make_config(),
        runtime_paths=runtime_paths,
        event_cache=make_event_cache_mock(),
        conversation_cache=conversation_cache,
        room=None,
        reply_to_event_id=None,
        storage_path=tmp_path,
    )


def _stub_spawn_followups(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[AsyncMock, AsyncMock, MagicMock]:
    summary_mock = AsyncMock(return_value="$summary:localhost")
    tag_mock = AsyncMock(return_value=SimpleNamespace())
    update_mock = MagicMock()
    monkeypatch.setattr(subagents_module, "send_thread_summary_event", summary_mock)
    monkeypatch.setattr(subagents_module, "set_thread_tag", tag_mock)
    monkeypatch.setattr(subagents_module, "update_last_summary_count", update_mock)
    return summary_mock, tag_mock, update_mock


def test_subagents_tool_registered_and_instantiates() -> None:
    """Subagents should be present in metadata and constructible from the registry."""
    assert "subagents" in TOOL_METADATA
    assert isinstance(get_tool_by_name("subagents", resolve_runtime_paths(), worker_target=None), SubAgentsTools)


def test_subagents_tool_name_contract() -> None:
    """Toolkit should expose the expected stable async method names."""
    tool = SubAgentsTools()
    exposed_names = {func.name for func in tool.functions.values()} | {
        func.name for func in tool.async_functions.values()
    }
    assert exposed_names == EXPECTED_SUBAGENT_TOOL_NAMES


@pytest.mark.asyncio
async def test_agents_list_requires_runtime_context() -> None:
    """agents_list should return a structured context-unavailable error outside runtime scope."""
    payload = json.loads(await SubAgentsTools().agents_list())
    assert payload["status"] == "error"
    assert payload["tool"] == "agents_list"
    assert "context" in payload["message"]


@pytest.mark.asyncio
async def test_agents_list_returns_sorted_agents(tmp_path: Path) -> None:
    """agents_list should return deterministic sorted agent ids from config."""
    config = _make_config()
    ctx = _make_context(tmp_path, config=config)

    with tool_runtime_context(ctx):
        payload = json.loads(await SubAgentsTools().agents_list())

    assert payload["status"] == "ok"
    assert payload["tool"] == "agents_list"
    assert payload["agents"] == ["code", "openclaw", "research"]
    assert payload["current_agent"] == "openclaw"


@pytest.mark.asyncio
async def test_sessions_send_requires_runtime_context() -> None:
    """sessions_send should return a structured context-unavailable error outside runtime scope."""
    payload = json.loads(await SubAgentsTools().sessions_send(message="hello"))
    assert payload["status"] == "error"
    assert payload["tool"] == "sessions_send"
    assert "context" in payload["message"]


@pytest.mark.asyncio
async def test_sessions_send_rejects_empty_message(tmp_path: Path) -> None:
    """sessions_send should validate non-empty message content."""
    ctx = _make_context(tmp_path)

    with tool_runtime_context(ctx):
        payload = json.loads(await SubAgentsTools().sessions_send(message="   "))

    assert payload["status"] == "error"
    assert payload["tool"] == "sessions_send"
    assert "cannot be empty" in payload["message"]


@pytest.mark.asyncio
async def test_sessions_send_returns_error_when_matrix_send_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """sessions_send should return an error payload when Matrix dispatch fails."""
    send_mock = AsyncMock(return_value=None)
    monkeypatch.setattr(subagents_module, "_send_matrix_text", send_mock)
    ctx = _make_context(tmp_path)

    with tool_runtime_context(ctx):
        payload = json.loads(await SubAgentsTools().sessions_send(message="hello"))

    assert payload["status"] == "error"
    assert payload["tool"] == "sessions_send"
    assert "Failed to send message" in payload["message"]


@pytest.mark.asyncio
async def test_sessions_send_relays_original_sender(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """sessions_send should preserve original requester identity for relayed events."""
    send_mock = AsyncMock(return_value="$evt")
    monkeypatch.setattr(subagents_module, "_send_matrix_text", send_mock)
    ctx = _make_context(tmp_path, requester_id="@user:localhost")

    with tool_runtime_context(ctx):
        payload = json.loads(await SubAgentsTools().sessions_send(message="hello"))

    assert payload["status"] == "ok"
    send_mock.assert_awaited_once_with(
        ctx,
        room_id=ctx.room_id,
        text="hello",
        thread_id=ctx.thread_id,
        original_sender=ctx.requester_id,
    )


@pytest.mark.asyncio
async def test_sessions_send_defaults_to_resolved_thread_session(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """sessions_send should keep first-turn follow-ups in the canonical reply thread."""
    send_mock = AsyncMock(return_value="$evt")
    monkeypatch.setattr(subagents_module, "_send_matrix_text", send_mock)
    ctx = replace(
        _make_context(tmp_path, thread_id=None),
        resolved_thread_id="$resolved-thread:localhost",
    )

    with tool_runtime_context(ctx):
        payload = json.loads(await SubAgentsTools().sessions_send(message="hello"))

    assert payload["status"] == "ok"
    assert payload["session_key"] == create_session_id(ctx.room_id, "$resolved-thread:localhost")
    send_mock.assert_awaited_once_with(
        ctx,
        room_id=ctx.room_id,
        text="hello",
        thread_id="$resolved-thread:localhost",
        original_sender=ctx.requester_id,
    )


@pytest.mark.asyncio
async def test_send_matrix_text_uses_latest_thread_event_id_for_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Threaded subagent sends should include the latest thread event for fallback replies."""
    send_mock = AsyncMock(side_effect=delivered_matrix_side_effect("$evt"))
    monkeypatch.setattr(subagents_module, "send_message_result", send_mock)
    event_cache = MagicMock()
    ctx = replace(_make_context(tmp_path, requester_id="@user:localhost"), event_cache=event_cache)
    ctx.conversation_cache.get_latest_thread_event_id_if_needed = AsyncMock(return_value="$latest:localhost")

    await subagents_module._send_matrix_text(
        ctx,
        room_id=ctx.room_id,
        text="hello",
        thread_id=ctx.thread_id,
        original_sender=ctx.requester_id,
    )

    ctx.conversation_cache.get_latest_thread_event_id_if_needed.assert_awaited_once_with(
        ctx.room_id,
        ctx.thread_id,
        caller_label="subagent_tool_send",
    )
    content = send_mock.await_args.args[2]
    assert content["m.relates_to"]["event_id"] == ctx.thread_id
    assert content["m.relates_to"]["m.in_reply_to"]["event_id"] == "$latest:localhost"
    assert content[ORIGINAL_SENDER_KEY] == ctx.requester_id


@pytest.mark.asyncio
async def test_sessions_send_rejects_room_mode_threaded_dispatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """sessions_send should reject threaded dispatch into room-mode target agents."""
    send_mock = AsyncMock(return_value="$evt")
    monkeypatch.setattr(subagents_module, "_send_matrix_text", send_mock)
    ctx = _make_context(tmp_path, config=_make_config(thread_mode="room"))
    target_session = create_session_id(ctx.room_id, "$worker-thread:localhost")

    with tool_runtime_context(ctx):
        payload = json.loads(
            await SubAgentsTools().sessions_send(
                message="hello",
                session_key=target_session,
                agent_id="openclaw",
            ),
        )

    assert payload["status"] == "error"
    assert payload["tool"] == "sessions_send"
    assert "thread_mode=room" in payload["message"]
    send_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_sessions_send_checks_target_room_thread_mode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """sessions_send should evaluate thread mode using the target room context."""
    send_mock = AsyncMock(return_value="$evt")
    monkeypatch.setattr(subagents_module, "_send_matrix_text", send_mock)
    config = _make_config()
    config.get_entity_thread_mode.side_effect = lambda _agent_name, _runtime_paths, room_id=None: (
        "room" if room_id == "!target:localhost" else "thread"
    )
    ctx = _make_context(tmp_path, config=config)
    target_session = create_session_id("!target:localhost", "$worker-thread:localhost")

    with tool_runtime_context(ctx):
        payload = json.loads(
            await SubAgentsTools().sessions_send(
                message="hello",
                session_key=target_session,
                agent_id="openclaw",
            ),
        )

    assert payload["status"] == "error"
    assert payload["tool"] == "sessions_send"
    assert "thread_mode=room" in payload["message"]
    send_mock.assert_not_awaited()
    config.get_entity_thread_mode.assert_called_with(
        "openclaw",
        ctx.runtime_paths,
        room_id="!target:localhost",
    )


@pytest.mark.asyncio
async def test_sessions_send_label_resolves_to_tracked_session(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """sessions_send should resolve labels to tracked session keys in scope."""
    send_mock = AsyncMock(return_value="$evt")
    monkeypatch.setattr(subagents_module, "_send_matrix_text", send_mock)
    ctx = _make_context(tmp_path)

    session_key = create_session_id(ctx.room_id, "$target:localhost")
    subagents_module._record_session(ctx, session_key=session_key, label="work", target_agent="openclaw")

    with tool_runtime_context(ctx):
        payload = json.loads(await SubAgentsTools().sessions_send(message="hello", label="work"))

    assert payload["status"] == "ok"
    assert payload["session_key"] == session_key
    send_mock.assert_awaited_once_with(
        ctx,
        room_id=ctx.room_id,
        text="hello",
        thread_id="$target:localhost",
        original_sender=ctx.requester_id,
    )


@pytest.mark.asyncio
async def test_sessions_spawn_requires_runtime_context() -> None:
    """sessions_spawn should return a structured context-unavailable error outside runtime scope."""
    payload = json.loads(await SubAgentsTools().sessions_spawn(task="do this", summary=TEST_SUMMARY, tag=TEST_TAG))
    assert payload["status"] == "error"
    assert payload["tool"] == "sessions_spawn"
    assert "context" in payload["message"]


@pytest.mark.asyncio
async def test_sessions_spawn_rejects_empty_task(tmp_path: Path) -> None:
    """sessions_spawn should validate non-empty task content."""
    ctx = _make_context(tmp_path)

    with tool_runtime_context(ctx):
        payload = json.loads(await SubAgentsTools().sessions_spawn(task="  ", summary=TEST_SUMMARY, tag=TEST_TAG))

    assert payload["status"] == "error"
    assert payload["tool"] == "sessions_spawn"
    assert "Task cannot be empty" in payload["message"]


@pytest.mark.asyncio
async def test_sessions_spawn_returns_error_when_matrix_send_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """sessions_spawn should return an error payload when Matrix dispatch fails."""
    send_mock = AsyncMock(return_value=None)
    monkeypatch.setattr(subagents_module, "_send_matrix_text", send_mock)
    ctx = _make_context(tmp_path)

    with tool_runtime_context(ctx):
        payload = json.loads(
            await SubAgentsTools().sessions_spawn(task="do thing", summary=TEST_SUMMARY, tag=TEST_TAG),
        )

    assert payload["status"] == "error"
    assert payload["tool"] == "sessions_spawn"
    assert "Failed to send spawn message" in payload["message"]


@pytest.mark.asyncio
async def test_sessions_spawn_relays_original_sender(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """sessions_spawn should preserve original requester identity for relayed events."""
    send_mock = AsyncMock(return_value="$event")
    monkeypatch.setattr(subagents_module, "_send_matrix_text", send_mock)
    _stub_spawn_followups(monkeypatch)
    ctx = _make_context(tmp_path, requester_id="@user:localhost")

    with tool_runtime_context(ctx):
        payload = json.loads(
            await SubAgentsTools().sessions_spawn(task="do thing", summary=TEST_SUMMARY, tag=TEST_TAG),
        )

    assert payload["status"] == "ok"
    assert payload["target_agent"] == "openclaw"
    assert payload["event_id"] == "$event"
    assert payload["summary"] == TEST_SUMMARY
    assert payload["tag"] == TEST_TAG
    send_mock.assert_awaited_once_with(
        ctx,
        room_id=ctx.room_id,
        text="@mindroom_openclaw do thing",
        thread_id=None,
        original_sender=ctx.requester_id,
    )


@pytest.mark.asyncio
async def test_sessions_spawn_rejects_room_mode_target_agent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """sessions_spawn should reject isolated sessions for room-mode target agents."""
    send_mock = AsyncMock(return_value="$event")
    monkeypatch.setattr(subagents_module, "_send_matrix_text", send_mock)
    ctx = _make_context(tmp_path, config=_make_config(thread_mode="room"))

    with tool_runtime_context(ctx):
        payload = json.loads(
            await SubAgentsTools().sessions_spawn(task="do thing", summary=TEST_SUMMARY, tag=TEST_TAG),
        )

    assert payload["status"] == "error"
    assert payload["tool"] == "sessions_spawn"
    assert "thread_mode=room" in payload["message"]
    send_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_sessions_spawn_rejects_blank_summary(tmp_path: Path) -> None:
    """sessions_spawn should reject blank summaries before spawning."""
    ctx = _make_context(tmp_path)

    with tool_runtime_context(ctx):
        payload = json.loads(await SubAgentsTools().sessions_spawn(task="do thing", summary="   ", tag=TEST_TAG))

    assert payload["status"] == "error"
    assert payload["tool"] == "sessions_spawn"
    assert "summary must be a non-empty string" in payload["message"]


@pytest.mark.asyncio
async def test_sessions_spawn_rejects_overlong_summary(tmp_path: Path) -> None:
    """sessions_spawn should reject summaries longer than the shared thread-summary limit."""
    ctx = _make_context(tmp_path)

    with tool_runtime_context(ctx):
        payload = json.loads(
            await SubAgentsTools().sessions_spawn(
                task="do thing",
                summary="x" * (THREAD_SUMMARY_MAX_LENGTH + 1),
                tag=TEST_TAG,
            ),
        )

    assert payload["status"] == "error"
    assert payload["tool"] == "sessions_spawn"
    assert f"{THREAD_SUMMARY_MAX_LENGTH} characters or fewer" in payload["message"]


@pytest.mark.asyncio
async def test_sessions_spawn_strips_markdown_from_summary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """sessions_spawn should normalize markdown before writing the summary event."""
    send_mock = AsyncMock(return_value="$event")
    tag_mock = AsyncMock(return_value=SimpleNamespace())
    update_mock = MagicMock()
    monkeypatch.setattr(subagents_module, "_send_matrix_text", send_mock)
    monkeypatch.setattr(subagents_module, "set_thread_tag", tag_mock)
    monkeypatch.setattr(subagents_module, "update_last_summary_count", update_mock)
    ctx = _make_context(tmp_path)
    ctx.client.room_send = AsyncMock(
        return_value=nio.RoomSendResponse(event_id="$summary:localhost", room_id=ctx.room_id),
    )

    with tool_runtime_context(ctx):
        payload = json.loads(
            await SubAgentsTools().sessions_spawn(
                task="do thing",
                summary="# **Fix** [ISSUE-116](http://example.com)\n> `deploy` ~~done~~",
                tag=TEST_TAG,
            ),
        )

    assert payload["status"] == "ok"
    assert payload["summary"] == "Fix ISSUE-116 deploy done"
    ctx.client.room_send.assert_awaited_once()
    content = ctx.client.room_send.call_args.kwargs["content"]
    assert content["body"] == "Fix ISSUE-116 deploy done"
    assert content["io.mindroom.thread_summary"]["summary"] == "Fix ISSUE-116 deploy done"
    tag_mock.assert_awaited_once_with(
        ctx.client,
        ctx.room_id,
        "$event",
        TEST_TAG,
        set_by=ctx.requester_id,
    )
    update_mock.assert_called_once_with(ctx.room_id, "$event", 1)


@pytest.mark.asyncio
async def test_sessions_spawn_rejects_invalid_tag(tmp_path: Path) -> None:
    """sessions_spawn should reject invalid tag names before spawning."""
    ctx = _make_context(tmp_path)

    with tool_runtime_context(ctx):
        payload = json.loads(
            await SubAgentsTools().sessions_spawn(task="do thing", summary=TEST_SUMMARY, tag="INVALID TAG!"),
        )

    assert payload["status"] == "error"
    assert payload["tool"] == "sessions_spawn"
    assert "lowercase letters, digits, or hyphens" in payload["message"]


@pytest.mark.asyncio
async def test_sessions_spawn_validates_before_matrix_send(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """sessions_spawn should validate required spawn metadata before any Matrix call."""
    send_mock = AsyncMock(return_value="$event")
    monkeypatch.setattr(subagents_module, "_send_matrix_text", send_mock)
    ctx = _make_context(tmp_path)

    with tool_runtime_context(ctx):
        payload = json.loads(
            await SubAgentsTools().sessions_spawn(task="do thing", summary=TEST_SUMMARY, tag="INVALID TAG!"),
        )

    assert payload["status"] == "error"
    send_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_sessions_spawn_sets_summary_after_spawn(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """sessions_spawn should write the requested thread summary after creating the thread."""
    send_mock = AsyncMock(return_value="$event")
    monkeypatch.setattr(subagents_module, "_send_matrix_text", send_mock)
    summary_mock, _, update_mock = _stub_spawn_followups(monkeypatch)
    ctx = _make_context(tmp_path)

    with tool_runtime_context(ctx):
        payload = json.loads(
            await SubAgentsTools().sessions_spawn(task="do thing", summary=TEST_SUMMARY, tag=TEST_TAG),
        )

    assert payload["status"] == "ok"
    summary_mock.assert_awaited_once_with(
        ctx.client,
        ctx.room_id,
        "$event",
        TEST_SUMMARY,
        1,
        "manual",
        ctx.conversation_cache,
    )
    update_mock.assert_called_once_with(ctx.room_id, "$event", 1)


@pytest.mark.asyncio
async def test_sessions_spawn_sets_tag_after_spawn(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """sessions_spawn should write the requested thread tag after creating the thread."""
    send_mock = AsyncMock(return_value="$event")
    monkeypatch.setattr(subagents_module, "_send_matrix_text", send_mock)
    _, tag_mock, _ = _stub_spawn_followups(monkeypatch)
    ctx = _make_context(tmp_path)

    with tool_runtime_context(ctx):
        payload = json.loads(
            await SubAgentsTools().sessions_spawn(task="do thing", summary=TEST_SUMMARY, tag=TEST_TAG),
        )

    assert payload["status"] == "ok"
    tag_mock.assert_awaited_once_with(
        ctx.client,
        ctx.room_id,
        "$event",
        TEST_TAG,
        set_by=ctx.requester_id,
    )


@pytest.mark.asyncio
async def test_sessions_spawn_returns_warnings_on_summary_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """sessions_spawn should still succeed when setting the summary fails."""
    send_mock = AsyncMock(return_value="$event")
    monkeypatch.setattr(subagents_module, "_send_matrix_text", send_mock)
    summary_mock, tag_mock, update_mock = _stub_spawn_followups(monkeypatch)
    summary_mock.side_effect = RuntimeError("boom")
    ctx = _make_context(tmp_path)

    with tool_runtime_context(ctx):
        payload = json.loads(
            await SubAgentsTools().sessions_spawn(task="do thing", summary=TEST_SUMMARY, tag=TEST_TAG),
        )

    assert payload["status"] == "ok"
    assert payload["warnings"] == ["Failed to set thread summary: boom"]
    tag_mock.assert_awaited_once()
    update_mock.assert_not_called()


@pytest.mark.asyncio
async def test_sessions_spawn_returns_warnings_when_summary_event_id_is_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """sessions_spawn should warn when summary send returns no event id."""
    send_mock = AsyncMock(return_value="$event")
    monkeypatch.setattr(subagents_module, "_send_matrix_text", send_mock)
    summary_mock, tag_mock, update_mock = _stub_spawn_followups(monkeypatch)
    summary_mock.return_value = None
    ctx = _make_context(tmp_path)

    with tool_runtime_context(ctx):
        payload = json.loads(
            await SubAgentsTools().sessions_spawn(task="do thing", summary=TEST_SUMMARY, tag=TEST_TAG),
        )

    assert payload["status"] == "ok"
    assert payload["warnings"] == ["Failed to set thread summary."]
    tag_mock.assert_awaited_once()
    update_mock.assert_not_called()


@pytest.mark.asyncio
async def test_sessions_spawn_returns_warnings_on_tag_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """sessions_spawn should still succeed when setting the tag fails."""
    send_mock = AsyncMock(return_value="$event")
    monkeypatch.setattr(subagents_module, "_send_matrix_text", send_mock)
    summary_mock, tag_mock, update_mock = _stub_spawn_followups(monkeypatch)
    tag_mock.side_effect = RuntimeError("no power")
    ctx = _make_context(tmp_path)

    with tool_runtime_context(ctx):
        payload = json.loads(
            await SubAgentsTools().sessions_spawn(task="do thing", summary=TEST_SUMMARY, tag=TEST_TAG),
        )

    assert payload["status"] == "ok"
    assert payload["warnings"] == ["Failed to set thread tag: no power"]
    summary_mock.assert_awaited_once()
    update_mock.assert_called_once_with(ctx.room_id, "$event", 1)


@pytest.mark.asyncio
async def test_sessions_spawn_response_includes_summary_and_tag(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """sessions_spawn should return normalized summary and tag values in the success payload."""
    send_mock = AsyncMock(return_value="$event")
    monkeypatch.setattr(subagents_module, "_send_matrix_text", send_mock)
    _stub_spawn_followups(monkeypatch)
    ctx = _make_context(tmp_path)

    with tool_runtime_context(ctx):
        payload = json.loads(
            await SubAgentsTools().sessions_spawn(
                task="do thing",
                summary="  test   summary  ",
                tag="Test-Tag",
            ),
        )

    assert payload["status"] == "ok"
    assert payload["summary"] == TEST_SUMMARY
    assert payload["tag"] == TEST_TAG
    assert "warnings" not in payload


@pytest.mark.asyncio
async def test_list_sessions_requires_runtime_context() -> None:
    """list_sessions should return a structured context-unavailable error outside runtime scope."""
    payload = json.loads(await SubAgentsTools().list_sessions())
    assert payload["status"] == "error"
    assert payload["tool"] == "list_sessions"
    assert "context" in payload["message"]


@pytest.mark.asyncio
async def test_list_sessions_returns_tracked_sessions(tmp_path: Path) -> None:
    """list_sessions should return sessions persisted via _record_session."""
    ctx = _make_context(tmp_path)
    session_key = create_session_id(ctx.room_id, "$child:localhost")
    subagents_module._record_session(ctx, session_key=session_key, label="my-task", target_agent="code")

    with tool_runtime_context(ctx):
        payload = json.loads(await SubAgentsTools().list_sessions())

    assert payload["status"] == "ok"
    assert payload["tool"] == "list_sessions"
    assert payload["total"] == 1
    session = payload["sessions"][0]
    assert session["session_key"] == session_key
    assert session["label"] == "my-task"
    assert session["target_agent"] == "code"


@pytest.mark.asyncio
async def test_list_sessions_empty_when_no_sessions(tmp_path: Path) -> None:
    """list_sessions should return an empty page when registry has no in-scope sessions."""
    ctx = _make_context(tmp_path)

    with tool_runtime_context(ctx):
        payload = json.loads(await SubAgentsTools().list_sessions())

    assert payload["status"] == "ok"
    assert payload["sessions"] == []
    assert payload["total"] == 0


def test_load_registry_handles_non_dict_payload(tmp_path: Path) -> None:
    """_load_registry should normalize non-dict JSON payloads to an empty mapping."""
    ctx = _make_context(tmp_path)
    registry_dir = tmp_path / "subagents"
    registry_dir.mkdir(parents=True)
    (registry_dir / "session_registry.json").write_text(json.dumps(["unexpected", "array"]))

    registry = subagents_module._load_registry(ctx)
    assert registry == {}


def test_load_registry_returns_existing_dict_without_migration(tmp_path: Path) -> None:
    """_load_registry should preserve dict payloads (including legacy shapes) as-is."""
    ctx = _make_context(tmp_path)
    registry_dir = tmp_path / "subagents"
    registry_dir.mkdir(parents=True)
    old_data = {
        "sessions": {
            "!room:localhost:$thread:localhost": {
                "label": "old-session",
                "target_agent": "code",
            },
        },
        "runs": {"run-1": {"status": "accepted"}},
    }
    (registry_dir / "session_registry.json").write_text(json.dumps(old_data))

    registry = subagents_module._load_registry(ctx)
    assert registry == old_data


def test_resolve_by_label_require_thread_skips_entries_without_thread_id(tmp_path: Path) -> None:
    """Thread reuse should ignore malformed labeled entries without a stored thread id."""
    ctx = _make_context(tmp_path)
    session_key = create_session_id(ctx.room_id, "$child:localhost")
    subagents_module._record_session(ctx, session_key=session_key, label="work", target_agent="code")

    registry = subagents_module._load_registry(ctx)
    registry[session_key]["thread_id"] = None
    subagents_module._save_registry(ctx, registry)

    assert subagents_module._resolve_by_label(ctx, "work", require_thread=True) is None


@pytest.mark.asyncio
async def test_sessions_spawn_dedup_returns_existing_for_duplicate_label(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """sessions_spawn should return existing session when label already exists in scope."""
    send_mock = AsyncMock(return_value="$event")
    monkeypatch.setattr(subagents_module, "_send_matrix_text", send_mock)
    summary_mock, tag_mock, update_mock = _stub_spawn_followups(monkeypatch)
    ctx = _make_context(tmp_path)

    # First spawn creates the session
    with tool_runtime_context(ctx):
        first = json.loads(
            await SubAgentsTools().sessions_spawn(
                task="do thing",
                summary=TEST_SUMMARY,
                tag=TEST_TAG,
                label="work",
            ),
        )
    assert first["status"] == "ok"
    assert "reused" not in first
    send_mock.assert_awaited_once()
    summary_mock.assert_awaited_once_with(
        ctx.client,
        ctx.room_id,
        "$event",
        TEST_SUMMARY,
        1,
        "manual",
        ctx.conversation_cache,
    )
    tag_mock.assert_awaited_once_with(
        ctx.client,
        ctx.room_id,
        "$event",
        TEST_TAG,
        set_by=ctx.requester_id,
    )
    update_mock.assert_called_once_with(ctx.room_id, "$event", 1)

    send_mock.reset_mock()
    summary_mock.reset_mock()
    tag_mock.reset_mock()
    update_mock.reset_mock()

    # Second spawn with same label should reuse, no Matrix message sent
    with tool_runtime_context(ctx):
        second = json.loads(
            await SubAgentsTools().sessions_spawn(
                task="do thing again",
                summary=TEST_SUMMARY,
                tag=TEST_TAG,
                label="work",
            ),
        )
    assert second["status"] == "ok"
    assert second["reused"] is True
    assert second["session_key"] == first["session_key"]
    assert second["target_agent"] == first["target_agent"]
    assert second["event_id"] == first["event_id"]
    assert second["summary"] == TEST_SUMMARY
    assert second["tag"] == TEST_TAG
    send_mock.assert_not_awaited()
    summary_mock.assert_awaited_once_with(
        ctx.client,
        ctx.room_id,
        "$event",
        TEST_SUMMARY,
        0,
        "manual",
        ctx.conversation_cache,
    )
    tag_mock.assert_awaited_once_with(
        ctx.client,
        ctx.room_id,
        "$event",
        TEST_TAG,
        set_by=ctx.requester_id,
    )
    update_mock.assert_not_called()


@pytest.mark.asyncio
async def test_sessions_spawn_skips_reuse_when_registry_entry_lacks_thread_id(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """sessions_spawn should create a fresh thread when a labeled entry is missing thread_id metadata."""
    send_mock = AsyncMock(return_value="$new-event")
    monkeypatch.setattr(subagents_module, "_send_matrix_text", send_mock)
    summary_mock, tag_mock, update_mock = _stub_spawn_followups(monkeypatch)
    ctx = _make_context(tmp_path)
    session_key = create_session_id(ctx.room_id, "$canonical-thread:localhost")
    subagents_module._record_session(ctx, session_key=session_key, label="work", target_agent="code")

    registry = subagents_module._load_registry(ctx)
    registry[session_key]["thread_id"] = None
    subagents_module._save_registry(ctx, registry)

    with tool_runtime_context(ctx):
        payload = json.loads(
            await SubAgentsTools().sessions_spawn(
                task="do thing",
                summary=TEST_SUMMARY,
                tag=TEST_TAG,
                label="work",
            ),
        )

    assert payload["status"] == "ok"
    assert "reused" not in payload
    assert payload["event_id"] == "$new-event"
    assert payload["session_key"] == create_session_id(ctx.room_id, "$new-event")
    send_mock.assert_awaited_once()
    summary_mock.assert_awaited_once_with(
        ctx.client,
        ctx.room_id,
        "$new-event",
        TEST_SUMMARY,
        1,
        "manual",
        ctx.conversation_cache,
    )
    tag_mock.assert_awaited_once_with(
        ctx.client,
        ctx.room_id,
        "$new-event",
        TEST_TAG,
        set_by=ctx.requester_id,
    )
    update_mock.assert_called_once_with(ctx.room_id, "$new-event", 1)


@pytest.mark.asyncio
async def test_sessions_spawn_reuse_derives_thread_id_from_session_key(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """sessions_spawn should target the canonical thread id encoded in the session key when reusing."""
    send_mock = AsyncMock(return_value="$new-event")
    monkeypatch.setattr(subagents_module, "_send_matrix_text", send_mock)
    summary_mock, tag_mock, update_mock = _stub_spawn_followups(monkeypatch)
    ctx = _make_context(tmp_path)
    session_key = create_session_id(ctx.room_id, "$canonical-thread:localhost")
    subagents_module._record_session(ctx, session_key=session_key, label="work", target_agent="code")

    registry = subagents_module._load_registry(ctx)
    registry[session_key]["thread_id"] = "$stale-thread:localhost"
    subagents_module._save_registry(ctx, registry)

    with tool_runtime_context(ctx):
        payload = json.loads(
            await SubAgentsTools().sessions_spawn(
                task="do thing",
                summary=TEST_SUMMARY,
                tag=TEST_TAG,
                label="work",
            ),
        )

    assert payload["status"] == "ok"
    assert payload["reused"] is True
    assert payload["session_key"] == session_key
    assert payload["event_id"] == "$canonical-thread:localhost"
    send_mock.assert_not_awaited()
    summary_mock.assert_awaited_once_with(
        ctx.client,
        ctx.room_id,
        "$canonical-thread:localhost",
        TEST_SUMMARY,
        0,
        "manual",
        ctx.conversation_cache,
    )
    tag_mock.assert_awaited_once_with(
        ctx.client,
        ctx.room_id,
        "$canonical-thread:localhost",
        TEST_TAG,
        set_by=ctx.requester_id,
    )
    update_mock.assert_not_called()


@pytest.mark.asyncio
async def test_sessions_spawn_skips_room_level_reuse_candidates(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """sessions_spawn should create a new thread when the only labeled match is room-level."""
    send_mock = AsyncMock(return_value="$event")
    monkeypatch.setattr(subagents_module, "_send_matrix_text", send_mock)
    summary_mock, tag_mock, update_mock = _stub_spawn_followups(monkeypatch)
    ctx = _make_context(tmp_path)
    subagents_module._record_session(ctx, session_key=ctx.room_id, label="work", target_agent="code")

    with tool_runtime_context(ctx):
        payload = json.loads(
            await SubAgentsTools().sessions_spawn(
                task="do thing",
                summary=TEST_SUMMARY,
                tag=TEST_TAG,
                label="work",
            ),
        )

    assert payload["status"] == "ok"
    assert "reused" not in payload
    assert payload["event_id"] == "$event"
    send_mock.assert_awaited_once()
    summary_mock.assert_awaited_once_with(
        ctx.client,
        ctx.room_id,
        "$event",
        TEST_SUMMARY,
        1,
        "manual",
        ctx.conversation_cache,
    )
    tag_mock.assert_awaited_once_with(
        ctx.client,
        ctx.room_id,
        "$event",
        TEST_TAG,
        set_by=ctx.requester_id,
    )
    update_mock.assert_called_once_with(ctx.room_id, "$event", 1)


@pytest.mark.asyncio
async def test_sessions_spawn_no_label_skips_dedup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """sessions_spawn should always spawn new session when label is None."""
    send_mock = AsyncMock(return_value="$event1")
    monkeypatch.setattr(subagents_module, "_send_matrix_text", send_mock)
    _stub_spawn_followups(monkeypatch)
    ctx = _make_context(tmp_path)

    with tool_runtime_context(ctx):
        first = json.loads(
            await SubAgentsTools().sessions_spawn(task="do thing", summary=TEST_SUMMARY, tag=TEST_TAG),
        )
    assert first["status"] == "ok"

    send_mock.return_value = "$event2"

    with tool_runtime_context(ctx):
        second = json.loads(
            await SubAgentsTools().sessions_spawn(task="do thing again", summary=TEST_SUMMARY, tag=TEST_TAG),
        )
    assert second["status"] == "ok"
    assert "reused" not in second
    assert send_mock.await_count == 2


@pytest.mark.asyncio
async def test_sessions_spawn_dedup_scoped_by_context(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """sessions_spawn with same label but different scope should spawn new session."""
    send_mock = AsyncMock(return_value="$event1")
    monkeypatch.setattr(subagents_module, "_send_matrix_text", send_mock)
    _stub_spawn_followups(monkeypatch)
    ctx1 = _make_context(tmp_path, requester_id="@alice:localhost")

    with tool_runtime_context(ctx1):
        first = json.loads(
            await SubAgentsTools().sessions_spawn(
                task="do thing",
                summary=TEST_SUMMARY,
                tag=TEST_TAG,
                label="work",
            ),
        )
    assert first["status"] == "ok"

    send_mock.return_value = "$event2"
    ctx2 = _make_context(tmp_path, requester_id="@bob:localhost")

    with tool_runtime_context(ctx2):
        second = json.loads(
            await SubAgentsTools().sessions_spawn(
                task="do thing",
                summary=TEST_SUMMARY,
                tag=TEST_TAG,
                label="work",
            ),
        )
    assert second["status"] == "ok"
    assert "reused" not in second
    assert second["session_key"] != first["session_key"]
    assert send_mock.await_count == 2


@pytest.mark.asyncio
async def test_sessions_spawn_dedup_scoped_by_room(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """sessions_spawn with same label but different room should spawn new session."""
    send_mock = AsyncMock(return_value="$event1")
    monkeypatch.setattr(subagents_module, "_send_matrix_text", send_mock)
    _stub_spawn_followups(monkeypatch)
    ctx1 = _make_context(tmp_path, room_id="!room_a:localhost")

    with tool_runtime_context(ctx1):
        first = json.loads(
            await SubAgentsTools().sessions_spawn(
                task="do thing",
                summary=TEST_SUMMARY,
                tag=TEST_TAG,
                label="work",
            ),
        )
    assert first["status"] == "ok"

    send_mock.return_value = "$event2"
    ctx2 = _make_context(tmp_path, room_id="!room_b:localhost")

    with tool_runtime_context(ctx2):
        second = json.loads(
            await SubAgentsTools().sessions_spawn(
                task="do thing",
                summary=TEST_SUMMARY,
                tag=TEST_TAG,
                label="work",
            ),
        )
    assert second["status"] == "ok"
    assert "reused" not in second
    assert second["session_key"] != first["session_key"]
    assert send_mock.await_count == 2


def test_record_session_updates_existing_entry_fields(tmp_path: Path) -> None:
    """_record_session should update mutable fields without dropping existing target_agent."""
    ctx = _make_context(tmp_path)
    session_key = "!room:localhost:$thread:localhost"

    subagents_module._record_session(ctx, session_key=session_key, label="first", target_agent="code")
    subagents_module._record_session(ctx, session_key=session_key, label="second")

    registry = subagents_module._load_registry(ctx)
    assert registry[session_key]["label"] == "second"
    assert registry[session_key]["target_agent"] == "code"

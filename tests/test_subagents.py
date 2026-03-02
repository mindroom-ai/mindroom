"""Tests for the standalone subagents toolkit and session registry helpers."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock

import pytest

import mindroom.tools  # noqa: F401
from mindroom.custom_tools import subagents as subagents_module
from mindroom.custom_tools.subagents import SubAgentsTools
from mindroom.thread_utils import create_session_id
from mindroom.tool_runtime_context import ToolRuntimeContext, tool_runtime_context
from mindroom.tools_metadata import TOOL_METADATA, get_tool_by_name

if TYPE_CHECKING:
    from pathlib import Path


EXPECTED_SUBAGENT_TOOL_NAMES = {
    "agents_list",
    "sessions_send",
    "sessions_spawn",
    "list_sessions",
}


def _make_config(*, thread_mode: str = "thread") -> MagicMock:
    config = MagicMock()
    config.agents = {
        "openclaw": SimpleNamespace(tools=["shell"]),
        "code": SimpleNamespace(tools=["shell"]),
        "research": SimpleNamespace(tools=["shell"]),
    }
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
    return ToolRuntimeContext(
        agent_name="openclaw",
        room_id=room_id,
        thread_id=thread_id,
        resolved_thread_id=thread_id,
        requester_id=requester_id,
        client=MagicMock(),
        config=config or _make_config(),
        room=None,
        reply_to_event_id=None,
        storage_path=tmp_path,
    )


def test_subagents_tool_registered_and_instantiates() -> None:
    """Subagents should be present in metadata and constructible from the registry."""
    assert "subagents" in TOOL_METADATA
    assert isinstance(get_tool_by_name("subagents"), SubAgentsTools)


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
    payload = json.loads(await SubAgentsTools().sessions_spawn(task="do this"))
    assert payload["status"] == "error"
    assert payload["tool"] == "sessions_spawn"
    assert "context" in payload["message"]


@pytest.mark.asyncio
async def test_sessions_spawn_rejects_empty_task(tmp_path: Path) -> None:
    """sessions_spawn should validate non-empty task content."""
    ctx = _make_context(tmp_path)

    with tool_runtime_context(ctx):
        payload = json.loads(await SubAgentsTools().sessions_spawn(task="  "))

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
        payload = json.loads(await SubAgentsTools().sessions_spawn(task="do thing"))

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
    ctx = _make_context(tmp_path, requester_id="@user:localhost")

    with tool_runtime_context(ctx):
        payload = json.loads(await SubAgentsTools().sessions_spawn(task="do thing"))

    assert payload["status"] == "ok"
    assert payload["target_agent"] == "openclaw"
    assert payload["event_id"] == "$event"
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
        payload = json.loads(await SubAgentsTools().sessions_spawn(task="do thing"))

    assert payload["status"] == "error"
    assert payload["tool"] == "sessions_spawn"
    assert "thread_mode=room" in payload["message"]
    send_mock.assert_not_awaited()


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


def test_record_session_updates_existing_entry_fields(tmp_path: Path) -> None:
    """_record_session should update mutable fields without dropping existing target_agent."""
    ctx = _make_context(tmp_path)
    session_key = "!room:localhost:$thread:localhost"

    subagents_module._record_session(ctx, session_key=session_key, label="first", target_agent="code")
    subagents_module._record_session(ctx, session_key=session_key, label="second")

    registry = subagents_module._load_registry(ctx)
    assert registry[session_key]["label"] == "second"
    assert registry[session_key]["target_agent"] == "code"

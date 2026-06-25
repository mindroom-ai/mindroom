"""Tests for the built-in todo tool."""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock

import mindroom.tools  # noqa: F401
from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.tool_system.metadata import TOOL_METADATA, get_tool_by_name
from mindroom.tool_system.runtime_context import ToolRuntimeContext, tool_runtime_context
from tests.conftest import (
    bind_runtime_paths,
    make_conversation_cache_mock,
    make_event_cache_mock,
    runtime_paths_for,
    test_runtime_paths,
)

if TYPE_CHECKING:
    from pathlib import Path


def _config(tmp_path: Path) -> Config:
    return bind_runtime_paths(
        Config(agents={"code": AgentConfig(display_name="Code", rooms=["!room:localhost"])}),
        runtime_paths=test_runtime_paths(tmp_path),
    )


def _tool_context(
    config: Config,
    *,
    room_id: str = "!room:localhost",
    thread_id: str | None = None,
    resolved_thread_id: str | None = "$thread-root",
) -> ToolRuntimeContext:
    return ToolRuntimeContext(
        agent_name="code",
        room_id=room_id,
        thread_id=thread_id,
        resolved_thread_id=resolved_thread_id,
        requester_id="@user:localhost",
        client=AsyncMock(),
        config=config,
        runtime_paths=runtime_paths_for(config),
        event_cache=make_event_cache_mock(),
        conversation_cache=make_conversation_cache_mock(),
        room=MagicMock(),
        reply_to_event_id=None,
        storage_path=None,
    )


def _thread_key(room_id: str, thread_id: str) -> str:
    return re.sub(r"_+", "_", re.sub(r"[^A-Za-z0-9]", "_", f"{room_id}_{thread_id}")).strip("_")


def _todos_path(config: Config, *, room_id: str, thread_id: str) -> Path:
    return runtime_paths_for(config).storage_root / "todo" / "threads" / _thread_key(room_id, thread_id) / "todos.json"


def _read_todos(
    config: Config,
    *,
    room_id: str = "!room:localhost",
    thread_id: str = "$thread-root",
) -> dict[str, object]:
    return json.loads(_todos_path(config, room_id=room_id, thread_id=thread_id).read_text(encoding="utf-8"))


def test_todo_is_registered_as_builtin_tool(tmp_path: Path) -> None:
    """Todo should be available without configuring an external plugin."""
    config = _config(tmp_path)

    assert "todo" in TOOL_METADATA
    metadata = TOOL_METADATA["todo"]
    assert metadata.display_name == "Todo"
    assert metadata.setup_type == "none"
    assert set(metadata.function_names) == {
        "add_todo",
        "complete_todo",
        "list_todos",
        "plan",
        "update_todo",
        "apply_template",
        "list_templates",
    }

    tool = get_tool_by_name("todo", runtime_paths_for(config), worker_target=None)

    assert tool.__class__.__name__ == "TodoTools"
    assert tool.name == "todo"


def test_todo_plan_and_complete_persist_under_current_thread(tmp_path: Path) -> None:
    """Tool calls should persist a per-thread work plan under MindRoom state."""
    config = _config(tmp_path)
    tool = get_tool_by_name("todo", runtime_paths_for(config), worker_target=None)

    with tool_runtime_context(_tool_context(config)):
        result = tool.plan(agent=MagicMock(), tasks="[high] Design API\nImplement storage")

    assert "Created 2 item" in result
    state = _read_todos(config)
    assert state["room_id"] == "!room:localhost"
    assert state["thread_id"] == "$thread-root"
    assert [item["title"] for item in state["items"]] == ["Design API", "Implement storage"]
    assert [item["priority"] for item in state["items"]] == ["high", "medium"]
    assert [item["assigned_agent"] for item in state["items"]] == ["code", "code"]

    first_id = state["items"][0]["id"]
    second_id = state["items"][1]["id"]
    with tool_runtime_context(_tool_context(config)):
        dep_result = tool.update_todo(agent=MagicMock(), todo_id=second_id, depends_on=first_id)
        complete_result = tool.complete_todo(agent=MagicMock(), todo_id=first_id)

    assert "depends_on" in dep_result
    assert "Now unblocked" in complete_result
    updated = _read_todos(config)
    assert updated["items"][0]["status"] == "done"
    assert updated["items"][1]["depends_on"] == [first_id]


def test_todo_bundled_templates_are_visible_and_apply(tmp_path: Path) -> None:
    """Built-in templates should ship with the package and create dependent todos."""
    config = _config(tmp_path)
    tool = get_tool_by_name("todo", runtime_paths_for(config), worker_target=None)

    with tool_runtime_context(_tool_context(config)):
        listing = tool.list_templates(agent=MagicMock())
        preview = tool.apply_template(
            agent=MagicMock(),
            name="mindroom-dev",
            params={"ISSUE_REF": "ISSUE-1", "REPO": "mindroom"},
            dry_run=True,
        )
        result = tool.apply_template(
            agent=MagicMock(),
            name="mindroom-dev",
            params={"ISSUE_REF": "ISSUE-1", "REPO": "mindroom"},
        )

    assert "`mindroom-dev`" in listing
    assert "Preview:" in preview
    assert "ISSUE-1" in preview
    assert "Applied template `mindroom-dev`" in result
    state = _read_todos(config)
    items = state["items"]
    assert len(items) > 1
    assert any(item["depends_on"] for item in items)

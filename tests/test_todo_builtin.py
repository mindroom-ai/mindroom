"""Tests for the built-in todo tool."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock

import pytest
from agno.agent import Agent as AgnoAgent
from agno.team.team import Team as AgnoTeam

import mindroom.tools  # noqa: F401
from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.custom_tools.todo import _thread_key
from mindroom.tool_schema_cache import process_function_schema_for_prompt
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
    agent_name: str = "code",
    room_id: str = "!room:localhost",
    thread_id: str | None = None,
    resolved_thread_id: str | None = "$thread-root",
) -> ToolRuntimeContext:
    return ToolRuntimeContext(
        agent_name=agent_name,
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


def _todos_path(config: Config, *, room_id: str, thread_id: str) -> Path:
    return runtime_paths_for(config).storage_root / "todo" / "threads" / _thread_key(room_id, thread_id) / "todos.json"


def _read_todos(
    config: Config,
    *,
    room_id: str = "!room:localhost",
    thread_id: str = "$thread-root",
) -> dict[str, object]:
    return json.loads(_todos_path(config, room_id=room_id, thread_id=thread_id).read_text(encoding="utf-8"))


def _workspace_template_dir(config: Config) -> Path:
    return runtime_paths_for(config).storage_root / "agents" / "code" / "workspace" / "todo" / "templates"


def _write_workspace_template(config: Config, name: str, text: str) -> None:
    template_dir = _workspace_template_dir(config)
    template_dir.mkdir(parents=True, exist_ok=True)
    (template_dir / f"{name}.yaml.j2").write_text(text, encoding="utf-8")


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


def test_todo_tool_schemas_include_model_visible_arguments(tmp_path: Path) -> None:
    """Runtime annotations should resolve so Agno exposes tool-call arguments."""
    config = _config(tmp_path)
    tool = get_tool_by_name("todo", runtime_paths_for(config), worker_target=None)
    for function in tool.functions.values():
        process_function_schema_for_prompt(function, strict=False)

    plan_params = tool.functions["plan"].parameters["properties"]
    add_params = tool.functions["add_todo"].parameters["properties"]
    complete_params = tool.functions["complete_todo"].parameters["properties"]
    apply_template_params = tool.functions["apply_template"].parameters["properties"]

    assert "tasks" in plan_params
    assert "title" in add_params
    assert "todo_id" in complete_params
    assert {"name", "params"}.issubset(apply_template_params)


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


def test_add_todo_rejects_empty_title(tmp_path: Path) -> None:
    """A blank title should not create an actionable item."""
    config = _config(tmp_path)
    tool = get_tool_by_name("todo", runtime_paths_for(config), worker_target=None)

    with tool_runtime_context(_tool_context(config)):
        result = tool.add_todo(agent=MagicMock(), title="   ")

    assert result == "Title cannot be empty."
    assert not _todos_path(config, room_id="!room:localhost", thread_id="$thread-root").exists()


def test_todo_defaults_to_member_agent_inside_team_context(tmp_path: Path) -> None:
    """Team-scoped calls should not use the team id as an invalid assignee."""
    config = _config(tmp_path)
    tool = get_tool_by_name("todo", runtime_paths_for(config), worker_target=None)
    member_agent = AgnoAgent(name="Code", id="code")

    with tool_runtime_context(_tool_context(config, agent_name="dev_team")):
        plan_result = tool.plan(agent=member_agent, tasks="Team plan")
        add_result = tool.add_todo(agent=member_agent, title="  Team item  ")
        template_result = tool.apply_template(
            agent=member_agent,
            name="mindroom-dev",
            params={"ISSUE_REF": "ISSUE-1", "REPO": "mindroom"},
        )

    assert "Created 1 item" in plan_result
    assert "Created" in add_result
    assert "Applied template `mindroom-dev`" in template_result
    state = _read_todos(config)
    by_title = {item["title"]: item for item in state["items"]}  # type: ignore[union-attr]
    assert by_title["Team plan"]["assigned_agent"] == "code"
    assert by_title["Team item"]["title"] == "Team item"
    assert by_title["Team item"]["assigned_agent"] == "code"
    assert "dev_team" not in {item["assigned_agent"] for item in state["items"]}  # type: ignore[union-attr]


def test_todo_team_object_default_assignee_is_unassigned(tmp_path: Path) -> None:
    """If Agno passes the team object, the team id should not be stored as an assignee."""
    config = _config(tmp_path)
    tool = get_tool_by_name("todo", runtime_paths_for(config), worker_target=None)
    team = AgnoTeam(name="dev_team", members=[AgnoAgent(name="code")])

    with tool_runtime_context(_tool_context(config, agent_name="dev_team")):
        result = tool.add_todo(agent=team, title="Team-owned item")

    assert "Created" in result
    state = _read_todos(config)
    assert state["items"][0]["assigned_agent"] == ""  # type: ignore[index]


def test_todo_thread_storage_keys_do_not_collide_for_similar_ids(tmp_path: Path) -> None:
    """Matrix IDs that normalize to the same slug should still use separate state files."""
    config = _config(tmp_path)
    tool = get_tool_by_name("todo", runtime_paths_for(config), worker_target=None)
    first_room = "!room-a:localhost"
    second_room = "!room_a:localhost"

    assert _todos_path(config, room_id=first_room, thread_id="$thread-root") != _todos_path(
        config,
        room_id=second_room,
        thread_id="$thread-root",
    )

    with tool_runtime_context(_tool_context(config, room_id=first_room)):
        tool.plan(agent=MagicMock(), tasks="First room item")
    with tool_runtime_context(_tool_context(config, room_id=second_room)):
        tool.plan(agent=MagicMock(), tasks="Second room item")

    first_state = _read_todos(config, room_id=first_room)
    second_state = _read_todos(config, room_id=second_room)
    assert first_state["items"][0]["title"] == "First room item"  # type: ignore[index]
    assert second_state["items"][0]["title"] == "Second room item"  # type: ignore[index]


def test_update_todo_validates_before_mutating_and_can_clear_dependencies(tmp_path: Path) -> None:
    """Rejected updates should not persist earlier field changes, and empty depends_on clears deps."""
    config = _config(tmp_path)
    tool = get_tool_by_name("todo", runtime_paths_for(config), worker_target=None)

    with tool_runtime_context(_tool_context(config)):
        tool.plan(agent=MagicMock(), tasks="Root\nChild")
    state = _read_todos(config)
    root_id = state["items"][0]["id"]  # type: ignore[index]
    child_id = state["items"][1]["id"]  # type: ignore[index]

    with tool_runtime_context(_tool_context(config)):
        invalid_result = tool.update_todo(
            agent=MagicMock(),
            todo_id=child_id,
            title="Mutated title",
            depends_on="missing",
        )

    assert "Dependency `missing` not found" in invalid_result
    unchanged = _read_todos(config)
    assert unchanged["items"][1]["title"] == "Child"  # type: ignore[index]

    with tool_runtime_context(_tool_context(config)):
        blank_title_result = tool.update_todo(agent=MagicMock(), todo_id=child_id, title="   ")

    assert blank_title_result == "Title cannot be empty."
    unchanged = _read_todos(config)
    assert unchanged["items"][1]["title"] == "Child"  # type: ignore[index]

    with tool_runtime_context(_tool_context(config)):
        set_result = tool.update_todo(agent=MagicMock(), todo_id=child_id, depends_on=root_id)
        clear_result = tool.update_todo(agent=MagicMock(), todo_id=child_id, depends_on="")

    assert "depends_on" in set_result
    assert "depends_on=[]" in clear_result
    cleared = _read_todos(config)
    assert cleared["items"][1]["depends_on"] == []  # type: ignore[index]


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


def test_workspace_template_root_symlink_escape_is_rejected(tmp_path: Path) -> None:
    """Workspace template discovery should fail closed if the template dir escapes the workspace."""
    config = _config(tmp_path)
    tool = get_tool_by_name("todo", runtime_paths_for(config), worker_target=None)
    outside = tmp_path / "outside-templates"
    outside.mkdir()
    template_dir = _workspace_template_dir(config)
    template_dir.parent.mkdir(parents=True, exist_ok=True)
    template_dir.symlink_to(outside, target_is_directory=True)

    with tool_runtime_context(_tool_context(config)), pytest.raises(ValueError, match="escapes workspace"):
        tool.list_templates(agent=MagicMock())


def test_workspace_template_shadow_uses_workspace_params_schema(tmp_path: Path) -> None:
    """A workspace template that shadows a built-in name should not inherit the built-in schema."""
    config = _config(tmp_path)
    tool = get_tool_by_name("todo", runtime_paths_for(config), worker_target=None)
    _write_workspace_template(
        config,
        "mindroom-dev",
        """name: mindroom-dev
version: "custom"
description: Custom workspace shadow.
todos:
  - title: Custom {{ CUSTOM }}
""",
    )

    with tool_runtime_context(_tool_context(config)):
        listing = tool.list_templates(agent=MagicMock())
        preview = tool.apply_template(
            agent=MagicMock(),
            name="mindroom-dev",
            params={"CUSTOM": "workspace"},
            dry_run=True,
        )
        result = tool.apply_template(
            agent=MagicMock(),
            name="mindroom-dev",
            params={"CUSTOM": "workspace"},
        )

    assert "| `workspace` | `mindroom-dev` | `custom` | Custom workspace shadow. | - |" in listing
    assert "ISSUE_REF" not in listing
    assert "Custom workspace" in preview
    assert "Custom workspace" in result


def test_team_member_context_uses_member_workspace_templates(tmp_path: Path) -> None:
    """Team member calls should resolve workspace templates from the member agent, not the team context."""
    config = _config(tmp_path)
    tool = get_tool_by_name("todo", runtime_paths_for(config), worker_target=None)
    member_agent = AgnoAgent(name="Code", id="code")
    _write_workspace_template(
        config,
        "team-member-template",
        """name: team-member-template
version: "1"
description: Member workspace template.
todos:
  - title: Member workspace task
""",
    )

    with tool_runtime_context(_tool_context(config, agent_name="dev_team")):
        listing = tool.list_templates(agent=member_agent)
        result = tool.apply_template(
            agent=member_agent,
            name="team-member-template",
            params={},
        )

    assert "| `workspace` | `team-member-template` | `1` | Member workspace template. | - |" in listing
    assert "Applied template `team-member-template`" in result
    state = _read_todos(config)
    assert state["items"][0]["title"] == "Member workspace task"  # type: ignore[index]
    assert state["items"][0]["assigned_agent"] == "code"  # type: ignore[index]


def test_missing_todo_read_and_error_paths_do_not_create_thread_state(tmp_path: Path) -> None:
    """Read-only and missing-item failures should not create empty per-thread state files."""
    config = _config(tmp_path)
    tool = get_tool_by_name("todo", runtime_paths_for(config), worker_target=None)
    path = _todos_path(config, room_id="!room:localhost", thread_id="$thread-root")

    with tool_runtime_context(_tool_context(config)):
        list_result = tool.list_todos(agent=MagicMock())
        complete_result = tool.complete_todo(agent=MagicMock(), todo_id="missing")
        update_result = tool.update_todo(agent=MagicMock(), todo_id="missing", title="Still missing")
        add_result = tool.add_todo(agent=MagicMock(), title="Blocked item", depends_on="missing")

    assert list_result == "No items in this thread's work plan."
    assert complete_result == "Todo `missing` not found."
    assert update_result == "Todo `missing` not found."
    assert add_result == "Dependency `missing` not found."
    assert not path.exists()
    assert not path.parent.exists()


def test_template_includes_depend_on_all_roots_and_unblock_after_all_terminals(tmp_path: Path) -> None:
    """Nested templates should preserve multi-root and multi-leaf dependency boundaries."""
    config = _config(tmp_path)
    tool = get_tool_by_name("todo", runtime_paths_for(config), worker_target=None)
    _write_workspace_template(
        config,
        "multi-child",
        """name: multi-child
version: "1"
description: Child graph with two roots and one terminal.
todos:
  - title: Child root A
  - title: Child root B
  - title: Child join
    depends_on: [1, 2]
""",
    )
    _write_workspace_template(
        config,
        "parent-flow",
        """name: parent-flow
version: "1"
description: Parent graph using nested child graph.
todos:
  - title: Setup
  - sub_template: multi-child
    depends_on: [1]
  - title: After child
    depends_on: [2]
""",
    )

    with tool_runtime_context(_tool_context(config)):
        tool.apply_template(agent=MagicMock(), name="parent-flow", params={})

    state = _read_todos(config)
    by_title = {item["title"]: item for item in state["items"]}  # type: ignore[union-attr]
    setup_id = by_title["Setup"]["id"]
    child_root_a_id = by_title["Child root A"]["id"]
    child_root_b_id = by_title["Child root B"]["id"]
    child_join_id = by_title["Child join"]["id"]

    assert by_title["Child root A"]["depends_on"] == [setup_id]
    assert by_title["Child root B"]["depends_on"] == [setup_id]
    assert by_title["Child join"]["depends_on"] == [child_root_a_id, child_root_b_id]
    assert by_title["After child"]["depends_on"] == [child_join_id]


def test_apply_template_rejects_unknown_assigned_agent(tmp_path: Path) -> None:
    """Templates should not write assignees outside the configured agent set."""
    config = _config(tmp_path)
    tool = get_tool_by_name("todo", runtime_paths_for(config), worker_target=None)
    _write_workspace_template(
        config,
        "bad-assignee",
        """name: bad-assignee
version: "1"
description: Invalid assignee.
todos:
  - title: Owned elsewhere
    assigned_agent: missing
""",
    )

    with tool_runtime_context(_tool_context(config)):
        result = tool.apply_template(agent=MagicMock(), name="bad-assignee", params={})

    assert "Unknown agent 'missing'" in result
    assert not _todos_path(config, room_id="!room:localhost", thread_id="$thread-root").exists()

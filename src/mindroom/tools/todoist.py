"""Todoist task and project management toolkit."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Literal

from mindroom.tool_system.authoring import Toolkit
from mindroom.tool_system.declarations import ConfigField, SetupType, ToolCategory, ToolFileAccess, ToolStatus
from mindroom.tool_system.registration import register_tool_with_metadata
from mindroom.tools.native_helpers import error_json, json_dumps, require_non_empty, safe_path_segment

_ReadAction = Literal["get_task", "active_tasks", "projects"]
_WriteAction = Literal["create_task", "update_task", "close_task", "delete_task"]


def _serialize_item(item: Any) -> dict[str, Any]:  # noqa: ANN401
    if hasattr(item, "to_dict") and callable(item.to_dict):
        return item.to_dict()
    result: dict[str, Any] = {
        "id": item.id,
        "content": getattr(item, "content", None),
        "description": getattr(item, "description", None),
        "project_id": getattr(item, "project_id", None),
        "section_id": getattr(item, "section_id", None),
        "parent_id": getattr(item, "parent_id", None),
        "order": getattr(item, "order", None),
        "priority": getattr(item, "priority", None),
        "url": getattr(item, "url", None),
        "creator_id": getattr(item, "creator_id", None),
        "created_at": getattr(item, "created_at", None),
        "labels": getattr(item, "labels", None),
    }
    if hasattr(item, "comment_count"):
        result["comment_count"] = item.comment_count
    due = getattr(item, "due", None)
    if due is not None:
        result["due"] = {
            "date": getattr(due, "date", None),
            "string": getattr(due, "string", None),
            "lang": getattr(due, "lang", None),
            "is_recurring": getattr(due, "is_recurring", None),
            "timezone": getattr(due, "timezone", None),
        }
    return result


def _flatten_pages(pages: Any) -> list[dict[str, Any]]:  # noqa: ANN401
    items: list[dict[str, Any]] = []
    for page in pages:
        if isinstance(page, list):
            items.extend(_serialize_item(item) for item in page)
        else:
            items.append(_serialize_item(page))
    return items


def _optional_task_fields(
    *,
    content: str | None = None,
    description: str | None = None,
    project_id: str | None = None,
    section_id: str | None = None,
    priority: int | None = None,
    labels: list[str] | None = None,
    due_string: str | None = None,
    due_date: str | None = None,
    due_datetime: str | None = None,
    due_lang: str | None = None,
    assignee_id: str | None = None,
) -> dict[str, Any]:
    raw: dict[str, Any] = {
        "content": content,
        "description": description,
        "project_id": safe_path_segment(project_id, "project_id") if project_id is not None else None,
        "section_id": safe_path_segment(section_id, "section_id") if section_id is not None else None,
        "priority": priority,
        "labels": labels,
        "due_string": due_string,
        "due_date": date.fromisoformat(due_date) if due_date is not None else None,
        "due_datetime": datetime.fromisoformat(due_datetime) if due_datetime is not None else None,
        "due_lang": due_lang,
        "assignee_id": assignee_id,
    }
    return {key: value for key, value in raw.items() if value is not None}


class _TodoistTools(Toolkit):
    """Manage Todoist tasks and projects via `todoist-api-python`."""

    def __init__(self, api_token: str | None = None) -> None:
        self.api_token = require_non_empty(api_token, "Todoist API token")

        from todoist_api_python.api import TodoistAPI

        self.api: Any = TodoistAPI(self.api_token)
        super().__init__(name="todoist", tools=[self.todoist_read, self.todoist_write])

    def todoist_read(
        self,
        action: _ReadAction,
        task_id: str | None = None,
        project_id: str | None = None,
    ) -> str:
        """Read Todoist tasks and projects.

        Actions:
        - `get_task`: Fetch a task by `task_id`.
        - `active_tasks`: List active tasks (optional `project_id` filter).
        - `projects`: List all projects.
        """
        try:
            if action == "get_task":
                safe_id = safe_path_segment(require_non_empty(task_id, "task_id"), "task_id")
                return json_dumps(_serialize_item(self.api.get_task(safe_id)))
            if action == "active_tasks":
                kwargs = {"project_id": safe_path_segment(project_id, "project_id")} if project_id is not None else {}
                return json_dumps(_flatten_pages(self.api.get_tasks(**kwargs)))
            return json_dumps(_flatten_pages(self.api.get_projects()))
        except Exception as exc:
            return error_json(str(exc))

    def _update_task(self, safe_id: str, fields: dict[str, Any], section_id: str | None) -> str:
        if not fields and section_id is None:
            return error_json("Provide at least one field to update.")
        moved = True
        if section_id is not None:
            safe_section = safe_path_segment(section_id, "section_id")
            moved = bool(self.api.move_task(task_id=safe_id, section_id=safe_section))
        if fields:
            updated = self.api.update_task(task_id=safe_id, **fields)
            return json_dumps({"success": moved, "task": _serialize_item(updated)})
        return json_dumps({"success": moved, "task_id": safe_id})

    def todoist_write(
        self,
        action: _WriteAction,
        task_id: str | None = None,
        content: str | None = None,
        description: str | None = None,
        project_id: str | None = None,
        section_id: str | None = None,
        priority: int | None = None,
        labels: list[str] | None = None,
        due_string: str | None = None,
        due_date: str | None = None,
        due_datetime: str | None = None,
        due_lang: str | None = None,
        assignee_id: str | None = None,
    ) -> str:
        """Create, update, complete, or delete Todoist tasks.

        Actions:
        - `create_task`: Create a task (`content`; optional `description`, `project_id`, `section_id`, `priority`, `labels`, `due_string`, `due_date`, `due_datetime`, `due_lang`, `assignee_id`).
        - `update_task`: Update `task_id` fields or move to `section_id`.
        - `close_task`: Mark `task_id` completed.
        - `delete_task`: Permanently delete `task_id`.
        """
        try:
            if action == "create_task":
                fields = _optional_task_fields(
                    content=require_non_empty(content, "content"),
                    description=description,
                    project_id=project_id,
                    section_id=section_id,
                    priority=priority,
                    labels=labels,
                    due_string=due_string,
                    due_date=due_date,
                    due_datetime=due_datetime,
                    due_lang=due_lang,
                    assignee_id=assignee_id,
                )
                return json_dumps(_serialize_item(self.api.add_task(**fields)))

            safe_id = safe_path_segment(require_non_empty(task_id, "task_id"), "task_id")
            if action == "close_task":
                return json_dumps({"success": bool(self.api.complete_task(safe_id))})
            if action == "delete_task":
                return json_dumps({"success": bool(self.api.delete_task(safe_id))})

            fields = _optional_task_fields(
                content=content,
                description=description,
                priority=priority,
                labels=labels,
                due_string=due_string,
                due_date=due_date,
                due_datetime=due_datetime,
                due_lang=due_lang,
                assignee_id=assignee_id,
            )
            return self._update_task(safe_id, fields, section_id)
        except Exception as exc:
            return error_json(str(exc))


@register_tool_with_metadata(
    name="todoist",
    file_access=ToolFileAccess.NONE,
    display_name="Todoist",
    description="Task management with Todoist - create, update, delete, and organize tasks and projects",
    category=ToolCategory.PRODUCTIVITY,
    status=ToolStatus.REQUIRES_CONFIG,
    setup_type=SetupType.API_KEY,
    icon="SiTodoist",
    icon_color="text-red-500",
    config_fields=[
        ConfigField(
            name="api_token",
            env=("TODOIST_API_TOKEN",),
            label="API Token",
            type="password",
            required=False,
            default=None,
        ),
    ],
    dependencies=["todoist-api-python"],
    docs_url="https://docs.mindroom.chat/tools/project-management/#todoist",
    function_names=("todoist_read", "todoist_write"),
    action_scoped_grants=("todoist_read", "todoist_write"),
)
def todoist_tools() -> type[Toolkit]:
    """Return Todoist tools for task management."""
    return _TodoistTools

"""Microsoft To Do tools that act as the requester through Microsoft Graph."""

from __future__ import annotations

import html
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING, Any, Literal, cast

from agno.tools import Toolkit

from mindroom.config.main import Config  # noqa: TC001  # resolved by tool contract introspection
from mindroom.credentials.manager import CredentialsManager  # noqa: TC001  # resolved by tool contract introspection
from mindroom.custom_tools.microsoft_graph import (
    MicrosoftGraphError,
    graph_error_payload,
    graph_session,
    graph_tool_result,
    html_to_text,
    next_page_token,
    page_path,
    path_segment,
)
from mindroom.oauth.microsoft import microsoft_todo_oauth_provider

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from mindroom.constants import RuntimePaths
    from mindroom.tool_system.worker_routing import ResolvedWorkerTarget

_WRITE_FUNCTIONS = frozenset(
    {"microsoft_todo_create_task", "microsoft_todo_update_task", "microsoft_todo_delete_task"},
)
# A listing returns up to 50 tasks, so each task's notes stay short.
_MAX_NOTES_CHARACTERS = 2_000
_MAX_TASKS = 50
_MAX_LISTS = 100
_TOOL_NAME = "microsoft_todo"
_OPEN_TASKS_FILTER = "status ne 'completed'"
_IMPORTANCE = frozenset({"low", "normal", "high"})
_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")


@dataclass(frozen=True)
class _Request:
    """One validated Graph request; every To Do function sends exactly one."""

    method: str
    path: str
    params: dict[str, str] | None = None
    body: dict[str, object] | None = None


def _list_path(list_id: object) -> str:
    return f"/me/todo/lists/{path_segment(list_id, 'list_id')}"


def _task_path(list_id: object, task_id: object) -> str:
    return f"{_list_path(list_id)}/tasks/{path_segment(task_id, 'task_id')}"


def _limit(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= _MAX_TASKS:
        raise MicrosoftGraphError(code="invalid_argument", message=f"limit must be an integer from 1 to {_MAX_TASKS}.")
    return value


def _flag(value: object, field_name: str) -> bool:
    if not isinstance(value, bool):
        raise MicrosoftGraphError(code="invalid_argument", message=f"{field_name} must be true or false.")
    return value


def _title(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise MicrosoftGraphError(code="invalid_argument", message="title must not be empty.")
    return value


def _due(value: object) -> dict[str, str]:
    """Return Graph's due date for a YYYY-MM-DD calendar date, as midnight UTC."""
    try:
        day = date.fromisoformat(value) if isinstance(value, str) and _DATE.fullmatch(value) else None
    except ValueError:
        day = None
    if day is None:
        raise MicrosoftGraphError(
            code="invalid_argument",
            message="due_date must be a calendar date in YYYY-MM-DD format.",
        )
    return {"dateTime": f"{day.isoformat()}T00:00:00", "timeZone": "UTC"}


def _notes(value: object) -> dict[str, str]:
    """Return plain-text notes as the HTML body To Do shows, keeping their characters and line breaks."""
    if not isinstance(value, str):
        raise MicrosoftGraphError(code="invalid_argument", message="notes must be text.")
    return {"content": html.escape("\n".join(value.splitlines())).replace("\n", "<br>"), "contentType": "html"}


def _importance(value: object) -> str:
    if not isinstance(value, str) or value not in _IMPORTANCE:
        raise MicrosoftGraphError(code="invalid_argument", message="importance must be low, normal, or high.")
    return value


def _fields(value: object) -> Mapping[str, Any]:
    return cast("Mapping[str, Any]", value) if isinstance(value, dict) else {}


def _date_time(value: object) -> dict[str, object] | None:
    fields = _fields(value)
    if not fields.get("dateTime"):
        return None
    return {"date_time": fields["dateTime"], "time_zone": fields.get("timeZone")}


def _due_date(value: object) -> str | None:
    """Return a due date as YYYY-MM-DD, rounding Graph's UTC time to the nearest midnight.

    A due date set in another zone comes back as that zone's midnight in UTC, so October 22 in UTC+2
    reads `2026-10-21T22:00:00`.
    """
    moment = _fields(value).get("dateTime")
    if not isinstance(moment, str):
        return None
    try:
        parsed = datetime.fromisoformat(moment.split(".", 1)[0])
    except ValueError:
        return None
    return (parsed + timedelta(hours=12)).date().isoformat()


def _task(task: Mapping[str, Any]) -> dict[str, Any]:
    """Return the fields a caller needs to identify, read, and change one task."""
    body = _fields(task.get("body"))
    content = body.get("content")
    notes = content if isinstance(content, str) else ""
    if body.get("contentType") == "html":
        notes = html_to_text(notes)
    summary = {
        "id": task.get("id"),
        "title": task.get("title"),
        "status": task.get("status"),
        "importance": task.get("importance"),
        "due_date": _due_date(task.get("dueDateTime")),
        "reminder": _date_time(task.get("reminderDateTime")) if task.get("isReminderOn") is True else None,
        "created": task.get("createdDateTime"),
        "last_modified": task.get("lastModifiedDateTime"),
        "notes": notes[:_MAX_NOTES_CHARACTERS] or None,
        "categories": [category for category in task.get("categories") or [] if isinstance(category, str)],
    }
    if len(notes) > _MAX_NOTES_CHARACTERS:
        summary["notes_truncated"] = True
    return summary


def _list(task_list: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "id": task_list.get("id"),
        "name": task_list.get("displayName"),
        "is_owner": task_list.get("isOwner"),
        "is_shared": task_list.get("isShared"),
        "wellknown_list_name": task_list.get("wellknownListName"),
    }


def _records(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    return [record for record in payload.get("value") or [] if isinstance(record, dict)]


class MicrosoftTodoTools(Toolkit):
    """List, create, update, complete, and delete Microsoft To Do tasks as the requester."""

    def __init__(
        self,
        *,
        runtime_paths: RuntimePaths,
        credentials_manager: CredentialsManager | None = None,
        worker_target: ResolvedWorkerTarget | None = None,
        runtime_config: Config | None = None,
        write: bool = True,
    ) -> None:
        if credentials_manager is None:
            msg = "MicrosoftTodoTools requires an explicit credentials_manager"
            raise RuntimeError(msg)
        self._provider = microsoft_todo_oauth_provider()
        self._runtime_paths = runtime_paths
        self._credentials_manager = credentials_manager
        self._worker_target = worker_target
        self._config = runtime_config
        functions: dict[str, Callable[..., str]] = {
            "microsoft_todo_list_lists": self.microsoft_todo_list_lists,
            "microsoft_todo_list_tasks": self.microsoft_todo_list_tasks,
            "microsoft_todo_create_task": self.microsoft_todo_create_task,
            "microsoft_todo_update_task": self.microsoft_todo_update_task,
            "microsoft_todo_delete_task": self.microsoft_todo_delete_task,
        }
        super().__init__(
            name=_TOOL_NAME,
            tools=[function for name, function in functions.items() if write or name not in _WRITE_FUNCTIONS],
        )

    def _send(
        self,
        build: Callable[[], _Request],
        result: Callable[[dict[str, Any]], Mapping[str, object]],
    ) -> str:
        """Refuse malformed arguments before any request, then send the request and render its result."""
        try:
            request = build()
        except MicrosoftGraphError as exc:
            return graph_error_payload(_TOOL_NAME, exc)

        def run() -> Mapping[str, object]:
            with graph_session(
                self._provider,
                self._runtime_paths,
                self._credentials_manager,
                self._worker_target,
                self._config,
            ) as session:
                return result(session.json(request.method, request.path, params=request.params, json=request.body))

        return graph_tool_result(_TOOL_NAME, run)

    def microsoft_todo_list_lists(self, page_token: str | None = None) -> str:
        """List the requester's Microsoft To Do task lists.

        The list whose wellknown_list_name is defaultList is the requester's default Tasks list, and flaggedEmails
        holds flagged Outlook messages.

        Args:
            page_token: The next_page_token of the previous call, to continue the same listing.

        """

        def build() -> _Request:
            if page_token is not None:
                return _Request("GET", page_path(page_token, "/lists"))
            return _Request("GET", "/me/todo/lists", params={"$top": str(_MAX_LISTS)})

        return self._send(
            build,
            lambda payload: {
                "lists": [_list(task_list) for task_list in _records(payload)],
                "next_page_token": next_page_token(payload),
            },
        )

    def microsoft_todo_list_tasks(
        self,
        list_id: str,
        include_completed: bool = False,
        limit: int = 50,
        page_token: str | None = None,
    ) -> str:
        """List the tasks in one Microsoft To Do list; completed tasks are left out unless include_completed is true.

        Each task's notes are cut at 2,000 characters, and notes_truncated is true when they were cut.

        Args:
            list_id: The list's id from microsoft_todo_list_lists.
            include_completed: Also return completed tasks.
            limit: Maximum number of tasks to return, from 1 to 50.
            page_token: The next_page_token of the previous call, to continue the same listing.

        """

        def build() -> _Request:
            if page_token is not None:
                return _Request("GET", page_path(page_token, "/tasks"))
            params = {"$top": str(_limit(limit))}
            if not _flag(include_completed, "include_completed"):
                params["$filter"] = _OPEN_TASKS_FILTER
            return _Request("GET", f"{_list_path(list_id)}/tasks", params=params)

        return self._send(
            build,
            lambda payload: {
                "tasks": [_task(task) for task in _records(payload)],
                "next_page_token": next_page_token(payload),
            },
        )

    def microsoft_todo_create_task(
        self,
        list_id: str,
        title: str,
        due_date: str | None = None,
        notes: str | None = None,
        importance: Literal["low", "normal", "high"] = "normal",
    ) -> str:
        """Create a task in one Microsoft To Do list.

        Before calling, tell the requester the task's title and the list's name, because an approval prompt may
        show only IDs.

        Args:
            list_id: The list's id from microsoft_todo_list_lists.
            title: The task's title.
            due_date: The due date as YYYY-MM-DD.
            notes: Notes for the task, as plain text.
            importance: low, normal, or high.

        """

        def build() -> _Request:
            body: dict[str, object] = {"title": _title(title), "importance": _importance(importance)}
            if due_date is not None:
                body["dueDateTime"] = _due(due_date)
            if notes is not None:
                body["body"] = _notes(notes)
            return _Request("POST", f"{_list_path(list_id)}/tasks", body=body)

        return self._send(build, lambda payload: {"task": _task(payload)})

    def microsoft_todo_update_task(
        self,
        list_id: str,
        task_id: str,
        title: str | None = None,
        due_date: str | None = None,
        notes: str | None = None,
        importance: Literal["low", "normal", "high"] | None = None,
        completed: bool | None = None,
    ) -> str:
        """Change a Microsoft To Do task's title, due date, notes, importance, or completion; omitted fields stay unchanged.

        Before calling, tell the requester the task's title, its list, and what will change, because an approval
        prompt may show only IDs.

        Args:
            list_id: The list's id from microsoft_todo_list_lists.
            task_id: The task's id from microsoft_todo_list_tasks.
            title: The new title.
            due_date: The new due date as YYYY-MM-DD; an empty string removes the due date.
            notes: The complete new notes, which replace the current notes; an empty string removes them.
            importance: low, normal, or high.
            completed: True marks the task completed; False reopens it as not started.

        """

        def build() -> _Request:
            path = _task_path(list_id, task_id)
            body: dict[str, object] = {}
            if title is not None:
                body["title"] = _title(title)
            if due_date is not None:
                body["dueDateTime"] = _due(due_date) if due_date != "" else None
            if notes is not None:
                body["body"] = _notes(notes)
            if importance is not None:
                body["importance"] = _importance(importance)
            if completed is not None:
                body["status"] = "completed" if _flag(completed, "completed") else "notStarted"
            if not body:
                raise MicrosoftGraphError(
                    code="invalid_argument",
                    message="Nothing to change; pass title, due_date, notes, importance, or completed.",
                )
            return _Request("PATCH", path, body=body)

        return self._send(build, lambda payload: {"task": _task(payload)})

    def microsoft_todo_delete_task(self, list_id: str, task_id: str) -> str:
        """Delete one Microsoft To Do task.

        Before calling, tell the requester the task's title and its list, because an approval prompt may show
        only IDs.

        Args:
            list_id: The list's id from microsoft_todo_list_lists.
            task_id: The task's id from microsoft_todo_list_tasks.

        """
        return self._send(
            lambda: _Request("DELETE", _task_path(list_id, task_id)),
            lambda _payload: {"deleted": {"list_id": list_id, "task_id": task_id}},
        )

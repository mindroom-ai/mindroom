"""Google Tasks tools backed by MindRoom-scoped OAuth credentials."""

from __future__ import annotations

import json
import re
from datetime import date
from typing import TYPE_CHECKING, Any, cast

from googleapiclient.errors import HttpError

from mindroom.config.main import Config  # noqa: TC001  # resolved by tool contract introspection
from mindroom.credentials import CredentialsManager  # noqa: TC001  # resolved by tool contract introspection
from mindroom.custom_tools.google_service import GoogleApiToolkit, google_http_error_result
from mindroom.oauth.google_tasks import google_tasks_oauth_provider

if TYPE_CHECKING:
    from mindroom.constants import RuntimePaths
    from mindroom.tool_system.worker_routing import ResolvedWorkerTarget

_SERVICE_NAME = "Google Tasks"
_DEFAULT_TASK_LIST_ID = "@default"
_MAX_TASK_LISTS = 1000
_MAX_TASKS_PAGE_SIZE = 100
_DUE_DATE_PATTERN = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
_INVALID_DUE_ERROR = "Google Tasks due must be a date in YYYY-MM-DD format"
_EMPTY_TITLE_ERROR = "Google Tasks title must not be empty"


class _InvalidTaskInputError(ValueError):
    """Tool input rejected before calling Google Tasks."""


def _error_result(message: str) -> str:
    return json.dumps({"error": message})


def _due_timestamp(due: str) -> str:
    """Return Google's midnight-UTC due timestamp for a YYYY-MM-DD calendar date."""
    if not _DUE_DATE_PATTERN.fullmatch(due):
        raise _InvalidTaskInputError(_INVALID_DUE_ERROR)
    try:
        due_date = date.fromisoformat(due)
    except ValueError as exc:
        raise _InvalidTaskInputError(_INVALID_DUE_ERROR) from exc
    return f"{due_date.isoformat()}T00:00:00.000Z"


def _task_patch_body(
    title: str | None,
    notes: str | None,
    due: str | None,
    completed: bool | None,
) -> dict[str, object]:
    """Return a patch body holding only the supplied fields, with null clearing a field."""
    body: dict[str, object] = {}
    if title is not None:
        if not title.strip():
            raise _InvalidTaskInputError(_EMPTY_TITLE_ERROR)
        body["title"] = title
    if notes is not None:
        body["notes"] = notes
    if due is not None:
        body["due"] = _due_timestamp(due) if due else None
    if completed is not None:
        body.update({"status": "completed"} if completed else {"status": "needsAction", "completed": None})
    if not body:
        msg = "Google Tasks update requires at least one field to change"
        raise _InvalidTaskInputError(msg)
    return body


class GoogleTasksTools(GoogleApiToolkit):
    """List, create, update, complete, and delete Google Tasks with scoped Google credentials."""

    _oauth_provider = google_tasks_oauth_provider()
    _oauth_tool_name = "google_tasks"
    _google_api_name = "tasks"
    _google_api_version = "v1"

    def __init__(
        self,
        *,
        runtime_paths: RuntimePaths,
        credentials_manager: CredentialsManager | None = None,
        worker_target: ResolvedWorkerTarget | None = None,
        runtime_config: Config | None = None,
        read_tasks: bool = True,
        manage_tasks: bool = True,
        **kwargs: Any,  # noqa: ANN401
    ) -> None:
        tools = []
        if read_tasks:
            tools.extend([self.google_tasks_list_task_lists, self.google_tasks_list_tasks])
        if manage_tasks:
            tools.extend(
                [
                    self.google_tasks_create_task,
                    self.google_tasks_update_task,
                    self.google_tasks_delete_task,
                ],
            )
        super().__init__(
            name="google_tasks",
            tools=tools,
            runtime_paths=runtime_paths,
            credentials_manager=credentials_manager,
            worker_target=worker_target,
            runtime_config=runtime_config,
            **kwargs,
        )

    def google_tasks_list_task_lists(self) -> str:
        """List the connected user's Google Tasks lists.

        Returns:
            JSON containing up to 1000 task lists with their IDs and titles.

        """
        try:
            response = cast(
                "dict[str, object]",
                self._google_api_service().tasklists().list(maxResults=_MAX_TASK_LISTS).execute(),
            )
        except HttpError as exc:
            return google_http_error_result(_SERVICE_NAME, "list_task_lists", exc)
        return json.dumps({"taskLists": response.get("items", [])})

    def google_tasks_list_tasks(
        self,
        task_list_id: str = _DEFAULT_TASK_LIST_ID,
        show_completed: bool = False,
        max_results: int = _MAX_TASKS_PAGE_SIZE,
        page_token: str | None = None,
    ) -> str:
        """List tasks in one Google Tasks list, including subtasks and tasks assigned to the user.

        Args:
            task_list_id: Task list ID from google_tasks_list_task_lists; "@default" is the user's default list.
            show_completed: Whether to include completed tasks, including tasks completed in Google's own apps.
            max_results: Maximum number of tasks to return, from 1 to 100.
            page_token: nextPageToken from a previous call, to fetch the following page.

        Returns:
            JSON containing the tasks and, when more tasks remain, a nextPageToken.

        """
        if not 1 <= max_results <= _MAX_TASKS_PAGE_SIZE:
            return _error_result(f"Google Tasks max_results must be between 1 and {_MAX_TASKS_PAGE_SIZE}")
        # Tasks completed in Google's own apps are hidden, so showCompleted alone would miss them.
        params: dict[str, object] = {
            "tasklist": task_list_id,
            "maxResults": max_results,
            "showCompleted": show_completed,
            "showHidden": show_completed,
            "showAssigned": True,
        }
        if page_token:
            params["pageToken"] = page_token
        try:
            response = cast("dict[str, object]", self._google_api_service().tasks().list(**params).execute())
        except HttpError as exc:
            return google_http_error_result(_SERVICE_NAME, "list_tasks", exc)
        result: dict[str, object] = {"taskListId": task_list_id, "tasks": response.get("items", [])}
        if response.get("nextPageToken"):
            result["nextPageToken"] = response["nextPageToken"]
        return json.dumps(result)

    def google_tasks_create_task(
        self,
        title: str,
        notes: str | None = None,
        due: str | None = None,
        task_list_id: str = _DEFAULT_TASK_LIST_ID,
        parent_task_id: str | None = None,
    ) -> str:
        """Create a task, optionally as a subtask of another task.

        Args:
            title: Title of the task.
            notes: Optional task description.
            due: Optional due date in YYYY-MM-DD format; Google Tasks stores dates without times.
            task_list_id: Task list ID from google_tasks_list_task_lists; "@default" is the user's default list.
            parent_task_id: Optional ID of the task this new task becomes a subtask of.

        Returns:
            JSON containing the created task.

        """
        if not title.strip():
            return _error_result(_EMPTY_TITLE_ERROR)
        body: dict[str, object] = {"title": title}
        if notes:
            body["notes"] = notes
        if due:
            try:
                body["due"] = _due_timestamp(due)
            except _InvalidTaskInputError as exc:
                return _error_result(str(exc))
        params: dict[str, object] = {"tasklist": task_list_id, "body": body}
        if parent_task_id:
            params["parent"] = parent_task_id
        try:
            task = self._google_api_service().tasks().insert(**params).execute()
        except HttpError as exc:
            return google_http_error_result(_SERVICE_NAME, "create_task", exc)
        return json.dumps({"task": task})

    def google_tasks_update_task(
        self,
        task_id: str,
        task_list_id: str = _DEFAULT_TASK_LIST_ID,
        title: str | None = None,
        notes: str | None = None,
        due: str | None = None,
        completed: bool | None = None,
    ) -> str:
        """Change a task's title, notes, due date, or completion state; omitted fields stay unchanged.

        Args:
            task_id: ID of the task to update.
            task_list_id: ID of the task list containing the task; "@default" is the user's default list.
            title: New task title.
            notes: New task description; an empty string removes it.
            due: New due date in YYYY-MM-DD format; an empty string removes it.
            completed: True marks the task completed, False reopens it.

        Returns:
            JSON containing the updated task.

        """
        if not task_id.strip():
            return _error_result("Google Tasks task_id must not be empty")
        try:
            body = _task_patch_body(title, notes, due, completed)
        except _InvalidTaskInputError as exc:
            return _error_result(str(exc))
        try:
            task = self._google_api_service().tasks().patch(tasklist=task_list_id, task=task_id, body=body).execute()
        except HttpError as exc:
            return google_http_error_result(_SERVICE_NAME, "update_task", exc)
        return json.dumps({"task": task})

    def google_tasks_delete_task(self, task_id: str, task_list_id: str = _DEFAULT_TASK_LIST_ID) -> str:
        """Delete a task; deleting a task assigned from Google Docs or Chat also deletes its original assignment.

        Args:
            task_id: ID of the task to delete.
            task_list_id: ID of the task list containing the task; "@default" is the user's default list.

        Returns:
            JSON confirming the deletion.

        """
        if not task_id.strip():
            return _error_result("Google Tasks task_id must not be empty")
        try:
            self._google_api_service().tasks().delete(tasklist=task_list_id, task=task_id).execute()
        except HttpError as exc:
            return google_http_error_result(_SERVICE_NAME, "delete_task", exc)
        return json.dumps({"deleted": True, "taskListId": task_list_id, "taskId": task_id})

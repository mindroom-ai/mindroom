"""Google Tasks tools backed by MindRoom-scoped OAuth credentials."""

from __future__ import annotations

import json
import re
from datetime import date
from typing import TYPE_CHECKING, Any, Literal, cast

from googleapiclient.errors import HttpError

from mindroom.config.legacy_fields import RETIRED_CONSOLIDATED_FUNCTIONS
from mindroom.config.main import Config  # noqa: TC001  # resolved by tool contract introspection
from mindroom.credentials.manager import CredentialsManager  # noqa: TC001  # resolved by tool contract introspection
from mindroom.custom_tools.google_service import GoogleApiToolkit, filter_google_tools, google_http_error_result
from mindroom.oauth.google import google_oauth_provider

if TYPE_CHECKING:
    from collections.abc import Callable

    from mindroom.constants import RuntimePaths
    from mindroom.tool_system.worker_routing import ResolvedWorkerTarget

_SERVICE_NAME = "Google Tasks"
_DEFAULT_TASK_LIST_ID = "@default"
_MAX_TASK_LISTS = 1000
_MAX_TASKS_PAGE_SIZE = 100
_DUE_DATE_PATTERN = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
_INVALID_DUE_ERROR = "Google Tasks due must be a date in YYYY-MM-DD format"
_EMPTY_TITLE_ERROR = "Google Tasks title must not be empty"

_ReadAction = Literal["list_task_lists", "list_tasks"]
_WriteAction = Literal["create", "update", "delete"]


class _InvalidTaskInputError(ValueError):
    """Tool input rejected before calling Google Tasks."""


def _error_result(message: str) -> str:
    return json.dumps({"error": message})


def _page_result(result: dict[str, object], response: dict[str, object]) -> str:
    """Return one page of results, adding Google's nextPageToken when more pages remain."""
    if response.get("nextPageToken"):
        result["nextPageToken"] = response["nextPageToken"]
    return json.dumps(result)


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

    _oauth_provider = google_oauth_provider("google_tasks")
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
        include_tools = kwargs.pop("include_tools", None)
        exclude_tools = kwargs.pop("exclude_tools", None)
        tools: list[Callable[..., str]] = []
        if read_tasks:
            tools.append(self.google_tasks_read)
        if manage_tasks:
            tools.append(self.google_tasks_write)
        tools = filter_google_tools(
            tools,
            include_tools,
            exclude_tools,
            retired_tools=RETIRED_CONSOLIDATED_FUNCTIONS["google_tasks"],
            known_tools=("google_tasks_read", "google_tasks_write"),
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

    def _list_task_lists(self, page_token: str | None) -> str:
        params: dict[str, object] = {"maxResults": _MAX_TASK_LISTS}
        if page_token:
            params["pageToken"] = page_token
        try:
            response = cast("dict[str, object]", self._google_api_service().tasklists().list(**params).execute())
        except HttpError as exc:
            return google_http_error_result(_SERVICE_NAME, "list_task_lists", exc)
        return _page_result({"taskLists": response.get("items", [])}, response)

    def _list_tasks(
        self,
        task_list_id: str,
        show_completed: bool,
        max_results: int,
        page_token: str | None,
    ) -> str:
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
        return _page_result({"taskListId": task_list_id, "tasks": response.get("items", [])}, response)

    def google_tasks_read(
        self,
        action: _ReadAction,
        task_list_id: str = _DEFAULT_TASK_LIST_ID,
        show_completed: bool = False,
        max_results: int = _MAX_TASKS_PAGE_SIZE,
        page_token: str | None = None,
    ) -> str:
        """List Google Tasks lists or tasks (returns `nextPageToken` when more pages remain).

        - `list_task_lists`: list up to 1000 task lists with IDs and titles (optional `page_token`)
        - `list_tasks`: list up to `max_results` (1-100) tasks in `task_list_id` (default `@default`), including subtasks and Docs/Chat-assigned tasks; set `show_completed=True` to include completed/hidden tasks (optional `page_token`)
        """
        if action == "list_task_lists":
            return self._list_task_lists(page_token)
        if action == "list_tasks":
            return self._list_tasks(task_list_id, show_completed, max_results, page_token)
        return _error_result(f"Unsupported Google Tasks read action {action!r}")

    def _create_task(
        self,
        title: str | None,
        notes: str | None,
        due: str | None,
        task_list_id: str,
        parent_task_id: str | None,
    ) -> str:
        if not title or not title.strip():
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

    def _update_task(
        self,
        task_id: str | None,
        task_list_id: str,
        title: str | None,
        notes: str | None,
        due: str | None,
        completed: bool | None,
    ) -> str:
        if not task_id or not task_id.strip():
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

    def _delete_task(self, task_id: str | None, task_list_id: str) -> str:
        if not task_id or not task_id.strip():
            return _error_result("Google Tasks task_id must not be empty")
        try:
            self._google_api_service().tasks().delete(tasklist=task_list_id, task=task_id).execute()
        except HttpError as exc:
            return google_http_error_result(_SERVICE_NAME, "delete_task", exc)
        return json.dumps({"deleted": True, "taskListId": task_list_id, "taskId": task_id})

    def google_tasks_write(
        self,
        action: _WriteAction,
        task_list_id: str = _DEFAULT_TASK_LIST_ID,
        task_id: str | None = None,
        title: str | None = None,
        notes: str | None = None,
        due: str | None = None,
        completed: bool | None = None,
        parent_task_id: str | None = None,
    ) -> str:
        """Create, update, complete, or delete Google Tasks (`task_list_id` defaults to `@default`).

        - `create`: create task with `title` (optional `notes`, `due` YYYY-MM-DD, `parent_task_id` for subtask)
        - `update`: patch `task_id` with supplied fields (`title`, `notes`, `due` YYYY-MM-DD, `completed`); pass `""` for `notes` or `due` to clear
        - `delete`: delete `task_id` (deleting a Docs/Chat-assigned task also deletes the original assignment)
        """
        if action == "create":
            return self._create_task(title, notes, due, task_list_id, parent_task_id)
        if action == "update":
            return self._update_task(task_id, task_list_id, title, notes, due, completed)
        if action == "delete":
            return self._delete_task(task_id, task_list_id)
        return _error_result(f"Unsupported Google Tasks write action {action!r}")

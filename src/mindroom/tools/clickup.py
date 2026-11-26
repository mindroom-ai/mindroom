"""ClickUp project management toolkit."""

from __future__ import annotations

from typing import Any, Literal

import httpx

from mindroom.tool_system.authoring import Toolkit
from mindroom.tool_system.declarations import ConfigField, SetupType, ToolCategory, ToolFileAccess, ToolStatus
from mindroom.tool_system.registration import register_tool_with_metadata
from mindroom.tools.native_helpers import error_json, json_dumps, require_non_empty, safe_path_segment

_ReadAction = Literal["list_spaces", "list_lists", "list_tasks", "get_task"]
_WriteAction = Literal["create_task", "update_task", "delete_task"]
_CLICKUP_BASE_URL = "https://api.clickup.com/api/v2"


class _ClickUpTools(Toolkit):
    """Manage ClickUp spaces, lists, and tasks via the ClickUp v2 REST API."""

    def __init__(
        self,
        api_key: str | None = None,
        master_space_id: str | None = None,
        timeout: int = 30,
    ) -> None:
        self.api_key = require_non_empty(api_key, "ClickUp API key")
        self.master_space_id = safe_path_segment(
            require_non_empty(master_space_id, "ClickUp master_space_id"),
            "master_space_id",
        )
        self.timeout = timeout
        self.base_url = _CLICKUP_BASE_URL
        self.headers = {"Authorization": self.api_key}
        super().__init__(name="clickup", tools=[self.clickup_read, self.clickup_write])

    def _request(
        self,
        method: str,
        endpoint: str,
        *,
        params: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        url = f"{self.base_url}/{endpoint.lstrip('/')}"
        try:
            response = httpx.request(
                method=method,
                url=url,
                headers=self.headers,
                params=params,
                json=data,
                timeout=self.timeout,
            )
            response.raise_for_status()
            if not response.content:
                return {}
            payload = response.json()
            return payload if isinstance(payload, dict) else {"data": payload}
        except (httpx.HTTPError, ValueError) as exc:
            return {"error": str(exc)}

    @staticmethod
    def _find_by_name(items: list[dict[str, Any]], name: str) -> dict[str, Any] | None:
        if not name:
            return items[0] if items else None
        needle = name.lower()
        for item in items:
            if str(item.get("name", "")).lower() == needle:
                return item
        return next((item for item in items if needle in str(item.get("name", "")).lower()), None)

    def _get_space(self, space_name: str) -> dict[str, Any]:
        spaces = self._request("GET", f"team/{self.master_space_id}/space")
        if "error" in spaces:
            return spaces
        spaces_list = spaces.get("spaces", [])
        if not isinstance(spaces_list, list) or not spaces_list:
            return {"error": "No spaces found"}
        space = self._find_by_name(spaces_list, space_name)
        return space if space is not None else {"error": f"Space '{space_name}' not found"}

    def _list_space_tasks(self, space_name: str) -> dict[str, Any]:
        space = self._get_space(space_name)
        if "error" in space:
            return space
        space_id = safe_path_segment(str(space.get("id", "")), "space_id")
        lists_resp = self._request("GET", f"space/{space_id}/list")
        if "error" in lists_resp:
            return lists_resp
        lists_data = lists_resp.get("lists", [])
        if not isinstance(lists_data, list) or not lists_data:
            return {"error": f"No lists found in space '{space_name}'"}
        all_tasks: list[dict[str, Any]] = []
        for list_info in lists_data:
            if not isinstance(list_info, dict) or not list_info.get("id"):
                continue
            list_id = safe_path_segment(str(list_info["id"]), "list_id")
            tasks_resp = self._request("GET", f"list/{list_id}/task")
            if "error" in tasks_resp:
                continue
            for task in tasks_resp.get("tasks", []):
                if isinstance(task, dict):
                    task["list_name"] = list_info.get("name")
                    all_tasks.append(task)
        return {"tasks": all_tasks}

    def clickup_read(
        self,
        action: _ReadAction,
        space_name: str | None = None,
        task_id: str | None = None,
    ) -> str:
        """Read ClickUp spaces, lists, and tasks.

        Actions:
        - `list_spaces`: List all spaces in the configured workspace.
        - `list_lists`: List lists in `space_name`.
        - `list_tasks`: List all tasks across lists in `space_name`.
        - `get_task`: Fetch a single task by `task_id`.
        """
        try:
            if action == "list_spaces":
                return json_dumps(self._request("GET", f"team/{self.master_space_id}/space"))
            if action == "get_task":
                safe_task = safe_path_segment(require_non_empty(task_id, "task_id"), "task_id")
                return json_dumps(self._request("GET", f"task/{safe_task}"))
            target_space = require_non_empty(space_name, "space_name")
            if action == "list_lists":
                space = self._get_space(target_space)
                if "error" in space:
                    return json_dumps(space)
                space_id = safe_path_segment(str(space.get("id", "")), "space_id")
                return json_dumps(self._request("GET", f"space/{space_id}/list"))
            return json_dumps(self._list_space_tasks(target_space))
        except ValueError as exc:
            return error_json(str(exc))

    def _create_task(
        self,
        space_name: str | None,
        list_name: str | None,
        task_name: str | None,
        task_description: str | None,
    ) -> dict[str, Any]:
        target_space = require_non_empty(space_name, "space_name")
        clean_name = require_non_empty(task_name, "task_name")
        space = self._get_space(target_space)
        if "error" in space:
            return space
        space_id = safe_path_segment(str(space.get("id", "")), "space_id")
        lists_resp = self._request("GET", f"space/{space_id}/list")
        if "error" in lists_resp:
            return lists_resp
        lists_data = lists_resp.get("lists", [])
        if not isinstance(lists_data, list) or not lists_data:
            return {"error": f"No lists found in space '{target_space}'"}
        target_list = self._find_by_name(lists_data, list_name or "")
        if target_list is None:
            return {"error": f"List '{list_name}' not found in space '{target_space}'"}
        list_id = safe_path_segment(str(target_list.get("id", "")), "list_id")
        return self._request(
            "POST",
            f"list/{list_id}/task",
            data={"name": clean_name, "description": task_description or ""},
        )

    def clickup_write(
        self,
        action: _WriteAction,
        space_name: str | None = None,
        list_name: str | None = None,
        task_id: str | None = None,
        task_name: str | None = None,
        task_description: str | None = None,
        updates: dict[str, Any] | None = None,
    ) -> str:
        """Create, update, or delete ClickUp tasks.

        Actions:
        - `create_task`: Create a task in `space_name` (`task_name`, optional `task_description`, optional `list_name`).
        - `update_task`: Update `task_id` (`updates` dict and/or `task_name`, `task_description`).
        - `delete_task`: Permanently delete `task_id`.
        """
        try:
            if action == "create_task":
                return json_dumps(self._create_task(space_name, list_name, task_name, task_description))
            safe_task = safe_path_segment(require_non_empty(task_id, "task_id"), "task_id")
            if action == "delete_task":
                result = self._request("DELETE", f"task/{safe_task}")
                if "error" not in result:
                    result = {"success": True, "message": f"Task {safe_task} deleted successfully"}
                return json_dumps(result)
            payload = dict(updates or {})
            if task_name is not None:
                payload.setdefault("name", task_name)
            if task_description is not None:
                payload.setdefault("description", task_description)
            if not payload:
                return error_json("Provide updates, task_name, or task_description.")
            return json_dumps(self._request("PUT", f"task/{safe_task}", data=payload))
        except ValueError as exc:
            return error_json(str(exc))


@register_tool_with_metadata(
    name="clickup",
    file_access=ToolFileAccess.NONE,
    display_name="ClickUp",
    description="Manage tasks, spaces, and lists in ClickUp project management",
    category=ToolCategory.PRODUCTIVITY,
    status=ToolStatus.REQUIRES_CONFIG,
    setup_type=SetupType.API_KEY,
    icon="SiClickup",
    icon_color="text-purple-600",
    config_fields=[
        ConfigField(
            name="api_key",
            env=("CLICKUP_API_KEY",),
            label="API Key",
            type="password",
            required=True,
            placeholder="ClickUp API key",
            description="API key from ClickUp",
        ),
        ConfigField(
            name="master_space_id",
            env=("MASTER_SPACE_ID",),
            label="Master Space ID",
            type="text",
            required=True,
            placeholder="ClickUp space ID",
            description="ID of the master space to work with",
        ),
        ConfigField(
            name="timeout",
            label="Timeout",
            type="number",
            required=False,
            default=30,
            description="Per-request HTTP timeout in seconds",
        ),
    ],
    dependencies=[],
    docs_url="https://docs.mindroom.chat/tools/project-management/#clickup",
    helper_text="Get your API key from [ClickUp Settings > Apps](https://app.clickup.com/settings/apps)",
    function_names=("clickup_read", "clickup_write"),
    action_scoped_grants=("clickup_read", "clickup_write"),
)
def clickup_tools() -> type[Toolkit]:
    """Return ClickUp tools for project management."""
    return _ClickUpTools

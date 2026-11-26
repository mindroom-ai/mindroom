"""Tests for the native Todoist toolkit."""

from __future__ import annotations

import json

import httpx
import pytest
from todoist_api_python.api import TodoistAPI

from mindroom.tools.todoist import todoist_tools


def _project(project_id: str) -> dict[str, object]:
    return {
        "id": project_id,
        "name": f"Project {project_id}",
        "description": "",
        "order": 1,
        "color": "charcoal",
        "is_collapsed": False,
        "is_shared": False,
        "is_favorite": False,
        "is_archived": False,
        "can_assign_tasks": False,
        "view_style": "list",
        "created_at": "2026-09-01T00:00:00.000000Z",
        "updated_at": "2026-09-01T00:00:00.000000Z",
    }


def _task(task_id: str, content: str = "Buy milk") -> dict[str, object]:
    return {
        "id": task_id,
        "user_id": "u1",
        "project_id": "p1",
        "section_id": None,
        "parent_id": None,
        "added_by_uid": "u1",
        "assigned_by_uid": None,
        "responsible_uid": None,
        "labels": ["home"],
        "deadline": None,
        "duration": None,
        "checked": False,
        "is_deleted": False,
        "added_at": "2026-09-01T00:00:00.000000Z",
        "completed_at": None,
        "completed_by_uid": None,
        "updated_at": "2026-09-01T00:00:00.000000Z",
        "due": None,
        "priority": 2,
        "child_order": 1,
        "content": content,
        "description": "",
        "note_count": 0,
        "day_order": -1,
        "is_collapsed": False,
    }


@pytest.mark.parametrize("project_pages", [[[]], [["first", "second"], ["third"]]])
def test_get_projects_flattens_sdk_pages(project_pages: list[list[str]]) -> None:
    """Project discovery flattens paginated SDK results into JSON-safe dicts."""
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        index = len(requests)
        requests.append(request)
        assert request.url.path.endswith("/projects")
        assert request.headers["authorization"] == "Bearer fake-todoist-token"
        assert request.url.params.get("cursor") == (None if index == 0 else f"page-{index}")
        return httpx.Response(
            200,
            json={
                "results": [_project(project_id) for project_id in project_pages[index]],
                "next_cursor": f"page-{index + 1}" if index + 1 < len(project_pages) else None,
            },
        )

    toolkit = todoist_tools()(api_token="fake-todoist-token")  # noqa: S106 - synthetic credential
    toolkit.api._client.close()
    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        toolkit.api = TodoistAPI("fake-todoist-token", client=client)
        projects = json.loads(toolkit.todoist_read(action="projects"))

    expected = [project_id for page in project_pages for project_id in page]
    assert [project["id"] for project in projects] == expected
    assert [project["name"] for project in projects] == [f"Project {project_id}" for project_id in expected]
    assert all(isinstance(project["created_at"], str) for project in projects)
    assert len(requests) == len(project_pages)


def test_todoist_tasks_crud_and_validation(monkeypatch: pytest.MonkeyPatch) -> None:
    """Task read/write actions serialize SDK responses, parse ISO dates, and reject traversal IDs."""
    monkeypatch.setenv("TODOIST_API_TOKEN", "host-env-secret")
    with pytest.raises(ValueError, match="Todoist API token"):
        todoist_tools()(api_token=None)

    routes: dict[tuple[str, str], httpx.Response] = {
        ("GET", "/tasks"): httpx.Response(200, json={"results": [_task("t1")], "next_cursor": None}),
        ("POST", "/tasks"): httpx.Response(200, json=_task("t2", "Ship Wave 8")),
        ("POST", "/tasks/t2/move"): httpx.Response(200, json=_task("t2", "Ship Wave 8")),
        ("POST", "/tasks/t2"): httpx.Response(200, json=_task("t2", "Updated")),
        ("POST", "/tasks/t2/close"): httpx.Response(204),
        ("DELETE", "/tasks/t2"): httpx.Response(204),
    }

    def respond(request: httpx.Request) -> httpx.Response:
        for (method, suffix), response in routes.items():
            if request.method == method and request.url.path.endswith(suffix):
                return response
        return httpx.Response(503, text="unavailable")

    toolkit = todoist_tools()(api_token="fake-todoist-token")  # noqa: S106 - synthetic credential
    toolkit.api._client.close()
    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        toolkit.api = TodoistAPI("fake-todoist-token", client=client)
        tasks = json.loads(toolkit.todoist_read(action="active_tasks"))
        assert [t["id"] for t in tasks] == ["t1"]

        created = json.loads(toolkit.todoist_write(action="create_task", content="Ship Wave 8", due_date="2026-12-01"))
        assert created["id"] == "t2"

        updated = json.loads(
            toolkit.todoist_write(action="update_task", task_id="t2", content="Updated", section_id="sec-1"),
        )
        assert updated["success"] is True
        assert updated["task"]["content"] == "Updated"

        assert json.loads(toolkit.todoist_write(action="close_task", task_id="t2")) == {"success": True}
        assert json.loads(toolkit.todoist_write(action="delete_task", task_id="t2")) == {"success": True}

        bad = json.loads(toolkit.todoist_read(action="get_task", task_id="../evil"))
        assert "Invalid task_id" in bad["error"]


def test_get_projects_preserves_provider_error_result() -> None:
    """Project fetch failures return a structured JSON error."""
    toolkit = todoist_tools()(api_token="fake-todoist-token")  # noqa: S106 - synthetic credential
    toolkit.api._client.close()
    with httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(503, text="unavailable"))) as client:
        toolkit.api = TodoistAPI("fake-todoist-token", client=client)
        result = json.loads(toolkit.todoist_read(action="projects"))
    assert "error" in result

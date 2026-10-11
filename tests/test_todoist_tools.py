"""Todoist project discovery follows the pinned SDK's page iterator."""

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


@pytest.mark.parametrize("project_pages", [[[]], [["first", "second"], ["third"]]])
def test_get_projects_flattens_sdk_pages(project_pages: list[list[str]]) -> None:
    """Registered discovery returns every project, preserving JSON-safe metadata."""
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
        entrypoint = toolkit.functions["get_projects"].entrypoint
        assert entrypoint is not None
        projects = json.loads(entrypoint())

    expected = [project_id for page in project_pages for project_id in page]
    assert isinstance(projects, list), projects
    assert [project["id"] for project in projects] == expected
    assert [project["name"] for project in projects] == [f"Project {project_id}" for project_id in expected]
    assert all(isinstance(project["created_at"], str) for project in projects)
    assert len(requests) == len(project_pages)


def test_get_projects_preserves_provider_error_result() -> None:
    """Project fetch failures keep the toolkit's structured error contract."""
    toolkit = todoist_tools()(api_token="fake-todoist-token")  # noqa: S106 - synthetic credential
    toolkit.api._client.close()
    with httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(503, text="unavailable"))) as client:
        toolkit.api = TodoistAPI("fake-todoist-token", client=client)
        result = json.loads(toolkit.get_projects())
    assert "error" in result

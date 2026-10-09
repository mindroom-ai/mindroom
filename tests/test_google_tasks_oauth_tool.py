"""Tests for the Google Tasks OAuth-backed tool."""

# ruff: noqa: D103

from __future__ import annotations

import json
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest
from google.oauth2.credentials import Credentials as GoogleOAuthCredentials
from googleapiclient.errors import HttpError

from mindroom import constants
from mindroom import tools as _mindroom_tools  # noqa: F401  # registers built-in tool metadata
from mindroom.config.main import Config
from mindroom.credentials import CredentialsManager
from mindroom.custom_tools.google_tasks import GoogleTasksTools
from mindroom.oauth.google import GOOGLE_IDENTITY_SCOPES
from mindroom.oauth.google_tasks import google_tasks_oauth_provider
from mindroom.tool_system.worker_routing import ToolExecutionIdentity, resolve_worker_target
from tests.oauth_test_utils import publish_oauth_credentials

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.tool_system.worker_routing import ResolvedWorkerTarget


def _valid_credentials() -> GoogleOAuthCredentials:
    return GoogleOAuthCredentials(
        token="valid-access-token",  # noqa: S106
        refresh_token="valid-refresh-token",  # noqa: S106
        token_uri="https://oauth2.googleapis.com/token",  # noqa: S106
        client_id="client-id",
        client_secret="client-secret",  # noqa: S106
        scopes=("scope",),
        expiry=datetime(2100, 1, 1, tzinfo=UTC),
    )


class _FakeTasksRequest:
    def __init__(self, response: object, error: Exception | None = None) -> None:
        self._response = response
        self._error = error

    def execute(self) -> object:
        if self._error is not None:
            raise self._error
        return self._response


class _FakeTaskListsResource:
    def __init__(self) -> None:
        self.list_calls: list[dict[str, object]] = []
        self.next_page_token: str | None = None

    def list(self, **kwargs: object) -> _FakeTasksRequest:
        self.list_calls.append(kwargs)
        response: dict[str, object] = {
            "kind": "tasks#taskLists",
            "items": [{"id": "list-1", "title": "My Tasks"}],
        }
        if self.next_page_token is not None:
            response["nextPageToken"] = self.next_page_token
        return _FakeTasksRequest(response)


class _FakeTasksResource:
    def __init__(self) -> None:
        self.list_calls: list[dict[str, object]] = []
        self.insert_calls: list[dict[str, object]] = []
        self.patch_calls: list[dict[str, object]] = []
        self.delete_calls: list[dict[str, object]] = []
        self.next_page_token: str | None = None

    def list(self, **kwargs: object) -> _FakeTasksRequest:
        self.list_calls.append(kwargs)
        response: dict[str, object] = {
            "kind": "tasks#tasks",
            "items": [{"id": "task-1", "title": "Buy milk", "status": "needsAction"}],
        }
        if self.next_page_token is not None:
            response["nextPageToken"] = self.next_page_token
        return _FakeTasksRequest(response)

    def insert(self, **kwargs: object) -> _FakeTasksRequest:
        self.insert_calls.append(kwargs)
        body = kwargs["body"]
        assert isinstance(body, dict)
        return _FakeTasksRequest({"id": "created-task", **body})

    def patch(self, **kwargs: object) -> _FakeTasksRequest:
        self.patch_calls.append(kwargs)
        body = kwargs["body"]
        assert isinstance(body, dict)
        return _FakeTasksRequest({"id": kwargs["task"], **body})

    def delete(self, **kwargs: object) -> _FakeTasksRequest:
        self.delete_calls.append(kwargs)
        return _FakeTasksRequest("")


class _FakeTasksService:
    def __init__(self) -> None:
        self.tasklists_resource = _FakeTaskListsResource()
        self.tasks_resource = _FakeTasksResource()

    def tasklists(self) -> _FakeTaskListsResource:
        return self.tasklists_resource

    def tasks(self) -> _FakeTasksResource:
        return self.tasks_resource


def _runtime_paths(tmp_path: Path, extra_env: dict[str, str] | None = None) -> constants.RuntimePaths:
    return constants.resolve_runtime_paths(
        storage_path=tmp_path / "mindroom_data",
        process_env={
            "MINDROOM_PUBLIC_URL": "https://mindroom.example.test",
            **(extra_env or {}),
        },
    )


def _worker_target() -> ResolvedWorkerTarget:
    identity = ToolExecutionIdentity(
        channel="matrix",
        agent_name="general",
        requester_id="@alice:example.org",
        room_id="!room:example.org",
        thread_id=None,
        resolved_thread_id=None,
        session_id=None,
    )
    return resolve_worker_target("user_agent", "general", execution_identity=identity)


def _connected_tool(tmp_path: Path) -> tuple[GoogleTasksTools, _FakeTasksService]:
    tool = GoogleTasksTools(
        runtime_paths=_runtime_paths(tmp_path),
        credentials_manager=CredentialsManager(tmp_path / "credentials"),
        worker_target=None,
        creds=_valid_credentials(),
    )
    service = _FakeTasksService()
    tool.service = service
    return tool, service


def test_google_tasks_missing_credentials_returns_scoped_connect_instruction(tmp_path: Path) -> None:
    tool = GoogleTasksTools(
        runtime_paths=_runtime_paths(tmp_path),
        credentials_manager=CredentialsManager(tmp_path / "credentials"),
        worker_target=_worker_target(),
    )

    result = json.loads(tool.google_tasks_list_tasks())

    assert result["oauth_connection_required"] is True
    assert result["provider"] == "google_tasks"
    assert "https://mindroom.example.test/api/oauth/google_tasks/authorize?connect_token=" in result["connect_url"]
    assert "@alice:example.org" not in result["connect_url"]


def test_google_tasks_tokens_stay_separate_from_dashboard_settings(tmp_path: Path) -> None:
    manager = CredentialsManager(tmp_path / "credentials")
    manager.save_credentials("google_tasks", {"manage_tasks": False, "_source": "ui"})
    publish_oauth_credentials(
        google_tasks_oauth_provider(),
        {"token": "access-token", "refresh_token": "refresh-token", "_source": "oauth"},
        credentials_manager=manager,
        worker_target=None,
    )
    tool = GoogleTasksTools(
        runtime_paths=_runtime_paths(tmp_path),
        credentials_manager=manager,
        worker_target=None,
    )

    token_data = tool._load_token_data()

    assert token_data is not None
    assert token_data["token"] == "access-token"  # noqa: S105
    assert "manage_tasks" not in token_data


def test_google_tasks_list_task_lists(tmp_path: Path) -> None:
    tool, service = _connected_tool(tmp_path)

    result = json.loads(tool.google_tasks_list_task_lists())

    assert service.tasklists_resource.list_calls == [{"maxResults": 1000}]
    assert result == {"taskLists": [{"id": "list-1", "title": "My Tasks"}]}


def test_google_tasks_list_task_lists_pages_beyond_first_response(tmp_path: Path) -> None:
    tool, service = _connected_tool(tmp_path)
    service.tasklists_resource.next_page_token = "lists-page-3"  # noqa: S105

    result = json.loads(tool.google_tasks_list_task_lists(page_token="lists-page-2"))  # noqa: S106

    assert service.tasklists_resource.list_calls == [{"maxResults": 1000, "pageToken": "lists-page-2"}]
    assert result["nextPageToken"] == "lists-page-3"


def test_google_tasks_list_tasks_defaults_to_open_tasks_in_default_list(tmp_path: Path) -> None:
    tool, service = _connected_tool(tmp_path)

    result = json.loads(tool.google_tasks_list_tasks())

    assert service.tasks_resource.list_calls == [
        {
            "tasklist": "@default",
            "maxResults": 100,
            "showCompleted": False,
            "showHidden": False,
            "showAssigned": True,
        },
    ]
    assert result == {
        "taskListId": "@default",
        "tasks": [{"id": "task-1", "title": "Buy milk", "status": "needsAction"}],
    }


def test_google_tasks_list_tasks_includes_tasks_completed_in_google_clients(tmp_path: Path) -> None:
    tool, service = _connected_tool(tmp_path)
    service.tasks_resource.next_page_token = "page-3"  # noqa: S105

    result = json.loads(
        tool.google_tasks_list_tasks(
            "list-1",
            show_completed=True,
            max_results=5,
            page_token="page-2",  # noqa: S106
        ),
    )

    assert service.tasks_resource.list_calls == [
        {
            "tasklist": "list-1",
            "maxResults": 5,
            "showCompleted": True,
            "showHidden": True,
            "showAssigned": True,
            "pageToken": "page-2",
        },
    ]
    assert result["nextPageToken"] == "page-3"


@pytest.mark.parametrize("max_results", [0, 101])
def test_google_tasks_list_tasks_rejects_out_of_range_page_sizes(tmp_path: Path, max_results: int) -> None:
    tool, service = _connected_tool(tmp_path)

    result = json.loads(tool.google_tasks_list_tasks(max_results=max_results))

    assert result == {"error": "Google Tasks max_results must be between 1 and 100"}
    assert service.tasks_resource.list_calls == []


def test_google_tasks_create_task_sends_due_date_as_google_timestamp(tmp_path: Path) -> None:
    tool, service = _connected_tool(tmp_path)

    result = json.loads(
        tool.google_tasks_create_task(
            "Buy milk",
            notes="Oat milk",
            due="2026-10-01",
            task_list_id="list-1",
            parent_task_id="parent-task",
        ),
    )

    assert service.tasks_resource.insert_calls == [
        {
            "tasklist": "list-1",
            "body": {"title": "Buy milk", "notes": "Oat milk", "due": "2026-10-01T00:00:00.000Z"},
            "parent": "parent-task",
        },
    ]
    assert result["task"]["id"] == "created-task"


def test_google_tasks_create_task_defaults_to_default_list(tmp_path: Path) -> None:
    tool, service = _connected_tool(tmp_path)

    tool.google_tasks_create_task("Buy milk")

    assert service.tasks_resource.insert_calls == [{"tasklist": "@default", "body": {"title": "Buy milk"}}]


def test_google_tasks_create_task_skips_empty_optional_fields(tmp_path: Path) -> None:
    tool, service = _connected_tool(tmp_path)

    tool.google_tasks_create_task("Buy milk", notes="", due="", parent_task_id="")

    assert service.tasks_resource.insert_calls == [{"tasklist": "@default", "body": {"title": "Buy milk"}}]


@pytest.mark.parametrize(
    ("title", "due", "error"),
    [
        ("  ", None, "Google Tasks title must not be empty"),
        ("Buy milk", "2026-10-01T09:00:00Z", "Google Tasks due must be a date in YYYY-MM-DD format"),
        ("Buy milk", "2026-02-30", "Google Tasks due must be a date in YYYY-MM-DD format"),
    ],
)
def test_google_tasks_create_task_rejects_invalid_input(
    tmp_path: Path,
    title: str,
    due: str | None,
    error: str,
) -> None:
    tool, service = _connected_tool(tmp_path)

    result = json.loads(tool.google_tasks_create_task(title, due=due))

    assert result == {"error": error}
    assert service.tasks_resource.insert_calls == []


def test_google_tasks_update_task_patches_only_supplied_fields(tmp_path: Path) -> None:
    tool, service = _connected_tool(tmp_path)

    result = json.loads(tool.google_tasks_update_task("task-1", task_list_id="list-1", title="Buy oat milk"))

    assert service.tasks_resource.patch_calls == [
        {"tasklist": "list-1", "task": "task-1", "body": {"title": "Buy oat milk"}},
    ]
    assert result["task"] == {"id": "task-1", "title": "Buy oat milk"}


def test_google_tasks_update_task_changes_several_fields_at_once(tmp_path: Path) -> None:
    tool, service = _connected_tool(tmp_path)

    tool.google_tasks_update_task(
        "task-1",
        title="Buy oat milk",
        notes="Two cartons",
        due="2026-10-02",
        completed=True,
    )

    assert service.tasks_resource.patch_calls == [
        {
            "tasklist": "@default",
            "task": "task-1",
            "body": {
                "title": "Buy oat milk",
                "notes": "Two cartons",
                "due": "2026-10-02T00:00:00.000Z",
                "status": "completed",
            },
        },
    ]


def test_google_tasks_update_task_completes_task(tmp_path: Path) -> None:
    tool, service = _connected_tool(tmp_path)

    tool.google_tasks_update_task("task-1", completed=True)

    assert service.tasks_resource.patch_calls == [
        {"tasklist": "@default", "task": "task-1", "body": {"status": "completed"}},
    ]


def test_google_tasks_update_task_reopens_task_and_clears_completion_time(tmp_path: Path) -> None:
    tool, service = _connected_tool(tmp_path)

    tool.google_tasks_update_task("task-1", completed=False)

    assert service.tasks_resource.patch_calls == [
        {"tasklist": "@default", "task": "task-1", "body": {"status": "needsAction", "completed": None}},
    ]


def test_google_tasks_update_task_can_clear_notes_and_due_date(tmp_path: Path) -> None:
    tool, service = _connected_tool(tmp_path)

    tool.google_tasks_update_task("task-1", notes="", due="")

    assert service.tasks_resource.patch_calls == [
        {"tasklist": "@default", "task": "task-1", "body": {"notes": "", "due": None}},
    ]


@pytest.mark.parametrize(
    ("kwargs", "error"),
    [
        ({"task_id": " "}, "Google Tasks task_id must not be empty"),
        ({"task_id": "task-1"}, "Google Tasks update requires at least one field to change"),
        ({"task_id": "task-1", "title": " "}, "Google Tasks title must not be empty"),
        ({"task_id": "task-1", "due": "tomorrow"}, "Google Tasks due must be a date in YYYY-MM-DD format"),
    ],
)
def test_google_tasks_update_task_rejects_invalid_input(
    tmp_path: Path,
    kwargs: dict[str, str],
    error: str,
) -> None:
    tool, service = _connected_tool(tmp_path)

    result = json.loads(tool.google_tasks_update_task(**kwargs))

    assert result == {"error": error}
    assert service.tasks_resource.patch_calls == []


def test_google_tasks_delete_task(tmp_path: Path) -> None:
    tool, service = _connected_tool(tmp_path)

    result = json.loads(tool.google_tasks_delete_task("task-1", task_list_id="list-1"))

    assert service.tasks_resource.delete_calls == [{"tasklist": "list-1", "task": "task-1"}]
    assert result == {"deleted": True, "taskListId": "list-1", "taskId": "task-1"}


def test_google_tasks_delete_task_rejects_empty_task_id(tmp_path: Path) -> None:
    tool, service = _connected_tool(tmp_path)

    result = json.loads(tool.google_tasks_delete_task(" "))

    assert result == {"error": "Google Tasks task_id must not be empty"}
    assert service.tasks_resource.delete_calls == []


@pytest.mark.parametrize("operation", ["list_task_lists", "list_tasks", "create", "update", "delete"])
def test_google_tasks_provider_failures_preserve_status_without_provider_text(
    tmp_path: Path,
    operation: str,
) -> None:
    error = HttpError(
        SimpleNamespace(status=404, reason="provider-controlled-reason"),
        b'{"error":{"message":"provider-controlled-tasks-secret"}}',
    )

    class Resource:
        @staticmethod
        def _request(**_kwargs: object) -> _FakeTasksRequest:
            return _FakeTasksRequest({}, error)

        list = insert = patch = delete = _request

    class Service:
        @staticmethod
        def tasklists() -> Resource:
            return Resource()

        @staticmethod
        def tasks() -> Resource:
            return Resource()

    tool, _service = _connected_tool(tmp_path)
    tool.service = Service()

    result = {
        "list_task_lists": tool.google_tasks_list_task_lists,
        "list_tasks": tool.google_tasks_list_tasks,
        "create": lambda: tool.google_tasks_create_task("Buy milk"),
        "update": lambda: tool.google_tasks_update_task("task-1", completed=True),
        "delete": lambda: tool.google_tasks_delete_task("task-1"),
    }[operation]()

    assert json.loads(result) == {"error": "Google Tasks request failed (HTTP 404)"}
    assert "provider-controlled" not in result


def test_google_tasks_config_flags_control_registered_functions(tmp_path: Path) -> None:
    read_only = GoogleTasksTools(
        runtime_paths=_runtime_paths(tmp_path),
        credentials_manager=CredentialsManager(tmp_path / "credentials"),
        worker_target=None,
        creds=_valid_credentials(),
        manage_tasks=False,
    )
    write_only = GoogleTasksTools(
        runtime_paths=_runtime_paths(tmp_path),
        credentials_manager=CredentialsManager(tmp_path / "credentials"),
        worker_target=None,
        creds=_valid_credentials(),
        read_tasks=False,
    )

    assert set(read_only.functions) == {"google_tasks_list_task_lists", "google_tasks_list_tasks"}
    assert set(write_only.functions) == {
        "google_tasks_create_task",
        "google_tasks_update_task",
        "google_tasks_delete_task",
    }


def test_google_tasks_agent_config_accepts_inline_operation_flags() -> None:
    config = Config(
        agents={
            "planner": {
                "display_name": "Planner",
                "tools": [{"google_tasks": {"read_tasks": True, "manage_tasks": False}}],
            },
        },
    )

    tasks_config = next(
        entry for entry in config.resolve_entity("planner").tool_configs if entry.name == "google_tasks"
    )

    assert tasks_config.tool_config_overrides == {"read_tasks": True, "manage_tasks": False}


def test_google_tasks_provider_requests_only_tasks_and_identity_scopes() -> None:
    provider = google_tasks_oauth_provider()

    assert provider.scopes == (*GOOGLE_IDENTITY_SCOPES, "https://www.googleapis.com/auth/tasks")
    assert "include_granted_scopes" not in provider.extra_auth_params


def test_google_tasks_service_account_env_uses_primary_runtime_auth(tmp_path: Path) -> None:
    service_account_path = tmp_path / "service-account.json"
    tool = GoogleTasksTools(
        runtime_paths=_runtime_paths(
            tmp_path,
            {"GOOGLE_SERVICE_ACCOUNT_FILE": str(service_account_path)},
        ),
        credentials_manager=CredentialsManager(tmp_path / "credentials"),
        worker_target=None,
    )

    assert tool.service_account_path == str(service_account_path)
    assert tool._should_fallback_to_original_auth() is True

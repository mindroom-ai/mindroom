"""Tests for the Microsoft To Do toolkit."""
# ruff: noqa: D103

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import httpx
import pytest
from agno.tools.function import Function

from mindroom.custom_tools.microsoft_todo import MicrosoftTodoTools
from mindroom.oauth.microsoft import microsoft_todo_oauth_provider
from mindroom.tool_system.catalog import TOOL_METADATA
from mindroom.tool_system.declarations import SetupType, ToolCategory, ToolFileAccess
from tests.microsoft_graph_test_support import (
    FakeGraph,
    publish_grant,
    runtime_paths,
    save_client_config,
    worker_target,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

LIST = "AAMkADIyAAAAABrJAAA="
TASK = "AQMkADAwATM0MDAAMS0yMDkyLWVjMzYtMDACLTAwCgBGAAAD"
TASKS_PATH = f"/v1.0/me/todo/lists/{LIST}/tasks"
TASK_PATH = f"{TASKS_PATH}/{TASK}"
NOTES_CAP = 2_000
WRITE_FUNCTIONS = ("microsoft_todo_create_task", "microsoft_todo_update_task", "microsoft_todo_delete_task")


def _task(**overrides: object) -> dict[str, object]:
    return {
        "@odata.etag": 'W/"xzyPKP0BiUGgld+lMKXwbQAAgdhkVw=="',
        "id": TASK,
        "title": "Send the quarterly report",
        "status": "notStarted",
        "importance": "high",
        "isReminderOn": True,
        "reminderDateTime": {"dateTime": "2026-10-21T08:00:00.0000000", "timeZone": "UTC"},
        "dueDateTime": {"dateTime": "2026-10-22T00:00:00.0000000", "timeZone": "UTC"},
        "createdDateTime": "2026-10-20T11:15:19.9359889Z",
        "lastModifiedDateTime": "2026-10-20T11:15:20.0614375Z",
        "categories": ["Important"],
        "body": {"content": "Attach the figures.", "contentType": "text"},
        **overrides,
    }


def _ok(payload: dict[str, object] | None = None, status: int = 200) -> Callable[[httpx.Request], httpx.Response]:
    return lambda _request: httpx.Response(status, json=payload) if payload is not None else httpx.Response(status)


def _tools(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    handler: Callable[[httpx.Request], httpx.Response],
    *,
    write: bool = True,
) -> tuple[MicrosoftTodoTools, FakeGraph]:
    graph = FakeGraph(handler).install(monkeypatch)
    paths = runtime_paths(tmp_path)
    manager = save_client_config(paths)
    publish_grant(microsoft_todo_oauth_provider(), manager)
    tools = MicrosoftTodoTools(
        runtime_paths=paths,
        credentials_manager=manager,
        worker_target=worker_target(),
        runtime_config=None,
        write=write,
    )
    return tools, graph


def _payload(result: str) -> dict[str, Any]:
    return json.loads(result)


def _description(tools: MicrosoftTodoTools, name: str) -> str:
    """Return the description the model sees; deferred toolkits do not show toolkit instructions."""
    return " ".join(Function.from_callable(getattr(tools, name)).to_dict()["description"].split())


def test_registered_metadata_declares_a_requester_scoped_tool_without_file_access() -> None:
    metadata = TOOL_METADATA["microsoft_todo"]

    assert metadata.display_name == "Microsoft To Do"
    assert metadata.category == ToolCategory.PRODUCTIVITY
    assert metadata.auth_provider == "microsoft_todo"
    assert metadata.setup_type == SetupType.OAUTH
    assert metadata.requires_primary_runtime is True
    assert metadata.file_access == ToolFileAccess.NONE
    assert metadata.consumes_workspace_paths is False
    assert metadata.function_names == (
        "microsoft_todo_list_lists",
        "microsoft_todo_list_tasks",
        "microsoft_todo_create_task",
        "microsoft_todo_update_task",
        "microsoft_todo_delete_task",
    )


def test_write_false_registers_only_reads(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    tools, _graph = _tools(tmp_path, monkeypatch, _ok({}), write=False)

    assert set(tools.functions) == {"microsoft_todo_list_lists", "microsoft_todo_list_tasks"}


def test_write_true_registers_every_function(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    tools, _graph = _tools(tmp_path, monkeypatch, _ok({}))

    assert set(tools.functions) == set(TOOL_METADATA["microsoft_todo"].function_names)


def test_write_descriptions_ask_the_agent_to_name_the_change(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    tools, _graph = _tools(tmp_path, monkeypatch, _ok({}))

    for name in WRITE_FUNCTIONS:
        assert "Before calling, tell the requester" in _description(tools, name), name
    update = Function.from_callable(tools.microsoft_todo_update_task).to_dict()["parameters"]["properties"]
    assert "an empty string removes the due date" in update["due_date"]["description"]
    assert update["importance"]["enum"] == ["low", "normal", "high"]


def test_list_lists_summarizes_each_list(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    lists = {
        "value": [
            {
                "id": LIST,
                "displayName": "Tasks",
                "isOwner": True,
                "isShared": False,
                "wellknownListName": "defaultList",
            },
            {"id": "L2", "displayName": "Team", "isOwner": False, "isShared": True, "wellknownListName": "none"},
        ],
    }
    tools, graph = _tools(tmp_path, monkeypatch, _ok(lists))

    result = _payload(tools.microsoft_todo_list_lists())

    assert graph.calls() == ["GET graph.microsoft.com/v1.0/me/todo/lists"]
    assert result["lists"] == [
        {"id": LIST, "name": "Tasks", "is_owner": True, "is_shared": False, "wellknown_list_name": "defaultList"},
        {"id": "L2", "name": "Team", "is_owner": False, "is_shared": True, "wellknown_list_name": "none"},
    ]
    assert result["more_available"] is False


def test_list_tasks_returns_open_tasks_by_default(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    tools, graph = _tools(tmp_path, monkeypatch, _ok({"value": [_task()], "@odata.nextLink": "https://x"}))

    result = _payload(tools.microsoft_todo_list_tasks(LIST))

    [request] = graph.requests
    assert (request.method, request.url.path) == ("GET", TASKS_PATH)
    assert dict(request.url.params) == {"$top": "50", "$filter": "status ne 'completed'"}
    assert result["status"] == "ok"
    assert result["more_available"] is True
    assert result["tasks"] == [
        {
            "id": TASK,
            "title": "Send the quarterly report",
            "status": "notStarted",
            "importance": "high",
            "due": {"date_time": "2026-10-22T00:00:00.0000000", "time_zone": "UTC"},
            "reminder": {"date_time": "2026-10-21T08:00:00.0000000", "time_zone": "UTC"},
            "created": "2026-10-20T11:15:19.9359889Z",
            "last_modified": "2026-10-20T11:15:20.0614375Z",
            "notes": "Attach the figures.",
            "categories": ["Important"],
        },
    ]


def test_list_tasks_includes_completed_tasks_on_request(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    tools, graph = _tools(tmp_path, monkeypatch, _ok({"value": [_task(status="completed")]}))

    result = _payload(tools.microsoft_todo_list_tasks(LIST, include_completed=True, limit=5))

    assert dict(graph.requests[0].url.params) == {"$top": "5"}
    assert result["tasks"][0]["status"] == "completed"
    assert result["more_available"] is False


def test_list_tasks_leaves_out_unset_dates_and_caps_notes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    long_notes = "x" * (NOTES_CAP + 1)
    task = _task(isReminderOn=False, body={"content": long_notes, "contentType": "text"})
    del task["dueDateTime"]
    tools, _graph = _tools(tmp_path, monkeypatch, _ok({"value": [task, _task(body={"content": ""})]}))

    capped, empty = _payload(tools.microsoft_todo_list_tasks(LIST))["tasks"]

    assert (capped["due"], capped["reminder"]) == (None, None)
    assert capped["notes"] == long_notes[:NOTES_CAP]
    assert capped["notes_truncated"] is True
    assert empty["notes"] is None
    assert "notes_truncated" not in empty


@pytest.mark.parametrize("limit", [0, 51, True, "5"])
def test_list_tasks_refuses_a_limit_outside_one_to_fifty(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    limit: object,
) -> None:
    tools, graph = _tools(tmp_path, monkeypatch, _ok({"value": []}))

    assert _payload(tools.microsoft_todo_list_tasks(LIST, limit=limit))["code"] == "invalid_argument"  # type: ignore[arg-type]
    assert graph.requests == []


def test_list_tasks_refuses_a_non_boolean_include_completed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    tools, graph = _tools(tmp_path, monkeypatch, _ok({"value": []}))

    result = _payload(tools.microsoft_todo_list_tasks(LIST, include_completed="yes"))  # type: ignore[arg-type]

    assert result["code"] == "invalid_argument"
    assert graph.requests == []


def test_ids_are_escaped_as_single_path_segments(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    tools, graph = _tools(tmp_path, monkeypatch, _ok({"value": []}))

    tools.microsoft_todo_list_tasks("AAMk+x y=")

    assert graph.requests[0].url.raw_path.split(b"?")[0] == b"/v1.0/me/todo/lists/AAMk%2Bx%20y=/tasks"


@pytest.mark.parametrize("bad_id", ["", " ", "..", "a/b", "a\\b", "a?b", "a#b", "a\nb", 7])
@pytest.mark.parametrize("field", ["list_id", "task_id"])
def test_every_function_refuses_malformed_ids_before_any_request(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    bad_id: object,
    field: str,
) -> None:
    tools, graph = _tools(tmp_path, monkeypatch, _ok({}))
    ids = {"list_id": LIST, "task_id": TASK, field: bad_id}

    results = [
        tools.microsoft_todo_update_task(ids["list_id"], ids["task_id"], title="New"),  # type: ignore[arg-type]
        tools.microsoft_todo_delete_task(ids["list_id"], ids["task_id"]),  # type: ignore[arg-type]
    ]
    if field == "list_id":
        results += [
            tools.microsoft_todo_list_tasks(bad_id),  # type: ignore[arg-type]
            tools.microsoft_todo_create_task(bad_id, "New"),  # type: ignore[arg-type]
        ]

    assert [_payload(result)["code"] for result in results] == ["invalid_argument"] * len(results)
    assert graph.requests == []


def test_create_task_posts_only_the_title_and_importance_by_default(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tools, graph = _tools(tmp_path, monkeypatch, _ok(_task(importance="normal"), status=201))

    result = _payload(tools.microsoft_todo_create_task(LIST, "Send the quarterly report"))

    [request] = graph.requests
    assert (request.method, request.url.path, request.url.query) == ("POST", TASKS_PATH, b"")
    assert json.loads(request.content) == {"title": "Send the quarterly report", "importance": "normal"}
    assert result["task"]["id"] == TASK


def test_create_task_sends_due_date_notes_and_importance(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    tools, graph = _tools(tmp_path, monkeypatch, _ok(_task(), status=201))

    tools.microsoft_todo_create_task(
        LIST,
        'Ask O\'Brien about "Q3"',
        due_date="2026-10-22",
        notes="Attach the figures.",
        importance="high",
    )

    assert json.loads(graph.requests[0].content) == {
        "title": 'Ask O\'Brien about "Q3"',
        "importance": "high",
        "dueDateTime": {"dateTime": "2026-10-22T00:00:00", "timeZone": "UTC"},
        "body": {"content": "Attach the figures.", "contentType": "text"},
    }
    assert graph.requests[0].url.query == b""


@pytest.mark.parametrize(
    "arguments",
    [
        {"title": ""},
        {"title": "   "},
        {"title": "x", "due_date": "2026-02-30"},
        {"title": "x", "due_date": "10/22/2026"},
        {"title": "x", "due_date": "2026-10-22T00:00:00"},
        {"title": "x", "due_date": ""},
        {"title": "x", "importance": "urgent"},
        {"title": "x", "notes": 5},
    ],
)
def test_create_task_refuses_malformed_arguments_before_any_request(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    arguments: dict[str, object],
) -> None:
    tools, graph = _tools(tmp_path, monkeypatch, _ok(_task(), status=201))

    assert _payload(tools.microsoft_todo_create_task(LIST, **arguments))["code"] == "invalid_argument"  # type: ignore[arg-type]
    assert graph.requests == []


@pytest.mark.parametrize(
    ("arguments", "body"),
    [
        ({"title": "Renamed"}, {"title": "Renamed"}),
        ({"completed": True}, {"status": "completed"}),
        ({"completed": False}, {"status": "notStarted"}),
        ({"due_date": "2026-11-01"}, {"dueDateTime": {"dateTime": "2026-11-01T00:00:00", "timeZone": "UTC"}}),
        ({"due_date": ""}, {"dueDateTime": None}),
        ({"notes": ""}, {"body": {"content": "", "contentType": "text"}}),
        (
            {"importance": "low", "notes": "New"},
            {"importance": "low", "body": {"content": "New", "contentType": "text"}},
        ),
    ],
)
def test_update_task_patches_only_the_given_fields(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    arguments: dict[str, object],
    body: dict[str, object],
) -> None:
    tools, graph = _tools(tmp_path, monkeypatch, _ok(_task()))

    result = _payload(tools.microsoft_todo_update_task(LIST, TASK, **arguments))  # type: ignore[arg-type]

    [request] = graph.requests
    assert (request.method, request.url.path) == ("PATCH", TASK_PATH)
    assert json.loads(request.content) == body
    assert result["task"]["id"] == TASK


@pytest.mark.parametrize(
    "arguments",
    [{}, {"title": ""}, {"due_date": "tomorrow"}, {"importance": "urgent"}, {"completed": "yes"}, {"notes": 1}],
)
def test_update_task_refuses_nothing_to_change_or_malformed_fields(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    arguments: dict[str, object],
) -> None:
    tools, graph = _tools(tmp_path, monkeypatch, _ok(_task()))

    assert _payload(tools.microsoft_todo_update_task(LIST, TASK, **arguments))["code"] == "invalid_argument"  # type: ignore[arg-type]
    assert graph.requests == []


def test_delete_task_deletes_the_task(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    tools, graph = _tools(tmp_path, monkeypatch, _ok(status=204))

    result = _payload(tools.microsoft_todo_delete_task(LIST, TASK))

    assert [(request.method, request.url.path) for request in graph.requests] == [("DELETE", TASK_PATH)]
    assert result["deleted"] == {"list_id": LIST, "task_id": TASK}


@pytest.mark.parametrize(("status", "code"), [(403, "forbidden"), (404, "not_found")])
def test_graph_errors_keep_their_codes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, status: int, code: str) -> None:
    tools, _graph = _tools(tmp_path, monkeypatch, _ok({"error": {"code": "ErrorItemNotFound"}}, status=status))

    result = _payload(tools.microsoft_todo_delete_task(LIST, TASK))

    assert (result["status"], result["code"], result["graph_code"]) == ("error", code, "ErrorItemNotFound")

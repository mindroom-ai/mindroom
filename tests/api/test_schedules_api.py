"""Tests for schedule management API endpoints."""

from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from unittest.mock import AsyncMock, patch

import aiohttp
import nio
import pytest
import yaml
from fastapi.testclient import TestClient
from httpx import Response

from mindroom.api import config_lifecycle, main
from mindroom.matrix.state import update_matrix_state
from mindroom.scheduling.tasks import CronSchedule, ScheduledTaskRecord, ScheduledWorkflow
from tests.conftest import make_matrix_client_mock
from tests.scheduling_helpers import SCHEDULE_WRITER_ID, scheduled_task_state_event, serve_stored_task


def _task(
    task_id: str,
    *,
    schedule_type: Literal["once", "cron"] = "once",
    execute_at: datetime | None = None,
    cron_fields: dict[str, str] | None = None,
    message: str = "@mindroom_test_agent ping",
    description: str = "Ping task",
    thread_id: str | None = "$thread123",
    new_thread: bool = False,
    history_limit: int | None = None,
    silent: bool = False,
    timezone: str | None = None,
) -> ScheduledTaskRecord:
    cron_schedule = None
    if cron_fields:
        cron_schedule = CronSchedule(**cron_fields)

    workflow = ScheduledWorkflow(
        schedule_type=schedule_type,
        execute_at=execute_at,
        cron_schedule=cron_schedule,
        message=message,
        description=description,
        history_limit=history_limit,
        thread_id=thread_id,
        room_id="test_room",
        created_by="@user:localhost",
        new_thread=new_thread,
        silent=silent,
        timezone=timezone,
    )
    return ScheduledTaskRecord(
        task_id=task_id,
        room_id="test_room",
        status="pending",
        created_at=datetime(2026, 1, 1, 12, 0, tzinfo=UTC),
        workflow=workflow,
    )


def _record_test_room() -> None:
    """Record the configured ``test_room`` as created, so listing reads its tasks under ``!test_room:localhost``."""
    update_matrix_state(
        main._app_runtime_paths(main.app),
        lambda state: state.add_room("test_room", "!test_room:localhost", "#test_room:localhost", "Test Room"),
    )


def _configure_agent_rooms(temp_config_file: Path, rooms: list[str]) -> None:
    """Give ``test_agent`` these rooms and load the config into the app."""
    config_data = yaml.safe_load(temp_config_file.read_text(encoding="utf-8"))
    config_data["agents"]["test_agent"]["rooms"] = rooms
    temp_config_file.write_text(yaml.dump(config_data), encoding="utf-8")
    assert config_lifecycle.load_config_into_app(main._app_runtime_paths(main.app), main.app).loaded


def test_list_schedules_success(test_client: TestClient) -> None:
    """List schedules should return serialized pending tasks."""
    _record_test_room()
    tasks = [
        _task(
            "once1234",
            execute_at=datetime(2026, 2, 10, 15, 30, tzinfo=UTC),
            description="One-time task",
        ),
        _task(
            "cron1234",
            schedule_type="cron",
            cron_fields={"minute": "0", "hour": "9", "day": "*", "month": "*", "weekday": "*"},
            execute_at=None,
            description="Daily task",
            thread_id=None,
            new_thread=True,
            history_limit=5,
            silent=True,
        ),
    ]

    with (
        patch("mindroom.api.schedules.create_agent_http_client", return_value=AsyncMock()),
        patch("mindroom.api.schedules.get_scheduled_tasks_for_room", return_value=tasks),
    ):
        response = test_client.get("/api/schedules")

    assert response.status_code == 200
    data = response.json()
    assert data["timezone"] == "UTC"
    assert len(data["tasks"]) == 2
    tasks_by_id = {task["task_id"]: task for task in data["tasks"]}
    assert tasks_by_id["once1234"]["schedule_type"] == "once"
    assert tasks_by_id["once1234"]["new_thread"] is False
    assert tasks_by_id["once1234"]["history_limit"] is None
    assert tasks_by_id["once1234"]["silent"] is False
    assert tasks_by_id["cron1234"]["cron_expression"] == "0 9 * * *"
    assert tasks_by_id["cron1234"]["new_thread"] is True
    assert tasks_by_id["cron1234"]["thread_id"] is None
    assert tasks_by_id["cron1234"]["history_limit"] == 5
    assert tasks_by_id["cron1234"]["silent"] is True


@pytest.mark.parametrize(
    ("failure", "reason"),
    [
        (
            nio.RoomGetStateError.from_dict(
                {"errcode": "M_FORBIDDEN", "error": "You are not in the room"},
                room_id="!unreadable:localhost",
            ),
            "RoomGetStateError: M_FORBIDDEN You are not in the room",
        ),
        (
            aiohttp.ClientPayloadError("Response payload is not completed"),
            "ClientPayloadError: Response payload is not completed",
        ),
    ],
    ids=["error response", "broken-off response"],
)
def test_list_schedules_reports_an_unreadable_room_beside_the_readable_ones(
    test_client: TestClient,
    temp_config_file: Path,
    failure: object,
    reason: str,
) -> None:
    """A room whose state cannot be read is reported with the reason instead of looking empty or failing the list."""
    _configure_agent_rooms(temp_config_file, ["!readable:localhost", "!unreadable:localhost"])
    stored = _stored()

    async def room_get_state(room_id: str) -> object:
        if room_id == "!unreadable:localhost":
            if isinstance(failure, Exception):
                raise failure
            return failure
        return nio.RoomGetStateResponse.from_dict(
            [scheduled_task_state_event("abc12345", stored, room_id=room_id)],
            room_id=room_id,
        )

    client = make_matrix_client_mock()
    client.room_get_state = AsyncMock(side_effect=room_get_state)
    response = _send_with(test_client, client, "GET", path="/api/schedules")

    assert response.status_code == 200
    data = response.json()
    assert [(task["task_id"], task["room_id"]) for task in data["tasks"]] == [("abc12345", "!readable:localhost")]
    assert data["unreadable_rooms"] == [
        {
            "room_id": "!unreadable:localhost",
            "room_alias": None,
            "error": f"Unable to retrieve scheduled tasks: {reason}",
        },
    ]


def test_list_schedules_reads_a_room_configured_by_alias_under_its_room_id(
    test_client: TestClient,
    temp_config_file: Path,
) -> None:
    """A configured alias is read once under the room ID it resolves to, and one that does not resolve says why."""
    _configure_agent_rooms(temp_config_file, ["#test_room:localhost", "test_room", "gone", "#gone:localhost"])
    # The room key and the alias name the same room, which must be read once.
    _record_test_room()

    client = make_matrix_client_mock()
    client.room_resolve_alias = AsyncMock(
        return_value=nio.RoomResolveAliasError.from_dict(
            {"errcode": "M_NOT_FOUND", "error": "Room alias #gone:localhost not found"},
        ),
    )
    client.room_get_state = AsyncMock(
        return_value=nio.RoomGetStateResponse.from_dict(
            [scheduled_task_state_event("abc12345", _stored(), room_id="!test_room:localhost")],
            room_id="!test_room:localhost",
        ),
    )
    response = _send_with(test_client, client, "GET", path="/api/schedules")

    assert response.status_code == 200
    data = response.json()
    client.room_resolve_alias.assert_awaited_once_with("#gone:localhost")
    client.room_get_state.assert_awaited_once_with("!test_room:localhost")
    assert [(task["task_id"], task["room_id"]) for task in data["tasks"]] == [("abc12345", "!test_room:localhost")]
    assert data["unreadable_rooms"] == [
        {
            "room_id": "#gone:localhost",
            "room_alias": "#gone:localhost",
            "error": "Unable to resolve room alias: RoomResolveAliasError: M_NOT_FOUND Room alias #gone:localhost not found",
        },
    ]


def test_list_schedules_reports_an_alias_whose_lookup_broke_off_beside_the_readable_rooms(
    test_client: TestClient,
    temp_config_file: Path,
) -> None:
    """An alias lookup that breaks off is reported with the reason, and the other rooms' tasks are still listed."""
    # The broken-off lookup comes first, so the next reference must not inherit its outcome.
    _configure_agent_rooms(temp_config_file, ["flaky", "#flaky:localhost", "readable", "#readable:localhost"])

    client = make_matrix_client_mock()
    client.room_resolve_alias = AsyncMock(
        side_effect=[
            aiohttp.ClientPayloadError("Response payload is not completed"),
            nio.RoomResolveAliasResponse(room_alias="#readable:localhost", room_id="!readable:localhost", servers=[]),
        ],
    )
    client.room_get_state = AsyncMock(
        return_value=nio.RoomGetStateResponse.from_dict(
            [scheduled_task_state_event("abc12345", _stored(), room_id="!readable:localhost")],
            room_id="!readable:localhost",
        ),
    )
    response = _send_with(test_client, client, "GET", path="/api/schedules")

    assert response.status_code == 200
    data = response.json()
    assert [(task["task_id"], task["room_id"]) for task in data["tasks"]] == [("abc12345", "!readable:localhost")]
    assert data["unreadable_rooms"] == [
        {
            "room_id": "#flaky:localhost",
            "room_alias": "#flaky:localhost",
            "error": "Unable to resolve room alias: ClientPayloadError: Response payload is not completed",
        },
    ]


def test_list_schedules_filtered_by_alias_reads_only_that_room(test_client: TestClient) -> None:
    """A room filter written as an alias is read under the room ID it resolves to."""
    client = make_matrix_client_mock()
    client.room_resolve_alias = AsyncMock(
        return_value=nio.RoomResolveAliasResponse(
            room_alias="#lobby:example.org",
            room_id="!lobby:localhost",
            servers=[],
        ),
    )
    client.room_get_state = AsyncMock(return_value=nio.RoomGetStateResponse.from_dict([], room_id="!lobby:localhost"))
    response = _send_with(test_client, client, "GET", path="/api/schedules", params={"room_id": "#lobby:example.org"})

    assert response.status_code == 200
    client.room_get_state.assert_awaited_once_with("!lobby:localhost")


def test_list_schedules_without_rooms_needs_no_matrix_account(test_client: TestClient, temp_config_file: Path) -> None:
    """With no room configured, the list is empty before MindRoom has created its Matrix accounts."""
    _configure_agent_rooms(temp_config_file, [])

    response = test_client.get("/api/schedules")

    assert response.status_code == 200
    assert response.json() == {"timezone": "UTC", "tasks": [], "unreadable_rooms": []}


def test_list_schedules_skips_a_configured_room_not_created_yet(test_client: TestClient) -> None:
    """A room key with no created room holds no tasks, so it is neither read nor reported unreadable."""
    client = make_matrix_client_mock()
    response = _send_with(test_client, client, "GET", path="/api/schedules")

    assert response.status_code == 200
    assert response.json()["tasks"] == []
    assert response.json()["unreadable_rooms"] == []
    client.room_get_state.assert_not_awaited()


def test_list_schedules_invalid_cron_does_not_fail(test_client: TestClient) -> None:
    """Invalid stored cron values should not crash schedule listing."""
    _record_test_room()
    tasks = [
        _task(
            "badcron1",
            schedule_type="cron",
            cron_fields={"minute": "70", "hour": "*", "day": "*", "month": "*", "weekday": "*"},
            execute_at=None,
            description="Invalid cron task",
        ),
    ]

    with (
        patch("mindroom.api.schedules.create_agent_http_client", return_value=AsyncMock()),
        patch("mindroom.api.schedules.get_scheduled_tasks_for_room", return_value=tasks),
    ):
        response = test_client.get("/api/schedules")

    assert response.status_code == 200
    data = response.json()
    assert len(data["tasks"]) == 1
    assert data["tasks"][0]["task_id"] == "badcron1"
    assert data["tasks"][0]["next_run_at"] is None


def test_list_schedules_describes_each_task_in_its_own_timezone(test_client: TestClient) -> None:
    """A task stored with a timezone is described in it; one without falls back to the global timezone (UTC here)."""
    _record_test_room()
    midnight_utc = {"minute": "0", "hour": "0", "day": "*", "month": "*", "weekday": "*"}
    tasks = [
        _task("tokyo123", schedule_type="cron", cron_fields=midnight_utc, timezone="Asia/Tokyo"),
        _task("global12", schedule_type="cron", cron_fields=midnight_utc),
    ]

    with (
        patch("mindroom.api.schedules.create_agent_http_client", return_value=AsyncMock()),
        patch("mindroom.api.schedules.get_scheduled_tasks_for_room", return_value=tasks),
    ):
        response = test_client.get("/api/schedules")

    assert response.status_code == 200
    tasks_by_id = {task["task_id"]: task for task in response.json()["tasks"]}
    assert tasks_by_id["tokyo123"]["cron_description"] == "At 09:00 (Asia/Tokyo)"
    assert tasks_by_id["global12"]["cron_description"] == "At 00:00 (UTC)"


def test_update_schedule_once_success(test_client: TestClient) -> None:
    """Update endpoint should persist prompt and once schedule changes."""
    existing_task = _task(
        "abc12345",
        execute_at=datetime(2026, 2, 10, 9, 0, tzinfo=UTC),
        description="Original description",
        message="@mindroom_test_agent original",
        thread_id=None,
        new_thread=True,
    )
    updated_workflow = ScheduledWorkflow(
        schedule_type="once",
        execute_at=datetime(2026, 3, 1, 10, 0, tzinfo=UTC),
        message="@mindroom_test_agent updated",
        description="Updated description",
        thread_id=existing_task.workflow.thread_id,
        room_id="test_room",
        created_by=existing_task.workflow.created_by,
        new_thread=existing_task.workflow.new_thread,
        silent=True,
    )
    updated_task = ScheduledTaskRecord(
        task_id="abc12345",
        room_id="test_room",
        status="pending",
        created_at=existing_task.created_at,
        workflow=updated_workflow,
    )
    save_mock = AsyncMock(return_value=updated_task)

    with (
        patch("mindroom.api.schedules.create_agent_http_client", return_value=AsyncMock()),
        patch("mindroom.api.schedules.require_scheduled_task", return_value=existing_task),
        patch("mindroom.api.schedules.save_edited_scheduled_task", save_mock),
    ):
        response = test_client.put(
            "/api/schedules/abc12345",
            json={
                "room_id": "test_room",
                "schedule_type": "once",
                "execute_at": "2026-03-01T10:00:00Z",
                "message": "@mindroom_test_agent updated",
                "description": "Updated description",
                "silent": True,
            },
        )

    assert response.status_code == 200
    data = response.json()
    assert data["task_id"] == "abc12345"
    assert data["schedule_type"] == "once"
    assert data["message"] == "@mindroom_test_agent updated"
    assert data["description"] == "Updated description"
    assert data["execute_at"] == "2026-03-01T10:00:00Z"
    assert data["new_thread"] is True
    assert data["silent"] is True
    save_mock.assert_awaited_once()
    assert save_mock.await_args.kwargs["existing_task"] is existing_task
    assert save_mock.await_args.kwargs["workflow"].new_thread is True
    assert save_mock.await_args.kwargs["workflow"].silent is True
    assert save_mock.await_args.kwargs["config"].timezone == "UTC"


def test_update_schedule_invalid_cron_expression(test_client: TestClient) -> None:
    """Invalid cron expressions should return 400."""
    existing_task = _task(
        "cronbad1",
        schedule_type="cron",
        cron_fields={"minute": "0", "hour": "9", "day": "*", "month": "*", "weekday": "*"},
        execute_at=None,
        description="Cron task",
    )

    with (
        patch("mindroom.api.schedules.create_agent_http_client", return_value=AsyncMock()),
        patch("mindroom.api.schedules.require_scheduled_task", return_value=existing_task),
    ):
        response = test_client.put(
            "/api/schedules/cronbad1",
            json={
                "room_id": "test_room",
                "schedule_type": "cron",
                "cron_expression": "bad cron",
            },
        )

    assert response.status_code == 400
    assert "Invalid cron expression" in response.json()["detail"]


def test_cancel_schedule_success(test_client: TestClient) -> None:
    """Cancel endpoint should return success wrapper."""
    cancel_mock = AsyncMock(return_value="✅ Cancelled task `abc12345`")
    with (
        patch("mindroom.api.schedules.create_agent_http_client", return_value=AsyncMock()),
        patch("mindroom.api.schedules.cancel_scheduled_task", cancel_mock),
    ):
        response = test_client.delete("/api/schedules/abc12345?room_id=test_room")

    assert response.status_code == 200
    assert response.json()["success"] is True
    assert cancel_mock.await_args.kwargs["task_id"] == "abc12345"
    assert cancel_mock.await_args.kwargs["room_id"] == "test_room"


@pytest.mark.parametrize(
    ("existing_task", "request_json"),
    [
        (
            _task("switch01", execute_at=datetime(2026, 2, 10, 9, 0, tzinfo=UTC), thread_id=None, new_thread=True),
            {"room_id": "test_room", "schedule_type": "cron", "cron_expression": "30 8 * * 1-5"},
        ),
        (
            _task(
                "switch02",
                schedule_type="cron",
                cron_fields={"minute": "0", "hour": "9", "day": "*", "month": "*", "weekday": "*"},
                execute_at=None,
            ),
            {"room_id": "test_room", "schedule_type": "once", "execute_at": "2026-04-01T12:00:00Z"},
        ),
    ],
    ids=["once-to-cron", "cron-to-once"],
)
def test_update_schedule_cannot_switch_schedule_type(
    test_client: TestClient,
    existing_task: ScheduledTaskRecord,
    request_json: dict[str, str],
) -> None:
    """Switching a task between once and cron is rejected by the API."""
    save_mock = AsyncMock()

    with (
        patch("mindroom.api.schedules.create_agent_http_client", return_value=AsyncMock()),
        patch("mindroom.api.schedules.require_scheduled_task", return_value=existing_task),
        patch("mindroom.api.schedules.save_edited_scheduled_task", save_mock),
    ):
        response = test_client.put(f"/api/schedules/{existing_task.task_id}", json=request_json)

    assert response.status_code == 400
    assert "Changing schedule_type is not supported" in response.json()["detail"]
    save_mock.assert_not_awaited()


def test_update_schedule_conflicting_fields(test_client: TestClient) -> None:
    """Sending execute_at with cron schedule_type should return 400."""
    existing_task = _task(
        "conflict1",
        schedule_type="cron",
        cron_fields={"minute": "0", "hour": "9", "day": "*", "month": "*", "weekday": "*"},
        execute_at=None,
    )

    with (
        patch("mindroom.api.schedules.create_agent_http_client", return_value=AsyncMock()),
        patch("mindroom.api.schedules.require_scheduled_task", return_value=existing_task),
    ):
        response = test_client.put(
            "/api/schedules/conflict1",
            json={
                "room_id": "test_room",
                "schedule_type": "cron",
                "execute_at": "2026-04-01T12:00:00Z",
            },
        )

    assert response.status_code == 400
    assert "execute_at" in response.json()["detail"]


def test_update_schedule_empty_message(test_client: TestClient) -> None:
    """Updating with an empty message should return 400."""
    existing_task = _task(
        "empty_msg",
        execute_at=datetime(2026, 2, 10, 9, 0, tzinfo=UTC),
    )

    with (
        patch("mindroom.api.schedules.create_agent_http_client", return_value=AsyncMock()),
        patch("mindroom.api.schedules.require_scheduled_task", return_value=existing_task),
    ):
        response = test_client.put(
            "/api/schedules/empty_msg",
            json={
                "room_id": "test_room",
                "message": "   ",
            },
        )

    assert response.status_code == 400
    assert "message" in response.json()["detail"]


def _matrix_client_storing(content: dict[str, object] | None, *, put_error: bool = False) -> AsyncMock:
    """Return a Matrix client whose room stores one bot-written task with ``content``, or none."""
    client = make_matrix_client_mock()
    client.homeserver = "http://localhost:8008"
    serve_stored_task(client, "test_room", "abc12345", content)
    if put_error:
        client.room_put_state = AsyncMock(
            return_value=nio.RoomPutStateError(message="denied", status_code="M_FORBIDDEN"),
        )
    else:
        client.room_put_state = AsyncMock(return_value=nio.RoomPutStateResponse(event_id="$put", room_id="test_room"))
    return client


def _stored(status: str = "pending") -> dict[str, object]:
    workflow = _task("abc12345", execute_at=datetime(2026, 2, 10, 9, 0, tzinfo=UTC)).workflow
    return {"task_id": "abc12345", "status": status, "workflow": workflow.model_dump_json()}


def _send_with(
    test_client: TestClient,
    client: AsyncMock,
    method: str,
    path: str = "/api/schedules/abc12345",
    **kwargs: object,
) -> Response:
    with (
        patch("mindroom.api.schedules.create_agent_http_client", return_value=client),
        patch("mindroom.scheduling.tasks.persisted_bot_user_ids", return_value=frozenset({SCHEDULE_WRITER_ID})),
    ):
        return test_client.request(method, path, **kwargs)


def test_cancel_of_a_missing_task_is_not_found(test_client: TestClient) -> None:
    """Cancelling a task the room does not store answers 404 with the scheduler's reason."""
    response = _send_with(test_client, _matrix_client_storing(None), "DELETE", params={"room_id": "test_room"})

    assert response.status_code == 404
    assert response.json()["detail"] == "Task `abc12345` not found."


def test_cancel_whose_state_write_is_refused_is_a_server_error(test_client: TestClient) -> None:
    """A cancellation Matrix refuses to store answers 500 with the scheduler's reason."""
    client = _matrix_client_storing(_stored(), put_error=True)

    response = _send_with(test_client, client, "DELETE", params={"room_id": "test_room"})

    assert response.status_code == 500
    assert response.json()["detail"] == (
        "Failed to cancel task `abc12345`: Failed to persist scheduled task state for task `abc12345`: "
        f"RoomPutStateError: M_FORBIDDEN denied. Ensure {client.user_id} is joined with permission to write room "
        "state, then retry."
    )


def test_cancel_of_a_task_whose_workflow_no_longer_parses(test_client: TestClient) -> None:
    """The dashboard cancels a stored task even when its workflow no longer parses, as chat cancellation does."""
    client = _matrix_client_storing({"task_id": "abc12345", "status": "pending", "workflow": "{not json"})

    with patch("mindroom.scheduling.tasks.withdraw_scheduled_call", new=AsyncMock()) as withdraw:
        response = _send_with(test_client, client, "DELETE", params={"room_id": "test_room"})

    assert response.status_code == 200
    assert response.json() == {"success": True, "message": "Cancelled task `abc12345`"}
    withdraw.assert_awaited_once()
    assert client.room_put_state.await_args.kwargs["content"]["status"] == "cancelled"


def test_cancel_of_a_task_that_already_ended_is_a_bad_request(test_client: TestClient) -> None:
    """Cancelling a task that already ran answers 400 with its status and leaves it as stored."""
    client = _matrix_client_storing(_stored("completed"))

    response = _send_with(test_client, client, "DELETE", params={"room_id": "test_room"})

    assert response.status_code == 400
    assert response.json()["detail"] == "Task `abc12345` cannot be cancelled because it is `completed`."
    client.room_put_state.assert_not_awaited()


@pytest.mark.parametrize(
    ("method", "kwargs"),
    [
        ("PUT", {"json": {"room_id": "test_room", "message": "updated"}}),
        ("DELETE", {"params": {"room_id": "test_room"}}),
    ],
)
def test_edit_or_cancel_whose_task_cannot_be_read_is_a_server_error(
    test_client: TestClient,
    method: str,
    kwargs: dict[str, object],
) -> None:
    """An edit or cancellation whose task read fails answers 500 with the scheduler's reason."""
    client = _matrix_client_storing(_stored())
    read_error = nio.RoomGetStateEventError(message="rate limited", status_code="M_LIMIT_EXCEEDED")
    client.room_get_state_event = AsyncMock(return_value=read_error)

    response = _send_with(test_client, client, method, **kwargs)

    assert response.status_code == 500
    assert response.json()["detail"] == f"Failed to get scheduled task `abc12345` from room `test_room`: {read_error}"
    client.room_put_state.assert_not_awaited()


def test_edit_of_a_task_whose_state_no_longer_parses_is_a_bad_request(test_client: TestClient) -> None:
    """Editing a task whose stored state cannot be understood answers 400 with how to replace it."""
    client = _matrix_client_storing({"task_id": "abc12345", "status": "pending", "workflow": "{not json"})

    response = _send_with(test_client, client, "PUT", json={"room_id": "test_room", "message": "updated"})

    assert response.status_code == 400
    assert response.json()["detail"] == "Task `abc12345` has invalid stored state; cancel it and schedule it again."
    client.room_put_state.assert_not_awaited()


def test_edit_of_a_missing_task_is_not_found(test_client: TestClient) -> None:
    """Editing a task the room does not store answers 404."""
    response = _send_with(
        test_client,
        _matrix_client_storing(None),
        "PUT",
        json={"room_id": "test_room", "message": "updated"},
    )

    assert response.status_code == 404
    assert response.json()["detail"] == "Task `abc12345` not found."


def test_edit_of_a_cancelled_task_is_a_bad_request(test_client: TestClient) -> None:
    """Editing a task that no longer runs answers 400 with its status."""
    response = _send_with(
        test_client,
        _matrix_client_storing(_stored("cancelled")),
        "PUT",
        json={"room_id": "test_room", "message": "updated"},
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "Task `abc12345` cannot be edited because it is `cancelled`."


def test_edit_whose_state_write_is_refused_is_a_server_error(test_client: TestClient) -> None:
    """An edit Matrix refuses to store answers 500 with the scheduler's reason, as a refused cancellation does."""
    client = _matrix_client_storing(_stored(), put_error=True)

    response = _send_with(test_client, client, "PUT", json={"room_id": "test_room", "message": "updated"})

    assert response.status_code == 500
    assert response.json()["detail"] == (
        "Failed to persist scheduled task state for task `abc12345`: RoomPutStateError: M_FORBIDDEN denied. "
        f"Ensure {client.user_id} is joined with permission to write room state, then retry."
    )

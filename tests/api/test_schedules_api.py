"""Tests for schedule management API endpoints."""

from datetime import UTC, datetime
from typing import Literal
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi.testclient import TestClient

from mindroom.scheduling import CronSchedule, ScheduledTaskRecord, ScheduledWorkflow


def _task(
    task_id: str,
    *,
    room_id: str = "test_room",
    schedule_type: Literal["once", "cron"] = "once",
    execute_at: datetime | None = None,
    cron_fields: dict[str, str] | None = None,
    message: str = "@mindroom_test_agent ping",
    description: str = "Ping task",
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
        thread_id="$thread123",
        room_id=room_id,
        created_by="@user:localhost",
    )
    return ScheduledTaskRecord(
        task_id=task_id,
        room_id=room_id,
        status="pending",
        created_at=datetime(2026, 1, 1, 12, 0, tzinfo=UTC),
        workflow=workflow,
    )


def _mock_agent_user() -> MagicMock:
    user = MagicMock()
    user.agent_name = "router"
    user.user_id = "@mindroom_router:localhost"
    user.display_name = "RouterAgent"
    user.password = "test_password"  # noqa: S105
    user.access_token = "test_token"  # noqa: S105
    return user


def _mock_matrix_client() -> AsyncMock:
    client = AsyncMock()
    client.close = AsyncMock()
    return client


def test_list_schedules_success(test_client: TestClient) -> None:
    """List schedules should return serialized pending tasks."""
    mock_client = _mock_matrix_client()
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
        ),
    ]

    with (
        patch("mindroom.api.schedules.create_agent_user", return_value=_mock_agent_user()),
        patch("mindroom.api.schedules.login_agent_user", return_value=mock_client),
        patch("mindroom.api.schedules.get_scheduled_tasks_for_room", return_value=tasks),
    ):
        response = test_client.get("/api/schedules")

    assert response.status_code == 200
    data = response.json()
    assert data["timezone"] == "UTC"
    assert len(data["tasks"]) == 2
    tasks_by_id = {task["task_id"]: task for task in data["tasks"]}
    assert tasks_by_id["once1234"]["schedule_type"] == "once"
    assert tasks_by_id["cron1234"]["cron_expression"] == "0 9 * * *"


def test_list_schedules_invalid_cron_does_not_fail(test_client: TestClient) -> None:
    """Invalid stored cron values should not crash schedule listing."""
    mock_client = _mock_matrix_client()
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
        patch("mindroom.api.schedules.create_agent_user", return_value=_mock_agent_user()),
        patch("mindroom.api.schedules.login_agent_user", return_value=mock_client),
        patch("mindroom.api.schedules.get_scheduled_tasks_for_room", return_value=tasks),
    ):
        response = test_client.get("/api/schedules")

    assert response.status_code == 200
    data = response.json()
    assert len(data["tasks"]) == 1
    assert data["tasks"][0]["task_id"] == "badcron1"
    assert data["tasks"][0]["next_run_at"] is None


def test_update_schedule_once_success(test_client: TestClient) -> None:
    """Update endpoint should persist prompt and once schedule changes."""
    mock_client = _mock_matrix_client()
    existing_task = _task(
        "abc12345",
        execute_at=datetime(2026, 2, 10, 9, 0, tzinfo=UTC),
        description="Original description",
        message="@mindroom_test_agent original",
    )
    save_mock = AsyncMock()

    with (
        patch("mindroom.api.schedules.create_agent_user", return_value=_mock_agent_user()),
        patch("mindroom.api.schedules.login_agent_user", return_value=mock_client),
        patch("mindroom.api.schedules.get_scheduled_task", return_value=existing_task),
        patch("mindroom.api.schedules.save_scheduled_task", save_mock),
    ):
        response = test_client.put(
            "/api/schedules/abc12345",
            json={
                "room_id": "test_room",
                "schedule_type": "once",
                "execute_at": "2026-03-01T10:00:00Z",
                "message": "@mindroom_test_agent updated",
                "description": "Updated description",
            },
        )

    assert response.status_code == 200
    data = response.json()
    assert data["task_id"] == "abc12345"
    assert data["schedule_type"] == "once"
    assert data["message"] == "@mindroom_test_agent updated"
    assert data["description"] == "Updated description"
    assert data["execute_at"] == "2026-03-01T10:00:00Z"
    save_mock.assert_awaited_once()
    assert save_mock.await_args.kwargs["task_id"] == "abc12345"
    assert save_mock.await_args.kwargs["room_id"] == "test_room"


def test_update_schedule_invalid_cron_expression(test_client: TestClient) -> None:
    """Invalid cron expressions should return 400."""
    mock_client = _mock_matrix_client()
    existing_task = _task(
        "cronbad1",
        schedule_type="cron",
        cron_fields={"minute": "0", "hour": "9", "day": "*", "month": "*", "weekday": "*"},
        execute_at=None,
        description="Cron task",
    )

    with (
        patch("mindroom.api.schedules.create_agent_user", return_value=_mock_agent_user()),
        patch("mindroom.api.schedules.login_agent_user", return_value=mock_client),
        patch("mindroom.api.schedules.get_scheduled_task", return_value=existing_task),
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
    mock_client = _mock_matrix_client()
    existing_task = _task("abc12345", execute_at=datetime(2026, 2, 10, 9, 0, tzinfo=UTC))
    cancel_mock = AsyncMock(return_value="✅ Cancelled task `abc12345`")
    with (
        patch("mindroom.api.schedules.create_agent_user", return_value=_mock_agent_user()),
        patch("mindroom.api.schedules.login_agent_user", return_value=mock_client),
        patch("mindroom.api.schedules.get_scheduled_task", return_value=existing_task),
        patch("mindroom.api.schedules.cancel_scheduled_task", cancel_mock),
    ):
        response = test_client.delete("/api/schedules/abc12345?room_id=test_room")

    assert response.status_code == 200
    assert response.json()["success"] is True
    assert cancel_mock.await_args.kwargs["task_id"] == "abc12345"
    assert cancel_mock.await_args.kwargs["room_id"] == "test_room"


def test_cancel_schedule_not_found(test_client: TestClient) -> None:
    """Cancel endpoint should return 404 when task does not exist."""
    mock_client = _mock_matrix_client()
    with (
        patch("mindroom.api.schedules.create_agent_user", return_value=_mock_agent_user()),
        patch("mindroom.api.schedules.login_agent_user", return_value=mock_client),
        patch("mindroom.api.schedules.get_scheduled_task", return_value=None),
    ):
        response = test_client.delete("/api/schedules/missing?room_id=test_room")

    assert response.status_code == 404


def test_update_schedule_once_to_cron(test_client: TestClient) -> None:
    """Switching from once to cron requires a cron_expression."""
    mock_client = _mock_matrix_client()
    existing_task = _task(
        "switch01",
        execute_at=datetime(2026, 2, 10, 9, 0, tzinfo=UTC),
    )
    save_mock = AsyncMock()

    with (
        patch("mindroom.api.schedules.create_agent_user", return_value=_mock_agent_user()),
        patch("mindroom.api.schedules.login_agent_user", return_value=mock_client),
        patch("mindroom.api.schedules.get_scheduled_task", return_value=existing_task),
        patch("mindroom.api.schedules.save_scheduled_task", save_mock),
    ):
        response = test_client.put(
            "/api/schedules/switch01",
            json={
                "room_id": "test_room",
                "schedule_type": "cron",
                "cron_expression": "30 8 * * 1-5",
            },
        )

    assert response.status_code == 200
    data = response.json()
    assert data["schedule_type"] == "cron"
    assert data["cron_expression"] == "30 8 * * 1-5"
    assert data["execute_at"] is None
    save_mock.assert_awaited_once()
    assert save_mock.await_args.kwargs["task_id"] == "switch01"
    assert save_mock.await_args.kwargs["room_id"] == "test_room"


def test_update_schedule_cron_to_once(test_client: TestClient) -> None:
    """Switching from cron to once requires execute_at."""
    mock_client = _mock_matrix_client()
    existing_task = _task(
        "switch02",
        schedule_type="cron",
        cron_fields={"minute": "0", "hour": "9", "day": "*", "month": "*", "weekday": "*"},
        execute_at=None,
    )
    save_mock = AsyncMock()

    with (
        patch("mindroom.api.schedules.create_agent_user", return_value=_mock_agent_user()),
        patch("mindroom.api.schedules.login_agent_user", return_value=mock_client),
        patch("mindroom.api.schedules.get_scheduled_task", return_value=existing_task),
        patch("mindroom.api.schedules.save_scheduled_task", save_mock),
    ):
        response = test_client.put(
            "/api/schedules/switch02",
            json={
                "room_id": "test_room",
                "schedule_type": "once",
                "execute_at": "2026-04-01T12:00:00Z",
            },
        )

    assert response.status_code == 200
    data = response.json()
    assert data["schedule_type"] == "once"
    assert data["execute_at"] == "2026-04-01T12:00:00Z"
    assert data["cron_expression"] is None
    save_mock.assert_awaited_once()
    assert save_mock.await_args.kwargs["task_id"] == "switch02"
    assert save_mock.await_args.kwargs["room_id"] == "test_room"


def test_update_schedule_conflicting_fields(test_client: TestClient) -> None:
    """Sending execute_at with cron schedule_type should return 400."""
    mock_client = _mock_matrix_client()
    existing_task = _task(
        "conflict1",
        schedule_type="cron",
        cron_fields={"minute": "0", "hour": "9", "day": "*", "month": "*", "weekday": "*"},
        execute_at=None,
    )

    with (
        patch("mindroom.api.schedules.create_agent_user", return_value=_mock_agent_user()),
        patch("mindroom.api.schedules.login_agent_user", return_value=mock_client),
        patch("mindroom.api.schedules.get_scheduled_task", return_value=existing_task),
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
    mock_client = _mock_matrix_client()
    existing_task = _task(
        "empty_msg",
        execute_at=datetime(2026, 2, 10, 9, 0, tzinfo=UTC),
    )

    with (
        patch("mindroom.api.schedules.create_agent_user", return_value=_mock_agent_user()),
        patch("mindroom.api.schedules.login_agent_user", return_value=mock_client),
        patch("mindroom.api.schedules.get_scheduled_task", return_value=existing_task),
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

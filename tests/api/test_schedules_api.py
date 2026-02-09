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
    with (
        patch("mindroom.api.schedules.create_agent_user", return_value=_mock_agent_user()),
        patch("mindroom.api.schedules.login_agent_user", return_value=mock_client),
        patch(
            "mindroom.api.schedules.cancel_scheduled_task",
            return_value="✅ Cancelled task `abc12345`",
        ),
    ):
        response = test_client.delete("/api/schedules/abc12345?room_id=test_room")

    assert response.status_code == 200
    assert response.json()["success"] is True


def test_cancel_schedule_not_found(test_client: TestClient) -> None:
    """Cancel endpoint should map not-found task responses to 404."""
    mock_client = _mock_matrix_client()
    with (
        patch("mindroom.api.schedules.create_agent_user", return_value=_mock_agent_user()),
        patch("mindroom.api.schedules.login_agent_user", return_value=mock_client),
        patch(
            "mindroom.api.schedules.cancel_scheduled_task",
            return_value="❌ Task `missing` not found.",
        ),
    ):
        response = test_client.delete("/api/schedules/missing?room_id=test_room")

    assert response.status_code == 404

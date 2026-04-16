"""Tests for schedule management API endpoints."""

from datetime import UTC, datetime
from typing import Literal
from unittest.mock import AsyncMock, MagicMock, patch

import nio
import pytest
from fastapi.testclient import TestClient

from mindroom.api.schedules import UpdateScheduleRequest, update_schedule
from mindroom.constants import ROUTER_AGENT_NAME
from mindroom.scheduling import (
    CronSchedule,
    ScheduledTaskMutationContext,
    ScheduledTaskOperationError,
    ScheduledTaskRecord,
    ScheduledWorkflow,
)


def _task(
    task_id: str,
    *,
    room_id: str = "test_room",
    schedule_type: Literal["once", "cron"] = "once",
    execute_at: datetime | None = None,
    cron_fields: dict[str, str] | None = None,
    message: str = "@mindroom_test_agent ping",
    description: str = "Ping task",
    thread_id: str | None = "$thread123",
    new_thread: bool = False,
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
        thread_id=thread_id,
        room_id=room_id,
        created_by="@user:localhost",
        new_thread=new_thread,
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


def _mock_named_agent_user(agent_name: str, user_id: str) -> MagicMock:
    user = MagicMock()
    user.agent_name = agent_name
    user.user_id = user_id
    user.display_name = agent_name
    user.password = "test_password"  # noqa: S105
    user.access_token = "test_token"  # noqa: S105
    return user


def _mock_matrix_client() -> AsyncMock:
    client = AsyncMock()
    client.user_id = "@mindroom_router:localhost"
    client.joined_members.return_value = nio.JoinedMembersResponse.from_dict(
        {
            "joined": {
                client.user_id: {"display_name": "Router"},
            },
        },
        room_id="test_room",
    )
    client.room_get_state_event.return_value = nio.RoomGetStateEventResponse(
        content={
            "events": {"com.mindroom.scheduled.task": 50},
            "state_default": 50,
            "users_default": 0,
            "users": {client.user_id: 100},
        },
        event_type="m.room.power_levels",
        state_key="",
        room_id="test_room",
    )
    client.close = AsyncMock()
    return client


def _mutation_context(
    task: ScheduledTaskRecord,
    *,
    writer_client: AsyncMock,
    task_sender_id: str = "@mindroom_router:localhost",
) -> ScheduledTaskMutationContext:
    return ScheduledTaskMutationContext(
        task=task,
        task_sender_id=task_sender_id,
        writer_client=writer_client,
        re_resolve_writer=AsyncMock(return_value=writer_client),
    )


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
            thread_id=None,
            new_thread=True,
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
    assert tasks_by_id["once1234"]["new_thread"] is False
    assert tasks_by_id["cron1234"]["cron_expression"] == "0 9 * * *"
    assert tasks_by_id["cron1234"]["new_thread"] is True
    assert tasks_by_id["cron1234"]["thread_id"] is None


def test_list_schedules_includes_router_ad_hoc_rooms(test_client: TestClient) -> None:
    """The list endpoint should include persisted router ad-hoc rooms when no room filter is provided."""
    mock_client = _mock_matrix_client()
    configured_task = _task("configured1", room_id="test_room")
    ad_hoc_task = _task("adhoc1", room_id="!adhoc:localhost")

    async def list_room_tasks(*, room_id: str, **_kwargs: object) -> list[ScheduledTaskRecord]:
        if room_id == "!adhoc:localhost":
            return [ad_hoc_task]
        return [configured_task]

    with (
        patch("mindroom.api.schedules.create_agent_user", return_value=_mock_agent_user()),
        patch("mindroom.api.schedules.login_agent_user", return_value=mock_client),
        patch(
            "mindroom.api.schedules.MatrixState.load",
            return_value=MagicMock(router_ad_hoc_room_ids={"!adhoc:localhost"}),
        ),
        patch("mindroom.api.schedules.get_scheduled_tasks_for_room", new=AsyncMock(side_effect=list_room_tasks)),
    ):
        response = test_client.get("/api/schedules")

    assert response.status_code == 200
    task_ids = {task["task_id"] for task in response.json()["tasks"]}
    assert task_ids == {"configured1", "adhoc1"}


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


def test_list_schedules_skips_unreadable_router_ad_hoc_room(test_client: TestClient) -> None:
    """Aggregate listing should skip unreadable persisted ad-hoc rooms instead of failing all rooms."""
    mock_client = _mock_matrix_client()
    configured_task = _task("configured1", room_id="test_room")
    reason = "state_unavailable"
    public_message = "Unable to retrieve scheduled tasks."

    async def list_room_tasks(*, room_id: str, **_kwargs: object) -> list[ScheduledTaskRecord]:
        if room_id == "!adhoc:localhost":
            raise ScheduledTaskOperationError(reason, public_message)
        return [configured_task]

    with (
        patch("mindroom.api.schedules.create_agent_user", return_value=_mock_agent_user()),
        patch("mindroom.api.schedules.login_agent_user", return_value=mock_client),
        patch(
            "mindroom.api.schedules.MatrixState.load",
            return_value=MagicMock(router_ad_hoc_room_ids={"!adhoc:localhost"}),
        ),
        patch("mindroom.api.schedules.get_scheduled_tasks_for_room", new=AsyncMock(side_effect=list_room_tasks)),
    ):
        response = test_client.get("/api/schedules")

    assert response.status_code == 200
    assert {task["task_id"] for task in response.json()["tasks"]} == {"configured1"}


def test_list_schedules_surfaces_room_state_failures(test_client: TestClient) -> None:
    """List endpoint should surface room-state failures instead of returning an empty list."""
    mock_client = _mock_matrix_client()

    with (
        patch("mindroom.api.schedules.create_agent_user", return_value=_mock_agent_user()),
        patch("mindroom.api.schedules.login_agent_user", return_value=mock_client),
        patch(
            "mindroom.api.schedules.get_scheduled_tasks_for_room",
            side_effect=ScheduledTaskOperationError("state_unavailable", "Unable to retrieve scheduled tasks."),
        ),
    ):
        response = test_client.get("/api/schedules")

    assert response.status_code == 503
    assert response.json()["detail"] == "Unable to retrieve scheduled tasks."


def test_update_schedule_once_success(test_client: TestClient) -> None:
    """Update endpoint should persist prompt and once schedule changes."""
    mock_client = _mock_matrix_client()
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
        patch("mindroom.api.schedules.create_agent_user", return_value=_mock_agent_user()),
        patch("mindroom.api.schedules.login_agent_user", return_value=mock_client),
        patch(
            "mindroom.api.schedules.resolve_existing_scheduled_task_mutation",
            return_value=_mutation_context(existing_task, writer_client=mock_client),
        ),
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
    save_mock.assert_awaited_once()
    assert save_mock.await_args.kwargs["task_id"] == "abc12345"
    assert save_mock.await_args.kwargs["room_id"] == "test_room"
    assert save_mock.await_args.kwargs["workflow"].new_thread is True


def test_update_schedule_uses_task_writer_candidate_before_router(test_client: TestClient) -> None:
    """Update should honor the shared mutation helper's chosen writer client."""
    task_writer_client = _mock_matrix_client()
    task_writer_client.user_id = "@mindroom_test_agent:localhost"
    existing_task = _task("abc12345", execute_at=datetime(2026, 2, 10, 9, 0, tzinfo=UTC))
    updated_task = _task("abc12345", execute_at=datetime(2026, 3, 1, 10, 0, tzinfo=UTC))

    with (
        patch("mindroom.api.schedules.create_agent_user", return_value=_mock_agent_user()),
        patch("mindroom.api.schedules.login_agent_user", return_value=_mock_matrix_client()),
        patch(
            "mindroom.api.schedules.resolve_existing_scheduled_task_mutation",
            return_value=_mutation_context(
                existing_task,
                writer_client=task_writer_client,
                task_sender_id="@mindroom_test_agent:localhost",
            ),
        ),
        patch(
            "mindroom.api.schedules.save_edited_scheduled_task",
            new=AsyncMock(return_value=updated_task),
        ) as mock_save,
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
    assert mock_save.await_args.kwargs["client"] is task_writer_client


@pytest.mark.asyncio
async def test_update_schedule_does_not_resolve_cache_path_when_not_restarting() -> None:
    """Pure API schedule edits should not construct or resolve an event cache."""
    mock_client = _mock_matrix_client()
    existing_task = _task(
        "abc12345",
        execute_at=datetime(2026, 2, 10, 9, 0, tzinfo=UTC),
        description="Original description",
        message="@mindroom_test_agent original",
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
    )
    updated_task = ScheduledTaskRecord(
        task_id="abc12345",
        room_id="test_room",
        status="pending",
        created_at=existing_task.created_at,
        workflow=updated_workflow,
    )
    runtime_config = MagicMock()
    runtime_config.cache.resolve_db_path.side_effect = AssertionError(
        "update_schedule should not resolve cache paths for state-only edits",
    )
    runtime_config.get_ids.return_value = {
        ROUTER_AGENT_NAME: MagicMock(full_id="@mindroom_router:localhost"),
    }
    runtime_config.agents = {}
    runtime_config.teams = {}
    save_mock = AsyncMock(return_value=updated_task)
    api_request = MagicMock()

    with (
        patch(
            "mindroom.api.schedules.config_lifecycle.read_committed_runtime_config",
            return_value=(runtime_config, MagicMock()),
        ),
        patch("mindroom.api.schedules.resolve_room_aliases", return_value=["test_room"]),
        patch("mindroom.api.schedules.get_room_alias_from_id", return_value=None),
        patch("mindroom.api.schedules.create_agent_user", return_value=_mock_agent_user()),
        patch("mindroom.api.schedules.login_agent_user", return_value=mock_client),
        patch(
            "mindroom.api.schedules.resolve_existing_scheduled_task_mutation",
            return_value=_mutation_context(existing_task, writer_client=mock_client),
        ),
        patch("mindroom.api.schedules.save_edited_scheduled_task", save_mock),
    ):
        response = await update_schedule(
            task_id="abc12345",
            request=UpdateScheduleRequest(
                room_id="test_room",
                schedule_type="once",
                execute_at=datetime(2026, 3, 1, 10, 0, tzinfo=UTC),
                message="@mindroom_test_agent updated",
                description="Updated description",
            ),
            api_request=api_request,
        )

    runtime_config.cache.resolve_db_path.assert_not_called()
    assert response.task_id == "abc12345"


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
        patch(
            "mindroom.api.schedules.resolve_existing_scheduled_task_mutation",
            return_value=_mutation_context(existing_task, writer_client=mock_client),
        ),
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
        patch(
            "mindroom.api.schedules.resolve_existing_scheduled_task_mutation",
            return_value=_mutation_context(existing_task, writer_client=mock_client),
        ),
        patch("mindroom.api.schedules.cancel_scheduled_task", cancel_mock),
    ):
        response = test_client.delete("/api/schedules/abc12345?room_id=test_room")

    assert response.status_code == 200
    assert response.json()["success"] is True
    assert cancel_mock.await_args.kwargs["task_id"] == "abc12345"
    assert cancel_mock.await_args.kwargs["room_id"] == "test_room"


def test_cancel_schedule_uses_task_writer_candidate_before_router(test_client: TestClient) -> None:
    """Cancel should delegate fully to the scheduling helper with runtime context."""
    with (
        patch("mindroom.api.schedules.create_agent_user", return_value=_mock_agent_user()),
        patch("mindroom.api.schedules.login_agent_user", return_value=_mock_matrix_client()),
        patch(
            "mindroom.api.schedules.cancel_scheduled_task",
            new=AsyncMock(return_value="✅ Cancelled task `abc12345`"),
        ) as mock_cancel,
    ):
        response = test_client.delete("/api/schedules/abc12345?room_id=test_room")

    assert response.status_code == 200
    assert mock_cancel.await_args.kwargs["client"].user_id == "@mindroom_router:localhost"
    assert mock_cancel.await_args.kwargs["router_client"].user_id == "@mindroom_router:localhost"
    assert mock_cancel.await_args.kwargs["cancel_in_memory"] is False
    assert mock_cancel.await_args.kwargs["config"] is not None
    assert mock_cancel.await_args.kwargs["runtime_paths"] is not None


def test_update_schedule_surfaces_task_lookup_failure(test_client: TestClient) -> None:
    """Update endpoint should map state-unavailable lookup failures to HTTP 503."""
    mock_client = _mock_matrix_client()

    with (
        patch("mindroom.api.schedules.create_agent_user", return_value=_mock_agent_user()),
        patch("mindroom.api.schedules.login_agent_user", return_value=mock_client),
        patch(
            "mindroom.api.schedules.resolve_existing_scheduled_task_mutation",
            side_effect=ScheduledTaskOperationError("state_unavailable", "Unable to retrieve scheduled task state."),
        ),
    ):
        response = test_client.put("/api/schedules/abc12345", json={"room_id": "test_room"})

    assert response.status_code == 503
    assert response.json()["detail"] == "Unable to retrieve scheduled task state."


def test_cancel_schedule_not_found(test_client: TestClient) -> None:
    """Cancel endpoint should return 404 when task does not exist."""
    mock_client = _mock_matrix_client()
    with (
        patch("mindroom.api.schedules.create_agent_user", return_value=_mock_agent_user()),
        patch("mindroom.api.schedules.login_agent_user", return_value=mock_client),
        patch(
            "mindroom.api.schedules.cancel_scheduled_task",
            side_effect=ScheduledTaskOperationError("not_found", "Task `missing` not found."),
        ),
    ):
        response = test_client.delete("/api/schedules/missing?room_id=test_room")

    assert response.status_code == 404


def test_cancel_schedule_surfaces_task_lookup_failure(test_client: TestClient) -> None:
    """Cancel endpoint should map state-unavailable lookup failures to HTTP 503."""
    mock_client = _mock_matrix_client()

    with (
        patch("mindroom.api.schedules.create_agent_user", return_value=_mock_agent_user()),
        patch("mindroom.api.schedules.login_agent_user", return_value=mock_client),
        patch(
            "mindroom.api.schedules.cancel_scheduled_task",
            side_effect=ScheduledTaskOperationError("state_unavailable", "Unable to retrieve scheduled task state."),
        ),
    ):
        response = test_client.delete("/api/schedules/abc12345?room_id=test_room")

    assert response.status_code == 503
    assert response.json()["detail"] == "Unable to retrieve scheduled task state."


def test_cancel_schedule_surfaces_writer_failure(test_client: TestClient) -> None:
    """Cancel endpoint should convert cancellation failures into HTTP 400 details."""
    mock_client = _mock_matrix_client()
    existing_task = _task("abc12345", execute_at=datetime(2026, 2, 10, 9, 0, tzinfo=UTC))

    with (
        patch("mindroom.api.schedules.create_agent_user", return_value=_mock_agent_user()),
        patch("mindroom.api.schedules.login_agent_user", return_value=mock_client),
        patch(
            "mindroom.api.schedules.resolve_existing_scheduled_task_mutation",
            return_value=_mutation_context(existing_task, writer_client=mock_client),
        ),
        patch(
            "mindroom.api.schedules.cancel_scheduled_task",
            side_effect=ScheduledTaskOperationError(
                "insufficient_power",
                "Ask a room admin to grant a joined MindRoom bot enough power to manage scheduled tasks, then retry.",
            ),
        ),
    ):
        response = test_client.delete("/api/schedules/abc12345?room_id=test_room")

    assert response.status_code == 400
    assert "grant a joined MindRoom bot enough power" in response.json()["detail"]


def test_cancel_schedule_surfaces_writer_unavailable_as_503(test_client: TestClient) -> None:
    """Cancel endpoint should treat retryable writer resolution failures as service unavailable."""
    mock_client = _mock_matrix_client()
    existing_task = _task("abc12345", execute_at=datetime(2026, 2, 10, 9, 0, tzinfo=UTC))

    with (
        patch("mindroom.api.schedules.create_agent_user", return_value=_mock_agent_user()),
        patch("mindroom.api.schedules.login_agent_user", return_value=mock_client),
        patch(
            "mindroom.api.schedules.resolve_existing_scheduled_task_mutation",
            return_value=_mutation_context(existing_task, writer_client=mock_client),
        ),
        patch(
            "mindroom.api.schedules.cancel_scheduled_task",
            side_effect=ScheduledTaskOperationError(
                "writer_unavailable",
                "MindRoom could not determine which joined bot should manage scheduled tasks. Retry in a moment.",
            ),
        ),
    ):
        response = test_client.delete("/api/schedules/abc12345?room_id=test_room")

    assert response.status_code == 503
    assert "Retry in a moment" in response.json()["detail"]


def test_cancel_schedule_surfaces_permission_denied_as_403(test_client: TestClient) -> None:
    """Permission-denied scheduling failures should map to HTTP 403."""
    mock_client = _mock_matrix_client()

    with (
        patch("mindroom.api.schedules.create_agent_user", return_value=_mock_agent_user()),
        patch("mindroom.api.schedules.login_agent_user", return_value=mock_client),
        patch(
            "mindroom.api.schedules.cancel_scheduled_task",
            side_effect=ScheduledTaskOperationError(
                "permission_denied",
                "Only the task creator, someone operating in the same thread, or a room admin can manage this scheduled task.",
            ),
        ),
    ):
        response = test_client.delete("/api/schedules/abc12345?room_id=test_room")

    assert response.status_code == 403
    assert "same thread" in response.json()["detail"]


def test_update_schedule_once_to_cron(test_client: TestClient) -> None:
    """Switching from once to cron is rejected by the API."""
    mock_client = _mock_matrix_client()
    existing_task = _task(
        "switch01",
        execute_at=datetime(2026, 2, 10, 9, 0, tzinfo=UTC),
        thread_id=None,
        new_thread=True,
    )
    save_mock = AsyncMock()

    with (
        patch("mindroom.api.schedules.create_agent_user", return_value=_mock_agent_user()),
        patch("mindroom.api.schedules.login_agent_user", return_value=mock_client),
        patch(
            "mindroom.api.schedules.resolve_existing_scheduled_task_mutation",
            return_value=_mutation_context(existing_task, writer_client=mock_client),
        ),
        patch("mindroom.api.schedules.save_edited_scheduled_task", save_mock),
    ):
        response = test_client.put(
            "/api/schedules/switch01",
            json={
                "room_id": "test_room",
                "schedule_type": "cron",
                "cron_expression": "30 8 * * 1-5",
            },
        )

    assert response.status_code == 400
    assert "Changing schedule_type is not supported" in response.json()["detail"]
    save_mock.assert_not_awaited()


def test_update_schedule_cron_to_once(test_client: TestClient) -> None:
    """Switching from cron to once is rejected by the API."""
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
        patch(
            "mindroom.api.schedules.resolve_existing_scheduled_task_mutation",
            return_value=_mutation_context(existing_task, writer_client=mock_client),
        ),
        patch("mindroom.api.schedules.save_edited_scheduled_task", save_mock),
    ):
        response = test_client.put(
            "/api/schedules/switch02",
            json={
                "room_id": "test_room",
                "schedule_type": "once",
                "execute_at": "2026-04-01T12:00:00Z",
            },
        )

    assert response.status_code == 400
    assert "Changing schedule_type is not supported" in response.json()["detail"]
    save_mock.assert_not_awaited()


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
        patch(
            "mindroom.api.schedules.resolve_existing_scheduled_task_mutation",
            return_value=_mutation_context(existing_task, writer_client=mock_client),
        ),
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
        patch(
            "mindroom.api.schedules.resolve_existing_scheduled_task_mutation",
            return_value=_mutation_context(existing_task, writer_client=mock_client),
        ),
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

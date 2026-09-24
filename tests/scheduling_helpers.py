"""Test helpers for runtime-authored scheduled-task Matrix state."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import nio

from mindroom.constants import ROUTER_AGENT_NAME, resolve_runtime_paths
from mindroom.matrix.identity import MatrixID, managed_account_key
from mindroom.matrix.state import MatrixState

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.constants import RuntimePaths

SCHEDULE_WRITER_ID = "@mindroom_router:server"
SCHEDULED_TASK_EVENT_TYPE = "com.mindroom.scheduled.task"


def persist_schedule_writer(runtime_paths: RuntimePaths, writer_id: str = SCHEDULE_WRITER_ID) -> None:
    """Persist the router account as ``writer_id`` so its schedule state counts as runtime-authored."""
    matrix_id = MatrixID.parse(writer_id)
    state = MatrixState.load(runtime_paths=runtime_paths)
    state.add_account(
        managed_account_key(ROUTER_AGENT_NAME),
        matrix_id.username,
        "mock_test_password",
        domain=matrix_id.domain,
    )
    state.save(runtime_paths=runtime_paths)


def schedule_runtime_paths(tmp_path: Path, writer_id: str = SCHEDULE_WRITER_ID) -> RuntimePaths:
    """Return isolated runtime paths whose persisted router account authors schedule state."""
    runtime_paths = resolve_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path / "storage",
        process_env={},
    )
    persist_schedule_writer(runtime_paths, writer_id)
    return runtime_paths


def scheduled_task_event(
    task_id: str,
    content: dict[str, Any],
    *,
    room_id: str = "!test:server",
    sender: str = SCHEDULE_WRITER_ID,
) -> dict[str, Any]:
    """Return one full scheduled-task state event as ``room_get_state`` reports it."""
    return {
        "type": SCHEDULED_TASK_EVENT_TYPE,
        "state_key": task_id,
        "content": content,
        "sender": sender,
        "event_id": f"$state_{task_id}",
        "origin_server_ts": 1234567890,
        "room_id": room_id,
    }


def scheduled_task_state_response(
    room_id: str,
    tasks: dict[str, dict[str, Any]],
    *,
    sender: str = SCHEDULE_WRITER_ID,
) -> nio.RoomGetStateResponse:
    """Return room state holding one runtime-authored event per task ID."""
    events = [
        scheduled_task_event(task_id, content, room_id=room_id, sender=sender) for task_id, content in tasks.items()
    ]
    return nio.RoomGetStateResponse.from_dict(events, room_id=room_id)


def joined_member_state(
    room_id: str,
    event_type: str,
    state_key: str = "",
) -> nio.RoomGetStateEventResponse:
    """Report every requested member as joined, for runners that check creator membership."""
    assert event_type == "m.room.member"
    return nio.RoomGetStateEventResponse({"membership": "join"}, event_type, state_key, room_id)

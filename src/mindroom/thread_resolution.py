"""Thread resolution state management via Matrix room state events."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import cast

import nio

from mindroom.matrix.event_info import EventInfo

THREAD_RESOLUTION_EVENT_TYPE = "com.mindroom.thread.resolution"
POWER_LEVELS_EVENT_TYPE = "m.room.power_levels"
RESOLVED_STATUS = "resolved"
DEFAULT_STATE_EVENT_POWER_LEVEL = 50
DEFAULT_USER_POWER_LEVEL = 0
MAX_THREAD_ROOT_NORMALIZATION_DEPTH = 500


class ThreadResolutionError(RuntimeError):
    """Raised when thread resolution state cannot be written."""


@dataclass(frozen=True)
class ThreadResolutionRecord:
    """Parsed thread resolution state for one room/thread root."""

    room_id: str
    thread_root_id: str
    status: str
    resolved_by: str
    resolved_at: datetime
    updated_at: datetime

    @property
    def is_resolved(self) -> bool:
        """Return whether this record marks the thread as resolved."""
        return self.status == RESOLVED_STATUS


def _parse_timestamp(value: object) -> datetime | None:
    """Parse an ISO-8601 timestamp into an aware datetime."""
    if not isinstance(value, str) or not value:
        return None

    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None

    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed


def _parse_power_level(value: object) -> int | None:
    """Return one Matrix power level value when it is a real integer."""
    if type(value) is not int:
        return None
    return value


def _normalize_non_empty_string(value: object) -> str | None:
    """Return a stripped non-empty string."""
    if not isinstance(value, str):
        return None

    normalized_value = value.strip()
    if not normalized_value:
        return None
    return normalized_value


def _require_non_empty_string(value: object, *, field_name: str) -> str:
    """Require a stripped non-empty string value."""
    normalized_value = _normalize_non_empty_string(value)
    if normalized_value is None:
        msg = f"{field_name} must be a non-empty string."
        raise ThreadResolutionError(msg)
    return normalized_value


def _next_related_event_id(current_event_id: str, event_info: EventInfo) -> str | None:
    """Return the next event to inspect while normalizing a thread root."""
    # Follow the authoritative relation target for edits instead of trusting
    # thread metadata copied into m.new_content.
    related_event_ids = [
        event_info.thread_id,
        event_info.safe_thread_root,
        event_info.reply_to_event_id,
    ]

    for related_event_id in related_event_ids:
        normalized_related_event_id = _normalize_non_empty_string(related_event_id)
        if normalized_related_event_id is None or normalized_related_event_id == current_event_id:
            continue
        return normalized_related_event_id
    return None


def _parse_thread_resolution_record(
    room_id: str,
    thread_root_id: str,
    content: Mapping[str, object],
) -> ThreadResolutionRecord | None:
    """Parse one thread resolution payload from Matrix state."""
    if not content:
        return None

    content_thread_root_id = content.get("thread_root_id")
    status = content.get("status")
    resolved_by = content.get("resolved_by")
    resolved_at = _parse_timestamp(content.get("resolved_at"))
    updated_at = _parse_timestamp(content.get("updated_at"))

    if content_thread_root_id != thread_root_id:
        return None
    if status != RESOLVED_STATUS:
        return None
    if not isinstance(resolved_by, str) or not resolved_by:
        return None
    if resolved_at is None or updated_at is None:
        return None

    return ThreadResolutionRecord(
        room_id=room_id,
        thread_root_id=thread_root_id,
        status=RESOLVED_STATUS,
        resolved_by=resolved_by,
        resolved_at=resolved_at,
        updated_at=updated_at,
    )


def _resolved_payload(
    thread_root_id: str,
    *,
    resolved_by: str,
    updated_at: datetime,
) -> dict[str, object]:
    """Build the canonical resolved-state payload."""
    timestamp = updated_at.isoformat()
    return {
        "thread_root_id": thread_root_id,
        "status": RESOLVED_STATUS,
        "resolved_by": resolved_by,
        "resolved_at": timestamp,
        "updated_at": timestamp,
    }


def _required_state_event_power_level(
    power_levels_content: Mapping[str, object],
    *,
    event_type: str,
) -> int:
    """Return the power level required to send one state event type."""
    events = power_levels_content.get("events")
    if isinstance(events, Mapping):
        typed_events = cast("Mapping[str, object]", events)
        event_level = _parse_power_level(typed_events.get(event_type))
        if event_level is not None:
            return event_level

    state_default = _parse_power_level(power_levels_content.get("state_default"))
    if state_default is not None:
        return state_default
    return DEFAULT_STATE_EVENT_POWER_LEVEL


def _user_power_level(
    power_levels_content: Mapping[str, object],
    *,
    user_id: str,
) -> int:
    """Return the current user's effective Matrix power level for one room."""
    users = power_levels_content.get("users")
    if isinstance(users, Mapping):
        typed_users = cast("Mapping[str, object]", users)
        user_level = _parse_power_level(typed_users.get(user_id))
        if user_level is not None:
            return user_level

    users_default = _parse_power_level(power_levels_content.get("users_default"))
    if users_default is not None:
        return users_default
    return DEFAULT_USER_POWER_LEVEL


def _raise_insufficient_power_level(
    room_id: str,
    *,
    subject_label: str,
    user_id: str,
    user_power_level: int,
    required_power_level: int,
) -> None:
    """Raise one consistent insufficient-power error."""
    msg = (
        f"Insufficient Matrix power level for {subject_label} to send {THREAD_RESOLUTION_EVENT_TYPE} "
        f"state events in {room_id}: {user_id} has {user_power_level}, requires {required_power_level}."
    )
    raise ThreadResolutionError(msg)


def _assert_user_can_write_thread_resolution(
    power_levels_content: Mapping[str, object],
    room_id: str,
    *,
    subject_label: str,
    user_id: str,
) -> None:
    """Assert one Matrix user can send the thread-resolution state event."""
    required_power_level = _required_state_event_power_level(
        power_levels_content,
        event_type=THREAD_RESOLUTION_EVENT_TYPE,
    )
    user_power_level = _user_power_level(
        power_levels_content,
        user_id=user_id,
    )
    if user_power_level >= required_power_level:
        return
    _raise_insufficient_power_level(
        room_id,
        subject_label=subject_label,
        user_id=user_id,
        user_power_level=user_power_level,
        required_power_level=required_power_level,
    )


async def _get_thread_resolution_state_content(
    client: nio.AsyncClient,
    room_id: str,
    thread_root_id: str,
) -> dict[str, object] | None:
    """Fetch one raw thread-resolution state payload."""
    response = await client.room_get_state_event(
        room_id=room_id,
        event_type=THREAD_RESOLUTION_EVENT_TYPE,
        state_key=thread_root_id,
    )
    if isinstance(response, nio.RoomGetStateEventError):
        if response.status_code == "M_NOT_FOUND":
            return None
        msg = f"Failed to fetch thread resolution state for {thread_root_id} in {room_id}: {response}"
        raise ThreadResolutionError(msg)
    if not isinstance(response, nio.RoomGetStateEventResponse):
        msg = f"Failed to fetch thread resolution state for {thread_root_id} in {room_id}: {response}"
        raise ThreadResolutionError(msg)
    if not isinstance(response.content, dict):
        return None
    return response.content


async def _assert_thread_resolution_write_allowed(
    client: nio.AsyncClient,
    room_id: str,
    *,
    requester_user_id: str | None = None,
) -> None:
    """Fail fast when the current Matrix account lacks state-event power."""
    actor_user_id = _require_non_empty_string(client.user_id, field_name="client.user_id")

    response = await client.room_get_state_event(
        room_id=room_id,
        event_type=POWER_LEVELS_EVENT_TYPE,
    )
    if not isinstance(response, nio.RoomGetStateEventResponse):
        msg = f"Failed to fetch Matrix power levels for {room_id}: {response}"
        raise ThreadResolutionError(msg)
    if not isinstance(response.content, dict):
        msg = f"Failed to parse Matrix power levels for {room_id}: {response.content!r}"
        raise ThreadResolutionError(msg)

    _assert_user_can_write_thread_resolution(
        response.content,
        room_id,
        subject_label="the Matrix client",
        user_id=actor_user_id,
    )
    if requester_user_id is None:
        return

    normalized_requester_user_id = _require_non_empty_string(
        requester_user_id,
        field_name="requester_user_id",
    )
    if normalized_requester_user_id == actor_user_id:
        return

    _assert_user_can_write_thread_resolution(
        response.content,
        room_id,
        subject_label="the requester",
        user_id=normalized_requester_user_id,
    )


async def normalize_thread_root_event_id(
    client: nio.AsyncClient,
    room_id: str,
    event_id: str,
) -> str | None:
    """Resolve a room event or related reply into the canonical thread root ID."""
    current_event_id = _normalize_non_empty_string(event_id)
    if not current_event_id:
        return None

    seen_event_ids: set[str] = set()

    while current_event_id:
        if len(seen_event_ids) >= MAX_THREAD_ROOT_NORMALIZATION_DEPTH:
            break
        if current_event_id in seen_event_ids:
            break
        seen_event_ids.add(current_event_id)

        response = await client.room_get_event(room_id, current_event_id)
        if not isinstance(response, nio.RoomGetEventResponse):
            return None

        event_info = EventInfo.from_event(response.event.source)
        next_event_id = _next_related_event_id(current_event_id, event_info)
        if next_event_id is None:
            if event_info.has_relations:
                return None
            return current_event_id
        current_event_id = next_event_id

    return None


async def get_thread_resolution(
    client: nio.AsyncClient,
    room_id: str,
    thread_root_id: str,
) -> ThreadResolutionRecord | None:
    """Fetch the resolution record for one thread root from Matrix state."""
    normalized_thread_root_id = _normalize_non_empty_string(thread_root_id)
    if normalized_thread_root_id is None:
        return None

    content = await _get_thread_resolution_state_content(
        client,
        room_id,
        normalized_thread_root_id,
    )
    if content is None:
        return None
    return _parse_thread_resolution_record(room_id, normalized_thread_root_id, content)


async def set_thread_resolved(
    client: nio.AsyncClient,
    room_id: str,
    thread_root_id: str,
    resolved_by: str,
) -> ThreadResolutionRecord:
    """Persist a resolved marker for one thread root."""
    normalized_thread_root_id = _require_non_empty_string(
        thread_root_id,
        field_name="thread_root_id",
    )
    normalized_resolved_by = _require_non_empty_string(
        resolved_by,
        field_name="resolved_by",
    )
    await _assert_thread_resolution_write_allowed(
        client,
        room_id,
        requester_user_id=normalized_resolved_by,
    )
    updated_at = datetime.now(UTC)
    content = _resolved_payload(
        normalized_thread_root_id,
        resolved_by=normalized_resolved_by,
        updated_at=updated_at,
    )
    response = await client.room_put_state(
        room_id=room_id,
        event_type=THREAD_RESOLUTION_EVENT_TYPE,
        content=content,
        state_key=normalized_thread_root_id,
    )
    if not isinstance(response, nio.RoomPutStateResponse):
        msg = f"Failed to write thread resolution state for {normalized_thread_root_id} in {room_id}: {response}"
        raise ThreadResolutionError(msg)

    record = _parse_thread_resolution_record(room_id, normalized_thread_root_id, content)
    if record is None:
        msg = f"Failed to parse thread resolution state for {normalized_thread_root_id} in {room_id} after writing it."
        raise ThreadResolutionError(msg)
    return record


async def clear_thread_resolution(
    client: nio.AsyncClient,
    room_id: str,
    thread_root_id: str,
    *,
    requester_user_id: str | None = None,
) -> None:
    """Clear a resolved marker by writing empty content to the same state key."""
    normalized_thread_root_id = _require_non_empty_string(
        thread_root_id,
        field_name="thread_root_id",
    )
    await _assert_thread_resolution_write_allowed(
        client,
        room_id,
        requester_user_id=requester_user_id,
    )
    existing_content = await _get_thread_resolution_state_content(
        client,
        room_id,
        normalized_thread_root_id,
    )
    if not existing_content:
        msg = f"No thread resolution state exists for {normalized_thread_root_id} in {room_id}."
        raise ThreadResolutionError(msg)

    response = await client.room_put_state(
        room_id=room_id,
        event_type=THREAD_RESOLUTION_EVENT_TYPE,
        content={},
        state_key=normalized_thread_root_id,
    )
    if not isinstance(response, nio.RoomPutStateResponse):
        msg = f"Failed to clear thread resolution state for {normalized_thread_root_id} in {room_id}: {response}"
        raise ThreadResolutionError(msg)


async def list_resolved_threads(
    client: nio.AsyncClient,
    room_id: str,
) -> dict[str, ThreadResolutionRecord]:
    """Return all currently resolved thread markers for a room."""
    response = await client.room_get_state(room_id)
    if not isinstance(response, nio.RoomGetStateResponse):
        return {}

    resolved_threads: dict[str, ThreadResolutionRecord] = {}
    for event in response.events:
        if event.get("type") != THREAD_RESOLUTION_EVENT_TYPE:
            continue

        state_key = event.get("state_key")
        content = event.get("content")
        if not isinstance(state_key, str) or not isinstance(content, dict):
            continue

        record = _parse_thread_resolution_record(room_id, state_key, content)
        if record is None:
            continue
        resolved_threads[state_key] = record

    return resolved_threads

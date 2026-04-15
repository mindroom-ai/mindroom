"""Thread tag state management via Matrix room state events."""

from __future__ import annotations

import json
import math
import re
from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, cast

import nio
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from mindroom.matrix.event_info import EventInfo
from mindroom.matrix.thread_membership import (
    ThreadMembershipAccess,
    resolve_event_thread_id,
    room_scan_thread_membership_access_for_client,
)

if TYPE_CHECKING:
    from mindroom.matrix.conversation_cache import ConversationCacheProtocol

THREAD_TAGS_EVENT_TYPE = "com.mindroom.thread.tags"
POWER_LEVELS_EVENT_TYPE = "m.room.power_levels"
DEFAULT_STATE_EVENT_POWER_LEVEL = 50
DEFAULT_USER_POWER_LEVEL = 0
MAX_THREAD_TAG_WRITE_ATTEMPTS = 3
_TAG_NAME_RE = re.compile(r"^[a-z0-9-]{1,50}$")
_PRIORITY_LEVELS = frozenset({"high", "medium", "low"})

# ARCHITECTURE DECISION: One State Event Per Thread Tag
#
# Each `(thread_root_id, tag)` pair is stored as its own
# `com.mindroom.thread.tags` state event.
# The state key is a JSON array `[thread_root_id, tag]`.
#
# This avoids sibling-tag clobbering under Matrix's last-writer-wins state
# semantics because updating `resolved` no longer rewrites `blocked`,
# `priority`, or any other sibling tag state keys.
#
# Reads still understand the earlier single-event-per-thread payload that used
# the same `com.mindroom.thread.tags` event type during this feature's
# development, but this intentionally does not read the removed
# `com.mindroom.thread.resolution` event type.


class ThreadTagsError(RuntimeError):
    """Raised when thread tag state cannot be read or written."""


class ThreadTagRecord(BaseModel):
    """One tag payload stored for one thread."""

    model_config = ConfigDict(extra="ignore")

    set_by: str
    set_at: datetime
    note: str | None = None
    data: dict[str, Any] = Field(default_factory=dict)

    @field_validator("set_by")
    @classmethod
    def _validate_set_by(cls, value: str) -> str:
        normalized_value = value.strip()
        if not normalized_value:
            msg = "set_by must be a non-empty string."
            raise ValueError(msg)
        return normalized_value

    @field_validator("set_at")
    @classmethod
    def _normalize_set_at(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value

    @field_validator("note", mode="before")
    @classmethod
    def _normalize_note(cls, value: object) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str):
            msg = "note must be a string."
            raise TypeError(msg)

        normalized_value = value.strip()
        if not normalized_value:
            return None
        return normalized_value

    @field_validator("data", mode="before")
    @classmethod
    def _normalize_data(cls, value: object) -> dict[str, Any]:
        if value is None:
            return {}
        return _normalize_object_mapping(value, error_type=TypeError)


class ThreadTagsState(BaseModel):
    """All valid tags stored for one thread root."""

    model_config = ConfigDict(extra="ignore")

    room_id: str
    thread_root_id: str
    tags: dict[str, ThreadTagRecord] = Field(default_factory=dict)


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


def _canonical_thread_id(event_info: EventInfo, *, fallback_root: str | None = None) -> str | None:
    """Return the explicit or canonical root thread ID represented by one event."""
    if event_info.thread_id is not None:
        return event_info.thread_id
    if event_info.thread_id_from_edit is not None:
        return event_info.thread_id_from_edit
    if fallback_root is not None and event_info.can_be_thread_root:
        return fallback_root
    return None


async def _event_info_for_event_id(
    client: nio.AsyncClient,
    room_id: str,
    event_id: str,
) -> EventInfo | None:
    """Fetch one event and return parsed relation metadata when available."""
    response = await client.room_get_event(room_id, event_id)
    if not isinstance(response, nio.RoomGetEventResponse):
        return None
    return EventInfo.from_event(response.event.source)


def _require_non_empty_string(value: object, *, field_name: str) -> str:
    """Require a stripped non-empty string value."""
    normalized_value = _normalize_non_empty_string(value)
    if normalized_value is None:
        msg = f"{field_name} must be a non-empty string."
        raise ThreadTagsError(msg)
    return normalized_value


def normalize_tag_name(tag: object) -> str:
    """Normalize and validate one thread tag name."""
    normalized_tag = _normalize_non_empty_string(tag)
    if normalized_tag is None:
        msg = "tag must be a non-empty string."
        raise ThreadTagsError(msg)

    normalized_tag = normalized_tag.lower()
    if not _TAG_NAME_RE.fullmatch(normalized_tag):
        msg = "tag must be 1-50 chars of lowercase letters, digits, or hyphens."
        raise ThreadTagsError(msg)
    return normalized_tag


def _normalize_blocked_by(value: object) -> list[str]:
    """Validate a blocked-by list."""
    if not isinstance(value, list):
        msg = "blocked.data.blocked_by must be a list of strings."
        raise ThreadTagsError(msg)

    normalized_values: list[str] = []
    for item in value:
        normalized_item = _normalize_non_empty_string(item)
        if normalized_item is None:
            msg = "blocked.data.blocked_by must be a list of strings."
            raise ThreadTagsError(msg)
        normalized_values.append(normalized_item)
    return normalized_values


def _normalize_object_mapping(
    value: object,
    *,
    error_type: type[Exception],
) -> dict[str, Any]:
    """Validate one JSON-like object payload with string keys."""
    if not isinstance(value, Mapping):
        msg = "data must be an object."
        raise error_type(msg)

    normalized_data: dict[str, Any] = {}
    for key, item in value.items():
        if not isinstance(key, str):
            msg = "data must be an object."
            raise error_type(msg)
        normalized_data[key] = _normalize_json_compatible_value(item, error_type=error_type)
    return normalized_data


def _normalize_json_compatible_value(
    value: object,
    *,
    error_type: type[Exception],
) -> object:
    """Validate one JSON-compatible nested value."""
    if value is None or isinstance(value, str | bool | int):
        return value
    if isinstance(value, float):
        if math.isfinite(value):
            return value
        msg = "data values must be finite JSON-compatible numbers."
        raise error_type(msg)
    if isinstance(value, list):
        return [_normalize_json_compatible_value(item, error_type=error_type) for item in value]
    if isinstance(value, Mapping):
        return _normalize_object_mapping(value, error_type=error_type)

    msg = "data values must be JSON-compatible."
    raise error_type(msg)


def _normalize_blocked_tag_data(normalized_data: dict[str, Any]) -> None:
    """Normalize the predefined blocked-tag schema."""
    if "blocked_by" not in normalized_data:
        return
    normalized_data["blocked_by"] = _normalize_blocked_by(normalized_data["blocked_by"])


def _normalize_waiting_tag_data(normalized_data: dict[str, Any]) -> None:
    """Normalize the predefined waiting-tag schema."""
    if "waiting_on" not in normalized_data:
        return

    waiting_on = _normalize_non_empty_string(normalized_data["waiting_on"])
    if waiting_on is None:
        msg = "waiting.data.waiting_on must be a non-empty string."
        raise ThreadTagsError(msg)
    normalized_data["waiting_on"] = waiting_on


def _normalize_priority_tag_data(normalized_data: dict[str, Any]) -> None:
    """Normalize the predefined priority-tag schema."""
    if "level" not in normalized_data:
        return

    priority_level = _normalize_non_empty_string(normalized_data["level"])
    if priority_level is None:
        msg = "priority.data.level must be one of: high, medium, low."
        raise ThreadTagsError(msg)
    normalized_level = priority_level.lower()
    if normalized_level not in _PRIORITY_LEVELS:
        msg = "priority.data.level must be one of: high, medium, low."
        raise ThreadTagsError(msg)
    normalized_data["level"] = normalized_level


def _normalize_due_tag_data(normalized_data: dict[str, Any]) -> None:
    """Normalize the predefined due-tag schema."""
    if "deadline" not in normalized_data:
        return

    deadline = _parse_timestamp(normalized_data["deadline"])
    if deadline is None:
        msg = "due.data.deadline must be an ISO-8601 timestamp."
        raise ThreadTagsError(msg)
    normalized_data["deadline"] = deadline.isoformat()


_PREDEFINED_TAG_DATA_NORMALIZERS: dict[str, Callable[[dict[str, Any]], None]] = {
    "blocked": _normalize_blocked_tag_data,
    "waiting": _normalize_waiting_tag_data,
    "priority": _normalize_priority_tag_data,
    "due": _normalize_due_tag_data,
}


def _normalize_tag_data(
    tag: str,
    data: Mapping[str, Any] | object | None,
) -> dict[str, Any]:
    """Normalize predefined tag payloads and validate their schema."""
    if data is None:
        normalized_data: dict[str, Any] = {}
    else:
        normalized_data = _normalize_object_mapping(data, error_type=ThreadTagsError)

    normalizer = _PREDEFINED_TAG_DATA_NORMALIZERS.get(tag)
    if normalizer is not None:
        normalizer(normalized_data)

    return normalized_data


def _parse_thread_tag_record(
    tag: str,
    value: object,
) -> ThreadTagRecord | None:
    """Parse one persisted tag payload and drop malformed entries."""
    try:
        normalized_tag = normalize_tag_name(tag)
    except ThreadTagsError:
        return None

    try:
        record = ThreadTagRecord.model_validate(value)
        normalized_data = _normalize_tag_data(normalized_tag, record.data)
    except (ThreadTagsError, ValidationError, TypeError, ValueError):
        return None

    return record.model_copy(update={"data": normalized_data})


def _parse_thread_tags_state(
    room_id: str,
    thread_root_id: str,
    content: Mapping[str, object],
) -> ThreadTagsState | None:
    """Parse one thread-tag payload from Matrix state."""
    raw_tags = content.get("tags")
    if not isinstance(raw_tags, Mapping):
        return None

    parsed_tags: dict[str, ThreadTagRecord] = {}
    for raw_tag, raw_value in raw_tags.items():
        if not isinstance(raw_tag, str):
            continue
        record = _parse_thread_tag_record(raw_tag, raw_value)
        if record is None:
            continue
        parsed_tags[normalize_tag_name(raw_tag)] = record

    if not parsed_tags:
        return None

    return ThreadTagsState(
        room_id=room_id,
        thread_root_id=thread_root_id,
        tags=parsed_tags,
    )


def _thread_tag_state_key(thread_root_id: str, tag: str) -> str:
    """Build one canonical per-tag state key."""
    return json.dumps([thread_root_id, tag], separators=(",", ":"))


def _parse_thread_tag_state_key(state_key: object) -> tuple[str, str] | None:
    """Parse one per-tag state key into its thread root and tag name."""
    if not isinstance(state_key, str):
        return None

    try:
        parsed = json.loads(state_key)
    except (json.JSONDecodeError, TypeError):
        return None

    if not isinstance(parsed, list) or len(parsed) != 2:
        return None

    thread_root_id = _normalize_non_empty_string(parsed[0])
    if thread_root_id is None:
        return None

    try:
        tag = normalize_tag_name(parsed[1])
    except ThreadTagsError:
        return None
    return thread_root_id, tag


def _thread_tags_state_from_tags(
    room_id: str,
    thread_root_id: str,
    tags: Mapping[str, ThreadTagRecord],
) -> ThreadTagsState | None:
    """Build one parsed state result when at least one tag survives."""
    if not tags:
        return None
    return ThreadTagsState(
        room_id=room_id,
        thread_root_id=thread_root_id,
        tags=dict(sorted(tags.items())),
    )


def _collect_thread_tag_room_state_event(
    room_id: str,
    event: Mapping[str, object],
    *,
    legacy_tags_by_thread: dict[str, dict[str, ThreadTagRecord]],
    per_tag_records_by_thread: dict[str, dict[str, ThreadTagRecord]],
    per_tag_tombstones_by_thread: dict[str, set[str]],
) -> None:
    """Parse one room-state event into either legacy or per-tag storage."""
    if event.get("type") != THREAD_TAGS_EVENT_TYPE:
        return

    state_key = event.get("state_key")
    content = event.get("content")
    _collect_thread_tag_state_entry(
        room_id,
        state_key,
        content,
        legacy_tags_by_thread=legacy_tags_by_thread,
        per_tag_records_by_thread=per_tag_records_by_thread,
        per_tag_tombstones_by_thread=per_tag_tombstones_by_thread,
    )


def _collect_thread_tag_state_entry(
    room_id: str,
    state_key: object,
    content: object,
    *,
    legacy_tags_by_thread: dict[str, dict[str, ThreadTagRecord]],
    per_tag_records_by_thread: dict[str, dict[str, ThreadTagRecord]],
    per_tag_tombstones_by_thread: dict[str, set[str]],
) -> None:
    """Parse one thread-tag state entry from either room state or hook state maps."""
    if not isinstance(content, Mapping):
        return
    typed_content = cast("Mapping[str, object]", content)

    parsed_state_key = _parse_thread_tag_state_key(state_key)
    if parsed_state_key is None:
        if not isinstance(state_key, str):
            return

        legacy_state = _parse_thread_tags_state(room_id, state_key, typed_content)
        if legacy_state is not None:
            legacy_tags_by_thread[legacy_state.thread_root_id] = dict(legacy_state.tags)
        return

    thread_root_id, tag = parsed_state_key
    if not typed_content:
        per_tag_tombstones_by_thread.setdefault(thread_root_id, set()).add(tag)
        return

    record = _parse_thread_tag_record(tag, typed_content)
    if record is not None:
        per_tag_records_by_thread.setdefault(thread_root_id, {})[tag] = record


def _merge_thread_tag_room_state(
    room_id: str,
    *,
    legacy_tags_by_thread: Mapping[str, Mapping[str, ThreadTagRecord]],
    per_tag_records_by_thread: Mapping[str, Mapping[str, ThreadTagRecord]],
    per_tag_tombstones_by_thread: Mapping[str, set[str]],
) -> dict[str, ThreadTagsState]:
    """Merge legacy thread payloads with per-tag overrides for one room."""
    merged_states: dict[str, ThreadTagsState] = {}
    for thread_root_id in sorted(
        set(legacy_tags_by_thread) | set(per_tag_records_by_thread) | set(per_tag_tombstones_by_thread),
    ):
        merged_tags = dict(legacy_tags_by_thread.get(thread_root_id, {}))
        for tag in per_tag_tombstones_by_thread.get(thread_root_id, set()):
            merged_tags.pop(tag, None)
        merged_tags.update(per_tag_records_by_thread.get(thread_root_id, {}))

        state = _thread_tags_state_from_tags(
            room_id,
            thread_root_id,
            merged_tags,
        )
        if state is not None:
            merged_states[thread_root_id] = state
    return merged_states


def _thread_tag_record_content(record: ThreadTagRecord) -> dict[str, object]:
    """Build one canonical serialized tag payload for equality checks."""
    return cast("dict[str, object]", record.model_dump(mode="json", exclude_none=True))


def _thread_tag_records_match(
    expected_record: ThreadTagRecord,
    actual_record: ThreadTagRecord | None,
) -> bool:
    """Return whether one persisted tag payload matches the expected write exactly."""
    if actual_record is None:
        return False
    return _thread_tag_record_content(expected_record) == _thread_tag_record_content(actual_record)


def _verified_state_contains_expected_tag(
    verified_state: ThreadTagsState | None,
    *,
    tag: str,
    expected_record: ThreadTagRecord,
) -> bool:
    """Require the verification read to preserve one exact tag payload."""
    if verified_state is None:
        return False
    return _thread_tag_records_match(expected_record, verified_state.tags.get(tag))


def _verified_remove_state_matches(
    verified_state: ThreadTagsState | None,
    *,
    removed_tag: str,
) -> bool:
    """Require a remove verification read to keep the removed tag absent."""
    if verified_state is None:
        return True
    return removed_tag not in verified_state.tags


def _empty_thread_tags_state(room_id: str, thread_root_id: str) -> ThreadTagsState:
    """Build one empty parsed state value for callers that need a concrete result."""
    return ThreadTagsState(
        room_id=room_id,
        thread_root_id=thread_root_id,
        tags={},
    )


async def _put_thread_tag_state(
    client: nio.AsyncClient,
    room_id: str,
    thread_root_id: str,
    tag: str,
    record: ThreadTagRecord | None,
    *,
    error_prefix: str,
) -> None:
    """Write one tag state event and fail on Matrix errors."""
    response = await client.room_put_state(
        room_id=room_id,
        event_type=THREAD_TAGS_EVENT_TYPE,
        content=_thread_tag_record_content(record) if record is not None else {},
        state_key=_thread_tag_state_key(thread_root_id, tag),
    )
    if isinstance(response, nio.RoomPutStateResponse):
        return

    msg = f"{error_prefix} for {thread_root_id} tag {tag!r} in {room_id}: {response}"
    raise ThreadTagsError(msg)


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
        f"Insufficient Matrix power level for {subject_label} to send {THREAD_TAGS_EVENT_TYPE} "
        f"state events in {room_id}: {user_id} has {user_power_level}, requires {required_power_level}."
    )
    raise ThreadTagsError(msg)


async def _assert_requester_joined_room(
    client: nio.AsyncClient,
    room_id: str,
    *,
    requester_user_id: str,
) -> None:
    """Require the requester to be a joined member of the target room."""
    response = await client.joined_members(room_id)
    if not isinstance(response, nio.JoinedMembersResponse):
        msg = f"Failed to verify requester membership for {requester_user_id} in {room_id}: {response}"
        raise ThreadTagsError(msg)

    joined_member_ids = {member.user_id for member in response.members}
    if requester_user_id in joined_member_ids:
        return

    msg = f"Requester is not joined to the target room: {requester_user_id} is not joined to {room_id}."
    raise ThreadTagsError(msg)


def _assert_user_can_write_thread_tags(
    power_levels_content: Mapping[str, object],
    room_id: str,
    *,
    subject_label: str,
    user_id: str,
) -> None:
    """Assert one Matrix user can send the thread-tags state event."""
    required_power_level = _required_state_event_power_level(
        power_levels_content,
        event_type=THREAD_TAGS_EVENT_TYPE,
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


async def _get_room_thread_tags_states(
    client: nio.AsyncClient,
    room_id: str,
) -> dict[str, ThreadTagsState]:
    """Fetch and merge all current thread-tag state for one room."""
    response = await client.room_get_state(room_id)
    if not isinstance(response, nio.RoomGetStateResponse):
        msg = f"Failed to fetch room state for thread tags in {room_id}: {response}"
        raise ThreadTagsError(msg)

    legacy_tags_by_thread: dict[str, dict[str, ThreadTagRecord]] = {}
    per_tag_records_by_thread: dict[str, dict[str, ThreadTagRecord]] = {}
    per_tag_tombstones_by_thread: dict[str, set[str]] = {}
    for event in response.events:
        _collect_thread_tag_room_state_event(
            room_id,
            event,
            legacy_tags_by_thread=legacy_tags_by_thread,
            per_tag_records_by_thread=per_tag_records_by_thread,
            per_tag_tombstones_by_thread=per_tag_tombstones_by_thread,
        )

    return _merge_thread_tag_room_state(
        room_id,
        legacy_tags_by_thread=legacy_tags_by_thread,
        per_tag_records_by_thread=per_tag_records_by_thread,
        per_tag_tombstones_by_thread=per_tag_tombstones_by_thread,
    )


def list_tagged_threads_from_state_map(
    room_id: str,
    room_state: Mapping[object, object],
    *,
    tag: str | None = None,
) -> dict[str, ThreadTagsState]:
    """Return tagged threads from one pre-fetched ``THREAD_TAGS_EVENT_TYPE`` state map."""
    normalized_tag = normalize_tag_name(tag) if tag is not None else None

    legacy_tags_by_thread: dict[str, dict[str, ThreadTagRecord]] = {}
    per_tag_records_by_thread: dict[str, dict[str, ThreadTagRecord]] = {}
    per_tag_tombstones_by_thread: dict[str, set[str]] = {}
    for state_key, content in room_state.items():
        _collect_thread_tag_state_entry(
            room_id,
            state_key,
            content,
            legacy_tags_by_thread=legacy_tags_by_thread,
            per_tag_records_by_thread=per_tag_records_by_thread,
            per_tag_tombstones_by_thread=per_tag_tombstones_by_thread,
        )

    tagged_threads = _merge_thread_tag_room_state(
        room_id,
        legacy_tags_by_thread=legacy_tags_by_thread,
        per_tag_records_by_thread=per_tag_records_by_thread,
        per_tag_tombstones_by_thread=per_tag_tombstones_by_thread,
    )
    if normalized_tag is None:
        return tagged_threads

    return {thread_root_id: state for thread_root_id, state in tagged_threads.items() if normalized_tag in state.tags}


async def _assert_thread_tags_write_allowed(
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
        raise ThreadTagsError(msg)
    if not isinstance(response.content, dict):
        msg = f"Failed to parse Matrix power levels for {room_id}: {response.content!r}"
        raise ThreadTagsError(msg)
    power_levels_content = response.content

    _assert_user_can_write_thread_tags(
        power_levels_content,
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

    await _assert_requester_joined_room(
        client,
        room_id,
        requester_user_id=normalized_requester_user_id,
    )
    _assert_user_can_write_thread_tags(
        power_levels_content,
        room_id,
        subject_label="the requester",
        user_id=normalized_requester_user_id,
    )


async def normalize_thread_root_event_id(
    client: nio.AsyncClient,
    room_id: str,
    event_id: str,
    conversation_cache: ConversationCacheProtocol | None = None,
) -> str | None:
    """Resolve one event ID to an explicit thread root when possible."""
    normalized_event_id = _normalize_non_empty_string(event_id)
    if not normalized_event_id:
        return None

    event_info = await _event_info_for_event_id(client, room_id, normalized_event_id)
    if event_info is None:
        return await _lookup_thread_id_from_cache(conversation_cache, room_id, normalized_event_id)

    normalized_thread_id = _canonical_thread_id(event_info, fallback_root=normalized_event_id)
    if normalized_thread_id is not None:
        return normalized_thread_id
    if event_info.is_edit and event_info.original_event_id is not None:
        original_info = await _event_info_for_event_id(client, room_id, event_info.original_event_id)
        if original_info is not None:
            normalized_original_thread_id = _canonical_thread_id(
                original_info,
                fallback_root=event_info.original_event_id,
            )
            if normalized_original_thread_id is not None:
                return normalized_original_thread_id

    return await resolve_event_thread_id(
        room_id,
        event_info,
        access=_thread_membership_access(
            client,
            conversation_cache=conversation_cache,
        ),
    )


def _thread_membership_access(
    client: nio.AsyncClient,
    *,
    conversation_cache: ConversationCacheProtocol | None,
) -> ThreadMembershipAccess:
    """Return the shared thread-membership accessors for thread-tag normalization."""

    async def lookup_thread_id(lookup_room_id: str, lookup_event_id: str) -> str | None:
        return await _lookup_thread_id_from_cache(
            conversation_cache,
            lookup_room_id,
            lookup_event_id,
        )

    async def fetch_event_info(lookup_room_id: str, lookup_event_id: str) -> EventInfo | None:
        return await _event_info_for_event_id(
            client,
            lookup_room_id,
            lookup_event_id,
        )

    return room_scan_thread_membership_access_for_client(
        client,
        lookup_thread_id=lookup_thread_id,
        fetch_event_info=fetch_event_info,
    )


async def _lookup_thread_id_from_cache(
    conversation_cache: ConversationCacheProtocol | None,
    room_id: str,
    event_id: str,
) -> str | None:
    if conversation_cache is None:
        return None
    return await conversation_cache.get_thread_id_for_event(room_id, event_id)


async def get_thread_tags(
    client: nio.AsyncClient,
    room_id: str,
    thread_root_id: str,
) -> ThreadTagsState | None:
    """Fetch all valid tags for one thread root from Matrix state."""
    normalized_thread_root_id = _normalize_non_empty_string(thread_root_id)
    if normalized_thread_root_id is None:
        return None

    states = await _get_room_thread_tags_states(
        client,
        room_id,
    )
    return states.get(normalized_thread_root_id)


async def set_thread_tag(
    client: nio.AsyncClient,
    room_id: str,
    thread_root_id: str,
    tag: str,
    *,
    set_by: str,
    note: str | None = None,
    data: Mapping[str, Any] | None = None,
) -> ThreadTagsState:
    """Persist one thread tag on a thread root."""
    normalized_thread_root_id = _require_non_empty_string(
        thread_root_id,
        field_name="thread_root_id",
    )
    normalized_tag = normalize_tag_name(tag)
    normalized_set_by = _require_non_empty_string(
        set_by,
        field_name="set_by",
    )
    if note is None:
        normalized_note = None
    elif isinstance(note, str):
        normalized_note = _normalize_non_empty_string(note)
    else:
        msg = "note must be a string."
        raise ThreadTagsError(msg)
    normalized_data = _normalize_tag_data(normalized_tag, data)

    await _assert_thread_tags_write_allowed(
        client,
        room_id,
        requester_user_id=normalized_set_by,
    )

    for _ in range(MAX_THREAD_TAG_WRITE_ATTEMPTS):
        expected_record = ThreadTagRecord(
            set_by=normalized_set_by,
            set_at=datetime.now(UTC),
            note=normalized_note,
            data=normalized_data,
        )

        await _put_thread_tag_state(
            client,
            room_id,
            normalized_thread_root_id,
            normalized_tag,
            expected_record,
            error_prefix="Failed to write thread tags state",
        )

        verified_state = await get_thread_tags(
            client,
            room_id,
            normalized_thread_root_id,
        )
        if _verified_state_contains_expected_tag(
            verified_state,
            tag=normalized_tag,
            expected_record=expected_record,
        ):
            assert verified_state is not None
            return verified_state

    msg = (
        f"Failed to preserve thread tag {normalized_tag!r} for {normalized_thread_root_id} in {room_id} "
        f"after {MAX_THREAD_TAG_WRITE_ATTEMPTS} concurrent-write attempts."
    )
    raise ThreadTagsError(msg)


async def remove_thread_tag(
    client: nio.AsyncClient,
    room_id: str,
    thread_root_id: str,
    tag: str,
    *,
    requester_user_id: str | None = None,
) -> ThreadTagsState:
    """Remove one tag from the persisted thread state."""
    normalized_thread_root_id = _require_non_empty_string(
        thread_root_id,
        field_name="thread_root_id",
    )
    normalized_tag = normalize_tag_name(tag)

    await _assert_thread_tags_write_allowed(
        client,
        room_id,
        requester_user_id=requester_user_id,
    )

    remove_written = False
    for _ in range(MAX_THREAD_TAG_WRITE_ATTEMPTS):
        existing_state = await get_thread_tags(
            client,
            room_id,
            normalized_thread_root_id,
        )
        if existing_state is None:
            if remove_written:
                return _empty_thread_tags_state(room_id, normalized_thread_root_id)
            msg = f"No thread tags state exists for {normalized_thread_root_id} in {room_id}."
            raise ThreadTagsError(msg)
        if normalized_tag not in existing_state.tags:
            if remove_written:
                return existing_state
            msg = f"Thread tag {normalized_tag!r} is not set for {normalized_thread_root_id} in {room_id}."
            raise ThreadTagsError(msg)

        await _put_thread_tag_state(
            client,
            room_id,
            normalized_thread_root_id,
            normalized_tag,
            None,
            error_prefix="Failed to update thread tags state",
        )
        remove_written = True

        verified_state = await get_thread_tags(
            client,
            room_id,
            normalized_thread_root_id,
        )
        if _verified_remove_state_matches(
            verified_state,
            removed_tag=normalized_tag,
        ):
            if verified_state is None:
                return _empty_thread_tags_state(room_id, normalized_thread_root_id)
            return verified_state

    msg = (
        f"Failed to remove thread tag {normalized_tag!r} for {normalized_thread_root_id} in {room_id} "
        f"after {MAX_THREAD_TAG_WRITE_ATTEMPTS} concurrent-write attempts."
    )
    raise ThreadTagsError(msg)


async def _get_thread_tags_via_room_state(
    room_id: str,
    thread_root_id: str,
    *,
    query_room_state: Callable[[str, str, str | None], Awaitable[Mapping[object, object] | None]],
) -> ThreadTagsState | None:
    """Load one thread-tag state through abstract room-state bindings."""
    room_state = await query_room_state(
        room_id,
        THREAD_TAGS_EVENT_TYPE,
        None,
    )
    if room_state is None:
        msg = f"Failed to fetch room state for thread tags in {room_id}."
        raise ThreadTagsError(msg)

    return list_tagged_threads_from_state_map(room_id, room_state).get(thread_root_id)


async def remove_thread_tag_via_room_state(
    room_id: str,
    thread_root_id: str,
    tag: str,
    *,
    query_room_state: Callable[[str, str, str | None], Awaitable[Mapping[object, object] | None]],
    put_room_state: Callable[[str, str, str, dict[str, Any]], Awaitable[bool]],
    expected_record: ThreadTagRecord | None = None,
) -> ThreadTagsState:
    """Remove one tag through abstract room-state bindings with verified reads."""
    normalized_thread_root_id = _require_non_empty_string(
        thread_root_id,
        field_name="thread_root_id",
    )
    normalized_tag = normalize_tag_name(tag)
    state_key = _thread_tag_state_key(normalized_thread_root_id, normalized_tag)

    remove_written = False
    for _ in range(MAX_THREAD_TAG_WRITE_ATTEMPTS):
        existing_state = await _get_thread_tags_via_room_state(
            room_id,
            normalized_thread_root_id,
            query_room_state=query_room_state,
        )
        if existing_state is None:
            if remove_written:
                return _empty_thread_tags_state(room_id, normalized_thread_root_id)
            msg = f"No thread tags state exists for {normalized_thread_root_id} in {room_id}."
            raise ThreadTagsError(msg)

        existing_record = existing_state.tags.get(normalized_tag)
        if existing_record is None:
            if remove_written:
                return existing_state
            msg = f"Thread tag {normalized_tag!r} is not set for {normalized_thread_root_id} in {room_id}."
            raise ThreadTagsError(msg)

        if expected_record is not None and not _thread_tag_records_match(expected_record, existing_record):
            return existing_state

        wrote = await put_room_state(
            room_id,
            THREAD_TAGS_EVENT_TYPE,
            state_key,
            {},
        )
        if not wrote:
            msg = f"Failed to update thread tags state for {normalized_thread_root_id} tag {normalized_tag!r} in {room_id}."
            raise ThreadTagsError(msg)
        remove_written = True

        verified_state = await _get_thread_tags_via_room_state(
            room_id,
            normalized_thread_root_id,
            query_room_state=query_room_state,
        )
        if _verified_remove_state_matches(
            verified_state,
            removed_tag=normalized_tag,
        ):
            if verified_state is None:
                return _empty_thread_tags_state(room_id, normalized_thread_root_id)
            return verified_state

    msg = (
        f"Failed to remove thread tag {normalized_tag!r} for {normalized_thread_root_id} in {room_id} "
        f"after {MAX_THREAD_TAG_WRITE_ATTEMPTS} concurrent-write attempts."
    )
    raise ThreadTagsError(msg)


async def list_tagged_threads(
    client: nio.AsyncClient,
    room_id: str,
    *,
    tag: str | None = None,
) -> dict[str, ThreadTagsState]:
    """Return all currently tagged thread markers for a room."""
    normalized_tag = normalize_tag_name(tag) if tag is not None else None

    tagged_threads = await _get_room_thread_tags_states(client, room_id)
    if normalized_tag is None:
        return tagged_threads

    return {thread_root_id: state for thread_root_id, state in tagged_threads.items() if normalized_tag in state.tags}

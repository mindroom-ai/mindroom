"""Thread tag state management via Matrix room state events."""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Any, cast

import nio
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from mindroom.matrix.reply_chain import canonicalize_related_event_id

THREAD_TAGS_EVENT_TYPE = "com.mindroom.thread.tags"
POWER_LEVELS_EVENT_TYPE = "m.room.power_levels"
DEFAULT_STATE_EVENT_POWER_LEVEL = 50
DEFAULT_USER_POWER_LEVEL = 0
MAX_THREAD_ROOT_NORMALIZATION_DEPTH = 500
MAX_THREAD_TAG_WRITE_ATTEMPTS = 3
_TAG_NAME_RE = re.compile(r"^[a-z0-9-]{1,50}$")
_PRIORITY_LEVELS = frozenset({"high", "medium", "low"})

# ARCHITECTURE DECISION: Single State Event Per Thread
#
# All tags for one thread live in a single Matrix state event
# (`com.mindroom.thread.tags`) keyed by the canonical thread root event ID.
# Set/remove operations therefore do read-merge-write on the full tag map.
#
# CONCURRENCY:
# Concurrent writes to different tags on the same thread can theoretically race
# because Matrix state is last-writer-wins at the event level.
# We accept that tradeoff and mitigate it with verify-after-write plus up to
# `MAX_THREAD_TAG_WRITE_ATTEMPTS` retries.
#
# This is accepted by design because:
# 1. The race window is only the few milliseconds between the merge read and the
#    verification read, so two writers have to hit the same thread at nearly
#    the exact same time.
# 2. Human- and agent-driven tagging is low-frequency enough that the practical
#    collision rate is negligible; this only becomes interesting for bulk
#    automation at far higher write rates.
# 3. The verify-and-retry loop recovers from the common collision cases without
#    introducing a more complex storage shape.
# 4. The main alternative, one state event per tag, would increase room-state
#    volume, add merge-on-read behavior everywhere, and complicate Cinny/UI
#    integration.
#
# If this becomes a real scaling problem, migrate to a one-event-per-tag design.
# See ISSUE-041 for the design discussion that intentionally chose the simpler
# single-event model.


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
    if value is None or isinstance(value, str | bool | int | float):
        return value
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


def _thread_tags_content(tags: Mapping[str, ThreadTagRecord]) -> dict[str, object]:
    """Build the canonical thread-tags event content."""
    if not tags:
        return {}

    return {
        "tags": {tag: record.model_dump(mode="json", exclude_none=True) for tag, record in tags.items()},
    }


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


def _verified_state_preserves_expected_tags(
    verified_state: ThreadTagsState | None,
    *,
    expected_tags: Mapping[str, ThreadTagRecord],
) -> bool:
    """Require the verification read to preserve each expected tag payload exactly."""
    if verified_state is None:
        return not expected_tags
    for tag, expected_record in expected_tags.items():
        if not _thread_tag_records_match(expected_record, verified_state.tags.get(tag)):
            return False
    return True


def _verified_remove_state_matches(
    verified_state: ThreadTagsState | None,
    *,
    removed_tag: str,
    expected_siblings: Mapping[str, ThreadTagRecord],
) -> bool:
    """Require a remove verification read to preserve sibling content exactly."""
    if not _verified_state_preserves_expected_tags(
        verified_state,
        expected_tags=expected_siblings,
    ):
        return False
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


async def _put_thread_tags_state(
    client: nio.AsyncClient,
    room_id: str,
    thread_root_id: str,
    tags: Mapping[str, ThreadTagRecord],
    *,
    error_prefix: str,
) -> None:
    """Write the current thread-tags payload and fail on Matrix errors."""
    response = await client.room_put_state(
        room_id=room_id,
        event_type=THREAD_TAGS_EVENT_TYPE,
        content=_thread_tags_content(tags),
        state_key=thread_root_id,
    )
    if isinstance(response, nio.RoomPutStateResponse):
        return

    msg = f"{error_prefix} for {thread_root_id} in {room_id}: {response}"
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


async def _get_thread_tags_state_content(
    client: nio.AsyncClient,
    room_id: str,
    thread_root_id: str,
) -> dict[str, object] | None:
    """Fetch one raw thread-tags payload."""
    response = await client.room_get_state_event(
        room_id=room_id,
        event_type=THREAD_TAGS_EVENT_TYPE,
        state_key=thread_root_id,
    )
    if isinstance(response, nio.RoomGetStateEventError):
        if response.status_code == "M_NOT_FOUND":
            return None
        msg = f"Failed to fetch thread tags state for {thread_root_id} in {room_id}: {response}"
        raise ThreadTagsError(msg)
    if not isinstance(response, nio.RoomGetStateEventResponse):
        msg = f"Failed to fetch thread tags state for {thread_root_id} in {room_id}: {response}"
        raise ThreadTagsError(msg)
    if not isinstance(response.content, dict):
        return None
    return response.content


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

    _assert_user_can_write_thread_tags(
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

    await _assert_requester_joined_room(
        client,
        room_id,
        requester_user_id=normalized_requester_user_id,
    )
    _assert_user_can_write_thread_tags(
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
    normalized_event_id = _normalize_non_empty_string(event_id)
    if not normalized_event_id:
        return None
    return await canonicalize_related_event_id(
        client,
        room_id,
        normalized_event_id,
        traversal_limit=MAX_THREAD_ROOT_NORMALIZATION_DEPTH,
    )


async def get_thread_tags(
    client: nio.AsyncClient,
    room_id: str,
    thread_root_id: str,
) -> ThreadTagsState | None:
    """Fetch all valid tags for one thread root from Matrix state."""
    normalized_thread_root_id = _normalize_non_empty_string(thread_root_id)
    if normalized_thread_root_id is None:
        return None

    content = await _get_thread_tags_state_content(
        client,
        room_id,
        normalized_thread_root_id,
    )
    if content is None:
        return None
    return _parse_thread_tags_state(room_id, normalized_thread_root_id, content)


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
        existing_state = await get_thread_tags(
            client,
            room_id,
            normalized_thread_root_id,
        )
        next_tags = dict(existing_state.tags) if existing_state else {}
        expected_record = ThreadTagRecord(
            set_by=normalized_set_by,
            set_at=datetime.now(UTC),
            note=normalized_note,
            data=normalized_data,
        )
        next_tags[normalized_tag] = expected_record

        await _put_thread_tags_state(
            client,
            room_id,
            normalized_thread_root_id,
            next_tags,
            error_prefix="Failed to write thread tags state",
        )

        verified_state = await get_thread_tags(
            client,
            room_id,
            normalized_thread_root_id,
        )
        if _verified_state_preserves_expected_tags(
            verified_state,
            expected_tags=next_tags,
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

        next_tags = dict(existing_state.tags)
        del next_tags[normalized_tag]

        await _put_thread_tags_state(
            client,
            room_id,
            normalized_thread_root_id,
            next_tags,
            error_prefix="Failed to update thread tags state",
        )
        remove_written = True

        # See the module-level ARCHITECTURE DECISION note: verify-after-write
        # plus retry is the intentional concurrency strategy for this state
        # event shape.
        verified_state = await get_thread_tags(
            client,
            room_id,
            normalized_thread_root_id,
        )
        if _verified_remove_state_matches(
            verified_state,
            removed_tag=normalized_tag,
            expected_siblings=next_tags,
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

    response = await client.room_get_state(room_id)
    if not isinstance(response, nio.RoomGetStateResponse):
        msg = f"Failed to fetch room state for thread tags in {room_id}: {response}"
        raise ThreadTagsError(msg)

    tagged_threads: dict[str, ThreadTagsState] = {}
    for event in response.events:
        if event.get("type") != THREAD_TAGS_EVENT_TYPE:
            continue

        state_key = event.get("state_key")
        content = event.get("content")
        if not isinstance(state_key, str) or not isinstance(content, dict):
            continue

        state = _parse_thread_tags_state(room_id, state_key, content)
        if state is None:
            continue
        if normalized_tag is not None and normalized_tag not in state.tags:
            continue
        tagged_threads[state_key] = state

    return tagged_threads

"""Shared Matrix power-level helpers."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, cast

POWER_LEVELS_EVENT_TYPE = "m.room.power_levels"
DEFAULT_STATE_EVENT_POWER_LEVEL = 50
DEFAULT_USER_POWER_LEVEL = 0


def parse_power_level(value: object) -> int | None:
    """Return one Matrix power-level value when it is a real integer."""
    if type(value) is not int:
        return None
    return value


def required_state_event_power_level(
    power_levels_content: Mapping[str, object],
    *,
    event_type: str,
) -> int:
    """Return the power level required to write one room state event type."""
    events = power_levels_content.get("events")
    if isinstance(events, Mapping):
        event_level = parse_power_level(cast("Mapping[str, object]", events).get(event_type))
        if event_level is not None:
            return event_level

    state_default = parse_power_level(power_levels_content.get("state_default"))
    if state_default is not None:
        return state_default
    return DEFAULT_STATE_EVENT_POWER_LEVEL


def user_power_level(
    power_levels_content: Mapping[str, object],
    *,
    user_id: str,
) -> int:
    """Return one user's effective Matrix power level in one room."""
    users = power_levels_content.get("users")
    if isinstance(users, Mapping):
        user_level = parse_power_level(cast("Mapping[str, object]", users).get(user_id))
        if user_level is not None:
            return user_level

    users_default = parse_power_level(power_levels_content.get("users_default"))
    if users_default is not None:
        return users_default
    return DEFAULT_USER_POWER_LEVEL


def with_state_event_power_level(
    power_levels_content: Mapping[str, Any],
    *,
    event_type: str,
    power_level: int,
) -> dict[str, Any]:
    """Return power-level content with one state-event override applied."""
    next_content = dict(power_levels_content)
    existing_events = power_levels_content.get("events")
    next_events = dict(existing_events) if isinstance(existing_events, Mapping) else {}
    next_events[event_type] = power_level
    next_content["events"] = next_events
    return next_content


def without_state_event_power_level(
    power_levels_content: Mapping[str, Any],
    *,
    event_type: str,
) -> dict[str, Any]:
    """Return power-level content with one state-event override removed."""
    existing_events = power_levels_content.get("events")
    if not isinstance(existing_events, Mapping):
        return dict(power_levels_content)

    next_events = dict(existing_events)
    if event_type not in next_events:
        return dict(power_levels_content)

    del next_events[event_type]
    next_content = dict(power_levels_content)
    next_content["events"] = next_events
    return next_content

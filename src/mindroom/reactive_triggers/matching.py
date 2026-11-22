"""Deterministic matching and loop guards for reactive trigger events."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from mindroom.constants import SOURCE_KIND_KEY
from mindroom.dispatch_source import EXTERNAL_TRIGGER_SOURCE_KIND, REACTIVE_TRIGGER_SOURCE_KIND
from mindroom.requester_identity import equivalent_requester_ids

if TYPE_CHECKING:
    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.reactive_triggers.models import (
        ExternalEventSourceSpec,
        RoomMessageSourceSpec,
        TriggerEvent,
    )

REACTIVE_TRIGGER_ID_KEY = "io.mindroom.reactive_trigger.id"
REACTIVE_TRIGGER_DEDUPE_KEY = "io.mindroom.reactive_trigger.dedupe_key"
_EXTERNAL_TRIGGER_ID_KEY = "io.mindroom.external_trigger.id"
_MISSING = object()


def is_reactive_trigger_event_content(content: Mapping[str, object]) -> bool:
    """Return whether Matrix event content originated from a reactive or external trigger."""
    if content.get(SOURCE_KIND_KEY) in (REACTIVE_TRIGGER_SOURCE_KIND, EXTERNAL_TRIGGER_SOURCE_KIND):
        return True
    return REACTIVE_TRIGGER_ID_KEY in content or _EXTERNAL_TRIGGER_ID_KEY in content


def matches_room_message(
    spec: RoomMessageSourceSpec,
    event: TriggerEvent,
    *,
    config: Config,
    runtime_paths: RuntimePaths,
) -> bool:
    """Return whether a normalized event matches a room_message source specification."""
    if event.source_type != "room_message" or event.room_id != spec.room_id:
        return False
    if spec.thread_id is not None and event.thread_id != spec.thread_id:
        return False
    if spec.senders:
        if event.actor is None:
            return False
        actor_ids = {event.actor, *equivalent_requester_ids(event.actor, config, runtime_paths)}
        if not any(
            sender in actor_ids or bool(actor_ids & equivalent_requester_ids(sender, config, runtime_paths))
            for sender in spec.senders
        ):
            return False
    return not (spec.pattern is not None and re.search(spec.pattern, event.text or "", re.IGNORECASE) is None)


def matches_external_event(spec: ExternalEventSourceSpec, event: TriggerEvent) -> bool:
    """Return whether a normalized event matches an external_event source specification."""
    if event.source_type != "external_event" or event.source_id != spec.source_id or event.kind != spec.kind:
        return False
    for key, expected in spec.match.items():
        actual = _lookup_data_key(event.data, key)
        if actual is _MISSING or type(actual) is not type(expected) or actual != expected:
            return False
    return True


def _lookup_data_key(data: Mapping[str, Any], key: str) -> object:
    """Look up a direct or dot-traversed key strictly within structured event data."""
    if key in data:
        return data[key]
    if "." not in key:
        return _MISSING
    current: object = data
    for segment in key.split("."):
        if not isinstance(current, Mapping) or segment not in current:
            return _MISSING
        current = current[segment]
    return current

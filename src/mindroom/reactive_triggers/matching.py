"""Deterministic matching and loop guards for reactive trigger events."""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

import regex

from mindroom.constants import SOURCE_KIND_KEY
from mindroom.dispatch_source import EXTERNAL_TRIGGER_SOURCE_KIND, REACTIVE_TRIGGER_SOURCE_KIND
from mindroom.logging_config import get_logger
from mindroom.requester_identity import equivalent_requester_ids

if TYPE_CHECKING:
    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.reactive_triggers.models import (
        ExternalEventSourceSpec,
        RoomMessageSourceSpec,
        TriggerEvent,
    )

logger = get_logger(__name__)

REACTIVE_TRIGGER_ID_KEY = "io.mindroom.reactive_trigger.id"
REACTIVE_TRIGGER_DEDUPE_KEY = "io.mindroom.reactive_trigger.dedupe_key"
_EXTERNAL_TRIGGER_ID_KEY = "io.mindroom.external_trigger.id"
_MAX_PATTERN_SEARCH_CHARS = 4_096
_PATTERN_SEARCH_TIMEOUT_SECONDS = 0.05
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
    if spec.senders and not _matches_configured_senders(
        spec.senders,
        event.actor,
        config=config,
        runtime_paths=runtime_paths,
    ):
        return False
    if spec.pattern is not None:
        return _matches_bounded_pattern(spec.pattern, event.text or "", room_id=spec.room_id)
    return True


def matches_external_event(spec: ExternalEventSourceSpec, event: TriggerEvent) -> bool:
    """Return whether a normalized event matches an external_event source specification."""
    if event.source_type != "external_event" or event.source_id != spec.source_id or event.kind != spec.kind:
        return False
    for key, expected in spec.match.items():
        actual = lookup_data_key(event.data, key, _MISSING)
        if actual is _MISSING or type(actual) is not type(expected) or actual != expected:
            return False
    return True


def _matches_configured_senders(
    senders: list[str],
    actor: str | None,
    *,
    config: Config,
    runtime_paths: RuntimePaths,
) -> bool:
    """Return whether actor matches any configured sender or its equivalent bridge aliases."""
    if actor is None:
        return False
    actor_ids = {actor, *equivalent_requester_ids(actor, config, runtime_paths)}
    return any(
        sender in actor_ids or bool(actor_ids & equivalent_requester_ids(sender, config, runtime_paths))
        for sender in senders
    )


def _matches_bounded_pattern(pattern: str, text: str, *, room_id: str) -> bool:
    """Search a bounded text prefix using regex with an explicit timeout."""
    try:
        return (
            regex.search(
                pattern,
                text[:_MAX_PATTERN_SEARCH_CHARS],
                flags=regex.IGNORECASE,
                timeout=_PATTERN_SEARCH_TIMEOUT_SECONDS,
            )
            is not None
        )
    except TimeoutError:
        logger.warning(
            "Reactive trigger regex pattern timed out",
            room_id=room_id,
            pattern_length=len(pattern),
        )
        return False
    except regex.error:
        logger.warning(
            "Invalid reactive trigger regex pattern during matching",
            room_id=room_id,
            pattern_length=len(pattern),
        )
        return False


def lookup_data_key(data: Mapping[str, Any], key: str, default: object = None) -> object:
    """Look up a direct or dot-traversed key strictly within structured event data."""
    if key in data:
        return data[key]
    if "." not in key:
        return default
    current: object = data
    for segment in key.split("."):
        if not isinstance(current, Mapping) or segment not in current:
            return default
        current = current[segment]
    return current

"""Shared source-kind and dispatch-policy vocabulary for inbound turns."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any, Protocol, cast, runtime_checkable

from mindroom.constants import SOURCE_KIND_KEY, VISIBLE_ROUTER_VOICE_ECHO_KEY

MESSAGE_SOURCE_KIND = "message"
VOICE_SOURCE_KIND = "voice"
IMAGE_SOURCE_KIND = "image"
MEDIA_SOURCE_KIND = "media"
EDIT_SOURCE_KIND = "edit"
SCHEDULED_SOURCE_KIND = "scheduled"
HOOK_SOURCE_KIND = "hook"
HOOK_DISPATCH_SOURCE_KIND = "hook_dispatch"
ACTIVE_THREAD_FOLLOW_UP_SOURCE_KIND = "active_thread_follow_up"
TRUSTED_INTERNAL_RELAY_SOURCE_KIND = "trusted_internal_relay"
_KNOWN_SOURCE_KINDS: frozenset[str] = frozenset(
    {
        MESSAGE_SOURCE_KIND,
        VOICE_SOURCE_KIND,
        IMAGE_SOURCE_KIND,
        MEDIA_SOURCE_KIND,
        EDIT_SOURCE_KIND,
        SCHEDULED_SOURCE_KIND,
        HOOK_SOURCE_KIND,
        HOOK_DISPATCH_SOURCE_KIND,
        TRUSTED_INTERNAL_RELAY_SOURCE_KIND,
    },
)
_AUTOMATION_SOURCE_KINDS: frozenset[str] = frozenset(
    {
        SCHEDULED_SOURCE_KIND,
        HOOK_SOURCE_KIND,
        HOOK_DISPATCH_SOURCE_KIND,
    },
)
_COALESCING_BYPASS_SOURCE_KINDS: frozenset[str] = _AUTOMATION_SOURCE_KINDS | frozenset(
    {
        TRUSTED_INTERNAL_RELAY_SOURCE_KIND,
    },
)
_TRUSTED_ORIGINAL_SENDER_SOURCE_KINDS: frozenset[str] = frozenset(
    {
        HOOK_DISPATCH_SOURCE_KIND,
        HOOK_SOURCE_KIND,
        SCHEDULED_SOURCE_KIND,
        TRUSTED_INTERNAL_RELAY_SOURCE_KIND,
        VOICE_SOURCE_KIND,
    },
)
_INTERNAL_RELAY_DETECTION_SOURCE_KINDS: frozenset[str] = frozenset(
    {
        "",
        MESSAGE_SOURCE_KIND,
        TRUSTED_INTERNAL_RELAY_SOURCE_KIND,
    },
)


@runtime_checkable
class _HasContent(Protocol):
    content: Mapping[str, object]


@runtime_checkable
class _HasSource(Protocol):
    source: Mapping[str, object]


@runtime_checkable
class _HasSourceKind(Protocol):
    source_kind: str


@runtime_checkable
class _HasSourceKindOverride(Protocol):
    source_kind_override: str | None


@runtime_checkable
class _HasSender(Protocol):
    sender: str


def is_automation_source_kind(source_kind: str) -> bool:
    """Return whether one source kind is synthetic automation."""
    return source_kind in _AUTOMATION_SOURCE_KINDS


def source_kind_bypasses_coalescing(source_kind: str | None) -> bool:
    """Return whether one source kind must dispatch as a FIFO barrier."""
    return source_kind in _COALESCING_BYPASS_SOURCE_KINDS


def source_kind_allows_trusted_original_sender(source_kind: str | None) -> bool:
    """Return whether trusted senders may promote original-sender metadata for this source kind."""
    return source_kind in _TRUSTED_ORIGINAL_SENDER_SOURCE_KINDS


def source_kind_allows_internal_relay_detection(source_kind: str | None) -> bool:
    """Return whether content metadata may promote this event to a trusted internal relay."""
    return source_kind in _INTERNAL_RELAY_DETECTION_SOURCE_KINDS


def _source_kind_from_value(value: object) -> str | None:
    """Return a canonical source kind from arbitrary metadata."""
    return value if isinstance(value, str) and value in _KNOWN_SOURCE_KINDS else None


def source_kind_from_content(content: Mapping[str, Any]) -> str | None:
    """Return canonical source-kind metadata from Matrix content."""
    source_kind = content.get(SOURCE_KIND_KEY)
    return _source_kind_from_value(source_kind)


def is_visible_router_voice_echo_content(content: object) -> bool:
    """Return whether Matrix content is a visible router voice transcript echo."""
    if not isinstance(content, Mapping):
        return False
    return cast("Mapping[str, object]", content).get(VISIBLE_ROUTER_VOICE_ECHO_KEY) is True


def _trusted_source_kind_from_event_content(
    event_or_envelope: object,
    *,
    sender_is_trusted: Callable[[str], bool] | None,
) -> str | None:
    if sender_is_trusted is None or not isinstance(event_or_envelope, _HasSender):
        return None
    if not sender_is_trusted(event_or_envelope.sender):
        return None
    if isinstance(event_or_envelope, _HasContent):
        return source_kind_from_content(cast("Mapping[str, Any]", event_or_envelope.content))
    if not isinstance(event_or_envelope, _HasSource):
        return None
    content = event_or_envelope.source.get("content")
    if not isinstance(content, Mapping):
        return None
    return source_kind_from_content(cast("Mapping[str, Any]", content))


def is_voice_event(
    event_or_envelope: object,
    *,
    sender_is_trusted: Callable[[str], bool] | None = None,
) -> bool:
    """Return whether one event, history message, or envelope originated from voice."""
    source_kind = (
        event_or_envelope.source_kind
        if isinstance(event_or_envelope, _HasSourceKind)
        else event_or_envelope.source_kind_override
        if isinstance(event_or_envelope, _HasSourceKindOverride)
        else _trusted_source_kind_from_event_content(
            event_or_envelope,
            sender_is_trusted=sender_is_trusted,
        )
    )
    return source_kind == VOICE_SOURCE_KIND

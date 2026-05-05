"""Shared source-kind and dispatch-policy vocabulary for inbound turns."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any, Protocol, cast, runtime_checkable

_MESSAGE_SOURCE_KIND = "message"
_VOICE_SOURCE_KIND = "voice"
_IMAGE_SOURCE_KIND = "image"
_MEDIA_SOURCE_KIND = "media"
SCHEDULED_SOURCE_KIND = "scheduled"
HOOK_SOURCE_KIND = "hook"
HOOK_DISPATCH_SOURCE_KIND = "hook_dispatch"
ACTIVE_THREAD_FOLLOW_UP_SOURCE_KIND = "active_thread_follow_up"
TRUSTED_INTERNAL_RELAY_SOURCE_KIND = "trusted_internal_relay"
_AUTOMATION_SOURCE_KINDS: frozenset[str] = frozenset(
    {
        SCHEDULED_SOURCE_KIND,
        HOOK_SOURCE_KIND,
        HOOK_DISPATCH_SOURCE_KIND,
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


def _source_kind_from_content(content: Mapping[str, Any]) -> str | None:
    source_kind = content.get("com.mindroom.source_kind")
    return source_kind if isinstance(source_kind, str) else None


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
        return _source_kind_from_content(cast("Mapping[str, Any]", event_or_envelope.content))
    if not isinstance(event_or_envelope, _HasSource):
        return None
    content = event_or_envelope.source.get("content")
    if not isinstance(content, Mapping):
        return None
    return _source_kind_from_content(cast("Mapping[str, Any]", content))


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
    return source_kind == _VOICE_SOURCE_KIND

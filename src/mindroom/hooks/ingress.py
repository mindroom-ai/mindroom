"""Pure helpers for hook-originated ingress behavior."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol, cast, runtime_checkable

from .types import EVENT_MESSAGE_RECEIVED

if TYPE_CHECKING:
    from .context import MessageEnvelope

ACTIVE_THREAD_FOLLOW_UP_SOURCE_KIND = "active_thread_follow_up"
TRUSTED_INTERNAL_RELAY_SOURCE_KIND = "trusted_internal_relay"
AUTOMATION_SOURCE_KINDS: frozenset[str] = frozenset({"scheduled", "hook", "hook_dispatch"})


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


@dataclass(frozen=True, slots=True)
class HookIngressPolicy:
    """Normalized ingress behavior for hook-originated synthetic messages."""

    rerun_message_received: bool = True
    skip_message_received_plugin_names: frozenset[str] = frozenset()
    bypass_unmentioned_agent_gate: bool = False
    allow_full_dispatch: bool = True


def is_automation_source_kind(source_kind: str) -> bool:
    """Return whether one source kind is synthetic automation."""
    return source_kind in AUTOMATION_SOURCE_KINDS


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
    return source_kind == "voice"


def split_hook_source(hook_source: str | None) -> tuple[str | None, str | None]:
    """Return ``(plugin_name, event_name)`` from one serialized hook source tag."""
    if not isinstance(hook_source, str):
        return None, None
    plugin_name, _, source_event_name = hook_source.partition(":")
    if not plugin_name or not source_event_name:
        return None, None
    return plugin_name, source_event_name


def hook_ingress_policy(envelope: MessageEnvelope) -> HookIngressPolicy:
    """Return the normalized ingress policy for one synthetic hook message."""
    if envelope.source_kind not in {"hook", "hook_dispatch"}:
        return HookIngressPolicy()

    plugin_name, source_event_name = split_hook_source(envelope.hook_source)
    policy = HookIngressPolicy(
        bypass_unmentioned_agent_gate=envelope.source_kind == "hook_dispatch",
    )
    if envelope.message_received_depth == 0:
        return policy
    if envelope.message_received_depth == 1:
        if source_event_name == EVENT_MESSAGE_RECEIVED:
            skip_plugin_names = frozenset({plugin_name}) if plugin_name is not None else frozenset()
            return HookIngressPolicy(
                bypass_unmentioned_agent_gate=policy.bypass_unmentioned_agent_gate,
                skip_message_received_plugin_names=skip_plugin_names,
            )
        return policy
    return HookIngressPolicy(
        rerun_message_received=False,
        bypass_unmentioned_agent_gate=policy.bypass_unmentioned_agent_gate,
        allow_full_dispatch=False,
    )


def should_handle_interactive_text_response(envelope: MessageEnvelope) -> bool:
    """Return whether one inbound text event may answer an interactive prompt."""
    return not is_automation_source_kind(envelope.source_kind)

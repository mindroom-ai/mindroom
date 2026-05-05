"""Pure helpers for hook-originated ingress behavior."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from mindroom.dispatch_source import is_automation_source_kind

from .types import EVENT_MESSAGE_RECEIVED

if TYPE_CHECKING:
    from .context import MessageEnvelope


@dataclass(frozen=True, slots=True)
class HookIngressPolicy:
    """Normalized ingress behavior for hook-originated synthetic messages."""

    rerun_message_received: bool = True
    skip_message_received_plugin_names: frozenset[str] = frozenset()
    bypass_unmentioned_agent_gate: bool = False
    allow_full_dispatch: bool = True


def _split_hook_source(hook_source: str | None) -> tuple[str | None, str | None]:
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

    plugin_name, source_event_name = _split_hook_source(envelope.hook_source)
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

"""Pure helpers for hook-originated ingress behavior."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from mindroom.dispatch_source import HOOK_DISPATCH_SOURCE_KIND, HOOK_SOURCE_KIND, is_automation_source_kind
from mindroom.turn_origin import TurnIntent

from .types import EVENT_MESSAGE_RECEIVED, split_hook_source

if TYPE_CHECKING:
    from .context import MessageEnvelope


@dataclass(frozen=True, slots=True)
class HookIngressPolicy:
    """Normalized ingress behavior for hook-originated synthetic messages."""

    rerun_message_received: bool = True
    skip_message_received_plugin_names: frozenset[str] = frozenset()
    bypass_unmentioned_agent_gate: bool = False
    allow_full_dispatch: bool = True


def hook_ingress_policy(envelope: MessageEnvelope) -> HookIngressPolicy:
    """Return the normalized ingress policy for one synthetic hook message."""
    origin = envelope.origin
    if origin is None:
        if envelope.source_kind not in {HOOK_SOURCE_KIND, HOOK_DISPATCH_SOURCE_KIND}:
            return HookIngressPolicy()
        bypass_unmentioned_agent_gate = envelope.source_kind == HOOK_DISPATCH_SOURCE_KIND
    else:
        if origin.intent not in {TurnIntent.HOOK_MESSAGE, TurnIntent.HOOK_DISPATCH}:
            return HookIngressPolicy()
        bypass_unmentioned_agent_gate = origin.may_dispatch_without_mention

    plugin_name, source_event_name = split_hook_source(envelope.hook_source)
    policy = HookIngressPolicy(
        bypass_unmentioned_agent_gate=bypass_unmentioned_agent_gate,
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
    if envelope.origin is not None:
        return envelope.origin.may_answer_interactive_prompt
    return not is_automation_source_kind(envelope.source_kind)

"""Pure helpers for hook-originated ingress behavior."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from mindroom.turn_origin import TurnIntent

from .types import EVENT_MESSAGE_RECEIVED, split_hook_source

if TYPE_CHECKING:
    from .context import MessageEnvelope


@dataclass(frozen=True, slots=True)
class HookIngressPolicy:
    """Normalized ingress behavior for hook-originated synthetic messages."""

    rerun_message_received: bool = True
    skip_message_received_plugin_names: frozenset[str] = frozenset()
    allow_full_dispatch: bool = True


def hook_ingress_policy(envelope: MessageEnvelope) -> HookIngressPolicy:
    """Return the normalized ingress policy for one synthetic hook message."""
    origin = envelope.origin
    if origin.intent not in {TurnIntent.HOOK_MESSAGE, TurnIntent.HOOK_DISPATCH}:
        return HookIngressPolicy()

    plugin_name, source_event_name = split_hook_source(envelope.hook_source)
    if envelope.message_received_depth == 0:
        return HookIngressPolicy()
    if envelope.message_received_depth == 1:
        if source_event_name == EVENT_MESSAGE_RECEIVED:
            skip_plugin_names = frozenset({plugin_name}) if plugin_name is not None else frozenset()
            return HookIngressPolicy(
                skip_message_received_plugin_names=skip_plugin_names,
            )
        return HookIngressPolicy()
    return HookIngressPolicy(
        rerun_message_received=False,
        allow_full_dispatch=False,
    )

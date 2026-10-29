"""Ingress hooks for inbound turns: hook-message policy, message:received, and message or system enrichment."""

from __future__ import annotations

import time
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from mindroom.constants import MATRIX_MESSAGE_TARGET_ENRICHMENT_KEY
from mindroom.timing import emit_elapsed_timing
from mindroom.turn_origin import TurnIntent

from .context import (
    HookContextSupport,
    MessageEnrichContext,
    MessageEnvelope,
    MessageReceivedContext,
    SystemEnrichContext,
)
from .enrichment import render_enrichment_block
from .execution import emit, emit_collect
from .types import (
    EVENT_MESSAGE_ENRICH,
    EVENT_MESSAGE_RECEIVED,
    EVENT_SYSTEM_ENRICH,
    EnrichmentItem,
    split_hook_source,
)

if TYPE_CHECKING:
    from mindroom.inbound_turn_normalizer import DispatchPayload
    from mindroom.turn_policy import PreparedDispatch


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


def _without_runtime_owned_enrichment(items: list[EnrichmentItem]) -> list[EnrichmentItem]:
    return [item for item in items if item.key != MATRIX_MESSAGE_TARGET_ENRICHMENT_KEY]


@dataclass(frozen=True)
class _PreparedHookedPayload:
    """Concrete payload returned after ingress enrichment hooks run."""

    payload: DispatchPayload
    envelope: MessageEnvelope
    transient_enrichment_items: tuple[EnrichmentItem, ...]


@dataclass
class IngressHookRunner:
    """Own ingress hook emission and message or system enrichment updates."""

    hook_context: HookContextSupport

    async def emit_message_received_hooks(
        self,
        *,
        envelope: MessageEnvelope,
        correlation_id: str,
        policy: HookIngressPolicy,
    ) -> bool:
        """Emit message:received and return whether hooks suppressed processing."""
        if not self.hook_context.registry.has_hooks(EVENT_MESSAGE_RECEIVED):
            return False
        if not policy.rerun_message_received:
            return False

        context = MessageReceivedContext(
            **self.hook_context.base_kwargs(EVENT_MESSAGE_RECEIVED, correlation_id),
            envelope=envelope,
            skip_plugin_names=policy.skip_message_received_plugin_names,
        )
        await emit(self.hook_context.registry, EVENT_MESSAGE_RECEIVED, context)
        return context.suppress

    async def apply_message_enrichment(
        self,
        dispatch: PreparedDispatch,
        payload: DispatchPayload,
        *,
        target_entity_name: str,
        target_member_names: tuple[str, ...] | None,
    ) -> _PreparedHookedPayload:
        """Run message:enrich and return the model-facing payload."""
        started = time.monotonic()
        hook_registered = self.hook_context.registry.has_hooks(EVENT_MESSAGE_ENRICH)
        item_count = 0
        transient_enrichment_items: tuple[EnrichmentItem, ...] = ()

        envelope = replace(
            dispatch.envelope,
            attachment_ids=(
                tuple(payload.attachment_ids)
                if payload.attachment_ids is not None
                else dispatch.envelope.attachment_ids
            ),
            agent_name=target_entity_name,
        )
        model_prompt = payload.model_prompt
        if hook_registered:
            context = MessageEnrichContext(
                **self.hook_context.base_kwargs(EVENT_MESSAGE_ENRICH, dispatch.correlation_id),
                envelope=envelope,
                target_entity_name=target_entity_name,
                target_member_names=target_member_names,
            )
            items = _without_runtime_owned_enrichment(
                await emit_collect(self.hook_context.registry, EVENT_MESSAGE_ENRICH, context),
            )
            item_count = len(items)
            persisted_items = [item for item in items if item.persist]
            transient_enrichment_items = tuple(item for item in items if not item.persist)
            if persisted_items:
                enrichment_block = render_enrichment_block(persisted_items)
                base_model_prompt = payload.model_prompt if payload.model_prompt is not None else payload.prompt
                model_prompt = f"{base_model_prompt.rstrip()}\n\n{enrichment_block}"

        emit_elapsed_timing(
            "response_payload.apply_message_enrichment",
            started,
            room_id=dispatch.envelope.room_id,
            target_entity_name=target_entity_name,
            hook_registered=hook_registered,
            enrichment_item_count=item_count,
        )
        return _PreparedHookedPayload(
            payload=replace(payload, model_prompt=model_prompt),
            envelope=envelope,
            transient_enrichment_items=transient_enrichment_items,
        )

    async def apply_system_enrichment(
        self,
        dispatch: PreparedDispatch,
        envelope: MessageEnvelope,
        *,
        target_entity_name: str,
        target_member_names: tuple[str, ...] | None,
    ) -> list[EnrichmentItem]:
        """Run system:enrich and return system-prompt enrichment items."""
        started = time.monotonic()
        hook_registered = self.hook_context.registry.has_hooks(EVENT_SYSTEM_ENRICH)

        def finish(items: list[EnrichmentItem]) -> list[EnrichmentItem]:
            emit_elapsed_timing(
                "response_payload.apply_system_enrichment",
                started,
                room_id=dispatch.envelope.room_id,
                target_entity_name=target_entity_name,
                hook_registered=hook_registered,
                enrichment_item_count=len(items),
            )
            return items

        if not hook_registered:
            return finish([])
        context = SystemEnrichContext(
            **self.hook_context.base_kwargs(EVENT_SYSTEM_ENRICH, dispatch.correlation_id),
            envelope=envelope,
            target_entity_name=target_entity_name,
            target_member_names=target_member_names,
        )
        return finish(
            _without_runtime_owned_enrichment(
                await emit_collect(self.hook_context.registry, EVENT_SYSTEM_ENRICH, context),
            ),
        )

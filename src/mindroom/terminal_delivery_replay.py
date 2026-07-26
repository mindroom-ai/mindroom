"""Replay response lifecycle after one durable terminal edit lands."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from mindroom.delivery_gateway import ResponseIdentity
from mindroom.final_delivery import FinalDeliveryOutcome
from mindroom.post_response_effects import ResponseOutcome
from mindroom.response_lifecycle import ResponseLifecycle, ResponseLifecycleDeps

if TYPE_CHECKING:
    import structlog

    from mindroom.delivery_gateway import ResponseHookService
    from mindroom.post_response_effects import PostResponseEffectsSupport
    from mindroom.terminal_delivery import PendingTerminalDelivery


@dataclass(frozen=True)
class TerminalDeliveryLifecycleReplayerDeps:
    """Runtime collaborators needed after one durable edit lands."""

    response_hooks: ResponseHookService
    post_response_effects: PostResponseEffectsSupport
    logger: structlog.stdlib.BoundLogger


@dataclass(frozen=True)
class TerminalDeliveryLifecycleReplayer:
    """Replay success-only lifecycle work from durable typed facts."""

    deps: TerminalDeliveryLifecycleReplayerDeps

    async def complete(self, item: PendingTerminalDelivery) -> None:
        """Run ordinary completed-response hooks and post-response effects."""
        facts = item.lifecycle
        identity = ResponseIdentity(
            response_kind=facts.response_kind,
            response_envelope=facts.response_envelope,
            correlation_id=facts.correlation_id,
            source_event_ids=item.source_event_ids,
            thread_summary_message_count_hint=facts.thread_summary_message_count_hint,
        )
        final_outcome = FinalDeliveryOutcome(
            terminal_status="completed",
            event_id=item.target_event_id,
            is_visible_response=True,
            final_visible_body=item.body,
            delivery_kind="edited",
            tool_trace=item.tool_trace,
            extra_content=dict(item.extra_content or {}),
            interactive_metadata=facts.interactive_metadata,
        )
        lifecycle = ResponseLifecycle(
            ResponseLifecycleDeps(
                response_hooks=self.deps.response_hooks,
                logger=self.deps.logger,
            ),
            identity=identity,
            pipeline_timing=None,
        )
        await lifecycle.finalize(
            final_outcome,
            build_post_response_outcome=lambda _outcome: ResponseOutcome(
                run_succeeded=True,
                interactive_target=item.target,
                thread_summary_room_id=(item.target.room_id if item.target.resolved_thread_id is not None else None),
                thread_summary_thread_id=item.target.resolved_thread_id,
                thread_summary_message_count_hint=facts.thread_summary_message_count_hint,
                thread_summary_entity_name=facts.thread_summary_entity_name,
            ),
            post_response_deps=lambda: self.deps.post_response_effects.build_deps(
                room_id=item.target.room_id,
                interactive_agent_name=item.agent_name,
            ),
        )


__all__ = [
    "TerminalDeliveryLifecycleReplayer",
    "TerminalDeliveryLifecycleReplayerDeps",
]

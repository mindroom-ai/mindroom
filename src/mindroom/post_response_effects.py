"""Shared post-response effects for Matrix delivery flows."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from mindroom import constants

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Sequence

    import nio
    import structlog
    from agno.db.base import SessionType

    from mindroom.delivery_gateway import DeliveryResult
    from mindroom.handled_turns import HandledTurnLedger, HandledTurnState
    from mindroom.history.types import CompactionOutcome
    from mindroom.matrix.client import ResolvedVisibleMessage
    from mindroom.message_target import MessageTarget
    from mindroom.stop import StopManager
    from mindroom.tool_system.worker_routing import ToolExecutionIdentity


@dataclass(frozen=True)
class ResponseOutcome:
    """Terminal response facts needed for post-delivery side effects."""

    resolved_event_id: str | None
    delivery_result: DeliveryResult | None
    response_run_id: str | None = None
    session_id: str | None = None
    session_type: SessionType | None = None
    execution_identity: ToolExecutionIdentity | None = None
    compaction_outcomes: tuple[CompactionOutcome, ...] = ()
    interactive_target: MessageTarget | None = None
    thread_summary_room_id: str | None = None
    thread_summary_thread_id: str | None = None
    thread_summary_message_count_hint: int | None = None
    memory_prompt: str | None = None
    memory_thread_history: Sequence[ResolvedVisibleMessage] | None = None
    strip_transient_enrichment_after_run: bool = False
    strip_transient_enrichment_before_effects: bool = False
    dispatch_compaction_when_suppressed: bool = False
    handled_turn: HandledTurnState | None = None


@dataclass(frozen=True)
class PostResponseEffectsDeps:
    """Narrow side-effect surface needed to finalize one response."""

    logger: structlog.stdlib.BoundLogger
    register_interactive: (
        Callable[
            [str, MessageTarget, dict[str, str], list[dict[str, str]]],
            Awaitable[None],
        ]
        | None
    ) = None
    dispatch_compaction_notices: (
        Callable[
            [str, Sequence[CompactionOutcome]],
            Awaitable[None],
        ]
        | None
    ) = None
    strip_transient_enrichment: Callable[[], None] | None = None
    queue_memory_persistence: Callable[[], None] | None = None
    persist_response_event_id: Callable[[str, str], None] | None = None
    queue_thread_summary: Callable[[str, str, int | None], None] | None = None
    record_handled_turn: Callable[[HandledTurnState], None] | None = None


def record_handled_turn(
    handled_turn_ledger: HandledTurnLedger,
    handled_turn: HandledTurnState,
) -> None:
    """Record a handled turn while preserving any prior visible echo linkage."""
    visible_echo_event_id = handled_turn.visible_echo_event_id or handled_turn_ledger.visible_echo_event_id_for_sources(
        handled_turn.source_event_ids,
    )
    handled_turn_ledger.record_handled_turn(
        handled_turn.with_visible_echo_event_id(visible_echo_event_id),
    )


def matrix_run_metadata_for_handled_turn(
    handled_turn: HandledTurnState,
) -> dict[str, Any] | None:
    """Build persisted run metadata for one handled turn."""
    if not handled_turn.is_coalesced:
        return None
    metadata: dict[str, Any] = {
        constants.MATRIX_SOURCE_EVENT_IDS_METADATA_KEY: list(handled_turn.source_event_ids),
    }
    if handled_turn.source_event_prompts:
        metadata[constants.MATRIX_SOURCE_EVENT_PROMPTS_METADATA_KEY] = dict(handled_turn.source_event_prompts)
    return metadata


def clear_tracked_response_message(
    stop_manager: StopManager,
    client: nio.AsyncClient,
    tracked_message_id: str,
    *,
    show_stop_button: bool,
) -> None:
    """Clear one tracked response and redact the stop button when still present."""
    tracked = stop_manager.tracked_messages.get(tracked_message_id)
    button_already_removed = tracked is None or tracked.reaction_event_id is None
    stop_manager.clear_message(
        tracked_message_id,
        client=client,
        remove_button=show_stop_button and not button_already_removed,
    )


async def apply_post_response_effects(  # noqa: C901
    outcome: ResponseOutcome,
    deps: PostResponseEffectsDeps,
) -> None:
    """Apply the shared side effects that happen after response delivery is known."""
    delivery_result = outcome.delivery_result
    delivered_event_id = delivery_result.event_id if delivery_result is not None else None
    delivered_interactive_target = bool(
        delivered_event_id is not None and delivery_result is not None and not delivery_result.suppressed,
    )
    should_dispatch_compaction = bool(
        delivered_event_id is not None
        and delivery_result is not None
        and (not delivery_result.suppressed or outcome.dispatch_compaction_when_suppressed),
    )

    def strip_transient_enrichment() -> None:
        if not outcome.strip_transient_enrichment_after_run or deps.strip_transient_enrichment is None:
            return
        try:
            deps.strip_transient_enrichment()
        except Exception:
            deps.logger.exception(
                "Failed to strip hook enrichment from session history",
                session_id=outcome.session_id,
                session_type=str(outcome.session_type) if outcome.session_type is not None else None,
            )

    if outcome.strip_transient_enrichment_before_effects:
        strip_transient_enrichment()

    if (
        delivered_interactive_target
        and deps.register_interactive is not None
        and delivery_result is not None
        and delivery_result.option_map
        and delivery_result.options_list
        and outcome.interactive_target is not None
    ):
        assert delivered_event_id is not None
        await deps.register_interactive(
            delivered_event_id,
            outcome.interactive_target,
            delivery_result.option_map,
            delivery_result.options_list,
        )

    if should_dispatch_compaction and deps.dispatch_compaction_notices is not None and outcome.compaction_outcomes:
        assert delivered_event_id is not None
        await deps.dispatch_compaction_notices(
            delivered_event_id,
            outcome.compaction_outcomes,
        )

    if not outcome.strip_transient_enrichment_before_effects:
        strip_transient_enrichment()

    if deps.queue_memory_persistence is not None:
        try:
            deps.queue_memory_persistence()
        except Exception:
            deps.logger.exception(
                "Failed to queue memory persistence after response",
                session_id=outcome.session_id,
                room_id=outcome.interactive_target.room_id if outcome.interactive_target is not None else None,
                thread_id=outcome.interactive_target.thread_id if outcome.interactive_target is not None else None,
            )

    if (
        outcome.response_run_id is not None
        and outcome.resolved_event_id is not None
        and deps.persist_response_event_id is not None
    ):
        try:
            deps.persist_response_event_id(outcome.response_run_id, outcome.resolved_event_id)
        except Exception:
            deps.logger.exception(
                "Failed to persist response event linkage in run metadata",
                session_id=outcome.session_id,
                run_id=outcome.response_run_id,
                response_event_id=outcome.resolved_event_id,
            )

    if (
        outcome.resolved_event_id is not None
        and (delivery_result is None or not delivery_result.suppressed)
        and outcome.thread_summary_room_id is not None
        and outcome.thread_summary_thread_id is not None
        and deps.queue_thread_summary is not None
    ):
        deps.queue_thread_summary(
            outcome.thread_summary_room_id,
            outcome.thread_summary_thread_id,
            outcome.thread_summary_message_count_hint,
        )

    if outcome.handled_turn is not None and deps.record_handled_turn is not None:
        deps.record_handled_turn(outcome.handled_turn)

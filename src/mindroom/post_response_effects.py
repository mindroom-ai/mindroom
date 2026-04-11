"""Shared post-response effects for Matrix delivery flows."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from mindroom import constants, interactive
from mindroom.background_tasks import create_background_task
from mindroom.delivery_gateway import CompactionNoticeRequest
from mindroom.message_target import MessageTarget
from mindroom.thread_summary import maybe_generate_thread_summary
from mindroom.timing import timed

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Sequence

    import nio
    import structlog
    from agno.db.base import SessionType

    from mindroom.bot_runtime_view import BotRuntimeView
    from mindroom.constants import RuntimePaths
    from mindroom.delivery_gateway import DeliveryGateway, DeliveryResult
    from mindroom.handled_turns import HandledTurnLedger, HandledTurnState
    from mindroom.history.types import CompactionOutcome
    from mindroom.matrix.client import ResolvedVisibleMessage
    from mindroom.matrix.conversation_access import ConversationReadAccess
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


@dataclass(frozen=True)
class PostResponseEffectsSupport:
    """Shared support used to build per-response post-effect deps."""

    runtime: BotRuntimeView
    logger: structlog.stdlib.BoundLogger
    runtime_paths: RuntimePaths
    delivery_gateway: DeliveryGateway
    conversation_access: ConversationReadAccess

    def _client(self) -> nio.AsyncClient:
        """Return the current Matrix client for interactive follow-up effects."""
        client = self.runtime.client
        if client is None:
            msg = "Matrix client is not ready for post-response effects"
            raise RuntimeError(msg)
        return client

    @timed("maybe_generate_thread_summary")
    async def _timed_thread_summary(
        self,
        *,
        thread_id: str,
        summary_coro: Awaitable[None],
    ) -> None:
        """Run thread-summary generation with duration logging."""
        del thread_id
        await summary_coro

    async def _register_interactive_delivery(
        self,
        *,
        event_id: str,
        room_id: str,
        target: MessageTarget,
        option_map: dict[str, str],
        options_list: list[dict[str, str]],
        agent_name: str,
    ) -> None:
        """Persist one interactive response and add its reaction buttons."""
        interactive.register_interactive_question(
            event_id,
            room_id,
            target.resolved_thread_id,
            option_map,
            agent_name,
        )
        await interactive.add_reaction_buttons(
            self._client(),
            room_id,
            event_id,
            options_list,
        )

    async def _dispatch_compaction_notices(
        self,
        *,
        room_id: str,
        reply_to_event_id: str,
        main_response_event_id: str | None,
        thread_id: str | None,
        compaction_outcomes: Sequence[CompactionOutcome],
    ) -> None:
        """Send compaction notices for all outcomes that request one."""
        if main_response_event_id is None:
            return
        for outcome in compaction_outcomes:
            if not outcome.notify:
                continue
            await self.delivery_gateway.send_compaction_notice(
                CompactionNoticeRequest(
                    target=MessageTarget.resolve(
                        room_id=room_id,
                        thread_id=thread_id,
                        reply_to_event_id=reply_to_event_id,
                    ),
                    main_response_event_id=main_response_event_id,
                    outcome=outcome,
                ),
            )

    def queue_thread_summary(
        self,
        room_id: str,
        thread_id: str,
        message_count_hint: int | None,
    ) -> None:
        """Queue background thread summarization with timing instrumentation."""
        summary_coro = maybe_generate_thread_summary(
            client=self._client(),
            room_id=room_id,
            thread_id=thread_id,
            config=self.runtime.config,
            runtime_paths=self.runtime_paths,
            conversation_access=self.conversation_access,
            message_count_hint=message_count_hint,
        )
        create_background_task(
            self._timed_thread_summary(
                thread_id=thread_id,
                summary_coro=summary_coro,
            ),
            name=f"thread_summary_{room_id}_{thread_id}",
            owner=self.runtime,
        )

    def build_deps(
        self,
        *,
        room_id: str,
        reply_to_event_id: str,
        thread_id: str | None,
        interactive_agent_name: str,
        strip_transient_enrichment: Callable[[], None] | None = None,
        queue_memory_persistence: Callable[[], None] | None = None,
        persist_response_event_id: Callable[[str, str], None] | None = None,
        record_handled_turn: Callable[[HandledTurnState], None] | None = None,
    ) -> PostResponseEffectsDeps:
        """Build the per-response post-effect dependency surface."""

        async def register_interactive(
            event_id: str,
            target: MessageTarget,
            option_map: dict[str, str],
            options_list: list[dict[str, str]],
        ) -> None:
            await self._register_interactive_delivery(
                event_id=event_id,
                room_id=room_id,
                target=target,
                option_map=option_map,
                options_list=options_list,
                agent_name=interactive_agent_name,
            )

        async def dispatch_compaction_notices(
            main_response_event_id: str,
            compaction_outcomes: Sequence[CompactionOutcome],
        ) -> None:
            await self._dispatch_compaction_notices(
                room_id=room_id,
                reply_to_event_id=reply_to_event_id,
                main_response_event_id=main_response_event_id,
                thread_id=thread_id,
                compaction_outcomes=compaction_outcomes,
            )

        return PostResponseEffectsDeps(
            logger=self.logger,
            register_interactive=register_interactive,
            dispatch_compaction_notices=dispatch_compaction_notices,
            strip_transient_enrichment=strip_transient_enrichment,
            queue_memory_persistence=queue_memory_persistence,
            persist_response_event_id=persist_response_event_id,
            queue_thread_summary=self.queue_thread_summary,
            record_handled_turn=record_handled_turn,
        )


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

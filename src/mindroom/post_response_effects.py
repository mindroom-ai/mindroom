"""Shared post-response effects for Matrix delivery flows."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from mindroom import interactive
from mindroom.background_tasks import create_background_task
from mindroom.matrix.conversation_reads import DeliveredResponse
from mindroom.runtime_protocols import SupportsClientConfig  # noqa: TC001
from mindroom.thread_summary import maybe_generate_thread_summary
from mindroom.thread_summary import should_queue_thread_summary as should_queue_thread_summary_check
from mindroom.timing import timed

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Sequence

    import nio
    import structlog
    from agno.db.base import SessionType

    from mindroom.constants import RuntimePaths
    from mindroom.final_delivery import FinalDeliveryOutcome
    from mindroom.matrix.client_visible_messages import ResolvedVisibleMessage
    from mindroom.matrix.conversation_reads import ConversationReader
    from mindroom.message_target import MessageTarget
    from mindroom.tool_system.worker_routing import ToolExecutionIdentity


@dataclass(frozen=True)
class ResponseOutcome:
    """Terminal response facts needed for post-delivery side effects."""

    response_run_id: str | None = None
    session_id: str | None = None
    session_type: SessionType | None = None
    execution_identity: ToolExecutionIdentity | None = None
    run_succeeded: bool = True
    response_target: MessageTarget | None = None
    thread_summary_room_id: str | None = None
    thread_summary_thread_id: str | None = None
    thread_summary_message_count_hint: int | None = None
    thread_summary_entity_name: str | None = None
    memory_prompt: str | None = None
    memory_thread_history: Sequence[ResolvedVisibleMessage] | None = None


@dataclass(frozen=True)
class PostResponseEffectsDeps:
    """Narrow side-effect surface needed to finalize one response."""

    logger: structlog.stdlib.BoundLogger
    add_interactive_buttons: Callable[[str, interactive.InteractiveMetadata], Awaitable[None]] | None = None
    queue_memory_persistence: Callable[[], None] | None = None
    persist_response_event_id: Callable[[str, str], None] | None = None
    should_queue_thread_summary: Callable[[str, str, int | None], bool] | None = None
    queue_thread_summary: Callable[[str, str, str | None, DeliveredResponse], None] | None = None


@dataclass(frozen=True)
class PostResponseEffectsSupport:
    """Shared support used to build per-response post-effect deps."""

    runtime: SupportsClientConfig
    logger: structlog.stdlib.BoundLogger
    runtime_paths: RuntimePaths
    conversation_reader: ConversationReader

    def _client(self) -> nio.AsyncClient:
        """Return the current Matrix client for interactive follow-up effects."""
        client = self.runtime.client
        if client is None:
            msg = "Matrix client is not ready for post-response effects"
            raise RuntimeError(msg)
        return client

    def _should_queue_thread_summary(
        self,
        room_id: str,
        thread_id: str,
        message_count_hint: int | None,
    ) -> bool:
        """Return whether a thread-summary check should be queued for this response."""
        return should_queue_thread_summary_check(
            room_id=room_id,
            thread_id=thread_id,
            config=self.runtime.config,
            runtime_paths=self.runtime_paths,
            message_count_hint=message_count_hint,
        )

    @timed("maybe_generate_thread_summary")
    async def _timed_thread_summary(
        self,
        *,
        summary_coro: Awaitable[None],
    ) -> None:
        """Run thread-summary generation with duration logging."""
        await summary_coro

    async def _add_interactive_buttons(
        self,
        *,
        event_id: str,
        room_id: str,
        interactive_metadata: interactive.InteractiveMetadata,
    ) -> None:
        """Add best-effort reaction buttons to an already durable prompt."""
        await interactive.add_reaction_buttons(
            self._client(),
            room_id,
            event_id,
            interactive_metadata.options_as_list(),
        )

    def _queue_thread_summary(
        self,
        room_id: str,
        thread_id: str,
        entity_name: str | None,
        delivered_response: DeliveredResponse,
    ) -> None:
        """Queue background thread summarization with timing instrumentation."""
        summary_coro = maybe_generate_thread_summary(
            client=self._client(),
            room_id=room_id,
            thread_id=thread_id,
            config=self.runtime.config,
            runtime_paths=self.runtime_paths,
            conversation_reader=self.conversation_reader,
            delivered_response=delivered_response,
            entity_name=entity_name,
        )
        create_background_task(
            self._timed_thread_summary(
                summary_coro=summary_coro,
            ),
            name=f"thread_summary_{room_id}_{thread_id}",
            owner=self.runtime,
        )

    def build_deps(
        self,
        *,
        room_id: str,
        queue_memory_persistence: Callable[[], None] | None = None,
        persist_response_event_id: Callable[[str, str], None] | None = None,
    ) -> PostResponseEffectsDeps:
        """Build the per-response post-effect dependency surface."""

        async def add_interactive_buttons(
            event_id: str,
            interactive_metadata: interactive.InteractiveMetadata,
        ) -> None:
            await self._add_interactive_buttons(
                event_id=event_id,
                room_id=room_id,
                interactive_metadata=interactive_metadata,
            )

        return PostResponseEffectsDeps(
            logger=self.logger,
            add_interactive_buttons=add_interactive_buttons,
            queue_memory_persistence=queue_memory_persistence,
            persist_response_event_id=persist_response_event_id,
            should_queue_thread_summary=self._should_queue_thread_summary,
            queue_thread_summary=self._queue_thread_summary,
        )


async def apply_post_response_effects(
    final_delivery_outcome: FinalDeliveryOutcome,
    outcome: ResponseOutcome,
    deps: PostResponseEffectsDeps,
) -> None:
    """Apply the shared side effects that happen after response delivery is known."""
    response_event_id = final_delivery_outcome.final_visible_event_id
    if (
        response_event_id is not None
        and deps.add_interactive_buttons is not None
        and final_delivery_outcome.terminal_status == "completed"
        and final_delivery_outcome.final_visible_body is not None
        and not final_delivery_outcome.suppressed
        and final_delivery_outcome.interactive_metadata is not None
    ):
        await deps.add_interactive_buttons(
            response_event_id,
            final_delivery_outcome.interactive_metadata,
        )
    else:  # noqa: PLR5501, RUF100
        if response_event_id is not None and (
            "React with an emoji or type the number to respond." in (final_delivery_outcome.final_visible_body or "")
            or final_delivery_outcome.interactive_metadata is not None
        ):
            deps.logger.warning(
                "Interactive question buttons skipped",
                response_event_id=response_event_id,
                add_interactive_buttons_is_none=deps.add_interactive_buttons is None,
                terminal_status=final_delivery_outcome.terminal_status,
                final_visible_body_is_none=final_delivery_outcome.final_visible_body is None,
                suppressed=final_delivery_outcome.suppressed,
                option_map_empty=not bool(final_delivery_outcome.interactive_metadata),
                options_list_empty=not bool(final_delivery_outcome.interactive_metadata),
            )

    if (
        outcome.response_run_id is not None
        and response_event_id is not None
        and deps.persist_response_event_id is not None
    ):
        try:
            deps.persist_response_event_id(outcome.response_run_id, response_event_id)
        except Exception:
            deps.logger.exception(
                "Failed to persist response event linkage in run metadata",
                session_id=outcome.session_id,
                run_id=outcome.response_run_id,
                response_event_id=response_event_id,
            )

    if outcome.run_succeeded and deps.queue_memory_persistence is not None:
        try:
            deps.queue_memory_persistence()
        except Exception:
            deps.logger.exception(
                "Failed to queue memory persistence after response",
                session_id=outcome.session_id,
                room_id=outcome.response_target.room_id if outcome.response_target is not None else None,
                thread_id=(outcome.response_target.resolved_thread_id if outcome.response_target is not None else None),
            )

    if (
        outcome.run_succeeded
        and final_delivery_outcome.terminal_status == "completed"
        and response_event_id is not None
        and not final_delivery_outcome.suppressed
        and outcome.thread_summary_room_id is not None
        and outcome.thread_summary_thread_id is not None
        and (
            deps.should_queue_thread_summary is None
            or deps.should_queue_thread_summary(
                outcome.thread_summary_room_id,
                outcome.thread_summary_thread_id,
                outcome.thread_summary_message_count_hint,
            )
        )
        and deps.queue_thread_summary is not None
    ):
        deps.queue_thread_summary(
            outcome.thread_summary_room_id,
            outcome.thread_summary_thread_id,
            outcome.thread_summary_entity_name,
            # Nothing has echoed this event back yet, so the projection the
            # summary reads stops one message before the answer it is being
            # queued for. The gate just above already counted that answer;
            # handing the fact over is what makes the pass agree with it.
            DeliveredResponse(
                event_id=response_event_id,
                body=final_delivery_outcome.final_visible_body or "",
            ),
        )

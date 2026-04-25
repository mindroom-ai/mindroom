"""Shared post-response effects for Matrix delivery flows."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from mindroom import interactive
from mindroom.background_tasks import create_background_task
from mindroom.delivery_gateway import MatrixCompactionLifecycle
from mindroom.message_target import MessageTarget
from mindroom.runtime_protocols import SupportsClientConfig  # noqa: TC001
from mindroom.thread_summary import maybe_generate_thread_summary
from mindroom.thread_summary import (
    should_queue_thread_summary as should_queue_thread_summary_check,
)
from mindroom.timing import timed

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Sequence
    from typing import Protocol

    import nio
    import structlog
    from agno.db.base import SessionType

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.delivery_gateway import DeliveryGateway
    from mindroom.final_delivery import FinalDeliveryOutcome
    from mindroom.history.types import CompactionOutcome, PostResponseCompactionCheck
    from mindroom.matrix.client_visible_messages import ResolvedVisibleMessage
    from mindroom.matrix.conversation_cache import ConversationCacheProtocol
    from mindroom.tool_system.worker_routing import ToolExecutionIdentity

    class PostResponseCompactionRunner(Protocol):
        """Callable shape for direct post-response compaction execution."""

        def __call__(
            self,
            *,
            check: PostResponseCompactionCheck,
            runtime_paths: RuntimePaths,
            config: Config,
            execution_identity: ToolExecutionIdentity | None,
            compaction_lifecycle: MatrixCompactionLifecycle,
        ) -> Awaitable[CompactionOutcome | None]:
            """Run one post-response compaction check."""
            ...


@dataclass(frozen=True)
class ResponseOutcome:
    """Terminal response facts needed for post-delivery side effects."""

    response_run_id: str | None = None
    session_id: str | None = None
    session_type: SessionType | None = None
    execution_identity: ToolExecutionIdentity | None = None
    run_succeeded: bool = True
    post_response_compaction_checks: tuple[PostResponseCompactionCheck, ...] = ()
    interactive_target: MessageTarget | None = None
    thread_summary_room_id: str | None = None
    thread_summary_thread_id: str | None = None
    thread_summary_message_count_hint: int | None = None
    memory_prompt: str | None = None
    memory_thread_history: Sequence[ResolvedVisibleMessage] | None = None


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
    queue_memory_persistence: Callable[[], None] | None = None
    run_post_response_compaction: Callable[[Sequence[PostResponseCompactionCheck], str], Awaitable[None]] | None = None
    persist_response_event_id: Callable[[str, str], None] | None = None
    should_queue_thread_summary: Callable[[str, str, int | None], bool] | None = None
    queue_thread_summary: Callable[[str, str, int | None], None] | None = None


@dataclass(frozen=True)
class PostResponseEffectsSupport:
    """Shared support used to build per-response post-effect deps."""

    runtime: SupportsClientConfig
    logger: structlog.stdlib.BoundLogger
    runtime_paths: RuntimePaths
    delivery_gateway: DeliveryGateway
    conversation_cache: ConversationCacheProtocol

    def _client(self) -> nio.AsyncClient:
        """Return the current Matrix client for interactive follow-up effects."""
        client = self.runtime.client
        if client is None:
            msg = "Matrix client is not ready for post-response effects"
            raise RuntimeError(msg)
        return client

    def should_queue_thread_summary(
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
            conversation_cache=self.conversation_cache,
            message_count_hint=message_count_hint,
        )
        create_background_task(
            self._timed_thread_summary(
                summary_coro=summary_coro,
            ),
            name=f"thread_summary_{room_id}_{thread_id}",
            owner=self.runtime,
        )

    async def run_post_response_compactions(
        self,
        checks: Sequence[PostResponseCompactionCheck],
        *,
        execution_identity: ToolExecutionIdentity | None,
        target: MessageTarget,
        reply_to_event_id: str,
        run_compaction: PostResponseCompactionRunner,
    ) -> None:
        """Run immediate post-response compaction checks before the turn fully completes."""
        for check in checks:
            compaction_lifecycle = MatrixCompactionLifecycle(
                delivery_gateway=self.delivery_gateway,
                target=target,
                reply_to_event_id=reply_to_event_id,
            )
            await run_compaction(
                check=check,
                runtime_paths=self.runtime_paths,
                config=self.runtime.config,
                execution_identity=execution_identity,
                compaction_lifecycle=compaction_lifecycle,
            )

    def build_deps(
        self,
        *,
        room_id: str,
        thread_id: str | None,
        interactive_agent_name: str,
        queue_memory_persistence: Callable[[], None] | None = None,
        persist_response_event_id: Callable[[str, str], None] | None = None,
        execution_identity: ToolExecutionIdentity | None = None,
        run_post_response_compaction: PostResponseCompactionRunner | None = None,
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

        return PostResponseEffectsDeps(
            logger=self.logger,
            register_interactive=register_interactive,
            queue_memory_persistence=queue_memory_persistence,
            run_post_response_compaction=(
                (
                    lambda checks, response_event_id: self.run_post_response_compactions(
                        checks,
                        execution_identity=execution_identity,
                        target=MessageTarget.resolve(
                            room_id=room_id,
                            thread_id=thread_id,
                            reply_to_event_id=response_event_id,
                        ),
                        reply_to_event_id=response_event_id,
                        run_compaction=run_post_response_compaction,
                    )
                )
                if run_post_response_compaction is not None
                else None
            ),
            persist_response_event_id=persist_response_event_id,
            should_queue_thread_summary=self.should_queue_thread_summary,
            queue_thread_summary=self.queue_thread_summary,
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
        and deps.register_interactive is not None
        and final_delivery_outcome.terminal_status == "completed"
        and final_delivery_outcome.final_visible_body is not None
        and not final_delivery_outcome.suppressed
        and final_delivery_outcome.option_map
        and final_delivery_outcome.options_list
        and outcome.interactive_target is not None
    ):
        await deps.register_interactive(
            response_event_id,
            outcome.interactive_target,
            dict(final_delivery_outcome.option_map),
            [dict(item) for item in final_delivery_outcome.options_list],
        )

    if deps.queue_memory_persistence is not None:
        try:
            deps.queue_memory_persistence()
        except Exception:
            deps.logger.exception(
                "Failed to queue memory persistence after response",
                session_id=outcome.session_id,
                room_id=outcome.interactive_target.room_id if outcome.interactive_target is not None else None,
                thread_id=(
                    outcome.interactive_target.resolved_thread_id if outcome.interactive_target is not None else None
                ),
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

    if (
        response_event_id is not None
        and final_delivery_outcome.terminal_status == "completed"
        and not final_delivery_outcome.suppressed
        and outcome.run_succeeded
        and outcome.post_response_compaction_checks
        and deps.run_post_response_compaction is not None
    ):
        try:
            await deps.run_post_response_compaction(outcome.post_response_compaction_checks, response_event_id)
        except Exception:
            deps.logger.exception(
                "Failed to run post-response compaction",
                session_id=outcome.session_id,
                room_id=outcome.interactive_target.room_id if outcome.interactive_target is not None else None,
                thread_id=(
                    outcome.interactive_target.resolved_thread_id if outcome.interactive_target is not None else None
                ),
            )

    if (
        response_event_id is not None
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
            outcome.thread_summary_message_count_hint,
        )

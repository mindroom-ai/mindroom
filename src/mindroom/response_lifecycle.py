"""Shared response lifecycle helpers for ResponseRunner."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

from mindroom.post_response_effects import apply_post_response_effects

if TYPE_CHECKING:
    from collections.abc import Callable

    from agno.db.base import BaseDb, SessionType

    from mindroom.final_delivery import FinalDeliveryOutcome
    from mindroom.history import HistoryScope
    from mindroom.hooks import MessageEnvelope
    from mindroom.post_response_effects import PostResponseEffectsDeps, ResponseOutcome
    from mindroom.tool_system.runtime_context import ToolRuntimeContext

    from .response_runner import ResponseRequest, ResponseRunner


@dataclass(frozen=True)
class SessionStartedWatch:
    """Pre-computed session:started eligibility and emission arguments."""

    should_watch: bool
    tool_context: ToolRuntimeContext | None
    scope: HistoryScope
    session_id: str
    room_id: str
    thread_id: str | None
    session_type: SessionType
    correlation_id: str
    create_storage: Callable[[], BaseDb]


class ResponseLifecycle:
    """Consolidate lifecycle helpers shared across response paths."""

    def __init__(
        self,
        runner: ResponseRunner,
        *,
        response_kind: str,
        request: ResponseRequest,
        response_envelope: MessageEnvelope,
        correlation_id: str,
    ) -> None:
        self.runner = runner
        self.response_kind = response_kind
        self.request = request
        self.response_envelope = response_envelope
        self.correlation_id = correlation_id

    def setup_session_watch(
        self,
        *,
        tool_context: ToolRuntimeContext | None,
        session_id: str,
        session_type: SessionType,
        scope: HistoryScope,
        room_id: str,
        thread_id: str | None,
        create_storage: Callable[[], BaseDb],
    ) -> SessionStartedWatch:
        """Pre-compute session:started eligibility for one response path."""
        return SessionStartedWatch(
            should_watch=self.runner._should_watch_session_started(
                tool_context=tool_context,
                session_id=session_id,
                session_type=session_type,
                create_storage=create_storage,
            ),
            tool_context=tool_context,
            scope=scope,
            session_id=session_id,
            room_id=room_id,
            thread_id=thread_id,
            session_type=session_type,
            correlation_id=self.correlation_id,
            create_storage=create_storage,
        )

    async def emit_session_started(self, watch: SessionStartedWatch) -> None:
        """Emit session:started using the existing runner-owned safety wrapper."""
        await self.runner._emit_session_started_safely(
            tool_context=watch.tool_context,
            should_watch_session_started=watch.should_watch,
            scope=watch.scope,
            session_id=watch.session_id,
            room_id=watch.room_id,
            thread_id=watch.thread_id,
            session_type=watch.session_type,
            correlation_id=watch.correlation_id,
            create_storage=watch.create_storage,
        )

    async def finalize(
        self,
        final_delivery_outcome: FinalDeliveryOutcome,
        *,
        build_post_response_outcome: Callable[[FinalDeliveryOutcome], ResponseOutcome],
        post_response_deps: PostResponseEffectsDeps | Callable[[], PostResponseEffectsDeps],
    ) -> FinalDeliveryOutcome:
        """Run outer lifecycle finalization and return the canonical terminal outcome."""
        response_event_id = final_delivery_outcome.final_visible_event_id
        try:
            if final_delivery_outcome.terminal_status == "completed":
                if (
                    response_event_id is not None
                    and final_delivery_outcome.final_visible_body is not None
                    and final_delivery_outcome.delivery_kind is not None
                ):
                    await self.runner.deps.delivery_gateway.deps.response_hooks.emit_after_response(
                        correlation_id=self.correlation_id,
                        envelope=self.response_envelope,
                        response_text=final_delivery_outcome.final_visible_body,
                        response_event_id=response_event_id,
                        delivery_kind=final_delivery_outcome.delivery_kind,
                        response_kind=self.response_kind,
                        continue_on_cancelled=True,
                    )
            else:
                await self.runner.deps.delivery_gateway.deps.response_hooks.emit_cancelled_response(
                    correlation_id=self.correlation_id,
                    envelope=self.response_envelope,
                    visible_response_event_id=response_event_id,
                    response_kind=self.response_kind,
                    failure_reason=final_delivery_outcome.failure_reason,
                )
        except asyncio.CancelledError as error:
            if response_event_id is None:
                raise
            self.runner._log_post_response_effects_failure(
                response_kind=self.response_kind,
                response_event_id=response_event_id,
                error=error,
            )
        except Exception as error:
            if response_event_id is None:
                raise
            self.runner._log_post_response_effects_failure(
                response_kind=self.response_kind,
                response_event_id=response_event_id,
                error=error,
            )
        await self.apply_effects_safely(
            final_delivery_outcome=final_delivery_outcome,
            post_response_outcome=lambda: build_post_response_outcome(final_delivery_outcome),
            post_response_deps=post_response_deps,
        )
        self.runner._emit_pipeline_timing_summary(
            self.request,
            outcome=self.runner._response_outcome(final_delivery_outcome),
        )
        return final_delivery_outcome

    async def apply_effects_safely(
        self,
        *,
        final_delivery_outcome: FinalDeliveryOutcome,
        post_response_outcome: ResponseOutcome | Callable[[], ResponseOutcome],
        post_response_deps: PostResponseEffectsDeps | Callable[[], PostResponseEffectsDeps],
    ) -> None:
        """Apply post-response effects without masking failures before visible delivery."""
        response_event_id = final_delivery_outcome.final_visible_event_id
        try:
            if callable(post_response_outcome):
                post_response_outcome = cast("Callable[[], ResponseOutcome]", post_response_outcome)()
            if callable(post_response_deps):
                post_response_deps = cast("Callable[[], PostResponseEffectsDeps]", post_response_deps)()
            await apply_post_response_effects(
                final_delivery_outcome,
                post_response_outcome,
                post_response_deps,
            )
        except asyncio.CancelledError as error:
            if response_event_id is None:
                raise
            self.runner._log_post_response_effects_failure(
                response_kind=self.response_kind,
                response_event_id=response_event_id,
                error=error,
            )
        except Exception as error:
            if response_event_id is None:
                raise
            self.runner._log_post_response_effects_failure(
                response_kind=self.response_kind,
                response_event_id=response_event_id,
                error=error,
            )

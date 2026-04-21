"""Shared response lifecycle helpers for ResponseRunner."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING

from mindroom.post_response_effects import apply_post_response_effects

if TYPE_CHECKING:
    from collections.abc import Callable

    from agno.db.base import SessionType
    from agno.db.sqlite import SqliteDb

    from mindroom.final_delivery import FinalDeliveryOutcome
    from mindroom.history.types import HistoryScope
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
    create_storage: Callable[[], SqliteDb]


@dataclass(frozen=True)
class DeliveryOutcome:
    """Terminal delivery facts for lifecycle finalization."""

    final_delivery_outcome: FinalDeliveryOutcome


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
        create_storage: Callable[[], SqliteDb],
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
        outcome: DeliveryOutcome,
        *,
        build_post_response_outcome: Callable[[FinalDeliveryOutcome], ResponseOutcome],
        post_response_deps: PostResponseEffectsDeps | Callable[[], PostResponseEffectsDeps],
    ) -> str | None:
        """Run outer lifecycle finalization and return the canonical final visible event id."""
        final_delivery_outcome = outcome.final_delivery_outcome
        final_visible_event_id = await self.apply_effects_safely(
            response_event_id=final_delivery_outcome.logical_response_event_id,
            post_response_outcome=lambda: build_post_response_outcome(final_delivery_outcome),
            post_response_deps=post_response_deps,
        )
        self.runner._emit_pipeline_timing_summary(
            self.request,
            outcome=self.runner._response_outcome(final_delivery_outcome),
        )
        return final_visible_event_id

    async def apply_effects_safely(
        self,
        *,
        response_event_id: str | None,
        post_response_outcome: ResponseOutcome | Callable[[], ResponseOutcome],
        post_response_deps: PostResponseEffectsDeps | Callable[[], PostResponseEffectsDeps],
    ) -> str | None:
        """Apply post-response effects without masking failures before visible delivery."""
        try:
            await apply_post_response_effects(
                post_response_outcome() if callable(post_response_outcome) else post_response_outcome,
                post_response_deps() if callable(post_response_deps) else post_response_deps,
            )
        except asyncio.CancelledError as error:
            if response_event_id is None:
                raise
            self.runner._log_post_response_effects_failure(
                response_kind=self.response_kind,
                response_event_id=response_event_id,
                error=error,
            )
            return response_event_id
        except Exception as error:
            if response_event_id is None:
                raise
            self.runner._log_post_response_effects_failure(
                response_kind=self.response_kind,
                response_event_id=response_event_id,
                error=error,
            )
            return response_event_id
        return response_event_id

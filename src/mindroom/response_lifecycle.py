"""Shared response lifecycle helpers for ResponseRunner."""

from __future__ import annotations

import asyncio
import copy
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from mindroom.constants import STREAM_STATUS_CANCELLED, STREAM_STATUS_KEY
from mindroom.post_response_effects import apply_post_response_effects

from .delivery_gateway import DeliveryResult, EditTextRequest

if TYPE_CHECKING:
    from collections.abc import Callable

    from agno.db.base import SessionType
    from agno.db.sqlite import SqliteDb

    from mindroom.history.types import HistoryScope
    from mindroom.hooks import MessageEnvelope
    from mindroom.message_target import MessageTarget
    from mindroom.post_response_effects import PostResponseEffectsDeps, ResponseOutcome
    from mindroom.tool_system.events import ToolTraceEntry
    from mindroom.tool_system.runtime_context import ToolRuntimeContext

    from .response_runner import ResponseRequest, ResponseRunner
    from .streaming import StreamDeliveryState, StreamFinalizationOutcome


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

    delivery_result: DeliveryResult | None = None
    delivery_failure_reason: str | None = None
    tracked_event_id: str | None = None
    stream_finalization: StreamFinalizationOutcome | None = None
    stream_state: StreamDeliveryState | None = None
    streaming_repair: StreamingRepair | None = None


@dataclass(frozen=True)
class StreamingRepair:
    """Best-effort payload for repairing one missed terminal stream edit."""

    target: MessageTarget
    response_text: str
    tool_trace: list[ToolTraceEntry] | None = None
    extra_content: dict[str, Any] | None = None
    option_map: dict[str, str] | None = None
    options_list: list[dict[str, str]] | None = None


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
        build_post_response_outcome: Callable[[str | None, DeliveryResult | None], ResponseOutcome],
        post_response_deps: PostResponseEffectsDeps | Callable[[], PostResponseEffectsDeps],
    ) -> str | None:
        """Run outer lifecycle finalization and return the resolved visible event id."""
        delivery_result = outcome.delivery_result
        repaired_delivery_result = await self._repair_stream_terminal_state(outcome)
        effective_delivery_result = repaired_delivery_result or delivery_result
        post_response_delivery_result = effective_delivery_result
        if self.runner._is_cancelled_delivery_result(delivery_result) and (
            outcome.stream_finalization is None
            or outcome.stream_finalization.terminal_status == STREAM_STATUS_CANCELLED
        ):
            post_response_delivery_result = delivery_result
            await self.runner.deps.delivery_gateway.deps.response_hooks.emit_cancelled_response(
                correlation_id=self.correlation_id,
                envelope=self.response_envelope,
                visible_response_event_id=(
                    repaired_delivery_result.event_id
                    if repaired_delivery_result is not None
                    else outcome.tracked_event_id
                ),
                response_kind=self.response_kind,
                failure_reason=outcome.delivery_failure_reason
                or (delivery_result.failure_reason if delivery_result is not None else None),
            )

        resolved_event_id = self.runner.resolve_response_event_id(
            delivery_result=effective_delivery_result,
            tracked_event_id=outcome.tracked_event_id,
            existing_event_id=self.request.existing_event_id,
            existing_event_is_placeholder=self.request.existing_event_is_placeholder,
        )
        resolved_event_id = await self.apply_effects_safely(
            resolved_event_id=resolved_event_id,
            post_response_outcome=lambda: build_post_response_outcome(
                resolved_event_id,
                post_response_delivery_result,
            ),
            post_response_deps=post_response_deps,
        )
        self.runner._emit_pipeline_timing_summary(
            self.request,
            outcome=self.runner._response_outcome(post_response_delivery_result),
        )
        return resolved_event_id

    async def _repair_stream_terminal_state(  # noqa: PLR0911
        self,
        outcome: DeliveryOutcome,
    ) -> DeliveryResult | None:
        """Repair one missed terminal stream edit from the outer lifecycle chokepoint."""
        stream_finalization = outcome.stream_finalization
        if stream_finalization is None or stream_finalization.terminal_landed:
            return None
        if outcome.delivery_result is not None and outcome.delivery_result.suppressed:
            return None
        if outcome.stream_state is not None and outcome.stream_state.suppressed_and_cleaned:
            return None
        use_stream_state_repair = outcome.stream_state is not None and outcome.stream_state.repair_text is not None
        if not use_stream_state_repair and outcome.streaming_repair is None:
            return None
        if outcome.tracked_event_id is None:
            return None

        repair = outcome.streaming_repair
        if use_stream_state_repair:
            assert outcome.stream_state is not None
            assert outcome.stream_state.repair_text is not None
            repair_text = outcome.stream_state.repair_text
            repair_tool_trace = outcome.stream_state.repair_tool_trace
            repair_extra_content = outcome.stream_state.repair_extra_content
            repair_option_map = outcome.stream_state.repair_option_map
            repair_options_list = outcome.stream_state.repair_options_list
        else:
            assert repair is not None
            repair_text = repair.response_text
            repair_tool_trace = repair.tool_trace
            repair_extra_content = repair.extra_content
            repair_option_map = repair.option_map
            repair_options_list = repair.options_list

        extra_content = copy.deepcopy(repair_extra_content or {})
        extra_content[STREAM_STATUS_KEY] = stream_finalization.terminal_status
        try:
            repaired = await self.runner.deps.delivery_gateway.edit_text(
                EditTextRequest(
                    target=(
                        repair.target if repair is not None else self.request.target or self.response_envelope.target
                    ),
                    event_id=outcome.tracked_event_id,
                    new_text=repair_text,
                    tool_trace=repair_tool_trace,
                    extra_content=extra_content,
                ),
            )
        except asyncio.CancelledError as error:
            self.runner.deps.logger.warning(
                "Outer stream repair edit failed; visible event remains stale",
                response_kind=self.response_kind,
                event_id=outcome.tracked_event_id,
                terminal_status=stream_finalization.terminal_status,
                reason=stream_finalization.reason,
                failure_reason=str(error),
                error_type=error.__class__.__name__,
            )
            return None
        except Exception as error:
            self.runner.deps.logger.warning(
                "Outer stream repair edit failed; visible event remains stale",
                response_kind=self.response_kind,
                event_id=outcome.tracked_event_id,
                terminal_status=stream_finalization.terminal_status,
                reason=stream_finalization.reason,
                failure_reason=str(error),
                error_type=error.__class__.__name__,
            )
            return None
        if not repaired:
            self.runner.deps.logger.error(
                "Failed to repair missed terminal stream edit",
                response_kind=self.response_kind,
                event_id=outcome.tracked_event_id,
                terminal_status=stream_finalization.terminal_status,
                reason=stream_finalization.reason,
            )
            return None
        self.runner.deps.logger.info(
            "Repaired missed terminal stream edit",
            response_kind=self.response_kind,
            event_id=outcome.tracked_event_id,
            terminal_status=stream_finalization.terminal_status,
            reason=stream_finalization.reason,
        )
        return DeliveryResult(
            event_id=outcome.tracked_event_id,
            response_text=repair_text,
            delivery_kind="edited",
            suppressed=False,
            option_map=copy.deepcopy(repair_option_map),
            options_list=copy.deepcopy(repair_options_list),
        )

    async def apply_effects_safely(
        self,
        *,
        resolved_event_id: str | None,
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
            if resolved_event_id is None:
                raise
            self.runner._log_post_response_effects_failure(
                response_kind=self.response_kind,
                response_event_id=resolved_event_id,
                error=error,
            )
            return resolved_event_id
        except Exception as error:
            if resolved_event_id is None:
                raise
            self.runner._log_post_response_effects_failure(
                response_kind=self.response_kind,
                response_event_id=resolved_event_id,
                error=error,
            )
            return resolved_event_id
        return resolved_event_id

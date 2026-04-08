"""Response lifecycle coordination extracted from ``bot.py``."""

from __future__ import annotations

import asyncio
import os
import time
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any, Literal, Protocol, cast
from uuid import uuid4

from agno.db.base import SessionType
from agno.run.agent import RunContentEvent

from mindroom import interactive
from mindroom.ai import queued_message_signal_context
from mindroom.constants import ROUTER_AGENT_NAME, STREAM_STATUS_KEY, STREAM_STATUS_PENDING
from mindroom.hooks import EnrichmentItem, MessageEnvelope
from mindroom.post_response_effects import PostResponseEffectsDeps, ResponseOutcome, apply_post_response_effects
from mindroom.streaming import (
    ReplacementStreamingResponse,
    StreamingDeliveryError,
    StreamingResponse,
)
from mindroom.teams import TeamMode
from mindroom.timing import timed
from mindroom.timing import timing_scope as timing_scope_context

from .delivery_gateway import (
    DeliveryGateway,
    DeliveryResult,
    StreamingDeliveryRequest,
)
from .media_inputs import MediaInputs

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable, Coroutine, Mapping, Sequence
    from contextlib import AbstractAsyncContextManager
    from pathlib import Path

    import nio
    import structlog
    from agno.db.sqlite import SqliteDb

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.history.types import CompactionOutcome
    from mindroom.hooks import ResponseDraft
    from mindroom.matrix.client import ResolvedVisibleMessage
    from mindroom.matrix.identity import MatrixID
    from mindroom.message_target import MessageTarget
    from mindroom.orchestrator import MultiAgentOrchestrator
    from mindroom.stop import StopManager
    from mindroom.tool_system.runtime_context import ToolRuntimeContext
    from mindroom.tool_system.worker_routing import ToolExecutionIdentity

_CANCELLED_RESPONSE_TEXT = "**[Response cancelled by user]**"


def _thread_summary_message_count_hint(
    thread_history: Sequence[ResolvedVisibleMessage],
) -> int:
    """Return a lower-bound post-response thread size without refetching history."""
    existing_non_summary_messages = sum(
        1 for message in thread_history if not isinstance(message.content.get("io.mindroom.thread_summary"), dict)
    )
    return existing_non_summary_messages + 1


class _ReplyEventWithSource(Protocol):
    """Minimal reply event surface needed for skill command responses."""

    source: dict[str, Any]


class _QueuedSignal(Protocol):
    """Minimal queued-message state surface needed by the coordinator."""

    def begin_response_turn(self) -> bool: ...

    def finish_response_turn(self) -> None: ...

    def add_waiting_human_message(self) -> None: ...

    def consume_waiting_human_message(self) -> None: ...

    def has_pending_human_messages(self) -> bool: ...


@dataclass(frozen=True)
class ResponseRequest:
    """Typed carrier for one response lifecycle request."""

    room_id: str
    reply_to_event_id: str
    thread_id: str | None
    thread_history: Sequence[ResolvedVisibleMessage]
    prompt: str
    model_prompt: str | None = None
    existing_event_id: str | None = None
    existing_event_is_placeholder: bool = False
    user_id: str | None = None
    media: MediaInputs | None = None
    attachment_ids: tuple[str, ...] | None = None
    response_envelope: MessageEnvelope | None = None
    correlation_id: str | None = None
    target: MessageTarget | None = None
    matrix_run_metadata: Mapping[str, Any] | None = None
    system_enrichment_items: tuple[EnrichmentItem, ...] = ()
    strip_transient_enrichment_after_run: bool = False
    received_monotonic: float | None = None
    on_lifecycle_lock_acquired: Callable[[], None] | None = None


@dataclass(frozen=True)
class TeamResponseRequest:
    """Typed carrier for one team response request plus team-specific inputs."""

    request: ResponseRequest
    team_agents: tuple[MatrixID, ...]
    team_mode: str
    reason_prefix: str = "Team request"


@dataclass(frozen=True)
class ResponseCoordinatorDeps:
    """Explicit collaborators for the response lifecycle."""

    client: nio.AsyncClient
    logger: structlog.stdlib.BoundLogger
    stop_manager: StopManager
    config: Config
    runtime_paths: RuntimePaths
    storage_path: Path
    agent_name: str
    matrix_full_id: str
    enable_streaming: bool
    show_tool_calls: bool
    orchestrator: MultiAgentOrchestrator | None
    cancelled_response_text: str
    ai_response: Callable[..., Awaitable[str]]
    is_user_online: Callable[..., Awaitable[bool]]
    should_use_streaming: Callable[..., Awaitable[bool]]
    typing_indicator: Callable[[nio.AsyncClient, str], AbstractAsyncContextManager[None]]
    team_response: Callable[..., Awaitable[str]]
    team_response_stream: Callable[..., Any]
    build_message_target: Callable[..., MessageTarget]
    response_lifecycle_lock: Callable[[MessageTarget], asyncio.Lock]
    get_or_create_queued_signal: Callable[[MessageTarget], _QueuedSignal]
    should_signal_queued_message: Callable[[MessageEnvelope | None], bool]
    acquire_response_lifecycle_lock: Callable[[asyncio.Lock], Awaitable[None]]
    prepare_memory_and_model_context: Callable[
        ...,
        tuple[str, Sequence[ResolvedVisibleMessage], str, list[ResolvedVisibleMessage]],
    ]
    timestamp_model_user_context: Callable[
        [str, Sequence[ResolvedVisibleMessage]],
        tuple[str, list[ResolvedVisibleMessage]],
    ]
    resolve_response_thread_root: Callable[..., str | None]
    append_matrix_prompt_context: Callable[..., str]
    build_tool_runtime_context: Callable[..., ToolRuntimeContext | None]
    build_tool_execution_identity: Callable[..., ToolExecutionIdentity]
    ensure_request_knowledge_managers: Callable[..., Awaitable[dict[str, Any]]]
    knowledge_for_agent: Callable[..., object]
    active_response_event_ids: Callable[[str], set[str]]
    stream_agent_response: Callable[..., Any]
    stream_in_tool_context: Callable[..., Any]
    run_in_tool_context: Callable[..., Awaitable[Any]]
    send_response: Callable[..., Awaitable[str | None]]
    edit_message: Callable[..., Awaitable[bool | None]]
    create_background_task: Callable[..., asyncio.Task[Any]]
    store_conversation_memory: Callable[..., Awaitable[None]]
    mark_auto_flush_dirty_session: Callable[..., None]
    reprioritize_auto_flush_sessions: Callable[..., None]
    strip_enrichment_from_session_storage: Callable[..., bool]
    clear_tracked_response_message: Callable[..., None]
    select_model_for_team: Callable[..., str]
    delivery_gateway: Callable[[], DeliveryGateway]
    deliver_generated_response: Callable[..., Awaitable[DeliveryResult]]
    apply_before_response_hooks: Callable[..., Awaitable[ResponseDraft]]
    emit_after_response_hooks: Callable[..., Awaitable[None]]
    post_response_effects_deps: Callable[..., PostResponseEffectsDeps]
    create_history_scope_storage: Callable[[ToolExecutionIdentity | None], SqliteDb]
    create_team_history_storage: Callable[..., SqliteDb]
    persist_response_event_id_in_session_run: Callable[..., None]
    queue_timed_thread_summary: Callable[..., None]
    thread_summary_message_count_hint: Callable[[Sequence[ResolvedVisibleMessage]], int | None]
    history_session_type: Callable[[], SessionType]
    show_tool_calls_for_agent: Callable[[str], bool]
    agent_has_matrix_messaging_tool: Callable[[str], bool]
    increment_in_flight_response_count: Callable[[], None]
    decrement_in_flight_response_count: Callable[[], None]
    merge_response_extra_content: Callable[[dict[str, Any] | None, Sequence[str] | None], dict[str, Any] | None]


@dataclass(frozen=True)
class _PreparedResponseRuntime:
    """Resolved runtime context shared by streaming and non-streaming responses."""

    resolved_target: MessageTarget
    response_thread_id: str | None
    media_inputs: MediaInputs
    session_id: str
    model_prompt: str
    tool_context: ToolRuntimeContext | None
    execution_identity: ToolExecutionIdentity
    request_knowledge_managers: dict[str, Any]
    room_mode: bool = False


@dataclass(frozen=True)
class ResponseCoordinator:
    """Coordinate one response lifecycle while keeping bot seams patchable."""

    deps: ResponseCoordinatorDeps

    def _resolve_request_target(self, request: ResponseRequest) -> MessageTarget:
        """Resolve the canonical response target for one request."""
        return request.target or (
            request.response_envelope.target
            if request.response_envelope is not None
            else self.deps.build_message_target(
                room_id=request.room_id,
                thread_id=request.thread_id,
                reply_to_event_id=request.reply_to_event_id,
            )
        )

    async def _run_locked_response_lifecycle(
        self,
        request: ResponseRequest,
        *,
        locked_operation: Callable[[MessageTarget], Awaitable[str | None]],
    ) -> str | None:
        """Run one locked response operation with shared queued-message bookkeeping."""
        resolved_target = self._resolve_request_target(request)
        lifecycle_lock = self.deps.response_lifecycle_lock(resolved_target)
        queued_signal = self.deps.get_or_create_queued_signal(resolved_target)
        existing_turn = queued_signal.begin_response_turn()
        queued_human_message = (existing_turn or lifecycle_lock.locked()) and self.deps.should_signal_queued_message(
            request.response_envelope,
        )
        if queued_human_message:
            queued_signal.add_waiting_human_message()
        lock_acquired = False
        try:
            await self.deps.acquire_response_lifecycle_lock(lifecycle_lock)
            lock_acquired = True
            try:
                if queued_human_message:
                    queued_signal.consume_waiting_human_message()
                    queued_human_message = False
                with queued_message_signal_context(queued_signal):
                    return await locked_operation(resolved_target)
            finally:
                if lock_acquired:
                    lifecycle_lock.release()
        finally:
            if queued_human_message:
                queued_signal.consume_waiting_human_message()
            queued_signal.finish_response_turn()

    def _build_session_storage_effects(
        self,
        *,
        session_id: str,
        session_type: SessionType,
        create_storage: Callable[[], SqliteDb],
    ) -> tuple[Callable[[], None], Callable[[str, str], None]]:
        """Build the shared strip/persist callbacks for one session-backed response."""

        def strip_transient_enrichment() -> None:
            storage = create_storage()
            try:
                self.deps.strip_enrichment_from_session_storage(
                    storage,
                    session_id,
                    session_type=session_type,
                )
            finally:
                storage.close()

        def persist_response_event_id(run_id: str, response_event_id: str) -> None:
            storage = create_storage()
            try:
                self.deps.persist_response_event_id_in_session_run(
                    storage=storage,
                    session_id=session_id,
                    session_type=session_type,
                    run_id=run_id,
                    response_event_id=response_event_id,
                )
            finally:
                storage.close()

        return strip_transient_enrichment, persist_response_event_id

    def _request_for_delivery(
        self,
        request: ResponseRequest,
        *,
        message_id: str | None,
    ) -> ResponseRequest:
        """Attach the current visible event id to one delivery request."""
        if message_id is None:
            return request
        if request.existing_event_id is None:
            return replace(request, existing_event_id=message_id, existing_event_is_placeholder=True)
        return replace(request, existing_event_id=message_id)

    async def _await_post_response_effects(
        self,
        *,
        finalize_effects: Callable[[str | None], Coroutine[Any, Any, None]],
        tracked_event_id: str | None,
        swallow_late_cancellation: bool = False,
    ) -> None:
        """Finish post-response cleanup even when cancellation lands after delivery."""
        if not swallow_late_cancellation:
            await finalize_effects(tracked_event_id)
            return

        finalize_task = asyncio.create_task(finalize_effects(tracked_event_id))
        try:
            await asyncio.shield(finalize_task)
        except asyncio.CancelledError:
            self.deps.logger.warning(
                "Late cancellation arrived during post-response cleanup; finishing cleanup",
                message_id=tracked_event_id,
            )
            current_task = asyncio.current_task()
            if current_task is not None:
                while current_task.cancelling():
                    current_task.uncancel()
            await finalize_task

    def _response_envelope_for_request(
        self,
        request: ResponseRequest,
        *,
        resolved_target: MessageTarget,
        requester_id: str | None = None,
        sender_id: str | None = None,
    ) -> MessageEnvelope:
        """Resolve the hook envelope for one response request."""
        if request.response_envelope is not None:
            return request.response_envelope
        resolved_requester_id = (
            requester_id if requester_id is not None else request.user_id or self.deps.matrix_full_id
        )
        resolved_sender_id = sender_id if sender_id is not None else request.user_id or self.deps.matrix_full_id
        return MessageEnvelope(
            source_event_id=request.reply_to_event_id,
            room_id=request.room_id,
            target=resolved_target,
            requester_id=resolved_requester_id,
            sender_id=resolved_sender_id,
            body=request.prompt,
            attachment_ids=tuple(request.attachment_ids or ()),
            mentioned_agents=(),
            agent_name=self.deps.agent_name,
            source_kind="message",
        )

    def _correlation_id_for_request(self, request: ResponseRequest) -> str:
        """Resolve the correlation id for one request."""
        return request.correlation_id or request.reply_to_event_id

    async def generate_team_response_helper(
        self,
        request: ResponseRequest,
        *,
        team_agents: list[MatrixID],
        team_mode: str,
        reason_prefix: str = "Team request",
    ) -> str | None:
        """Generate a team response with lifecycle locking and queued-message state."""
        team_request = TeamResponseRequest(
            request=request,
            team_agents=tuple(team_agents),
            team_mode=team_mode,
            reason_prefix=reason_prefix,
        )
        return await self._run_locked_response_lifecycle(
            request,
            locked_operation=lambda resolved_target: self.generate_team_response_helper_locked(
                team_request,
                resolved_target=resolved_target,
            ),
        )

    async def generate_team_response_helper_locked(  # noqa: C901, PLR0915
        self,
        team_request: TeamResponseRequest,
        *,
        resolved_target: MessageTarget,
    ) -> str | None:
        """Generate a team response once the per-thread lifecycle lock is held."""
        request = team_request.request
        if request.on_lifecycle_lock_acquired is not None:
            request.on_lifecycle_lock_acquired()
        requester_user_id = request.user_id or ""
        prepared_prompt, model_thread_history = self.deps.timestamp_model_user_context(
            request.model_prompt or request.prompt,
            request.thread_history,
        )
        model_name = self.deps.select_model_for_team(
            self.deps.agent_name,
            request.room_id,
            self.deps.config,
            self.deps.runtime_paths,
        )
        room_mode = (
            self.deps.config.get_entity_thread_mode(
                self.deps.agent_name,
                self.deps.runtime_paths,
                room_id=request.room_id,
            )
            == "room"
        )
        use_streaming = await self.deps.should_use_streaming(
            self.deps.client,
            request.room_id,
            requester_user_id=requester_user_id,
            enable_streaming=self.deps.enable_streaming,
        )
        mode = TeamMode.COORDINATE if team_request.team_mode == "coordinate" else TeamMode.COLLABORATE
        agent_names = [
            mid.agent_name(self.deps.config, self.deps.runtime_paths) or mid.username
            for mid in team_request.team_agents
        ]
        self.deps.config.assert_team_agents_supported(
            [agent_name for agent_name in agent_names if agent_name != ROUTER_AGENT_NAME],
        )
        include_matrix_prompt_context = any(self.deps.agent_has_matrix_messaging_tool(name) for name in agent_names)
        response_thread_id = resolved_target.resolved_thread_id
        resolved_target = resolved_target.with_thread_root(response_thread_id)
        model_message = self.deps.append_matrix_prompt_context(
            prepared_prompt,
            target=resolved_target,
            include_context=include_matrix_prompt_context,
        )
        resolved_request = replace(
            request,
            target=resolved_target,
            thread_history=model_thread_history,
            media=request.media or MediaInputs(),
        )
        resolved_response_envelope = self._response_envelope_for_request(
            request,
            resolved_target=resolved_target,
            requester_id=requester_user_id,
            sender_id=requester_user_id,
        )
        resolved_correlation_id = self._correlation_id_for_request(request)
        delivery_target = (
            resolved_target
            if request.existing_event_id is None or request.existing_event_is_placeholder
            else resolved_target.with_thread_root(request.thread_id)
        )
        delivery_request_base = replace(resolved_request, target=delivery_target)
        session_id = resolved_target.session_id
        tool_context = self.deps.build_tool_runtime_context(
            resolved_target,
            user_id=requester_user_id,
            active_model_name=model_name,
            session_id=session_id,
            attachment_ids=request.attachment_ids,
            correlation_id=resolved_correlation_id,
            source_envelope=request.response_envelope,
        )
        execution_identity = self.deps.build_tool_execution_identity(
            target=resolved_target,
            user_id=requester_user_id,
            session_id=session_id,
        )
        orchestrator = self.deps.orchestrator
        if orchestrator is None:
            msg = "Orchestrator is not set"
            raise RuntimeError(msg)
        response_run_id = str(uuid4())
        delivery_result: DeliveryResult | None = None
        compaction_outcomes: list[CompactionOutcome] = []
        resolved_event_id: str | None = None

        strip_transient_enrichment, persist_response_event_id = self._build_session_storage_effects(
            session_id=session_id,
            session_type=SessionType.TEAM,
            create_storage=lambda: self.deps.create_team_history_storage(
                team_agents=list(team_request.team_agents),
                execution_identity=execution_identity,
            ),
        )

        async def finalize_post_response_effects(message_id: str | None) -> None:
            nonlocal resolved_event_id
            resolved_event_id = self.resolve_response_event_id(
                delivery_result=delivery_result,
                tracked_event_id=message_id,
                existing_event_id=request.existing_event_id,
                existing_event_is_placeholder=request.existing_event_is_placeholder,
            )
            await apply_post_response_effects(
                ResponseOutcome(
                    resolved_event_id=resolved_event_id,
                    delivery_result=delivery_result,
                    response_run_id=response_run_id,
                    session_id=session_id,
                    session_type=SessionType.TEAM,
                    execution_identity=execution_identity,
                    compaction_outcomes=tuple(compaction_outcomes),
                    interactive_target=resolved_target,
                    strip_transient_enrichment_after_run=request.strip_transient_enrichment_after_run,
                    strip_transient_enrichment_before_effects=True,
                    dispatch_compaction_when_suppressed=True,
                ),
                self.deps.post_response_effects_deps(
                    room_id=request.room_id,
                    reply_to_event_id=request.reply_to_event_id,
                    thread_id=request.thread_id,
                    interactive_agent_name=self.deps.agent_name,
                    strip_transient_enrichment=strip_transient_enrichment,
                    persist_response_event_id=persist_response_event_id,
                ),
            )

        async def generate_team_response(message_id: str | None) -> None:  # noqa: C901
            nonlocal delivery_result
            delivery_request = self._request_for_delivery(delivery_request_base, message_id=message_id)
            delivery_target = delivery_request.target
            assert delivery_target is not None

            def _note_attempt_run_id(current_run_id: str) -> None:
                self.deps.stop_manager.update_run_id(message_id, current_run_id)

            if use_streaming and (
                delivery_request.existing_event_id is None or delivery_request.existing_event_is_placeholder
            ):
                async with self.deps.typing_indicator(self.deps.client, request.room_id):

                    def build_response_stream() -> AsyncIterator[object]:
                        return self.deps.team_response_stream(
                            agent_ids=list(team_request.team_agents),
                            message=model_message,
                            orchestrator=orchestrator,
                            execution_identity=execution_identity,
                            mode=mode,
                            thread_history=model_thread_history,
                            model_name=model_name,
                            media=resolved_request.media,
                            show_tool_calls=self.deps.show_tool_calls,
                            session_id=session_id,
                            run_id=response_run_id,
                            run_id_callback=_note_attempt_run_id,
                            user_id=requester_user_id,
                            reply_to_event_id=request.reply_to_event_id,
                            active_event_ids=self.deps.active_response_event_ids(request.room_id),
                            response_sender_id=self.deps.matrix_full_id,
                            compaction_outcomes_collector=compaction_outcomes,
                            configured_team_name=self.deps.agent_name
                            if self.deps.agent_name in self.deps.config.teams
                            else None,
                            system_enrichment_items=request.system_enrichment_items,
                            reason_prefix=team_request.reason_prefix,
                            matrix_run_metadata=request.matrix_run_metadata,
                        )

                    response_stream = self.deps.stream_in_tool_context(
                        execution_identity=execution_identity,
                        tool_context=tool_context,
                        stream_factory=build_response_stream,
                    )

                    event_id, accumulated = await self.deps.delivery_gateway().deliver_stream(
                        StreamingDeliveryRequest(
                            room_id=request.room_id,
                            reply_to_event_id=request.reply_to_event_id,
                            response_thread_id=delivery_target.resolved_thread_id,
                            response_stream=response_stream,
                            existing_event_id=delivery_request.existing_event_id,
                            adopt_existing_placeholder=delivery_request.existing_event_id is not None
                            and delivery_request.existing_event_is_placeholder,
                            target=delivery_target,
                            room_mode=room_mode,
                            header=None,
                            show_tool_calls=self.deps.show_tool_calls,
                            streaming_cls=ReplacementStreamingResponse,
                        ),
                    )
                if event_id is None:
                    delivery_result = DeliveryResult(
                        event_id=None,
                        response_text=accumulated,
                        delivery_kind=None,
                    )
                    return

                delivery_kind: Literal["sent", "edited"] = "edited" if message_id else "sent"
                draft = await self.deps.apply_before_response_hooks(
                    correlation_id=resolved_correlation_id,
                    envelope=resolved_response_envelope,
                    response_text=accumulated,
                    response_kind="team",
                    tool_trace=None,
                    extra_content=None,
                )
                if draft.suppress:
                    if delivery_request.existing_event_is_placeholder or delivery_request.existing_event_id is None:
                        delivery_result = await self.deps.delivery_gateway().cleanup_suppressed_streamed_response(
                            room_id=request.room_id,
                            event_id=event_id,
                            response_text=accumulated,
                            response_kind="team",
                            response_envelope=resolved_response_envelope,
                            correlation_id=resolved_correlation_id,
                        )
                    else:
                        self.deps.logger.warning(
                            "Team streaming response was already delivered before a suppressing hook ran",
                            source_event_id=resolved_response_envelope.source_event_id,
                            correlation_id=resolved_correlation_id,
                        )
                        delivery_result = DeliveryResult(
                            event_id=event_id,
                            response_text=accumulated,
                            delivery_kind=delivery_kind,
                            suppressed=True,
                        )
                    return

                if draft.response_text != accumulated:
                    delivery_result = await self.deps.deliver_generated_response(
                        room_id=request.room_id,
                        reply_to_event_id=request.reply_to_event_id,
                        thread_id=request.thread_id,
                        target=delivery_request.target,
                        existing_event_id=event_id,
                        existing_event_is_placeholder=delivery_request.existing_event_is_placeholder,
                        response_text=draft.response_text,
                        response_kind="team",
                        response_envelope=resolved_response_envelope,
                        correlation_id=resolved_correlation_id,
                        tool_trace=None,
                        extra_content=None,
                        apply_before_hooks=False,
                    )
                else:
                    interactive_response = interactive.parse_and_format_interactive(
                        accumulated,
                        extract_mapping=True,
                    )
                    await self.deps.emit_after_response_hooks(
                        correlation_id=resolved_correlation_id,
                        envelope=resolved_response_envelope,
                        response_text=interactive_response.formatted_text,
                        response_event_id=event_id,
                        delivery_kind=delivery_kind,
                        response_kind="team",
                    )
                    delivery_result = DeliveryResult(
                        event_id=event_id,
                        response_text=interactive_response.formatted_text,
                        delivery_kind=delivery_kind,
                        option_map=interactive_response.option_map,
                        options_list=interactive_response.options_list,
                    )
            else:
                try:
                    async with self.deps.typing_indicator(self.deps.client, request.room_id):

                        async def build_response_text() -> str:
                            return await self.deps.team_response(
                                agent_names=agent_names,
                                mode=mode,
                                message=model_message,
                                orchestrator=orchestrator,
                                execution_identity=execution_identity,
                                thread_history=model_thread_history,
                                model_name=model_name,
                                media=resolved_request.media,
                                session_id=session_id,
                                run_id=response_run_id,
                                run_id_callback=_note_attempt_run_id,
                                user_id=requester_user_id,
                                reply_to_event_id=request.reply_to_event_id,
                                active_event_ids=self.deps.active_response_event_ids(request.room_id),
                                response_sender_id=self.deps.matrix_full_id,
                                compaction_outcomes_collector=compaction_outcomes,
                                configured_team_name=self.deps.agent_name
                                if self.deps.agent_name in self.deps.config.teams
                                else None,
                                system_enrichment_items=request.system_enrichment_items,
                                reason_prefix=team_request.reason_prefix,
                                matrix_run_metadata=request.matrix_run_metadata,
                            )

                        response_text = await self.deps.run_in_tool_context(
                            execution_identity=execution_identity,
                            tool_context=tool_context,
                            operation=build_response_text,
                        )
                except asyncio.CancelledError:
                    self.deps.logger.warning(
                        "Team non-streaming response cancelled — traceback for diagnosis",
                        message_id=message_id,
                        exc_info=True,
                    )
                    if message_id:
                        await self.deps.edit_message(
                            request.room_id,
                            message_id,
                            self.deps.cancelled_response_text,
                            delivery_target.resolved_thread_id,
                        )
                    raise

                delivery_result = await self.deps.deliver_generated_response(
                    room_id=request.room_id,
                    reply_to_event_id=request.reply_to_event_id,
                    thread_id=request.thread_id,
                    target=delivery_request.target,
                    existing_event_id=message_id,
                    existing_event_is_placeholder=delivery_request.existing_event_is_placeholder,
                    response_text=response_text,
                    response_kind="team",
                    response_envelope=resolved_response_envelope,
                    correlation_id=resolved_correlation_id,
                    tool_trace=None,
                    extra_content=None,
                )

        thinking_msg = None
        if not request.existing_event_id:
            thinking_msg = "🤝 Team Response: Thinking..."

        tracked_event_id = await self.run_cancellable_response(
            room_id=request.room_id,
            reply_to_event_id=request.reply_to_event_id,
            thread_id=request.thread_id,
            target=delivery_target,
            response_function=generate_team_response,
            thinking_message=thinking_msg,
            existing_event_id=request.existing_event_id,
            user_id=requester_user_id,
            run_id=response_run_id,
        )
        if resolved_event_id is None:
            resolved_event_id = self.resolve_response_event_id(
                delivery_result=delivery_result,
                tracked_event_id=tracked_event_id,
                existing_event_id=request.existing_event_id,
                existing_event_is_placeholder=request.existing_event_is_placeholder,
            )
        await finalize_post_response_effects(tracked_event_id)
        return resolved_event_id

    async def run_cancellable_response(
        self,
        *,
        room_id: str,
        reply_to_event_id: str,
        thread_id: str | None,
        response_function: Callable[[str | None], Coroutine[Any, Any, None]],
        thinking_message: str | None = None,
        existing_event_id: str | None = None,
        user_id: str | None = None,
        run_id: str | None = None,
        target: MessageTarget | None = None,
    ) -> str | None:
        """Run one response generation function with cancellation support."""
        resolved_target = target or self.deps.build_message_target(
            room_id=room_id,
            thread_id=thread_id,
            reply_to_event_id=reply_to_event_id,
        )

        assert not (thinking_message and existing_event_id), (
            "thinking_message and existing_event_id are mutually exclusive"
        )

        try:
            self.deps.increment_in_flight_response_count()

            initial_message_id = None
            if thinking_message:
                assert not existing_event_id
                initial_message_id = await self.deps.send_response(
                    room_id,
                    reply_to_event_id,
                    thinking_message,
                    thread_id,
                    target=resolved_target,
                    extra_content={STREAM_STATUS_KEY: STREAM_STATUS_PENDING},
                )

            message_id = existing_event_id or initial_message_id
            task: asyncio.Task[None] = asyncio.create_task(response_function(message_id))

            message_to_track = existing_event_id or initial_message_id
            tracked_message_id = message_to_track or f"__pending_response__:{id(task)}"
            show_stop_button = False

            self.deps.stop_manager.set_current(
                tracked_message_id,
                room_id,
                task,
                None,
                run_id=run_id,
            )

            if message_to_track:
                show_stop_button = self.deps.config.defaults.show_stop_button
                if show_stop_button and user_id:
                    user_is_online = await self.deps.is_user_online(
                        self.deps.client,
                        user_id,
                        room_id=room_id,
                    )
                    show_stop_button = user_is_online
                    self.deps.logger.info(
                        "Stop button decision",
                        message_id=message_to_track,
                        user_online=user_is_online,
                        show_button=show_stop_button,
                    )

                if show_stop_button:
                    self.deps.logger.info("Adding stop button", message_id=message_to_track)
                    await self.deps.stop_manager.add_stop_button(self.deps.client, room_id, message_to_track)

            try:
                await task
            except asyncio.CancelledError:
                self.deps.logger.warning(
                    "Response cancelled — traceback for diagnosis",
                    message_id=message_to_track or tracked_message_id,
                    exc_info=True,
                )
            except Exception as error:
                self.deps.logger.exception("Error during response generation", error=str(error))
                raise
            finally:
                self.deps.clear_tracked_response_message(
                    self.deps.stop_manager,
                    self.deps.client,
                    tracked_message_id,
                    show_stop_button=show_stop_button,
                )

            return message_id
        finally:
            self.deps.decrement_in_flight_response_count()

    async def _stream_response_with_first_token_log(
        self,
        response_stream: object,
        *,
        room_id: str,
        received_monotonic: float | None = None,
    ) -> AsyncIterator[object]:
        """Proxy one streaming response and log time-to-first visible token."""
        first_visible_token_logged = False
        async for chunk in cast("AsyncIterator[object]", response_stream):
            if (
                received_monotonic is not None
                and os.environ.get("MINDROOM_TIMING") == "1"
                and not first_visible_token_logged
                and isinstance(chunk, RunContentEvent)
                and chunk.content
            ):
                first_visible_token_logged = True
                elapsed_seconds = time.monotonic() - received_monotonic
                scope = timing_scope_context.get()
                prefix = f"[{scope}] " if scope else ""
                self.deps.logger.info(
                    f"TIMING {prefix}message_receipt_to_first_stream_token: {elapsed_seconds:.3f}s",
                    timing_scope=scope,
                    timing_step="message_receipt_to_first_stream_token",
                    elapsed_s=round(elapsed_seconds, 3),
                    room_id=room_id,
                )
            yield chunk

    async def _prepare_response_runtime_common(
        self,
        request: ResponseRequest,
        *,
        existing_event_uses_thread_id: bool,
        room_mode: bool,
    ) -> _PreparedResponseRuntime:
        resolved_target = request.target or (
            request.response_envelope.target
            if request.response_envelope is not None
            else self.deps.build_message_target(
                room_id=request.room_id,
                thread_id=request.thread_id,
                reply_to_event_id=request.reply_to_event_id,
            )
        )
        response_thread_id = (
            resolved_target.resolved_thread_id
            if request.target is not None
            else request.thread_id
            if request.existing_event_id is not None and existing_event_uses_thread_id
            else self.deps.resolve_response_thread_root(
                request.thread_id,
                request.reply_to_event_id,
                room_id=request.room_id,
                response_envelope=request.response_envelope,
            )
        )
        resolved_target = resolved_target.with_thread_root(response_thread_id)
        media_inputs = request.media or MediaInputs()
        session_id = resolved_target.session_id
        resolved_model_prompt = self.deps.append_matrix_prompt_context(
            request.model_prompt or request.prompt,
            target=resolved_target,
            include_context=self.deps.agent_has_matrix_messaging_tool(self.deps.agent_name),
        )
        tool_context = self.deps.build_tool_runtime_context(
            resolved_target,
            user_id=request.user_id,
            session_id=session_id,
            attachment_ids=request.attachment_ids,
            correlation_id=request.correlation_id,
            source_envelope=request.response_envelope,
        )
        execution_identity = self.deps.build_tool_execution_identity(
            target=resolved_target,
            user_id=request.user_id,
            session_id=session_id,
        )
        request_knowledge_managers = await self.deps.ensure_request_knowledge_managers(
            [self.deps.agent_name],
            execution_identity,
        )
        return _PreparedResponseRuntime(
            resolved_target=resolved_target,
            response_thread_id=response_thread_id,
            media_inputs=media_inputs,
            session_id=session_id,
            model_prompt=resolved_model_prompt,
            tool_context=tool_context,
            execution_identity=execution_identity,
            request_knowledge_managers=request_knowledge_managers,
            room_mode=room_mode,
        )

    @timed("prepare_non_streaming_runtime")
    async def prepare_non_streaming_runtime(
        self,
        request: ResponseRequest,
    ) -> _PreparedResponseRuntime:
        """Resolve non-streaming runtime context."""
        return await self._prepare_response_runtime_common(
            request,
            existing_event_uses_thread_id=not request.existing_event_is_placeholder,
            room_mode=False,
        )

    @timed("prepare_streaming_runtime")
    async def prepare_streaming_runtime(
        self,
        request: ResponseRequest,
    ) -> _PreparedResponseRuntime:
        """Resolve streaming runtime context."""
        room_mode = (
            self.deps.config.get_entity_thread_mode(
                self.deps.agent_name,
                self.deps.runtime_paths,
                room_id=request.room_id,
            )
            == "room"
        )
        return await self._prepare_response_runtime_common(
            request,
            existing_event_uses_thread_id=not request.existing_event_is_placeholder,
            room_mode=room_mode,
        )

    @timed("non_streaming_response_generation")
    async def generate_non_streaming_ai_response(
        self,
        request: ResponseRequest,
        *,
        run_id: str | None,
        runtime: _PreparedResponseRuntime,
        active_event_ids: set[str],
        tool_trace: list[Any],
        run_metadata_content: dict[str, Any],
        compaction_outcomes: list[CompactionOutcome],
    ) -> str:
        """Run one non-streaming AI request."""

        def note_attempt_run_id(current_run_id: str) -> None:
            self.deps.stop_manager.update_run_id(request.existing_event_id, current_run_id)

        async def build_response_text() -> str:
            knowledge = self.deps.knowledge_for_agent(
                self.deps.agent_name,
                request_knowledge_managers=runtime.request_knowledge_managers,
            )
            return await self.deps.ai_response(
                agent_name=self.deps.agent_name,
                prompt=runtime.model_prompt,
                session_id=runtime.session_id,
                runtime_paths=self.deps.runtime_paths,
                config=self.deps.config,
                thread_history=request.thread_history,
                room_id=request.room_id,
                knowledge=knowledge,
                user_id=request.user_id,
                run_id=run_id,
                run_id_callback=note_attempt_run_id,
                media=runtime.media_inputs,
                reply_to_event_id=request.reply_to_event_id,
                active_event_ids=active_event_ids,
                show_tool_calls=self.deps.show_tool_calls,
                tool_trace_collector=tool_trace,
                run_metadata_collector=run_metadata_content,
                execution_identity=runtime.execution_identity,
                compaction_outcomes_collector=compaction_outcomes,
                matrix_run_metadata=request.matrix_run_metadata,
                system_enrichment_items=request.system_enrichment_items,
            )

        async with self.deps.typing_indicator(self.deps.client, request.room_id):
            return await self.deps.run_in_tool_context(
                execution_identity=runtime.execution_identity,
                tool_context=runtime.tool_context,
                operation=build_response_text,
            )

    @timed("streaming_response_generation")
    async def generate_streaming_ai_response(
        self,
        request: ResponseRequest,
        *,
        run_id: str | None,
        runtime: _PreparedResponseRuntime,
        active_event_ids: set[str],
        tool_trace: list[Any],
        run_metadata_content: dict[str, Any],
        compaction_outcomes: list[CompactionOutcome],
        received_monotonic: float | None = None,
    ) -> tuple[str | None, str]:
        """Run one streaming AI request and send the streamed Matrix response."""

        def note_attempt_run_id(current_run_id: str) -> None:
            self.deps.stop_manager.update_run_id(request.existing_event_id, current_run_id)

        knowledge = self.deps.knowledge_for_agent(
            self.deps.agent_name,
            request_knowledge_managers=runtime.request_knowledge_managers,
        )
        response_stream = cast(
            "AsyncIterator[object]",
            self.deps.stream_agent_response(
                agent_name=self.deps.agent_name,
                prompt=runtime.model_prompt,
                session_id=runtime.session_id,
                runtime_paths=self.deps.runtime_paths,
                config=self.deps.config,
                thread_history=request.thread_history,
                room_id=request.room_id,
                knowledge=knowledge,
                user_id=request.user_id,
                run_id=run_id,
                run_id_callback=note_attempt_run_id,
                media=runtime.media_inputs,
                reply_to_event_id=request.reply_to_event_id,
                active_event_ids=active_event_ids,
                show_tool_calls=self.deps.show_tool_calls,
                run_metadata_collector=run_metadata_content,
                execution_identity=runtime.execution_identity,
                compaction_outcomes_collector=compaction_outcomes,
                matrix_run_metadata=request.matrix_run_metadata,
                system_enrichment_items=request.system_enrichment_items,
            ),
        )

        async with self.deps.typing_indicator(self.deps.client, request.room_id):
            wrapped_response_stream = self.deps.stream_in_tool_context(
                execution_identity=runtime.execution_identity,
                tool_context=runtime.tool_context,
                stream_factory=lambda: response_stream,
            )
            timed_response_stream = self._stream_response_with_first_token_log(
                wrapped_response_stream,
                room_id=request.room_id,
                received_monotonic=received_monotonic,
            )
            response_extra_content = self.deps.merge_response_extra_content(
                run_metadata_content,
                request.attachment_ids,
            )
            return await self.deps.delivery_gateway().deliver_stream(
                StreamingDeliveryRequest(
                    room_id=request.room_id,
                    reply_to_event_id=request.reply_to_event_id,
                    response_thread_id=runtime.response_thread_id,
                    response_stream=timed_response_stream,
                    existing_event_id=request.existing_event_id,
                    adopt_existing_placeholder=request.existing_event_id is not None
                    and request.existing_event_is_placeholder,
                    target=runtime.resolved_target,
                    room_mode=runtime.room_mode,
                    show_tool_calls=self.deps.show_tool_calls,
                    extra_content=response_extra_content,
                    tool_trace_collector=tool_trace,
                    streaming_cls=StreamingResponse,
                ),
            )

    async def process_and_respond(
        self,
        request: ResponseRequest,
        *,
        run_id: str | None = None,
        response_kind: str = "ai",
        compaction_outcomes_collector: list[CompactionOutcome] | None = None,
    ) -> DeliveryResult:
        """Process a message and send a response without streaming."""
        if not request.prompt.strip():
            return DeliveryResult(event_id=request.existing_event_id, response_text="", delivery_kind=None)

        runtime = await self.prepare_non_streaming_runtime(request)
        tool_trace: list[Any] = []
        compaction_outcomes: list[CompactionOutcome] = []
        run_metadata_content: dict[str, Any] = {}
        active_event_ids = self.deps.active_response_event_ids(request.room_id)

        try:
            response_text = await self.generate_non_streaming_ai_response(
                request,
                run_id=run_id,
                runtime=runtime,
                active_event_ids=active_event_ids,
                tool_trace=tool_trace,
                run_metadata_content=run_metadata_content,
                compaction_outcomes=compaction_outcomes,
            )
        except asyncio.CancelledError:
            self.deps.logger.warning(
                "Non-streaming response cancelled — traceback for diagnosis",
                message_id=request.existing_event_id,
                exc_info=True,
            )
            if request.existing_event_id:
                await self.deps.edit_message(
                    request.room_id,
                    request.existing_event_id,
                    self.deps.cancelled_response_text,
                    runtime.response_thread_id,
                )
            raise
        except Exception as error:
            self.deps.logger.exception("Error in non-streaming response", error=str(error))
            raise

        response_extra_content = self.deps.merge_response_extra_content(
            run_metadata_content,
            request.attachment_ids,
        )
        delivery = await self.deps.deliver_generated_response(
            room_id=request.room_id,
            reply_to_event_id=request.reply_to_event_id,
            thread_id=request.thread_id,
            target=runtime.resolved_target,
            existing_event_id=request.existing_event_id,
            existing_event_is_placeholder=request.existing_event_is_placeholder,
            response_text=response_text,
            response_kind=response_kind,
            response_envelope=self._response_envelope_for_request(
                request,
                resolved_target=runtime.resolved_target,
            ),
            correlation_id=self._correlation_id_for_request(request),
            tool_trace=tool_trace if self.deps.show_tool_calls else None,
            extra_content=response_extra_content or None,
        )
        if compaction_outcomes_collector is not None:
            compaction_outcomes_collector.extend(compaction_outcomes)
        return delivery

    async def process_and_respond_streaming(  # noqa: C901, PLR0911, PLR0912, PLR0915
        self,
        request: ResponseRequest,
        *,
        run_id: str | None = None,
        response_kind: str = "ai",
        received_monotonic: float | None = None,
        compaction_outcomes_collector: list[CompactionOutcome] | None = None,
    ) -> DeliveryResult:
        """Process a message and send a streamed response."""
        if not request.prompt.strip():
            return DeliveryResult(event_id=request.existing_event_id, response_text="", delivery_kind=None)

        runtime = await self.prepare_streaming_runtime(request)
        compaction_outcomes: list[CompactionOutcome] = []
        run_metadata_content: dict[str, Any] = {}
        active_event_ids = self.deps.active_response_event_ids(request.room_id)
        tool_trace: list[Any] = []

        try:
            event_id, accumulated = await self.generate_streaming_ai_response(
                request,
                run_id=run_id,
                runtime=runtime,
                active_event_ids=active_event_ids,
                tool_trace=tool_trace,
                run_metadata_content=run_metadata_content,
                compaction_outcomes=compaction_outcomes,
                received_monotonic=received_monotonic,
            )
        except StreamingDeliveryError as error:
            self.deps.logger.exception("Error in streaming response", error=str(error.error))
            tool_trace[:] = error.tool_trace
            if compaction_outcomes_collector is not None:
                compaction_outcomes_collector.extend(compaction_outcomes)
            delivery_kind: Literal["sent", "edited"] | None = None
            if error.event_id is not None:
                delivery_kind = "edited" if request.existing_event_id else "sent"
            return DeliveryResult(
                event_id=error.event_id,
                response_text=error.accumulated_text,
                delivery_kind=delivery_kind,
            )
        except asyncio.CancelledError:
            self.deps.logger.warning(
                "Bot streaming response cancelled — traceback for diagnosis",
                message_id=request.existing_event_id,
                exc_info=True,
            )
            raise
        except Exception as error:
            self.deps.logger.exception("Error in streaming response", error=str(error))
            return DeliveryResult(event_id=None, response_text="", delivery_kind=None)

        if event_id is None:
            if compaction_outcomes_collector is not None:
                compaction_outcomes_collector.extend(compaction_outcomes)
            return DeliveryResult(event_id=None, response_text=accumulated, delivery_kind=None)

        response_extra_content = self.deps.merge_response_extra_content(
            run_metadata_content,
            request.attachment_ids,
        )
        delivery_kind: Literal["sent", "edited"] = "edited" if request.existing_event_id else "sent"
        if request.response_envelope is None or request.correlation_id is None:
            interactive_response = interactive.parse_and_format_interactive(
                accumulated,
                extract_mapping=True,
            )
            if compaction_outcomes_collector is not None:
                compaction_outcomes_collector.extend(compaction_outcomes)
            return DeliveryResult(
                event_id=event_id,
                response_text=interactive_response.formatted_text,
                delivery_kind=delivery_kind,
                option_map=interactive_response.option_map,
                options_list=interactive_response.options_list,
            )

        visible_tool_trace = tool_trace if self.deps.show_tool_calls else None
        draft = await self.deps.apply_before_response_hooks(
            correlation_id=request.correlation_id,
            envelope=request.response_envelope,
            response_text=accumulated,
            response_kind=response_kind,
            tool_trace=visible_tool_trace,
            extra_content=response_extra_content,
        )
        if draft.suppress:
            if request.existing_event_is_placeholder or request.existing_event_id is None:
                delivery = await self.deps.delivery_gateway().cleanup_suppressed_streamed_response(
                    room_id=request.room_id,
                    event_id=event_id,
                    response_text=accumulated,
                    response_kind=response_kind,
                    response_envelope=request.response_envelope,
                    correlation_id=request.correlation_id,
                )
                if compaction_outcomes_collector is not None:
                    compaction_outcomes_collector.extend(compaction_outcomes)
                return delivery
            self.deps.logger.warning(
                "Streaming response was already delivered before a suppressing hook ran",
                source_event_id=request.response_envelope.source_event_id,
                correlation_id=request.correlation_id,
            )
            if compaction_outcomes_collector is not None:
                compaction_outcomes_collector.extend(compaction_outcomes)
            return DeliveryResult(
                event_id=event_id,
                response_text=accumulated,
                delivery_kind=delivery_kind,
                suppressed=True,
            )

        needs_final_edit = (
            draft.response_text != accumulated
            or draft.tool_trace != visible_tool_trace
            or draft.extra_content != response_extra_content
        )
        if needs_final_edit:
            delivery = await self.deps.deliver_generated_response(
                room_id=request.room_id,
                reply_to_event_id=request.reply_to_event_id,
                thread_id=request.thread_id,
                target=runtime.resolved_target,
                existing_event_id=event_id,
                existing_event_is_placeholder=request.existing_event_is_placeholder,
                response_text=draft.response_text,
                response_kind=response_kind,
                response_envelope=request.response_envelope,
                correlation_id=request.correlation_id,
                tool_trace=draft.tool_trace,
                extra_content=draft.extra_content,
                apply_before_hooks=False,
            )
        else:
            interactive_response = interactive.parse_and_format_interactive(
                accumulated,
                extract_mapping=True,
            )
            await self.deps.emit_after_response_hooks(
                correlation_id=request.correlation_id,
                envelope=request.response_envelope,
                response_text=interactive_response.formatted_text,
                response_event_id=event_id,
                delivery_kind=delivery_kind,
                response_kind=response_kind,
            )
            delivery = DeliveryResult(
                event_id=event_id,
                response_text=interactive_response.formatted_text,
                delivery_kind=delivery_kind,
                option_map=interactive_response.option_map,
                options_list=interactive_response.options_list,
            )

        if compaction_outcomes_collector is not None:
            compaction_outcomes_collector.extend(compaction_outcomes)
        return delivery

    async def send_skill_command_response(
        self,
        *,
        room_id: str,
        reply_to_event_id: str,
        thread_id: str | None,
        thread_history: Sequence[ResolvedVisibleMessage],
        prompt: str,
        agent_name: str,
        user_id: str | None,
        reply_to_event: _ReplyEventWithSource | None = None,
        source_envelope: MessageEnvelope | None = None,
    ) -> str | None:
        """Send a skill command response using a specific agent."""
        target = self.deps.build_message_target(
            room_id=room_id,
            thread_id=thread_id,
            reply_to_event_id=reply_to_event_id,
        )
        lifecycle_lock = self.deps.response_lifecycle_lock(target)
        async with lifecycle_lock:
            return await self.send_skill_command_response_locked(
                room_id=room_id,
                reply_to_event_id=reply_to_event_id,
                thread_id=thread_id,
                thread_history=thread_history,
                prompt=prompt,
                agent_name=agent_name,
                user_id=user_id,
                reply_to_event=reply_to_event,
                source_envelope=source_envelope,
            )

    async def send_skill_command_response_locked(
        self,
        *,
        room_id: str,
        reply_to_event_id: str,
        thread_id: str | None,
        thread_history: Sequence[ResolvedVisibleMessage],
        prompt: str,
        agent_name: str,
        user_id: str | None,
        reply_to_event: _ReplyEventWithSource | None = None,
        source_envelope: MessageEnvelope | None = None,
    ) -> str | None:
        """Send a skill command response after acquiring the per-thread lock."""
        if not prompt.strip():
            return None
        memory_prompt, memory_thread_history, model_prompt, thread_history = self.deps.prepare_memory_and_model_context(
            prompt,
            thread_history,
        )

        resolved_target = self.deps.build_message_target(
            room_id=room_id,
            thread_id=thread_id,
            reply_to_event_id=reply_to_event_id,
            event_source=reply_to_event.source if reply_to_event is not None else None,
        )
        session_id = resolved_target.session_id
        model_prompt = self.deps.append_matrix_prompt_context(
            model_prompt,
            target=resolved_target,
            include_context=self.deps.agent_has_matrix_messaging_tool(agent_name),
        )
        tool_context = self.deps.build_tool_runtime_context(
            resolved_target,
            user_id=user_id,
            session_id=session_id,
            agent_name=agent_name,
            source_envelope=source_envelope,
        )
        execution_identity = self.deps.build_tool_execution_identity(
            target=resolved_target,
            user_id=user_id,
            session_id=session_id,
            agent_name=agent_name,
        )
        request_knowledge_managers = await self.deps.ensure_request_knowledge_managers([agent_name], execution_identity)
        self.deps.reprioritize_auto_flush_sessions(
            self.deps.storage_path,
            self.deps.config,
            self.deps.runtime_paths,
            agent_name=agent_name,
            active_session_id=session_id,
            execution_identity=execution_identity,
        )
        show_tool_calls = self.deps.show_tool_calls_for_agent(agent_name)
        tool_trace: list[Any] = []
        run_metadata_content: dict[str, Any] = {}
        active_event_ids = self.deps.active_response_event_ids(room_id)
        async with self.deps.typing_indicator(self.deps.client, room_id):

            async def build_response_text() -> str:
                knowledge = self.deps.knowledge_for_agent(
                    agent_name,
                    request_knowledge_managers=request_knowledge_managers,
                )
                return await self.deps.ai_response(
                    agent_name=agent_name,
                    prompt=model_prompt,
                    session_id=session_id,
                    runtime_paths=self.deps.runtime_paths,
                    config=self.deps.config,
                    thread_history=thread_history,
                    room_id=room_id,
                    knowledge=knowledge,
                    reply_to_event_id=reply_to_event_id,
                    active_event_ids=active_event_ids,
                    show_tool_calls=show_tool_calls,
                    tool_trace_collector=tool_trace,
                    run_metadata_collector=run_metadata_content,
                    execution_identity=execution_identity,
                )

            response_text = await self.deps.run_in_tool_context(
                execution_identity=execution_identity,
                tool_context=tool_context,
                operation=build_response_text,
            )

        response = interactive.parse_and_format_interactive(response_text, extract_mapping=True)
        event_id = await self.deps.send_response(
            room_id,
            reply_to_event_id,
            response.formatted_text,
            thread_id,
            target=resolved_target,
            reply_to_event=reply_to_event,
            skip_mentions=True,
            tool_trace=tool_trace if show_tool_calls else None,
            extra_content=run_metadata_content or None,
        )

        def queue_memory_persistence() -> None:
            try:
                self.deps.mark_auto_flush_dirty_session(
                    self.deps.storage_path,
                    self.deps.config,
                    self.deps.runtime_paths,
                    agent_name=agent_name,
                    session_id=session_id,
                    execution_identity=execution_identity,
                )
                if self.deps.config.get_agent_memory_backend(agent_name) == "mem0":
                    self.deps.create_background_task(
                        self.deps.store_conversation_memory(
                            memory_prompt,
                            agent_name,
                            self.deps.storage_path,
                            session_id,
                            self.deps.config,
                            self.deps.runtime_paths,
                            memory_thread_history,
                            user_id,
                            execution_identity=execution_identity,
                        ),
                        name=f"memory_save_{agent_name}_{session_id}",
                    )
            except Exception:  # pragma: no cover
                self.deps.logger.debug("Skipping memory storage due to configuration error")

        await apply_post_response_effects(
            ResponseOutcome(
                resolved_event_id=event_id,
                delivery_result=DeliveryResult(
                    event_id=event_id,
                    response_text=response.formatted_text,
                    delivery_kind="sent" if event_id is not None else None,
                    option_map=response.option_map,
                    options_list=response.options_list,
                ),
                session_id=session_id,
                session_type=SessionType.AGENT,
                execution_identity=execution_identity,
                interactive_target=resolved_target,
                memory_prompt=memory_prompt,
                memory_thread_history=memory_thread_history,
            ),
            self.deps.post_response_effects_deps(
                room_id=room_id,
                reply_to_event_id=reply_to_event_id,
                thread_id=thread_id,
                interactive_agent_name=agent_name,
                queue_memory_persistence=queue_memory_persistence,
            ),
        )

        return event_id

    def resolve_response_event_id(
        self,
        *,
        delivery_result: DeliveryResult | None,
        tracked_event_id: str | None,
        existing_event_id: str | None,
        existing_event_is_placeholder: bool = False,
    ) -> str | None:
        """Resolve the final response event id across send, edit, and placeholder reuse."""
        if delivery_result is not None and delivery_result.event_id is not None:
            return delivery_result.event_id
        if delivery_result is not None and existing_event_is_placeholder:
            return None
        if delivery_result is not None and delivery_result.suppressed:
            return None
        if delivery_result is not None and existing_event_id is not None:
            return existing_event_id
        return tracked_event_id or existing_event_id

    async def generate_response(self, request: ResponseRequest) -> str | None:
        """Generate and send/edit an agent response with lifecycle locking."""
        return await self._run_locked_response_lifecycle(
            request,
            locked_operation=lambda resolved_target: self.generate_response_locked(
                request,
                resolved_target=resolved_target,
            ),
        )

    async def generate_response_locked(
        self,
        request: ResponseRequest,
        *,
        resolved_target: MessageTarget,
    ) -> str | None:
        """Generate one agent response after acquiring the per-thread lock."""
        delivery_thread_id = resolved_target.resolved_thread_id
        resolved_target = resolved_target.with_thread_root(delivery_thread_id)
        if request.on_lifecycle_lock_acquired is not None:
            request.on_lifecycle_lock_acquired()
        memory_prompt, memory_thread_history, model_prompt_text, model_thread_history = (
            self.deps.prepare_memory_and_model_context(
                request.prompt,
                request.thread_history,
                model_prompt=request.model_prompt,
            )
        )
        normalized_request = replace(
            request,
            prompt=memory_prompt,
            model_prompt=model_prompt_text,
            thread_history=model_thread_history,
            media=request.media or MediaInputs(),
            target=resolved_target,
        )

        session_id = resolved_target.session_id
        execution_identity = self.deps.build_tool_execution_identity(
            target=resolved_target,
            user_id=request.user_id,
            session_id=session_id,
        )
        self.deps.reprioritize_auto_flush_sessions(
            self.deps.storage_path,
            self.deps.config,
            self.deps.runtime_paths,
            agent_name=self.deps.agent_name,
            active_session_id=session_id,
            execution_identity=execution_identity,
        )

        use_streaming = await self.deps.should_use_streaming(
            self.deps.client,
            request.room_id,
            requester_user_id=request.user_id,
            enable_streaming=self.deps.enable_streaming,
        )
        delivery_result: DeliveryResult | None = None
        compaction_outcomes: list[CompactionOutcome] = []
        response_run_id = str(uuid4())
        resolved_event_id: str | None = None

        def queue_memory_persistence() -> None:
            self.deps.mark_auto_flush_dirty_session(
                self.deps.storage_path,
                self.deps.config,
                self.deps.runtime_paths,
                agent_name=self.deps.agent_name,
                session_id=session_id,
                execution_identity=execution_identity,
            )
            if self.deps.config.get_agent_memory_backend(self.deps.agent_name) == "mem0":
                self.deps.create_background_task(
                    self.deps.store_conversation_memory(
                        memory_prompt,
                        self.deps.agent_name,
                        self.deps.storage_path,
                        session_id,
                        self.deps.config,
                        self.deps.runtime_paths,
                        memory_thread_history,
                        request.user_id,
                        execution_identity=execution_identity,
                    ),
                    name=f"memory_save_{self.deps.agent_name}_{session_id}",
                )

        strip_transient_enrichment, persist_response_event_id = self._build_session_storage_effects(
            session_id=session_id,
            session_type=self.deps.history_session_type(),
            create_storage=lambda: self.deps.create_history_scope_storage(execution_identity),
        )

        def queue_thread_summary(summary_room_id: str, summary_thread_id: str, message_count_hint: int | None) -> None:
            self.deps.queue_timed_thread_summary(
                room_id=summary_room_id,
                thread_id=summary_thread_id,
                message_count_hint=message_count_hint,
            )

        async def finalize_post_response_effects(message_id: str | None) -> None:
            nonlocal resolved_event_id
            resolved_event_id = self.resolve_response_event_id(
                delivery_result=delivery_result,
                tracked_event_id=message_id,
                existing_event_id=request.existing_event_id,
                existing_event_is_placeholder=request.existing_event_is_placeholder,
            )
            await apply_post_response_effects(
                ResponseOutcome(
                    resolved_event_id=resolved_event_id,
                    delivery_result=delivery_result,
                    response_run_id=response_run_id,
                    session_id=session_id,
                    session_type=self.deps.history_session_type(),
                    execution_identity=execution_identity,
                    compaction_outcomes=tuple(compaction_outcomes),
                    interactive_target=resolved_target,
                    thread_summary_room_id=request.room_id if request.thread_id is not None else None,
                    thread_summary_thread_id=request.thread_id,
                    thread_summary_message_count_hint=self.deps.thread_summary_message_count_hint(
                        request.thread_history,
                    ),
                    memory_prompt=memory_prompt,
                    memory_thread_history=memory_thread_history,
                    strip_transient_enrichment_after_run=request.strip_transient_enrichment_after_run,
                ),
                self.deps.post_response_effects_deps(
                    room_id=request.room_id,
                    reply_to_event_id=request.reply_to_event_id,
                    thread_id=request.thread_id,
                    interactive_agent_name=self.deps.agent_name,
                    strip_transient_enrichment=strip_transient_enrichment,
                    queue_memory_persistence=queue_memory_persistence,
                    persist_response_event_id=persist_response_event_id,
                    queue_thread_summary=queue_thread_summary,
                ),
            )

        async def generate(message_id: str | None) -> None:
            nonlocal delivery_result
            delivery_request = self._request_for_delivery(normalized_request, message_id=message_id)
            if use_streaming:
                delivery_result = await self.process_and_respond_streaming(
                    delivery_request,
                    run_id=response_run_id,
                    received_monotonic=request.received_monotonic,
                    compaction_outcomes_collector=compaction_outcomes,
                )
            else:
                delivery_result = await self.process_and_respond(
                    delivery_request,
                    run_id=response_run_id,
                    compaction_outcomes_collector=compaction_outcomes,
                )

        thinking_msg = None
        if not request.existing_event_id:
            thinking_msg = "Thinking..."

        tracked_event_id = await self.run_cancellable_response(
            room_id=request.room_id,
            reply_to_event_id=request.reply_to_event_id,
            thread_id=request.thread_id,
            target=resolved_target,
            response_function=generate,
            thinking_message=thinking_msg,
            existing_event_id=request.existing_event_id,
            user_id=request.user_id,
            run_id=response_run_id,
        )
        if resolved_event_id is None:
            resolved_event_id = self.resolve_response_event_id(
                delivery_result=delivery_result,
                tracked_event_id=tracked_event_id,
                existing_event_id=request.existing_event_id,
                existing_event_is_placeholder=request.existing_event_is_placeholder,
            )
        await self._await_post_response_effects(
            finalize_effects=finalize_post_response_effects,
            tracked_event_id=tracked_event_id,
            swallow_late_cancellation=True,
        )
        return resolved_event_id

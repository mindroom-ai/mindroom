"""Control one inbound turn from ingress to recorded outcome."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol, cast

import nio

from mindroom import interactive
from mindroom.attachments import merge_attachment_ids, parse_attachment_ids_from_event_source
from mindroom.authorization import (
    filter_agents_by_sender_permissions,
    get_effective_sender_id_for_reply_permissions,
    is_authorized_sender,
)
from mindroom.coalescing import (
    CoalescedBatch,
    CoalescingGate,
    CoalescingKey,
    PendingEvent,
    PreparedTextEvent,
    build_batch_dispatch_event,
)
from mindroom.commands.handler import (
    CommandHandlerContext,
    handle_command,
)
from mindroom.commands.parsing import command_parser
from mindroom.constants import (
    ATTACHMENT_IDS_KEY,
    ORIGINAL_SENDER_KEY,
    ROUTER_AGENT_NAME,
    STREAM_STATUS_ERROR,
    STREAM_STATUS_KEY,
    STREAM_STATUS_PENDING,
    STREAM_STATUS_STREAMING,
    VOICE_RAW_AUDIO_FALLBACK_KEY,
    RuntimePaths,
)
from mindroom.delivery_gateway import FinalDeliveryRequest, SendTextRequest
from mindroom.error_handling import get_user_friendly_error_message
from mindroom.final_delivery import TurnDeliveryResolution
from mindroom.handled_turns import HandledTurnState, apply_delivery_resolution
from mindroom.hooks import MessageEnvelope, build_hook_matrix_admin
from mindroom.hooks.ingress import (
    hook_ingress_policy,
    is_automation_source_kind,
    is_voice_event,
    should_handle_interactive_text_response,
)
from mindroom.inbound_turn_normalizer import (
    BatchMediaAttachmentRequest,
    DispatchPayload,
    DispatchPayloadWithAttachmentsRequest,
    InboundTurnNormalizer,
    TextNormalizationRequest,
    VoiceNormalizationRequest,
)
from mindroom.logging_config import bound_log_context
from mindroom.matrix.cache import ThreadHistoryResult
from mindroom.matrix.event_info import EventInfo
from mindroom.matrix.identity import extract_agent_name, is_agent_id
from mindroom.matrix.message_content import is_v2_sidecar_text_preview
from mindroom.matrix.rooms import is_dm_room
from mindroom.response_runner import PostLockRequestPreparationError, ResponseRequest
from mindroom.routing import suggest_agent_for_message
from mindroom.thread_utils import (
    check_agent_mentioned,
    get_configured_agents_for_room,
    thread_requires_explicit_agent_targeting,
)
from mindroom.timing import (
    DispatchPipelineTiming,
    attach_dispatch_pipeline_timing,
    create_dispatch_pipeline_timing,
    emit_elapsed_timing,
    event_timing_scope,
    get_dispatch_pipeline_timing,
)
from mindroom.timing import timing_scope as timing_scope_context
from mindroom.turn_policy import IngressHookRunner, PreparedDispatch, ResponseAction, TurnPolicy

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Sequence

    import structlog
    from agno.media import Image

    from mindroom.bot_runtime_view import BotRuntimeView
    from mindroom.commands.parsing import Command
    from mindroom.conversation_resolver import ConversationResolver, MessageContext
    from mindroom.delivery_gateway import DeliveryGateway
    from mindroom.matrix.client_visible_messages import ResolvedVisibleMessage
    from mindroom.matrix.conversation_cache import MatrixConversationCache
    from mindroom.matrix.identity import MatrixID
    from mindroom.message_target import MessageTarget
    from mindroom.response_runner import ResponseRunner
    from mindroom.tool_system.runtime_context import ToolRuntimeSupport
    from mindroom.turn_store import TurnStore

type DispatchPayloadBuilder = Callable[[MessageContext], Awaitable[DispatchPayload]]

type _MediaDispatchEvent = (
    nio.RoomMessageImage
    | nio.RoomEncryptedImage
    | nio.RoomMessageFile
    | nio.RoomEncryptedFile
    | nio.RoomMessageVideo
    | nio.RoomEncryptedVideo
)
type _InboundMediaEvent = _MediaDispatchEvent | nio.RoomMessageAudio | nio.RoomEncryptedAudio
type _TextDispatchEvent = nio.RoomMessageText | PreparedTextEvent
type _DispatchEvent = _TextDispatchEvent | _MediaDispatchEvent
type MatrixEventId = str
type RequesterUserId = str


class _EditRegenerator(Protocol):
    """Minimal edit-regeneration surface needed by turn sequencing."""

    async def handle_message_edit(
        self,
        room: nio.MatrixRoom,
        event: nio.RoomMessageText,
        event_info: EventInfo,
        requester_user_id: str,
    ) -> None:
        """Regenerate the owned response for one edited user turn."""


@dataclass(frozen=True)
class _PrecheckedEvent[T]:
    """A raw or prepared event that already passed ingress prechecks."""

    event: T
    requester_user_id: str


type _PrecheckedTextDispatchEvent = _PrecheckedEvent[_TextDispatchEvent]
type _PrecheckedMediaDispatchEvent = _PrecheckedEvent[_MediaDispatchEvent]


@dataclass(frozen=True)
class TurnControllerDeps:
    """Collaborators needed for turn control, policy, and execution."""

    runtime: BotRuntimeView
    logger: structlog.stdlib.BoundLogger
    runtime_paths: RuntimePaths
    agent_name: str
    matrix_id: MatrixID
    conversation_cache: MatrixConversationCache
    resolver: ConversationResolver
    normalizer: InboundTurnNormalizer
    turn_policy: TurnPolicy
    ingress_hook_runner: IngressHookRunner
    response_runner: ResponseRunner
    delivery_gateway: DeliveryGateway
    tool_runtime: ToolRuntimeSupport
    turn_store: TurnStore
    coalescing_gate: CoalescingGate
    edit_regenerator: _EditRegenerator


@dataclass
class TurnController:
    """Own sequencing for one inbound text or media turn."""

    deps: TurnControllerDeps

    def _client(self) -> nio.AsyncClient:
        client = self.deps.runtime.client
        if client is None:
            msg = "Matrix client is not ready for turn execution"
            raise RuntimeError(msg)
        return client

    def _requester_user_id(
        self,
        *,
        sender: str,
        source: object,
    ) -> str:
        """Return the effective requester for reply-permission checks."""
        source_dict = cast("dict[str, Any] | None", source if isinstance(source, dict) else None)
        content = source_dict.get("content") if source_dict is not None else None
        if (
            sender == self.deps.matrix_id.full_id
            and isinstance(content, dict)
            and isinstance(content.get(ORIGINAL_SENDER_KEY), str)
        ):
            return content[ORIGINAL_SENDER_KEY]
        return get_effective_sender_id_for_reply_permissions(
            sender,
            source_dict,
            self.deps.runtime.config,
            self.deps.runtime_paths,
        )

    def _sender_is_trusted_for_ingress_metadata(self, sender_id: str) -> bool:
        """Return whether one sender may supply trusted ingress metadata overrides."""
        return extract_agent_name(sender_id, self.deps.runtime.config, self.deps.runtime_paths) is not None

    def _is_trusted_internal_relay_event(self, event: _DispatchEvent) -> bool:
        """Return whether one agent-authored relay should bypass user-turn coalescing."""
        if not isinstance(event, nio.RoomMessageText | PreparedTextEvent):
            return False
        if not self._sender_is_trusted_for_ingress_metadata(event.sender):
            return False
        content = event.source.get("content") if isinstance(event.source, dict) else None
        if not isinstance(content, dict):
            return False
        if content.get("com.mindroom.source_kind") == "scheduled":
            return False
        original_sender = content.get(ORIGINAL_SENDER_KEY)
        return isinstance(original_sender, str) and bool(original_sender)

    def _is_trusted_router_relay_event(self, event: _DispatchEvent) -> bool:
        """Return whether one trusted internal relay originated from the router."""
        if not self._is_trusted_internal_relay_event(event):
            return False
        sender_agent_name = extract_agent_name(
            event.sender,
            self.deps.runtime.config,
            self.deps.runtime_paths,
        )
        return sender_agent_name == ROUTER_AGENT_NAME

    def _precheck_event(
        self,
        room: nio.MatrixRoom,
        event: _DispatchEvent | _InboundMediaEvent,
        *,
        is_edit: bool = False,
    ) -> RequesterUserId | None:
        """Run shared early-exit checks for inbound text and media events."""
        content = event.source.get("content") if isinstance(event.source, dict) else None
        source_kind = content.get("com.mindroom.source_kind") if isinstance(content, dict) else None
        requester_user_id = self._requester_user_id(
            sender=event.sender,
            source=event.source,
        )

        if requester_user_id == self.deps.matrix_id.full_id and source_kind != "hook_dispatch":
            return None

        if not is_edit and self.deps.turn_store.is_handled(event.event_id):
            return None

        if not is_authorized_sender(
            requester_user_id,
            self.deps.runtime.config,
            room.room_id,
            self.deps.runtime_paths,
            room_alias=room.canonical_alias,
        ):
            self._mark_source_events_responded(HandledTurnState.from_source_event_id(event.event_id))
            return None

        if not self.deps.turn_policy.can_reply_to_sender(requester_user_id):
            self._mark_source_events_responded(HandledTurnState.from_source_event_id(event.event_id))
            return None

        return requester_user_id

    def _precheck_dispatch_event[T: _DispatchEvent](
        self,
        room: nio.MatrixRoom,
        event: T,
        *,
        is_edit: bool = False,
    ) -> _PrecheckedEvent[T] | None:
        """Return a typed prechecked event for turn dispatch."""
        requester_user_id = self._precheck_event(room, event, is_edit=is_edit)
        if requester_user_id is None:
            return None
        return _PrecheckedEvent(event=event, requester_user_id=requester_user_id)

    def _mark_source_events_responded(self, handled_turn: HandledTurnState) -> None:
        """Mark one or more source events as handled by the same terminal outcome."""
        self.deps.turn_store.record_turn(handled_turn)

    def _has_newer_unresponded_in_thread(
        self,
        event: _TextDispatchEvent,
        requester_user_id: str,
        thread_history: Sequence[ResolvedVisibleMessage],
    ) -> bool:
        """Return True when a newer unresponded message from the same requester exists."""
        if isinstance(event, PreparedTextEvent) and is_automation_source_kind(event.source_kind_override or ""):
            return False
        event_ts = event.server_timestamp
        if event_ts is None or not thread_history:
            return False
        for message in thread_history:
            if (
                self._requester_user_id(
                    sender=message.sender,
                    source={"content": message.content},
                )
                != requester_user_id
            ):
                continue
            if message.timestamp is None or message.timestamp <= event_ts:
                continue
            if message.event_id == event.event_id:
                continue
            if self.deps.turn_store.is_handled(message.event_id):
                continue
            if (
                message.body
                and isinstance(message.body, str)
                and not is_voice_event(message, sender_is_trusted=self._sender_is_trusted_for_ingress_metadata)
                and command_parser.parse(message.body.strip()) is not None
            ):
                continue
            self.deps.logger.info(
                "Skipping older message — newer unresponded message from same sender in thread",
                skipped_event_id=event.event_id,
                newer_event_id=message.event_id,
            )
            return True
        return False

    def _should_skip_deep_synthetic_full_dispatch(
        self,
        *,
        event_id: str,
        envelope: MessageEnvelope,
    ) -> bool:
        """Return True when a deep synthetic hook relay must stop before dispatch."""
        resolved_policy = hook_ingress_policy(envelope)
        if resolved_policy.allow_full_dispatch:
            return False
        self.deps.logger.debug(
            "Ignoring deep synthetic hook relay before command/response dispatch",
            event_id=event_id,
            source_kind=envelope.source_kind,
            hook_source=envelope.hook_source,
            message_received_depth=envelope.message_received_depth,
        )
        return True

    def _should_bypass_coalescing_for_active_thread_follow_up(
        self,
        *,
        target: MessageTarget,
        source_kind: str,
        sender_id: str,
    ) -> bool:
        """Return whether one human thread follow-up should skip in-flight coalescing."""
        if target.resolved_thread_id is None:
            return False
        if is_automation_source_kind(source_kind):
            return False
        if is_agent_id(sender_id, self.deps.runtime.config, self.deps.runtime_paths):
            return False
        return self.deps.response_runner.has_active_response_for_target(target)

    async def _should_skip_router_before_shared_ingress_work(
        self,
        room: nio.MatrixRoom,
        event: nio.RoomMessageText,
        *,
        requester_user_id: str,
        thread_id: str | None,
    ) -> bool:
        """Return whether the router can safely skip shared ingress work for one text event."""
        if self.deps.agent_name != ROUTER_AGENT_NAME:
            return False
        if command_parser.parse(event.body.strip()) is not None:
            return False

        mentioned_agents, _am_i_mentioned, has_non_agent_mentions = check_agent_mentioned(
            event.source,
            self.deps.matrix_id,
            self.deps.runtime.config,
            self.deps.runtime_paths,
        )
        if mentioned_agents or has_non_agent_mentions:
            return True
        if thread_id is None:
            return False

        thread_history = await self.deps.conversation_cache.get_dispatch_thread_snapshot(
            room.room_id,
            thread_id,
        )
        return thread_requires_explicit_agent_targeting(
            thread_history,
            sender_id=requester_user_id,
            config=self.deps.runtime.config,
            runtime_paths=self.deps.runtime_paths,
        )

    async def _coalescing_key_for_event(
        self,
        room: nio.MatrixRoom,
        event: _DispatchEvent,
        requester_user_id: str,
    ) -> CoalescingKey:
        """Return the sender or thread scoped dispatch key for one event."""
        return (
            room.room_id,
            await self.deps.resolver.coalescing_thread_id(room, event),
            requester_user_id,
        )

    async def _append_live_event_with_timing(
        self,
        room_id: str,
        event: nio.RoomMessage,
        *,
        event_info: EventInfo,
        dispatch_timing: DispatchPipelineTiming | None,
    ) -> None:
        """Persist one ingress cache mutation while recording its contribution to ingress latency."""
        if dispatch_timing is not None:
            dispatch_timing.mark("ingress_cache_append_start")
        await self.deps.conversation_cache.append_live_event(room_id, event, event_info=event_info)
        if dispatch_timing is not None:
            dispatch_timing.mark("ingress_cache_append_ready")

    async def _resolve_text_event_with_ingress_timing(
        self,
        event: nio.RoomMessageText,
        *,
        dispatch_timing: DispatchPipelineTiming | None,
    ) -> PreparedTextEvent:
        """Normalize one inbound text event while recording ingress timing boundaries."""
        if dispatch_timing is not None:
            dispatch_timing.mark("ingress_normalize_start")
        prepared_event = await self.deps.normalizer.resolve_text_event(
            TextNormalizationRequest(event=event),
        )
        if dispatch_timing is not None:
            dispatch_timing.mark("ingress_normalize_ready")
        attach_dispatch_pipeline_timing(prepared_event.source, dispatch_timing)
        return prepared_event

    async def _enqueue_for_dispatch(
        self,
        event: _DispatchEvent,
        room: nio.MatrixRoom,
        *,
        source_kind: str,
        requester_user_id: str | None = None,
        coalescing_key: CoalescingKey | None = None,
    ) -> None:
        """Route one inbound event through the live coalescing gate."""
        dispatch_timing = get_dispatch_pipeline_timing(event.source)
        if dispatch_timing is not None:
            dispatch_timing.mark("gate_enter")
        enqueue_start = time.monotonic()
        timing_scope = event_timing_scope(event.event_id)
        effective_requester_user_id = requester_user_id or self._requester_user_id(
            sender=event.sender,
            source=event.source,
        )
        if self._is_trusted_internal_relay_event(event):
            if dispatch_timing is not None:
                dispatch_timing.note(coalescing_bypassed=True, coalescing_bypass_reason="trusted_internal_relay")
                dispatch_timing.mark("gate_exit")
            trusted_relay_event = cast("_TextDispatchEvent", event)
            await self._dispatch_text_message(
                room,
                trusted_relay_event,
                effective_requester_user_id,
            )
            emit_elapsed_timing(
                "ingress_handoff.enqueue_for_dispatch",
                enqueue_start,
                path="trusted_internal_relay",
                timing_scope=timing_scope,
            )
            return
        coalescing_key_start = time.monotonic()
        resolved_key = coalescing_key or await self._coalescing_key_for_event(room, event, effective_requester_user_id)
        emit_elapsed_timing(
            "ingress_handoff.enqueue_for_dispatch.coalescing_key",
            coalescing_key_start,
            thread_id=resolved_key[1],
            timing_scope=timing_scope,
        )
        gate_enqueue_start = time.monotonic()
        await self.deps.coalescing_gate.enqueue(
            resolved_key,
            PendingEvent(
                event=event,
                room=room,
                source_kind=source_kind,
            ),
        )
        emit_elapsed_timing(
            "ingress_handoff.enqueue_for_dispatch.coalescing_gate",
            gate_enqueue_start,
            source_kind=source_kind,
            timing_scope=timing_scope,
        )
        emit_elapsed_timing(
            "ingress_handoff.enqueue_for_dispatch",
            enqueue_start,
            source_kind=source_kind,
            timing_scope=timing_scope,
        )

    async def _maybe_send_visible_voice_echo(
        self,
        room: nio.MatrixRoom,
        event: nio.RoomMessageAudio | nio.RoomEncryptedAudio,
        *,
        text: str,
        thread_id: str | None,
    ) -> MatrixEventId | None:
        """Optionally post a display-only router echo for normalized audio."""
        if self.deps.agent_name != ROUTER_AGENT_NAME or not self.deps.runtime.config.voice.visible_router_echo:
            return None

        existing_visible_echo_event_id = self.deps.turn_store.visible_echo_for_source(event.event_id)
        if existing_visible_echo_event_id:
            return existing_visible_echo_event_id

        target = self.deps.resolver.build_message_target(
            room_id=room.room_id,
            thread_id=thread_id,
            reply_to_event_id=event.event_id,
            event_source=event.source,
        )
        visible_echo_event_id = await self.deps.delivery_gateway.send_text(
            SendTextRequest(
                target=target,
                response_text=text,
                skip_mentions=True,
            ),
        )
        if visible_echo_event_id:
            self.deps.turn_store.record_visible_echo(event.event_id, visible_echo_event_id)
        return visible_echo_event_id

    async def _prepare_dispatch(
        self,
        room: nio.MatrixRoom,
        event: _TextDispatchEvent,
        requester_user_id: str,
        *,
        event_label: str,
        handled_turn: HandledTurnState,
    ) -> PreparedDispatch | None:
        """Build the shared dispatch context for one prepared inbound turn."""
        extract_context_start = time.monotonic()
        if self._is_trusted_router_relay_event(event):
            context = await self.deps.resolver.extract_trusted_router_relay_context(room, event)
            emit_elapsed_timing(
                "dispatch_handoff.prepare_dispatch.extract_context",
                extract_context_start,
                path="trusted_router_relay",
            )
        else:
            context = await self.deps.resolver.extract_dispatch_context(room, event)
            emit_elapsed_timing(
                "dispatch_handoff.prepare_dispatch.extract_context",
                extract_context_start,
                path="normal",
            )
        target_start = time.monotonic()
        target = self.deps.resolver.build_message_target(
            room_id=room.room_id,
            thread_id=context.thread_id,
            reply_to_event_id=event.event_id,
            event_source=event.source,
        )
        emit_elapsed_timing(
            "dispatch_handoff.prepare_dispatch.build_message_target",
            target_start,
            resolved_thread_id=target.resolved_thread_id,
        )
        correlation_id = event.event_id
        envelope_start = time.monotonic()
        envelope = self.deps.resolver.build_message_envelope(
            room_id=room.room_id,
            event=event,
            requester_user_id=requester_user_id,
            context=context,
            target=target,
        )
        emit_elapsed_timing(
            "dispatch_handoff.prepare_dispatch.build_message_envelope",
            envelope_start,
            source_kind=envelope.source_kind,
        )
        ingress_policy = hook_ingress_policy(envelope)
        hooks_start = time.monotonic()
        suppressed = await self.deps.ingress_hook_runner.emit_message_received_hooks(
            envelope=envelope,
            correlation_id=correlation_id,
            policy=ingress_policy,
        )
        emit_elapsed_timing(
            "dispatch_handoff.prepare_dispatch.emit_message_received_hooks",
            hooks_start,
            suppressed=suppressed,
        )
        if suppressed:
            self._mark_source_events_responded(handled_turn)
            return None

        sender_agent_name = extract_agent_name(requester_user_id, self.deps.runtime.config, self.deps.runtime_paths)
        if sender_agent_name and not context.am_i_mentioned and not ingress_policy.bypass_unmentioned_agent_gate:
            self.deps.logger.debug(
                "ignore_unmentioned_agent_event",
                agent=sender_agent_name,
                event_label=event_label,
                user_id=requester_user_id,
            )
            return None

        return PreparedDispatch(
            requester_user_id=requester_user_id,
            context=context,
            target=target,
            correlation_id=correlation_id,
            envelope=envelope,
        )

    async def _execute_command(
        self,
        room: nio.MatrixRoom,
        event: _TextDispatchEvent,
        requester_user_id: str,
        command: Command,
    ) -> None:
        """Run one explicit command executor path from the turn controller."""
        event = await self.deps.normalizer.resolve_text_event(
            TextNormalizationRequest(event=event),
        )

        async def send_response(
            room_id: str,
            reply_to_event_id: str | None,
            response_text: str,
            thread_id: str | None,
            reply_to_event: nio.RoomMessageText | None = None,
            skip_mentions: bool = False,
        ) -> TurnDeliveryResolution:
            target = self.deps.resolver.build_message_target(
                room_id=room_id,
                thread_id=thread_id,
                reply_to_event_id=reply_to_event_id,
                event_source=reply_to_event.source if reply_to_event is not None else None,
            )
            response_envelope = source_envelope or MessageEnvelope(
                source_event_id=event.event_id,
                room_id=room_id,
                target=target,
                requester_id=requester_user_id,
                sender_id=requester_user_id,
                body=event.body,
                attachment_ids=(),
                mentioned_agents=(),
                agent_name=self.deps.agent_name,
                source_kind="command",
            )
            final_outcome = await self.deps.delivery_gateway.deliver_final(
                FinalDeliveryRequest(
                    target=target,
                    existing_event_id=None,
                    response_text=response_text,
                    response_kind=("team" if self.deps.agent_name in self.deps.runtime.config.teams else "ai"),
                    response_envelope=response_envelope,
                    correlation_id=event.event_id,
                    tool_trace=None,
                    extra_content=None,
                    apply_before_hooks=False,
                    skip_mentions=skip_mentions,
                ),
            )
            return TurnDeliveryResolution.from_outcome(final_outcome)

        orchestrator = self.deps.runtime.orchestrator
        matrix_admin = None
        if orchestrator is not None:
            matrix_admin = orchestrator._hook_matrix_admin()
        elif self.deps.agent_name == ROUTER_AGENT_NAME:
            matrix_admin = build_hook_matrix_admin(self._client(), self.deps.runtime_paths)
        reload_plugins = (
            (lambda: orchestrator.reload_plugins_now(source="command")) if orchestrator is not None else None
        )

        context = CommandHandlerContext(
            client=self._client(),
            config=self.deps.runtime.config,
            runtime_paths=self.deps.runtime_paths,
            logger=self.deps.logger,
            derive_conversation_context=self.deps.resolver.derive_conversation_context,
            conversation_cache=self.deps.resolver.deps.conversation_cache,
            event_cache=self.deps.runtime.event_cache,
            matrix_admin=matrix_admin,
            build_message_target=self.deps.resolver.build_message_target,
            record_handled_turn=self.deps.turn_store.record_turn,
            send_response=send_response,
            reload_plugins=reload_plugins,
        )
        await handle_command(
            context=context,
            room=room,
            event=event,
            command=command,
            requester_user_id=requester_user_id,
        )

    async def handle_interactive_selection(
        self,
        room: nio.MatrixRoom,
        *,
        selection: interactive.InteractiveSelection,
        user_id: str,
        source_event_id: str | None = None,
    ) -> None:
        """Execute one validated interactive selection through the normal response path."""
        thread_history = (
            await self.deps.resolver.fetch_thread_history(self._client(), room.room_id, selection.thread_id)
            if selection.thread_id
            else []
        )
        ack_target = self.deps.resolver.build_message_target(
            room_id=room.room_id,
            thread_id=selection.thread_id,
            reply_to_event_id=None if selection.thread_id else selection.question_event_id,
        )
        response_target = self.deps.resolver.build_message_target(
            room_id=room.room_id,
            thread_id=selection.thread_id,
            reply_to_event_id=selection.question_event_id,
        )
        ack_event_id = await self.deps.delivery_gateway.send_text(
            SendTextRequest(
                target=ack_target,
                response_text=(
                    f"You selected: {selection.selection_key} {selection.selected_value}\n\nProcessing your response..."
                ),
            ),
        )
        if not ack_event_id:
            self.deps.logger.error(
                "Failed to send acknowledgment for interactive selection",
                source_event_id=selection.question_event_id,
            )
            return
        selection_matrix_run_metadata = self.deps.turn_store.build_run_metadata(
            HandledTurnState.from_source_event_id(selection.question_event_id),
            additional_source_event_ids=(
                (source_event_id,) if source_event_id and source_event_id != selection.question_event_id else ()
            ),
        )

        response_resolution = await self.deps.response_runner.generate_response(
            ResponseRequest(
                room_id=room.room_id,
                prompt=f"The user selected: {selection.selected_value}",
                reply_to_event_id=selection.question_event_id,
                thread_id=selection.thread_id,
                thread_history=thread_history,
                existing_event_id=ack_event_id,
                existing_event_is_placeholder=True,
                user_id=user_id,
                target=response_target,
                matrix_run_metadata=selection_matrix_run_metadata,
            ),
        )
        if response_resolution.should_mark_handled:
            self._mark_source_events_responded(
                apply_delivery_resolution(
                    HandledTurnState.from_source_event_id(selection.question_event_id),
                    response_resolution,
                ),
            )
            if source_event_id and source_event_id != selection.question_event_id:
                self._mark_source_events_responded(
                    apply_delivery_resolution(
                        HandledTurnState.from_source_event_id(source_event_id),
                        response_resolution,
                    ),
                )

    async def _execute_router_relay(
        self,
        room: nio.MatrixRoom,
        event: _DispatchEvent,
        thread_history: Sequence[ResolvedVisibleMessage],
        thread_id: str | None = None,
        message: str | None = None,
        *,
        requester_user_id: str,
        extra_content: dict[str, Any] | None = None,
        media_events: list[_MediaDispatchEvent] | None = None,
        handled_turn: HandledTurnState | None = None,
    ) -> None:
        """Run one explicit router relay from the turn controller."""
        assert self.deps.agent_name == ROUTER_AGENT_NAME

        permission_sender_id = requester_user_id
        available_agents = get_configured_agents_for_room(
            room.room_id,
            self.deps.runtime.config,
            self.deps.runtime_paths,
        )
        available_agents = filter_agents_by_sender_permissions(
            available_agents,
            permission_sender_id,
            self.deps.runtime.config,
            self.deps.runtime_paths,
        )
        if not available_agents:
            self.deps.logger.debug(
                "No configured agents to route to in this room for sender",
                sender=permission_sender_id,
            )
            return

        with bound_log_context(room_id=room.room_id, thread_id=thread_id):
            self.deps.logger.info("Handling AI routing", event_id=event.event_id)

        routing_text = message or event.body
        suggested_agent = await suggest_agent_for_message(
            routing_text,
            available_agents,
            self.deps.runtime.config,
            self.deps.runtime_paths,
            thread_history,
        )

        if not suggested_agent:
            response_text = (
                "⚠️ I couldn't determine which agent should help with this. "
                "Please try mentioning an agent directly with @ or rephrase your request."
            )
            with bound_log_context(room_id=room.room_id, thread_id=thread_id):
                self.deps.logger.warning("Router failed to determine agent")
        else:
            response_text = f"@{suggested_agent} could you help with this?"

        target_thread_mode = (
            self.deps.runtime.config.get_entity_thread_mode(
                suggested_agent,
                self.deps.runtime_paths,
                room_id=room.room_id,
            )
            if suggested_agent
            else None
        )
        resolved_target = self.deps.resolver.build_message_target(
            room_id=room.room_id,
            thread_id=thread_id,
            reply_to_event_id=event.event_id,
            event_source=event.source,
            thread_mode_override=target_thread_mode,
        )
        thread_event_id = resolved_target.resolved_thread_id
        routed_extra_content = dict(extra_content) if extra_content is not None else {}
        routed_media_events = list(media_events or [])
        if not routed_media_events and isinstance(
            event,
            nio.RoomMessageFile
            | nio.RoomEncryptedFile
            | nio.RoomMessageVideo
            | nio.RoomEncryptedVideo
            | nio.RoomMessageImage
            | nio.RoomEncryptedImage,
        ):
            routed_media_events.append(event)
        if routed_media_events:
            routed_attachment_ids = merge_attachment_ids(
                parse_attachment_ids_from_event_source({"content": routed_extra_content}),
                [
                    attachment_id
                    for attachment_id in await asyncio.gather(
                        *(
                            self.deps.normalizer.register_routed_attachment(
                                room_id=room.room_id,
                                thread_id=thread_event_id,
                                event=media_event,
                            )
                            for media_event in routed_media_events
                        ),
                    )
                    if attachment_id is not None
                ],
            )
            if routed_attachment_ids:
                routed_extra_content[ATTACHMENT_IDS_KEY] = routed_attachment_ids
            else:
                routed_extra_content.pop(ATTACHMENT_IDS_KEY, None)

        event_id = await self.deps.delivery_gateway.send_text(
            SendTextRequest(
                target=resolved_target,
                response_text=response_text,
                extra_content=routed_extra_content or None,
            ),
        )
        tracked_handled_turn = handled_turn or HandledTurnState.from_source_event_id(event.event_id)
        tracked_handled_turn = self.deps.turn_store.attach_response_context(
            tracked_handled_turn,
            history_scope=None,
            conversation_target=resolved_target,
        )
        with bound_log_context(**resolved_target.log_context):
            if event_id:
                self.deps.logger.info("Routed to agent", suggested_agent=suggested_agent)
                self._mark_source_events_responded(tracked_handled_turn.with_response_event_id(event_id))
            else:
                self.deps.logger.error("Failed to route to agent", agent=suggested_agent)

    def _router_handled_turn_outcome(
        self,
        handled_turn: HandledTurnState,
    ) -> HandledTurnState | None:
        """Return the terminal handled-turn outcome for one ignored router turn."""
        visible_router_echo_event_id = (
            handled_turn.visible_echo_event_id
            or self.deps.turn_store.visible_echo_for_sources(
                handled_turn.source_event_ids,
            )
        )
        if visible_router_echo_event_id is None:
            return None
        if all(self.deps.turn_store.is_handled(source_event_id) for source_event_id in handled_turn.source_event_ids):
            return None
        return handled_turn.with_response_event_id(visible_router_echo_event_id)

    async def _finalize_dispatch_failure(
        self,
        *,
        room_id: str,
        reply_to_event_id: str,
        thread_id: str | None,
        response_envelope: MessageEnvelope,
        correlation_id: str,
        error: Exception,
    ) -> TurnDeliveryResolution:
        """Convert dispatch setup failures into one typed terminal delivery resolution."""
        error_text = get_user_friendly_error_message(error, self.deps.agent_name)
        terminal_extra_content = {STREAM_STATUS_KEY: STREAM_STATUS_ERROR}
        target = self.deps.resolver.build_message_target(
            room_id=room_id,
            thread_id=thread_id,
            reply_to_event_id=reply_to_event_id,
        )
        response_kind = "team" if self.deps.agent_name in self.deps.runtime.config.teams else "ai"
        final_outcome = await self.deps.delivery_gateway.deliver_final(
            FinalDeliveryRequest(
                target=target,
                existing_event_id=None,
                response_text=error_text,
                response_kind=response_kind,
                response_envelope=response_envelope,
                correlation_id=correlation_id,
                tool_trace=None,
                extra_content=terminal_extra_content,
                apply_before_hooks=False,
            ),
        )
        return TurnDeliveryResolution.from_outcome(final_outcome)

    def _log_dispatch_latency(
        self,
        *,
        event_id: str,
        action_kind: str,
        dispatch_started_at: float,
        context_ready_monotonic: float,
        payload_ready_monotonic: float,
        thread_history: Sequence[ResolvedVisibleMessage],
    ) -> None:
        """Emit startup latency metrics for dispatch decisions that will respond."""
        latency_event_data: dict[str, str | float | int | bool] = {
            "event_id": event_id,
            "action_kind": action_kind,
            "context_hydration_ms": round((context_ready_monotonic - dispatch_started_at) * 1000, 1),
            "payload_hydration_ms": round((payload_ready_monotonic - context_ready_monotonic) * 1000, 1),
            "startup_total_ms": round((payload_ready_monotonic - dispatch_started_at) * 1000, 1),
        }
        if isinstance(thread_history, ThreadHistoryResult):
            latency_event_data.update(thread_history.diagnostics)
        self.deps.logger.info(
            "Response startup latency",
            **latency_event_data,
        )

    async def _execute_response_action(  # noqa: C901, PLR0912, PLR0915
        self,
        room: nio.MatrixRoom,
        event: _DispatchEvent,
        dispatch: PreparedDispatch,
        action: ResponseAction,
        payload_builder: DispatchPayloadBuilder,
        *,
        processing_log: str,
        dispatch_started_at: float,
        handled_turn: HandledTurnState,
        matrix_run_metadata: dict[str, Any] | None = None,
    ) -> None:
        """Execute one final response path for a prepared dispatch action."""
        action = self.deps.turn_policy.effective_response_action(action)
        dispatch_timing = get_dispatch_pipeline_timing(event.source)
        if dispatch_timing is not None:
            dispatch_timing.note(response_action_kind=action.kind)

        if action.kind == "reject":
            assert action.rejection_message is not None
            final_outcome = await self.deps.delivery_gateway.deliver_final(
                FinalDeliveryRequest(
                    target=dispatch.target,
                    existing_event_id=None,
                    existing_event_is_placeholder=False,
                    response_text=action.rejection_message,
                    response_kind=("team" if self.deps.agent_name in self.deps.runtime.config.teams else "ai"),
                    response_envelope=dispatch.envelope,
                    correlation_id=dispatch.correlation_id,
                    tool_trace=None,
                    extra_content=None,
                ),
            )
            response_resolution = TurnDeliveryResolution.from_outcome(final_outcome)
            if response_resolution.should_mark_handled:
                self._mark_source_events_responded(
                    apply_delivery_resolution(handled_turn, response_resolution),
                )
            if dispatch_timing is not None and response_resolution.turn_completion_event_id:
                dispatch_timing.mark_first_visible_reply("final")
                dispatch_timing.mark("response_complete")
                dispatch_timing.emit_summary(self.deps.logger, outcome="reject")
            return

        if not dispatch.context.am_i_mentioned:
            with bound_log_context(**dispatch.target.log_context):
                self.deps.logger.info("Will respond: only agent in thread")

        target_member_names: tuple[str, ...] | None = None
        if action.kind == "team":
            assert action.form_team is not None
            assert action.form_team.mode is not None
            target_member_names = tuple(
                member.agent_name(self.deps.runtime.config, self.deps.runtime_paths) or member.username
                for member in action.form_team.eligible_members
            )

        try:
            context_ready_monotonic = time.monotonic()
            payload_ready_monotonic = context_ready_monotonic
        except Exception as error:
            response_resolution = await self._finalize_dispatch_failure(
                room_id=room.room_id,
                reply_to_event_id=event.event_id,
                thread_id=dispatch.context.thread_id,
                response_envelope=dispatch.envelope,
                correlation_id=dispatch.correlation_id,
                error=error,
            )
            if response_resolution.should_mark_handled:
                self._mark_source_events_responded(apply_delivery_resolution(handled_turn, response_resolution))
            if dispatch_timing is not None and response_resolution.turn_completion_event_id:
                dispatch_timing.mark_first_visible_reply("final")
                dispatch_timing.mark("response_complete")
                dispatch_timing.emit_summary(self.deps.logger, outcome="dispatch_failure")
            return

        with bound_log_context(**dispatch.target.log_context):
            if dispatch_timing is not None and isinstance(dispatch.context.thread_history, ThreadHistoryResult):
                dispatch_timing.note(**dispatch.context.thread_history.diagnostics)

        with bound_log_context(**dispatch.target.log_context):
            self.deps.logger.info(processing_log, event_id=event.event_id)
        try:

            async def prepare_request_after_lock(request: ResponseRequest) -> ResponseRequest:
                nonlocal payload_ready_monotonic
                if dispatch_timing is not None:
                    dispatch_timing.mark("response_payload_start")
                dispatch.context.thread_history = request.thread_history
                dispatch.context.thread_id = request.thread_id
                dispatch.context.requires_full_thread_history = False
                payload = await payload_builder(dispatch.context)
                prepared_payload = await self.deps.ingress_hook_runner.apply_message_enrichment(
                    dispatch,
                    payload,
                    target_entity_name=self.deps.agent_name,
                    target_member_names=target_member_names,
                )
                system_enrichment_items = await self.deps.ingress_hook_runner.apply_system_enrichment(
                    dispatch,
                    prepared_payload.envelope,
                    target_entity_name=self.deps.agent_name,
                    target_member_names=target_member_names,
                )
                if system_enrichment_items:
                    prepared_payload = type(prepared_payload)(
                        payload=prepared_payload.payload,
                        envelope=prepared_payload.envelope,
                        system_enrichment_items=tuple(system_enrichment_items),
                    )
                payload_ready_monotonic = time.monotonic()
                if dispatch_timing is not None:
                    dispatch_timing.mark("response_payload_ready")
                with bound_log_context(**dispatch.target.log_context):
                    self._log_dispatch_latency(
                        event_id=event.event_id,
                        action_kind=action.kind,
                        dispatch_started_at=dispatch_started_at,
                        context_ready_monotonic=context_ready_monotonic,
                        payload_ready_monotonic=payload_ready_monotonic,
                        thread_history=request.thread_history,
                    )
                return ResponseRequest(
                    room_id=request.room_id,
                    reply_to_event_id=request.reply_to_event_id,
                    thread_id=request.thread_id,
                    thread_history=request.thread_history,
                    prompt=prepared_payload.payload.prompt,
                    model_prompt=prepared_payload.payload.model_prompt,
                    existing_event_id=request.existing_event_id,
                    existing_event_is_placeholder=request.existing_event_is_placeholder,
                    user_id=request.user_id,
                    media=prepared_payload.payload.media,
                    attachment_ids=tuple(prepared_payload.payload.attachment_ids or ()),
                    response_envelope=prepared_payload.envelope,
                    correlation_id=request.correlation_id,
                    target=request.target,
                    matrix_run_metadata=request.matrix_run_metadata,
                    system_enrichment_items=prepared_payload.system_enrichment_items,
                    requires_full_thread_history=False,
                    on_lifecycle_lock_acquired=request.on_lifecycle_lock_acquired,
                    pipeline_timing=request.pipeline_timing,
                )

            if action.kind == "team":
                assert action.form_team is not None
                assert action.form_team.mode is not None
                response_resolution = await self.deps.response_runner.generate_team_response_helper(
                    ResponseRequest(
                        room_id=room.room_id,
                        reply_to_event_id=event.event_id,
                        thread_id=dispatch.context.thread_id,
                        thread_history=dispatch.context.thread_history,
                        prompt=event.body,
                        user_id=dispatch.requester_user_id,
                        response_envelope=dispatch.envelope,
                        correlation_id=dispatch.correlation_id,
                        target=dispatch.target,
                        matrix_run_metadata=matrix_run_metadata,
                        requires_full_thread_history=dispatch.context.requires_full_thread_history,
                        prepare_after_lock=prepare_request_after_lock,
                        pipeline_timing=dispatch_timing,
                    ),
                    team_agents=action.form_team.eligible_members,
                    team_mode=action.form_team.mode.value,
                )
            else:
                response_resolution = await self.deps.response_runner.generate_response(
                    ResponseRequest(
                        room_id=room.room_id,
                        reply_to_event_id=event.event_id,
                        thread_id=dispatch.context.thread_id,
                        thread_history=dispatch.context.thread_history,
                        prompt=event.body,
                        user_id=dispatch.requester_user_id,
                        response_envelope=dispatch.envelope,
                        correlation_id=dispatch.correlation_id,
                        target=dispatch.target,
                        matrix_run_metadata=matrix_run_metadata,
                        requires_full_thread_history=dispatch.context.requires_full_thread_history,
                        prepare_after_lock=prepare_request_after_lock,
                        pipeline_timing=dispatch_timing,
                    ),
                )
        except PostLockRequestPreparationError as error:
            failure = error.__cause__ if isinstance(error.__cause__, Exception) else error
            response_resolution = await self._finalize_dispatch_failure(
                room_id=room.room_id,
                reply_to_event_id=event.event_id,
                thread_id=dispatch.context.thread_id,
                response_envelope=dispatch.envelope,
                correlation_id=dispatch.correlation_id,
                error=failure,
            )
            if response_resolution.should_mark_handled:
                self._mark_source_events_responded(apply_delivery_resolution(handled_turn, response_resolution))
            return
        if response_resolution.should_mark_handled:
            self._mark_source_events_responded(apply_delivery_resolution(handled_turn, response_resolution))

    async def handle_coalesced_batch(self, batch: CoalescedBatch) -> None:
        """Dispatch one flushed batch through the normal text pipeline."""
        dispatch_event = build_batch_dispatch_event(batch)
        timing_scope = event_timing_scope(dispatch_event.event_id)
        dispatch_timing = get_dispatch_pipeline_timing(dispatch_event.source)
        if dispatch_timing is not None:
            dispatch_timing.mark("gate_exit")
        retarget_start = time.monotonic()
        batch_coalescing_key = await self._coalescing_key_for_event(
            batch.room,
            batch.primary_event,
            batch.requester_user_id,
        )
        canonical_key = (
            batch.room.room_id,
            self.deps.resolver.build_message_target(
                room_id=batch.room.room_id,
                thread_id=batch_coalescing_key[1],
                reply_to_event_id=dispatch_event.event_id,
                event_source=dispatch_event.source,
            ).resolved_thread_id,
            batch.requester_user_id,
        )
        self.deps.coalescing_gate.retarget(batch_coalescing_key, canonical_key)
        emit_elapsed_timing(
            "coalescing.handle_batch.retarget",
            retarget_start,
            original_thread_id=batch_coalescing_key[1],
            resolved_thread_id=canonical_key[1],
            timing_scope=timing_scope,
        )
        async with self.deps.resolver.turn_thread_cache_scope():
            dispatch_start = time.monotonic()
            await self._dispatch_text_message(
                batch.room,
                dispatch_event,
                batch.requester_user_id,
                media_events=batch.media_events or None,
                handled_turn=HandledTurnState.create(
                    batch.source_event_ids,
                    source_event_prompts=batch.source_event_prompts,
                ),
            )
            emit_elapsed_timing(
                "coalescing.handle_batch.dispatch_text_message",
                dispatch_start,
                source_event_count=len(batch.source_event_ids),
                timing_scope=timing_scope,
            )

    async def handle_text_event(self, room: nio.MatrixRoom, event: nio.RoomMessageText) -> None:
        """Handle one inbound text event."""
        async with self.deps.resolver.turn_thread_cache_scope():
            await self._handle_message_inner(room, event)

    async def _handle_message_inner(  # noqa: C901, PLR0911
        self,
        room: nio.MatrixRoom,
        event: nio.RoomMessageText,
    ) -> None:
        """Handle one text message inside the per-turn conversation lookup scope."""
        ingress_thread_id = await self.deps.resolver.coalescing_thread_id(room, event)
        event_info = EventInfo.from_event(event.source)
        if not isinstance(event.body, str):
            return
        event_content = event.source.get("content") if isinstance(event.source, dict) else None
        if isinstance(event_content, dict) and event_content.get(STREAM_STATUS_KEY) in {
            STREAM_STATUS_PENDING,
            STREAM_STATUS_STREAMING,
        }:
            return

        prechecked_event = self._precheck_dispatch_event(room, event, is_edit=event_info.is_edit)
        if prechecked_event is None:
            return
        if await self._should_skip_router_before_shared_ingress_work(
            room,
            prechecked_event.event,
            requester_user_id=prechecked_event.requester_user_id,
            thread_id=ingress_thread_id,
        ):
            self.deps.logger.debug(
                "skip_router_shared_ingress_work",
                event_id=event.event_id,
                room_id=room.room_id,
                thread_id=ingress_thread_id,
            )
            return

        self.deps.logger.info(
            "Received message",
            event_id=event.event_id,
            room_id=room.room_id,
            sender=event.sender,
            thread_id=ingress_thread_id,
        )
        dispatch_timing = create_dispatch_pipeline_timing(
            event_id=event.event_id,
            room_id=room.room_id,
        )
        attach_dispatch_pipeline_timing(event.source, dispatch_timing)
        await self._append_live_event_with_timing(
            room.room_id,
            event,
            event_info=event_info,
            dispatch_timing=dispatch_timing,
        )

        if event_info.is_edit:
            await self.deps.edit_regenerator.handle_message_edit(
                room,
                prechecked_event.event,
                event_info,
                prechecked_event.requester_user_id,
            )
            return

        prepared_event = await self._resolve_text_event_with_ingress_timing(
            prechecked_event.event,
            dispatch_timing=dispatch_timing,
        )
        envelope = self.deps.resolver.build_ingress_envelope(
            room_id=room.room_id,
            event=prepared_event,
            requester_user_id=prechecked_event.requester_user_id,
        )
        if self._should_skip_deep_synthetic_full_dispatch(
            event_id=prepared_event.event_id,
            envelope=envelope,
        ):
            return
        coalescing_thread_id = await self.deps.resolver.coalescing_thread_id(room, prepared_event)
        if should_handle_interactive_text_response(envelope):
            selection = await interactive.handle_text_response(
                self._client(),
                room,
                prepared_event,
                self.deps.agent_name,
                resolved_thread_id=coalescing_thread_id,
            )
            if selection is not None:
                await self.handle_interactive_selection(
                    room,
                    selection=selection,
                    user_id=prepared_event.sender,
                    source_event_id=prepared_event.event_id,
                )
                return
        target = self.deps.resolver.build_message_target(
            room_id=room.room_id,
            thread_id=coalescing_thread_id,
            reply_to_event_id=prepared_event.event_id,
            event_source=prepared_event.source,
        )
        if self._should_bypass_coalescing_for_active_thread_follow_up(
            target=target,
            source_kind=envelope.source_kind,
            sender_id=prepared_event.sender,
        ):
            if dispatch_timing is not None:
                dispatch_timing.mark("gate_enter")
                dispatch_timing.note(
                    coalescing_bypassed=True,
                    coalescing_bypass_reason="active_thread_follow_up",
                )
                dispatch_timing.mark("gate_exit")
            await self._dispatch_text_message(
                room,
                prepared_event,
                prechecked_event.requester_user_id,
            )
        else:
            await self._enqueue_for_dispatch(
                prechecked_event.event,
                room,
                source_kind="message",
                requester_user_id=prechecked_event.requester_user_id,
                coalescing_key=(room.room_id, coalescing_thread_id, prechecked_event.requester_user_id),
            )

    async def _dispatch_text_message(  # noqa: C901, PLR0912, PLR0915
        self,
        room: nio.MatrixRoom,
        event: _TextDispatchEvent | _PrecheckedTextDispatchEvent,
        requester_user_id: str | None = None,
        *,
        media_events: list[_MediaDispatchEvent] | None = None,
        handled_turn: HandledTurnState | None = None,
    ) -> None:
        """Run the normal text or command dispatch pipeline for a prepared text event."""
        raw_event: _TextDispatchEvent
        if isinstance(event, _PrecheckedEvent):
            requester_user_id = event.requester_user_id
            raw_event = cast("_TextDispatchEvent", event.event)
        else:
            raw_event = event
        if requester_user_id is None:
            msg = "requester_user_id is required when dispatching a raw event"
            raise TypeError(msg)
        router_event: _DispatchEvent = raw_event
        event = await self.deps.normalizer.resolve_text_event(
            TextNormalizationRequest(event=raw_event),
        )
        dispatch_timing = get_dispatch_pipeline_timing(raw_event.source)
        attach_dispatch_pipeline_timing(event.source, dispatch_timing)
        timing_scope_token = timing_scope_context.set(event_timing_scope(event.event_id))
        try:
            if dispatch_timing is not None:
                dispatch_timing.mark("dispatch_start")
            dispatch_started_at = time.monotonic()
            handled_turn = handled_turn or HandledTurnState.from_source_event_id(event.event_id)

            if dispatch_timing is not None:
                dispatch_timing.mark("dispatch_prepare_start")
            dispatch = await self._prepare_dispatch(
                room,
                event,
                requester_user_id,
                event_label="message",
                handled_turn=handled_turn,
            )
            if dispatch_timing is not None:
                dispatch_timing.mark("dispatch_prepare_ready")
            if dispatch is None:
                return

            command = None
            if not media_events and dispatch.envelope.source_kind != "voice":
                command = command_parser.parse(event.body)
            if command:
                if self.deps.agent_name == ROUTER_AGENT_NAME:
                    await self._execute_command(
                        room=room,
                        event=event,
                        requester_user_id=requester_user_id,
                        command=command,
                    )
                return
            if self._has_newer_unresponded_in_thread(
                event,
                requester_user_id,
                dispatch.context.replay_guard_history,
            ):
                self._mark_source_events_responded(handled_turn)
                return
            if self._should_skip_deep_synthetic_full_dispatch(
                event_id=event.event_id,
                envelope=dispatch.envelope,
            ):
                return
            content = event.source.get("content") if isinstance(event.source, dict) else None
            message_attachment_ids = parse_attachment_ids_from_event_source(event.source)
            message_extra_content: dict[str, Any] = {}
            if message_attachment_ids:
                message_extra_content[ATTACHMENT_IDS_KEY] = message_attachment_ids
            if isinstance(content, dict):
                original_sender = content.get(ORIGINAL_SENDER_KEY)
                if isinstance(original_sender, str):
                    message_extra_content[ORIGINAL_SENDER_KEY] = original_sender
                raw_audio_fallback = content.get(VOICE_RAW_AUDIO_FALLBACK_KEY)
                if isinstance(raw_audio_fallback, bool) and raw_audio_fallback:
                    message_extra_content[VOICE_RAW_AUDIO_FALLBACK_KEY] = True
            router_extra_content = dict(message_extra_content)
            if media_events and ORIGINAL_SENDER_KEY not in router_extra_content:
                router_extra_content[ORIGINAL_SENDER_KEY] = requester_user_id
            await self.deps.resolver.hydrate_dispatch_context(room, event, dispatch.context)
            if dispatch_timing is not None:
                dispatch_timing.mark("dispatch_plan_start")
            plan = await self.deps.turn_policy.plan_turn(
                room,
                event,
                dispatch,
                is_dm=await is_dm_room(self._client(), room.room_id),
                has_active_response_for_target=self.deps.response_runner.has_active_response_for_target,
                extra_content=router_extra_content or None,
                media_events=media_events,
                router_event=media_events[0]
                if media_events and len(handled_turn.source_event_ids) == 1
                else router_event,
            )
            if dispatch_timing is not None:
                dispatch_timing.mark("dispatch_plan_ready")
            if plan.kind == "ignore":
                if plan.ignore_reason == "router":
                    router_outcome = self._router_handled_turn_outcome(handled_turn)
                    if router_outcome is not None:
                        self._mark_source_events_responded(router_outcome)
                return
            if plan.kind == "route":
                route_event = plan.router_event or event
                tracked_route_handled_turn = (
                    handled_turn
                    if handled_turn.is_coalesced
                    or (handled_turn.source_event_ids and handled_turn.source_event_ids[0] != event.event_id)
                    else None
                )
                single_direct_media_route = (
                    isinstance(
                        route_event,
                        nio.RoomMessageFile
                        | nio.RoomEncryptedFile
                        | nio.RoomMessageVideo
                        | nio.RoomEncryptedVideo
                        | nio.RoomMessageImage
                        | nio.RoomEncryptedImage,
                    )
                    and media_events == [route_event]
                    and handled_turn.source_event_ids == (event.event_id,)
                )
                routing_kwargs: dict[str, Any] = {
                    "message": event.body if media_events else plan.router_message,
                    "requester_user_id": dispatch.requester_user_id,
                    "extra_content": plan.extra_content,
                }
                if plan.media_events is not None and not single_direct_media_route:
                    routing_kwargs["media_events"] = plan.media_events
                if (
                    tracked_route_handled_turn is not None
                    and list(tracked_route_handled_turn.source_event_ids) != [route_event.event_id]
                    and not single_direct_media_route
                ):
                    routing_kwargs["handled_turn"] = self.deps.turn_store.attach_response_context(
                        tracked_route_handled_turn,
                        history_scope=None,
                        conversation_target=dispatch.target,
                    )
                await self._execute_router_relay(
                    room,
                    route_event,
                    dispatch.context.thread_history,
                    dispatch.context.thread_id,
                    **routing_kwargs,
                )
                return
            assert plan.response_action is not None
            handled_turn = self.deps.turn_store.attach_response_context(
                handled_turn,
                history_scope=self.deps.turn_store.response_history_scope(plan.response_action),
                conversation_target=dispatch.target,
            )
            matrix_run_metadata = self.deps.turn_store.build_run_metadata(handled_turn)

            async def build_payload(context: MessageContext) -> DispatchPayload:
                effective_thread_id = self.deps.resolver.build_message_target(
                    room_id=room.room_id,
                    thread_id=context.thread_id,
                    reply_to_event_id=event.event_id,
                    event_source=event.source,
                ).resolved_thread_id
                media_attachment_ids: list[str] = []
                fallback_images: list[Image] | None = None
                if media_events:
                    media_result = await self.deps.normalizer.register_batch_media_attachments(
                        BatchMediaAttachmentRequest(
                            room_id=room.room_id,
                            thread_id=effective_thread_id,
                            media_events=media_events,
                        ),
                    )
                    media_attachment_ids = media_result.attachment_ids
                    fallback_images = media_result.fallback_images
                return await self.deps.normalizer.build_dispatch_payload_with_attachments(
                    DispatchPayloadWithAttachmentsRequest(
                        room_id=room.room_id,
                        prompt=event.body,
                        current_attachment_ids=merge_attachment_ids(
                            message_attachment_ids,
                            media_attachment_ids,
                        ),
                        thread_id=context.thread_id,
                        media_thread_id=effective_thread_id,
                        thread_history=context.thread_history,
                        fallback_images=fallback_images,
                    ),
                )

            await self._execute_response_action(
                room,
                event,
                dispatch,
                plan.response_action,
                build_payload,
                processing_log="Processing",
                dispatch_started_at=dispatch_started_at,
                handled_turn=handled_turn,
                matrix_run_metadata=matrix_run_metadata,
            )
        finally:
            timing_scope_context.reset(timing_scope_token)

    async def handle_media_event(
        self,
        room: nio.MatrixRoom,
        event: _MediaDispatchEvent,
    ) -> None:
        """Handle one inbound media event."""
        async with self.deps.resolver.turn_thread_cache_scope():
            await self._handle_media_message_inner(room, event)

    async def _handle_media_message_inner(
        self,
        room: nio.MatrixRoom,
        event: _MediaDispatchEvent,
    ) -> None:
        """Handle one media event inside the per-turn conversation lookup scope."""
        prechecked_event = self._precheck_dispatch_event(room, event)
        if prechecked_event is None:
            return
        dispatch_timing = create_dispatch_pipeline_timing(
            event_id=prechecked_event.event.event_id,
            room_id=room.room_id,
        )
        attach_dispatch_pipeline_timing(prechecked_event.event.source, dispatch_timing)
        # Prime transitive ancestor lookups before writing advisory cache membership.
        await self.deps.resolver.coalescing_thread_id(room, prechecked_event.event)
        event_info = EventInfo.from_event(prechecked_event.event.source)
        await self._append_live_event_with_timing(
            room.room_id,
            prechecked_event.event,
            event_info=event_info,
            dispatch_timing=dispatch_timing,
        )

        if await self._dispatch_special_media_as_text(room, prechecked_event):
            return
        event = prechecked_event.event
        await self._enqueue_for_dispatch(
            event,
            room,
            source_kind="image" if isinstance(event, nio.RoomMessageImage | nio.RoomEncryptedImage) else "media",
            requester_user_id=prechecked_event.requester_user_id,
        )

    async def _dispatch_special_media_as_text(
        self,
        room: nio.MatrixRoom,
        prechecked_event: _PrecheckedMediaDispatchEvent,
    ) -> bool:
        """Handle media events that normalize into the text dispatch pipeline."""
        event = prechecked_event.event
        if isinstance(event, nio.RoomMessageAudio | nio.RoomEncryptedAudio):
            await self._on_audio_media_message(
                room,
                _PrecheckedEvent(
                    event=event,
                    requester_user_id=prechecked_event.requester_user_id,
                ),
            )
            return True
        if isinstance(event, nio.RoomMessageFile | nio.RoomEncryptedFile):
            return await self._dispatch_file_sidecar_text_preview(
                room,
                _PrecheckedEvent(
                    event=event,
                    requester_user_id=prechecked_event.requester_user_id,
                ),
            )
        return False

    async def _on_audio_media_message(
        self,
        room: nio.MatrixRoom,
        prechecked_event: _PrecheckedEvent[nio.RoomMessageAudio | nio.RoomEncryptedAudio],
    ) -> None:
        """Normalize audio into a synthetic text event and reuse text dispatch."""
        event = prechecked_event.event

        if is_agent_id(event.sender, self.deps.runtime.config, self.deps.runtime_paths):
            self.deps.logger.debug(
                "Ignoring agent audio event for voice transcription",
                event_id=event.event_id,
                sender=event.sender,
            )
            self._mark_source_events_responded(HandledTurnState.from_source_event_id(event.event_id))
            return

        dispatch_timing = get_dispatch_pipeline_timing(event.source)
        if dispatch_timing is not None:
            dispatch_timing.mark("ingress_normalize_start")
        normalized_voice = await self.deps.normalizer.prepare_voice_event(
            VoiceNormalizationRequest(
                room=room,
                event=event,
            ),
        )
        if dispatch_timing is not None:
            dispatch_timing.mark("ingress_normalize_ready")
        if normalized_voice is None:
            self._mark_source_events_responded(HandledTurnState.from_source_event_id(event.event_id))
            return
        attach_dispatch_pipeline_timing(
            normalized_voice.event.source,
            dispatch_timing,
        )

        await self._maybe_send_visible_voice_echo(
            room,
            event,
            text=normalized_voice.event.body,
            thread_id=normalized_voice.effective_thread_id,
        )

        await self._enqueue_for_dispatch(
            normalized_voice.event,
            room,
            source_kind="voice",
            requester_user_id=prechecked_event.requester_user_id,
        )

    async def _dispatch_file_sidecar_text_preview(
        self,
        room: nio.MatrixRoom,
        prechecked_event: _PrecheckedEvent[nio.RoomMessageFile | nio.RoomEncryptedFile],
    ) -> bool:
        """Dispatch one sidecar-backed file preview through the normal text pipeline."""
        event = prechecked_event.event
        if not is_v2_sidecar_text_preview(event.source):
            return False

        dispatch_timing = get_dispatch_pipeline_timing(event.source)
        if dispatch_timing is not None:
            dispatch_timing.mark("ingress_normalize_start")
        prepared_text_event = await self.deps.normalizer.prepare_file_sidecar_text_event(event)
        if dispatch_timing is not None:
            dispatch_timing.mark("ingress_normalize_ready")
        assert prepared_text_event is not None
        attach_dispatch_pipeline_timing(prepared_text_event.source, dispatch_timing)
        envelope = self.deps.resolver.build_ingress_envelope(
            room_id=room.room_id,
            event=prepared_text_event,
            requester_user_id=prechecked_event.requester_user_id,
        )
        if self._should_skip_deep_synthetic_full_dispatch(
            event_id=prepared_text_event.event_id,
            envelope=envelope,
        ):
            return True
        coalescing_thread_id = await self.deps.resolver.coalescing_thread_id(room, prepared_text_event)
        if should_handle_interactive_text_response(envelope):
            selection = await interactive.handle_text_response(
                self._client(),
                room,
                prepared_text_event,
                self.deps.agent_name,
                resolved_thread_id=coalescing_thread_id,
            )
            if selection is not None:
                await self.handle_interactive_selection(
                    room,
                    selection=selection,
                    user_id=prepared_text_event.sender,
                    source_event_id=prepared_text_event.event_id,
                )
                return True
        await self._dispatch_text_message(
            room,
            _PrecheckedEvent(
                event=prepared_text_event,
                requester_user_id=prechecked_event.requester_user_id,
            ),
        )
        return True

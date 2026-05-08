"""Own conversation identity and ingress envelope assembly for inbound turns."""

from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import nio
from nio.responses import RoomGetEventError

from mindroom.attachments import parse_attachment_ids_from_event_source
from mindroom.constants import HOOK_MESSAGE_RECEIVED_DEPTH_KEY
from mindroom.dispatch_handoff import DispatchEvent, DispatchPayloadMetadata, PreparedTextEvent
from mindroom.dispatch_thread_context import (
    DispatchThreadContext,
    context_with_dispatch_thread_context,
    planning_history_for,
    planning_history_unavailable_for,
)
from mindroom.matrix.cache.thread_history_result import ThreadHistoryResult
from mindroom.matrix.cache.thread_reads import ThreadReadMode
from mindroom.matrix.client_delivery import cached_room as matrix_cached_room
from mindroom.matrix.event_info import EventInfo
from mindroom.matrix.identity import MatrixID, extract_agent_name
from mindroom.matrix.media import MatrixMediaEvent, is_audio_message_event, is_image_message_event
from mindroom.matrix.message_content import resolve_event_source_content
from mindroom.matrix.thread_diagnostics import is_thread_history_degraded
from mindroom.matrix.thread_membership import (
    ThreadMembershipAccess,
    resolve_event_thread_membership,
    resolve_related_event_thread_id_best_effort,
    thread_messages_thread_membership_access,
)
from mindroom.message_target import MessageTarget
from mindroom.runtime_protocols import SupportsClientConfig  # noqa: TC001
from mindroom.thread_utils import check_agent_mentioned

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Sequence

    import structlog

    from mindroom.constants import RuntimePaths
    from mindroom.hooks import MessageEnvelope
    from mindroom.matrix.client_visible_messages import ResolvedVisibleMessage
    from mindroom.matrix.conversation_cache import MatrixConversationCache, ThreadReadResult


_SKIP_MENTIONS_KEY = "com.mindroom.skip_mentions"


def _should_skip_mentions(event_source: dict[str, Any]) -> bool:
    """Return whether mentions in this message should be ignored."""
    content = event_source.get("content", {})
    if not isinstance(content, dict):
        return False
    if bool(content.get(_SKIP_MENTIONS_KEY, False)):
        return True

    new_content = content.get("m.new_content")
    return isinstance(new_content, dict) and bool(new_content.get(_SKIP_MENTIONS_KEY, False))


def _with_skip_mentions_metadata(content: dict[str, Any], skip_mentions: bool) -> dict[str, Any]:
    content[_SKIP_MENTIONS_KEY] = skip_mentions
    new_content = content.get("m.new_content")
    if isinstance(new_content, dict):
        visible_content = dict(new_content)
        if skip_mentions:
            visible_content[_SKIP_MENTIONS_KEY] = True
        else:
            visible_content.pop(_SKIP_MENTIONS_KEY, None)
        content["m.new_content"] = visible_content
    return content


def _source_with_payload_metadata(
    event_source: dict[str, Any],
    payload_metadata: DispatchPayloadMetadata | None,
) -> dict[str, Any]:
    """Return event source overlaid with trusted handoff payload metadata."""
    if payload_metadata is None:
        return event_source
    content = event_source.get("content")
    content = {} if not isinstance(content, dict) else dict(content)
    if payload_metadata.mentioned_user_ids is not None:
        content["m.mentions"] = {"user_ids": list(payload_metadata.mentioned_user_ids)}
    if payload_metadata.formatted_bodies is not None:
        if payload_metadata.formatted_bodies:
            content["formatted_body"] = "<br>".join(payload_metadata.formatted_bodies)
            content["format"] = "org.matrix.custom.html"
        else:
            content.pop("formatted_body", None)
    if payload_metadata.skip_mentions is not None:
        content = _with_skip_mentions_metadata(content, payload_metadata.skip_mentions)
    return {**event_source, "content": content}


@dataclass
class MessageContext:
    """Context extracted from a Matrix message event."""

    am_i_mentioned: bool
    is_thread: bool
    thread_id: str | None
    thread_history: Sequence[ResolvedVisibleMessage]
    mentioned_agents: list[MatrixID]
    has_non_agent_mentions: bool
    replay_guard_history: Sequence[ResolvedVisibleMessage] = field(default_factory=tuple)
    requires_model_history_refresh: bool = False

    @property
    def planning_thread_history(self) -> Sequence[ResolvedVisibleMessage]:
        """Return thread history only when it is safe to use for planning decisions."""
        return planning_history_for(self.thread_history)

    @property
    def planning_thread_history_unavailable(self) -> bool:
        """Return whether thread policy history degraded and must not be treated as empty."""
        return self.is_thread and planning_history_unavailable_for(
            self.thread_history,
            requires_model_history_refresh=self.requires_model_history_refresh,
        )


@dataclass(frozen=True)
class _ThreadIdLookup:
    """Resolved thread id plus any dispatch-local candidate."""

    thread_id: str | None
    candidate_thread_root_id: str | None = None
    thread_history: ThreadReadResult | None = None


@dataclass(frozen=True)
class _ThreadContextLookup:
    """Resolved thread context from one Matrix event."""

    is_thread: bool
    thread_id: str | None
    thread_history: Sequence[ResolvedVisibleMessage]
    requires_model_history_refresh: bool
    candidate_thread_root_id: str | None = None
    replay_guard_history: Sequence[ResolvedVisibleMessage] = field(default_factory=tuple)
    replay_guard_degraded: bool = False

    @classmethod
    def room_level(cls) -> _ThreadContextLookup:
        """Return a proven room-level context."""
        return cls(
            is_thread=False,
            thread_id=None,
            thread_history=[],
            requires_model_history_refresh=False,
        )

    @classmethod
    def unproven_candidate_without_history(
        cls,
        candidate_thread_root_id: str,
    ) -> _ThreadContextLookup:
        """Return a room-level demotion when candidate proof produced no reusable history."""
        return cls(
            is_thread=False,
            thread_id=None,
            thread_history=[],
            requires_model_history_refresh=False,
            candidate_thread_root_id=candidate_thread_root_id,
            replay_guard_history=[],
            replay_guard_degraded=True,
        )

    @classmethod
    def unproven_candidate_demoted(
        cls,
        candidate_thread_root_id: str,
        candidate_history: Sequence[ResolvedVisibleMessage],
    ) -> _ThreadContextLookup:
        """Return a room-level demotion that keeps candidate history only for replay safety."""
        return cls(
            is_thread=False,
            thread_id=None,
            thread_history=[],
            requires_model_history_refresh=False,
            candidate_thread_root_id=candidate_thread_root_id,
            replay_guard_history=candidate_history,
            replay_guard_degraded=is_thread_history_degraded(candidate_history),
        )

    @classmethod
    def proven_thread(
        cls,
        thread_id: str,
        history: ThreadReadResult,
    ) -> _ThreadContextLookup:
        """Return a proven thread context with model and replay history."""
        return cls(
            is_thread=True,
            thread_id=thread_id,
            thread_history=history,
            requires_model_history_refresh=not history.is_full_history,
            replay_guard_history=history,
            replay_guard_degraded=is_thread_history_degraded(history),
        )


@dataclass(frozen=True)
class DispatchContextResult:
    """Stable message context plus dispatch-local thread resolution evidence."""

    context: MessageContext
    thread_context: DispatchThreadContext | None


@dataclass(frozen=True)
class ConversationResolverDeps:
    """Explicit collaborators for conversation resolution."""

    runtime: SupportsClientConfig
    logger: structlog.stdlib.BoundLogger
    runtime_paths: RuntimePaths
    agent_name: str
    matrix_id: MatrixID
    conversation_cache: MatrixConversationCache


@dataclass
class ConversationResolver:
    """Resolve explicit thread context, history, mentions, and ingress envelopes."""

    deps: ConversationResolverDeps

    def _client(self) -> nio.AsyncClient:
        client = self.deps.runtime.client
        if client is None:
            msg = "Matrix client is not ready for conversation resolution"
            raise RuntimeError(msg)
        return client

    def _matrix_id(self) -> MatrixID:
        return self.deps.matrix_id

    def _envelope_ingress_metadata(  # noqa: C901
        self,
        *,
        event: DispatchEvent,
        source_kind: str | None = None,
        hook_source: str | None = None,
        message_received_depth: int | None = None,
    ) -> tuple[str, str | None, int]:
        """Return source-kind and hook ingress metadata for one inbound event."""
        content = event.source.get("content") if isinstance(event.source, dict) else None
        resolved_source_kind = (
            source_kind
            if source_kind is not None
            else event.source_kind_override
            if isinstance(event, PreparedTextEvent)
            else None
        )
        config = self.deps.runtime.config
        source_kind_sender_is_trusted = extract_agent_name(event.sender, config, self.deps.runtime_paths) is not None
        if resolved_source_kind is None and isinstance(content, dict):
            source_kind_override = content.get("com.mindroom.source_kind")
            if isinstance(source_kind_override, str) and source_kind_override and source_kind_sender_is_trusted:
                resolved_source_kind = source_kind_override
        if resolved_source_kind is None:
            if is_audio_message_event(event):
                resolved_source_kind = "voice"
            elif is_image_message_event(event):
                resolved_source_kind = "image"
            else:
                resolved_source_kind = "message"

        resolved_hook_source: str | None = hook_source
        resolved_message_received_depth = message_received_depth or 0
        if isinstance(content, dict) and source_kind_sender_is_trusted:
            if resolved_hook_source is None:
                hook_source_override = content.get("com.mindroom.hook_source")
                if isinstance(hook_source_override, str) and hook_source_override:
                    resolved_hook_source = hook_source_override
            if resolved_message_received_depth <= 0:
                depth_override = content.get(HOOK_MESSAGE_RECEIVED_DEPTH_KEY)
                if isinstance(depth_override, int) and not isinstance(depth_override, bool) and depth_override > 0:
                    resolved_message_received_depth = depth_override
        return resolved_source_kind, resolved_hook_source, resolved_message_received_depth

    def build_message_target(
        self,
        *,
        room_id: str,
        thread_id: str | None,
        reply_to_event_id: str | None,
        event_source: dict[str, Any] | None = None,
        thread_mode_override: str | None = None,
    ) -> MessageTarget:
        """Build the canonical delivery target for one outbound response."""
        config = self.deps.runtime.config
        effective_thread_mode = thread_mode_override or config.get_entity_thread_mode(
            self.deps.agent_name,
            self.deps.runtime_paths,
            room_id=room_id,
        )
        thread_start_root_event_id = None
        if event_source is not None:
            event_info = EventInfo.from_event(event_source)
            if event_info.can_be_thread_root and reply_to_event_id is not None:
                thread_start_root_event_id = reply_to_event_id
        return MessageTarget.resolve(
            room_id=room_id,
            thread_id=thread_id,
            reply_to_event_id=reply_to_event_id,
            thread_start_root_event_id=thread_start_root_event_id,
            room_mode=effective_thread_mode == "room",
        )

    def resolve_response_thread_root(
        self,
        thread_id: str | None,
        reply_to_event_id: str | None,
        *,
        room_id: str,
        response_envelope: MessageEnvelope | None = None,
    ) -> str | None:
        """Return the canonical thread root for outbound response delivery."""
        if response_envelope is not None:
            return response_envelope.target.resolved_thread_id
        return self.build_message_target(
            room_id=room_id,
            thread_id=thread_id,
            reply_to_event_id=reply_to_event_id,
        ).resolved_thread_id

    def build_message_envelope(
        self,
        *,
        room_id: str,
        event: DispatchEvent,
        requester_user_id: str,
        context: MessageContext,
        target: MessageTarget | None = None,
        attachment_ids: list[str] | None = None,
        agent_name: str | None = None,
        body: str | None = None,
        source_kind: str | None = None,
        dispatch_policy_source_kind: str | None = None,
        hook_source: str | None = None,
        message_received_depth: int | None = None,
    ) -> MessageEnvelope:
        """Build the normalized inbound envelope consumed by message hooks."""
        from mindroom.hooks import MessageEnvelope  # noqa: PLC0415

        config = self.deps.runtime.config
        resolved_source_kind, hook_source, message_received_depth = self._envelope_ingress_metadata(
            event=event,
            source_kind=source_kind,
            hook_source=hook_source,
            message_received_depth=message_received_depth,
        )
        resolved_target = target or self.build_message_target(
            room_id=room_id,
            thread_id=context.thread_id,
            reply_to_event_id=event.event_id,
            event_source=event.source,
        )

        return MessageEnvelope(
            source_event_id=event.event_id,
            room_id=room_id,
            target=resolved_target,
            requester_id=requester_user_id,
            sender_id=event.sender,
            body=body or event.body,
            attachment_ids=tuple(
                attachment_ids if attachment_ids is not None else parse_attachment_ids_from_event_source(event.source),
            ),
            mentioned_agents=tuple(
                agent_id.agent_name(config, self.deps.runtime_paths) or agent_id.username
                for agent_id in context.mentioned_agents
            ),
            agent_name=agent_name or self.deps.agent_name,
            source_kind=resolved_source_kind,
            hook_source=hook_source,
            message_received_depth=message_received_depth,
            dispatch_policy_source_kind=dispatch_policy_source_kind,
        )

    def build_ingress_envelope(
        self,
        *,
        room_id: str,
        event: DispatchEvent,
        requester_user_id: str,
        thread_id: str | None = None,
        attachment_ids: list[str] | None = None,
        agent_name: str | None = None,
        body: str | None = None,
        source_kind: str | None = None,
        dispatch_policy_source_kind: str | None = None,
        hook_source: str | None = None,
        message_received_depth: int | None = None,
    ) -> MessageEnvelope:
        """Build one lightweight ingress envelope without extracting thread context."""
        from mindroom.hooks import MessageEnvelope  # noqa: PLC0415

        resolved_source_kind, hook_source, message_received_depth = self._envelope_ingress_metadata(
            event=event,
            source_kind=source_kind,
            hook_source=hook_source,
            message_received_depth=message_received_depth,
        )
        return MessageEnvelope(
            source_event_id=event.event_id,
            room_id=room_id,
            target=self.build_message_target(
                room_id=room_id,
                thread_id=thread_id,
                reply_to_event_id=event.event_id,
                event_source=event.source,
            ),
            requester_id=requester_user_id,
            sender_id=event.sender,
            body=body or event.body,
            attachment_ids=tuple(
                attachment_ids if attachment_ids is not None else parse_attachment_ids_from_event_source(event.source),
            ),
            mentioned_agents=(),
            agent_name=agent_name or self.deps.agent_name,
            source_kind=resolved_source_kind,
            hook_source=hook_source,
            message_received_depth=message_received_depth,
            dispatch_policy_source_kind=dispatch_policy_source_kind,
        )

    async def coalescing_thread_id(
        self,
        room: nio.MatrixRoom,
        event: DispatchEvent | MatrixMediaEvent,
    ) -> str | None:
        """Return the coalescing thread scope for one inbound event."""
        config = self.deps.runtime.config
        if (
            config.get_entity_thread_mode(
                self.deps.agent_name,
                self.deps.runtime_paths,
                room_id=room.room_id,
            )
            == "room"
        ):
            return None
        try:
            resolution = await resolve_event_thread_membership(
                room.room_id,
                EventInfo.from_event(event.source),
                event_id=event.event_id,
                access=self.thread_membership_access(
                    mode=ThreadReadMode.DISPATCH_SNAPSHOT,
                    caller_label="coalescing_thread_id",
                ),
            )
        except Exception as exc:
            # Coalescing is only a batching optimization; any membership failure must fail open.
            self.deps.logger.debug(
                "Failed to resolve coalescing thread id; continuing room-level",
                room_id=room.room_id,
                event_id=event.event_id,
                error=str(exc),
            )
            return None
        else:
            return resolution.thread_id or resolution.candidate_thread_root_id

    async def _explicit_thread_id_for_event(
        self,
        room_id: str,
        event_id: str | None,
        event_info: EventInfo,
        *,
        mode: ThreadReadMode,
        caller_label: str,
    ) -> _ThreadIdLookup:
        """Resolve thread membership and identify unproven dispatch candidates."""
        access = self.thread_membership_access(
            mode=mode,
            caller_label=caller_label,
        )
        resolution = await resolve_event_thread_membership(
            room_id,
            event_info,
            event_id=event_id,
            access=access,
        )
        thread_history = (
            resolution.thread_history if isinstance(resolution.thread_history, ThreadHistoryResult) else None
        )
        if not mode.dispatch_safe:
            return _ThreadIdLookup(thread_id=resolution.thread_id, thread_history=thread_history)
        if resolution.thread_id is not None:
            return _ThreadIdLookup(thread_id=resolution.thread_id, thread_history=thread_history)
        if resolution.candidate_thread_root_id is not None:
            return _ThreadIdLookup(
                thread_id=None,
                candidate_thread_root_id=resolution.candidate_thread_root_id,
                thread_history=thread_history,
            )
        return _ThreadIdLookup(thread_id=None, thread_history=thread_history)

    async def resolve_related_event_thread_id_dispatch_snapshot_best_effort(
        self,
        room_id: str,
        related_event_id: str,
        *,
        caller_label: str,
    ) -> str | None:
        """Return dispatch-snapshot thread membership without exposing cache read modes."""
        return await resolve_related_event_thread_id_best_effort(
            room_id,
            related_event_id,
            access=self.thread_membership_access(
                mode=ThreadReadMode.DISPATCH_SNAPSHOT,
                caller_label=caller_label,
            ),
        )

    def thread_membership_access(
        self,
        *,
        mode: ThreadReadMode,
        caller_label: str,
    ) -> ThreadMembershipAccess:
        """Return the shared thread-membership accessors for this resolver."""
        return thread_messages_thread_membership_access(
            lookup_thread_id=self.deps.conversation_cache.get_thread_id_for_event,
            fetch_event_info=self._event_info_for_event_id,
            fetch_thread_messages=lambda room_id, thread_id: self._read_thread_messages(
                room_id,
                thread_id,
                mode=mode,
                caller_label=caller_label,
            ),
        )

    async def _read_thread_messages(
        self,
        room_id: str,
        thread_id: str,
        *,
        mode: ThreadReadMode,
        caller_label: str,
    ) -> ThreadReadResult:
        """Resolve one thread read through the shared cache entrypoint."""
        read_thread = {
            ThreadReadMode.ADVISORY_FULL: self.deps.conversation_cache.get_thread_history,
            ThreadReadMode.DISPATCH_SNAPSHOT: self.deps.conversation_cache.get_dispatch_thread_snapshot,
            ThreadReadMode.DISPATCH_FULL: self.deps.conversation_cache.get_dispatch_thread_history,
        }[mode]
        return await read_thread(room_id, thread_id, caller_label=caller_label)

    async def _event_info_for_event_id(
        self,
        room_id: str,
        event_id: str,
    ) -> EventInfo | None:
        target_event = await self.deps.conversation_cache.get_event(room_id, event_id)
        if not isinstance(target_event, nio.RoomGetEventResponse):
            if isinstance(target_event, RoomGetEventError) and target_event.status_code == "M_NOT_FOUND":
                return None
            detail = (
                target_event.message
                if isinstance(target_event, RoomGetEventError) and isinstance(target_event.message, str)
                else "unknown error"
            )
            msg = f"Failed to resolve related Matrix event {event_id}: {detail}"
            raise RuntimeError(msg)
        return EventInfo.from_event(target_event.event.source)

    async def _resolve_thread_context(
        self,
        room_id: str,
        event_id: str | None,
        event_info: EventInfo,
        *,
        mode: ThreadReadMode,
        caller_label: str,
    ) -> _ThreadContextLookup:
        """Resolve one thread context using either snapshot or full history."""
        thread_lookup = await self._explicit_thread_id_for_event(
            room_id,
            event_id,
            event_info,
            mode=mode,
            caller_label=caller_label,
        )
        thread_id = thread_lookup.thread_id
        if thread_id is None:
            if thread_lookup.candidate_thread_root_id is None:
                return _ThreadContextLookup.room_level()
            candidate_history = thread_lookup.thread_history
            if candidate_history is None:
                return _ThreadContextLookup.unproven_candidate_without_history(
                    thread_lookup.candidate_thread_root_id,
                )
            return _ThreadContextLookup.unproven_candidate_demoted(
                thread_lookup.candidate_thread_root_id,
                candidate_history,
            )

        thread_messages = thread_lookup.thread_history
        if thread_messages is None:
            thread_messages = await self._read_thread_messages(
                room_id,
                thread_id,
                mode=mode,
                caller_label=caller_label,
            )
        return _ThreadContextLookup.proven_thread(
            thread_id,
            thread_messages,
        )

    async def extract_dispatch_context(
        self,
        room: nio.MatrixRoom,
        event: DispatchEvent | MatrixMediaEvent,
        *,
        payload_metadata: DispatchPayloadMetadata | None = None,
        mode: ThreadReadMode = ThreadReadMode.DISPATCH_FULL,
        caller_label: str = "dispatch_context",
    ) -> DispatchContextResult:
        """Extract bounded dispatch context using the requested thread read mode."""
        context, thread_context = await self._extract_message_context_parts(
            room,
            event,
            mode=mode,
            include_dispatch_context=True,
            payload_metadata=payload_metadata,
            caller_label=caller_label,
        )
        return DispatchContextResult(context=context, thread_context=thread_context)

    async def extract_trusted_router_relay_context(
        self,
        room: nio.MatrixRoom,
        event: DispatchEvent,
        *,
        payload_metadata: DispatchPayloadMetadata | None = None,
    ) -> DispatchContextResult:
        """Extract minimal context for router relays and defer thread hydration until after lock."""
        resolved_event_source = await resolve_event_source_content(event.source, self._client())
        resolved_event_source = _source_with_payload_metadata(resolved_event_source, payload_metadata)
        config = self.deps.runtime.config

        if _should_skip_mentions(resolved_event_source):
            mentioned_agents: list[MatrixID] = []
            am_i_mentioned = False
            has_non_agent_mentions = False
        else:
            mentioned_agents, am_i_mentioned, has_non_agent_mentions = check_agent_mentioned(
                resolved_event_source,
                self._matrix_id(),
                config,
                self.deps.runtime_paths,
            )

        if am_i_mentioned:
            self.deps.logger.info("Mentioned", event_id=event.event_id, room_id=room.room_id)

        if (
            config.get_entity_thread_mode(
                self.deps.agent_name,
                self.deps.runtime_paths,
                room_id=room.room_id,
            )
            == "room"
        ):
            resolved_thread_id = None
        else:
            event_info = EventInfo.from_event(resolved_event_source)
            resolved_thread_id = event_info.thread_id or event_info.thread_id_from_edit
        context = MessageContext(
            am_i_mentioned=am_i_mentioned,
            is_thread=resolved_thread_id is not None,
            thread_id=resolved_thread_id,
            thread_history=(),
            mentioned_agents=mentioned_agents,
            has_non_agent_mentions=has_non_agent_mentions,
            replay_guard_history=(),
            requires_model_history_refresh=resolved_thread_id is not None,
        )
        return DispatchContextResult(context=context, thread_context=None)

    async def resolve_dispatch_target(
        self,
        room: nio.MatrixRoom,
        event: DispatchEvent | MatrixMediaEvent,
        *,
        caller_label: str,
    ) -> MessageTarget:
        """Resolve a bounded stable target for non-response dispatch helpers."""
        context_result = await self.extract_dispatch_context(
            room,
            event,
            mode=ThreadReadMode.DISPATCH_SNAPSHOT,
            caller_label=caller_label,
        )
        if context_result.thread_context is not None:
            return context_result.thread_context.stable_target
        return self.build_message_target(
            room_id=room.room_id,
            thread_id=context_result.context.thread_id,
            reply_to_event_id=event.event_id,
            event_source=event.source,
        )

    async def extract_message_context(
        self,
        room: nio.MatrixRoom,
        event: DispatchEvent,
        *,
        payload_metadata: DispatchPayloadMetadata | None = None,
        caller_label: str = "message_context",
    ) -> MessageContext:
        """Extract advisory full message context for one inbound turn."""
        context, _thread_context = await self._extract_message_context_parts(
            room,
            event,
            mode=ThreadReadMode.ADVISORY_FULL,
            include_dispatch_context=False,
            payload_metadata=payload_metadata,
            caller_label=caller_label,
        )
        return context

    async def _extract_message_context_parts(
        self,
        room: nio.MatrixRoom,
        event: DispatchEvent | MatrixMediaEvent,
        *,
        mode: ThreadReadMode,
        include_dispatch_context: bool,
        payload_metadata: DispatchPayloadMetadata | None = None,
        caller_label: str,
    ) -> tuple[MessageContext, DispatchThreadContext | None]:
        """Resolve event metadata, mentions, stable context, and optional dispatch-local state."""
        resolved_event_source = await resolve_event_source_content(event.source, self._client())
        resolved_event_source = _source_with_payload_metadata(resolved_event_source, payload_metadata)
        config = self.deps.runtime.config

        if _should_skip_mentions(resolved_event_source):
            mentioned_agents: list[MatrixID] = []
            am_i_mentioned = False
            has_non_agent_mentions = False
        else:
            mentioned_agents, am_i_mentioned, has_non_agent_mentions = check_agent_mentioned(
                resolved_event_source,
                self._matrix_id(),
                config,
                self.deps.runtime_paths,
            )

        if am_i_mentioned:
            self.deps.logger.info("Mentioned", event_id=event.event_id, room_id=room.room_id)

        event_info = EventInfo.from_event(resolved_event_source)
        dispatch_context: DispatchThreadContext | None = None
        if (
            config.get_entity_thread_mode(
                self.deps.agent_name,
                self.deps.runtime_paths,
                room_id=room.room_id,
            )
            == "room"
        ):
            is_thread = False
            thread_id = None
            thread_history: list[ResolvedVisibleMessage] = []
            requires_model_history_refresh = False
            replay_guard_history: Sequence[ResolvedVisibleMessage] = ()
        else:
            thread_lookup = await self._resolve_thread_context(
                room.room_id,
                event.event_id,
                event_info,
                mode=mode,
                caller_label=caller_label,
            )
            is_thread = thread_lookup.is_thread
            thread_id = thread_lookup.thread_id
            thread_history = thread_lookup.thread_history
            requires_model_history_refresh = thread_lookup.requires_model_history_refresh
            replay_guard_history = thread_lookup.replay_guard_history
            if include_dispatch_context:
                if thread_lookup.candidate_thread_root_id is not None and thread_lookup.thread_id is None:
                    stable_target = MessageTarget.resolve(
                        room_id=room.room_id,
                        thread_id=None,
                        reply_to_event_id=event.event_id,
                        room_mode=True,
                    )
                else:
                    stable_target = self.build_message_target(
                        room_id=room.room_id,
                        thread_id=thread_lookup.thread_id,
                        reply_to_event_id=event.event_id,
                        event_source=event.source,
                    )
                dispatch_context = DispatchThreadContext(
                    stable_target=stable_target,
                    candidate_thread_root_id=thread_lookup.candidate_thread_root_id,
                    thread_history=thread_lookup.thread_history,
                    requires_model_history_refresh=thread_lookup.requires_model_history_refresh,
                    replay_guard_history=thread_lookup.replay_guard_history,
                    replay_guard_degraded=thread_lookup.replay_guard_degraded,
                )

        context = MessageContext(
            am_i_mentioned=am_i_mentioned,
            is_thread=is_thread,
            thread_id=thread_id,
            thread_history=thread_history,
            mentioned_agents=mentioned_agents,
            has_non_agent_mentions=has_non_agent_mentions,
            replay_guard_history=replay_guard_history,
            requires_model_history_refresh=requires_model_history_refresh,
        )
        if dispatch_context is not None:
            context = context_with_dispatch_thread_context(context, dispatch_context)
        return context, dispatch_context

    def cached_room(self, room_id: str) -> nio.MatrixRoom | None:
        """Return room from client cache when available."""
        client = self.deps.runtime.client
        if client is None:
            return None
        return matrix_cached_room(client, room_id)

    @asynccontextmanager
    async def turn_thread_cache_scope(self) -> AsyncIterator[None]:
        """Initialize per-turn conversation lookup memoization."""
        async with self.deps.conversation_cache.turn_scope():
            yield

    async def fetch_thread_history(
        self,
        room_id: str,
        thread_id: str,
        *,
        caller_label: str = "unknown",
    ) -> ThreadReadResult:
        """Fetch strict full thread history through the shared conversation-cache policy."""
        return await self.deps.conversation_cache.get_strict_thread_history(
            room_id,
            thread_id,
            caller_label=caller_label,
        )

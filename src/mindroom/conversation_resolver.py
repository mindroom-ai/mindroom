"""Own conversation identity and ingress envelope assembly for inbound turns."""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence  # noqa: TC003
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import nio
from nio.responses import RoomGetEventError

from mindroom.attachments import parse_attachment_ids_from_event_source
from mindroom.constants import HOOK_MESSAGE_RECEIVED_DEPTH_KEY
from mindroom.dispatch_handoff import DispatchEvent, DispatchPayloadMetadata, PreparedTextEvent
from mindroom.matrix.client_delivery import cached_room as matrix_cached_room
from mindroom.matrix.event_info import EventInfo
from mindroom.matrix.identity import MatrixID, extract_agent_name
from mindroom.matrix.media import MatrixMediaEvent, is_audio_message_event, is_image_message_event
from mindroom.matrix.message_content import resolve_event_source_content
from mindroom.matrix.thread_membership import (
    ThreadMembershipAccess,
    resolve_event_thread_id,
    resolve_event_thread_id_best_effort,
    resolve_related_event_thread_id_best_effort,
    thread_messages_thread_membership_access,
)
from mindroom.message_target import MessageTarget
from mindroom.runtime_protocols import SupportsClientConfig  # noqa: TC001
from mindroom.thread_utils import check_agent_mentioned

if TYPE_CHECKING:
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
    requires_full_thread_history: bool = False


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
            return await resolve_event_thread_id_best_effort(
                room.room_id,
                EventInfo.from_event(event.source),
                event_id=event.event_id,
                access=self.thread_membership_access(
                    full_history=False,
                    dispatch_safe=True,
                    caller_label="coalescing_thread_id",
                ),
            )
        except Exception as exc:
            self.deps.logger.debug(
                "Failed to resolve coalescing thread id; continuing room-level",
                room_id=room.room_id,
                event_id=event.event_id,
                error=str(exc),
            )
            return None

    async def _explicit_thread_id_for_event(
        self,
        room_id: str,
        event_id: str | None,
        event_info: EventInfo,
        *,
        full_history: bool,
        dispatch_safe: bool,
        caller_label: str,
    ) -> str | None:
        """Resolve canonical thread membership for one event."""
        return await resolve_event_thread_id(
            room_id,
            event_info,
            event_id=event_id,
            access=self.thread_membership_access(
                full_history=full_history,
                dispatch_safe=dispatch_safe,
                caller_label=caller_label,
            ),
        )

    async def resolve_related_event_thread_id_best_effort(
        self,
        room_id: str,
        related_event_id: str,
        *,
        access: ThreadMembershipAccess,
    ) -> str | None:
        """Return best-effort canonical thread membership for one related target event."""
        return await resolve_related_event_thread_id_best_effort(
            room_id,
            related_event_id,
            access=access,
        )

    def thread_membership_access(
        self,
        *,
        full_history: bool,
        dispatch_safe: bool,
        caller_label: str,
    ) -> ThreadMembershipAccess:
        """Return the shared thread-membership accessors for this resolver."""
        return thread_messages_thread_membership_access(
            lookup_thread_id=self.deps.conversation_cache.get_thread_id_for_event,
            fetch_event_info=self._event_info_for_event_id,
            fetch_thread_messages=lambda room_id, thread_id: self._read_thread_messages(
                room_id,
                thread_id,
                full_history=full_history,
                dispatch_safe=dispatch_safe,
                caller_label=caller_label,
            ),
        )

    async def _read_thread_messages(
        self,
        room_id: str,
        thread_id: str,
        *,
        full_history: bool,
        dispatch_safe: bool,
        caller_label: str,
    ) -> ThreadReadResult:
        """Resolve one thread read through the shared cache entrypoint."""
        return await self.deps.conversation_cache.get_thread_messages(
            room_id,
            thread_id,
            full_history=full_history,
            dispatch_safe=dispatch_safe,
            caller_label=caller_label,
        )

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

    async def derive_conversation_context(
        self,
        room_id: str,
        event_info: EventInfo,
        *,
        event_id: str | None = None,
        caller_label: str = "unknown",
    ) -> tuple[bool, str | None, Sequence[ResolvedVisibleMessage]]:
        """Derive conversation context from canonical Matrix thread membership."""
        is_thread, thread_id, thread_history, _requires_full_thread_history = await self._resolve_thread_context(
            room_id,
            event_id,
            event_info,
            full_history=True,
            dispatch_safe=False,
            caller_label=caller_label,
        )
        return is_thread, thread_id, thread_history

    async def _resolve_thread_context(
        self,
        room_id: str,
        event_id: str | None,
        event_info: EventInfo,
        *,
        full_history: bool,
        dispatch_safe: bool,
        caller_label: str,
    ) -> tuple[bool, str | None, Sequence[ResolvedVisibleMessage], bool]:
        """Resolve one thread context using either snapshot or full history."""
        thread_id = await self._explicit_thread_id_for_event(
            room_id,
            event_id,
            event_info,
            full_history=full_history,
            dispatch_safe=dispatch_safe,
            caller_label=caller_label,
        )
        if thread_id is None:
            return False, None, [], False

        thread_messages = await self._read_thread_messages(
            room_id,
            thread_id,
            full_history=full_history,
            dispatch_safe=dispatch_safe,
            caller_label=caller_label,
        )
        if full_history:
            return True, thread_id, thread_messages, False

        return True, thread_id, thread_messages, not thread_messages.is_full_history

    async def extract_dispatch_context(
        self,
        room: nio.MatrixRoom,
        event: DispatchEvent,
        *,
        payload_metadata: DispatchPayloadMetadata | None = None,
        caller_label: str = "dispatch_context",
    ) -> MessageContext:
        """Extract lightweight routing context without hydrating full thread history."""
        return await self.extract_message_context_impl(
            room,
            event,
            full_history=False,
            dispatch_safe=True,
            payload_metadata=payload_metadata,
            caller_label=caller_label,
        )

    async def extract_trusted_router_relay_context(
        self,
        room: nio.MatrixRoom,
        event: DispatchEvent,
        *,
        payload_metadata: DispatchPayloadMetadata | None = None,
    ) -> MessageContext:
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

        event_info = EventInfo.from_event(resolved_event_source)
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
            resolved_thread_id = event_info.thread_id or event_info.thread_id_from_edit
        return MessageContext(
            am_i_mentioned=am_i_mentioned,
            is_thread=resolved_thread_id is not None,
            thread_id=resolved_thread_id,
            thread_history=(),
            mentioned_agents=mentioned_agents,
            has_non_agent_mentions=has_non_agent_mentions,
            replay_guard_history=(),
            requires_full_thread_history=resolved_thread_id is not None,
        )

    async def extract_message_context(
        self,
        room: nio.MatrixRoom,
        event: DispatchEvent,
        *,
        full_history: bool = True,
        payload_metadata: DispatchPayloadMetadata | None = None,
        caller_label: str = "message_context",
    ) -> MessageContext:
        """Extract message context, optionally using a lightweight thread snapshot."""
        return await self.extract_message_context_impl(
            room,
            event,
            full_history=full_history,
            dispatch_safe=False,
            payload_metadata=payload_metadata,
            caller_label=caller_label,
        )

    async def extract_message_context_impl(
        self,
        room: nio.MatrixRoom,
        event: DispatchEvent,
        *,
        full_history: bool,
        dispatch_safe: bool,
        payload_metadata: DispatchPayloadMetadata | None = None,
        caller_label: str,
    ) -> MessageContext:
        """Resolve event metadata, mentions, and thread history for one inbound turn."""
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
            requires_full_thread_history = False
        else:
            (
                is_thread,
                thread_id,
                thread_history,
                requires_full_thread_history,
            ) = await self._resolve_thread_context(
                room.room_id,
                event.event_id,
                event_info,
                full_history=full_history,
                dispatch_safe=dispatch_safe,
                caller_label=caller_label,
            )

        return MessageContext(
            am_i_mentioned=am_i_mentioned,
            is_thread=is_thread,
            thread_id=thread_id,
            thread_history=thread_history,
            mentioned_agents=mentioned_agents,
            has_non_agent_mentions=has_non_agent_mentions,
            replay_guard_history=thread_history,
            requires_full_thread_history=requires_full_thread_history,
        )

    async def hydrate_dispatch_context(
        self,
        room: nio.MatrixRoom,
        event: DispatchEvent,
        context: MessageContext,
        *,
        payload_metadata: DispatchPayloadMetadata | None = None,
    ) -> None:
        """Replace lightweight thread snapshots with full planning history once a reply is required."""
        if not context.requires_full_thread_history or context.thread_id is None:
            context.requires_full_thread_history = False
            return
        full_context = await self.extract_message_context_impl(
            room,
            event,
            full_history=True,
            dispatch_safe=True,
            payload_metadata=payload_metadata,
            caller_label="dispatch_hydration",
        )
        context.thread_history = full_context.thread_history
        context.is_thread = full_context.is_thread
        context.thread_id = full_context.thread_id
        context.requires_full_thread_history = False

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
        _client: nio.AsyncClient,
        room_id: str,
        thread_id: str,
        *,
        caller_label: str = "unknown",
    ) -> ThreadReadResult:
        """Fetch strict post-lock thread history through the shared conversation-cache policy."""
        return await self._read_thread_messages(
            room_id,
            thread_id,
            full_history=True,
            dispatch_safe=True,
            caller_label=caller_label,
        )

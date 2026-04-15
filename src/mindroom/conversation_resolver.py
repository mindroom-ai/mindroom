"""Own conversation identity and ingress envelope assembly for inbound turns."""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence  # noqa: TC003
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import nio

from mindroom.attachments import parse_attachment_ids_from_event_source
from mindroom.coalescing import PreparedTextEvent
from mindroom.constants import HOOK_MESSAGE_RECEIVED_DEPTH_KEY
from mindroom.matrix.client import cached_room as matrix_cached_room
from mindroom.matrix.event_info import EventInfo
from mindroom.matrix.identity import MatrixID, extract_agent_name
from mindroom.matrix.message_content import resolve_event_source_content
from mindroom.matrix.thread_membership import (
    ThreadMembershipAccess,
    resolve_event_thread_id,
    thread_messages_thread_membership_access,
)
from mindroom.message_target import MessageTarget
from mindroom.thread_utils import check_agent_mentioned

if TYPE_CHECKING:
    import structlog

    from mindroom.bot_runtime_view import BotRuntimeView
    from mindroom.constants import RuntimePaths
    from mindroom.hooks import MessageEnvelope
    from mindroom.matrix.client import ResolvedVisibleMessage
    from mindroom.matrix.conversation_cache import MatrixConversationCache

type TextDispatchEvent = nio.RoomMessageText | PreparedTextEvent
type MediaDispatchEvent = (
    nio.RoomMessageImage
    | nio.RoomEncryptedImage
    | nio.RoomMessageFile
    | nio.RoomEncryptedFile
    | nio.RoomMessageVideo
    | nio.RoomEncryptedVideo
)
type DispatchEvent = TextDispatchEvent | MediaDispatchEvent


def should_skip_mentions(event_source: dict[str, Any]) -> bool:
    """Return whether mentions in this message should be ignored."""
    content = event_source.get("content", {})
    if not isinstance(content, dict):
        return False
    if bool(content.get("com.mindroom.skip_mentions", False)):
        return True

    new_content = content.get("m.new_content")
    return isinstance(new_content, dict) and bool(new_content.get("com.mindroom.skip_mentions", False))


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

    runtime: BotRuntimeView
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

    def _envelope_ingress_metadata(
        self,
        *,
        event: DispatchEvent,
        source_kind: str | None = None,
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
            if isinstance(event, nio.RoomMessageAudio | nio.RoomEncryptedAudio):
                resolved_source_kind = "voice"
            elif isinstance(event, nio.RoomMessageImage | nio.RoomEncryptedImage):
                resolved_source_kind = "image"
            else:
                resolved_source_kind = "message"

        hook_source: str | None = None
        message_received_depth = 0
        if isinstance(content, dict) and source_kind_sender_is_trusted:
            hook_source_override = content.get("com.mindroom.hook_source")
            if isinstance(hook_source_override, str) and hook_source_override:
                hook_source = hook_source_override
            depth_override = content.get(HOOK_MESSAGE_RECEIVED_DEPTH_KEY)
            if isinstance(depth_override, int) and not isinstance(depth_override, bool) and depth_override > 0:
                message_received_depth = depth_override
        return resolved_source_kind, hook_source, message_received_depth

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
    ) -> MessageEnvelope:
        """Build the normalized inbound envelope consumed by message hooks."""
        from mindroom.hooks import MessageEnvelope  # noqa: PLC0415

        config = self.deps.runtime.config
        resolved_source_kind, hook_source, message_received_depth = self._envelope_ingress_metadata(
            event=event,
            source_kind=source_kind,
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
            attachment_ids=tuple(attachment_ids or parse_attachment_ids_from_event_source(event.source)),
            mentioned_agents=tuple(
                agent_id.agent_name(config, self.deps.runtime_paths) or agent_id.username
                for agent_id in context.mentioned_agents
            ),
            agent_name=agent_name or self.deps.agent_name,
            source_kind=resolved_source_kind,
            hook_source=hook_source,
            message_received_depth=message_received_depth,
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
    ) -> MessageEnvelope:
        """Build one lightweight ingress envelope without extracting thread context."""
        from mindroom.hooks import MessageEnvelope  # noqa: PLC0415

        resolved_source_kind, hook_source, message_received_depth = self._envelope_ingress_metadata(
            event=event,
            source_kind=source_kind,
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
            attachment_ids=tuple(attachment_ids or parse_attachment_ids_from_event_source(event.source)),
            mentioned_agents=(),
            agent_name=agent_name or self.deps.agent_name,
            source_kind=resolved_source_kind,
            hook_source=hook_source,
            message_received_depth=message_received_depth,
        )

    async def coalescing_thread_id(
        self,
        room: nio.MatrixRoom,
        event: DispatchEvent,
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
        return await self._explicit_thread_id_for_event(
            room.room_id,
            event.event_id,
            EventInfo.from_event(event.source),
            full_history=False,
            dispatch_safe=True,
        )

    async def _explicit_thread_id_for_event(
        self,
        room_id: str,
        event_id: str | None,
        event_info: EventInfo,
        *,
        full_history: bool,
        dispatch_safe: bool,
    ) -> str | None:
        """Resolve canonical thread membership for one event."""
        return await resolve_event_thread_id(
            room_id,
            event_info,
            event_id=event_id,
            access=self.thread_membership_access(
                full_history=full_history,
                dispatch_safe=dispatch_safe,
            ),
        )

    def thread_membership_access(
        self,
        *,
        full_history: bool,
        dispatch_safe: bool,
    ) -> ThreadMembershipAccess:
        """Return the shared thread-membership accessors for this resolver."""
        fetch_thread_messages = (
            self.deps.conversation_cache.get_dispatch_thread_history
            if full_history and dispatch_safe
            else self.deps.conversation_cache.get_thread_history
            if full_history
            else self.deps.conversation_cache.get_dispatch_thread_snapshot
            if dispatch_safe
            else self.deps.conversation_cache.get_thread_snapshot
        )
        return thread_messages_thread_membership_access(
            lookup_thread_id=self.deps.conversation_cache.get_thread_id_for_event,
            fetch_event_info=self._event_info_for_event_id,
            fetch_thread_messages=fetch_thread_messages,
        )

    async def _event_info_for_event_id(
        self,
        room_id: str,
        event_id: str,
    ) -> EventInfo | None:
        target_event = await self.deps.conversation_cache.get_event(room_id, event_id)
        if not isinstance(target_event, nio.RoomGetEventResponse):
            return None
        return EventInfo.from_event(target_event.event.source)

    async def derive_conversation_context(
        self,
        room_id: str,
        event_info: EventInfo,
        *,
        event_id: str | None = None,
    ) -> tuple[bool, str | None, list[ResolvedVisibleMessage]]:
        """Derive conversation context from canonical Matrix thread membership."""
        is_thread, thread_id, thread_history, _requires_full_thread_history = await self._resolve_thread_context(
            room_id,
            event_id,
            event_info,
            full_history=True,
            dispatch_safe=False,
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
    ) -> tuple[bool, str | None, list[ResolvedVisibleMessage], bool]:
        """Resolve one thread context using either snapshot or full history."""
        thread_id = await self._explicit_thread_id_for_event(
            room_id,
            event_id,
            event_info,
            full_history=full_history,
            dispatch_safe=dispatch_safe,
        )
        if thread_id is None:
            return False, None, [], False

        if full_history:
            fetch_history = (
                self.deps.conversation_cache.get_dispatch_thread_history
                if dispatch_safe
                else self.deps.conversation_cache.get_thread_history
            )
            thread_history = await fetch_history(room_id, thread_id)
            return True, thread_id, list(thread_history), False

        fetch_snapshot = (
            self.deps.conversation_cache.get_dispatch_thread_snapshot
            if dispatch_safe
            else self.deps.conversation_cache.get_thread_snapshot
        )
        snapshot = await fetch_snapshot(room_id, thread_id)
        return True, thread_id, list(snapshot), not snapshot.is_full_history

    async def extract_dispatch_context(
        self,
        room: nio.MatrixRoom,
        event: DispatchEvent,
    ) -> MessageContext:
        """Extract lightweight routing context without hydrating full thread history."""
        return await self.extract_message_context_impl(
            room,
            event,
            full_history=False,
            dispatch_safe=True,
        )

    async def extract_message_context(
        self,
        room: nio.MatrixRoom,
        event: DispatchEvent,
        *,
        full_history: bool = True,
    ) -> MessageContext:
        """Extract message context, optionally using a lightweight thread snapshot."""
        return await self.extract_message_context_impl(
            room,
            event,
            full_history=full_history,
            dispatch_safe=False,
        )

    async def extract_message_context_impl(
        self,
        room: nio.MatrixRoom,
        event: DispatchEvent,
        *,
        full_history: bool,
        dispatch_safe: bool,
    ) -> MessageContext:
        """Resolve event metadata, mentions, and thread history for one inbound turn."""
        resolved_event_source = await resolve_event_source_content(event.source, self._client())
        config = self.deps.runtime.config

        if should_skip_mentions(resolved_event_source):
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
    ) -> list[ResolvedVisibleMessage]:
        """Fetch strict post-lock thread history through the shared conversation-cache policy."""
        return await self.deps.conversation_cache.get_dispatch_thread_history(room_id, thread_id)

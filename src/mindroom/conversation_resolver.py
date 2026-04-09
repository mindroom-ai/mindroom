"""Conversation resolution and envelope assembly for bot dispatch."""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence  # noqa: TC003
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import nio

from mindroom.attachments import parse_attachment_ids_from_event_source
from mindroom.coalescing import PreparedTextEvent
from mindroom.constants import HOOK_MESSAGE_RECEIVED_DEPTH_KEY
from mindroom.matrix.client import fetch_thread_snapshot
from mindroom.matrix.event_info import EventInfo
from mindroom.matrix.identity import MatrixID, extract_agent_name
from mindroom.matrix.message_content import resolve_event_source_content
from mindroom.matrix.reply_chain import (
    ReplyChainCaches,
    derive_conversation_context,
    derive_conversation_target,
)
from mindroom.matrix.room_cache import cached_room
from mindroom.message_target import MessageTarget
from mindroom.thread_utils import check_agent_mentioned

if TYPE_CHECKING:
    import structlog

    from mindroom.bot_runtime_view import BotRuntimeView
    from mindroom.constants import RuntimePaths
    from mindroom.conversation_state_writer import ConversationStateWriter
    from mindroom.hooks import MessageEnvelope
    from mindroom.matrix.client import ResolvedVisibleMessage

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
    requires_full_thread_history: bool = False


@dataclass(frozen=True)
class ConversationResolverDeps:
    """Explicit collaborators for conversation resolution."""

    runtime: BotRuntimeView
    logger: structlog.stdlib.BoundLogger
    runtime_paths: RuntimePaths
    agent_name: str
    matrix_id: MatrixID
    state_writer: ConversationStateWriter


@dataclass
class ConversationResolver:
    """Resolve conversation targets, context, and normalized envelopes."""

    deps: ConversationResolverDeps
    reply_chain: ReplyChainCaches = field(default_factory=ReplyChainCaches)
    turn_thread_cache: ContextVar[dict[str, list[ResolvedVisibleMessage]] | None] = field(
        default_factory=lambda: ContextVar("mindroom_turn_thread_cache", default=None),
    )

    def _client(self) -> nio.AsyncClient:
        client = self.deps.runtime.client
        if client is None:
            msg = "Matrix client is not ready for conversation resolution"
            raise RuntimeError(msg)
        return client

    def _matrix_id(self) -> MatrixID:
        return self.deps.matrix_id

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
        safe_thread_root = EventInfo.from_event(event_source).safe_thread_root if event_source is not None else None
        return MessageTarget.resolve(
            room_id=room_id,
            thread_id=thread_id,
            reply_to_event_id=reply_to_event_id,
            safe_thread_root=safe_thread_root,
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

    async def build_dispatch_envelope(
        self,
        *,
        room: nio.MatrixRoom,
        event: DispatchEvent,
        requester_user_id: str,
    ) -> MessageEnvelope:
        """Build the normalized inbound envelope for one prepared dispatch event."""
        context = await self.extract_dispatch_context(room, event)
        return self.build_message_envelope(
            room_id=room.room_id,
            event=event,
            requester_user_id=requester_user_id,
            context=context,
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
        event_info = EventInfo.from_event(event.source)
        if event_info.thread_id:
            return event_info.thread_id
        if event_info.thread_id_from_edit:
            return event_info.thread_id_from_edit
        if not event_info.has_relations:
            return None
        _, thread_id, _, _ = await self.derive_conversation_target(room.room_id, event_info)
        return thread_id

    async def derive_conversation_context(
        self,
        room_id: str,
        event_info: EventInfo,
    ) -> tuple[bool, str | None, list[ResolvedVisibleMessage]]:
        """Derive conversation context from threads or reply chains."""
        is_thread, thread_id, thread_history = await derive_conversation_context(
            self._client(),
            room_id,
            event_info,
            self.reply_chain,
            self.deps.logger,
            self.fetch_thread_history,
            event_cache=self.deps.runtime.event_cache,
        )
        return is_thread, thread_id, thread_history

    async def derive_conversation_target(
        self,
        room_id: str,
        event_info: EventInfo,
    ) -> tuple[bool, str | None, list[ResolvedVisibleMessage], bool]:
        """Derive dispatch target using lightweight thread snapshots."""
        return await derive_conversation_target(
            self._client(),
            room_id,
            event_info,
            self.reply_chain,
            self.deps.logger,
            fetch_thread_snapshot,
            event_cache=self.deps.runtime.event_cache,
        )

    async def extract_dispatch_context(
        self,
        room: nio.MatrixRoom,
        event: DispatchEvent,
    ) -> MessageContext:
        """Extract lightweight routing context without hydrating full thread history."""
        return await self.extract_message_context(room, event, full_history=False)

    async def extract_message_context(
        self,
        room: nio.MatrixRoom,
        event: DispatchEvent,
        *,
        full_history: bool = True,
    ) -> MessageContext:
        """Extract message context, optionally using a lightweight thread snapshot."""
        return await self.extract_message_context_impl(room, event, full_history=full_history)

    async def extract_message_context_impl(
        self,
        room: nio.MatrixRoom,
        event: DispatchEvent,
        *,
        full_history: bool,
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
        elif full_history:
            is_thread, thread_id, thread_history = await self.derive_conversation_context(
                room.room_id,
                event_info,
            )
            requires_full_thread_history = False
        else:
            (
                is_thread,
                thread_id,
                thread_history,
                requires_full_thread_history,
            ) = await self.derive_conversation_target(room.room_id, event_info)

        return MessageContext(
            am_i_mentioned=am_i_mentioned,
            is_thread=is_thread,
            thread_id=thread_id,
            thread_history=thread_history,
            mentioned_agents=mentioned_agents,
            has_non_agent_mentions=has_non_agent_mentions,
            requires_full_thread_history=requires_full_thread_history,
        )

    async def hydrate_dispatch_context(
        self,
        room: nio.MatrixRoom,
        event: DispatchEvent,
        context: MessageContext,
    ) -> None:
        """Replace lightweight thread snapshots with full history once a reply is required."""
        if not context.requires_full_thread_history or context.thread_id is None:
            context.requires_full_thread_history = False
            return
        full_context = await self.extract_message_context(room, event)
        context.thread_history = full_context.thread_history
        context.is_thread = full_context.is_thread
        context.thread_id = full_context.thread_id
        context.requires_full_thread_history = False

    def cached_room(self, room_id: str) -> nio.MatrixRoom | None:
        """Return room from client cache when available."""
        client = self.deps.runtime.client
        if client is None:
            return None
        return cached_room(client, room_id)

    @asynccontextmanager
    async def turn_thread_cache_scope(self) -> AsyncIterator[None]:
        """Cache thread history for the lifetime of one message-handling turn."""
        existing_cache = self.turn_thread_cache.get()
        if existing_cache is not None:
            yield
            return

        token = self.turn_thread_cache.set({})
        try:
            yield
        finally:
            self.turn_thread_cache.reset(token)

    async def fetch_thread_history(
        self,
        client: nio.AsyncClient,
        room_id: str,
        thread_id: str,
    ) -> list[ResolvedVisibleMessage]:
        """Fetch thread history once per turn for the same room/thread pair."""
        cache = self.turn_thread_cache.get()
        cache_key = f"{room_id}:{thread_id}"
        if cache is not None and cache_key in cache:
            return cache[cache_key]

        thread_history = list(await self.deps.state_writer.fetch_thread_history(client, room_id, thread_id))
        if cache is not None:
            cache[cache_key] = thread_history
        return thread_history

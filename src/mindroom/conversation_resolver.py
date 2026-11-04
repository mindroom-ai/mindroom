"""Own conversation identity and ingress envelope assembly for inbound turns."""

from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from mindroom.attachments.storage import parse_attachment_ids_from_event_source
from mindroom.constants import SKIP_MENTIONS_KEY
from mindroom.dispatch_source import (
    ACTIVE_THREAD_FOLLOW_UP_SOURCE_KIND,
    SILENT_SCHEDULE_SOURCE_KIND,
    content_owns_per_fire_thread_root,
    per_fire_thread_root_event_id_from_content,
    source_kind_from_content,
)
from mindroom.entity_resolution import entity_identity_registry
from mindroom.matrix.client_delivery import cached_room as matrix_cached_room
from mindroom.matrix.conversation_hydration import HYDRATED_PROMPT_WINDOW_MESSAGES
from mindroom.matrix.conversation_reads import (
    ConversationReader,
    ThreadReadMode,
    complete_thread_history,
    consecutive_agent_messages,
    projected_thread_history,
)
from mindroom.matrix.event_info import EventInfo
from mindroom.matrix.message_content import resolve_event_source_content
from mindroom.matrix.thread_diagnostics import is_thread_history_degraded
from mindroom.matrix.thread_history_result import ThreadHistoryResult
from mindroom.matrix.thread_membership import (
    RelatedEventUnavailableError,
    ThreadMembershipAccess,
    ThreadMembershipLookupError,
    ThreadResolution,
    ThreadResolutionState,
    resolve_event_thread_membership,
    resolve_related_event_thread_membership,
    thread_messages_thread_membership_access,
)
from mindroom.message_target import MessageTarget
from mindroom.runtime_protocols import SupportsClientConfig  # noqa: TC001
from mindroom.thread_utils import check_agent_mentioned

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Sequence

    import nio
    import structlog

    from mindroom.constants import RuntimePaths
    from mindroom.dispatch_handoff import DispatchEvent, DispatchPayloadMetadata
    from mindroom.hooks import MessageEnvelope
    from mindroom.matrix.client_visible_messages import ResolvedVisibleMessage
    from mindroom.matrix.identity import MatrixID
    from mindroom.matrix.media import MatrixMediaEvent
    from mindroom.matrix.relation_lookup import RelationLookup
    from mindroom.turns.facts import IngressFacts


def _should_skip_mentions(event_source: dict[str, Any]) -> bool:
    """Return whether mentions in this message should be ignored."""
    content = event_source.get("content", {})
    if not isinstance(content, dict):
        return False
    if bool(content.get(SKIP_MENTIONS_KEY, False)):
        return True

    new_content = content.get("m.new_content")
    return isinstance(new_content, dict) and bool(new_content.get(SKIP_MENTIONS_KEY, False))


def _with_skip_mentions_metadata(content: dict[str, Any], skip_mentions: bool) -> dict[str, Any]:
    content[SKIP_MENTIONS_KEY] = skip_mentions
    new_content = content.get("m.new_content")
    if isinstance(new_content, dict):
        visible_content = dict(new_content)
        if skip_mentions:
            visible_content[SKIP_MENTIONS_KEY] = True
        else:
            visible_content.pop(SKIP_MENTIONS_KEY, None)
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
    """Context extracted from a Matrix message event, in the thread its turn was decided to belong to."""

    am_i_mentioned: bool
    is_thread: bool
    thread_id: str | None
    thread_history: Sequence[ResolvedVisibleMessage]
    mentioned_agents: list[MatrixID]
    has_non_agent_mentions: bool
    requires_model_history_refresh: bool = False

    @property
    def planning_thread_history(self) -> Sequence[ResolvedVisibleMessage]:
        """Return thread history only when it is safe to use for planning decisions."""
        history = self.thread_history
        if (
            not isinstance(history, ThreadHistoryResult)
            or not history.is_full_history
            or is_thread_history_degraded(history)
        ):
            return ()
        return tuple(history)

    @property
    def planning_thread_history_unavailable(self) -> bool:
        """Return whether thread policy history degraded and must not be treated as empty."""
        if not self.is_thread:
            return False
        history = self.thread_history
        if is_thread_history_degraded(history):
            return True
        if isinstance(history, ThreadHistoryResult):
            return not history.is_full_history
        # Plain sequences have no completeness signal, so visible content must not masquerade as policy-grade history.
        return self.requires_model_history_refresh or bool(history)


@dataclass(frozen=True)
class ConversationResolverDeps:
    """Explicit collaborators for conversation resolution."""

    runtime: SupportsClientConfig
    logger: structlog.stdlib.BoundLogger
    runtime_paths: RuntimePaths
    agent_name: str
    matrix_id: MatrixID
    conversation_reader: ConversationReader
    relations: RelationLookup


@dataclass
class ConversationResolver:
    """Resolve explicit thread context, history, mentions, and ingress envelopes."""

    deps: ConversationResolverDeps

    def room_is_one_conversation(self, room_id: str) -> bool:
        """Return whether this agent treats the whole room as one conversation, threads included."""
        return (
            self.deps.runtime.config.get_entity_thread_mode(
                self.deps.agent_name,
                self.deps.runtime_paths,
                room_id=room_id,
            )
            == "room"
        )

    def _client(self) -> nio.AsyncClient:
        client = self.deps.runtime.client
        if client is None:
            msg = "Matrix client is not ready for conversation resolution"
            raise RuntimeError(msg)
        return client

    def _mention_facts(
        self,
        event_source: dict[str, Any],
        *,
        room: nio.MatrixRoom,
    ) -> tuple[list[MatrixID], bool, bool]:
        """Return mention facts from one already-normalized event source."""
        if _should_skip_mentions(event_source):
            return [], False, False
        return check_agent_mentioned(
            event_source,
            self.deps.matrix_id,
            self.deps.runtime.config,
            self.deps.runtime_paths,
            room=room,
        )

    def _mentioned_agent_names(self, mentioned_agents: Sequence[MatrixID]) -> tuple[str, ...]:
        """Return canonical entity names for mentioned managed users."""
        registry = entity_identity_registry(self.deps.runtime.config, self.deps.runtime_paths)
        return tuple(registry.entity_name_or_username(agent_id) for agent_id in mentioned_agents)

    def pre_hydration_mentions(
        self,
        *,
        room: nio.MatrixRoom,
        event: DispatchEvent,
        payload_metadata: DispatchPayloadMetadata | None = None,
    ) -> tuple[list[MatrixID], bool]:
        """Return whom one event mentions and whether it mentions this entity, without reading conversation history."""
        mentioned_agents, am_i_mentioned, _has_non_agent_mentions = self._mention_facts(
            _source_with_payload_metadata(event.source, payload_metadata),
            room=room,
        )
        return mentioned_agents, am_i_mentioned

    def _sender_is_managed_entity(self, user_id: str) -> bool:
        """Return whether one Matrix user ID belongs to a managed entity."""
        registry = entity_identity_registry(self.deps.runtime.config, self.deps.runtime_paths)
        return registry.current_entity_name_for_user_id(user_id) is not None

    def _is_trusted_automation_fire(self, event_source: dict[str, Any]) -> bool:
        """Return whether one event is a per-fire automation delivery from a managed entity."""
        content = event_source.get("content")
        if not isinstance(content, dict) or not content_owns_per_fire_thread_root(content):
            return False
        sender = event_source.get("sender")
        return isinstance(sender, str) and self._sender_is_managed_entity(sender)

    def _trusted_automation_fire_root_event_id(
        self,
        event_source: dict[str, Any],
        event_info: EventInfo,
        *,
        fallback_root_event_id: str | None,
    ) -> str | None:
        """Return the explicit root for one trusted per-fire automation delivery."""
        if not self._is_trusted_automation_fire(event_source):
            return None
        content = event_source.get("content")
        relayed_root_event_id = (
            per_fire_thread_root_event_id_from_content(content) if isinstance(content, dict) else None
        )
        if relayed_root_event_id is not None:
            return relayed_root_event_id
        if event_info.thread_id is not None:
            return event_info.thread_id
        return fallback_root_event_id if event_info.can_be_thread_root else None

    def _is_trusted_room_level_silent_schedule(
        self,
        event_source: dict[str, Any],
        event_info: EventInfo,
        *,
        thread_id: str | None,
    ) -> bool:
        """Return whether a hidden schedule trigger must stay at room level."""
        if thread_id is not None or event_info.thread_id is not None:
            return False
        content = event_source.get("content")
        if not isinstance(content, dict) or source_kind_from_content(content) != SILENT_SCHEDULE_SOURCE_KIND:
            return False
        sender = event_source.get("sender")
        return isinstance(sender, str) and self._sender_is_managed_entity(sender)

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
        automation_fire_root = False
        room_level_silent_schedule = False
        if event_source is not None:
            event_info = EventInfo.from_event(event_source)
            room_level_silent_schedule = self._is_trusted_room_level_silent_schedule(
                event_source,
                event_info,
                thread_id=thread_id,
            )
            if event_info.can_be_thread_root and reply_to_event_id is not None and not room_level_silent_schedule:
                thread_start_root_event_id = reply_to_event_id
            automation_root_event_id = self._trusted_automation_fire_root_event_id(
                event_source,
                event_info,
                fallback_root_event_id=thread_start_root_event_id,
            )
            if automation_root_event_id is not None:
                thread_start_root_event_id = automation_root_event_id
                automation_fire_root = True
        return MessageTarget.resolve(
            room_id=room_id,
            thread_id=thread_id,
            reply_to_event_id=None if room_level_silent_schedule else reply_to_event_id,
            thread_start_root_event_id=thread_start_root_event_id,
            room_mode=room_level_silent_schedule or (effective_thread_mode == "room" and not automation_fire_root),
        )

    def build_envelope(
        self,
        *,
        event: DispatchEvent,
        facts: IngressFacts,
        target: MessageTarget,
        mentioned_agents: Sequence[MatrixID] = (),
        attachment_ids: Sequence[str] | None = None,
        body: str | None = None,
        active_follow_up: bool = False,
    ) -> MessageEnvelope:
        """Build the normalized inbound envelope consumed by message hooks from one turn's classified provenance."""
        from mindroom.hooks import MessageEnvelope  # noqa: PLC0415

        return MessageEnvelope(
            source_event_id=event.event_id,
            target=target,
            body=body or event.body,
            attachment_ids=tuple(
                attachment_ids if attachment_ids is not None else parse_attachment_ids_from_event_source(event.source),
            ),
            mentioned_agents=self._mentioned_agent_names(mentioned_agents),
            agent_name=self.deps.agent_name,
            hook_source=facts.hook_source,
            message_received_depth=facts.message_received_depth,
            dispatch_policy_source_kind=ACTIVE_THREAD_FOLLOW_UP_SOURCE_KIND if active_follow_up else None,
            origin=facts.origin,
        )

    async def coalescing_thread_id(
        self,
        room: nio.MatrixRoom,
        event: DispatchEvent | MatrixMediaEvent,
    ) -> str | None:
        """Return the coalescing thread scope for one inbound event.

        Raises ``RelatedEventUnavailableError`` when the event's relation
        target is one the homeserver will not serve, and
        ``ThreadMembershipLookupError`` when the scope is unknown for now.
        """
        event_info = EventInfo.from_event(event.source)
        if self.room_is_one_conversation(room.room_id):
            return self._trusted_automation_fire_root_event_id(
                event.source,
                event_info,
                fallback_root_event_id=event.event_id,
            )
        resolution = await self._coalescing_thread_resolution(
            room,
            event,
            event_info,
            mode=ThreadReadMode.NONBLOCKING,
        )
        if resolution.state is ThreadResolutionState.INDETERMINATE:
            # A non-blocking read cannot prove a root in a conversation it has
            # never hydrated, and a plain reply to an unthreaded message is the
            # ordinary way to reach one. This is the one decision of the turn's
            # thread -- the batch is formed under it and dispatch never revisits
            # it -- so an unproven candidate gets the strict walk here, which
            # `ensure_hydrated` shares per conversation.
            resolution = await self._coalescing_thread_resolution(
                room,
                event,
                event_info,
                mode=ThreadReadMode.STRICT,
            )
        if resolution.state is ThreadResolutionState.THREADED:
            return resolution.thread_id
        if resolution.state is ThreadResolutionState.ROOM_LEVEL:
            return None
        msg = f"Could not resolve canonical coalescing thread for {event.event_id}"
        if isinstance(resolution.error, RelatedEventUnavailableError):
            # The event names a relation target the homeserver will not serve,
            # so no retry can place it either.
            raise RelatedEventUnavailableError(msg) from resolution.error
        if resolution.error is not None:
            raise ThreadMembershipLookupError(msg) from resolution.error
        raise ThreadMembershipLookupError(msg)

    async def _coalescing_thread_resolution(
        self,
        room: nio.MatrixRoom,
        event: DispatchEvent | MatrixMediaEvent,
        event_info: EventInfo,
        *,
        mode: ThreadReadMode,
    ) -> ThreadResolution:
        """Resolve one event's coalescing membership under one read mode."""
        msg = f"Could not resolve canonical coalescing thread for {event.event_id}"
        try:
            return await resolve_event_thread_membership(
                room.room_id,
                event_info,
                access=self._thread_membership_access(
                    mode=mode,
                    requires_complete_history=True,
                ),
            )
        except RelatedEventUnavailableError as exc:
            raise RelatedEventUnavailableError(msg) from exc
        except Exception as exc:
            raise ThreadMembershipLookupError(msg) from exc

    async def resolve_related_event_thread_id_dispatch_snapshot_best_effort(
        self,
        room_id: str,
        related_event_id: str,
    ) -> str | None:
        """Return thread membership from local state without waiting on the homeserver."""
        resolution = await resolve_related_event_thread_membership(
            room_id,
            related_event_id,
            access=self._thread_membership_access(
                mode=ThreadReadMode.NONBLOCKING,
                # Reaction hook context wants the target's thread, not its
                # conversation, so an incomplete page answers it just as well.
                requires_complete_history=False,
            ),
        )
        return resolution.thread_id

    def _thread_membership_access(
        self,
        *,
        mode: ThreadReadMode,
        requires_complete_history: bool,
    ) -> ThreadMembershipAccess:
        """Return the shared thread-membership accessors for this resolver.

        ``requires_complete_history`` has no default on purpose. A non-blocking
        read never waits, so an unhydrated conversation can only report that it
        does not know whether it holds everything. Passing ``False`` is a claim
        that the caller is content with whatever is already local, and that has
        to be written down at the call site rather than inherited by accident.
        """
        return thread_messages_thread_membership_access(
            lookup_thread_id=self.deps.relations.thread_id,
            fetch_event_info=self.deps.relations.event_info,
            fetch_thread_messages=lambda room_id, thread_id: self._read_thread_messages(
                room_id,
                thread_id,
                mode=mode,
                requires_complete_history=requires_complete_history,
            ),
            # A turn has to be answered. A reply to a message whose replies this
            # bot cannot read is answered as a reply to a plain message, which
            # starts a thread of its own, rather than held back until history
            # nobody will ever decrypt turns up.
            absence_needs_full_history=False,
        )

    async def _read_thread_messages(
        self,
        room_id: str,
        thread_id: str,
        *,
        mode: ThreadReadMode,
        requires_complete_history: bool = True,
    ) -> ThreadHistoryResult:
        """Resolve one thread read against the conversation projection.

        There are two read contracts because there were only ever two
        questions. A caller assembling a prompt must not be handed a
        conversation with a message missing from it, so it waits for the
        server; a caller serving a UI or a hook must not block on a homeserver,
        so it takes whatever is already known.

        The page is bounded by the window hydration guarantees, which is far
        above what any consumer renders -- teams cut to thirty messages -- so
        the bound removes work rather than context.
        """
        reader = self.deps.conversation_reader
        # A strict read feeds a prompt or a root proof, both of which are wrong
        # when a message is missing, so it blocks. A non-blocking read runs
        # before the turn is accepted, so it never waits on the homeserver.
        if mode is ThreadReadMode.STRICT:
            return await complete_thread_history(reader, room_id, thread_id)
        # A non-blocking read is still complete when the conversation was
        # already hydrated and nothing is pending -- completeness is a property
        # of what is known, not of how hard the caller was willing to work for
        # it. Claiming otherwise would send every dispatch through a redundant
        # strict re-read of a conversation it had already proven whole.
        source_degraded = requires_complete_history and await reader.may_have_unread_history(
            room_id=room_id,
            thread_id=thread_id,
        )
        page = await reader.read(
            room_id=room_id,
            thread_id=thread_id,
            limit=HYDRATED_PROMPT_WINDOW_MESSAGES,
        )
        # Two independent ways this page can fall short of the conversation.
        # `source_degraded` means local state might not have caught up yet.
        # Hydration stopping at a ceiling is not ruled out by anything a caller
        # can do: the walk already ran, it already installed its marker, and it
        # will not run again under this membership. Only the recorded flag
        # distinguishes a page that ends because the conversation does from one
        # that ends because the walk ran out of allowance.
        hydration_truncated = await reader.hydration_was_truncated(room_id=room_id, thread_id=thread_id)
        return projected_thread_history(
            page,
            complete=not hydration_truncated and not source_degraded,
            source_degraded=source_degraded,
        )

    async def _message_context(
        self,
        room: nio.MatrixRoom,
        event: DispatchEvent | MatrixMediaEvent,
        *,
        thread_id: str | None,
        thread_history: ThreadHistoryResult | None,
        payload_metadata: DispatchPayloadMetadata | None,
    ) -> MessageContext:
        """Assemble one context from the event's mentions and its thread's history, which is ``None`` while deferred."""
        resolved_event_source = await resolve_event_source_content(event.source, self._client())
        resolved_event_source = _source_with_payload_metadata(resolved_event_source, payload_metadata)
        mentioned_agents, am_i_mentioned, has_non_agent_mentions = self._mention_facts(
            resolved_event_source,
            room=room,
        )
        if am_i_mentioned:
            self.deps.logger.info("Mentioned", event_id=event.event_id, room_id=room.room_id)
        return MessageContext(
            am_i_mentioned=am_i_mentioned,
            is_thread=thread_id is not None,
            thread_id=thread_id,
            thread_history=() if thread_history is None else thread_history,
            mentioned_agents=mentioned_agents,
            has_non_agent_mentions=has_non_agent_mentions,
            requires_model_history_refresh=(
                thread_id is not None and (thread_history is None or not thread_history.is_full_history)
            ),
        )

    async def extract_dispatch_context(
        self,
        room: nio.MatrixRoom,
        event: DispatchEvent | MatrixMediaEvent,
        *,
        thread_id: str | None,
        payload_metadata: DispatchPayloadMetadata | None = None,
    ) -> MessageContext:
        """Extract dispatch context in the thread this turn was decided to belong to.

        The thread is an input and is never resolved again here: ingress
        decided it once, formed the batch under it, and persisted it for
        replay, and the target says which thread the source is in under the
        room's thread mode. A non-blocking read that degrades is repeated
        strictly, because planning must not take a partial read for the thread.
        """
        history: ThreadHistoryResult | None = None
        if thread_id is not None:
            snapshot = await self.dispatch_thread_snapshot(room.room_id, thread_id)
            history = (
                await self.fetch_thread_history(room.room_id, thread_id)
                if is_thread_history_degraded(snapshot)
                else snapshot
            )
        return await self._message_context(
            room,
            event,
            thread_id=thread_id,
            thread_history=history,
            payload_metadata=payload_metadata,
        )

    async def extract_trusted_router_relay_context(
        self,
        room: nio.MatrixRoom,
        event: DispatchEvent,
        *,
        thread_id: str | None,
        payload_metadata: DispatchPayloadMetadata | None = None,
    ) -> MessageContext:
        """Extract minimal context for a router relay, deferring its thread read until after the lock."""
        return await self._message_context(
            room,
            event,
            thread_id=thread_id,
            thread_history=None,
            payload_metadata=payload_metadata,
        )

    async def extract_message_context(
        self,
        room: nio.MatrixRoom,
        event: DispatchEvent,
        *,
        thread_id: str | None,
    ) -> MessageContext:
        """Extract strict context in a recorded thread, whatever the room's thread mode is now."""
        history = None if thread_id is None else await self.fetch_thread_history(room.room_id, thread_id)
        return await self._message_context(
            room,
            event,
            thread_id=thread_id,
            thread_history=history,
            payload_metadata=None,
        )

    def cached_room(self, room_id: str) -> nio.MatrixRoom | None:
        """Return room from client cache when available."""
        client = self.deps.runtime.client
        if client is None:
            return None
        return matrix_cached_room(client, room_id)

    @asynccontextmanager
    async def turn_lookup_scope(self) -> AsyncIterator[None]:
        """Memoize related-event lookups for the lifetime of one inbound turn."""
        async with self.deps.relations.turn_scope():
            yield

    async def dispatch_thread_snapshot(
        self,
        room_id: str,
        thread_id: str,
    ) -> ThreadHistoryResult:
        """Read one thread without waiting for the homeserver.

        For callers that run before a turn is accepted and must not block on
        Matrix to decide whether to look at it at all.
        """
        return await self._read_thread_messages(
            room_id,
            thread_id,
            mode=ThreadReadMode.NONBLOCKING,
        )

    async def fetch_thread_history(
        self,
        room_id: str,
        thread_id: str,
    ) -> ThreadHistoryResult:
        """Fetch complete thread history from the conversation projection."""
        return await self._read_thread_messages(
            room_id,
            thread_id,
            mode=ThreadReadMode.STRICT,
        )

    async def consecutive_agent_messages(self, room_id: str, thread_id: str | None, *, limit: int) -> int:
        """Count the newest agent or team messages in one conversation, looking back at most ``limit``."""
        return await consecutive_agent_messages(
            self.deps.conversation_reader,
            self.deps.runtime.config,
            self.deps.runtime_paths,
            room_id=room_id,
            thread_id=thread_id,
            limit=limit,
        )
